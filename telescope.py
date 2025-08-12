from dataclasses import dataclass
import sublime
import sublime_plugin
import shlex
import os


# LSP "Go to symbols" has a similar feature
# Hope this issue is fixed one day...
# > https://github.com/sublimehq/sublime_text/issues/4796
from .utils import DynamicListInputHandler, PreselectedListInputHandler


regions_to_add = {}
init_active_view = {}  # {window: view}
init_view_sel = {}  # {window: {view: sel}}
preview_panels = {}

current_extension = ""
current_highlight_index = -1

search_results = ()


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
        # Always set the input to see the breadcrumb item
        # when reloading the command input with the hack in `utils.py`
        if not args:
            _save_initial_state(self.window)
        return ExtensionInputHandler(self)


class ExtensionInputHandler(PreselectedListInputHandler):
    def __init__(self, window_command):
        self.window_command = window_command

        self.extensions, default = _get_valid_file_extensions(
            self.window_command.window
        )
        super().__init__(window_command.window, current_extension or default)

    def get_list_items(self):
        return self.extensions

    def name(self):
        return "extension"

    def placeholder(self):
        return "File Extension"

    def next_input(self, args):
        global current_extension
        current_extension = args[self.name()]
        if "result" not in args:
            return TelescopeListInputHandler(self.window_command, args)


class TelescopeListInputHandler(DynamicListInputHandler):
    def __init__(self, window_command, args):
        super().__init__(window_command, args)
        self.window_command = window_command

    def name(self):
        return "result"

    def placeholder(self):
        return "Fuzzy find"

    def cancel(self):
        for view in self.window_command.window.views(include_transient=True):
            view.erase_regions("telescope-result-view")

        _reset_initial_state(self.window_command.window)

    def validate(self, text):
        global search_results

        if not (text or "").strip():
            return False

        return True

    def initial_selection(self):
        if hasattr(self.command, "_selection"):
            return self.command._selection
        return super().initial_selection()

    def preview(self, text):
        """Save the current highlighted index and show the preview.

        Save the highlighted element, so we can re-open the view
        at the same position.
        """
        global current_highlight_index
        if (text or "").strip():
            current_highlight_index = int(text.split(":", 1)[0])
            _preview_result(
                self.window_command.window,
                search_results,
                current_highlight_index,
            )

    def on_modified(self, text: str) -> None:
        global search_results
        search_results = _live_search(
            self.window_command.window,
            text,
            self.args["extension"],
        )

        setattr(
            self.command,
            "_selection",
            [s.to_tuple() for s in self.input_view.sel()],
        )
        self.update(self._list_items(search_results))

    def get_list_items(self):
        return self._list_items(search_results)

    def _list_items(self, search_results):
        if not search_results:
            return []

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
    def on_load(self, view):
        window = view.window()
        if window in regions_to_add and view == regions_to_add[window][0]:
            _set_file_view_regions(*regions_to_add[window])
            del regions_to_add[window]


def _save_initial_state(window):
    global init_active_view
    init_active_view[window] = window.active_view()

    init_view_sel[window] = {}
    for view in window.views():
        init_view_sel[window][view] = list(view.sel())


def _reset_initial_state(window, focus_old_view=True, close_preview=False):
    global init_active_view, preview_panels
    if window in init_active_view and focus_old_view:
        window.focus_view(init_active_view[window])

    if close_preview:
        preview_panel = preview_panels.get(window)
        if preview_panel and preview_panel.sheet().is_semi_transient():
            preview_panel.close()

    if window in init_view_sel:
        for view, sel in init_view_sel[window].items():
            view.sel().clear()
            view.sel().add_all(sel)


def _live_search(window, search_query, extension):
    global preview_panels, search_results
    if len(search_query) < 3:
        return

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
            "base_dir": " ".join(shlex.quote(d) for d in window.folders()),
            "rg_search": " ".join(f"-e {shlex.quote(p)}" for p in rg_search),
            "search_query": shlex.quote(search_query),
        }
    )

    search_results = []
    for i, line in enumerate(os.popen(cmd).readlines()):
        if not line.strip():
            continue
        path, line_number, content = line.split(":", 2)
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

    return search_results


def _preview_result(window, search_results, result_index):
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


def _fixed_size(s, size):
    """Make the string having a fixed size."""
    s = s or ""
    s = s[:size]
    s += " " * (size - len(s))
    return s


_extension_cache = {}


def _get_valid_file_extensions(window):
    if window not in _extension_cache:
        _get_valid_file_extensions_update_cache(window)
    else:
        sublime.set_timeout_async(
            lambda: _get_valid_file_extensions_update_cache(window)
        )

    return _extension_cache[window]


def _get_valid_file_extensions_update_cache(window):
    # Execute the first time, the second time returns the cached value
    # and update the cache in background
    view = window.active_view()
    exclude_patterns = view.settings().get("binary_file_patterns") or []
    exclude_patterns += view.settings().get("file_exclude_patterns") or []
    exclude_patterns += [
        f"**/{f}**/" for f in view.settings().get("folder_exclude_patterns") or []
    ]

    glob = " ".join(f"--iglob {shlex.quote('!' + e)}" for e in exclude_patterns)
    cmd = (
        """
        rg  --no-heading --follow --max-filesize 100M --files-with-matches --fixed-strings %(glob)s --glob= '' %(base_dir)s | sed -n 's/.*\(\.[^/]*\)$/\\1/p' | sort -u
        """.strip().replace("\n", " ")
        % {
            "base_dir": " ".join(shlex.quote(d) for d in window.folders()),
            "glob": glob,
        }
    )

    extensions = [e.strip()[1:] for e in os.popen(cmd).readlines()]

    default = None
    if file_name := view.file_name():
        default = file_name.rsplit(".", 1)[-1]
        if default not in extensions:
            extensions.append(default)

    _extension_cache[window] = extensions, default
