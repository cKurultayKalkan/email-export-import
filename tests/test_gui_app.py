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
    from email_export_import.gui.controller import PlanResult
    from email_export_import.gui.i18n import I18n
    from email_export_import.gui.run_manager import RunSnapshot

    i18n = I18n(locale="en")
    noop = lambda *a, **k: None

    snap = RunSnapshot(
        key="a__b", title="a → b", status="running", processed=3, total=10,
        current_folder="INBOX",
    )
    dash = views.build_dashboard(
        i18n, [snap], noop, noop, noop, noop, noop, noop, noop
    )
    assert dash.route == "/" and isinstance(dash.controls, list)

    plan = views.build_plan(
        i18n, PlanResult(plans=[], counts={}, total=0), set(), 4, False,
        noop, noop, noop, noop, noop,
    )
    assert plan.route == "/plan" and isinstance(plan.controls, list)

    detail = views.build_detail(i18n, snap, noop, noop, noop, noop)
    assert detail.route == "/detail" and isinstance(detail.controls, list)


def test_dashboard_shows_statuses_and_password_dialog_builds():
    import flet as ft

    from email_export_import.gui import views
    from email_export_import.gui.i18n import I18n
    from email_export_import.gui.run_manager import RunSnapshot

    i18n = I18n(locale="en")
    noop = lambda *a, **k: None
    snaps = [
        RunSnapshot(key=f"k{i}", title=f"t{i}", status=s, processed=1, total=0,
                    current_folder=None)
        for i, s in enumerate(["running", "paused", "done", "error", "cancelled"])
    ]
    dash = views.build_dashboard(i18n, snaps, noop, noop, noop, noop, noop, noop, noop)
    assert isinstance(dash.controls, list) and len(dash.controls) >= 5

    dlg = views.build_password_dialog(i18n, "a → b", noop, noop)
    assert isinstance(dlg, ft.AlertDialog)


def test_detail_terminal_run_has_no_dead_dismiss():
    from email_export_import.gui import views
    from email_export_import.gui.i18n import I18n
    from email_export_import.gui.run_manager import RunSnapshot

    i18n = I18n(locale="en")
    noop = lambda *a, **k: None

    def button_labels(view):
        found = []
        def walk(c):
            for label_attr in ("text", "value"):
                v = getattr(c, label_attr, None)
                if isinstance(v, str):
                    found.append(v)
            for child in getattr(c, "controls", []) or []:
                walk(child)
            content = getattr(c, "content", None)
            if isinstance(content, str):  # button labels are plain strings in .content
                found.append(content)
            elif content is not None:
                walk(content)
        for c in view.controls:
            walk(c)
        return found

    done = RunSnapshot(key="k", title="t", status="done", processed=1, total=1,
                       current_folder=None)
    labels = button_labels(views.build_detail(i18n, done, noop, noop, noop, noop))
    assert i18n.t("dash.dismiss") not in labels  # no dead dismiss on detail
    assert i18n.t("detail.back") in labels
