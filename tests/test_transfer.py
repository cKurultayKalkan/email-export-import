from datetime import datetime

import pytest
from imapclient.exceptions import IMAPClientError

from email_export_import import connection
from email_export_import.connection import MailConnection
from email_export_import.errors import QuotaExceeded
from email_export_import.models import Account, FolderPlan
from email_export_import.state import MigrationState
from email_export_import.transfer import (
    is_quota_error,
    migrate,
    parse_message_id,
    preserved_flags,
)
from tests.fakes import FakeIMAPClient, make_message

SRC = Account(host="src.test", port=993, ssl=True, email="a@x", password="p")
DST = Account(host="dst.test", port=993, ssl=True, email="b@y", password="p")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(connection.time, "sleep", lambda s: None)


def make_conns(monkeypatch, src_fake, dst_fake):
    def factory(host, port=993, ssl=True):
        return src_fake if host == "src.test" else dst_fake

    monkeypatch.setattr(connection, "IMAPClient", factory)
    return MailConnection(SRC), MailConnection(DST)


def test_parse_message_id():
    assert parse_message_id(b"Message-ID: <a@x>\r\n\r\n") == "<a@x>"
    assert parse_message_id(b"\r\n") is None
    assert parse_message_id(None) is None


def test_preserved_flags_filters_recent_and_unknown():
    got = preserved_flags((b"\\Seen", b"\\Recent", b"\\Flagged", b"$Custom"))
    assert got == (b"\\Seen", b"\\Flagged")


def test_is_quota_error():
    assert is_quota_error(IMAPClientError("APPEND failed: [OVERQUOTA] full"))
    assert is_quota_error(RuntimeError("Quota exceeded"))
    assert not is_quota_error(RuntimeError("parse error"))


def test_roundtrip_preserves_body_flags_date(monkeypatch, tmp_path):
    when = datetime(2019, 5, 5, 12, 0, 0)
    msg = make_message(uid=1, message_id="<m1@x>", flags=(b"\\Seen", b"\\Recent"),
                       internaldate=when)
    src_fake = FakeIMAPClient(folders={"INBOX": [msg]})
    dst_fake = FakeIMAPClient(folders={"INBOX": []})
    src, dst = make_conns(monkeypatch, src_fake, dst_fake)
    state = MigrationState(tmp_path / "s.json")

    progress = migrate(src, dst, [FolderPlan("INBOX", "INBOX", create=False)], state)

    assert (progress.migrated, progress.skipped, progress.failed) == (1, 0, 0)
    stored = dst_fake.folders["INBOX"][0]
    assert stored["body"] == msg["body"]          # raw copy, byte-identical
    assert stored["flags"] == (b"\\Seen",)         # \Recent dropped
    assert stored["internaldate"] == when


def test_rerun_skips_everything(monkeypatch, tmp_path):
    msgs = [make_message(uid=i, message_id=f"<m{i}@x>") for i in (1, 2)]
    src_fake = FakeIMAPClient(folders={"INBOX": msgs})
    dst_fake = FakeIMAPClient(folders={"INBOX": []})
    src, dst = make_conns(monkeypatch, src_fake, dst_fake)
    plans = [FolderPlan("INBOX", "INBOX", create=False)]

    state = MigrationState(tmp_path / "s.json")
    first = migrate(src, dst, plans, state)
    assert first.migrated == 2

    state2 = MigrationState(tmp_path / "s.json")  # fresh load, same file
    second = migrate(src, dst, plans, state2)
    assert (second.migrated, second.skipped) == (0, 2)
    assert len(dst_fake.folders["INBOX"]) == 2  # no duplicates


def test_message_without_id_uses_uid_dedup(monkeypatch, tmp_path):
    msg = make_message(uid=42, message_id=None)
    src_fake = FakeIMAPClient(folders={"INBOX": [msg]})
    dst_fake = FakeIMAPClient(folders={"INBOX": []})
    src, dst = make_conns(monkeypatch, src_fake, dst_fake)
    plans = [FolderPlan("INBOX", "INBOX", create=False)]

    state = MigrationState(tmp_path / "s.json")
    assert migrate(src, dst, plans, state).migrated == 1
    state2 = MigrationState(tmp_path / "s.json")
    assert migrate(src, dst, plans, state2).skipped == 1


def test_missing_dest_folder_is_created(monkeypatch, tmp_path):
    src_fake = FakeIMAPClient(folders={"Work/P": [make_message(uid=1, message_id="<a@x>")]})
    dst_fake = FakeIMAPClient(folders={"INBOX": []}, delimiter=".")
    src, dst = make_conns(monkeypatch, src_fake, dst_fake)
    state = MigrationState(tmp_path / "s.json")

    migrate(src, dst, [FolderPlan("Work/P", "Work.P", create=True)], state)
    assert dst_fake.folder_exists("Work.P")
    assert len(dst_fake.folders["Work.P"]) == 1


def test_single_bad_message_does_not_kill_run(monkeypatch, tmp_path):
    msgs = [make_message(uid=1, message_id="<a@x>"), make_message(uid=2, message_id="<b@x>")]
    src_fake = FakeIMAPClient(folders={"INBOX": msgs})
    dst_fake = FakeIMAPClient(folders={"INBOX": []})

    real_append = dst_fake.append
    def flaky_append(folder, body, flags=(), msg_time=None):
        if b"<a@x>" in body:
            raise IMAPClientError("message rejected")
        return real_append(folder, body, flags=flags, msg_time=msg_time)
    dst_fake.append = flaky_append

    src, dst = make_conns(monkeypatch, src_fake, dst_fake)
    state = MigrationState(tmp_path / "s.json")
    progress = migrate(src, dst, [FolderPlan("INBOX", "INBOX", create=False)], state)

    assert progress.migrated == 1
    assert progress.failed == 1
    assert "<a@x>" in progress.failures[0]


def test_quota_error_aborts_run(monkeypatch, tmp_path):
    msgs = [make_message(uid=i, message_id=f"<m{i}@x>") for i in (1, 2, 3)]
    src_fake = FakeIMAPClient(folders={"INBOX": msgs})
    dst_fake = FakeIMAPClient(folders={"INBOX": []})
    dst_fake.append_error = IMAPClientError("APPEND failed [OVERQUOTA]")
    src, dst = make_conns(monkeypatch, src_fake, dst_fake)
    state = MigrationState(tmp_path / "s.json")

    with pytest.raises(QuotaExceeded):
        migrate(src, dst, [FolderPlan("INBOX", "INBOX", create=False)], state)


def test_on_message_fires_for_every_processed_message(monkeypatch, tmp_path):
    msgs = [make_message(uid=i, message_id=f"<m{i}@x>") for i in (1, 2)]
    src_fake = FakeIMAPClient(folders={"INBOX": msgs})
    dst_fake = FakeIMAPClient(folders={"INBOX": []})
    src, dst = make_conns(monkeypatch, src_fake, dst_fake)
    state = MigrationState(tmp_path / "s.json")

    seen = []
    migrate(src, dst, [FolderPlan("INBOX", "INBOX", create=False)], state,
            on_message=lambda folder, uid: seen.append((folder, uid)))
    assert seen == [("INBOX", 1), ("INBOX", 2)]


def test_source_fetch_error_mentioning_exceeded_is_not_quota_abort(monkeypatch, tmp_path):
    msgs = [make_message(uid=1, message_id="<a@x>"), make_message(uid=2, message_id="<b@x>")]
    src_fake = FakeIMAPClient(folders={"INBOX": msgs})
    dst_fake = FakeIMAPClient(folders={"INBOX": []})
    real_fetch = src_fake.fetch
    def flaky_fetch(uids, data):
        if data == [b"BODY.PEEK[]"] and uids == [1]:
            raise IMAPClientError("fetch limit exceeded")
        return real_fetch(uids, data)
    src_fake.fetch = flaky_fetch
    src, dst = make_conns(monkeypatch, src_fake, dst_fake)
    state = MigrationState(tmp_path / "s.json")
    progress = migrate(src, dst, [FolderPlan("INBOX", "INBOX", create=False)], state)
    assert progress.failed == 1
    assert progress.migrated == 1


def test_flush_failure_after_append_propagates_not_double_counts(monkeypatch, tmp_path):
    msg = make_message(uid=1, message_id="<a@x>")
    src_fake = FakeIMAPClient(folders={"INBOX": [msg]})
    dst_fake = FakeIMAPClient(folders={"INBOX": []})
    src, dst = make_conns(monkeypatch, src_fake, dst_fake)
    state = MigrationState(tmp_path / "s.json")
    real_flush = MigrationState.flush
    calls = {"n": 0}
    def flaky_flush(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("No space left on device")
        return real_flush(self)
    monkeypatch.setattr(MigrationState, "flush", flaky_flush)
    with pytest.raises(OSError):
        migrate(src, dst, [FolderPlan("INBOX", "INBOX", create=False)], state)
    assert len(dst_fake.folders["INBOX"]) == 1  # appended once, not retried


def test_vanished_uid_counts_skipped_and_fires_on_message(monkeypatch, tmp_path):
    msg = make_message(uid=1, message_id="<a@x>")
    src_fake = FakeIMAPClient(folders={"INBOX": [msg]})
    dst_fake = FakeIMAPClient(folders={"INBOX": []})
    src_fake.search = lambda criteria="ALL": [1, 99]  # 99 expunged after search
    src, dst = make_conns(monkeypatch, src_fake, dst_fake)
    state = MigrationState(tmp_path / "s.json")
    seen = []
    progress = migrate(src, dst, [FolderPlan("INBOX", "INBOX", create=False)], state,
                       on_message=lambda folder, uid: seen.append(uid))
    assert progress.migrated == 1
    assert progress.skipped == 1
    assert seen == [1, 99]
