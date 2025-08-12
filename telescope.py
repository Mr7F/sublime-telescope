from dataclasses import dataclass
import sublime
import sublime_plugin
import shlex
import os
from sys import platform
import time

# perform less than 1 update each "x" ms, waiting for https://github.com/sublimehq/sublime_text/issues/4796
DEBOUNCE_MS = 500

regions_to_add = {}

init_active_view = {}  # {window: view}

preview_panels = {}

current_text = ""
current_sel = [(0, 0)]
current_extension = ""
current_highlight_index = -1

search_results = ()
is_telescope_open = False
skip_next_auto_select = False


@dataclass
class SearchResult:
    path: str
    line_number: int
    # Position of the match in the line
    line_position: "tuple[int, int]"
    # Region in the IO panel
    line_content: str


class TelescopeCommand(sublime_plugin.WindowCommand):
    """Executed on the output panel, set the result in the view."""

    def run(self, extension, result):
        global search_results
        print("run command", extension, result)

        for view in self.window.views(include_transient=True):
            view.erase_regions("telescope-result-view")
        s = search_results[int(result.split(":", 1)[0])]
        # TODO: keep transient view if possible
        self.window.open_file(s.path, flags=sublime.SEMI_TRANSIENT)

    def input(self, args):
        print("input", args)
        _save_initial_state(self.window)

        if "extension" not in args:
            print("here")
            return ExtensionInputHandler("extension", self.window)

        if "result" not in args:
            return TelescopeListInputHandler("result", self.window)


class ExtensionInputHandler(sublime_plugin.TextInputHandler):
    def __init__(self, name, window):
        global current_extension, skip_next_auto_select
        self._name = name
        self.window = window

        if current_extension:
            # An other hack because we miss feature in the API...
            # Auto-select the item, but it needs to not auto-select
            # the ListInput
            skip_next_auto_select = time.time() + 0.5
            sublime.set_timeout(lambda: self.window.run_command("select"))

    def name(self):
        return self._name

    def placeholder(self):
        return "File Extension"

    def initial_text(self):
        return current_extension

    def next_input(self, args):
        global current_extension
        current_extension = args[self._name]
        if "result" not in args:
            return TelescopeListInputHandler("result", self.window)


class TelescopeListInputHandler(sublime_plugin.ListInputHandler):
    def __init__(self, name, window):
        global is_telescope_open, search_results
        is_telescope_open = True
        self._name = name
        self.window = window

    def name(self):
        return self._name

    def initial_text(self):
        global current_text
        print("initial query", current_text, len(current_text))
        return current_text

    def want_event(self):
        return True

    def initial_selection(self):
        print("initial_selection", current_sel)
        return current_sel

    def placeholder(self):
        return "Fuzzy find"

    def cancel(self):
        global search_results, is_telescope_open, current_text

        is_telescope_open = False
        for view in self.window.views(include_transient=True):
            view.erase_regions("telescope-result-view")

        _search_queries.pop(self.window, None)  # Cancel all debounce
        _reset_initial_state(self.window)

    def validate(self, text, event):
        global search_results, is_telescope_open, current_text, skip_next_auto_select

        if skip_next_auto_select and skip_next_auto_select >= time.time():
            return False

        print("validate", text, event)
        if not (text or "").strip():
            return False

        is_telescope_open = False
        return True

    def preview(self, text):
        global current_highlight_index
        if (text or "").strip():
            current_highlight_index = int(text.split(":", 1)[0])
            print("current_highlight_index", current_highlight_index)
            _next_result(self.window, search_results, current_highlight_index)

    def list_items(self):
        first_el = sublime.ListInputItem(
            text=_fixed_size("", 100),
            details=_fixed_size("", 100),
            value=None,
        )
        print("list_items")
        if not search_results:
            return [first_el]

        print("current_highlight_index", current_highlight_index)

        return [
            sublime.ListInputItem(
                text=_fixed_size(s.line_content.strip(), 100),
                details=_fixed_size(
                    f"{s.path}:{s.line_number}:{s.line_position[0]}", 100
                ),
                # TODO: remove that hack (otherwise it's closed)
                value=str(i) + ":" + s.line_content.strip(),
                annotation="",
            )
            for i, s in enumerate(search_results)
        ], current_highlight_index


class IoPanelEventListener(sublime_plugin.EventListener):
    def on_modified(self, view: sublime.View):
        global current_text, is_telescope_open, current_sel
        # print("on_modified_async", view.substr(sublime.Region(0, view.size())))
        # print(view.element(), is_telescope_open)

        # TODO: is there a better way to detect that it's the telescope quick panel?
        if view.element() == "command_palette:input" and is_telescope_open:
            query = view.substr(sublime.Region(0, view.size()))
            # print("here", view.element(), view.name(), query)
            window = view.window()
            if query == current_text:
                return

            current_text = query
            current_sel = [s.to_tuple() for s in view.sel()]

            _debounced_live_search(window, query, current_extension)

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
_last_calls = {}


def _debounced_live_search(window, search_query, extension):
    print("_debounced_live_search")

    last_call = _last_calls.get(window, -float("inf"))
    _search_queries[window] = (search_query, extension)
    delay_ms = min(DEBOUNCE_MS, max(0, DEBOUNCE_MS - (last_call - time.time() * 1000)))
    print("delay_ms", delay_ms)
    sublime.set_timeout_async(lambda w=window: _call_live_search(w), delay_ms)


def _call_live_search(window):
    global _last_calls
    if window not in _search_queries:
        return

    last_call = _last_calls.get(window, -float("inf"))
    query, extension = _search_queries[window]
    if (time.time() - last_call) * 1000 < DEBOUNCE_MS:
        return

    _last_calls[window] = float("inf")
    _search_queries[window] = (query, extension)
    _live_search(window, query, extension)
    _last_calls[window] = time.time()


def _live_search(window, search_query, extension):
    global preview_panels, search_results, current_sel, current_text
    if len(search_query) < 3:
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
        rg -t %(extension)s --no-heading --max-filesize 100M --max-count 10000 --follow --line-number --fixed-strings %(rg_search)s %(base_dir)s
        | fzf --filter %(search_query)s
        | head -n50
        """.strip().replace("\n", " ")
        % {
            "extension": shlex.quote(extension),
            "base_dir": " ".join(shlex.quote(d) for d in folders),
            "rg_search": " ".join(f"-e {shlex.quote(p)}" for p in rg_search),
            "search_query": shlex.quote(search_query),
        }
    )

    # print(cmd)
    print("run", search_query)

    search_results = []

    for i, line in enumerate(os.popen(cmd).readlines()):
        if not line.strip():
            continue
        path, line_number, content = line.split(":", 2)
        to_trim = next((i for i, s in enumerate(content) if s.strip()), 0)
        content = content.strip()
        # print(path, line_number, content)

        search_results.append(
            SearchResult(
                path,
                int(line_number),
                (to_trim, to_trim + len(content)),
                content[:200],
            )
        )

    if command_panel := _get_command_panel(window):
        current_text = command_panel.substr(sublime.Region(0, command_panel.size()))
        current_sel = [s.to_tuple() for s in command_panel.sel()]
    window.run_command("hide_overlay")
    window.run_command("telescope")


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


def _get_command_panel(window):
    # TODO: is there a better way?
    # Because of the debounced, the view can be destroyed if we are late
    for n in range(1000):
        view = sublime.View(n)
        if view.element() != "command_palette:input":
            continue
        if view.window() != window:
            continue
        return view


def _fixed_size(s, size):
    """Make the string having a fixed size."""
    s = s or ""
    s = s[:size]
    s += " " * (size - len(s))
    return s
