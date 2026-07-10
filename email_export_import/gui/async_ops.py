"""Run blocking work off the UI thread, delivering the outcome via callbacks.

The Flet event thread must never run IMAP calls; views hand them to
run_async() and update controls from the callback (Flet control updates are
thread-safe; a RuntimeError from an unmounted control means the page closed).
"""
from __future__ import annotations

import threading
from typing import Callable, TypeVar

T = TypeVar("T")


def run_async(
    fn: Callable[[], T],
    on_done: Callable[[T], None],
    on_error: Callable[[Exception], None],
) -> threading.Thread:
    def worker() -> None:
        try:
            result = fn()
        except Exception as exc:
            on_error(exc)
            return
        on_done(result)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread
