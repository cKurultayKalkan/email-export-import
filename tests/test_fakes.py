from datetime import datetime

from tests.fakes import FakeIMAPClient, make_message


def test_fetch_returns_imapclient_shaped_metadata():
    msg = make_message(uid=1, message_id="<a@x>", flags=(b"\\Seen",),
                       internaldate=datetime(2020, 1, 2, 3, 4, 5))
    fake = FakeIMAPClient(folders={"INBOX": [msg]})
    fake.login("u", "p")
    info = fake.select_folder("INBOX")
    assert info[b"UIDVALIDITY"] == 1
    assert fake.search() == [1]

    meta = fake.fetch([1], [b"FLAGS", b"INTERNALDATE", b"RFC822.SIZE",
                           b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"])
    entry = meta[1]
    assert entry[b"FLAGS"] == (b"\\Seen",)
    assert entry[b"INTERNALDATE"] == datetime(2020, 1, 2, 3, 4, 5)
    assert b"<a@x>" in entry[b"BODY[HEADER.FIELDS (MESSAGE-ID)]"]

    body = fake.fetch([1], [b"BODY.PEEK[]"])[1][b"BODY[]"]
    assert b"Message-ID: <a@x>" in body


def test_append_stores_message_with_flags_and_time():
    fake = FakeIMAPClient(folders={"INBOX": []})
    when = datetime(2021, 6, 1)
    fake.append("INBOX", b"raw-message", flags=(b"\\Seen",), msg_time=when)
    fake.select_folder("INBOX")
    uid = fake.search()[0]
    got = fake.fetch([uid], [b"FLAGS", b"INTERNALDATE", b"BODY.PEEK[]"])[uid]
    assert got[b"FLAGS"] == (b"\\Seen",)
    assert got[b"INTERNALDATE"] == when
    assert got[b"BODY[]"] == b"raw-message"


def test_create_folder_and_exists():
    fake = FakeIMAPClient()
    assert not fake.folder_exists("Archive")
    fake.create_folder("Archive")
    assert fake.folder_exists("Archive")


def test_append_error_injection():
    fake = FakeIMAPClient(folders={"INBOX": []})
    fake.append_error = RuntimeError("boom")
    try:
        fake.append("INBOX", b"x")
        raise AssertionError("should have raised")
    except RuntimeError:
        pass


def test_message_without_message_id():
    msg = make_message(uid=5, message_id=None)
    fake = FakeIMAPClient(folders={"INBOX": [msg]})
    fake.select_folder("INBOX")
    blob = fake.fetch([5], [b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"])[5]
    assert b"Message-ID" not in blob[b"BODY[HEADER.FIELDS (MESSAGE-ID)]"]
