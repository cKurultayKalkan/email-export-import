"""LocalBackend: the in-process backend that presents the DaemonBackend
interface over a live RunManager + Controller. Exercised headless with the
in-memory IMAP fake — no network, no daemon."""
import time

import pytest

from email_export_import.errors import AuthFailed
from email_export_import.gui.controller import Controller
from email_export_import.gui.local_backend import LocalBackend
from email_export_import.gui.run_manager import RunManager
from email_export_import.state import MigrationState


@pytest.fixture
def backend(tmp_path):
    manager = RunManager(state_dir=tmp_path)
    controller = Controller(state_dir=tmp_path)
    return LocalBackend(manager, controller), manager, tmp_path


def _wire_imap(monkeypatch, src, dst):
    """Point connection.IMAPClient at the fakes, routed by host.

    Note we deliberately do NOT patch connection.time.sleep: `connection.time`
    IS the global time module, so patching it would also neuter the poll loop's
    own time.sleep, starving the in-process worker thread of scheduling. The
    happy path and a first-time auth rejection never sleep, so there is nothing
    to patch away."""
    from email_export_import import connection

    monkeypatch.setattr(
        connection, "IMAPClient",
        lambda host, port=993, ssl=True, **kw: src if host == "src.test" else dst,
    )


def test_connection_ok_and_auth_failure(backend, monkeypatch):
    from email_export_import import connection
    from tests.fakes import FakeIMAPClient

    b, _manager, _ = backend
    good = FakeIMAPClient(folders={"INBOX": []})
    _wire_imap(monkeypatch, good, good)

    assert b.test_connection(
        {"host": "src.test", "email": "a@x", "password": "p"}) == {"ok": True}

    bad = FakeIMAPClient(folders={"INBOX": []})
    bad.login_error = AuthFailed("bad creds")
    monkeypatch.setattr(
        connection, "IMAPClient",
        lambda host, port=993, ssl=True, **kw: bad,
    )
    res = b.test_connection({"host": "any", "email": "a@x", "password": "wrong"})
    assert res["ok"] is False and res["kind"] == "auth"


def test_plan_and_start_run_to_done(backend, monkeypatch):
    from tests.fakes import FakeIMAPClient, make_message

    b, manager, _ = backend
    src = FakeIMAPClient(folders={"INBOX": [make_message(uid=1, message_id="<m1@x>")]})
    dst = FakeIMAPClient(folders={"INBOX": []})
    _wire_imap(monkeypatch, src, dst)

    plan = b.plan(
        {"host": "src.test", "email": "a@x", "password": "p"},
        {"host": "dst.test", "email": "b@y", "password": "p"}, skip=[])
    assert plan["plan_id"] and plan["total"] == 1
    assert plan["folders"] == [{"source": "INBOX", "dest": "INBOX", "count": 1}]

    key = b.start(plan["plan_id"], skip=[], workers=1)
    for _ in range(200):
        s = next((x for x in manager.snapshot_all() if x.key == key), None)
        if s and s.status in ("done", "error"):
            break
        time.sleep(0.02)
    s = next(x for x in b.refresh() if x.key == key)
    assert s.status == "done" and s.processed == 1


def test_settings_write_through(backend):
    b, manager, _ = backend
    b.set_workers(8)
    assert manager.workers == 8 and b.workers == 8


def test_cancel_and_remove(backend, tmp_path):
    b, manager, _ = backend
    s = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)
    s.set_config({"src": {"email": "a@x"}, "dst": {"email": "b@y"}, "total": 3})
    s.flush()
    manager.load_resumable()
    key = manager.snapshot_all()[0].key

    b.cancel(key)
    assert manager.get(key).snapshot().status == "cancelled"
    b.remove(key)
    assert manager.snapshot_all() == []


def test_add_placeholder_creates_queued_card(backend):
    b, manager, _ = backend
    key = b.add_placeholder("a@x", "b@y")
    assert key == "a@x__b@y"
    snaps = manager.snapshot_all()
    assert [s.key for s in snaps] == [key]
    # Idempotent: calling again does not add a second card for the pair.
    assert b.add_placeholder("a@x", "b@y") == key
    assert len(manager.snapshot_all()) == 1


def test_config_for_returns_run_config(backend, tmp_path):
    b, manager, _ = backend
    s = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)
    s.set_config({"src": {"email": "a@x", "host": "sh"},
                  "dst": {"email": "b@y"}, "total": 3})
    s.flush()
    manager.load_resumable()
    key = manager.snapshot_all()[0].key

    cfg = b.config_for(key)
    assert cfg["src"]["host"] == "sh"
