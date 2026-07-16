"""DaemonClient wire details that don't need a live server."""
from email_export_import.daemon.client import DaemonClient, SLOW_OP_TIMEOUT


def test_slow_imap_ops_use_a_generous_timeout(monkeypatch):
    # connect/login/folder-scan can take tens of seconds; a short HTTP timeout
    # would mislabel them "timed out" (and mask the real error). plan and
    # test_connection must pass the long ceiling; the default (5s) would not do.
    c = DaemonClient("http://127.0.0.1:1", token="t")
    seen: list = []
    monkeypatch.setattr(
        c, "_request",
        lambda method, path, body=None, timeout=None:
            seen.append((path, timeout)) or {"key": "k"},
    )

    c.plan({"email": "a"}, {"email": "b"}, [])
    c.test_connection({"email": "a"})

    assert ("/plan", SLOW_OP_TIMEOUT) in seen
    assert ("/test-connection", SLOW_OP_TIMEOUT) in seen
    assert SLOW_OP_TIMEOUT >= 120  # comfortably longer than a slow login+scan
