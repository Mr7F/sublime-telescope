import re
import json
from dataclasses import dataclass
import sublime
import sublime_plugin
import shlex
import os
from sys import platform
import time

# perform less than 1 update each "x" ms, waiting for https://github.com/sublimehq/sublime_text/issues/4796
DEBOUNCE_MS = 1000

regions_to_add = {}

init_active_view = {}  # {window: view}

preview_panels = {}

current_text = ""

search_results = ()
is_telescope_open = False


@dataclass
class SearchResult:
    path: str
    line_number: int
    # Position of the match in the line
    line_position: "tuple[int, int]"
    # Region in the IO panel
    line_content: str


def _fixed_size(s, size):
    """Make the string having a fixed size."""
    s = s or ""
    s = s[:size]
    s += " " * (size - len(s))
    return s


class TelescopeCommand(sublime_plugin.WindowCommand):
    """Executed on the output panel, set the result in the view."""

    def run(self, text=None):
        global current_text, search_results, is_telescope_open
        if text is None:
            # Initial call
            _save_initial_state(self.window)
            print("search result", len(search_results))

        if search_results:
            result = [
                sublime.QuickPanelItem(
                    trigger=_fixed_size(s.line_content.strip(), 200),
                    details=f"{s.path}:{s.line_number}:{s.line_position[0]}",
                )
                for s in search_results
            ]
            _next_result(self.window, 0, search_results)
        else:
            result = [[_fixed_size(" ", 200), _fixed_size(" ", 200)]]

        current_text = text
        self.window.show_quick_panel(
            result,
            on_select=self.on_select,
            on_highlight=self.on_highlight,
        )
        is_telescope_open = True
        if text:
            # TODO: keep selection
            self.window.run_command("append", {"characters": text})
            self.window.run_command("move_to", {"to": "eol"})

    def on_select(self, idx):
        global search_results, is_telescope_open
        is_telescope_open = False

        for view in self.window.views(include_transient=True):
            view.erase_regions("telescope-result-view")

        _search_queries.pop(self.window, None)  # Cancel all debounce

        if idx >= 0:  # TODO: on_cancel
            print("on_select", idx)
            s = search_results[idx]
            # TODO: keep transient view if possible
            view = self.window.open_file(s.path, flags=sublime.SEMI_TRANSIENT)
            view = preview_panels[self.window]
        else:
            _reset_initial_state(self.window)

    def on_highlight(self, idx):
        _next_result(self.window, search_results, idx)


class IoPanelEventListener(sublime_plugin.EventListener):
    def on_modified_async(self, view: sublime.View):
        global current_text, is_telescope_open
        # TODO: is there a better way to detect that it's the telescope quick panel?
        if view.element() == "quick_panel:input" and is_telescope_open:
            print(view.element(), view.name())
            window = view.window()
            query = view.substr(sublime.Region(0, view.size()))
            if query == current_text:
                return

            _debounced_live_search(window, query)

    def on_window_command(self, window, command_name, args):
        print("command_name", command_name, args, command_name == "find_all")

    def on_load(self, view):
        window = view.window()
        if window in regions_to_add and view == regions_to_add[window][0]:
            _set_file_view_regions(*regions_to_add[window])
            del regions_to_add[window]


def _save_initial_state(window):
    global init_active_view
    init_active_view[window] = window.active_view()
    print("_save_initial_state", init_active_view)


def _reset_initial_state(window, focus_old_view=True, close_preview=False):
    global init_active_view, preview_panels
    print("_reset_initial_state", init_active_view)
    if window in init_active_view and focus_old_view:
        window.focus_view(init_active_view[window])

    if close_preview:
        preview_panel = preview_panels.get(window)
        if preview_panel and preview_panel.sheet().is_semi_transient():
            preview_panel.close()

    # del init_active_view[window]


_search_queries = {}


def _debounced_live_search(window, search_query):
    now = time.time()

    if window not in _search_queries:
        _search_queries[window] = (now, search_query)
        _call_live_search(window)
    else:
        last_call, _ = _search_queries[window]
        _search_queries[window] = (last_call, search_query)

        delay_ms = max(0, DEBOUNCE_MS - int((now - last_call) * 1000))
        sublime.set_timeout_async(lambda w=window: _call_live_search(w), delay_ms)


def _call_live_search(window):
    if window not in _search_queries:
        return

    last_call, query = _search_queries[window]
    if int((time.time() - last_call) * 1000) < DEBOUNCE_MS:
        return

    _search_queries[window] = (time.time(), query)
    _live_search(window, query)


def _live_search(window, search_query):
    global preview_panels, search_results
    if len(search_query) < 5:
        return

    folders = window.folders()

    # Heuristic to filter result with ripgrep first
    rg_search = []
    for i in range(len(search_query) - 2):
        rg_search.append(search_query[i : i + 3])
    rg_search = {
        rg_search[0],
        rg_search[-1],
        rg_search[len(rg_search) // 2],
    }

    cmd = (
        """
        rg -t py --no-heading --max-filesize 100M --max-count 100 --follow --line-number --fixed-strings %(rg_search)s  %(base_dir)s | fzf --filter %(search_query)s | head -n50
        """.strip()
        % {
            "base_dir": " ".join(shlex.quote(d) for d in folders),
            "rg_search": " ".join(f"-e {shlex.quote(p)}" for p in rg_search),
            "search_query": shlex.quote(search_query),
        }
    )

    print(cmd)

    search_results = []

    for i, line in enumerate(os.popen(cmd).readlines()):
        if not line.strip():
            continue
        path, line_number, content = line.split(":", 2)
        to_trim = next((i for i, s in enumerate(content) if s.strip()), 0)
        content = content.strip()
        print(path, line_number, content)

        search_results.append(
            SearchResult(
                path,
                int(line_number),
                (to_trim, to_trim + len(content)),
                content[:200],
            )
        )

    quick_panel = _get_quick_panel(window)
    if quick_panel:
        search_query = quick_panel.substr(sublime.Region(0, quick_panel.size()))
    window.run_command("hide_overlay")
    window.run_command("telescope", {"text": search_query})


def _next_result(window, search_results, result_index):
    if not search_results:
        return

    if (
        window not in preview_panels
        or preview_panels[window].file_name() != search_results[result_index].path
    ):
        preview_panels[window] = window.open_file(
            search_results[result_index].path,
            flags=sublime.TRANSIENT,
        )

    _set_file_view_regions(preview_panels[window], search_results, result_index)


def _set_file_view_regions(
    view,
    search_results: "list[SearchResult]",
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


def _get_quick_panel(window):
    # TODO: is there a better way?
    for n in range(1000):
        view = sublime.View(n)
        if view.element() != "quick_panel:input":
            continue
        if view.window() != window:
            continue
        return view
