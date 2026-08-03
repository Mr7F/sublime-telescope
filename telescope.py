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
from itertools import groupby, islice
from sublime import View, Window
from typing import Any

from .utils import _convert_sublime_glob_to_rg_glob, debounced


PANEL_NAME = "telescope"
PARSER_PANEL_NAME = "telescope-syntax-parser"
PHANTOM_REFRESH_MS = 50
MAX_RENDER_LINES = 50_000

StyledToken = "tuple[str, dict[str, Any]]"
LabelWidths = "tuple[int, int]"
MatchIndexes = "frozenset[int]"


@dataclass
class SearchResult:
    path: str
    line_number: int
    # Column of the first matched character, where the cursor goes on `enter`
    column: int
    line_content: str


@dataclass
class SearchTask:
    processes: tuple[subprocess.Popen, ...] = ()


@dataclass
class SearchState:
    directory_glob: str = ""
    current_file: bool = False  # search only in the current file
    text: str = ""
    highlight_index: int = 0
    results: list[SearchResult] = field(default_factory=list)
    is_open: bool = False  # the search panel is open
    phantom_refresh_active: bool = False
    phantom_render_generation: int = 0
    phantom_render_thread: threading.Thread | None = None
    phantom_visible_rows: frozenset[int] = frozenset()
    reset_render_cache: bool = False
    # The io panel views and input listener, they survive closing the
    # panel: it is only hidden and reused on the next open
    output_view: View | None = None
    input_view: View | None = None
    listener: InputListener | None = None
    result_phantom_set: sublime.PhantomSet | None = None
    result_phantoms: dict[int, sublime.Phantom] = field(default_factory=dict)
    result_label_widths: LabelWidths = (0, 0)  # (max path size, max line number size)

    # Hidden panel used to parse and style the result lines, and the file it holds
    parser_view: View | None = None
    parser_view_path: str | None = None
    styled_line_cache: dict[tuple[str, int], list[StyledToken]] = field(
        default_factory=dict
    )
    unstyled_result_paths: set[str] = field(default_factory=set)

    preview: View | None = None  # The view opened to preview the highlighted result
    loading_preview: SearchResult | None = (
        None  # Result waiting for the preview file to finish loading
    )
    search_task: SearchTask | None = None

    # State of the window before the search, restored when canceling
    active_view: View | None = None
    view_states: dict[View, tuple[list[sublime.Region], tuple[float, float]]] = field(
        default_factory=dict
    )


search_state: defaultdict[Window, SearchState] = defaultdict(SearchState)


class TelescopeCommand(sublime_plugin.WindowCommand):
    """Open the search io panel."""

    def run(self, directory_glob: str | None = None, current_file: bool = False):
        window = self.window
        state = search_state[window]
        previous_search = _search_context(state)

        _save_window_state(window)
        _set_search_text_from_selection(window)
        if directory_glob is not None:
            _set_directory_glob(state, directory_glob)
        state.current_file = current_file

        _close_panel(window)
        # Create the panel once per window: `create_io_panel` clears the
        # content of an existing panel, and `is_valid` is not reliable
        # for the hidden panel views
        if not state.output_view:
            output_view, input_view = window.create_io_panel(
                PANEL_NAME,
                on_input=lambda text: window.run_command("telescope_confirm"),
                unlisted=True,
            )
            output_view.set_read_only(True)
            output_view.settings().set("gutter", False)
            output_view.settings().set("word_wrap", False)
            input_view.settings().set("telescope_input", True)
            state.output_view = output_view
            state.input_view = input_view

        input_view = state.input_view
        input_view.assign_syntax("scope:text.telescope.input")
        state.is_open = True
        _assign_panel_syntax(window)

        window.run_command("show_panel", {"panel": "output." + PANEL_NAME})
        window.focus_view(input_view)

        if missing_tools := [tool for tool in ("rg", "fzf") if not shutil.which(tool)]:
            _set_panel_text(
                state.output_view,
                "Missing dependencies: {} (see the readme to install them)".format(
                    ", ".join(missing_tools)
                ),
            )
            return

        _start_phantom_refresh(window)

        input_text = _input_text(state)
        if input_text:
            # Restore the previous search, but select the text
            input_view.run_command("select_all")
            input_view.run_command("insert", {"characters": input_text})
            _select_search_text(input_view, state)
        else:
            # The reused input can hold text typed too quickly before the
            # last close, never searched
            input_view.run_command("select_all")
            input_view.run_command("right_delete")

        # Attach after setting the text to only search once, below
        state.listener = InputListener(window)
        state.listener.attach(input_view.buffer())

        if state.text and _search_context(state) != previous_search:
            # New search: restart from the first result with an empty
            # panel, the files may also have changed since the last search
            state.highlight_index = 0
            state.results = []
            state.reset_render_cache = True
            _clear_result_phantoms(state)
            _set_panel_text(state.output_view, "")
            state.output_view.erase_regions("telescope-selected")
            _search(window, input_text)
        elif state.results:
            # Same search: the reused panel still shows the results and
            # the highlighted line, only reopen the preview
            _preview_result(window, state.results[state.highlight_index])
            # The preview steals the focus when it opens a new file
            window.focus_view(input_view)


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
        preview = state.preview
        _close_panel(self.window)
        # Skip the opened file: restoring its scroll position to move it
        # back to the result just after would make it flick
        _restore_view_states(self.window, skip=self.window.find_open_file(result.path))

        if _can_update_preview_in_place(preview, result):
            # Keep the preview view, promoted from transient to a real
            # tab. Its cursor is already on the result
            self.window.promote_sheet(preview.sheet())
            self.window.focus_view(preview)
            return

        # The preview is still loading or was not opened
        self.window.open_file(
            _encoded_position(result),
            flags=sublime.SEMI_TRANSIENT | sublime.ENCODED_POSITION,
        )


class TelescopeCancelCommand(sublime_plugin.WindowCommand):
    """Close the search and restore the window state, bound to escape."""

    def run(self):
        _close_panel(self.window)
        _reset_window_state(self.window)


class TelescopeReplaceCommand(sublime_plugin.TextCommand):
    """Replace the complete contents of a view."""

    def run(self, edit, text=""):
        self.view.replace(
            edit,
            sublime.Region(0, self.view.size()),
            text,
        )
        self.view.sel().clear()


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
        window = view.window()
        state = search_state.get(window) if window else None
        if not state:
            return
        if view == state.preview and state.loading_preview:
            _highlight_result_in_preview(view, state.loading_preview, state.text)
            state.loading_preview = None

    def on_pre_close_window(self, window: Window):
        state = search_state.get(window)
        if state:
            state.is_open = False
            state.phantom_render_generation += 1
            _kill_search(window)
            search_state.pop(window, None)


def _search_context(state: SearchState) -> tuple:
    """The parameters defining the search results.

    Reopening the panel with the same ones keeps the previous search.
    """
    return (
        state.text,
        state.directory_glob if not state.current_file else "",
        state.current_file,
        # In current file mode the searched file matters
        state.active_view if state.current_file else None,
    )


def _search(window: Window, input_text: str):
    state = search_state[window]
    directory_glob, search_query = _parse_input_text(input_text, state.current_file)
    if search_query != state.text or (
        not state.current_file and directory_glob != state.directory_glob
    ):
        # New search, restart from the first result
        state.highlight_index = 0
    state.text = search_query
    if not state.current_file:
        state.directory_glob = directory_glob
    _kill_search(window)
    task = SearchTask()
    state.search_task = task
    search_context = _search_context(state)
    # Search in the background, it can lag on big projects
    threading.Thread(
        target=_search_in_background,
        args=(
            window,
            state,
            task,
            search_query,
            state.directory_glob,
            search_context,
        ),
        daemon=True,
    ).start()


def _search_in_background(
    window: Window,
    state: SearchState,
    task: SearchTask,
    search_query: str,
    directory_glob: str,
    search_context: tuple,
):
    start = time.time()
    results = _live_search(
        window, search_query, directory_glob, state=state, search_task=task
    )

    def show_results():
        if (
            search_state.get(window) is not state
            or state.search_task is not task
            or _search_context(state) != search_context
            or not state.is_open
        ):
            # The search changed or was closed in the meantime
            return
        state.search_task = None
        state.results = results
        state.result_label_widths = _result_label_widths(results)
        state.highlight_index = min(state.highlight_index, max(len(results) - 1, 0))
        _render_results(window)
        if len(search_query) >= _settings().get("min_query_length", 3):
            window.status_message(
                "Telescope: {} results in {:.2f}s".format(
                    len(results), time.time() - start
                )
            )

    sublime.set_timeout(show_results)


def _render_results(window: Window):
    """Fill the panel with the labels, phantoms and highlight of the results."""
    state = search_state[window]
    results = state.results
    _clear_result_phantoms(state)
    _set_panel_text(
        state.output_view,
        (
            "\n".join(
                _result_location_label(result, state.result_label_widths)
                for result in results
            )
            if results
            else "No result"
        ),
    )
    _refresh_result_phantoms(window)
    if results:
        _highlight_result_in_output_panel(window)
    else:
        state.output_view.erase_regions("telescope-selected")


def _assign_panel_syntax(window: Window):
    """Use a custom syntax to highlight the line number."""
    output_view = search_state[window].output_view
    output_view.assign_syntax("scope:text.telescope")
    _copy_color_scheme(window, output_view)


def _copy_color_scheme(
    window: Window, view: View, state: SearchState | None = None
):
    if state:
        source_view = state.active_view or window.active_view()
    else:
        source_view = _source_view(window)
    color_scheme = source_view.settings().get("color_scheme") if source_view else None
    if color_scheme:
        view.settings().set("color_scheme", color_scheme)


def _source_view(window: Window) -> View | None:
    """The view the search was opened from."""
    return search_state[window].active_view or window.active_view()


def _set_panel_text(view: View, text: str):
    view.set_read_only(False)
    view.run_command("telescope_replace", {"text": text})
    view.set_read_only(True)


# Output Panel


def _update_result_phantoms(window: Window, visible_rows: frozenset[int]):
    state = search_state[window]
    if not state.results or state.phantom_render_thread is not None:
        return

    # Only render the visible rows, and only once: scrolling reveals the
    # missing ones through the periodic refresh. Group rows by path so each
    # full file is loaded into the parser panel only once per batch.
    rows = sorted(
        visible_rows - state.result_phantoms.keys(),
        key=lambda row: state.results[row].path,
    )
    if not rows:
        return

    results = state.results
    generation = state.phantom_render_generation
    worker = threading.Thread(
        target=_render_result_phantoms,
        args=(window, state, generation, rows, results),
        daemon=True,
    )
    state.phantom_render_thread = worker
    worker.start()


def _render_result_phantoms(
    window: Window,
    state: SearchState,
    generation: int,
    rows: list[int],
    results: list[SearchResult],
):
    """Compute visible result styles on this state's single renderer thread."""
    try:
        if state.reset_render_cache:
            state.styled_line_cache.clear()
            state.unstyled_result_paths.clear()
            state.parser_view_path = None
            state.reset_render_cache = False
        for row in rows:
            if (
                not state.is_open
                or state.phantom_render_generation != generation
            ):
                return
            result = results[row]
            tokens = _result_tokens(window, state, result) or [
                (result.line_content, {})
            ]
            sublime.set_timeout(
                lambda row=row, tokens=tokens: _insert_rendered_result(
                    window, state, generation, results, row, tokens
                )
            )
    finally:
        worker = threading.current_thread()
        sublime.set_timeout(
            lambda: _finish_result_render(window, state, worker)
        )


def _finish_result_render(
    window: Window, state: SearchState, worker: threading.Thread
):
    if state.phantom_render_thread is worker:
        state.phantom_render_thread = None
    if search_state.get(window) is state and state.is_open:
        _refresh_result_phantoms(window)


def _insert_rendered_result(
    window: Window,
    state: SearchState,
    generation: int,
    results: list[SearchResult],
    row: int,
    tokens: list[StyledToken],
):
    """Insert worker output only if its search is still displayed."""
    if (
        search_state.get(window) is state
        and state.phantom_render_generation == generation
        and state.results is results
    ):
        _insert_result_phantom(window, row, tokens)


def _visible_result_rows(state: SearchState) -> set[int]:
    view = state.output_view
    visible = view.visible_region()
    first_row = max(0, view.rowcol(visible.begin())[0] - 2)
    last_row = min(len(state.results) - 1, view.rowcol(visible.end())[0] + 2)
    return set(range(first_row, last_row + 1)) | {state.highlight_index}


def _insert_result_phantom(window: Window, row: int, tokens: list[StyledToken]):
    """Anchor and show a row's phantom after the panel text was updated."""
    state = search_state[window]
    if (
        not state.is_open
        or row in state.result_phantoms
        or row >= len(state.results)
    ):
        return
    output_view = state.output_view
    line_text = "".join(text for text, _ in tokens)

    if state.result_phantom_set is None:
        state.result_phantom_set = sublime.PhantomSet(
            output_view, "telescope-results"
        )

    # After the "file:line" label, which is the panel text of the row
    line_end = output_view.line(output_view.text_point(row, 0)).end()
    state.result_phantoms[row] = sublime.Phantom(
        sublime.Region(line_end, line_end),
        _result_html(
            _style_view(window),
            tokens,
            _fuzzy_match_indexes(state.text, line_text),
        ),
        sublime.PhantomLayout.INLINE,
    )
    state.result_phantom_set.update(list(state.result_phantoms.values()))


def _style_view(window: Window) -> View:
    """The view providing the selection and label colors of the results."""
    return _source_view(window) or search_state[window].output_view


def _result_tokens(
    window: Window, state: SearchState, result: SearchResult
) -> list[StyledToken]:
    """Build the `StyledToken` for the given search, for color highlighting."""
    key = (result.path, result.line_number)
    if key not in state.styled_line_cache:
        if result.path in state.unstyled_result_paths:
            view = None
        elif state.current_file:
            view = state.active_view or window.active_view()
            if view and _view_line_count(view) > MAX_RENDER_LINES:
                state.unstyled_result_paths.add(result.path)
                view = None
        else:
            view = _parser_view_for_result(window, state, result)
        state.styled_line_cache[key] = _tokens_from_view(view, result) if view else []
    return state.styled_line_cache[key]


def _view_line_count(view: View) -> int:
    if not view.size():
        return 0
    return view.rowcol(view.size() - 1)[0] + 1


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


def _parser_view_for_result(
    window: Window, state: SearchState, result: SearchResult
) -> View | None:
    """Load the search result in a view for color highlighting."""
    parser_view = state.parser_view
    if parser_view and state.parser_view_path == result.path:
        return parser_view

    try:
        with open(result.path, encoding="utf-8", errors="replace") as f:
            lines = list(islice(f, MAX_RENDER_LINES + 1))
    except OSError:
        state.unstyled_result_paths.add(result.path)
        return None
    if len(lines) > MAX_RENDER_LINES:
        state.unstyled_result_paths.add(result.path)
        return None
    content = "".join(lines)

    if not parser_view:
        parser_view = window.create_output_panel(PARSER_PANEL_NAME, unlisted=True)
        parser_view.settings().set("gutter", False)
        parser_view.settings().set("word_wrap", False)
        state.parser_view = parser_view

    syntax = sublime.find_syntax_for_file(result.path, content.split("\n", 1)[0])
    if syntax:
        parser_view.assign_syntax(syntax)
    _copy_color_scheme(window, parser_view, state)

    _set_panel_text(parser_view, content)
    state.parser_view_path = result.path
    return parser_view


def _start_phantom_refresh(window: Window):
    """Color result rows as the viewport moves."""
    state = search_state[window]
    if state.phantom_refresh_active:
        # Reopening reuses the loop of the previous search
        return
    state.phantom_refresh_active = True

    def refresh():
        state = search_state.get(window)
        if not state:
            return
        if not state.is_open:
            state.phantom_refresh_active = False
            return
        _refresh_result_phantoms(window)
        sublime.set_timeout(refresh, PHANTOM_REFRESH_MS)

    sublime.set_timeout(refresh, PHANTOM_REFRESH_MS)


def _refresh_result_phantoms(window: Window):
    state = search_state[window]
    visible_rows = (
        frozenset(_visible_result_rows(state)) if state.results else frozenset()
    )
    if visible_rows != state.phantom_visible_rows:
        state.phantom_visible_rows = visible_rows
        state.phantom_render_generation += 1
        _remove_hidden_result_phantoms(state, visible_rows)
    _update_result_phantoms(window, visible_rows)


def _remove_hidden_result_phantoms(
    state: SearchState, visible_rows: frozenset[int]
):
    hidden_rows = state.result_phantoms.keys() - visible_rows
    if not hidden_rows:
        return
    for row in hidden_rows:
        state.result_phantoms.pop(row)
    state.result_phantom_set.update(list(state.result_phantoms.values()))


def _clear_result_phantoms(state: SearchState):
    state.phantom_render_generation += 1
    state.phantom_visible_rows = frozenset()
    state.result_phantoms.clear()
    if state.result_phantom_set:
        state.result_phantom_set.update([])


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

    # Switching file: erase the highlight left in the previous preview
    if state.preview and state.preview.is_valid():
        state.preview.erase_regions("telescope-result-view")

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
    """Close the io panel and clean the state."""
    state = search_state[window]
    _kill_search(window)
    if state.listener and state.listener.is_attached():
        state.listener.detach()
    _erase_result_regions(window)
    window.run_command("hide_panel", {"panel": "output." + PANEL_NAME})
    state.is_open = False
    # A pending `on_load` would highlight the preview after the close
    state.loading_preview = None


def _set_directory_glob(state: SearchState, directory_glob: str):
    directory_glob = directory_glob.strip()
    if directory_glob != state.directory_glob:
        # New directory filter, restart from the first result
        state.highlight_index = 0
    state.directory_glob = directory_glob


def _input_text(state: SearchState) -> str:
    if state.current_file or not state.directory_glob:
        return state.text
    return f"{state.directory_glob}  {state.text}"


def _select_search_text(input_view: View, state: SearchState):
    if state.current_file or not state.directory_glob:
        input_view.run_command("select_all")
        return

    start = len(state.directory_glob) + 2
    input_view.sel().clear()
    input_view.sel().add(sublime.Region(start, start + len(state.text)))


def _parse_input_text(input_text: str, current_file: bool) -> tuple[str, str]:
    if current_file:
        return "", input_text.strip()

    parts = re.split(r"( {2,})", input_text, 1)
    if len(parts) == 1:
        return "", input_text.strip()
    directory_glob, _separator, search_query = parts
    return directory_glob.strip(), search_query.strip()


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


def _restore_view_states(window: Window, skip: View | None = None):
    """Restore the initial state (cursor position, scroll position, etc.)."""
    for view, (sel, viewport_position) in search_state[window].view_states.items():
        if view == skip or not view.is_valid():
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

    view = _source_view(window)
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
                include_globs += _convert_sublime_glob_to_rg_glob(
                    pattern,
                    roots=[path],
                )
            for pattern in folder.get("folder_include_patterns") or []:
                include_globs += _convert_sublime_glob_to_rg_glob(
                    pattern,
                    directory=True,
                    roots=[path],
                )
            for glob in include_globs or [f"{path}/**"]:
                args += ("--glob", glob)

    # The excludes come last: for rg the last matching glob wins, they
    # apply within the included entries
    sources = []
    if view:
        sources.append((view.settings(), folders))
    sources.extend((folder, [path]) for path, folder in zip(folders, project_folders))
    for source, roots in sources:
        for pattern in source.get("folder_exclude_patterns") or []:
            for glob in _convert_sublime_glob_to_rg_glob(
                pattern,
                directory=True,
                roots=roots,
            ):
                args += ("--glob", f"!{glob}")
        file_patterns = source.get("file_exclude_patterns") or []
        file_patterns += source.get("binary_file_patterns") or []
        for pattern in file_patterns:
            for glob in _convert_sublime_glob_to_rg_glob(pattern, roots=roots):
                args += ("--glob", f"!{glob}")

    return args + folders


def _path_glob_patterns(directory_glob: str) -> list[str]:
    return [
        pattern.strip()
        for pattern in directory_glob.split(",")
        if pattern.strip() and "\n" not in pattern
    ]


def _live_search(
    window: Window,
    search_query: str,
    directory_glob: str = "",
    *,
    state: SearchState | None = None,
    search_task: SearchTask | None = None,
) -> list[SearchResult]:
    state = state or search_state[window]
    if search_task is not None and state.search_task is not search_task:
        return []
    settings = _settings()
    if len(search_query) < settings.get("min_query_length", 3):
        return []

    rg_cmd = [
        "rg",
        "--field-match-separator", # use \0 as column deliminator instead of `:`
        "\\x00",
        "--no-heading",
        "--max-filesize",
        str(settings.get("max_filesize", "100M")),
        "--max-count",
        "10000",
        "--follow",
        "--hidden",
        "--with-filename",
        "--line-number",
        "--smart-case",
        *settings.get("rg_args", []),
        "-e",
        ".*".join(map(re.escape, search_query)),
    ]

    view = state.active_view or window.active_view()

    folders = []
    if state.current_file:
        if not view or not view.file_name() or "\n" in view.file_name():
            return []
        rg_cmd.append(view.file_name())
    else:
        folders = _get_sidebar_folders(window)
        if not folders:
            # Without folders rg would search its working directory
            return []

        path_globs = _path_glob_patterns(directory_glob)
        # Sublime applies every exclusion after the union of all includes,
        # independent of their order in the Where field.
        path_globs.sort(key=lambda glob: glob.startswith("-"))
        for glob in path_globs:
            exclude = glob.startswith("-")
            if exclude:
                glob = glob[1:].strip()
            if not glob:
                continue
            # `--type` exists, but it works only for a fixed list of types.
            # Sublime's Where field matches both files and directories.
            rg_globs = _convert_sublime_glob_to_rg_glob(
                glob, roots=window.folders()
            )
            normalized_glob = glob.replace("\\", "/")
            last_component = normalized_glob.rstrip("/").rsplit("/", 1)[-1]
            if normalized_glob.endswith("/") or "." not in last_component:
                rg_globs += _convert_sublime_glob_to_rg_glob(
                    glob, directory=True, roots=window.folders()
                )
            for rg_glob in dict.fromkeys(rg_globs):
                rg_cmd.extend(("--glob", f"!{rg_glob}" if exclude else rg_glob))

        # Newlines in paths split fzf's line-delimited records. Keep this
        # exclude after path globs so it cannot be overridden.
        rg_cmd.extend(("--glob", "!*\n*"))
        rg_cmd += folders

    if settings.get("debug"):
        print(" ".join(rg_cmd))

    rg_process = _create_process(rg_cmd)
    if search_task is not None:
        search_task.processes = (rg_process,)
    if search_task is not None and state.search_task is not search_task:
        rg_process.stdout.close()
        _kill_processes((rg_process,))
        search_task.processes = ()
        return []

    fzf_cmd = [
        "fzf",
        "--filter",
        search_query,
        "--delimiter",  # use \0 for column deliminator
        "\\x00",
        "--nth",  # only do fuzzy match on the third column (the file content)
        "3..",
    ]
    try:
        fzf_process = _create_process(
            fzf_cmd,
            stdin=rg_process.stdout,
            decode_stdout=True,
        )
    except Exception:
        rg_process.stdout.close()
        _kill_processes((rg_process,))
        if search_task is not None:
            search_task.processes = ()
        raise
    rg_process.stdout.close()
    processes = (rg_process, fzf_process)
    if search_task is not None:
        search_task.processes = processes
    if search_task is not None and state.search_task is not search_task:
        _kill_processes(processes)
        search_task.processes = ()
        return []

    try:
        results = []
        for _ in range(settings.get("max_results", 5000)):
            line = fzf_process.stdout.readline().rstrip("\n")
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
                    min(
                        match_indexes,
                        default=len(content) - len(content.lstrip()),
                    ),
                    content[:200],
                )
            )
        return results
    finally:
        _kill_processes(processes)
        if search_task is not None and search_task.processes is processes:
            search_task.processes = ()


def _kill_search(window: Window):
    state = search_state.get(window)
    if not state:
        return
    task = state.search_task
    state.search_task = None
    if task:
        _kill_processes(task.processes)
        task.processes = ()


def _kill_processes(processes: tuple[subprocess.Popen, ...]):
    for process in processes:
        try:
            if process.poll() is None:
                process.kill()
        except ProcessLookupError:
            pass


def _settings() -> sublime.Settings:
    return sublime.load_settings("Telescope.sublime-settings")


def _highlight_scope() -> str:
    return _settings().get("highlight_scope", "comment | region.yellowish")


def _parse_rg_result(result: str) -> tuple[str, int, str] | None:
    path, sep, rest = result.partition("\0")
    if not sep:
        return None

    line_number, sep, content = rest.partition("\0")
    if not sep or not line_number.isdigit():
        return None

    return path, int(line_number), content


def _create_process(
    args: list[str],
    stdin: Any | None = None,
    decode_stdout: bool = False,
) -> subprocess.Popen:
    cmd_args: dict[str, Any] = {}
    if sys.platform.startswith("win"):
        CREATE_NO_WINDOW = 0x08000000
        cmd_args["creationflags"] = CREATE_NO_WINDOW
    if stdin is not None:
        cmd_args["stdin"] = stdin
    if decode_stdout:
        cmd_args.update(
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    return subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        # Never read, a filling stderr pipe would block the process
        stderr=subprocess.DEVNULL,
        # rg matches its globs against the paths relative to the working
        # directory: from the root they are the absolute paths
        cwd=os.path.abspath(os.sep),
        shell=False,
        **cmd_args,
    )
