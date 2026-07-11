# From: https://github.com/sublimelsp/LSP/blob/main/plugin/core/input_handlers.py
from __future__ import annotations
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
