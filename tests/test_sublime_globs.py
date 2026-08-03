from __future__ import annotations

import os
import shutil
import sys
import tempfile

import sublime
from unittesting import DeferrableTestCase


NEEDLE = "sublime_glob_vector_needle"


def _write(path: str, content: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    return path


class SublimeGlobVectorTests(DeferrableTestCase):
    def setUp(self):
        missing_tools = [tool for tool in ("rg", "fzf") if not shutil.which(tool)]
        if missing_tools:
            self.skipTest("missing external tools: {}".format(", ".join(missing_tools)))

        self.window = sublime.active_window()
        self.previous_project_data = self.window.project_data()
        self.previous_panel = self.window.active_panel()
        self.project_dir = tempfile.mkdtemp(prefix="sublime-telescope-globs-", dir="/tmp")
        self.views_to_close = []
        self._reset_telescope_state()
        self._write_fixture()
        self._set_base_project()

    def tearDown(self):
        self.window.run_command("hide_panel", {"panel": "find_in_files"})
        self.window.run_command("hide_panel", {"panel": "output.find_results"})
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
        if self.previous_panel:
            self.window.run_command("show_panel", {"panel": self.previous_panel})

    def test_rg_globs_match_collected_sublime_find_in_files_vectors(self):
        yield from self._open_file(self.path("seed.py"))
        vectors = [
            ("file", "*.md", {"pkg/alpha.md"}),
            (
                "file",
                "alpha.py",
                {"alpha.py", "pkg/alpha.py", "pkg/deep/alpha.py"},
            ),
            ("file", "pkg/alpha.py", {"pkg/alpha.py"}),
            ("file", "parent/*one.py", {"parent/mydir/a/b/one.py"}),
            (
                "file",
                "mydir/tw*",
                {
                    "parent/mydir/two/first.py",
                    "parent/mydir/two_sub/ignored.py",
                    "parent/mydir/two/deep/nested/last.py",
                },
            ),
            (
                "file",
                "mydir/tw*.py",
                {
                    "parent/mydir/two/first.py",
                    "parent/mydir/two_sub/ignored.py",
                    "parent/mydir/two/deep/nested/last.py",
                },
            ),
            (
                "file",
                "mydir/th*_report",
                {
                    "parent/mydir/three_report",
                    "parent/mydir/three_sub_report",
                    "parent/mydir/three/deep/nested_report",
                },
            ),
            (
                "file",
                "alp?a.py",
                {"alpha.py", "pkg/alpha.py", "pkg/deep/alpha.py"},
            ),
            ("file", "//alpha.py", {"alpha.py"}),
            ("file", "a[1].txt", {"literal/a[1].txt"}),
            ("file", "literal/a{2}.txt", {"literal/a{2}.txt"}),
            (
                "folder",
                "mydir/two/",
                {
                    "parent/mydir/two/first.py",
                    "parent/mydir/two/deep/nested/last.py",
                },
            ),
            (
                "folder",
                "mydir/*one/",
                {"parent/mydir/a/b/one/hit.py", "parent/mydir/abone/hit.py"},
            ),
            ("folder", "//mydir/five/", {"mydir/five/third.py"}),
            ("folder", "./mydir/five/", {"mydir/five/third.py"}),
            ("folder", self.path("mydir/five") + "/", {"mydir/five/third.py"}),
        ]

        for mode, where, expected_relative_paths in vectors:
            expected = self.paths(expected_relative_paths)
            self._set_filtered_project(mode, where)
            telescope_paths = self._telescope_live_search_paths(NEEDLE)
            self.assertEqual(telescope_paths, expected, where)

    def test_path_globs_match_sublime_find_in_files(self):
        yield from self._open_file(self.path("seed.py"))
        patterns = (
            "*.md",
            "alpha.py",
            "pkg/alpha.py",
            "parent/*one.py",
            "mydir/tw*",
            "mydir/tw*.py",
            "mydir/th*_report",
            "alp?a.py",
            "//alpha.py",
            "a[1].txt",
            "literal/a{2}.txt",
            "mydir/two",
            "mydir/two/",
            "mydir/*one/",
            "//mydir/five/",
            "./mydir/five/",
            self.path("mydir/five") + "/",
            "*.py,*.md",
            "*.py,-alpha.py",
            "**/*.py",
            "parent/**/one.py",
            "parent/*/one.py",
            "*.PY",
            "*.*",
            "mydir/*.*",
            "-*.txt",
            "*.py,-pkg/*",
            "mydir/*",
            "mydir/**",
            "mydir/two/*.py",
            "mydir/two/*/last.py",
            "mydir/two/**/last.py",
            "literal/*",
            "*.py, *.md",
            "*.py,-*/one.py",
            "//*.py",
            self.path("alpha.py"),
            "*",
            "-alpha.py,*.py",
            "*.py,-alpha.py,alpha.py",
            "*.py,-*.PY",
            "*.PY,-UPPER.PY",
            "space name.py",
            "space*.py",
            "dir space/*.py",
            "folder.py",
            "*.py,-pkg/",
            "*.py,-pkg/deep/*",
            "*.py,-**/deep/**",
        )

        mismatches = {}
        for where in patterns:
            sublime_paths = yield from self._sublime_find_in_files_paths(where)
            telescope_paths = self._telescope_path_glob_paths(where)
            if telescope_paths != sublime_paths:
                mismatches[where] = {
                    "telescope": sorted(
                        os.path.relpath(path, self.project_dir)
                        for path in telescope_paths
                    ),
                    "sublime": sorted(
                        os.path.relpath(path, self.project_dir)
                        for path in sublime_paths
                    ),
                }
        self.maxDiff = None
        self.assertEqual(mismatches, {})

    def test_converter_output_for_representative_vectors(self):
        utils = self._utils_module()
        root = self.project_dir
        other_root = os.path.join(self.project_dir, "other-root")

        self.assertEqual(
            utils._convert_sublime_glob_to_rg_glob("*.py", roots=[root]),
            [f"{root}/**/*.py"],
        )
        self.assertEqual(
            utils._convert_sublime_glob_to_rg_glob("*.*", roots=[root]),
            [f"{root}/**/*"],
        )
        self.assertEqual(
            utils._convert_sublime_glob_to_rg_glob(
                "models",
                directory=True,
                roots=[root],
            ),
            [f"{root}/**/models/**"],
        )
        self.assertEqual(
            utils._convert_sublime_glob_to_rg_glob("mydir/*one", roots=[root]),
            [f"{root}/**/mydir/*one", f"{root}/**/mydir/**/*one"],
        )
        self.assertEqual(
            utils._convert_sublime_glob_to_rg_glob(
                "//mydir/five",
                roots=[root, other_root],
            ),
            [f"{root}/mydir/five", f"{other_root}/mydir/five"],
        )
        self.assertEqual(
            utils._convert_sublime_glob_to_rg_glob("a[1].txt", roots=[root]),
            [f"{root}/**/a\\[1\\].txt"],
        )
        self.assertEqual(
            utils._convert_sublime_glob_to_rg_glob(
                "parent/*one.py",
                roots=[root],
            ),
            [
                f"{root}/**/parent/*one.py",
                f"{root}/**/parent/**/*one.py",
            ],
        )
        self.assertEqual(
            utils._convert_sublime_glob_to_rg_glob("mydir/tw*", roots=[root]),
            [f"{root}/**/mydir/tw*", f"{root}/**/mydir/tw*/**"],
        )
        self.assertEqual(
            utils._convert_sublime_glob_to_rg_glob("mydir/tw*.py", roots=[root]),
            [
                f"{root}/**/mydir/tw*.py",
                f"{root}/**/mydir/tw**/*.py",
                f"{root}/**/mydir/tw*/**/*.py",
            ],
        )
        self.assertEqual(
            utils._convert_sublime_glob_to_rg_glob("mydir/th*_report", roots=[root]),
            [
                f"{root}/**/mydir/th*_report",
                f"{root}/**/mydir/th**/*_report",
                f"{root}/**/mydir/th*/**/*_report",
            ],
        )
        self.assertEqual(
            utils._convert_sublime_glob_to_rg_glob("alp?a.py", roots=[root]),
            [f"{root}/**/alp?a.py"],
        )
        self.assertEqual(
            utils._convert_sublime_glob_to_rg_glob("literal/a{2}.txt", roots=[root]),
            [f"{root}/**/literal/a\\{{2\\}}.txt"],
        )
        self.assertEqual(
            utils._convert_sublime_glob_to_rg_glob(self.path("mydir/five") + "/"),
            [self.path("mydir/five") + "/**"],
        )
        sidebar_root = self.path("workspace")
        self.assertEqual(
            utils._convert_sublime_glob_to_rg_glob(
                "workspace/writable",
                directory=True,
                roots=[sidebar_root],
            ),
            [
                f"{sidebar_root}/**/workspace/writable/**",
                f"{sidebar_root}/writable/**",
            ],
        )

    def _write_fixture(self):
        for relative_path in (
            "seed.py",
            "alpha.py",
            "UPPER.PY",
            "alpha.txt",
            ".hidden.py",
            "space name.py",
            "dir space/space child.py",
            "folder.py/inside.py",
            "folder.py/inside.txt",
            "pkg/alpha.py",
            "pkg/deep/alpha.py",
            "pkg/alpha.md",
            "parent/mydir/two/first.py",
            "parent/mydir/two_sub/ignored.py",
            "parent/mydir/two/deep/nested/last.py",
            "parent/mydir/three_report",
            "parent/mydir/three_sub_report",
            "parent/mydir/three/deep/nested_report",
            "parent/mydir/a/b/one.py",
            "parent/mydir/a/b/one/hit.py",
            "parent/mydir/abone/hit.py",
            "mydir/five/third.py",
            "nested/mydir/five/ignored.py",
            "literal/a[1].txt",
            "literal/a{2}.txt",
        ):
            _write(self.path(relative_path), NEEDLE + "\n")

    def _set_base_project(self):
        self.window.set_project_data(
            {
                "folders": [
                    {
                        "path": self.project_dir,
                    },
                ],
            }
        )

    def _set_filtered_project(self, mode: str, where: str):
        key = "file_include_patterns" if mode == "file" else "folder_include_patterns"
        self.window.set_project_data(
            {
                "folders": [
                    {
                        "path": self.project_dir,
                        key: [where],
                    },
                ],
            }
        )

    def _telescope_live_search_paths(self, pattern: str):
        telescope = self._telescope_module()
        return {result.path for result in telescope._live_search(self.window, pattern)}

    def _telescope_path_glob_paths(self, where: str):
        telescope = self._telescope_module()
        return {
            result.path
            for result in telescope._live_search(self.window, NEEDLE, where)
        }

    def _sublime_find_in_files_paths(self, where: str):
        native_where = (
            where
            if where.startswith(("./", "/")) and not where.startswith("//")
            else f"<open folders>,{where}"
        )
        self.window.run_command(
            "show_panel",
            {
                "panel": "find_in_files",
                "pattern": NEEDLE,
                "where": native_where,
                "regex": False,
                "case_sensitive": True,
                "whole_word": False,
                "use_buffer": False,
            },
        )
        self.window.run_command("find_all")
        yield 100
        result_view = self.window.find_output_panel("find_results")

        def search_finished():
            text = result_view.substr(sublime.Region(0, result_view.size())).rstrip()
            return bool(text) and " match" in text.splitlines()[-1]

        yield search_finished
        return {path for path, _line, _column in result_view.find_all_results()}

    def _open_file(self, path: str):
        view = self.window.open_file(path)
        self.views_to_close.append(view)
        yield lambda: not view.is_loading()
        self.window.focus_view(view)
        yield 25
        return view

    def _reset_telescope_state(self):
        telescope = self._telescope_module()
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

    def _utils_module(self):
        utils_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), os.pardir, "utils.py")
        )
        for module in sys.modules.values():
            module_path = getattr(module, "__file__", None)
            if module_path and os.path.abspath(module_path) == utils_path:
                return module
        raise RuntimeError("sublime-telescope utils module is not loaded")

    def path(self, relative_path: str) -> str:
        return os.path.join(self.project_dir, *relative_path.split("/"))

    def paths(self, relative_paths: set[str]) -> set[str]:
        return {self.path(relative_path) for relative_path in relative_paths}
