"""Spec integration scenarios (design doc §Testing) against FakeIMAPClient."""
from datetime import datetime

import pytest
from imapclient.exceptions import IMAPClientError

from email_export_import import connection
from email_export_import.connection import MailConnection
from email_export_import.errors import QuotaExceeded
from email_export_import.folders import build_folder_plan
from email_export_import.models import Account
from email_export_import.state import MigrationState
from email_export_import.transfer import migrate
from tests.fakes import FakeIMAPClient, make_message

SRC = Account(host="src.test", port=993, ssl=True, email="a@x", password="p")
DST = Account(host="dst.test", port=993, ssl=True, email="b@y", password="p")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(connection.time, "sleep", lambda s: None)


def make_conns(monkeypatch, src_fake, dst_fake):
    monkeypatch.setattr(
        connection, "IMAPClient",
        lambda host, port=993, ssl=True, **kwargs: src_fake if host == "src.test" else dst_fake,
    )
    return MailConnection(SRC), MailConnection(DST)


def test_attachment_survives_byte_identical(monkeypatch, tmp_path):
    payload = bytes(range(256)) * 100  # 25.6 KB binary attachment
    msg = make_message(uid=1, message_id="<att@x>", attachment=payload)
    src_fake = FakeIMAPClient(folders={"INBOX": [msg]})
    dst_fake = FakeIMAPClient(folders={"INBOX": []})
    src, dst = make_conns(monkeypatch, src_fake, dst_fake)

    plans = build_folder_plan(src_fake.list_folders(), dst_fake.list_folders())
    migrate(src, dst, plans, MigrationState(tmp_path / "s.json"))

    assert dst_fake.folders["INBOX"][0]["body"] == msg["body"]


def test_interrupted_run_resumes_without_duplicates(monkeypatch, tmp_path):
    msgs = [make_message(uid=i, message_id=f"<m{i}@x>") for i in range(1, 6)]
    src_fake = FakeIMAPClient(folders={"INBOX": msgs})
    dst_fake = FakeIMAPClient(folders={"INBOX": []})
    src, dst = make_conns(monkeypatch, src_fake, dst_fake)
    plans = build_folder_plan(src_fake.list_folders(), dst_fake.list_folders())
    state_path = tmp_path / "s.json"

    # First run dies at message 3 (quota simulates any mid-run abort: the
    # engine flushes state before raising). Failure must be persistent —
    # with_retry re-attempts APPEND, so a fail-once fake would succeed on
    # the retry and the run would never abort.
    real_append = dst_fake.append
    calls = {"n": 0}
    def dying_append(folder, body, flags=(), msg_time=None):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise IMAPClientError("OVERQUOTA")
        return real_append(folder, body, flags=flags, msg_time=msg_time)
    dst_fake.append = dying_append

    with pytest.raises(QuotaExceeded):
        migrate(src, dst, plans, MigrationState(state_path))
    assert len(dst_fake.folders["INBOX"]) == 2

    # Second run: obstacle gone, resume completes the rest exactly once.
    dst_fake.append = real_append
    result = migrate(src, dst, plans, MigrationState(state_path))
    assert result.migrated == 3
    assert result.skipped == 2
    assert len(dst_fake.folders["INBOX"]) == 5
    ids = sorted(
        m["body"].split(b"Message-ID: ")[1].split(b"\r\n")[0]
        for m in dst_fake.folders["INBOX"]
    )
    assert ids == [f"<m{i}@x>".encode() for i in range(1, 6)]


def test_cross_delimiter_migration_full_stack(monkeypatch, tmp_path):
    when = datetime(2018, 3, 3, 9, 30)
    src_fake = FakeIMAPClient(
        folders={
            "INBOX": [make_message(uid=1, message_id="<i@x>", flags=(b"\\Seen",), internaldate=when)],
            "Work/Projects": [make_message(uid=2, message_id="<w@x>")],
            "Sent Messages": [make_message(uid=3, message_id="<s@x>")],
        },
        special_use={"Sent Messages": b"\\Sent"},
        delimiter="/",
    )
    dst_fake = FakeIMAPClient(
        folders={"INBOX": [], "Gesendet": []},
        special_use={"Gesendet": b"\\Sent"},
        delimiter=".",
    )
    src, dst = make_conns(monkeypatch, src_fake, dst_fake)

    plans = build_folder_plan(src_fake.list_folders(), dst_fake.list_folders())
    result = migrate(src, dst, plans, MigrationState(tmp_path / "s.json"))

    assert result.migrated == 3
    assert len(dst_fake.folders["INBOX"]) == 1
    assert dst_fake.folders["INBOX"][0]["flags"] == (b"\\Seen",)
    assert dst_fake.folders["INBOX"][0]["internaldate"] == when
    assert len(dst_fake.folders["Work.Projects"]) == 1   # delimiter translated
    assert len(dst_fake.folders["Gesendet"]) == 1        # SPECIAL-USE matched
    assert "Sent Messages" not in dst_fake.folders       # no duplicate sent folder
