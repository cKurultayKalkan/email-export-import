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
    assert ws.spool is False


def test_view_builders_set_route_and_controls():
    from email_export_import.gui import views
    from email_export_import.gui.controller import PlanResult, RunSnapshot
    from email_export_import.gui.i18n import I18n

    i18n = I18n(locale="en")
    noop = lambda *a, **k: None

    welcome = views.build_welcome(i18n, [], noop, noop, noop)
    assert welcome.route == "/" and isinstance(welcome.controls, list)

    plan = views.build_plan(
        i18n, PlanResult(plans=[], counts={}, total=0), set(), 4, False,
        noop, noop, noop, noop, noop,
    )
    assert plan.route == "/plan" and isinstance(plan.controls, list)

    progress, _bar, _counter, _folder = views.build_progress(i18n, noop)
    assert progress.route == "/progress" and isinstance(progress.controls, list)

    done = views.build_done(
        i18n,
        RunSnapshot(processed=0, total=0, current_folder=None, running=False),
        noop,
    )
    assert done.route == "/done" and isinstance(done.controls, list)
