import threading
import time

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


def test_cancel_after_done_keeps_done(monkeypatch, tmp_path):
    src_data = {"INBOX": [make_message(uid=1, message_id="<m1@x>")]}
    dst = FakeIMAPClient(folders={"INBOX": []})
    run = build_run(monkeypatch, tmp_path, src_data, dst)
    run.start()
    run.join(timeout=10)
    assert run.snapshot().status == "done"
    run.cancel()
    assert run.snapshot().status == "done"  # terminal status not stomped


def test_manager_concurrent_runs_are_independent(monkeypatch, tmp_path):
    from email_export_import.gui.run_manager import RunManager

    dst_ok = FakeIMAPClient(folders={"INBOX": []})
    dst_bad = FakeIMAPClient(folders={"INBOX": []})
    dst_bad.append_error = IMAPClientError("APPEND failed [OVERQUOTA]")

    def factory(host, port=993, ssl=True, **kw):
        if host == "src.test":
            return FakeIMAPClient(
                folders={"INBOX": [make_message(uid=1, message_id="<m1@x>")]}
            )
        return dst_ok if host == "dst.test" else dst_bad

    monkeypatch.setattr(connection, "IMAPClient", factory)
    c = Controller(state_dir=tmp_path)

    def make_run(dst_host, dst_email, key):
        src_conn = c.test_connection(SRC).conn
        dst_acc = Account(host=dst_host, port=993, ssl=True, email=dst_email, password="p")
        dst_conn = c.test_connection(dst_acc).conn
        plan = c.build_plan(src_conn, dst_conn, skip=set())
        state = MigrationState.for_pair("a@x", dst_email, base_dir=tmp_path)
        return Run(key=key, title=key, src_conn=src_conn, dst_conn=dst_conn,
                   plans=plan.plans, state=state, workers=1, total=plan.total,
                   skip=set(), spool_enabled=False, state_dir=tmp_path)

    m = RunManager(state_dir=tmp_path)
    ok_run = make_run("dst.test", "b@y", "a@x__b@y")
    bad_run = make_run("bad.test", "q@z", "a@x__q@z")
    assert m.add(ok_run) and m.add(bad_run)
    ok_run.start()
    bad_run.start()
    ok_run.join(timeout=10)
    bad_run.join(timeout=10)

    statuses = {s.key: s.status for s in m.snapshot_all()}
    assert statuses["a@x__b@y"] == "done"
    assert statuses["a@x__q@z"] == "error"


def test_manager_duplicate_key_guard(monkeypatch, tmp_path):
    from email_export_import.gui.run_manager import RunManager

    src_data = {"INBOX": [make_message(uid=i, message_id=f"<m{i}@x>") for i in range(1, 20)]}
    dst = FakeIMAPClient(folders={"INBOX": []})
    gate = threading.Event()
    real_append = dst.append
    dst.append = lambda f, b, flags=(), msg_time=None: (gate.wait(timeout=5), real_append(f, b, flags=flags, msg_time=msg_time))[1]

    m = RunManager(state_dir=tmp_path)
    run1 = build_run(monkeypatch, tmp_path, src_data, dst)
    assert m.add(run1) is True
    run1.start()
    run2 = build_run(monkeypatch, tmp_path, src_data, dst)
    assert m.add(run2) is False  # active run with same key
    gate.set()
    run1.join(timeout=10)
    assert m.add(run2) is True  # replace once inactive


def test_manager_load_resumable_and_remove_cancelled(tmp_path):
    from email_export_import.gui.run_manager import RunManager

    state = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)
    state.set_config({"src": {"email": "a@x", "host": "h"},
                      "dst": {"email": "b@y", "host": "h2"}, "total": 5})
    state.flush()

    m = RunManager(state_dir=tmp_path)
    m.load_resumable()
    assert [s.status for s in m.snapshot_all()] == ["paused"]

    run = m.get("a@x__b@y")
    run.cancel()  # placeholder → immediate terminal
    assert run.snapshot().status == "cancelled"
    m.remove("a@x__b@y")
    assert m.snapshot_all() == []
    # dismissed-cancelled stays gone across launches
    m2 = RunManager(state_dir=tmp_path)
    m2.load_resumable()
    assert m2.snapshot_all() == []


def test_manager_default_workers(monkeypatch, tmp_path):
    from email_export_import.gui.run_manager import RunManager

    src_data = {"INBOX": [make_message(uid=i, message_id=f"<m{i}@x>") for i in range(1, 20)]}
    dst = FakeIMAPClient(folders={"INBOX": []})
    gate = threading.Event()
    real_append = dst.append
    dst.append = lambda f, b, flags=(), msg_time=None: (gate.wait(timeout=5), real_append(f, b, flags=flags, msg_time=msg_time))[1]

    m = RunManager(state_dir=tmp_path)
    assert m.default_workers() == 4
    run = build_run(monkeypatch, tmp_path, src_data, dst)
    m.add(run)
    run.start()
    assert m.default_workers() == 2
    gate.set()
    run.join(timeout=10)
    assert m.default_workers() == 4


def test_manager_concurrent_add_remove_during_iteration(monkeypatch, tmp_path):
    from email_export_import.gui.run_manager import RunManager

    m = RunManager(state_dir=tmp_path)
    for i in range(20):
        state = MigrationState.for_pair(f"a{i}@x", "b@y", base_dir=tmp_path)
        state.set_config({"src": {"email": f"a{i}@x", "host": "h"},
                          "dst": {"email": "b@y", "host": "h2"}})
        state.flush()
        m.add(Run.placeholder(state, state_dir=tmp_path))

    stop = threading.Event()
    errors = []

    def churn():
        i = 100
        while not stop.is_set():
            state = MigrationState.for_pair(f"c{i}@x", "d@y", base_dir=tmp_path)
            state.set_config({"src": {"email": f"c{i}@x", "host": "h"},
                              "dst": {"email": "d@y", "host": "h2"}})
            state.flush()
            m.add(Run.placeholder(state, state_dir=tmp_path))
            m.remove(f"c{i}@x__d@y")
            i += 1

    def poll():
        while not stop.is_set():
            try:
                m.snapshot_all()
            except Exception as exc:
                errors.append(exc)
                return

    threads = [threading.Thread(target=churn), threading.Thread(target=poll)]
    for t in threads:
        t.start()
    time.sleep(0.5)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    assert errors == []  # no dictionary-changed-size crash


def test_stopping_status_while_draining(monkeypatch, tmp_path):
    src_data = {"INBOX": [make_message(uid=i, message_id=f"<m{i}@x>") for i in range(1, 20)]}
    dst = FakeIMAPClient(folders={"INBOX": []})
    gate = threading.Event()
    real_append = dst.append
    dst.append = lambda f, b, flags=(), msg_time=None: (gate.wait(timeout=5), real_append(f, b, flags=flags, msg_time=msg_time))[1]
    run = build_run(monkeypatch, tmp_path, src_data, dst)
    run.start()
    # let it park mid-run, then request cancel
    import time as _t
    _t.sleep(0.05)
    run.cancel()
    assert run.snapshot().status == "stopping"  # immediate feedback while draining
    gate.set()
    run.join(timeout=10)
    assert run.snapshot().status == "cancelled"


def test_cancel_then_pause_stays_cancelled(monkeypatch, tmp_path):
    src_data = {"INBOX": [make_message(uid=i, message_id=f"<m{i}@x>") for i in range(1, 20)]}
    dst = FakeIMAPClient(folders={"INBOX": []})
    gate = threading.Event()
    real_append = dst.append
    dst.append = lambda f, b, flags=(), msg_time=None: (gate.wait(timeout=5), real_append(f, b, flags=flags, msg_time=msg_time))[1]
    run = build_run(monkeypatch, tmp_path, src_data, dst)
    run.start()
    import time as _t
    _t.sleep(0.05)
    run.cancel()
    run.pause()  # pause() must no-op: status is no longer "running" (it's running+stop_requested, but pause guards on _status=="running")
    gate.set()
    run.join(timeout=10)
    assert run.snapshot().status == "cancelled"  # not flipped to paused
