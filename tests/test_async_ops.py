import threading
import time

from email_export_import.gui.async_ops import run_async


def test_result_reaches_on_done():
    done = []
    t = run_async(lambda: 41 + 1, done.append, lambda e: done.append(("err", e)))
    t.join(timeout=5)
    assert done == [42]


def test_exception_reaches_on_error_only():
    outcomes = []

    def boom():
        raise ValueError("nope")

    t = run_async(boom, lambda r: outcomes.append(("done", r)),
                  lambda e: outcomes.append(("err", type(e).__name__, str(e))))
    t.join(timeout=5)
    assert outcomes == [("err", "ValueError", "nope")]


def test_caller_thread_is_not_blocked():
    release = threading.Event()
    finished = []

    def slow():
        release.wait(timeout=5)
        return "ok"

    start = time.monotonic()
    t = run_async(slow, finished.append, lambda e: finished.append(e))
    took = time.monotonic() - start
    assert took < 0.5  # returned immediately, fn still parked
    assert finished == []
    release.set()
    t.join(timeout=5)
    assert finished == ["ok"]
