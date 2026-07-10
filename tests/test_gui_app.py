import pytest

flet = pytest.importorskip("flet")


def test_gui_modules_import():
    from email_export_import.gui import app, views  # noqa: F401

    assert callable(app.main)


def test_wizard_state_defaults():
    from email_export_import.gui.app import WizardState

    ws = WizardState()
    assert ws.workers == 4
    assert ws.skip == set()
