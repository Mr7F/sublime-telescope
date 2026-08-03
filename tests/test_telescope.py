import os
import shutil
import subprocess
import sys
import tempfile
import threading

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
        # The unittest output panel, replaced by the telescope panel
        self.previous_panel = self.window.active_panel()
        self.previous_layout = self.window.layout()
        self._reset_telescope_state()

        # Count the searches: one rg process per search, other packages
        # also spawn unrelated processes
        self.search_count = 0
        self._popen = subprocess.Popen

        def popen_spy(cmd, *args, **kwargs):
            if cmd[0] == "rg":
                self.search_count += 1
            return self._popen(cmd, *args, **kwargs)

        subprocess.Popen = popen_spy
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
        subprocess.Popen = self._popen
        self.window.run_command("hide_overlay")
        self.window.run_command("telescope_cancel")
        self._reset_telescope_state()
        for view in self.window.views(include_transient=True):
            file_name = view.file_name()
            if file_name and file_name.startswith(self.project_dir) and view.is_valid():
                view.close()

        for view in self.views_to_close:
            if view.is_valid():
                view.close()

        self.window.set_project_data(self.previous_project_data or {"folders": []})
        shutil.rmtree(self.project_dir, ignore_errors=True)
        self.window.set_layout(self.previous_layout)
        if self.previous_panel:
            self.window.run_command("show_panel", {"panel": self.previous_panel})

    def _reset_telescope_state(self):
        telescope = self._telescope_module()
        # Erase the phantoms before dropping the state referencing them
        telescope._clear_result_phantoms(telescope.search_state[self.window])
        telescope.search_state[self.window] = telescope.SearchState()

    def _telescope_module(self):
        telescope_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), os.pardir, "telescope.py")
        )
        for module in sys.modules.values():
            module_path = getattr(module, "__file__", None)
            if module_path and os.path.abspath(module_path) == telescope_path:
                return module
        raise RuntimeError("sublime-telescope plugin module is not loaded")

    def test_project_search_uses_directory_glob_and_opens_matching_file(self):
        seed = _write(
            os.path.join(self.project_dir, "seed.py"),
            "open before searching\n",
        )
        target = _write(
            os.path.join(self.project_dir, "models", "first.py"),
            "alpha glob_unique_target one\n",
        )
        ignored = _write(
            os.path.join(self.project_dir, "views", "ignored.py"),
            "alpha glob_unique_target one\n",
        )
        yield from self._open_file(seed)

        self.window.run_command("telescope")
        yield from self._replace_search_input("models  glob_unique_target")
        yield from self._wait_for_preview(target)
        self.assertEqual(self._panel_text(), "first.py:1")
        yield from self._confirm()
        yield lambda: self.window.active_view().file_name() == target

        self.assertEqual(self.window.active_view().file_name(), target)
        self.assertIsNone(self._view_for_file(ignored))

    def test_project_search_path_glob_matches_file_names(self):
        seed = _write(
            os.path.join(self.project_dir, "seed.txt"),
            "open before searching\n",
        )
        target = _write(
            os.path.join(self.project_dir, "target.py"),
            "file_glob_unique_target\n",
        )
        _write(
            os.path.join(self.project_dir, "ignored.xml"),
            "file_glob_unique_target\n",
        )
        yield from self._open_file(seed)

        self.window.run_command("telescope")
        yield from self._replace_search_input("*.py  file_glob_unique_target")
        yield from self._wait_for_preview(target)

        self.assertEqual(self._panel_text(), "target.py:1")

    def test_project_search_fuzzy_scores_content_not_path(self):
        """Check that the path is not used for fuzzy scoring."""
        seed = _write(
            os.path.join(self.project_dir, "seed.py"),
            "open before searching\n",
        )
        # fuzzy ranking: file 1 path > file 2 content > file 1 content
        better_path = _write(
            # path has better ranking, but content ranking is worse
            os.path.join(self.project_dir, "abc_path_rank.py"),
            "a xxx b xxx c\n",
        )
        better_content = _write(
            # path does not match fuzzy ranking, but content has better ranking
            os.path.join(self.project_dir, "zzz_path_rank.py"),
            "a---bc\n",
        )
        yield from self._open_file(seed)

        self.window.run_command("telescope")
        yield from self._replace_search_input("abc")
        yield from self._wait_for_preview(better_content)

        self.assertEqual(
            self._panel_text(),
            "zzz_path_rank.py:1\nabc_path_rank.py:1",
        )
        self.assertEqual(self._highlighted_row(), 0)
        self.assertIsNotNone(self._view_for_file(better_content))
        self.assertIsNone(self._view_for_file(better_path))

    def test_completed_search_clears_task(self):
        seed = _write(
            os.path.join(self.project_dir, "completed.py"),
            "completed_search_task_needle\n",
        )
        yield from self._open_file(seed)

        self.window.run_command("telescope")
        yield from self._replace_search_input("completed_search_task_needle")

        state = self._telescope_module().search_state[self.window]
        yield lambda: state.search_task is None
        self.assertEqual(len(state.results), 1)

    def test_rapid_search_replacement_discards_the_old_task(self):
        seed = _write(
            os.path.join(self.project_dir, "replacement.py"),
            "obsolete_search_needle\nreplacement_search_needle\n",
        )
        yield from self._open_file(seed)

        self.window.run_command("telescope")
        yield from self._wait_for_search_input()
        telescope = self._telescope_module()
        state = telescope.search_state[self.window]

        telescope._search(self.window, "obsolete_search_needle")
        obsolete_task = state.search_task
        self.assertIsNotNone(obsolete_task)
        telescope._search(self.window, "replacement_search_needle")

        yield lambda: state.search_task is None
        self.assertEqual(obsolete_task.processes, ())
        self.assertTrue(state.results)
        self.assertTrue(
            all(
                "replacement_search_needle" in result.line_content
                for result in state.results
            )
        )

    def test_cancel_kills_active_search_process(self):
        seed = _write(
            os.path.join(self.project_dir, "cancel_process.py"),
            "cancel process seed\n",
        )
        yield from self._open_file(seed)

        self.window.run_command("telescope")
        yield from self._wait_for_search_input()
        telescope = self._telescope_module()
        process = telescope._create_process(
            ["fzf", "--filter", "blocked_search"],
            stdin=subprocess.PIPE,
            decode_stdout=True,
        )
        task = telescope.SearchTask((process,))
        state = telescope.search_state[self.window]
        state.search_task = task

        try:
            self.assertIsNone(process.poll())
            self.window.run_command("telescope_cancel")
            yield lambda: process.poll() is not None

            self.assertIsNone(state.search_task)
            self.assertEqual(task.processes, ())
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdin.close()
            process.stdout.close()

    def test_project_search_excludes_newline_paths_even_when_directory_glob_matches(self):
        r"""Check that file with \n in their name are ignored.

        File with \n in their path can break fzf parsing.
        """
        if sys.platform.startswith("win"):
            self.skipTest("Windows filenames cannot contain newlines")

        seed = _write(
            os.path.join(self.project_dir, "seed.py"),
            "open before searching\n",
        )
        target = _write(
            os.path.join(self.project_dir, "kept", "normal_path.py"),
            "newline_path_needle\n",
        )
        _write(
            os.path.join(self.project_dir, "kept", "path_with\nnewline.py"),
            "newline_path_needle\n",
        )
        yield from self._open_file(seed)

        telescope = self._telescope_module()
        results = telescope._live_search(self.window, "newline_path_needle", "kept")

        self.assertEqual([result.path for result in results], [target])

    def test_current_file_search_excludes_newline_path(self):
        if sys.platform.startswith("win"):
            self.skipTest("Windows filenames cannot contain newlines")

        current = _write(
            os.path.join(self.project_dir, "current\nfile.py"),
            "newline_current_needle\n",
        )
        yield from self._open_file(current)

        telescope = self._telescope_module()
        state = telescope.search_state[self.window]
        state.current_file = True
        results = telescope._live_search(self.window, "newline_current_needle")

        self.assertEqual(results, [])

    def test_project_folder_exclude_patterns_hide_results(self):
        seed = _write(
            os.path.join(self.project_dir, "seed.py"),
            "open before searching\n",
        )
        target = _write(
            os.path.join(self.project_dir, "kept", "match.py"),
            "alpha folder_filter_needle one\n",
        )
        _write(
            os.path.join(self.project_dir, "hidden", "match.py"),
            "alpha folder_filter_needle one\n",
        )
        self.window.set_project_data(
            {
                "folders": [
                    {
                        "path": self.project_dir,
                        "folder_exclude_patterns": ["hidden"],
                    },
                ],
            }
        )
        yield from self._open_file(seed)

        self.window.run_command("telescope")
        yield from self._replace_search_input("folder_filter_needle")
        yield from self._wait_for_preview(target)

        self.assertEqual(self._panel_text(), "match.py:1")

    def test_folder_exclude_pattern_can_include_sidebar_root_name(self):
        root = os.path.join(self.project_dir, "workspace")
        target = _write(
            os.path.join(root, "kept", "match.py"),
            "root_name_exclude_needle\n",
        )
        _write(
            os.path.join(root, "writable", "match.py"),
            "root_name_exclude_needle\n",
        )
        self.window.set_project_data(
            {
                "folders": [
                    {
                        "path": root,
                        "folder_exclude_patterns": ["workspace/writable"],
                    },
                ],
            }
        )
        yield from self._open_file(target)

        self.window.run_command("telescope")
        yield from self._replace_search_input("root_name_exclude_needle")
        yield from self._wait_for_preview(target)

        self.assertEqual(self._panel_text(), "match.py:1")

    def test_project_folder_include_patterns_limit_results(self):
        seed = _write(
            os.path.join(self.project_dir, "social_seed", "seed.py"),
            "open before searching\n",
        )
        target = _write(
            os.path.join(self.project_dir, "social_app", "deep", "match.py"),
            "alpha include_filter_needle one\n",
        )
        _write(
            os.path.join(self.project_dir, "documents", "match.py"),
            "alpha include_filter_needle one\n",
        )
        include = os.path.join(self.project_dir, "social*")
        self.window.set_project_data(
            {
                "folders": [
                    {
                        "path": self.project_dir,
                        "file_include_patterns": [include],
                        "folder_include_patterns": [include],
                    },
                ],
            }
        )
        yield from self._open_file(seed)

        self.window.run_command("telescope")
        yield from self._replace_search_input("include_filter_needle")
        yield from self._wait_for_preview(target)

        self.assertEqual(self._panel_text(), "match.py:1")

    def test_result_phantom_is_anchored_after_the_full_label(self):
        """The color phantom must appear after the whole "file:line" label,
        not truncate it (e.g. land between the filename and the line
        number). Computing where to anchor it needs the output view's
        current line layout, which is why `_insert_result_phantom` runs on
        the main thread after the result panel has been updated.
        """
        seed = _write(
            os.path.join(self.project_dir, "seed.py"),
            "open before searching\n",
        )
        for i in range(8):
            _write(
                os.path.join(self.project_dir, f"phantom_anchor_{i}.py"),
                f"def foo_{i}():\n    return phantom_anchor_needle\n",
            )
        yield from self._open_file(seed)

        telescope = self._telescope_module()
        original_insert = telescope._insert_result_phantom
        checked = []

        def spy(window, row, tokens):
            original_phantom = sublime.Phantom
            captured = {}

            class CapturingPhantom(original_phantom):
                def __init__(self, region, *args, **kwargs):
                    captured["begin"] = region.begin()
                    super().__init__(region, *args, **kwargs)

            sublime.Phantom = CapturingPhantom
            try:
                original_insert(window, row, tokens)
            finally:
                sublime.Phantom = original_phantom

            if "begin" in captured:
                state = telescope.search_state[window]
                expected_label = telescope._result_location_label(
                    state.results[row], state.result_label_widths
                )
                row_start = state.output_view.text_point(row, 0)
                checked.append((captured["begin"], row_start + len(expected_label)))

        telescope._insert_result_phantom = spy
        try:
            self.window.run_command("telescope")
            yield from self._replace_search_input("phantom_anchor_needle")
            yield lambda: len(checked) >= 5
            yield 300  # let the periodic refresh color a few more rows
        finally:
            telescope._insert_result_phantom = original_insert

        self.assertTrue(checked)
        for anchor, expected_anchor in checked:
            self.assertEqual(anchor, expected_anchor)

    def test_result_styles_render_off_main_thread(self):
        seed = _write(
            os.path.join(self.project_dir, "seed.py"),
            "open before searching\n",
        )
        _write(
            os.path.join(self.project_dir, "slow_render.py"),
            "".join(f"slow_render_needle {line}\n" for line in range(100)),
        )
        yield from self._open_file(seed)

        telescope = self._telescope_module()
        original_result_tokens = telescope._result_tokens
        main_thread = threading.current_thread()
        render_threads = []
        render_started = threading.Event()
        release_render = threading.Event()

        def slow_result_tokens(window, state, result):
            render_threads.append(threading.current_thread())
            render_started.set()
            release_render.wait(10)
            return [(result.line_content, {})]

        telescope._result_tokens = slow_result_tokens
        try:
            self.window.run_command("telescope")
            yield from self._replace_search_input("slow_render_needle")
            yield render_started.is_set

            main_thread_ran = threading.Event()
            sublime.set_timeout(main_thread_ran.set)
            yield main_thread_ran.is_set

            state = telescope.search_state[self.window]
            generation = state.phantom_render_generation
            self.assertIsNotNone(state.phantom_render_thread)
            state.output_view.set_viewport_position(
                (0, state.output_view.layout_extent()[1]), animate=False
            )
            yield lambda: state.phantom_render_generation > generation
        finally:
            release_render.set()
            telescope._result_tokens = original_result_tokens
            state = telescope.search_state[self.window]
            yield lambda: state.phantom_render_thread is None

        self.assertTrue(render_threads)
        self.assertTrue(all(thread is not main_thread for thread in render_threads))

    def test_files_over_render_line_cap_use_raw_result_text(self):
        telescope = self._telescope_module()
        at_limit = _write(
            os.path.join(self.project_dir, "at_render_limit.py"),
            "render line\n" * telescope.MAX_RENDER_LINES,
        )
        over_limit = _write(
            os.path.join(self.project_dir, "over_render_limit.py"),
            "render line\n" * (telescope.MAX_RENDER_LINES + 1),
        )
        state = telescope.search_state[self.window]

        self.assertIsNotNone(
            telescope._parser_view_for_result(
                self.window,
                state,
                telescope.SearchResult(at_limit, 1, 0, "render line"),
            )
        )
        result = telescope.SearchResult(over_limit, 1, 0, "render line")
        self.assertEqual(telescope._result_tokens(self.window, state, result), [])
        self.assertIn(over_limit, state.unstyled_result_paths)

        os.unlink(over_limit)
        self.assertEqual(telescope._result_tokens(self.window, state, result), [])

    def test_current_file_search_opens_only_the_active_file(self):
        current = _write(
            os.path.join(self.project_dir, "current.py"),
            "one\nxxx needle_current_file in active file\n",
        )
        _write(
            os.path.join(self.project_dir, "other.py"),
            "needle_current_file in other file\n",
        )
        yield from self._open_file(current)

        self.window.run_command("telescope", {"current_file": True})
        yield from self._replace_search_input("needle_current_file")
        yield from self._wait_for_preview(current)
        panel = self.window.find_output_panel("telescope")

        self.assertEqual(panel.syntax().scope, "text.telescope")
        self.assertEqual(
            panel.substr(sublime.Region(0, panel.size())),
            "current.py:2",
        )
        self.assertEqual(panel.folded_regions(), [])
        yield from self._confirm()
        yield lambda: self.window.active_view().file_name() == current

        self.assertEqual(self.window.active_view().file_name(), current)
        # The cursor is at the start of the match
        match_point = self.window.active_view().text_point(1, len("xxx "))
        self.assertEqual(
            [region.to_tuple() for region in self.window.active_view().sel()],
            [(match_point, match_point)],
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

        # Reopening the same search does not search again
        self.assertEqual(self.search_count, 1)
        self.assertEqual(self._input_text(input_view), "restore_unique_query")
        self.assertEqual(
            [region.to_tuple() for region in input_view.sel()],
            [(0, len("restore_unique_query"))],
        )

    def test_directory_glob_is_hidden_in_current_file_mode_and_restored_in_project_mode(self):
        current = _write(
            os.path.join(self.project_dir, "models", "restore_glob.py"),
            "restore_glob_query in active file\n",
        )
        yield from self._open_file(current)

        self.window.run_command("telescope")
        yield from self._replace_search_input("models  restore_glob_query")
        yield from self._wait_for_preview(current)
        yield from self._confirm()
        yield lambda: self.window.active_view().file_name() == current

        self.window.run_command("telescope", {"current_file": True})
        input_view = yield from self._wait_for_search_input_text("restore_glob_query")
        self.assertEqual(self._input_text(input_view), "restore_glob_query")
        self.assertEqual(
            [region.to_tuple() for region in input_view.sel()],
            [(0, len("restore_glob_query"))],
        )
        self.window.run_command("telescope_cancel")
        yield 100

        self.window.run_command("telescope")
        input_view = yield from self._wait_for_search_input_text(
            "models  restore_glob_query"
        )
        self.assertEqual(self._input_text(input_view), "models  restore_glob_query")
        self.assertEqual(
            [region.to_tuple() for region in input_view.sel()],
            [(len("models  "), len("models  restore_glob_query"))],
        )

    def test_up_down_moves_the_highlight_without_wrapping(self):
        current = _write(
            os.path.join(self.project_dir, "navigate.py"),
            "navigate_needle one\nnavigate_needle two\n",
        )
        view = yield from self._open_file(current)

        self.window.run_command("telescope", {"current_file": True})
        yield from self._replace_search_input("navigate_needle")
        yield from self._wait_for_preview(current)
        self.assertEqual(self._panel_text(), "navigate.py:1\nnavigate.py:2")
        self.assertEqual(self._highlighted_row(), 0)

        self.window.run_command("telescope_move", {"forward": True})
        yield 50
        self.assertEqual(self._highlighted_row(), 1)
        # the preview follows the highlight, selecting the match
        self.assertEqual(view.sel()[0].begin(), view.text_point(1, 0))

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
        # The results are kept, with the highlight, without a new search
        self.assertEqual(self._panel_text(), "restore_index.py:1\nrestore_index.py:2")
        self.assertEqual(self._highlighted_row(), 1)
        self.assertEqual(self.search_count, 1)

        yield from self._confirm()
        yield lambda: self.window.active_view().file_name() == current

        self.window.run_command("telescope", {"current_file": True})
        yield from self._wait_for_search_input_text("restore_index_query")
        yield from self._wait_for_preview(current)
        self.assertEqual(self._panel_text(), "restore_index.py:1\nrestore_index.py:2")
        self.assertEqual(self._highlighted_row(), 1)
        self.assertEqual(self.search_count, 1)

    def test_split_view_preview_highlight_follows_the_result(self):
        """Test when we are in a split view, and result match on both view."""
        file_a = _write(
            os.path.join(self.project_dir, "split_a.py"),
            "alpha split_view_needle one\n",
        )
        file_b = _write(
            os.path.join(self.project_dir, "split_b.py"),
            "alpha split_view_needle one\n",
        )
        self.window.set_layout(
            {
                "cols": [0.0, 0.5, 1.0],
                "rows": [0.0, 1.0],
                "cells": [[0, 0, 1, 1], [1, 0, 2, 1]],
            }
        )
        self.window.focus_group(0)
        yield from self._open_file(file_a)
        self.window.focus_group(1)
        yield from self._open_file(file_b)

        self.window.run_command("telescope")
        yield from self._replace_search_input("split_view_needle")
        view_a = self._view_for_file(file_a)
        view_b = self._view_for_file(file_b)
        # One result per file, the first one previewed in its group
        yield (
            lambda: (
                view_a.get_regions("telescope-result-view")
                or view_b.get_regions("telescope-result-view")
            )
        )
        if view_a.get_regions("telescope-result-view"):
            first, second = view_a, view_b
        else:
            first, second = view_b, view_a

        # Moving to the result of the other file moves the highlight
        # there, without leaving one in the visible previous preview
        self.window.run_command("telescope_move", {"forward": True})
        yield lambda: second.get_regions("telescope-result-view")
        self.assertEqual(first.get_regions("telescope-result-view"), [])

    def test_switching_mode_resets_the_highlight(self):
        current = _write(
            os.path.join(self.project_dir, "mode_switch.py"),
            "mode_switch_needle one\nmode_switch_needle two\n",
        )
        yield from self._open_file(current)

        self.window.run_command("telescope", {"current_file": True})
        yield from self._replace_search_input("mode_switch_needle")
        yield from self._wait_for_preview(current)
        self.window.run_command("telescope_move", {"forward": True})
        yield 100
        self.assertEqual(self._highlighted_row(), 1)

        self.window.run_command("telescope_cancel")
        yield 100

        # The same search in the other mode is a new search, it restarts
        # from the first result, without showing the previous results
        self.window.run_command("telescope")
        self.assertEqual(self._panel_text(), "")
        yield from self._wait_for_search_input_text("mode_switch_needle")
        yield from self._wait_for_preview(current)
        self.assertEqual(self._highlighted_row(), 0)
        self.assertEqual(self.search_count, 2)

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

    def test_selected_text_fills_project_search(self):
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
        # The match is selected in the preview
        self.assertEqual(view.sel()[0].begin(), view.text_point(80, 0))

        self.window.run_command("telescope_cancel")
        yield 100

        self.assertEqual(self.window.active_view(), view)
        self.assertEqual([region.to_tuple() for region in view.sel()], [(10, 10)])
        self.assertEqual(view.viewport_position(), (0, 350))

    def test_parse_rg_result(self):
        telescope = self._telescope_module()
        SEPARATOR = "\0"
        content = f"hello{SEPARATOR}world"  # the content of the line has the separator
        # Windows
        self.assertEqual(
            telescope._parse_rg_result(f"C:\\proj\\file.py{SEPARATOR}12{SEPARATOR}{content}"),
            (r"C:\proj\file.py", 12, content),
        )

        # Linux / MacOS
        self.assertEqual(
            telescope._parse_rg_result(f"/proj/file.py{SEPARATOR}12{SEPARATOR}{content}"),
            ("/proj/file.py", 12, content),
        )
        self.assertIsNone(
            telescope._parse_rg_result(f"/proj/file.py{SEPARATOR}not-a-line{SEPARATOR}{content}")
        )
        self.assertEqual(
            telescope._parse_rg_result(f"/proj/path-with-semicolon-:-/file.py{SEPARATOR}12{SEPARATOR}{content}"),
            ("/proj/path-with-semicolon-:-/file.py", 12, content),
        )

    def _open_file(self, path: str):
        view = self.window.open_file(path)
        self.views_to_close.append(view)
        yield lambda: not view.is_loading()
        self.window.focus_view(view)
        yield 25
        return view

    def _replace_search_input(self, text: str):
        input_view = yield from self._wait_for_search_input()
        input_view.run_command("select_all")
        input_view.run_command("insert", {"characters": text})
        yield 750  # wait for the debounced live search
        return input_view

    def _confirm(self):
        self.window.run_command("telescope_confirm")
        yield 150

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
        # The preview highlights the fuzzy matched text
        yield lambda: view.get_regions("telescope-result-view")
        return view

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

    def _panel_text(self):
        panel = self.window.find_output_panel("telescope")
        return panel.substr(sublime.Region(0, panel.size()))

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
