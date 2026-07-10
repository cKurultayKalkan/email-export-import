import ssl as ssl_mod

import pytest
from imapclient.exceptions import LoginError

from email_export_import import connection
from email_export_import.gui.controller import ConnectionResult, Controller
from email_export_import.models import Account
from email_export_import.state import MigrationState
from tests.fakes import FakeIMAPClient, make_message

ACCOUNT = Account(host="imap.test", port=993, ssl=True, email="a@x", password="p")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(connection.time, "sleep", lambda s: None)


def install(monkeypatch, factory):
    monkeypatch.setattr(connection, "IMAPClient", factory)


def test_list_sessions_delegates_to_state(tmp_path):
    s = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)
    s.set_config({"src": {"host": "h", "email": "a@x"}, "dst": {"host": "h2", "email": "b@y"}})
    s.flush()
    sessions = Controller(state_dir=tmp_path).list_sessions()
    assert len(sessions) == 1
    assert sessions[0].config["src"]["email"] == "a@x"


def test_test_connection_success_returns_live_conn(monkeypatch, tmp_path):
    fake = FakeIMAPClient()
    install(monkeypatch, lambda host, port=993, ssl=True, **kw: fake)
    result = Controller(state_dir=tmp_path).test_connection(ACCOUNT)
    assert result.ok
    assert result.conn is not None
    assert fake.logged_in


def test_test_connection_auth_failure(monkeypatch, tmp_path):
    fake = FakeIMAPClient()
    fake.login_error = LoginError("AUTHENTICATIONFAILED")
    install(monkeypatch, lambda host, port=993, ssl=True, **kw: fake)
    result = Controller(state_dir=tmp_path).test_connection(ACCOUNT)
    assert (result.ok, result.kind) == (False, "auth")
    assert result.message


def test_test_connection_cert_failure(monkeypatch, tmp_path):
    def factory(host, port=993, ssl=True, **kw):
        raise ssl_mod.SSLCertVerificationError(1, "certificate verify failed")

    install(monkeypatch, factory)
    result = Controller(state_dir=tmp_path).test_connection(ACCOUNT)
    assert (result.ok, result.kind) == (False, "cert")


def test_test_connection_network_failure(monkeypatch, tmp_path):
    def factory(host, port=993, ssl=True, **kw):
        raise OSError("no route to host")

    install(monkeypatch, factory)
    result = Controller(state_dir=tmp_path).test_connection(ACCOUNT)
    assert (result.ok, result.kind) == (False, "connection")


def test_build_plan_applies_namespace_and_skip(monkeypatch, tmp_path):
    src = FakeIMAPClient(folders={
        "INBOX": [make_message(uid=1, message_id="<a@x>")],
        "Noise": [make_message(uid=2, message_id="<b@x>")],
        "Work": [make_message(uid=3, message_id="<c@x>")],
    })
    dst = FakeIMAPClient(folders={"INBOX": []}, delimiter=".", namespace_prefix="INBOX.")
    install(monkeypatch, lambda host, port=993, ssl=True, **kw: src if host == "imap.test" else dst)

    c = Controller(state_dir=tmp_path)
    src_conn = c.test_connection(ACCOUNT).conn
    dst_conn = c.test_connection(
        Account(host="dst.test", port=993, ssl=True, email="b@y", password="p")
    ).conn
    plan = c.build_plan(src_conn, dst_conn, skip={"Noise"})
    by_source = {p.source: p for p in plan.plans}
    assert set(by_source) == {"INBOX", "Work"}
    assert by_source["Work"].dest == "INBOX.Work"
    assert plan.counts["INBOX"] == 1
    assert plan.total == 2


def _wire_pair(monkeypatch, src_fake, dst_fake):
    install(
        monkeypatch,
        lambda host, port=993, ssl=True, **kw: src_fake if host == "imap.test" else dst_fake,
    )


def test_runner_completes_and_marks_state(monkeypatch, tmp_path):
    src_fake = FakeIMAPClient(
        folders={"INBOX": [make_message(uid=i, message_id=f"<m{i}@x>") for i in (1, 2, 3)]}
    )
    dst_fake = FakeIMAPClient(folders={"INBOX": []})
    _wire_pair(monkeypatch, src_fake, dst_fake)

    c = Controller(state_dir=tmp_path)
    src_conn = c.test_connection(ACCOUNT).conn
    dst_acc = Account(host="dst.test", port=993, ssl=True, email="b@y", password="p")
    dst_conn = c.test_connection(dst_acc).conn
    plan = c.build_plan(src_conn, dst_conn, skip=set())
    state = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)

    c.start(src_conn, dst_conn, plan.plans, state, workers=1, total=plan.total)
    c.join(timeout=10)

    snap = c.snapshot()
    assert snap.running is False
    assert snap.error_kind is None
    assert snap.result.migrated == 3
    assert snap.processed == 3
    assert len(dst_fake.folders["INBOX"]) == 3
    # Session config saved (no passwords) and marked completed.
    reloaded = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)
    assert reloaded.status == "completed"
    assert "password" not in str(reloaded.config).lower()


def test_runner_cancel_leaves_session_resumable(monkeypatch, tmp_path):
    import threading

    msgs = [make_message(uid=i, message_id=f"<m{i}@x>") for i in range(1, 30)]
    src_fake = FakeIMAPClient(folders={"INBOX": msgs})
    dst_fake = FakeIMAPClient(folders={"INBOX": []})

    gate = threading.Event()
    real_append = dst_fake.append

    def slow_append(folder, body, flags=(), msg_time=None):
        gate.wait(timeout=5)  # hold until the test cancels
        return real_append(folder, body, flags=flags, msg_time=msg_time)

    dst_fake.append = slow_append
    _wire_pair(monkeypatch, src_fake, dst_fake)

    c = Controller(state_dir=tmp_path)
    src_conn = c.test_connection(ACCOUNT).conn
    dst_acc = Account(host="dst.test", port=993, ssl=True, email="b@y", password="p")
    dst_conn = c.test_connection(dst_acc).conn
    plan = c.build_plan(src_conn, dst_conn, skip=set())
    state = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)

    c.start(src_conn, dst_conn, plan.plans, state, workers=1, total=plan.total)
    c.cancel()
    gate.set()  # release the in-flight append
    c.join(timeout=10)

    snap = c.snapshot()
    assert snap.running is False
    assert snap.result.migrated < len(msgs)  # stopped early
    reloaded = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)
    assert reloaded.status == "running"  # still resumable


def test_runner_quota_reports_error(monkeypatch, tmp_path):
    from imapclient.exceptions import IMAPClientError

    src_fake = FakeIMAPClient(
        folders={"INBOX": [make_message(uid=1, message_id="<m1@x>")]}
    )
    dst_fake = FakeIMAPClient(folders={"INBOX": []})
    dst_fake.append_error = IMAPClientError("APPEND failed [OVERQUOTA]")
    _wire_pair(monkeypatch, src_fake, dst_fake)

    c = Controller(state_dir=tmp_path)
    src_conn = c.test_connection(ACCOUNT).conn
    dst_acc = Account(host="dst.test", port=993, ssl=True, email="b@y", password="p")
    dst_conn = c.test_connection(dst_acc).conn
    plan = c.build_plan(src_conn, dst_conn, skip=set())
    state = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)

    c.start(src_conn, dst_conn, plan.plans, state, workers=1, total=plan.total)
    c.join(timeout=10)

    snap = c.snapshot()
    assert snap.error_kind == "quota"
    assert MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path).status == "running"


def test_snapshot_polls_during_live_run(monkeypatch, tmp_path):
    import threading

    msgs = [make_message(uid=i, message_id=f"<m{i}@x>") for i in range(1, 6)]
    src_fake = FakeIMAPClient(folders={"INBOX": msgs})
    dst_fake = FakeIMAPClient(folders={"INBOX": []})

    first_done = threading.Event()
    gate = threading.Event()
    real_append = dst_fake.append
    calls = {"n": 0}

    def gated_append(folder, body, flags=(), msg_time=None):
        calls["n"] += 1
        if calls["n"] == 2:
            first_done.set()
            gate.wait(timeout=5)  # park the worker mid-run
        return real_append(folder, body, flags=flags, msg_time=msg_time)

    dst_fake.append = gated_append
    _wire_pair(monkeypatch, src_fake, dst_fake)

    c = Controller(state_dir=tmp_path)
    src_conn = c.test_connection(ACCOUNT).conn
    dst_acc = Account(host="dst.test", port=993, ssl=True, email="b@y", password="p")
    dst_conn = c.test_connection(dst_acc).conn
    plan = c.build_plan(src_conn, dst_conn, skip=set())
    state = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)

    c.start(src_conn, dst_conn, plan.plans, state, workers=1, total=plan.total)
    assert first_done.wait(timeout=5)
    snap = c.snapshot()  # read WHILE the run thread is alive and mid-message
    assert snap.running is True
    assert snap.processed >= 1
    assert snap.total == 5
    gate.set()
    c.join(timeout=10)
    assert c.snapshot().result.migrated == 5


def test_default_skip_for_gmail_and_custom(tmp_path):
    c = Controller(state_dir=tmp_path)
    assert "[Gmail]/All Mail" in c.default_skip("gmail")
    assert c.default_skip(None) == set()
    assert c.default_skip("nonexistent") == set()


def test_overlapping_start_is_ignored(monkeypatch, tmp_path):
    import threading

    msgs = [make_message(uid=i, message_id=f"<m{i}@x>") for i in range(1, 4)]
    src_fake = FakeIMAPClient(folders={"INBOX": msgs})
    dst_fake = FakeIMAPClient(folders={"INBOX": []})
    gate = threading.Event()
    real_append = dst_fake.append

    def gated_append(folder, body, flags=(), msg_time=None):
        gate.wait(timeout=5)
        return real_append(folder, body, flags=flags, msg_time=msg_time)

    dst_fake.append = gated_append
    _wire_pair(monkeypatch, src_fake, dst_fake)

    c = Controller(state_dir=tmp_path)
    src_conn = c.test_connection(ACCOUNT).conn
    dst_acc = Account(host="dst.test", port=993, ssl=True, email="b@y", password="p")
    dst_conn = c.test_connection(dst_acc).conn
    plan = c.build_plan(src_conn, dst_conn, skip=set())
    state = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)

    c.start(src_conn, dst_conn, plan.plans, state, workers=1, total=plan.total)
    c.start(src_conn, dst_conn, plan.plans, state, workers=1, total=plan.total)  # ignored
    gate.set()
    c.join(timeout=10)
    assert c.snapshot().result.migrated == 3  # ran exactly once


def test_runner_with_parallel_workers(monkeypatch, tmp_path):
    src_data = {
        "A": [make_message(uid=i, message_id=f"<a{i}@x>") for i in range(1, 8)],
        "B": [make_message(uid=i, message_id=f"<b{i}@x>") for i in range(1, 6)],
    }
    dst_fake = FakeIMAPClient(folders={"A": [], "B": []})

    def factory(host, port=993, ssl=True, **kw):
        if host == "imap.test":
            return FakeIMAPClient(folders=src_data)  # fresh session per connection
        return dst_fake

    install(monkeypatch, factory)

    c = Controller(state_dir=tmp_path)
    src_conn = c.test_connection(ACCOUNT).conn
    dst_acc = Account(host="dst.test", port=993, ssl=True, email="b@y", password="p")
    dst_conn = c.test_connection(dst_acc).conn
    plan = c.build_plan(src_conn, dst_conn, skip=set())
    state = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)

    c.start(src_conn, dst_conn, plan.plans, state, workers=3, total=plan.total)
    c.join(timeout=10)

    snap = c.snapshot()
    assert snap.result.migrated == 12
    assert snap.processed == 12
    assert len(dst_fake.folders["A"]) == 7
    assert len(dst_fake.folders["B"]) == 5
