"""Headless end-to-end drive of the Flet app wiring.

A FakePage stands in for ft.Page so we can run _page_main() and simulate
clicks by invoking the real on_click handlers the views wire up — catching
wiring/threading bugs that the per-view unit tests cannot.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

flet = pytest.importorskip("flet")
import flet as ft  # noqa: E402

from email_export_import import connection, state  # noqa: E402
from email_export_import.gui import i18n as i18n_module  # noqa: E402
from email_export_import.gui.i18n import I18n  # noqa: E402
from email_export_import.state import MigrationState  # noqa: E402
from tests.fakes import FakeIMAPClient, make_message  # noqa: E402

EN = I18n(locale="en").t


class FakeWindow:
    def __init__(self):
        self.width = 0
        self.height = 0

    def close(self):
        pass


class FakePage:
    """Minimal ft.Page stand-in covering exactly what _page_main touches."""

    def __init__(self):
        self.title = ""
        self.window = FakeWindow()
        self.views: list = []
        self.dialog = None
        self.update_calls = 0
        self.run_thread_calls: list = []

    def update(self):
        self.update_calls += 1

    def show_dialog(self, dlg):
        self.dialog = dlg

    def pop_dialog(self):
        self.dialog = None
        return None

    def run_thread(self, fn, *args, **kwargs):
        # Model Flet: run_thread runs the handler on the page's executor (with
        # the page context that makes UI updates render). We run it on a real
        # daemon thread, exactly like flet, and record it so a test can prove a
        # callback was marshalled through here (the fix for run_async rendering).
        self.run_thread_calls.append(fn)
        threading.Thread(target=lambda: fn(*args, **kwargs), daemon=True).start()

    def run_task(self, fn, *a):
        pass


def _walk(control, out):
    """Collect (label, control) for every control that has an on_click."""
    if control is None:
        return
    on_click = getattr(control, "on_click", None)
    if on_click is not None:
        label = getattr(control, "content", None)
        if not isinstance(label, str):
            label = getattr(control, "text", None)
        out.append((label, control))
    for child in getattr(control, "controls", []) or []:
        _walk(child, out)
    content = getattr(control, "content", None)
    if content is not None and not isinstance(content, str):
        _walk(content, out)
    for action in getattr(control, "actions", []) or []:
        _walk(action, out)


def _clickables(view_or_dialog):
    out: list = []
    for c in getattr(view_or_dialog, "controls", []) or []:
        _walk(c, out)
    for a in getattr(view_or_dialog, "actions", []) or []:
        _walk(a, out)
    content = getattr(view_or_dialog, "content", None)
    if content is not None:
        _walk(content, out)
    return out


def _click(view_or_dialog, label):
    for lbl, ctrl in _clickables(view_or_dialog):
        if lbl == label:
            ctrl.on_click(None)
            return True
    return False


def _text_fields(dialog):
    out: list = []

    def walk(c):
        if isinstance(c, ft.TextField):
            out.append(c)
        for child in getattr(c, "controls", []) or []:
            walk(child)
        content = getattr(c, "content", None)
        if content is not None and not isinstance(content, str):
            walk(content)

    content = getattr(dialog, "content", None)
    if content is not None:
        walk(content)
    return out


def _make_paused_session(tmp_path, dst_verify_ssl=True):
    s = MigrationState.for_pair("a@x.com", "b@y.com", base_dir=tmp_path)
    s.set_config({
        "src": {"host": "src.test", "port": 993, "ssl": True, "verify_ssl": True,
                "email": "a@x.com"},
        "dst": {"host": "dst.test", "port": 993, "ssl": True,
                "verify_ssl": dst_verify_ssl, "email": "b@y.com"},
        "skip": [], "workers": 2, "total": 3,
    })
    s.flush()
    return s


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(state, "DEFAULT_BASE_DIR", tmp_path)
    prefs = tmp_path / "gui.json"
    prefs.write_text(json.dumps({"locale": "en"}))  # deterministic English labels
    monkeypatch.setattr(i18n_module, "DEFAULT_PREFS_PATH", prefs)
    monkeypatch.setattr(connection.time, "sleep", lambda s: None)


def _run_page():
    from email_export_import.gui import app as app_module

    page = FakePage()
    app_module._page_main(page)
    return page


def _wait(pred, timeout=4.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_resume_success_starts_the_run(monkeypatch, tmp_path):
    _make_paused_session(tmp_path)
    dst = FakeIMAPClient(folders={"INBOX": []})

    def factory(host, port=993, ssl=True, **kw):
        if host == "src.test":
            return FakeIMAPClient(
                folders={"INBOX": [make_message(uid=i, message_id=f"<m{i}@x>")
                                   for i in (1, 2, 3)]}
            )
        return dst

    monkeypatch.setattr(connection, "IMAPClient", factory)

    page = _run_page()
    dash = page.views[-1]

    assert _click(dash, EN("dash.resume")), "Resume button not found on paused card"
    assert page.dialog is not None, "password dialog did not open"

    fields = _text_fields(page.dialog)
    assert len(fields) == 2
    fields[0].value = "srcpw"
    fields[1].value = "dstpw"
    calls_before = len(page.run_thread_calls)
    assert _click(page.dialog, EN("resume.go")), "resume-go button not found in dialog"

    # Resume now routes through the plan screen (folder selection before
    # transfer). It appears via a callback marshalled onto the page thread.
    assert _wait(lambda: page.views and page.views[-1].route == "/plan"), \
        "plan screen did not appear after Resume — callback not marshalled/rendered"
    assert len(page.run_thread_calls) > calls_before, \
        "resume callback was not marshalled onto the page thread (UI would not render)"

    # Starting from the plan screen migrates all 3 messages.
    assert _click(page.views[-1], EN("plan.start")), "Start button not found on plan screen"
    assert _wait(lambda: len(dst.folders["INBOX"]) == 3), \
        "migration did not run after Start"


def test_resume_plan_honors_skip_from_config(monkeypatch, tmp_path):
    # Session saved with Junk skipped; resume → plan (Junk unchecked) → Start
    # must migrate INBOX but not Junk.
    s = MigrationState.for_pair("a@x.com", "b@y.com", base_dir=tmp_path)
    s.set_config({
        "src": {"host": "src.test", "port": 993, "ssl": True, "verify_ssl": True,
                "email": "a@x.com"},
        "dst": {"host": "dst.test", "port": 993, "ssl": True, "verify_ssl": False,
                "email": "b@y.com"},
        "skip": ["Junk"], "workers": 1, "total": 2,
    })
    s.flush()
    dst = FakeIMAPClient(folders={"INBOX": [], "Junk": []})

    def factory(host, port=993, ssl=True, **kw):
        if host == "src.test":
            return FakeIMAPClient(folders={
                "INBOX": [make_message(uid=1, message_id="<a@x>")],
                "Junk": [make_message(uid=2, message_id="<b@x>")],
            })
        return dst

    monkeypatch.setattr(connection, "IMAPClient", factory)

    page = _run_page()
    assert _click(page.views[-1], EN("dash.resume"))
    fields = _text_fields(page.dialog)
    fields[0].value = "p"
    fields[1].value = "p"
    _click(page.dialog, EN("resume.go"))
    assert _wait(lambda: page.views and page.views[-1].route == "/plan")

    _click(page.views[-1], EN("plan.start"))
    assert _wait(lambda: len(dst.folders["INBOX"]) == 1)
    assert dst.folders["Junk"] == []  # skipped folder never migrated


def test_cancel_requires_confirmation(monkeypatch, tmp_path):
    _make_paused_session(tmp_path)
    page = _run_page()
    dash = page.views[-1]

    # Clicking Cancel must NOT cancel immediately — it opens an "are you sure?"
    # confirmation first, so a misclick cannot throw away a migration.
    assert _click(dash, EN("dash.cancel")), "Cancel button not found on paused card"
    assert page.dialog is not None, "cancel did not ask for confirmation"
    confirm_labels = [lbl for lbl, _ in _clickables(page.dialog)]
    assert EN("cancel.confirm_yes") in confirm_labels
    assert EN("cancel.confirm_no") in confirm_labels

    # Confirming performs the cancel: the card turns terminal and gains Dismiss
    # (a paused card never has Dismiss).
    assert _click(page.dialog, EN("cancel.confirm_yes"))
    assert _wait(lambda: EN("dash.dismiss")
                 in [lbl for lbl, _ in _clickables(page.views[-1])]), \
        "run was not cancelled after confirming"


def test_cancel_can_be_declined(monkeypatch, tmp_path):
    _make_paused_session(tmp_path)
    page = _run_page()
    dash = page.views[-1]

    assert _click(dash, EN("dash.cancel"))
    assert _click(page.dialog, EN("cancel.confirm_no")), "decline button missing"
    assert page.dialog is None, "declining should close the dialog"
    # The run stays paused: Resume present, no Dismiss.
    labels = [lbl for lbl, _ in _clickables(page.views[-1])]
    assert EN("dash.resume") in labels
    assert EN("dash.dismiss") not in labels


def test_resume_from_cancelled_card(monkeypatch, tmp_path):
    _make_paused_session(tmp_path)
    dst = FakeIMAPClient(folders={"INBOX": []})

    def factory(host, port=993, ssl=True, **kw):
        if host == "src.test":
            return FakeIMAPClient(
                folders={"INBOX": [make_message(uid=i, message_id=f"<m{i}@x>")
                                   for i in (1, 2, 3)]}
            )
        return dst

    monkeypatch.setattr(connection, "IMAPClient", factory)

    page = _run_page()
    # Cancel the paused run (through the confirmation).
    assert _click(page.views[-1], EN("dash.cancel"))
    assert _click(page.dialog, EN("cancel.confirm_yes"))
    assert _wait(lambda: EN("dash.resume")
                 in [lbl for lbl, _ in _clickables(page.views[-1])]), \
        "cancelled card offers no Resume"

    # Resuming a cancelled run reconnects and routes through the plan screen,
    # exactly like resuming a paused one — the on-disk progress is intact.
    assert _click(page.views[-1], EN("dash.resume"))
    assert page.dialog is not None, "password dialog did not open for cancelled resume"
    fields = _text_fields(page.dialog)
    assert len(fields) == 2
    fields[0].value = "p"
    fields[1].value = "p"
    assert _click(page.dialog, EN("resume.go"))
    assert _wait(lambda: page.views and page.views[-1].route == "/plan"), \
        "cancelled resume did not reach the plan screen"


def test_detail_edit_saves_connection(monkeypatch, tmp_path):
    _make_paused_session(tmp_path)  # dst verify_ssl True, host dst.test
    # no IMAP needed — editing/saving is pure config
    page = _run_page()

    assert _click(page.views[-1], EN("dash.detail")), "Detail button not found"
    assert page.views[-1].route == "/detail"
    assert _click(page.views[-1], EN("detail.edit")), "Edit button not found on detail"

    # editor now shown — find the destination host field and change it
    detail = page.views[-1]
    tfs = []

    def collect_tf(c):
        if isinstance(c, ft.TextField):
            tfs.append(c)
        for ch in getattr(c, "controls", []) or []:
            collect_tf(ch)

    for c in detail.controls:
        collect_tf(c)
    # editor has host+port per side; set the last host field (destination)
    host_fields = [t for t in tfs if t.label == EN("account.host")]
    assert len(host_fields) == 2
    host_fields[1].value = "newdst.example.com"

    assert _click(detail, EN("detail.save")), "Save button not found"

    from email_export_import.state import MigrationState as MS
    reloaded = MS.for_pair("a@x.com", "b@y.com", base_dir=tmp_path)
    assert reloaded.config["dst"]["host"] == "newdst.example.com"
    assert reloaded.config["src"]["email"] == "a@x.com"  # untouched


def test_resume_connection_failure_is_visible(monkeypatch, tmp_path):
    _make_paused_session(tmp_path)
    from imapclient.exceptions import LoginError

    def factory(host, port=993, ssl=True, **kw):
        fake = FakeIMAPClient(folders={"INBOX": []})
        if host == "dst.test":
            fake.login_error = LoginError("AUTHENTICATIONFAILED")
        return fake

    monkeypatch.setattr(connection, "IMAPClient", factory)

    page = _run_page()

    assert _click(page.views[-1], EN("dash.resume"))
    fields = _text_fields(page.dialog)
    fields[0].value = "srcpw"
    fields[1].value = "dstpw"
    _click(page.dialog, EN("resume.go"))

    # The failure must reach the user — an error dialog — not vanish silently.
    assert _wait(lambda: page.dialog is not None
                 and EN("status.error") in _dialog_title(page.dialog)), \
        "resume failure was invisible — no error surfaced to the user"


def _dialog_title(dlg):
    title = getattr(dlg, "title", None)
    if title is None:
        return ""
    return getattr(title, "value", "") or ""


def test_settings_button_opens_settings_and_completed_card_shown(monkeypatch, tmp_path):
    # a completed session must appear as a done card on the dashboard
    done = MigrationState.for_pair("done@x.com", "done@y.com", base_dir=tmp_path)
    done.set_config({"src": {"host": "s", "port": 993, "ssl": True, "verify_ssl": True,
                             "email": "done@x.com"},
                     "dst": {"host": "d", "port": 993, "ssl": True, "verify_ssl": True,
                             "email": "done@y.com"},
                     "skip": [], "workers": 2, "total": 5})
    done.mark_migrated("INBOX", "<m@x>", 1)
    done.mark_completed()
    done.flush()

    page = _run_page()
    dash = page.views[-1]
    labels = [lbl for lbl, _ in _clickables(dash)]

    # done card is visible (Detail button present) and Settings nav exists
    assert EN("nav.settings") in labels
    assert EN("dash.detail") in labels  # the completed card rendered

    assert _click(dash, EN("nav.settings")), "Settings button did nothing"
    assert page.views[-1].route == "/settings"
    # language + back present on settings
    settings_labels = [lbl for lbl, _ in _clickables(page.views[-1])]
    assert EN("detail.back") in settings_labels


def test_update_banner_shown_when_newer_available(monkeypatch, tmp_path):
    from email_export_import.gui import app as app_module
    from email_export_import.gui import updater as updater_mod
    from email_export_import.gui.updater import UpdateInfo

    fake = UpdateInfo(version="v9.9.9", asset_url="https://x/a",
                      asset_name="email-export-import-macos.zip", sha256="abc")
    monkeypatch.setattr(updater_mod, "check_for_update", lambda *a, **k: fake)
    monkeypatch.setattr(app_module.updater, "check_for_update", lambda *a, **k: fake)

    page = _run_page()
    # startup check runs async and shows the update dialog
    assert _wait(lambda: page.dialog is not None
                 and "9.9.9" in _dialog_content(page.dialog)), \
        "update dialog not shown for a newer release"
    # the dialog offers Update
    labels = [lbl for lbl, _ in _clickables(page.dialog)]
    assert EN("update.now") in labels


def test_no_update_dialog_when_up_to_date(monkeypatch, tmp_path):
    from email_export_import.gui import app as app_module

    monkeypatch.setattr(app_module.updater, "check_for_update", lambda *a, **k: None)
    page = _run_page()
    # give the async startup check a moment; no dialog should appear
    import time as _t
    _t.sleep(0.3)
    assert page.dialog is None


def _dialog_content(dlg):
    content = getattr(dlg, "content", None)
    return getattr(content, "value", "") or ""
