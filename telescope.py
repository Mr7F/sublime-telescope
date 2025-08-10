import re
import json
from dataclasses import dataclass
import sublime
import sublime_plugin
import shlex
import os
from sys import platform

# perform less than 1 update each "x" ms
# to avoid making the UI lagging
DEBOUNCE_MS = 150

regions_to_add = {}

current_selection = {}  # {window: (result_index, Region, view, search_results)}

init_active_view = {}  # {window: view}


output_panels = {}
preview_panels = {}


@dataclass
class SearchResult:
    path: str
    line_number: int
    # Position of the match in the line
    line_position: "tuple[int, int]"
    # Region in the IO panel
    region_io: "tuple[int, int]"


class TelescopeSetResultCommand(sublime_plugin.TextCommand):
    """Executed on the output panel, set the result in the view."""

    def run(self, edit, result):
        self.view.set_read_only(False)
        self.view.erase(edit, sublime.Region(0, self.view.size()))
        self.view.assign_syntax("Packages/Default/Find Results.hidden-tmLanguage")
        self.view.settings().set("result_file_regex", r"^([^ \t].*):$")
        self.view.settings().set("result_line_regex", r"^ +([0-9]+):")
        self.view.settings().set("gutter", False)
        self.view.settings().set("word_wrap", False)
        self.view.set_scratch(True)

        result = [json.loads(line) for line in result if line.strip()]

        search_results = []

        max_line_no = max(
            (
                r.get("data", {}).get("line_number", 0)
                for r in result
                if r.get("type") == "match"
            ),
            default=0,
        )

        to_show = ""
        ii_view = 0
        for i, line in enumerate(result):
            if line.get("type") == "begin":
                path = line.get("data", {}).get("path", {}).get("text") or ""
                if to_show:
                    to_show += "\n\n"
                to_show += path + ":"

            if line.get("type") == "match":
                data = line.get("data", {})
                content = data.get("lines", {}).get("text").replace("\n", " ")
                path = line.get("data", {}).get("path", {}).get("text") or ""
                line_number = data.get("line_number", 0)
                to_show += (
                    "\n " + str(line_number).rjust(len(str(max_line_no)), " ") + ": "
                )
                to_trim = next((i for i, s in enumerate(content) if s.strip()), 0)
                offset = ii_view + len(to_show) - to_trim

                search_results.extend(
                    SearchResult(
                        path,
                        line_number,
                        (m["start"], m["end"]),
                        (m["start"] + offset, m["end"] + offset),
                    )
                    for m in data.get("submatches", ())
                )
                to_show += content[to_trim:][:500]

            if i % 100 == 0:
                self.view.insert(edit, self.view.size(), to_show)
                ii_view += len(to_show)
                to_show = ""

        if to_show:
            self.view.insert(edit, self.view.size(), to_show)

        self.view.set_read_only(True)
        sublime.set_timeout_async(lambda: self._run(search_results))

    def _run(self, search_results: "list[SearchResult]"):
        window = self.view.window()
        _next_result(window, self.view, 0, search_results, 0)


class TelescopeFindNextCommand(sublime_plugin.WindowCommand):
    def run(self):
        result_index, r_view, _view, search_results = current_selection[self.window]
        output_panel = output_panels[self.window]
        _next_result(self.window, output_panel, 1, search_results, result_index)


class TelescopeFindPrevCommand(sublime_plugin.WindowCommand):
    def run(self):
        result_index, r_view, _view, search_results = current_selection[self.window]
        output_panel = output_panels[self.window]
        _next_result(self.window, output_panel, -1, search_results, result_index)


def _next_result(window, output_panel, direction, search_results, result_index):
    result_index = (result_index + direction) % (len(search_results) or 1)

    if search_results:
        if (
            window not in preview_panels
            or preview_panels[window].file_name() != search_results[result_index].path
        ):
            if window in preview_panels:
                preview_panels[window].close()

            preview_panels[window] = window.open_file(
                search_results[result_index].path,
                flags=sublime.ADD_TO_SELECTION | sublime.SEMI_TRANSIENT,
            )
            # preview_panels[window].set_read_only(True)

        _set_file_view_regions(preview_panels[window], search_results, result_index)

        find_in_files_input = _get_find_in_files_input(window)
        if find_in_files_input:
            window.focus_view(find_in_files_input)

    else:
        current_selection[window] = None

    regions = [sublime.Region(*r.region_io) for r in search_results]
    output_panel.add_regions(
        "telescope-result-output",
        regions,
        icon="",
        scope="comment",
        flags=sublime.DRAW_NO_FILL,
    )
    if search_results:
        output_panel.add_regions(
            "telescope-result-output-current",
            [regions[result_index]],
            icon="",
            scope="comment | region.yellowish",
            flags=sublime.DRAW_EMPTY,
        )
        output_panel.show(regions[result_index])


class IoPanelEventListener(sublime_plugin.EventListener):
    def on_modified_async(self, view: sublime.View):
        if view.element() == "find_in_files:input:find":
            window = sublime.active_window()
            if window.active_panel() == "find_in_files":
                search_query = view.substr(sublime.Region(0, view.size()))
                _debounced_live_search(window, search_query)

    def on_window_command(self, window, command_name, args):
        print("command_name", command_name, args, command_name == "find_all")

        if command_name in (
            "toggle_whole_word",
            "toggle_case_sensitive",
            "toggle_regex",
        ):
            pass

        if (
            command_name == "show_panel"
            and args.get("panel") == "find_in_files"
            and window.active_panel() != "find_in_files"
        ):
            _save_initial_state(window)
            return

        if command_name == "hide_panel" and window.active_panel() == "find_in_files":
            _reset_initial_state(window)

    def on_load(self, view):
        window = view.window()
        if window in regions_to_add and view == regions_to_add[window][0]:
            _set_file_view_regions(*regions_to_add[window])
            del regions_to_add[window]

    def on_close(self, view):
        if view.element() == "find_in_files:output":
            _reset_initial_state(view.window(), focus_old_view=False)


def _save_initial_state(window):
    global init_active_view
    init_active_view[window] = window.active_view()


def _reset_initial_state(window, focus_old_view=True):
    global init_active_view, output_panels, preview_panels
    if window in init_active_view and focus_old_view:
        window.focus_view(init_active_view[window])

    output_panel = output_panels.get(window)
    if output_panel:
        output_panel.close()

    preview_panel = preview_panels.get(window)
    if preview_panel and preview_panel.sheet().is_semi_transient():
        preview_panel.close()

    del init_active_view[window]


_search_queries = {}


def _debounced_live_search(window, search_query):
    _search_queries[window] = search_query

    def _call():
        if window not in _search_queries:
            return
        search_query = _search_queries[window]
        del _search_queries[window]
        _live_search(window, search_query)

    sublime.set_timeout_async(_call, DEBOUNCE_MS)


def _live_search(window, search_query):
    global output_panels, preview_panels

    view = window.active_view()

    args = []
    folders = window.folders()

    # Exclude binary files and excluded pattern (by default, search in the sidebar tree)
    exclude_patterns = view.settings().get("binary_file_patterns") or []
    exclude_patterns += view.settings().get("file_exclude_patterns") or []
    exclude_patterns += [
        f"**/{f}**/" for f in view.settings().get("folder_exclude_patterns") or []
    ]
    args.extend(("--glob", f"!{e}") for e in exclude_patterns)

    locations = [l.strip() for l in _get_location(window).split(",") if l.strip()]
    for location in locations:
        if location.startswith("-"):
            location = "!" + location[1:]
        if location == "*":
            continue
        location = re.sub(r"\*+", "**", location)  # Change `*` to be recursive
        args.extend(("--glob", location) for e in exclude_patterns)

    # - max-count: maximum number of lines for each file (many match on the same column)
    # - follow: follow symlink (like sublime text search)
    # - smart-case: case-insensitive if the search query is lower case
    cmd = (
        "rg --json --smart-case --max-filesize 100M --max-count 100 --follow --fixed-strings %(args)s %(search)s %(base_dir)s"
        % {
            "search": shlex.quote(search_query) or "''",
            "base_dir": " ".join(shlex.quote(d) for d in folders),
            "args": " ".join(f"{name} {shlex.quote(value)}" for name, value in args),
        }
    )

    if "linux" in platform or "darwin" in platform:
        # TODO: should work on windows
        cmd = "timeout 1s " + cmd + " | head -n200"

    # print(cmd)

    output_panels[window] = window.open_file(
        "/tmp/telescope-result",
        flags=sublime.SEMI_TRANSIENT,
    )

    output_panels[window].run_command(
        "telescope_set_result",
        {"result": list(os.popen(cmd).readlines())},
    )


def _get_location(window):
    # TODO: is there a better way?
    for n in range(1000):
        view = sublime.View(n)
        if view.element() != "find_in_files:input:location":
            continue
        if view.window() != window:
            continue
        return view.substr(sublime.Region(0, view.size()))
    return ""


def _get_find_in_files_input(window):
    # TODO: is there a better way?
    for n in range(1000):
        view = sublime.View(n)
        if view.element() != "find_in_files:input:find":
            continue
        if view.window() != window:
            continue
        return view


def _set_file_view_regions(
    view,
    search_results: "list[SearchResult]",
    result_index: int,
):
    """Set the region in the preview file we opened."""
    global current_selection
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
    view.show(r_view, animate=False)
    view.add_regions(
        "telescope-result-view",
        [r_view],
        icon="",
        scope="comment | region.yellowish",
    )
    current_selection[view.window()] = (result_index, r_view, view, search_results)
