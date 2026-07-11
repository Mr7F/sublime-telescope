from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
import os
import sublime
from sublime import View, Window
import re
import shutil
import sublime_plugin
import threading
import time
import sys
import subprocess

from .utils import debounced


PANEL_NAME = "telescope"


@dataclass
class SearchResult:
    path: str
    line_number: int
    # Position of the match in the line
    line_position: tuple[int, int]
    line_content: str


@dataclass
class SearchState:
    globs: str = ""
    current_file: bool = False
    text: str = ""
    highlight_index: int = 0
    results: list[SearchResult] = field(default_factory=list)
    # The io panel views and input listener, set while the search is open
    output_view: View | None = None
    input_view: View | None = None
    listener: InputListener | None = None
    # The view opened to preview the highlighted result
    preview: View | None = None
    # Result waiting for the preview file to finish loading
    loading_preview: SearchResult | None = None
    # Processes of the search in progress
    processes: tuple[subprocess.Popen, ...] = ()
    # State of the window before the search, restored when canceling
    active_view: View | None = None
    view_states: dict[View, tuple[list[sublime.Region], tuple[float, float]]] = field(
        default_factory=dict
    )


search_state: defaultdict[Window, SearchState] = defaultdict(SearchState)


class TelescopeCommand(sublime_plugin.WindowCommand):
    """Open the search io panel."""

    def run(self, globs: str | None = None, current_file: bool = False):
        window = self.window
        _save_window_state(window)
        _set_search_text_from_selection(window)

        state = search_state[window]
        if globs is not None:
            normalized_globs = _normalize_globs(globs)
            if normalized_globs != state.globs:
                state.highlight_index = 0
            state.globs = normalized_globs
        state.current_file = current_file

        _close_panel(window)
        output_view, input_view = window.create_io_panel(
            PANEL_NAME,
            lambda text: window.run_command("telescope_confirm"),
        )
        output_view.set_read_only(True)
        output_view.settings().set("gutter", False)
        output_view.settings().set("word_wrap", False)
        output_view.assign_syntax("scope:text.telescope")
        input_view.settings().set("telescope_input", True)
        state.output_view = output_view
        state.input_view = input_view

        window.run_command("show_panel", {"panel": "output." + PANEL_NAME})
        window.focus_view(input_view)

        missing_tools = [tool for tool in ("rg", "fzf") if not shutil.which(tool)]
        if missing_tools:
            _set_panel_text(
                output_view,
                "Missing dependencies: {} (see the readme to install them)".format(
                    ", ".join(missing_tools)
                ),
            )
            return

        if state.text:
            # Restore the previous search, but select the text
            input_view.run_command("select_all")
            input_view.run_command("insert", {"characters": state.text})
            input_view.run_command("select_all")

        # Attach after setting the text to only search once, below
        state.listener = InputListener(window)
        state.listener.attach(input_view.buffer())

        if state.text:
            _search(window, state.text)

    def input(self, args: dict[str, Any]) -> sublime_plugin.InputHandler | None:
        return None


class TelescopeSetGlobsCommand(sublime_plugin.WindowCommand):
    """Ask for globs while the project search input panel is open."""

    def run(self, globs: str | None = None):
        print("telescope_set_globs")
        state = search_state[self.window]
        if not state.input_view or state.current_file:
            return

        if globs is None:
            self.window.run_command(
                "show_overlay",
                {
                    "overlay": "command_palette",
                    "command": "telescope_set_globs",
                },
            )
            return

        normalized_globs = _normalize_globs(globs)
        if normalized_globs != state.globs:
            state.highlight_index = 0
        state.globs = normalized_globs
        self.window.focus_view(state.input_view)
        _search(self.window, state.text)

    def input(self, args: dict[str, Any]) -> sublime_plugin.InputHandler | None:
        state = search_state[self.window]
        if "globs" in args or not state.input_view or state.current_file:
            return None
        return GlobsInputHandler(state.globs)

    def is_enabled(self) -> bool:
        state = search_state[self.window]
        return bool(state.input_view and not state.current_file)


class GlobsInputHandler(sublime_plugin.TextInputHandler):
    def __init__(self, initial_value: str):
        self._initial_value = initial_value

    def name(self) -> str:
        return "globs"

    def placeholder(self) -> str:
        return ".py, .js, views/*.html"

    def validate(self, text: str) -> bool:
        return not text or bool(text.strip())

    def description(self, value: str) -> str:
        return value or "All"

    def initial_text(self) -> str:
        return self._initial_value

    def initial_selection(self) -> list[tuple[int, int]]:
        return [(0, len(self._initial_value))]


class TelescopeMoveCommand(sublime_plugin.WindowCommand):
    """Move the highlighted result, bound to up/down in the io panel input."""

    def run(self, forward: bool):
        state = search_state[self.window]
        if not state.results:
            return
        step = 1 if forward else -1
        state.highlight_index = max(
            0, min(state.highlight_index + step, len(state.results) - 1)
        )
        _highlight_result_in_output_panel(self.window)


class TelescopeConfirmCommand(sublime_plugin.WindowCommand):
    """Open the highlighted result, executed when pressing enter."""

    def run(self):
        state = search_state[self.window]
        if not state.results:
            return
        _erase_result_regions(self.window)
        result = state.results[state.highlight_index]
        _close_panel(self.window)
        # TODO: keep transient view if possible
        self.window.open_file(
            _encoded_position(result),
            flags=sublime.SEMI_TRANSIENT | sublime.ENCODED_POSITION,
        )


class TelescopeCancelCommand(sublime_plugin.WindowCommand):
    """Close the search and restore the window state, bound to escape."""

    def run(self):
        _erase_result_regions(self.window)
        _close_panel(self.window)
        _reset_window_state(self.window)


class InputListener(sublime_plugin.TextChangeListener):
    """Live search while typing in the io panel input."""

    def __init__(self, window: Window):
        super().__init__()
        self.window = window

    @classmethod
    def is_applicable(cls, buffer: sublime.Buffer) -> bool:
        # Only attached manually on the io panel input
        return False

    @debounced
    def on_text_changed(self, changes: list[sublime.TextChange]):
        if not self.is_attached():
            return
        view = self.buffer.primary_view()
        _search(self.window, view.substr(sublime.Region(0, view.size())))


class TelescopeEventListener(sublime_plugin.EventListener):
    def on_load(self, view: View):
        state = search_state[view.window()]
        if view == state.preview and state.loading_preview:
            _add_result_region(view, state.loading_preview)
            state.loading_preview = None

    def on_pre_close_window(self, window: Window):
        search_state.pop(window, None)


def _search(window: Window, text: str):
    state = search_state[window]
    if text != state.text:
        # New search, restart from the first result
        state.highlight_index = 0
    state.text = text
    _kill_search(window)
    # Search in the background, it can lag on big projects
    threading.Thread(
        target=_search_in_background, args=(window, text), daemon=True
    ).start()


def _search_in_background(window: Window, text: str):
    start = time.time()
    results = _live_search(window, text)

    def show_results():
        state = search_state[window]
        if text != state.text or not state.output_view:
            # The search changed or was closed in the meantime
            return
        state.results = results
        state.highlight_index = min(state.highlight_index, max(len(results) - 1, 0))
        _render_results(window)
        if len(text) >= _settings().get("min_query_length", 3):
            window.status_message(
                "Telescope: {} results in {:.2f}s".format(
                    len(results), time.time() - start
                )
            )

    sublime.set_timeout(show_results)


def _render_results(window: Window):
    state = search_state[window]
    _set_panel_text(
        state.output_view,
        "\n".join(
            f"{_relative_path(window, s.path)}:{s.line_number}: {s.line_content}"
            for s in state.results
        ),
    )
    _highlight_result_in_output_panel(window)


def _set_panel_text(view: View, text: str):
    view.set_read_only(False)
    view.run_command("select_all")
    view.run_command("right_delete")
    view.run_command("append", {"characters": text})
    view.set_read_only(True)


def _highlight_result_in_output_panel(window: Window):
    state = search_state[window]
    view = state.output_view

    if not state.results:
        view.erase_regions("telescope-selected")
        return

    line = view.line(view.text_point(state.highlight_index, 0))
    view.add_regions(
        "telescope-selected",
        [line],
        icon="",
        scope="comment | region.yellowish",
    )
    # Defer, the panel is not laid out yet when it just opened
    sublime.set_timeout(lambda: _center_highlight(view, line))
    _preview_result(window, state.results[state.highlight_index])
    # The preview steals the focus when it opens a new file
    window.focus_view(state.input_view)


def _center_highlight(view: View, line: sublime.Region):
    """Center the highlighted line, scrolling by whole lines.

    `show` and `show_at_center` can leave the line half visible on the
    edges, especially when the panel is not laid out yet.
    """
    line_height = view.line_height()
    half_screen = view.viewport_extent()[1] // (2 * line_height) * line_height
    top = view.text_to_layout(line.begin())[1]
    view.set_viewport_position((0, max(0.0, top - half_screen)), animate=False)


def _preview_result(window: Window, result: SearchResult):
    """Preview the file in a tab while navigating the result."""
    state = search_state[window]
    # Transient: sublime keeps a single preview per group and replaces
    # it automatically when previewing an other file. The encoded
    # position moves the cursor, even if the file is still loading
    state.preview = window.open_file(
        _encoded_position(result),
        flags=sublime.TRANSIENT | sublime.ENCODED_POSITION,
    )
    if state.preview.is_loading():
        state.loading_preview = result
    else:
        _add_result_region(state.preview, result)


def _encoded_position(result: SearchResult) -> str:
    return "{}:{}:{}".format(
        result.path, result.line_number, result.line_position[0] + 1
    )


def _add_result_region(view: View, result: SearchResult):
    """Highlight the result in the preview file we opened."""
    line_a = view.text_point(result.line_number - 1, result.line_position[0])
    view.add_regions(
        "telescope-result-view",
        [
            sublime.Region(
                line_a,
                line_a - result.line_position[0] + result.line_position[1],
            )
        ],
        icon="",
        scope="comment | region.yellowish",
    )


def _erase_result_regions(window: Window):
    for view in window.views(include_transient=True):
        view.erase_regions("telescope-result-view")


def _close_panel(window: Window):
    state = search_state[window]
    _kill_search(window)
    if state.listener and state.listener.is_attached():
        state.listener.detach()
    if state.output_view:
        window.destroy_output_panel(PANEL_NAME)
    state.output_view = state.input_view = state.listener = None


def _normalize_globs(globs: str) -> str:
    return ", ".join(g.strip() for g in globs.split(","))


def _close_preview(window: Window):
    state = search_state[window]
    preview = state.preview
    state.preview = None
    state.loading_preview = None
    if not preview or not preview.is_valid() or preview == state.active_view:
        return

    sheet = preview.sheet()
    if sheet and (sheet.is_transient() or sheet.is_semi_transient()):
        sheet.close()


def _save_window_state(window: Window):
    state = search_state[window]
    state.active_view = window.active_view()
    state.view_states = {
        view: (list(view.sel()), view.viewport_position()) for view in window.views()
    }


def _reset_window_state(window: Window):
    """Reset the initial state (cursor position, file opened, scroll position, etc.)."""
    state = search_state[window]
    if state.active_view:
        window.focus_view(state.active_view)

    _close_preview(window)

    for view, (sel, viewport_position) in state.view_states.items():
        if not view.is_valid():
            continue
        view.sel().clear()
        view.sel().add_all(sel)
        view.set_viewport_position(viewport_position, animate=False)


def _set_search_text_from_selection(window: Window):
    view = window.active_view()
    if not view:
        return

    for region in view.sel():
        selected_text = view.substr(region).strip()
        if selected_text:
            search_state[window].text = selected_text
            return


def _live_search(window: Window, search_query: str) -> list[SearchResult]:
    settings = _settings()
    if len(search_query) < settings.get("min_query_length", 3):
        return []

    rg_cmd = [
        "rg",
        "--no-heading",
        "--max-filesize",
        str(settings.get("max_filesize", "100M")),
        "--max-count",
        "10000",
        "--follow",
        "--with-filename",
        "--line-number",
        "--smart-case",
        *settings.get("rg_args", []),
        "-e",
        ".*".join(map(re.escape, search_query)),
    ]

    state = search_state[window]
    view = state.active_view or window.active_view()

    if state.current_file:
        if not view or not view.file_name():
            return []
        rg_cmd.append(view.file_name())
    else:
        if view:
            # The `--iglob` when is negated is done in addition to the
            # default filter (.gitignore, etc)
            exclude_patterns = view.settings().get("binary_file_patterns") or []
            exclude_patterns += view.settings().get("file_exclude_patterns") or []
            exclude_patterns += [
                f"**/{f}**/"
                for f in view.settings().get("folder_exclude_patterns") or []
            ]
            for glob in exclude_patterns:
                glob = re.sub(r"\*+", "**", glob)
                rg_cmd.extend(("--iglob", f"!**/*{glob}"))

        for glob in state.globs.split(","):
            glob = glob.strip()
            if not glob:
                continue
            glob = re.sub(r"\*+", "**", glob)
            # `--type` exist, but it works only for a fixed list of types
            # mimic sublime text glob logic
            rg_cmd.extend(("--iglob", f"**/*{glob}"))

        rg_cmd += window.folders()

    if settings.get("debug"):
        print(" ".join(rg_cmd))

    rg_process = _create_process(rg_cmd)
    fzf_process = _create_process(
        ["fzf", "--filter", search_query],
        stdin=rg_process.stdout,
    )
    rg_process.stdout.close()
    state.processes = (rg_process, fzf_process)
    results = []
    for _ in range(settings.get("max_results", 50)):
        line = fzf_process.stdout.readline().strip()
        if not line:
            break

        path, line_number, content = _parse_rg_result(line)
        to_trim = next((i for i, s in enumerate(content) if s.strip()), 0)
        content = content.strip()

        results.append(
            SearchResult(
                path,
                int(line_number),
                (to_trim, to_trim + len(content)),
                content[:200],
            )
        )

    rg_process.terminate()
    fzf_process.terminate()

    return results


def _kill_search(window: Window):
    for process in search_state[window].processes:
        process.terminate()


def _settings() -> sublime.Settings:
    return sublime.load_settings("Telescope.sublime-settings")


def _relative_path(window: Window, path: str) -> str:
    """Convert the absolute path returned by the CLI tools to relative based on the current folders."""
    for folder in window.folders():
        if path.startswith(folder + os.sep):
            return path[len(folder) + 1 :]
    return path


def _parse_rg_result(result: str) -> tuple[str, str, str]:
    if sys.platform.startswith("win"):
        drive, path, line_number, content = result.split(":", 3)
        path = drive + ":" + path
        return path, line_number, content
    return tuple(result.split(":", 2))


def _create_process(
    args: list[str],
    stdin: Any | None = None,
) -> subprocess.Popen:
    cmd_args: dict[str, Any] = {}
    if sys.platform.startswith("win"):
        CREATE_NO_WINDOW = 0x08000000
        cmd_args["creationflags"] = CREATE_NO_WINDOW
    if stdin is not None:
        cmd_args["stdin"] = stdin
    return subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        # Never read, a filling stderr pipe would block the process
        stderr=subprocess.DEVNULL,
        text=True,
        shell=False,
        **cmd_args,
    )
