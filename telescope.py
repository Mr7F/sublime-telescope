from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any
import sublime
from sublime import View, Window
import re
import sublime_plugin
import time
import sys
import subprocess


# LSP "Go to symbols" has a similar feature
# Hope this issue is fixed one day...
# > https://github.com/sublimehq/sublime_text/issues/4796
from .utils import DynamicListInputHandler


SearchContext = tuple[Any, ...]
RgResult = tuple[str, str, str]


@dataclass
class SearchResult:
    path: str
    line_number: int
    # Position of the match in the line
    line_position: tuple[int, int]
    # Region in the IO panel
    line_content: str


@dataclass
class WindowState:
    """State of the window before executing the command.

    Allows restoring the state when canceling the search.
    """
    active_view: View | None
    view_sel: dict[View, list[sublime.Region]]
    view_viewport: dict[View, tuple[float, float]]


@dataclass
class SearchState:
    globs: str = ""
    highlight_index: int = -1
    text: str = ""
    context: SearchContext | None = None


regions_to_add: dict[Window, tuple[View, list[SearchResult], int]] = {}
window_state: dict[Window, WindowState] = {}
preview_panels: dict[Window, View] = {}

search_state: defaultdict[Window, SearchState] = defaultdict(SearchState)
select_search_text_on_open: defaultdict[Window, bool] = defaultdict(bool)

search_results: list[SearchResult] = []


class TelescopeCommand(sublime_plugin.WindowCommand):
    """Executed on the output panel, set the result in the view."""

    def run(self, result: str, globs: str = "", current_file: bool = False):
        for view in self.window.views(include_transient=True):
            view.erase_regions("telescope-result-view")
        s = search_results[int(result.split(":", 1)[0])]
        # TODO: keep transient view if possible
        self.window.open_file(s.path, flags=sublime.SEMI_TRANSIENT)

    def input(self, args: dict[str, Any]) -> sublime_plugin.InputHandler:
        # Always set the input to see the breadcrumb item
        # when reloading the command input with the hack in `utils.py`
        if "result" not in args and "globs" not in args:
            _save_window_state(self.window)
            _set_search_text_from_selection(self.window)
            select_search_text_on_open[self.window] = True
        if "globs" in args and "result" not in args:
            return TelescopeListInputHandler(self, args)
        if args.get("current_file") and "result" not in args:
            search_args = dict(args)
            search_args.setdefault("globs", "")
            return TelescopeListInputHandler(self, search_args)
        return GlobsInputHandler(self, search_state[self.window].globs)


class GlobsInputHandler(sublime_plugin.TextInputHandler):
    def __init__(
        self,
        window_command: TelescopeCommand,
        initial_value: str | None,
    ):
        self.window = window_command.window
        self.window_command = window_command
        self._initial_value = initial_value or ""

    def validate(self, text: str) -> bool:
        return not text or bool((text or "").strip())

    def description(self, value: str | None) -> str | None:
        if not value and value is not None:
            return "All"
        return value

    def initial_text(self) -> str:
        if self._initial_value is not None:
            sublime.set_timeout(self._select_and_reset)
        return self._initial_value

    def _select_and_reset(self):
        # See: https://github.com/sublimehq/sublime_text/issues/5507
        # Taken from the "LSP Go to symbols" and adapted for Text input
        self._initial_value = None
        if self.window.is_valid():
            self.window.run_command("select")

    def name(self) -> str:
        return "globs"

    def placeholder(self) -> str:
        return ".py, .js, views/*.html"

    def cancel(self):
        _reset_window_state(self.window, close_preview=True)

    def next_input(
        self,
        args: dict[str, Any],
    ) -> TelescopeListInputHandler | None:
        search_state[self.window].globs = ", ".join(
            g.strip() for g in args[self.name()].split(",")
        )
        if "result" not in args:
            return TelescopeListInputHandler(self.window_command, args)


class TelescopeListInputHandler(DynamicListInputHandler):
    def __init__(self, window_command: TelescopeCommand, args: dict[str, Any]):
        global search_results
        super().__init__(window_command, args)
        self.window = window_command.window
        self.window_command = window_command
        self.search_state = search_state[self.window]
        self.search_context = _get_search_context(self.window, args)
        same_search = self.search_state.context == self.search_context
        self.search_results = list(search_results) if same_search else []

        if not self.search_state.text:
            return

        self.text = self.search_state.text
        if not same_search:
            self.search_state.highlight_index = -1
            self.search_results = _live_search(
                self.window_command.window,
                self.text,
                self.args["globs"],
                self.args.get("current_file", False),
            )
            search_results = self.search_results
            self.search_state.context = self.search_context

        setattr(self.command, "_text", self.text)
        setattr(self.command, "_items", self._list_items(self.search_results))

    def name(self) -> str:
        return "result"

    def placeholder(self) -> str:
        return "Fuzzy find"

    def cancel(self):
        super().cancel()
        for view in self.window_command.window.views(include_transient=True):
            view.erase_regions("telescope-result-view")

        _reset_window_state(self.window_command.window, close_preview=True)

    def validate(self, text: str) -> bool:
        return bool((text or "").strip())

    def initial_selection(self) -> list[tuple[int, int]]:
        if select_search_text_on_open[self.window]:
            # if we search, then press enter, then press the shortcut again
            # keep the old search in the search bar, but select it
            select_search_text_on_open[self.window] = False
            if self.text:
                return [(0, len(self.text))]
        if hasattr(self.command, "_selection"):
            return self.command._selection
        return super().initial_selection()

    def preview(self, text: str):
        """Save the current highlighted index and show the preview.

        Save the highlighted element, so we can re-open the view
        at the same position.
        """
        if (text or "").strip():
            self.search_state.highlight_index = int(text.split(":", 1)[0])
            _preview_result(
                self.window_command.window,
                self.search_results,
                self.search_state.highlight_index,
            )

    def on_modified(self, text: str):
        global search_results
        self.search_state.highlight_index = -1
        self.search_state.text = text

        search_results = _live_search(
            self.window_command.window,
            text,
            self.args["globs"],
            self.args.get("current_file", False),
        )
        self.search_results = list(search_results)
        self.search_state.context = self.search_context

        setattr(
            self.command,
            "_selection",
            [s.to_tuple() for s in self.input_view.sel()],
        )
        self.update(self._list_items(search_results))

    def get_list_items(self) -> Any:
        return self._list_items(self.search_results)

    def _list_items(
        self,
        search_results: list[SearchResult],
    ) -> Any:
        if not search_results:
            return []

        return [
            sublime.ListInputItem(
                text=_fixed_size(s.line_content.strip(), 100),
                details=_fixed_size(
                    f"{s.path}:{s.line_number}:{s.line_position[0]}", 100
                ),
                # TODO: remove that hack (otherwise it's closed by the fuzzy search of sublime)
                value=str(i) + ":" + s.line_content.strip(),
                annotation="",
            )
            for i, s in enumerate(search_results)
        ], self.search_state.highlight_index


class IoPanelEventListener(sublime_plugin.EventListener):
    def on_load(self, view: View):
        window = view.window()
        if window in regions_to_add and view == regions_to_add[window][0]:
            _set_file_view_regions(*regions_to_add[window])
            del regions_to_add[window]


def _save_window_state(window: Window):
    views = window.views()

    window_state[window] = WindowState(
        active_view=window.active_view(),
        view_sel={view: list(view.sel()) for view in views},
        view_viewport={view: view.viewport_position() for view in views},
    )


def _set_search_text_from_selection(window: Window):
    view = window.active_view()
    if not view:
        return

    for region in view.sel():
        if region.empty():
            continue

        selected_text = view.substr(region).strip()
        if selected_text:
            search_state[window].text = selected_text
            search_state[window].context = None
            return


def _reset_window_state(
    window: Window,
    focus_old_view: bool = True,
    close_preview: bool = False,
):
    """Reset the initial state (cursor position, file opened, scroll position, etc.)."""
    state = window_state.get(window)
    if not state:
        return

    if state.active_view and focus_old_view:
        window.focus_view(state.active_view)

    if close_preview:
        _close_preview_panel(window)

    for view, sel in state.view_sel.items():
        if not view.is_valid():
            continue
        view.sel().clear()
        view.sel().add_all(sel)

    for view, viewport_position in state.view_viewport.items():
        if view.is_valid():
            view.set_viewport_position(viewport_position, animate=False)


def _close_preview_panel(window: Window):
    preview_panel = preview_panels.get(window)
    if not preview_panel or not preview_panel.is_valid():
        return

    state = window_state.get(window)
    if state and preview_panel == state.active_view:
        return

    sheet = preview_panel.sheet()
    if not sheet:
        return

    is_transient = getattr(sheet, "is_transient", None)
    is_semi_transient = getattr(sheet, "is_semi_transient", None)
    if (
        callable(is_transient)
        and is_transient()
        or callable(is_semi_transient)
        and is_semi_transient()
    ):
        sheet.close()
        preview_panels.pop(window, None)


def _live_search(
    window: Window,
    search_query: str,
    globs: str,
    current_file: bool = False,
) -> list[SearchResult]:
    global search_results
    if len(search_query) < 3:
        return []

    start = time.time()

    rg_cmd = [
        "rg",
        "--no-heading",
        "--max-filesize",
        "100M",
        "--max-count",
        "10000",
        "--follow",
        "--with-filename",
        "--line-number",
        "--smart-case",
        "-e",
        ".*" + ".*".join(map(re.escape, search_query)) + ".*",
    ]

    state = _get_window_state(window)
    view = state.active_view if state else window.active_view()
    if view and not current_file:
        # The `--iglob` when is negated is done in addition to the
        # default filter (.gitignore, etc)
        exclude_patterns = view.settings().get("binary_file_patterns") or []
        exclude_patterns += view.settings().get("file_exclude_patterns") or []
        exclude_patterns += [
            f"**/{f}**/" for f in view.settings().get("folder_exclude_patterns") or []
        ]
        for glob in exclude_patterns:
            glob = re.sub(r"\*+", "**", glob)
            rg_cmd.extend(("--iglob", f"!**/*{glob}"))

    if current_file:
        current_file_name = _get_current_file_name(window)
        if not current_file_name:
            return []
        rg_cmd.append(current_file_name)
    else:
        for glob in globs.split(","):
            glob = glob.strip()
            if not glob:
                continue
            glob = re.sub(r"\*+", "**", glob)
            # `--type` exist, but it works only for a fixed list of types
            # mimic sublime text glob logic
            rg_cmd.extend(("--iglob", f"**/*{glob}"))

        rg_cmd += window.folders()

    print(" ".join(rg_cmd))

    rg_process = _create_process(rg_cmd)
    fzf_process = _create_process(
        ["fzf", "--filter", search_query],
        stdin=rg_process.stdout,
    )
    rg_process.stdout.close()
    search_results = []
    for _ in range(50):  # Read first X lines
        line = fzf_process.stdout.readline().strip()
        if not line:
            break

        path, line_number, content = _parse_rg_result(line)
        to_trim = next((i for i, s in enumerate(content) if s.strip()), 0)
        content = content.strip()

        search_results.append(
            SearchResult(
                path,
                int(line_number),
                (to_trim, to_trim + len(content)),
                content[:200],
            )
        )

    rg_process.terminate()
    fzf_process.terminate()

    print("Search done in", time.time() - start)

    return search_results


def _preview_result(
    window: Window,
    search_results: list[SearchResult],
    result_index: int,
):
    if not search_results:
        return

    if (
        window not in preview_panels
        or preview_panels[window].file_name() != search_results[result_index].path
        or not preview_panels[window].sheet()
        or not preview_panels[window].sheet().is_selected()
    ):
        preview_panels[window] = window.open_file(
            search_results[result_index].path,
            flags=sublime.TRANSIENT,
        )

    _set_file_view_regions(preview_panels[window], search_results, result_index)


def _set_file_view_regions(
    view: View,
    search_results: list[SearchResult],
    result_index: int,
):
    """Set the region in the preview file we opened."""
    if view.is_loading():
        # Need to wait
        regions_to_add[view.window()] = (view, search_results, result_index)
        return

    search_result = search_results[result_index]
    line_a = view.text_point(
        search_result.line_number - 1,
        search_result.line_position[0],
    )
    r_view = sublime.Region(
        line_a,
        line_a - search_result.line_position[0] + search_result.line_position[1],
    )

    view.sel().clear()
    view.sel().add(sublime.Region(line_a, line_a))

    view.show(r_view, animate=False)
    view.add_regions(
        "telescope-result-view",
        [r_view],
        icon="",
        scope="comment | region.yellowish",
    )


def _fixed_size(s: str | None, size: int) -> str:
    """Make the string having a fixed size."""
    s = s or ""
    s = s[:size]
    s += " " * (size - len(s))
    return s


def _get_window_state(window: Window) -> WindowState | None:
    return window_state.get(window)


def _get_current_file_name(window: Window) -> str | None:
    state = _get_window_state(window)
    view = state.active_view if state else window.active_view()
    if not view:
        return None
    return view.file_name()


def _get_search_context(window: Window, args: dict[str, Any]) -> SearchContext:
    return (
        ("current_file", _get_current_file_name(window))
        if args.get("current_file")
        else ("folders", tuple(window.folders()), args.get("globs", ""))
    )


def _parse_rg_result(result: str) -> RgResult:
    if sys.platform.startswith("win"):
        drive, path, line_number, content = result.split(":", 3)
        path = drive + ":" + path
        return path, line_number, content
    return result.split(":", 2)


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
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
        **cmd_args,
    )
