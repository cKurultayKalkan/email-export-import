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


def test_run_declines_without_a_backend(monkeypatch):
    # No usable tray backend -> run() returns False (the daemon then falls back
    # to a plain serve loop) and touches none of the callbacks.
    monkeypatch.setattr(trayapp, "available", lambda: False)
    called: list = []
    ok = trayapp.run("title", lambda: "status",
                     lambda: called.append("open"), lambda: called.append("quit"))
    assert ok is False and called == []
