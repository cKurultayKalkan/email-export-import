"""The daemon's live tray (daemon/trayapp.py). Source runs / CI have no tray
backend, so run() must decline gracefully; the envelope glyph must still draw."""
from email_export_import.daemon import trayapp


def test_envelope_image_renders():
    import pytest

    pytest.importorskip("PIL")
    img = trayapp._envelope_image(44)
    assert img.size == (44, 44)
    assert img.getbbox() is not None  # actually drew something


def test_available_returns_bool_without_raising():
    # Reports feasibility; never raises regardless of platform/backends.
    assert isinstance(trayapp.available(), bool)


def test_fmt_duration_is_human_readable():
    from email_export_import.daemon.__main__ import _fmt_duration

    assert _fmt_duration(42 * 60) == "42m"
    assert _fmt_duration(3 * 3600 + 5 * 60) == "3h 5m"
    assert _fmt_duration(2 * 86400 + 4 * 3600) == "2d 4h"
    assert _fmt_duration(877 * 60) == "14h 37m"  # the reported raw "877m"


def test_build_run_lines_shows_dest_only_and_readable_elapsed():
    from email_export_import.daemon.__main__ import _build_run_lines
    from email_export_import.gui.run_manager import RunSnapshot

    snaps = [
        RunSnapshot(key="a", title="a@x → b@y", status="running",
                    processed=10000, total=29000, current_folder="INBOX"),
        RunSnapshot(key="b", title="c → d", status="done",
                    processed=5, total=5, current_folder=None),
        RunSnapshot(key="c", title="e → f", status="queued",
                    processed=0, total=0, current_folder=None),
    ]
    started = {"a": 1000.0, "c": None}
    fmt = "{dest} · {done}/{total} · {dur}"
    lines = _build_run_lines(snaps, started, now=1000.0 + 34 * 60, line_fmt=fmt)

    assert len(lines) == 2  # running + queued; the done run is excluded
    assert lines[0] == "b@y · 10,000/29,000 · 34m"  # DEST only, thousands, elapsed
    assert lines[1] == "f · 0/? · —"  # queued, total 0 -> "?", no start -> em dash


def test_run_declines_without_a_backend(monkeypatch):
    # No usable tray backend -> run() returns False (the daemon then falls back
    # to a plain serve loop) and touches none of the callbacks.
    monkeypatch.setattr(trayapp, "available", lambda: False)
    called: list = []
    ok = trayapp.run("title", lambda: "status",
                     lambda: called.append("open"), lambda: called.append("quit"))
    assert ok is False and called == []
