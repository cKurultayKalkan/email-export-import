from email_export_import.gui import tray


def test_status_item_is_inert_without_a_running_nsapp():
    # Source runs and tests have no NSApplication main loop (that only exists
    # inside the packaged app, where the Flutter engine drives it) — the
    # status item must decline to appear rather than render dead chrome.
    assert tray.start_status_item("title", [("label", None)]) is None


def test_envelope_image_renders():
    import pytest

    pytest.importorskip("PIL")
    img = tray._envelope_image(44)
    assert img.size == (44, 44)
    assert img.getbbox() is not None  # actually drew something
