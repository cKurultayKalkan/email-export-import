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
