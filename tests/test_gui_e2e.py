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

    # After submit: run_async reconnects + starts the run; the resumed run must
    # actually migrate the 3 messages to the destination.
    assert _wait(lambda: len(dst.folders["INBOX"]) == 3), \
        "resumed run never migrated — the Resume click did nothing"
    # ...and its completion callback must have been marshalled onto the page
    # thread (run_thread), not left on the raw run_async worker (which cannot
    # render UI). This is the fix that makes dialogs/view changes appear.
    assert len(page.run_thread_calls) > calls_before, \
        "resume callback was not marshalled onto the page thread (UI would not render)"


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
