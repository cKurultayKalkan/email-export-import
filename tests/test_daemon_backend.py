"""DaemonBackend adapter: reconstructs RunSnapshots from the wire and turns
GUI control calls into daemon HTTP calls. Exercised against a live daemon."""
import time

import pytest

from email_export_import.daemon.server import DaemonServer
from email_export_import.daemon.client import DaemonClient
from email_export_import.gui.daemon_backend import DaemonBackend
from email_export_import.gui.run_manager import RunManager
from email_export_import.state import MigrationState


@pytest.fixture
def backend(tmp_path):
    manager = RunManager(state_dir=tmp_path)
    server = DaemonServer(manager, token="tok")
    server.start()
    client = DaemonClient(f"http://127.0.0.1:{server.port}", token="tok")
    try:
        yield DaemonBackend(client), manager, tmp_path
    finally:
        server.stop()


def _completed(tmp_path, src="a@x", dst="b@y", total=5):
    s = MigrationState.for_pair(src, dst, base_dir=tmp_path)
    s.set_config({"src": {"email": src, "host": "sh", "port": 993},
                  "dst": {"email": dst, "host": "dh", "port": 993}, "total": total})
    s.mark_migrated("INBOX", "<m@x>", 1)
    s.mark_completed()
    s.flush()


def test_refresh_yields_runsnapshots_with_config(backend):
    b, manager, tmp_path = backend
    _completed(tmp_path)
    manager.load_completed()

    snaps = b.refresh()
    assert len(snaps) == 1
    s = snaps[0]
    assert s.status == "done" and s.total == 5
    assert s.processed == 1  # done_uids-based honest count survives the wire
    # config comes across for the side panel
    cfg = b.config_for(s.key)
    assert cfg["src"]["host"] == "sh"


def test_settings_write_through(backend):
    b, manager, _ = backend
    b.set_workers(8)
    b.set_max_active(3)
    assert manager.workers == 8 and manager.max_active == 3
    assert b.workers == 8


def test_poll_events_reflects_show_and_quit(backend):
    b, _, _ = backend
    # Nothing pending.
    assert b.poll_events() == {"show": False, "quit": False}
    # A tray show request (set on the daemon) surfaces once (one-shot).
    b._client.request_show()
    assert b.poll_events() == {"show": True, "quit": False}
    assert b.poll_events() == {"show": False, "quit": False}


def test_poll_events_degrades_when_daemon_unreachable(backend):
    b, _, _ = backend
    # Point the client at a dead port: poll_events must not raise.
    b._client._base = "http://127.0.0.1:1"
    assert b.poll_events() == {"show": False, "quit": False}


def test_cancel_and_dismiss(backend, tmp_path):
    b, manager, _ = backend
    s = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)
    s.set_config({"src": {"email": "a@x"}, "dst": {"email": "b@y"}, "total": 3})
    s.flush()
    manager.load_resumable()
    key = b.refresh()[0].key

    b.cancel(key)
    assert manager.get(key).snapshot().status == "cancelled"
    b.remove(key)
    assert manager.snapshot_all() == []


def test_plan_and_start_through_backend(backend, monkeypatch):
    from email_export_import import connection
    from tests.fakes import FakeIMAPClient, make_message

    b, manager, _ = backend
    src = FakeIMAPClient(folders={"INBOX": [make_message(uid=1, message_id="<m1@x>")]})
    dst = FakeIMAPClient(folders={"INBOX": []})
    monkeypatch.setattr(connection.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        connection, "IMAPClient",
        lambda host, port=993, ssl=True, **kw: src if host == "src.test" else dst,
    )

    plan = b.plan(
        {"host": "src.test", "email": "a@x", "password": "p"},
        {"host": "dst.test", "email": "b@y", "password": "p"}, skip=[])
    key = b.start(plan["plan_id"], skip=[], workers=1)
    for _ in range(200):
        s = next((x for x in b.refresh() if x.key == key), None)
        if s and s.status in ("done", "error"):
            break
        time.sleep(0.02)
    s = next(x for x in b.refresh() if x.key == key)
    assert s.status == "done"
