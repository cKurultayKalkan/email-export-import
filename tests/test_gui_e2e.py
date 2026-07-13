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
        self.minimized = False
        self.prevent_close = False
        self.on_event = None
        self.destroy_calls = 0

    def close(self):
        pass

    async def destroy(self):
        self.destroy_calls += 1


class FakePage:
    """Minimal ft.Page stand-in covering exactly what _page_main touches."""

    def __init__(self):
        self.title = ""
        self.window = FakeWindow()
        self.views: list = []
        self.overlay: list = []
        self.dialog = None
        self.update_calls = 0
        self.run_thread_calls: list = []
        self.run_task_calls: list = []

    def update(self):
        self.update_calls += 1

    def show_dialog(self, dlg):
        self.dialog = dlg

    def pop_dialog(self):
        self.dialog = None
        return None

    def run_thread(self, fn, *args, **kwargs):
        # Model Flet: run_thread runs the handler on the page's executor — a
        # bare worker thread, OFF the event loop. In flet 0.85 UI mutation is
        # only race-free on the loop, so app code must NOT route UI callbacks
        # here; recording it lets a test assert nothing does.
        self.run_thread_calls.append(fn)
        threading.Thread(target=lambda: fn(*args, **kwargs), daemon=True).start()

    def run_task(self, fn, *a):
        # Model Flet: run_task runs a coroutine ON the event loop (via
        # run_coroutine_threadsafe). Headless we drive it to completion on a
        # daemon thread with its own loop, and record it so a test can prove a
        # UI callback / the poll was marshalled onto the loop — the fix that
        # keeps buttons live.
        import asyncio

        self.run_task_calls.append(fn)

        def runner():
            try:
                asyncio.run(fn(*a))
            except Exception:
                pass

        threading.Thread(target=runner, daemon=True).start()


def _walk(control, out):
    """Collect (label, control) for every control that has an on_click."""
    if control is None:
        return
    on_click = getattr(control, "on_click", None)
    if on_click is not None:
        label = getattr(control, "content", None)
        if not isinstance(label, str):
            # a Text control as content (e.g. menu items) — use its value
            label = getattr(label, "value", None) if label is not None else None
        if not isinstance(label, str):
            label = getattr(control, "text", None)
        if not isinstance(label, str):
            # composite content (e.g. toolbar icon+label): first Text within
            stack = [getattr(control, "content", None)]
            while stack:
                c = stack.pop()
                if c is None:
                    continue
                v = getattr(c, "value", None)
                if isinstance(v, str) and v:
                    label = v
                    break
                stack.extend(getattr(c, "controls", []) or [])
                inner = getattr(c, "content", None)
                if inner is not None and not isinstance(inner, str):
                    stack.append(inner)
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


def test_ui_work_is_marshalled_onto_the_event_loop_not_the_executor(monkeypatch, tmp_path):
    """Regression: buttons went dead (no hover/click/action) because the poll
    and the run_async result callbacks mutated the UI from executor threads
    (page.run_thread). In flet 0.85 control.update()/page.update() run the diff
    engine and enqueue frames on a loop-bound asyncio.Queue with no thread
    marshalling — safe ONLY on the event loop. Off-loop mutation races the
    loop's own click handling and desyncs the client, so clicks reach nothing.

    Every UI-touching path must therefore go through run_task (the loop), never
    run_thread (the executor). This test fails on the old routing.
    """
    _make_paused_session(tmp_path)
    dst = FakeIMAPClient(folders={"INBOX": []})

    def factory(host, port=993, ssl=True, **kw):
        if host == "src.test":
            return FakeIMAPClient(
                folders={"INBOX": [make_message(uid=1, message_id="<m1@x>")]}
            )
        return dst

    monkeypatch.setattr(connection, "IMAPClient", factory)

    page = _run_page()

    # The background poll must be started on the event loop.
    assert any(getattr(fn, "__name__", "") == "poll" for fn in page.run_task_calls), \
        "poll was not started via run_task — it runs off the event loop"

    # Resume's reconnect result must be delivered on the loop too.
    assert _click(page.views[-1], EN("dash.resume"))
    fields = _text_fields(page.dialog)
    fields[0].value = "p"
    fields[1].value = "p"
    tasks_before = len(page.run_task_calls)
    assert _click(page.dialog, EN("resume.go"))
    assert _wait(lambda: page.views and page.views[-1].route == "/plan"), \
        "resume callback did not render the plan screen"
    assert len(page.run_task_calls) > tasks_before, \
        "resume callback was not marshalled onto the event loop"

    # Nothing UI-related may ever hop to the executor.
    assert page.run_thread_calls == [], \
        f"UI work ran on the executor (off-loop): {page.run_thread_calls}"


class _SharedMailboxFake(FakeIMAPClient):
    """A fake whose mailbox is keyed by the logged-in account email and shared
    across every connection for that account — like a real server, where the
    run's planning connection and its parallel worker connections all see the
    same UIDs. (A fresh mailbox per connection would make every message look
    vanished under the parallel worker path.)"""

    def __init__(self, mailboxes, seed, gate=None, gate_email=None):
        super().__init__(folders={"INBOX": []})
        self._mailboxes = mailboxes
        self._seed = seed
        self._gate = gate
        self._gate_email = gate_email
        self._user = None

    def login(self, user, password):
        super().login(user, password)
        self._user = user
        self.folders = self._mailboxes.setdefault(user, self._seed(user))

    def list_folders(self):
        if self._gate is not None and self._user == self._gate_email:
            self._gate.wait(timeout=5)
        return super().list_folders()


def _bulk_factories(gate=None, gate_email=None):
    """Return (factory, src_mailboxes, dst_mailboxes). Source accounts each get
    one message; destinations start empty. Mailboxes are shared per email."""
    src_mailboxes: dict = {}
    dst_mailboxes: dict = {}

    def seed_src(email):
        return {"INBOX": [make_message(uid=1, message_id=f"<{email}>")]}

    def factory(host, port=993, ssl=True, **kw):
        if host == "src.test":
            return _SharedMailboxFake(src_mailboxes, seed_src, gate, gate_email)
        return _SharedMailboxFake(dst_mailboxes, lambda e: {"INBOX": []})

    return factory, src_mailboxes, dst_mailboxes


def _all_text_fields(view):
    out = []

    def walk(c):
        if isinstance(c, ft.TextField):
            out.append(c)
        for ch in getattr(c, "controls", []) or []:
            walk(ch)
        content = getattr(c, "content", None)
        if content is not None and not isinstance(content, str):
            walk(content)

    for c in getattr(view, "controls", []) or []:
        walk(c)
    return out


def _fill_bulk(page, src_host, dst_host, accounts):
    """On the /bulk view: set source+dest host, add rows, fill each account,
    then click Start all. accounts = [(email, src_pw, dst_pw), ...]."""
    view = page.views[-1]
    # add the extra rows (one row exists by default)
    for _ in range(len(accounts) - 1):
        assert _click(view, EN("bulk.add_row"))
    tfs = _all_text_fields(view)
    hosts = [t for t in tfs if t.label == EN("account.host")]
    hosts[0].value = src_host
    hosts[1].value = dst_host
    emails = [t for t in tfs if t.label == EN("bulk.email")]
    src_pws = [t for t in tfs if t.label == EN("bulk.src_password")]
    dst_pws = [t for t in tfs if t.label == EN("bulk.dst_password")]
    for i, (email, spw, dpw) in enumerate(accounts):
        emails[i].value = email
        src_pws[i].value = spw
        dst_pws[i].value = dpw
    assert _click(view, EN("bulk.start_all")), "Start-all button not found"


def test_bulk_starts_all_accounts(monkeypatch, tmp_path):
    factory, src_mb, dst_mb = _bulk_factories()
    monkeypatch.setattr(connection, "IMAPClient", factory)

    page = _run_page()
    assert _click(page.views[-1], EN("menu.bulk")), "Bulk menu item not found"
    assert page.views[-1].route == "/bulk"
    _fill_bulk(page, "src.test", "dst.test",
               [("a@x.com", "p1", "p1"), ("b@x.com", "p2", "p2")])

    # both accounts migrate their one message (cap default 2 → both run)
    assert _wait(lambda: sum(len(mb["INBOX"]) for mb in list(dst_mb.values())) == 2), \
        "not all bulk accounts migrated"
    assert dst_mb["a@x.com"]["INBOX"] and dst_mb["b@x.com"]["INBOX"]


def test_bulk_cap_one_queues_second(monkeypatch, tmp_path):
    from email_export_import.gui import prefs
    prefs.save_pref(tmp_path / "gui.json", "max_active", 1)  # cap = 1

    gate = threading.Event()
    factory, src_mb, dst_mb = _bulk_factories(gate=gate, gate_email="a@x.com")
    monkeypatch.setattr(connection, "IMAPClient", factory)

    page = _run_page()
    assert _click(page.views[-1], EN("menu.bulk"))
    _fill_bulk(page, "src.test", "dst.test",
               [("a@x.com", "p1", "p1"), ("b@x.com", "p2", "p2")])

    # account 1 connects and blocks in planning; with cap=1 account 2 must NOT
    # even start connecting (its source never logs in).
    assert _wait(lambda: "a@x.com" in src_mb), "first account never connected"
    time.sleep(0.4)
    assert "b@x.com" not in src_mb, "second account started despite cap=1"

    gate.set()  # release account 1
    assert _wait(lambda: sum(len(mb["INBOX"]) for mb in list(dst_mb.values())) == 2), \
        "queued account did not run after the first finished"
    assert dst_mb["b@x.com"]["INBOX"], "second (queued) account never ran"


def _make_completed_session(tmp_path, already_migrated="<old@x>"):
    s = MigrationState.for_pair("a@x.com", "b@y.com", base_dir=tmp_path)
    s.set_config({
        "src": {"host": "src.test", "port": 993, "ssl": True, "verify_ssl": True,
                "email": "a@x.com"},
        "dst": {"host": "dst.test", "port": 993, "ssl": True, "verify_ssl": True,
                "email": "b@y.com"},
        "skip": [], "workers": 1, "total": 1,
    })
    s.mark_migrated("INBOX", already_migrated, 1)
    s.mark_completed()
    s.flush()
    return s


def test_sync_a_finished_migration_copies_only_new_mail(monkeypatch, tmp_path):
    """A finished migration can be re-synced to pick up mail that arrived since.
    Dedup (by Message-ID) must skip everything already moved and copy ONLY the
    new messages — no duplicates."""
    _make_completed_session(tmp_path, already_migrated="<old@x>")
    dst = FakeIMAPClient(folders={"INBOX": []})

    def factory(host, port=993, ssl=True, **kw):
        if host == "src.test":
            # the old (already-migrated) message plus one that arrived since
            return FakeIMAPClient(folders={"INBOX": [
                make_message(uid=1, message_id="<old@x>"),
                make_message(uid=2, message_id="<new@x>"),
            ]})
        return dst

    monkeypatch.setattr(connection, "IMAPClient", factory)

    page = _run_page()
    # the done card offers Sync (not Resume)
    labels = [lbl for lbl, _ in _clickables(page.views[-1])]
    assert EN("dash.sync") in labels, "finished migration has no Sync button"

    assert _click(page.views[-1], EN("dash.sync"))
    fields = _text_fields(page.dialog)
    fields[0].value = "p"
    fields[1].value = "p"
    _click(page.dialog, EN("resume.go"))
    assert _wait(lambda: page.views and page.views[-1].route == "/plan"), \
        "sync did not reach the plan screen"

    _click(page.views[-1], EN("plan.start"))
    assert _wait(lambda: len(dst.folders["INBOX"]) == 1), \
        "sync copied nothing (or the wrong count)"
    time.sleep(0.3)  # let any further (wrong) appends land before asserting

    bodies = b"".join(m["body"] for m in dst.folders["INBOX"])
    assert b"<new@x>" in bodies, "the newly-arrived message was not copied"
    assert b"<old@x>" not in bodies, "already-migrated message was copied again"
    assert len(dst.folders["INBOX"]) == 1, "sync duplicated mail"

    # the state is reopened while running, then filed as completed again
    assert _wait(lambda: MigrationState.for_pair(
        "a@x.com", "b@y.com", base_dir=tmp_path).status == "completed"), \
        "synced run was not filed as completed"
    reloaded = MigrationState.for_pair("a@x.com", "b@y.com", base_dir=tmp_path)
    assert reloaded.is_migrated("INBOX", "<new@x>", 2), \
        "the new message was not recorded — a second sync would copy it again"


def test_resume_shows_loading_overlay_until_plan_appears(monkeypatch, tmp_path):
    """Reconnect + folder listing can take many seconds on a slow/rate-limiting
    server. Without feedback the app looks frozen ('screen just sat there'), so
    a dimmed spinner layer must cover the wait and clear once the plan renders."""
    _make_paused_session(tmp_path)
    gate = threading.Event()

    def factory(host, port=993, ssl=True, **kw):
        if host == "src.test":
            c = FakeIMAPClient(
                folders={"INBOX": [make_message(uid=1, message_id="<m1@x>")]}
            )
            orig = c.list_folders
            c.list_folders = lambda: (gate.wait(timeout=5), orig())[1]  # stall planning
            return c
        return FakeIMAPClient(folders={"INBOX": []})

    monkeypatch.setattr(connection, "IMAPClient", factory)

    page = _run_page()
    overlay = page.overlay[0]
    assert overlay.visible is False, "spinner must start hidden"

    assert _click(page.views[-1], EN("dash.resume"))
    fields = _text_fields(page.dialog)
    fields[0].value = "p"
    fields[1].value = "p"
    _click(page.dialog, EN("resume.go"))

    assert _wait(lambda: overlay.visible is True), \
        "no loading overlay while resume reconnects — the app looks frozen"

    gate.set()
    assert _wait(lambda: page.views and page.views[-1].route == "/plan")
    assert overlay.visible is False, "loading overlay not cleared once the plan rendered"


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
    calls_before = len(page.run_task_calls)
    assert _click(page.dialog, EN("resume.go")), "resume-go button not found in dialog"

    # Resume now routes through the plan screen (folder selection before
    # transfer). It appears via a callback marshalled onto the event loop.
    assert _wait(lambda: page.views and page.views[-1].route == "/plan"), \
        "plan screen did not appear after Resume — callback not marshalled/rendered"
    assert len(page.run_task_calls) > calls_before, \
        "resume callback was not marshalled onto the event loop (UI would not render)"

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

    # the paused run is auto-selected; Edit lives in the side panel and
    # opens a dialog editor
    assert _click(page.views[-1], EN("detail.edit")), "Edit button not found in panel"
    assert page.dialog is not None
    detail = page.dialog
    tfs = []

    def collect_tf(c):
        if isinstance(c, ft.TextField):
            tfs.append(c)
        for ch in getattr(c, "controls", []) or []:
            collect_tf(ch)
        content = getattr(c, "content", None)
        if content is not None and not isinstance(content, str):
            collect_tf(content)

    collect_tf(detail.content)  # the editor lives in the dialog's content
    # editor has host+port per side; set the last host field (destination)
    host_fields = [t for t in tfs if t.label == EN("account.host")]
    assert len(host_fields) == 2
    host_fields[1].value = "newdst.example.com"

    assert _click(detail, EN("detail.save")), "Save button not found"
    assert page.dialog is None  # dialog closed on save

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
    # …and a failed transition must never leave the spinner stuck on screen.
    assert page.overlay[0].visible is False, "loading overlay stuck after a failure"


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
    assert EN("dash.sync") in labels  # the completed run's panel rendered

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


# ---- close-to-background ---------------------------------------------------
# Closing the window must not silently kill running migrations: with work
# active the app offers to keep going minimized (Dock/taskbar), with nothing
# active it just quits.

def _fire_close(page):
    import types

    assert page.window.on_event is not None, "no window event handler wired"
    page.window.on_event(types.SimpleNamespace(type=ft.WindowEventType.CLOSE))


def _fake_running_snapshot():
    from email_export_import.gui.run_manager import RunSnapshot

    return RunSnapshot(key="k", title="t", status="running", processed=1,
                       total=5, current_folder="INBOX")


def test_window_close_is_intercepted():
    page = _run_page()
    assert page.window.prevent_close is True
    assert page.window.on_event is not None


def test_close_when_idle_quits():
    page = _run_page()
    _fire_close(page)
    assert _wait(lambda: page.window.destroy_calls == 1), \
        "idle close did not destroy the window"
    assert page.dialog is None  # no pointless dialog when nothing is running


def test_close_with_active_run_offers_background(monkeypatch):
    from email_export_import.gui.run_manager import RunManager

    page = _run_page()
    monkeypatch.setattr(RunManager, "snapshot_all",
                        lambda self: [_fake_running_snapshot()])
    _fire_close(page)
    assert page.dialog is not None, "close with active runs must ask, not die"
    labels = [lbl for lbl, _ in _clickables(page.dialog)]
    assert EN("close.background") in labels
    assert EN("close.quit") in labels
    assert page.window.destroy_calls == 0

    assert _click(page.dialog, EN("close.background"))
    assert page.dialog is None
    assert page.window.minimized is True, "background choice must minimize"
    assert page.window.destroy_calls == 0  # still alive


def test_close_with_active_run_can_still_quit(monkeypatch):
    from email_export_import.gui.run_manager import RunManager

    page = _run_page()
    monkeypatch.setattr(RunManager, "snapshot_all",
                        lambda self: [_fake_running_snapshot()])
    _fire_close(page)
    assert _click(page.dialog, EN("close.quit"))
    assert page.dialog is None
    assert _wait(lambda: page.window.destroy_calls == 1), \
        "quit choice did not destroy the window"


def test_queued_bulk_work_counts_as_active(monkeypatch):
    from email_export_import.gui.run_manager import RunManager, RunSnapshot

    page = _run_page()
    queued = RunSnapshot(key="k", title="t", status="queued", processed=0,
                         total=0, current_folder=None)
    monkeypatch.setattr(RunManager, "snapshot_all", lambda self: [queued])
    _fire_close(page)
    assert page.dialog is not None, \
        "queued (bulk) work pending — close must ask, not quit"


# ---- menu bar ---------------------------------------------------------------
# Every screen carries the same top menu (Migration / View / Help); the
# Migration menu additionally grows context items for the current page.

def _menubar_of(view):
    # The bar ships wrapped in a full-width container — find it by walking.
    found: list = []

    def walk(c):
        if isinstance(c, ft.MenuBar):
            found.append(c)
            return
        for ch in getattr(c, "controls", []) or []:
            walk(ch)
        content = getattr(c, "content", None)
        if content is not None and not isinstance(content, str):
            walk(content)

    for c in view.controls:
        walk(c)
    return found[0] if found else None


def test_every_screen_has_the_menubar():
    page = _run_page()
    assert _menubar_of(page.views[-1]) is not None  # dashboard

    assert _click(page.views[-1], EN("menu.settings")) or \
        _click(page.views[-1], EN("nav.settings"))
    assert page.views[-1].route == "/settings"
    assert _menubar_of(page.views[-1]) is not None

    assert _click(page.views[-1], EN("menu.new"))
    assert page.views[-1].route == "/source"  # wizard starts at the source step
    assert _menubar_of(page.views[-1]) is not None


def test_menubar_routes_dashboard_bulk_and_updates(monkeypatch):
    from email_export_import.gui import app as app_module

    checked = []
    monkeypatch.setattr(app_module.updater, "check_for_update",
                        lambda *a, **k: checked.append(True) or None)
    page = _run_page()
    assert _click(page.views[-1], EN("menu.bulk"))
    assert page.views[-1].route == "/bulk"
    assert _click(page.views[-1], EN("menu.dashboard"))
    assert page.views[-1].route == "/"
    before = len(checked)
    assert _click(page.views[-1], EN("settings.check_updates"))
    assert _wait(lambda: len(checked) > before), "menu did not trigger update check"


def test_menubar_about_dialog():
    page = _run_page()
    assert _click(page.views[-1], EN("menu.about"))
    assert page.dialog is not None
    assert "0." in _dialog_content(page.dialog)  # shows the version


def test_menubar_quit_uses_the_close_guard(monkeypatch):
    from email_export_import.gui.run_manager import RunManager

    page = _run_page()
    monkeypatch.setattr(RunManager, "snapshot_all",
                        lambda self: [_fake_running_snapshot()])
    assert _click(page.views[-1], EN("menu.quit"))
    assert page.dialog is not None, "quit with active runs must ask first"
    assert page.window.destroy_calls == 0


def _texts_of(view):
    found: list = []

    def walk(c):
        v = getattr(c, "value", None)
        if isinstance(v, str):
            found.append(v)
        for ch in getattr(c, "controls", []) or []:
            walk(ch)
        content = getattr(c, "content", None)
        if content is not None and not isinstance(content, str):
            walk(content)

    for c in view.controls:
        walk(c)
    return found


def test_desktop_chrome_toolbar_and_statusbar():
    page = _run_page()
    view = page.views[-1]
    texts = _texts_of(view)
    assert EN("status.ready") in texts, "status bar missing"
    # toolbar carries the core actions as icon+label buttons
    labels = [lbl for lbl, _ in _clickables(view)]
    assert labels.count(EN("menu.new")) >= 2  # menu item + toolbar button
    # chrome present on settings too, with the version in the status bar
    assert _click(view, EN("nav.settings"))
    from email_export_import import __version__
    assert f"v{__version__}" in _texts_of(page.views[-1])


def test_toolbar_context_action_on_plan_screen(monkeypatch, tmp_path):
    _make_paused_session(tmp_path)
    monkeypatch.setattr(
        connection, "IMAPClient",
        lambda host, port=993, ssl=True, **kw: FakeIMAPClient(
            folders={"INBOX": [make_message(uid=1, message_id="<m1@x>")]}
        ),
    )
    page = _run_page()
    assert _click(page.views[-1], EN("dash.resume"))
    fields = _text_fields(page.dialog)
    fields[0].value = "p"
    fields[1].value = "p"
    assert _click(page.dialog, EN("resume.go"))
    assert _wait(lambda: page.views and page.views[-1].route == "/plan")
    labels = [lbl for lbl, _ in _clickables(page.views[-1])]
    assert labels.count(EN("plan.start")) >= 2  # plan button + toolbar/menu extra
