import threading

import pytest
from imapclient.exceptions import IMAPClientError

from email_export_import import connection
from email_export_import.gui.controller import Controller
from email_export_import.gui.run_manager import Run, RunSnapshot
from email_export_import.models import Account
from email_export_import.state import MigrationState
from tests.fakes import FakeIMAPClient, make_message

SRC = Account(host="src.test", port=993, ssl=True, email="a@x", password="p")
DST = Account(host="dst.test", port=993, ssl=True, email="b@y", password="p")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(connection.time, "sleep", lambda s: None)


def wire(monkeypatch, src_data, dst_fake):
    def factory(host, port=993, ssl=True, **kw):
        if host == "src.test":
            return FakeIMAPClient(folders=src_data)
        return dst_fake

    monkeypatch.setattr(connection, "IMAPClient", factory)


def build_run(monkeypatch, tmp_path, src_data, dst_fake, key="a@x__b@y", **kw):
    wire(monkeypatch, src_data, dst_fake)
    c = Controller(state_dir=tmp_path)
    src_conn = c.test_connection(SRC).conn
    dst_conn = c.test_connection(DST).conn
    plan = c.build_plan(src_conn, dst_conn, skip=set())
    state = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)
    return Run(
        key=key, title="a@x → b@y", src_conn=src_conn, dst_conn=dst_conn,
        plans=plan.plans, state=state, workers=kw.get("workers", 1),
        total=plan.total, skip=set(), spool_enabled=kw.get("spool", False),
        state_dir=tmp_path,
    )


def test_run_completes_and_marks_done(monkeypatch, tmp_path):
    src_data = {"INBOX": [make_message(uid=i, message_id=f"<m{i}@x>") for i in (1, 2, 3)]}
    dst = FakeIMAPClient(folders={"INBOX": []})
    run = build_run(monkeypatch, tmp_path, src_data, dst)
    run.start()
    run.join(timeout=10)
    snap = run.snapshot()
    assert snap.status == "done"
    assert snap.result.migrated == 3
    assert snap.processed == 3
    assert MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path).status == "completed"
    assert MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path).config["total"] == 3


def test_pause_leaves_state_resumable(monkeypatch, tmp_path):
    src_data = {"INBOX": [make_message(uid=i, message_id=f"<m{i}@x>") for i in range(1, 30)]}
    dst = FakeIMAPClient(folders={"INBOX": []})
    gate = threading.Event()
    real_append = dst.append

    def gated(folder, body, flags=(), msg_time=None):
        gate.wait(timeout=5)
        return real_append(folder, body, flags=flags, msg_time=msg_time)

    dst.append = gated
    run = build_run(monkeypatch, tmp_path, src_data, dst)
    run.start()
    run.pause()
    gate.set()
    run.join(timeout=10)
    snap = run.snapshot()
    assert snap.status == "paused"
    reloaded = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)
    assert reloaded.status == "running"  # still resumable on disk
    assert snap.result.migrated < 29


def test_cancel_is_terminal_status(monkeypatch, tmp_path):
    src_data = {"INBOX": [make_message(uid=i, message_id=f"<m{i}@x>") for i in range(1, 30)]}
    dst = FakeIMAPClient(folders={"INBOX": []})
    gate = threading.Event()
    real_append = dst.append

    def gated(folder, body, flags=(), msg_time=None):
        gate.wait(timeout=5)
        return real_append(folder, body, flags=flags, msg_time=msg_time)

    dst.append = gated
    run = build_run(monkeypatch, tmp_path, src_data, dst)
    run.start()
    run.cancel()
    gate.set()
    run.join(timeout=10)
    assert run.snapshot().status == "cancelled"


def test_quota_becomes_error_status(monkeypatch, tmp_path):
    src_data = {"INBOX": [make_message(uid=1, message_id="<m1@x>")]}
    dst = FakeIMAPClient(folders={"INBOX": []})
    dst.append_error = IMAPClientError("APPEND failed [OVERQUOTA]")
    run = build_run(monkeypatch, tmp_path, src_data, dst)
    run.start()
    run.join(timeout=10)
    snap = run.snapshot()
    assert snap.status == "error"
    assert snap.error_kind == "quota"
    assert MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path).status == "running"


def test_placeholder_from_disk_session(tmp_path):
    state = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)
    state.set_config({
        "src": {"host": "h", "email": "a@x"},
        "dst": {"host": "h2", "email": "b@y"},
        "total": 100,
    })
    state.mark_migrated("INBOX", "<m1@x>", 1)
    state.flush()

    run = Run.placeholder(state, state_dir=tmp_path)
    snap = run.snapshot()
    assert snap.status == "paused"
    assert snap.key == "a@x__b@y"
    assert "a@x" in snap.title and "b@y" in snap.title
    assert snap.processed == 1
    assert snap.total == 100
    assert run.is_active is False


def test_placeholder_without_total_shows_zero_total(tmp_path):
    state = MigrationState.for_pair("c@x", "d@y", base_dir=tmp_path)
    state.set_config({"src": {"email": "c@x", "host": "h"}, "dst": {"email": "d@y", "host": "h2"}})
    state.flush()
    snap = Run.placeholder(state, state_dir=tmp_path).snapshot()
    assert snap.total == 0  # CLI-written session: M unknown, UI shows only N
