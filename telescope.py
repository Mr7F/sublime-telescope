import re
import json
from dataclasses import dataclass
import sublime
import sublime_plugin
import shlex
import os
from os.path import expanduser
from sys import platform

# perform less than 1 update each "x" ms
# to avoid making the UI lagging
MIN_UPDATE_PERIOD = 200

regions_to_add = {}
search_queries = {}  # Used for debounce per output panel

current_selection = {}  # {window: (result_index, Region, view, search_results)}

# Save the view scroll to restore it when pressing escape
init_views_scroll = {}  # {window: {view: int}}
init_active_view = {}  # {window: view}
skip_telescope_reset = False


@dataclass
class SearchResult:
    path: str
    line_number: int
    # Position of the match in the line
    line_position: "tuple[int, int]"
    # Region in the IO panel
    region_io: "tuple[int, int]"


class TelescopeCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        global init_active_view
        self.window = self.view.window()
        init_views_scroll[self.window] = {}
        for view in self.view.window().views(include_transient=True):
            init_views_scroll[self.window][view] = view.viewport_position()
        init_active_view[self.window] = self.window.active_view()

        self.out_view, self.in_view = self.window.create_io_panel(
            "telescope",
            on_input=self.on_enter,
        )
        settings = self.out_view.settings()
        settings.set("gutter", False)
        settings = self.in_view.settings()
        settings.set("is_input_widget", True)

        if len(self.view.sel()):
            text = self.view.substr(self.view.sel()[-1])
        if text and "\n" not in text:
            self.in_view.run_command("telescope_set_initial_search", {"search": text})
            self.out_view.run_command("telescope_query", {"search_query": text})
        else:
            self.in_view.run_command("telescope_clear")

        self.window.run_command("show_panel", {"panel": "output.telescope"})

    def on_enter(self, value):
        # We pressed enter, jump to the selected item
        global current_selection, skip_telescope_reset

        vv = current_selection.pop(self.window, None)
        if not vv:
            self.window.run_command("hide_panel")
            return

        index, region, view, _search_results = vv
        _reset_initial_state(self.window, close=False)
        self.window.focus_view(view)
        view.sel().clear()
        view.sel().add(region)

        skip_telescope_reset = True
        self.window.run_command("hide_panel")


class TelescopeQueryCommand(sublime_plugin.TextCommand):
    """Executed on the output panel, execute the search."""

    def run(self, edit, search_query):
        """Search using the query.

        Syntax queries are splited using space,
        - `~` -> search in the home directory instead of searching in the current project
        - `*.py` -> search only in the file having `.py` extension
        - `-*.js` -> search only in the file having `.py` extension
        """
        global search_queries

        was_none = self.view.window() not in search_queries
        search_queries[self.view] = search_query
        if was_none:
            sublime.set_timeout_async(self._run, 0)
        else:
            sublime.set_timeout_async(self._run, MIN_UPDATE_PERIOD)

    def _run(self):
        global search_queries
        search_query = search_queries.pop(self.view, None)
        if search_query is None:
            return

        if not search_query:
            self.view.run_command("telescope_set_result", {"result": []})
            return

        search_terms = search_query.split(" ")
        search = []
        args = []
        folders = self.view.window().folders()

        # Exclude binary files and excluded pattern (by default, search in the sidebar tree)
        exclude_patterns = self.view.settings().get("binary_file_patterns") or []
        exclude_patterns += self.view.settings().get("file_exclude_patterns") or []
        exclude_patterns += [
            f"**/{f}**/"
            for f in self.view.settings().get("folder_exclude_patterns") or []
        ]

        args.extend(("--glob", f"!{e}") for e in exclude_patterns)

        for s in search_terms:
            if s.startswith(("*", "-*")):
                s = s.replace("-*", "!*")
                s = re.sub(r"\*+", "**", s)  # Change `*` to be recursive
                args.append(("--glob", s))
            elif s == "~":
                folders = [expanduser("~")]
            elif s.strip():
                search.append(s)

        search = " ".join(search)

        only_files = False
        if not search:
            only_files = True
            args.append(("--files-with-matches", ""))

        # - max-count: maximum number of lines for each file (many match on the same column)
        # - follow: follow symlink (like sublime text search)
        # - smart-case: case-insensitive if the search query is lower case
        cmd = (
            "rg --json --smart-case --max-filesize 100M --max-count 100 --follow --fixed-strings %(args)s %(search)s %(base_dir)s"
            % {
                "search": shlex.quote(search) or "''",
                "base_dir": " ".join(shlex.quote(d) for d in folders),
                "args": " ".join(
                    f"{name} {shlex.quote(value)}" for name, value in args
                ),
            }
        )

        if "linux" in platform or "darwin" in platform:
            # TODO: should work on windows
            cmd = "timeout 1s " + cmd + " | head -n200"

        print(cmd)

        self.view.run_command(
            "telescope_set_result",
            {"result": list(os.popen(cmd).readlines()), "only_files": only_files},
        )


class TelescopeSetResultCommand(sublime_plugin.TextCommand):
    """Executed on the output panel, set the result in the view."""

    def run(self, edit, result, only_files=False):
        self.view.erase(edit, sublime.Region(0, self.view.size()))
        self.view.assign_syntax("Packages/Default/Find Results.hidden-tmLanguage")
        self.view.settings().set("result_file_regex", r"^([^ \t].*):$")
        self.view.settings().set("result_line_regex", r"^ +([0-9]+):")

        search_results = []

        to_show = ""
        ii_view = 0
        for i, line in enumerate(result):
            if not line.strip():
                continue

            if only_files:
                # We didn't search anything except the path
                path = line[:-1]
                to_show += path + "\n"
                offset = ii_view + len(to_show) - 1
                search_results.append(SearchResult(path, 0, (0, 0), (offset, offset)))
            else:
                line = json.loads(line)
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
                    to_show += "\n " + str(line_number) + ": "
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

        sublime.set_timeout_async(lambda: self._run(search_results))

    def _run(self, search_results: "list[SearchResult]"):
        window = self.view.window()
        _next_result(window, self.view, 0, search_results, 0)


class TelescopeClearCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        self.view.erase(edit, sublime.Region(0, self.view.size()))


class TelescopeSetInitialSearchCommand(sublime_plugin.TextCommand):
    def run(self, edit, search):
        self.view.replace(edit, sublime.Region(0, self.view.size()), search)
        self.view.sel().add(sublime.Region(0, self.view.size()))


class TelescopeFindNextCommand(sublime_plugin.WindowCommand):
    def run(self):
        out_panel, inp_panel = self.window.find_io_panel("telescope")
        if not out_panel:
            return
        result_index, r_view, _view, search_results = current_selection[self.window]
        _next_result(self.window, out_panel, 1, search_results, result_index)


class TelescopeFindPrevCommand(sublime_plugin.WindowCommand):
    def run(self):
        out_panel, inp_panel = self.window.find_io_panel("telescope")
        if not out_panel:
            return
        result_index, r_view, _view, search_results = current_selection[self.window]
        _next_result(self.window, out_panel, -1, search_results, result_index)


def _next_result(window, output_panel, direction, search_results, result_index):
    result_index = (result_index + direction) % (len(search_results) or 1)
    regions = [sublime.Region(*r.region_io) for r in search_results]

    _reset_initial_views_scroll(window)

    if search_results:
        view = window.open_file(
            search_results[result_index].path,
            flags=sublime.SEMI_TRANSIENT | sublime.REPLACE_MRU,
        )
        _set_file_view_regions(view, search_results, result_index)
    else:
        current_selection[window] = None

    output_panel.sel().clear()
    output_panel.sel().add(sublime.Region(0, 0))
    output_panel.show(sublime.Region(0, 0))
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
    def on_modified(self, view: sublime.View):
        # TODO: better way to catch "on_input" on the input panel?
        if not view.settings().get("is_input_widget") or not view.window():
            return

        out_panel, inp_panel = view.window().find_io_panel("telescope")
        if inp_panel == view:
            search_query = view.substr(sublime.Region(0, view.size()))
            out_panel.run_command("telescope_query", {"search_query": search_query})

    def on_window_command(self, window, command_name, args):
        global skip_telescope_reset
        # TODO: better way to catch when the `io_panel` is closed?
        if command_name != "hide_panel":
            return

        if skip_telescope_reset:
            skip_telescope_reset = False
            return

        # Restore the state to what it was before we executed the command
        if window.active_panel() == "output.telescope":
            _reset_initial_state(window)

    def on_load(self, view):
        window = view.window()
        if window in regions_to_add and view == regions_to_add[window][0]:
            _set_file_view_regions(*regions_to_add[window])
            del regions_to_add[window]


def _reset_initial_state(window, close=True):
    global init_active_view
    if window in init_active_view:
        window.focus_view(init_active_view[window])
    _reset_initial_views_scroll(window)

    if close:
        for view in window.views(include_transient=True):
            if (
                view not in init_views_scroll[window]
                and view.is_valid()
                and view.sheet()
                and view.sheet().is_semi_transient()
            ):
                view.close()

    init_views_scroll[window] = {}
    del init_active_view[window]


def _reset_initial_views_scroll(window):
    for view in window.views(include_transient=True):
        view.erase_regions("telescope-result-view")
        if view in init_views_scroll[window]:
            view.set_viewport_position(init_views_scroll[window][view])


def _set_file_view_regions(
    view,
    search_results: "list[SearchResult]",
    result_index: int,
):
    """Set the region in the file we opened."""
    global current_selection

    if view.is_loading():
        # Need to wait
        regions_to_add[view.window()] = (view, search_results, result_index)
        return

    _, inp_panel = view.window().find_io_panel("telescope")
    if not inp_panel:
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
    view.show(r_view)
    view.window().focus_view(inp_panel)

    view.add_regions(
        "telescope-result-view",
        [r_view],
        icon="",
        scope="comment | region.yellowish",
    )
    current_selection[view.window()] = (result_index, r_view, view, search_results)
