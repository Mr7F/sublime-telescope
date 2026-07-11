import os
import shutil
import tempfile

import sublime
from unittesting import DeferrableTestCase


def _write(path: str, content: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    return path


class TestTelescope(DeferrableTestCase):
    def setUp(self):
        missing_tools = [tool for tool in ("rg", "fzf") if not shutil.which(tool)]
        if missing_tools:
            self.skipTest("missing external tools: {}".format(", ".join(missing_tools)))

        self.window = sublime.active_window()
        self.project_dir = tempfile.mkdtemp(prefix="sublime-telescope-", dir="/tmp")
        self.previous_project_data = self.window.project_data()
        self.views_to_close = []

        self.window.set_project_data(
            {
                "folders": [
                    {
                        "path": self.project_dir,
                    },
                ],
            }
        )

    def tearDown(self):
        self.window.run_command("hide_overlay")
        self.window.run_command("telescope_cancel")
        for view in self.window.views(include_transient=True):
            file_name = view.file_name()
            if file_name and file_name.startswith(self.project_dir) and view.is_valid():
                view.close()

        for view in self.views_to_close:
            if view.is_valid():
                view.close()

        self.window.set_project_data(self.previous_project_data or {"folders": []})
        shutil.rmtree(self.project_dir, ignore_errors=True)

    def test_project_search_uses_globs_and_opens_matching_file(self):
        seed = _write(
            os.path.join(self.project_dir, "seed.py"),
            "open before searching\n",
        )
        target = _write(
            os.path.join(self.project_dir, "first.py"),
            "alpha glob_unique_target one\n",
        )
        ignored = _write(
            os.path.join(self.project_dir, "ignored.txt"),
            "alpha glob_unique_target one\n",
        )
        yield from self._open_file(seed)

        self.window.run_command("telescope")
        yield from self._replace_glob_input(".py")
        yield from self._press_enter()
        yield from self._replace_search_input("glob_unique_target")
        yield from self._wait_for_preview(target)
        yield from self._confirm()
        yield lambda: self.window.active_view().file_name() == target

        self.assertEqual(self.window.active_view().file_name(), target)
        self.assertIsNone(self._view_for_file(ignored))

    def test_current_file_search_opens_only_the_active_file(self):
        current = _write(
            os.path.join(self.project_dir, "current.py"),
            "one\nneedle_current_file in active file\n",
        )
        _write(
            os.path.join(self.project_dir, "other.py"),
            "needle_current_file in other file\n",
        )
        yield from self._open_file(current)

        self.window.run_command("telescope", {"current_file": True})
        yield from self._replace_search_input("needle_current_file")
        yield from self._wait_for_preview(current)
        yield from self._confirm()
        yield lambda: self.window.active_view().file_name() == current

        self.assertEqual(self.window.active_view().file_name(), current)
        self.assertEqual(
            [region.to_tuple() for region in self.window.active_view().sel()],
            [(4, 4)],
        )

    def test_current_file_search_is_restored_and_selected_on_next_run(self):
        current = _write(
            os.path.join(self.project_dir, "restore.py"),
            "restore_unique_query in active file\n",
        )
        yield from self._open_file(current)

        self.window.run_command("telescope", {"current_file": True})
        yield from self._replace_search_input("restore_unique_query")
        yield from self._wait_for_preview(current)
        yield from self._confirm()
        yield lambda: self.window.active_view().file_name() == current

        self.window.run_command("telescope", {"current_file": True})
        input_view = yield from self._wait_for_search_input_text("restore_unique_query")

        self.assertEqual(self._input_text(input_view), "restore_unique_query")
        self.assertEqual(
            [region.to_tuple() for region in input_view.sel()],
            [(0, len("restore_unique_query"))],
        )

    def test_current_file_search_is_restored_when_reopening_glob_mode(self):
        current = _write(
            os.path.join(self.project_dir, "restore_glob.py"),
            "restore_glob_query in active file\n",
        )
        yield from self._open_file(current)

        self.window.run_command("telescope", {"current_file": True})
        yield from self._replace_search_input("restore_glob_query")
        yield from self._wait_for_preview(current)
        yield from self._confirm()
        yield lambda: self.window.active_view().file_name() == current

        self.window.run_command("telescope", {"globs": "*"})
        input_view = yield from self._wait_for_search_input_text("restore_glob_query")

        # should have restored the search we did in the "current file mode"
        self.assertEqual(self._input_text(input_view), "restore_glob_query")

    def test_up_down_moves_the_highlight_without_wrapping(self):
        current = _write(
            os.path.join(self.project_dir, "navigate.py"),
            "navigate_needle one\nnavigate_needle two\n",
        )
        view = yield from self._open_file(current)

        self.window.run_command("telescope", {"current_file": True})
        yield from self._replace_search_input("navigate_needle")
        yield from self._wait_for_preview(current)
        self.assertEqual(self._highlighted_row(), 0)

        self.window.run_command("telescope_move", {"forward": True})
        yield 50
        self.assertEqual(self._highlighted_row(), 1)
        # the preview follows the highlight
        self.assertEqual(
            view.get_regions("telescope-result-view")[0].begin(),
            view.text_point(1, 0),
        )

        self.window.run_command("telescope_move", {"forward": True})
        yield 50
        self.assertEqual(self._highlighted_row(), 1)

        self.window.run_command("telescope_move", {"forward": False})
        yield 50
        self.assertEqual(self._highlighted_row(), 0)

        self.window.run_command("telescope_move", {"forward": False})
        yield 50
        self.assertEqual(self._highlighted_row(), 0)

    def test_highlight_index_is_restored_on_next_run(self):
        current = _write(
            os.path.join(self.project_dir, "restore_index.py"),
            "restore_index_query one\nrestore_index_query two\n",
        )
        yield from self._open_file(current)

        self.window.run_command("telescope", {"current_file": True})
        yield from self._replace_search_input("restore_index_query")
        yield from self._wait_for_preview(current)
        self.window.run_command("telescope_move", {"forward": True})
        yield 100
        self.assertEqual(self._highlighted_row(), 1)

        self.window.run_command("telescope_cancel")
        yield 100

        self.window.run_command("telescope", {"current_file": True})
        yield from self._wait_for_search_input_text("restore_index_query")
        yield from self._wait_for_preview(current)
        self.assertEqual(self._highlighted_row(), 1)

        yield from self._confirm()
        yield lambda: self.window.active_view().file_name() == current

        self.window.run_command("telescope", {"current_file": True})
        yield from self._wait_for_search_input_text("restore_index_query")
        yield from self._wait_for_preview(current)
        self.assertEqual(self._highlighted_row(), 1)

    def test_selected_text_fills_current_file_search(self):
        current = _write(
            os.path.join(self.project_dir, "selection.py"),
            "one\nselected_query_text in active file\n",
        )
        view = yield from self._open_file(current)

        start = view.substr(sublime.Region(0, view.size())).index("selected_query_text")
        view.sel().clear()
        view.sel().add(sublime.Region(start, start + len("selected_query_text")))
        yield 25

        self.window.run_command("telescope", {"current_file": True})
        input_view = yield from self._wait_for_search_input_text("selected_query_text")

        self.assertEqual(self._input_text(input_view), "selected_query_text")
        self.assertEqual(
            [region.to_tuple() for region in input_view.sel()],
            [(0, len("selected_query_text"))],
        )

    def test_selected_text_fills_glob_search_after_glob_input(self):
        current = _write(
            os.path.join(self.project_dir, "selection_glob.py"),
            "one\nselected_glob_query_text in active file\n",
        )
        view = yield from self._open_file(current)

        start = view.substr(sublime.Region(0, view.size())).index(
            "selected_glob_query_text"
        )
        view.sel().clear()
        view.sel().add(sublime.Region(start, start + len("selected_glob_query_text")))
        yield 25

        self.window.run_command("telescope")
        yield from self._replace_glob_input("*")
        yield from self._press_enter()
        input_view = yield from self._wait_for_search_input_text(
            "selected_glob_query_text"
        )

        self.assertEqual(self._input_text(input_view), "selected_glob_query_text")
        self.assertEqual(
            [region.to_tuple() for region in input_view.sel()],
            [(0, len("selected_glob_query_text"))],
        )

    def test_escape_restores_focus_selection_and_scroll_in_current_file_mode(self):
        current = _write(
            os.path.join(self.project_dir, "escape.py"),
            "\n".join(
                ["line {}".format(i) for i in range(80)]
                + ["needle_escape_scroll"]
                + ["line {}".format(i) for i in range(80, 160)]
            ),
        )
        view = yield from self._open_file(current)

        original_region = sublime.Region(10, 10)
        view.sel().clear()
        view.sel().add(original_region)
        view.set_viewport_position((0, 350), animate=False)
        yield 25

        self.window.run_command("telescope", {"current_file": True})
        yield from self._replace_search_input("needle_escape_scroll")
        yield from self._wait_for_preview(current)
        self.assertTrue(view.get_regions("telescope-result-view"))

        self.window.run_command("telescope_cancel")
        yield 100

        self.assertEqual(self.window.active_view(), view)
        self.assertEqual([region.to_tuple() for region in view.sel()], [(10, 10)])
        self.assertEqual(view.viewport_position(), (0, 350))
        self.assertEqual(view.get_regions("telescope-result-view"), [])

    def _open_file(self, path: str):
        view = self.window.open_file(path)
        self.views_to_close.append(view)
        yield lambda: not view.is_loading()
        self.window.focus_view(view)
        yield 25
        return view

    def _replace_glob_input(self, text: str):
        input_view = yield from self._wait_for_glob_input()
        input_view.sel().clear()
        input_view.sel().add(sublime.Region(0, input_view.size()))
        input_view.run_command("insert", {"characters": text})
        yield 150
        return input_view

    def _replace_search_input(self, text: str):
        input_view = yield from self._wait_for_search_input()
        input_view.run_command("select_all")
        input_view.run_command("insert", {"characters": text})
        yield 750  # wait for the debounced live search
        return input_view

    def _press_enter(self):
        self.window.run_command("select")
        yield 150

    def _confirm(self):
        self.window.run_command("telescope_confirm")
        yield 150

    def _wait_for_glob_input(self):
        yield lambda: self._glob_input_view() is not None
        return self._glob_input_view()

    def _wait_for_search_input(self):
        yield lambda: self._search_input_view() is not None
        return self._search_input_view()

    def _wait_for_search_input_text(self, text: str):
        yield (
            lambda: (
                self._search_input_view() is not None
                and self._input_text(self._search_input_view()) == text
            )
        )
        return self._search_input_view()

    def _wait_for_preview(self, file_name: str):
        yield lambda: self._view_for_file(file_name) is not None
        view = self._view_for_file(file_name)
        yield lambda: view.get_regions("telescope-result-view")
        return view

    def _glob_input_view(self):
        for buffer in sublime._buffers():
            view = buffer.primary_view()
            if (
                view
                and view.element() == "command_palette:input"
                and view.window() == self.window
            ):
                return view
        return None

    def _search_input_view(self):
        for buffer in sublime._buffers():
            view = buffer.primary_view()
            if (
                view
                and view.is_valid()
                and view.window() == self.window
                and view.settings().get("telescope_input")
            ):
                return view
        return None

    def _highlighted_row(self):
        panel = self.window.find_output_panel("telescope")
        region = panel.get_regions("telescope-selected")[0]
        return panel.rowcol(region.begin())[0]

    def _view_for_file(self, file_name: str):
        for view in self.window.views(include_transient=True):
            if view.file_name() == file_name:
                return view
        return None

    def _input_text(self, view) -> str:
        return view.substr(sublime.Region(0, view.size()))
