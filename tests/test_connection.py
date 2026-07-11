import socket

import pytest
from imapclient.exceptions import IMAPClientError, LoginError

from email_export_import import connection
from email_export_import.connection import MailConnection
from email_export_import.errors import AuthFailed, ConnectionFailed
from email_export_import.models import Account
from tests.fakes import FakeIMAPClient

ACCOUNT = Account(host="imap.test", port=993, ssl=True, email="a@x", password="p")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(connection.time, "sleep", lambda s: None)


def install_factory(monkeypatch, clients):
    """Monkeypatch connection.IMAPClient with a factory popping from *clients*."""
    calls = []

    def factory(host, port=993, ssl=True, **kwargs):
        calls.append((host, port, ssl))
        client = clients.pop(0)
        if isinstance(client, Exception):
            raise client
        return client

    monkeypatch.setattr(connection, "IMAPClient", factory)
    return calls


def test_connect_success(monkeypatch):
    fake = FakeIMAPClient()
    install_factory(monkeypatch, [fake])
    conn = MailConnection(ACCOUNT)
    conn.connect()
    assert fake.logged_in


def test_unreachable_host_raises_connection_failed(monkeypatch):
    install_factory(monkeypatch, [socket.gaierror("no such host")])
    conn = MailConnection(ACCOUNT)
    with pytest.raises(ConnectionFailed) as exc:
        conn.connect()
    assert "imap.test" in str(exc.value)


def test_bad_login_raises_auth_failed(monkeypatch):
    fake = FakeIMAPClient()
    fake.login_error = LoginError("AUTHENTICATIONFAILED")
    install_factory(monkeypatch, [fake])
    conn = MailConnection(ACCOUNT)
    with pytest.raises(AuthFailed) as exc:
        conn.connect()
    assert "app password" in str(exc.value)


def test_network_error_during_login_raises_connection_failed(monkeypatch):
    fake = FakeIMAPClient()
    fake.login_error = OSError("connection reset by peer")
    install_factory(monkeypatch, [fake])
    conn = MailConnection(ACCOUNT)
    with pytest.raises(ConnectionFailed) as exc:
        conn.connect()
    assert "imap.test" in str(exc.value)


def test_with_retry_reconnects_and_reselects(monkeypatch):
    broken = FakeIMAPClient(folders={"INBOX": []})
    healthy = FakeIMAPClient(folders={"INBOX": []})
    install_factory(monkeypatch, [broken, healthy])
    conn = MailConnection(ACCOUNT)
    conn.select_folder("INBOX")

    attempts = []

    def flaky(client):
        attempts.append(client)
        if client is broken:
            raise IMAPClientError("connection dropped")
        return "ok"

    assert conn.with_retry(flaky) == "ok"
    assert attempts == [broken, healthy]
    assert healthy.select_calls == ["INBOX"]  # reselected after reconnect


def test_with_retry_gives_up_after_max_retries(monkeypatch):
    fakes = [FakeIMAPClient() for _ in range(4)]
    install_factory(monkeypatch, list(fakes))
    conn = MailConnection(ACCOUNT, max_retries=3)

    def always_fails(client):
        raise IMAPClientError("still broken")

    with pytest.raises(IMAPClientError):
        conn.with_retry(always_fails)


def test_with_retry_retries_login_rejected_on_reconnect(monkeypatch):
    """Servers that rate-limit logins (fail2ban, per-IP caps) reject a
    reconnect's LOGIN even though the password is correct. Once this
    connection has authenticated successfully, a later rejection is
    transient — retry it instead of failing the message."""
    healthy = FakeIMAPClient(folders={"INBOX": []})
    rejecting = FakeIMAPClient()
    rejecting.login_error = LoginError("too many connections")
    recovered = FakeIMAPClient(folders={"INBOX": []})
    install_factory(monkeypatch, [healthy, rejecting, recovered])
    conn = MailConnection(ACCOUNT)
    conn.select_folder("INBOX")

    attempts = []

    def flaky(client):
        attempts.append(client)
        if client is healthy:
            raise IMAPClientError("connection dropped")
        return "ok"

    assert conn.with_retry(flaky) == "ok"
    assert attempts == [healthy, recovered]
    assert recovered.select_calls == ["INBOX"]


def test_reconnect_login_rejections_use_long_backoff_then_give_up(monkeypatch):
    """fail2ban-style login bans last minutes, far longer than the network
    backoff — auth retries wait on their own long schedule, then give up."""
    sleeps: list[float] = []
    monkeypatch.setattr(connection.time, "sleep", lambda s: sleeps.append(s))
    healthy = FakeIMAPClient(folders={"INBOX": []})
    rejecting = []
    for _ in range(len(connection.AUTH_RETRY_SLEEPS) + 1):
        c = FakeIMAPClient()
        c.login_error = LoginError("too many connections")
        rejecting.append(c)
    install_factory(monkeypatch, [healthy] + rejecting)
    conn = MailConnection(ACCOUNT)

    def drop_once(client):
        if client is healthy:
            raise IMAPClientError("connection dropped")
        return "ok"

    with pytest.raises(AuthFailed):
        conn.with_retry(drop_once)
    # One short network sleep, then the dedicated auth schedule.
    assert sleeps[0] <= 8
    assert sleeps[1:] == list(connection.AUTH_RETRY_SLEEPS)


def test_with_retry_retries_connection_failed_on_reconnect(monkeypatch):
    """A reconnect that cannot even reach the server (ConnectionFailed) is
    as transient as a dropped command — retry it, don't kill the worker."""
    healthy = FakeIMAPClient(folders={"INBOX": []})
    recovered = FakeIMAPClient(folders={"INBOX": []})
    install_factory(
        monkeypatch, [healthy, socket.gaierror("temporary failure"), recovered]
    )
    conn = MailConnection(ACCOUNT)
    conn.select_folder("INBOX")

    def drop_once(client):
        if client is healthy:
            raise IMAPClientError("connection dropped")
        return "ok"

    assert conn.with_retry(drop_once) == "ok"
    assert recovered.select_calls == ["INBOX"]


def test_with_retry_does_not_retry_first_login_rejection(monkeypatch):
    """A rejection on the very first login means bad credentials —
    surface it immediately, no retries."""
    rejecting = FakeIMAPClient()
    rejecting.login_error = LoginError("AUTHENTICATIONFAILED")
    install_factory(monkeypatch, [rejecting])
    conn = MailConnection(ACCOUNT)
    with pytest.raises(AuthFailed):
        conn.with_retry(lambda c: "ok")


def test_generic_imap_error_during_login_raises_auth_failed(monkeypatch):
    fake = FakeIMAPClient()
    fake.login_error = IMAPClientError("BAD unexpected command")
    install_factory(monkeypatch, [fake])
    conn = MailConnection(ACCOUNT)
    with pytest.raises(AuthFailed):
        conn.connect()


def test_close_is_idempotent(monkeypatch):
    fake = FakeIMAPClient()
    install_factory(monkeypatch, [fake])
    conn = MailConnection(ACCOUNT)
    conn.connect()
    conn.close()
    conn.close()
    assert not fake.logged_in


def test_cert_verify_failure_raises_specific_error(monkeypatch):
    import ssl as ssl_mod

    from email_export_import.errors import CertificateVerifyFailed

    install_factory(
        monkeypatch,
        [ssl_mod.SSLCertVerificationError(1, "certificate verify failed: self-signed certificate")],
    )
    conn = MailConnection(ACCOUNT)
    with pytest.raises(CertificateVerifyFailed) as exc:
        conn.connect()
    assert "imap.test" in str(exc.value)


def test_no_verify_ssl_uses_relaxed_context(monkeypatch):
    import ssl as ssl_mod

    fake = FakeIMAPClient()
    captured = {}

    def factory(host, port=993, ssl=True, **kwargs):
        captured.update(kwargs)
        return fake

    monkeypatch.setattr(connection, "IMAPClient", factory)
    account = Account(
        host="imap.test", port=993, ssl=True, email="a@x", password="p", verify_ssl=False
    )
    MailConnection(account).connect()
    ctx = captured["ssl_context"]
    assert ctx.verify_mode == ssl_mod.CERT_NONE
    assert ctx.check_hostname is False


def test_verify_ssl_default_passes_no_custom_context(monkeypatch):
    fake = FakeIMAPClient()
    captured = {}

    def factory(host, port=993, ssl=True, **kwargs):
        captured.update(kwargs)
        return fake

    monkeypatch.setattr(connection, "IMAPClient", factory)
    MailConnection(ACCOUNT).connect()
    assert "ssl_context" not in captured


def test_cancel_event_interrupts_retry_backoff(monkeypatch):
    import threading
    import time as time_module

    fake_broken = FakeIMAPClient()
    install_factory(monkeypatch, [fake_broken] + [FakeIMAPClient() for _ in range(5)])
    cancel = threading.Event()
    conn = MailConnection(ACCOUNT, cancel=cancel)

    def always_fails(client):
        cancel.set()  # cancelled while the retry backoff would sleep
        raise IMAPClientError("still broken")

    start = time_module.monotonic()
    with pytest.raises(IMAPClientError):
        conn.with_retry(always_fails)
    assert time_module.monotonic() - start < 2  # no multi-second sleep happened


def test_socket_timeout_configured(monkeypatch):
    captured = {}

    def factory(host, port=993, ssl=True, **kwargs):
        captured.update(kwargs)
        return FakeIMAPClient()

    monkeypatch.setattr(connection, "IMAPClient", factory)
    MailConnection(ACCOUNT).connect()
    assert captured["timeout"] == 60


def test_set_cancel_makes_backoff_cancellable(monkeypatch):
    import threading
    import time as time_module

    fake_broken = FakeIMAPClient()
    install_factory(monkeypatch, [fake_broken] + [FakeIMAPClient() for _ in range(5)])
    conn = MailConnection(ACCOUNT)  # built WITHOUT a cancel event
    cancel = threading.Event()
    conn.set_cancel(cancel)  # planning/serial path wires this in

    def always_fails(client):
        cancel.set()
        raise IMAPClientError("still broken")

    start = time_module.monotonic()
    with pytest.raises(IMAPClientError):
        conn.with_retry(always_fails)
    assert time_module.monotonic() - start < 2  # backoff aborted, not slept out


def test_jitter_widens_net_backoff_within_bounds(monkeypatch):
    recorded = []
    monkeypatch.setattr(connection.time, "sleep", lambda s: recorded.append(s))
    install_factory(monkeypatch, [FakeIMAPClient() for _ in range(5)])
    conn = MailConnection(ACCOUNT, jitter=0.5)  # no cancel -> time.sleep path
    calls = {"n": 0}

    def once_fails(client):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IMAPClientError("blip")
        return "ok"

    assert conn.with_retry(once_fails) == "ok"
    assert len(recorded) == 1
    assert 1.0 <= recorded[0] <= 1.5  # base 1s + up to 50% jitter


def test_no_jitter_is_exact(monkeypatch):
    recorded = []
    monkeypatch.setattr(connection.time, "sleep", lambda s: recorded.append(s))
    install_factory(monkeypatch, [FakeIMAPClient() for _ in range(5)])
    conn = MailConnection(ACCOUNT)  # jitter defaults to 0
    calls = {"n": 0}

    def once_fails(client):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IMAPClientError("blip")
        return "ok"

    conn.with_retry(once_fails)
    assert recorded == [1]  # exact, no jitter
