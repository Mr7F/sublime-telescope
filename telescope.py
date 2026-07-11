from __future__ import annotations

import html
import os
import re
import shutil
import sublime
import sublime_plugin
import subprocess
import sys
import threading
import time

from collections import defaultdict
from dataclasses import dataclass, field
from itertools import groupby
from sublime import View, Window
from typing import Any

from .utils import debounced


PANEL_NAME = "telescope"
PARSER_PANEL_NAME = "telescope-syntax-parser"

StyledToken = tuple[str, dict[str, Any]]
LabelWidths = tuple[int, int]
MatchIndexes = frozenset[int]


@dataclass
class SearchResult:
    path: str
    line_number: int
    # Column of the first matched character, where the cursor goes on `enter`
    column: int
    line_content: str


@dataclass
class SearchState:
    globs: str = ""
    current_file: bool = False  # search only in the current file
    text: str = ""
    highlight_index: int = 0
    results: list[SearchResult] = field(default_factory=list)
    # The io panel views and input listener, set while the search is open
    output_view: View | None = None
    input_view: View | None = None
    listener: InputListener | None = None
    result_phantom_sets: dict[int, sublime.PhantomSet] = field(default_factory=dict)
    result_label_widths: LabelWidths = (0, 0)  # (max path size, max line number size)

    # Hidden panel used to parse and style the result lines, and the file it holds
    parser_view: View | None = None
    parser_view_path: str | None = None
    styled_line_cache: dict[tuple[str, int], list[StyledToken]] = field(
        default_factory=dict
    )

    preview: View | None = None  # The view opened to preview the highlighted result
    loading_preview: SearchResult | None = (
        None  # Result waiting for the preview file to finish loading
    )
    processes: tuple[subprocess.Popen, ...] = ()  # Processes of the search in progress

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
        state.styled_line_cache.clear()
        output_view, input_view = window.create_io_panel(
            PANEL_NAME,
            on_input=lambda text: window.run_command("telescope_confirm"),
        )

        output_view.set_read_only(True)
        output_view.settings().set("gutter", False)
        output_view.settings().set("word_wrap", False)
        input_view.settings().set("telescope_input", True)

        state.output_view = output_view
        state.input_view = input_view
        _assign_panel_syntax(window)

        window.run_command("show_panel", {"panel": "output." + PANEL_NAME})
        window.focus_view(input_view)

        if missing_tools := [tool for tool in ("rg", "fzf") if not shutil.which(tool)]:
            _set_panel_text(
                output_view,
                "Missing dependencies: {} (see the readme to install them)".format(
                    ", ".join(missing_tools)
                ),
            )
            return

        _start_phantom_refresh(window)

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


class TelescopeSetGlobsCommand(sublime_plugin.WindowCommand):
    """Ask for globs while the project search input panel is open."""

    def run(self, globs: str | None = None):
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
    """Input hadler to configure the globs."""

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
        previous_index = state.highlight_index
        step = 1 if forward else -1
        state.highlight_index = max(
            0, min(state.highlight_index + step, len(state.results) - 1)
        )
        if state.highlight_index != previous_index:
            _highlight_result_in_output_panel(self.window)


class TelescopeConfirmCommand(sublime_plugin.WindowCommand):
    """Open the highlighted result, executed when pressing enter."""

    def run(self):
        state = search_state[self.window]
        if not state.results:
            return
        result = state.results[state.highlight_index]
        _close_panel(self.window)
        _restore_view_states(self.window)
        # TODO: keep transient view if possible
        self.window.open_file(
            _encoded_position(result),
            flags=sublime.SEMI_TRANSIENT | sublime.ENCODED_POSITION,
        )


class TelescopeCancelCommand(sublime_plugin.WindowCommand):
    """Close the search and restore the window state, bound to escape."""

    def run(self):
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
            _highlight_result_in_preview(view, state.loading_preview, state.text)
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
        target=_search_in_background,
        args=(window, text),
        daemon=True,
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
        state.result_label_widths = _result_label_widths(results)
        _clear_result_phantoms(state)
        state.highlight_index = min(state.highlight_index, max(len(results) - 1, 0))
        _set_panel_text(
            state.output_view,
            "\n".join(
                _result_location_label(result, state.result_label_widths)
                for result in results
            ),
        )
        _update_result_phantoms(window)
        if results:
            _highlight_result_in_output_panel(window)
        else:
            state.output_view.erase_regions("telescope-selected")
        if len(text) >= _settings().get("min_query_length", 3):
            window.status_message(
                "Telescope: {} results in {:.2f}s".format(
                    len(results), time.time() - start
                )
            )

    sublime.set_timeout(show_results)


def _assign_panel_syntax(window: Window):
    """Use a custom synthax to highlight the lin number."""
    state = search_state[window]
    state.output_view.assign_syntax("scope:text.telescope")
    source_view = state.active_view or window.active_view()
    color_scheme = source_view.settings().get("color_scheme") if source_view else None
    if color_scheme:
        state.output_view.settings().set("color_scheme", color_scheme)


def _set_panel_text(view: View, text: str):
    view.set_read_only(False)
    view.run_command("telescope_replace", {"text": text})
    view.set_read_only(True)


# Output Pannel


def _update_result_phantoms(window: Window):
    state = search_state[window]
    if not state.output_view or not state.results:
        return

    # Only render the visible rows, and only once: scrolling reveals the
    # missing ones through the periodic refresh
    rows = _visible_result_rows(state) - state.result_phantom_sets.keys()
    # Group by path so the parser panel loads each file only once
    for row in sorted(rows, key=lambda row: state.results[row].path):
        _update_result_phantom(window, row)


def _visible_result_rows(state: SearchState) -> set[int]:
    view = state.output_view
    visible = view.visible_region()
    first_row = max(0, view.rowcol(visible.begin())[0] - 2)
    last_row = min(len(state.results) - 1, view.rowcol(visible.end())[0] + 2)
    return set(range(first_row, last_row + 1)) | {state.highlight_index}


def _update_result_phantom(window: Window, row: int):
    state = search_state[window]
    output_view = state.output_view
    result = state.results[row]
    tokens = _result_tokens(window, result) or [(result.line_content, {})]
    line_text = "".join(text for text, _ in tokens)

    phantom_set = state.result_phantom_sets.get(row)
    if not phantom_set:
        phantom_set = sublime.PhantomSet(output_view, f"telescope-result-{row}")
        state.result_phantom_sets[row] = phantom_set

    # After the "file:line" label, which is the panel text of the row
    line_end = output_view.line(output_view.text_point(row, 0)).end()
    phantom_set.update(
        [
            sublime.Phantom(
                sublime.Region(line_end, line_end),
                _result_html(
                    _style_view(window),
                    tokens,
                    _fuzzy_match_indexes(state.text, line_text),
                ),
                sublime.PhantomLayout.INLINE,
            )
        ]
    )


def _style_view(window: Window) -> View:
    """The view providing the selection and label colors of the results."""
    state = search_state[window]
    return state.active_view or window.active_view() or state.output_view


def _result_tokens(window: Window, result: SearchResult) -> list[StyledToken]:
    """Build the `StyledToken` for the given search, for color highlighting."""
    state = search_state[window]
    key = (result.path, result.line_number)
    if key not in state.styled_line_cache:
        if state.current_file:
            view = state.active_view or window.active_view()
        else:
            view = _parser_view_for_result(window, result)
        state.styled_line_cache[key] = _tokens_from_view(view, result) if view else []
    return state.styled_line_cache[key]


def _tokens_from_view(view: View, result: SearchResult) -> list[StyledToken]:
    """Extract the style for each tokens of the search result, in the given view."""
    line = view.line(view.text_point(result.line_number - 1, 0))
    tokens = []
    cursor = line.begin()

    for region, scope in view.extract_tokens_with_scopes(line):
        if cursor < region.begin():
            tokens.append((view.substr(sublime.Region(cursor, region.begin())), {}))
        tokens.append((view.substr(region), view.style_for_scope(scope)))
        cursor = region.end()

    if cursor < line.end():
        tokens.append((view.substr(sublime.Region(cursor, line.end())), {}))

    return tokens


def _parser_view_for_result(window: Window, result: SearchResult) -> View | None:
    """Load the search result in a view for color highlighting."""
    state = search_state[window]
    parser_view = state.parser_view
    if not parser_view or not parser_view.is_valid():
        parser_view = window.create_output_panel(PARSER_PANEL_NAME, unlisted=True)
        parser_view.settings().set("gutter", False)
        parser_view.settings().set("word_wrap", False)
        state.parser_view = parser_view
        state.parser_view_path = None

    if state.parser_view_path == result.path:
        return parser_view

    try:
        with open(result.path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return None

    syntax = sublime.find_syntax_for_file(result.path, content.split("\n", 1)[0])
    if syntax:
        parser_view.assign_syntax(syntax)

    source_view = state.active_view or window.active_view()
    color_scheme = source_view.settings().get("color_scheme") if source_view else None
    if color_scheme:
        parser_view.settings().set("color_scheme", color_scheme)

    _set_panel_text(parser_view, content)
    state.parser_view_path = result.path
    return parser_view


def _start_phantom_refresh(window: Window):
    """Color the result rows lazily as they get scrolled or moved into view."""
    output_view = search_state[window].output_view

    def refresh():
        state = search_state.get(window)
        if not state or state.output_view != output_view:
            # The panel was closed or reopened, this loop is done
            return
        _update_result_phantoms(window)
        sublime.set_timeout(refresh, 150)

    sublime.set_timeout(refresh, 150)


def _clear_result_phantoms(state: SearchState):
    for phantom_set in state.result_phantom_sets.values():
        phantom_set.update([])
    state.result_phantom_sets.clear()


def _result_html(
    style_view: View,
    tokens: list[StyledToken],
    match_indexes: MatchIndexes,
) -> str:
    match_background = style_view.style().get("selection", "#3f4753")
    chunks = _styled_line_chunks(tokens, match_indexes, match_background)
    return (
        '<body id="telescope-result">'
        '<span style="white-space: pre">'
        f"&nbsp;{''.join(chunks)}"
        "</span>"
        "</body>"
    )


def _result_location_label(
    result: SearchResult,
    label_widths: LabelWidths,
) -> str:
    file_name_width, line_number_width = label_widths
    name = _result_file_name(result)
    return f"{name:<{file_name_width}}:{result.line_number:>{line_number_width}}"


def _result_label_widths(results: list[SearchResult]) -> LabelWidths:
    return (
        max((len(_result_file_name(result)) for result in results), default=0),
        max((len(str(result.line_number)) for result in results), default=0),
    )


def _result_file_name(result: SearchResult) -> str:
    return os.path.basename(result.path) if result.path else "untitled"


def _styled_line_chunks(
    tokens: list[StyledToken],
    match_indexes: MatchIndexes,
    match_background: str,
) -> list[str]:
    chunks = []
    cursor = 0
    for text, style in tokens:
        for segment, matched in _match_segments(text, cursor, match_indexes):
            background = match_background if matched else None
            chunks.append(_html_span(segment, style, background))
        cursor += len(text)
    return chunks


def _match_segments(
    text: str,
    offset: int,
    match_indexes: MatchIndexes,
) -> list[tuple[str, bool]]:
    return [
        ("".join(char for _, char in group), matched)
        for matched, group in groupby(
            enumerate(text), lambda pair: offset + pair[0] in match_indexes
        )
    ]


def _fuzzy_match_indexes(query: str, text: str) -> MatchIndexes:
    if not query or not text:
        return frozenset()

    case_sensitive = any(char.isupper() for char in query)
    query_to_match = query if case_sensitive else query.lower()
    text_to_match = text if case_sensitive else text.lower()

    forward_indexes = []
    search_from = 0
    for char in query_to_match:
        index = text_to_match.find(char, search_from)
        if index == -1:
            return frozenset()
        forward_indexes.append(index)
        search_from = index + 1

    search_before = forward_indexes[-1]
    indexes = [search_before]
    for char in reversed(query_to_match[:-1]):
        index = text_to_match.rfind(char, 0, search_before)
        if index == -1:
            return frozenset(forward_indexes)
        indexes.append(index)
        search_before = index

    return frozenset(indexes)


def _html_span(
    text: str,
    style: dict[str, Any],
    background: str | None = None,
) -> str:
    css = []
    if foreground := style.get("foreground"):
        css.append(f"color: {html.escape(foreground)}")
    if background:
        css.append(f"background-color: {html.escape(background)}")
    elif style_background := style.get("background"):
        css.append(f"background-color: {html.escape(style_background)}")
    if style.get("bold"):
        css.append("font-weight: bold")
    if style.get("italic"):
        css.append("font-style: italic")
    if style.get("underline"):
        css.append("text-decoration: underline")

    if not css:
        return _html_text(text)
    return '<span style="{}">{}</span>'.format(
        "; ".join(css),
        _html_text(text),
    )


def _html_text(text: str) -> str:
    return (
        html.escape(text)
        .replace(" ", "&nbsp;")
        .replace("\t", "&nbsp;&nbsp;&nbsp;&nbsp;")
    )


def _highlight_result_in_output_panel(window: Window):
    """Highlight the selected result, the phantom refresh colors it later."""
    state = search_state[window]
    view = state.output_view
    line = view.line(view.text_point(state.highlight_index, 0))
    view.add_regions(
        "telescope-selected",
        [line],
        icon="",
        scope=_highlight_scope(),
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


# Preview


def _preview_result(window: Window, result: SearchResult):
    """Preview the file in a tab while navigating the result."""
    state = search_state[window]
    if _can_update_preview_in_place(state.preview, result):
        _highlight_result_in_preview(state.preview, result, state.text)
        return

    # Transient: sublime keeps a single preview per group and replaces it
    # automatically when previewing an other file. The `ENCODED_POSITION`
    # moves the cursor, even if the file is still loading
    state.preview = window.open_file(
        _encoded_position(result),
        flags=sublime.TRANSIENT | sublime.ENCODED_POSITION,
    )
    if state.preview.is_loading():
        state.loading_preview = result
    else:
        _highlight_result_in_preview(state.preview, result, state.text)


def _can_update_preview_in_place(view: View | None, result: SearchResult) -> bool:
    return bool(
        view
        and view.is_valid()
        and not view.is_loading()
        and view.file_name() == result.path
    )


def _highlight_result_in_preview(view: View, result: SearchResult, query: str):
    """Move the cursor to the start of the match and highlight the fuzzy matches."""
    line = view.line(view.text_point(result.line_number - 1, 0))
    point = line.begin() + result.column
    view.sel().clear()
    view.sel().add(point)
    view.add_regions(
        "telescope-result-view",
        _fuzzy_match_regions(view, line, query),
        icon="",
        scope=_highlight_scope(),
    )
    view.show(point)


def _encoded_position(result: SearchResult) -> str:
    return "{}:{}:{}".format(result.path, result.line_number, result.column + 1)


def _fuzzy_match_regions(
    view: View,
    line: sublime.Region,
    query: str,
) -> list[sublime.Region]:
    """FZF does not return the region that match the query, so we compute it ourself to highlight the matching terms."""
    regions = []
    for index in sorted(_fuzzy_match_indexes(query, view.substr(line))):
        point = line.begin() + index
        if regions and regions[-1].end() == point:
            regions[-1].b = point + 1
        else:
            regions.append(sublime.Region(point, point + 1))
    return regions


def _erase_result_regions(window: Window):
    for view in window.views(include_transient=True):
        view.erase_regions("telescope-result-view")


def _close_panel(window: Window):
    state = search_state[window]
    _kill_search(window)
    if state.listener and state.listener.is_attached():
        state.listener.detach()
    _erase_result_regions(window)
    _clear_result_phantoms(state)
    if state.output_view:
        window.destroy_output_panel(PANEL_NAME)
    if state.parser_view:
        window.destroy_output_panel(PARSER_PANEL_NAME)
    state.output_view = state.input_view = state.listener = None
    state.parser_view = state.parser_view_path = None


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
    state = search_state[window]
    if state.active_view:
        window.focus_view(state.active_view)

    _close_preview(window)
    _restore_view_states(window)


def _restore_view_states(window: Window):
    """Restore the initial state (cursor position, scroll position, etc.)."""
    for view, (sel, viewport_position) in search_state[window].view_states.items():
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


# Search


def _get_sidebar_folders(window: Window) -> list[str]:
    """The rg arguments searching the folders visible in the side bar.

    There is no API exposing the visible entries, mimic the side bar
    filtering: the exclude and include patterns of the settings and of
    the project folders, in addition to the default rg filter
    (.gitignore, etc). rg matches the globs against absolute paths
    because its working directory is the root, see `_create_process`.
    """
    folders = window.folders()
    if not folders:
        return []

    view = search_state[window].active_view or window.active_view()
    project_folders = (window.project_data() or {}).get("folders", [])
    args = []

    # The include patterns are a whitelist: only the matching entries of
    # the folder are visible. A positive glob restricts the whole search:
    # scope it with absolute paths, and include the other folders entirely
    if any(
        folder.get("file_include_patterns") or folder.get("folder_include_patterns")
        for folder in project_folders
    ):
        for path, folder in zip(folders, project_folders):
            include_globs = []
            for pattern in folder.get("file_include_patterns") or []:
                include_globs += _convert_sublime_glob_to_rg_glob(pattern)
            for pattern in folder.get("folder_include_patterns") or []:
                include_globs += _convert_sublime_glob_to_rg_glob(
                    pattern, directory=True
                )
            for glob in include_globs or [f"{path}/**"]:
                args += ("--iglob", glob)

    # The excludes come last: for rg the last matching glob wins, they
    # apply within the included entries
    sources = ([view.settings()] if view else []) + project_folders
    for source in sources:
        for pattern in source.get("folder_exclude_patterns") or []:
            for glob in _convert_sublime_glob_to_rg_glob(pattern, directory=True):
                args += ("--iglob", f"!{glob}")
        file_patterns = source.get("file_exclude_patterns") or []
        file_patterns += source.get("binary_file_patterns") or []
        for pattern in file_patterns:
            for glob in _convert_sublime_glob_to_rg_glob(pattern):
                args += ("--iglob", f"!{glob}")

    return args + folders


def _convert_sublime_glob_to_rg_glob(
    pattern: str,
    directory: bool = False,
) -> list[str]:
    """Convert an include/exclude pattern of sublime to rg globs.

    Sublime matches them with fnmatch: patterns without a separator
    match the entry names at any depth, and `*` also matches separators.
    Directory patterns hide or show everything below them.
    """
    if not pattern.startswith("/"):
        pattern = f"**/{pattern}"
    if directory:
        return [f"{pattern}/**"]
    if pattern.endswith("*"):
        # The trailing `*` also matches in the folders below
        return [pattern, f"{pattern}/**"]
    return [pattern]


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
        folders = _get_sidebar_folders(window)
        if not folders:
            # Without folders rg would search its working directory
            return []

        for glob in state.globs.split(","):
            glob = glob.strip()
            if not glob:
                continue
            glob = re.sub(r"\*+", "**", glob)
            # `--type` exist, but it works only for a fixed list of types
            # mimic sublime text glob logic
            rg_cmd.extend(("--iglob", f"**/*{glob}"))

        rg_cmd += folders

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

        parsed = _parse_rg_result(line)
        if not parsed:
            continue

        path, line_number, content = parsed
        match_indexes = _fuzzy_match_indexes(search_query, content)
        results.append(
            SearchResult(
                path,
                line_number,
                # The match can be on the path, fall back to the content
                min(match_indexes, default=len(content) - len(content.lstrip())),
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


def _highlight_scope() -> str:
    return _settings().get("highlight_scope", "comment | region.yellowish")


def _parse_rg_result(result: str) -> tuple[str, int, str] | None:
    drive = ""
    if sys.platform.startswith("win"):
        drive, _, result = result.partition(":")
        drive += ":"

    path, _, rest = result.partition(":")
    line_number, _, content = rest.partition(":")
    if not line_number.isdigit():
        return None
    return drive + path, int(line_number), content


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
        # rg matches its globs against the paths relative to the working
        # directory: from the root they are the absolute paths
        cwd=os.path.abspath(os.sep),
        text=True,
        shell=False,
        **cmd_args,
    )
