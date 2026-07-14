"""Daemon server + client round-trip over a real loopback socket.

The daemon owns the RunManager in its own process so migrations outlive the
GUI; the GUI talks to it through DaemonClient. Here both live in-process but
communicate over a real 127.0.0.1 socket, exercising the HTTP + token layer.
"""
import json
import time

import pytest

pytest.importorskip("email_export_import.daemon.server")

from email_export_import.daemon.server import DaemonServer  # noqa: E402
from email_export_import.daemon.client import DaemonClient, DaemonError  # noqa: E402
from email_export_import.gui.run_manager import Run, RunManager  # noqa: E402
from email_export_import.state import MigrationState  # noqa: E402


@pytest.fixture
def daemon(tmp_path):
    manager = RunManager(state_dir=tmp_path)
    server = DaemonServer(manager, host="127.0.0.1", port=0, token="secret-token")
    server.start()
    try:
        yield server, manager
    finally:
        server.stop()


def _client(server, token="secret-token"):
    return DaemonClient(f"http://127.0.0.1:{server.port}", token=token)


def _completed_state(tmp_path, src="a@x", dst="b@y", total=5):
    s = MigrationState.for_pair(src, dst, base_dir=tmp_path)
    s.set_config({"src": {"email": src, "host": "h"},
                  "dst": {"email": dst, "host": "h2"}, "total": total})
    s.mark_migrated("INBOX", "<m@x>", 1)
    s.mark_completed()
    s.flush()


def test_ping_reports_version(daemon):
    server, _ = daemon
    from email_export_import import __version__

    assert _client(server).ping()["version"] == __version__


def test_runs_round_trip(daemon, tmp_path):
    server, manager = daemon
    _completed_state(tmp_path)
    manager.load_completed()

    runs = _client(server).runs()
    assert len(runs) == 1
    assert runs[0]["status"] == "done"
    assert runs[0]["total"] == 5


def test_bad_token_is_rejected(daemon):
    server, _ = daemon
    with pytest.raises(DaemonError):
        _client(server, token="wrong").runs()


def test_control_actions_reach_the_manager(daemon, tmp_path, monkeypatch):
    server, manager = daemon
    # a paused placeholder run: cancel must flip it to cancelled through HTTP
    s = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)
    s.set_config({"src": {"email": "a@x"}, "dst": {"email": "b@y"}, "total": 3})
    s.flush()
    manager.load_resumable()
    key = manager.snapshot_all()[0].key

    _client(server).cancel(key)
    assert manager.get(key).snapshot().status == "cancelled"


def test_dismiss_removes_a_run(daemon, tmp_path):
    server, manager = daemon
    _completed_state(tmp_path)
    manager.load_completed()
    key = manager.snapshot_all()[0].key

    _client(server).dismiss(key)
    assert manager.snapshot_all() == []


def test_settings_round_trip(daemon):
    server, manager = daemon
    c = _client(server)
    c.set_settings({"max_active": 3, "workers": 8, "rate_limit": 1048576})
    assert manager.max_active == 3
    assert manager.workers == 8
    assert c.get_settings()["workers"] == 8


def test_rendezvous_file_is_written_and_private(tmp_path, monkeypatch):
    # The GUI finds the daemon by reading {port, token, pid} from a 0600 file
    # in the state dir. Drive main() briefly on a thread, then read it back.
    import threading
    import time

    from email_export_import.daemon import __main__ as dm

    t = threading.Thread(target=lambda: dm.main(base_dir=tmp_path), daemon=True)
    t.start()
    path = dm.rendezvous_path(tmp_path)
    for _ in range(100):
        if path.exists():
            break
        time.sleep(0.02)
    assert path.exists(), "daemon never wrote its rendezvous file"
    assert (path.stat().st_mode & 0o777) == 0o600  # no other user may read it

    info = json.loads(path.read_text())
    assert set(info) == {"port", "token", "pid"}

    # a client built from the file can reach the live daemon
    c = DaemonClient(f"http://127.0.0.1:{info['port']}", token=info["token"])
    assert c.is_alive()
    c.shutdown()


def test_plan_then_start_runs_a_migration(daemon, tmp_path, monkeypatch):
    # The daemon owns the whole connect -> plan -> start flow: the GUI posts
    # credentials (held in memory only), gets a folder plan back keyed by a
    # plan id, then starts the run by that id.
    from email_export_import import connection
    from tests.fakes import FakeIMAPClient, make_message

    server, manager = daemon
    src = FakeIMAPClient(folders={"INBOX": [make_message(uid=1, message_id="<m1@x>")]})
    dst = FakeIMAPClient(folders={"INBOX": []})
    monkeypatch.setattr(connection.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        connection, "IMAPClient",
        lambda host, port=993, ssl=True, **kw: src if host == "src.test" else dst,
    )

    c = _client(server)
    plan = c.plan(
        {"host": "src.test", "port": 993, "ssl": True, "verify_ssl": True,
         "email": "a@x", "password": "p"},
        {"host": "dst.test", "port": 993, "ssl": True, "verify_ssl": True,
         "email": "b@y", "password": "p"},
        skip=[],
    )
    assert plan["total"] == 1
    assert "plan_id" in plan

    started = c.start(plan["plan_id"], skip=[], workers=1, spool=False)
    key = started["key"]
    for _ in range(200):
        s = next((r for r in c.runs() if r["key"] == key), None)
        if s and s["status"] in ("done", "error"):
            break
        time.sleep(0.02)
    s = next(r for r in c.runs() if r["key"] == key)
    assert s["status"] == "done"
    assert s["processed"] == 1


def test_plan_reports_a_connection_failure(daemon, monkeypatch):
    from email_export_import import connection
    from tests.fakes import FakeIMAPClient

    server, _ = daemon
    bad = FakeIMAPClient()
    bad.login_error = __import__("imapclient").exceptions.LoginError("AUTHENTICATIONFAILED")
    monkeypatch.setattr(connection.time, "sleep", lambda s: None)
    monkeypatch.setattr(connection, "IMAPClient",
                        lambda host, port=993, ssl=True, **kw: bad)

    with pytest.raises(DaemonError):
        _client(server).plan(
            {"host": "src.test", "email": "a@x", "password": "p"},
            {"host": "dst.test", "email": "b@y", "password": "p"},
            skip=[],
        )


def test_test_connection_endpoint(daemon, monkeypatch):
    from email_export_import import connection
    from tests.fakes import FakeIMAPClient

    server, _ = daemon
    ok_fake = FakeIMAPClient()
    monkeypatch.setattr(connection.time, "sleep", lambda s: None)
    monkeypatch.setattr(connection, "IMAPClient",
                        lambda host, port=993, ssl=True, **kw: ok_fake)
    res = _client(server).test_connection(
        {"host": "h.test", "port": 993, "ssl": True, "email": "a@x", "password": "p"})
    assert res["ok"] is True

    bad = FakeIMAPClient()
    bad.login_error = __import__("imapclient").exceptions.LoginError("AUTHENTICATIONFAILED")
    monkeypatch.setattr(connection, "IMAPClient",
                        lambda host, port=993, ssl=True, **kw: bad)
    res = _client(server).test_connection(
        {"host": "h.test", "email": "a@x", "password": "wrong"})
    assert res["ok"] is False and res["kind"] == "auth"
