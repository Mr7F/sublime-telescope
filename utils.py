# From: https://github.com/sublimelsp/LSP/blob/main/plugin/core/input_handlers.py
from __future__ import annotations
import os
from typing import Any, Callable
import functools
import sublime
import time


def debounced(user_function: Callable[..., Any]) -> Callable[..., None]:
    """A decorator which debounces the calls to a function.

    Note that the return value of the function will be discarded, so it only makes sense to use this decorator for
    functions that return None. The function will run on Sublime's main thread.
    """

    def debounce_time() -> float:
        return sublime.load_settings("Telescope.sublime-settings").get("debounce", 0.5)

    @functools.wraps(user_function)
    def wrapped_function(*args: Any, **kwargs: Any) -> None:
        def check_call_function() -> None:
            target_time = getattr(wrapped_function, "_target_time", None)
            if isinstance(target_time, float):
                additional_delay = target_time - time.monotonic()
                if additional_delay > 0:
                    setattr(wrapped_function, "_target_time", None)
                    sublime.set_timeout(
                        check_call_function, int(additional_delay * 1000)
                    )
                    return
            delattr(wrapped_function, "_target_time")
            user_function(*args, **kwargs)

        if hasattr(wrapped_function, "_target_time"):
            setattr(
                wrapped_function, "_target_time", time.monotonic() + debounce_time()
            )
            return
        setattr(wrapped_function, "_target_time", None)
        sublime.set_timeout(check_call_function, int(debounce_time() * 1000))

    return wrapped_function


def _convert_sublime_glob_to_rg_glob(
    pattern: str,
    directory: bool = False,
    roots: list[str] | None = None,
) -> list[str]:
    """Convert a Sublime file pattern to rg globs."""
    roots = [_rg_path(root) for root in roots or []]
    pattern = pattern.strip().replace("\\", "/")
    if not pattern:
        return []

    project_relative = pattern.startswith("//")
    root_relative = pattern.startswith("./")
    absolute = pattern.startswith("/") and not project_relative

    if project_relative:
        pattern = pattern[2:]
    elif root_relative:
        pattern = pattern[2:]

    directory_pattern = directory or pattern.endswith("/")
    pattern = pattern.rstrip("/") if directory_pattern else pattern
    path_pattern = absolute or project_relative or root_relative or "/" in pattern
    body_globs = _sublime_pattern_body_rg_globs(pattern, path_pattern)

    if absolute:
        prefixes = [""]
    elif roots:
        if project_relative or root_relative:
            prefixes = [f"{root}/" for root in roots]
        else:
            prefixes = [f"{root}/**/" for root in roots]
    elif path_pattern:
        prefixes = ["**/"]
    else:
        prefixes = [""]

    globs = []
    for prefix in prefixes:
        for body in body_globs:
            glob = prefix + body
            if directory_pattern:
                glob = f"{glob}/**"
            globs.append(glob)

    # A pattern may include the sidebar root's name itself.
    if roots and path_pattern and not (absolute or project_relative or root_relative):
        for root in roots:
            root_name = root.rsplit("/", 1)[-1] + "/"
            if not pattern.startswith(root_name):
                continue
            remainder = pattern[len(root_name) :]
            for body in _sublime_pattern_body_rg_globs(remainder, "/" in remainder):
                glob = f"{root}/{body}"
                if directory_pattern:
                    glob = f"{glob}/**"
                globs.append(glob)
    return _dedupe(globs)


def _sublime_pattern_body_rg_globs(pattern: str, path_pattern: bool) -> list[str]:
    # Sublime treats a bare `*.*` as matching all files, including files with no dot.
    if pattern == "*.*":
        pattern = "*"
    pattern = _escape_rg_glob(pattern)
    if not path_pattern:
        return [pattern]
    return _expand_path_wildcards(pattern)


def _escape_rg_glob(pattern: str) -> str:
    escaped = []
    for char in pattern:
        if char in "[]{}!\\":
            escaped.append("\\")
        escaped.append(char)
    return "".join(escaped)


def _expand_path_wildcards(pattern: str) -> list[str]:
    parts = pattern.split("*")
    if len(parts) == 1:
        return [pattern]

    globs = [parts[0]]
    for index, part in enumerate(parts[1:], start=1):
        trailing_star = index == len(parts) - 1 and not part
        prefix = parts[index - 1].rsplit("/", 1)[-1]
        if trailing_star:
            wildcards = ("*", "*/**")
        elif prefix:
            wildcards = ("*", "**/*", "*/**/*")
        else:
            wildcards = ("*", "**/*")
        globs = [glob + wildcard + part for glob in globs for wildcard in wildcards]
    return _dedupe(globs)


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _rg_path(path: str) -> str:
    return os.path.abspath(path).replace(os.sep, "/").rstrip("/")
