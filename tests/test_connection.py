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

    def factory(host, port=993, ssl=True):
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


def test_close_is_idempotent(monkeypatch):
    fake = FakeIMAPClient()
    install_factory(monkeypatch, [fake])
    conn = MailConnection(ACCOUNT)
    conn.connect()
    conn.close()
    conn.close()
    assert not fake.logged_in
