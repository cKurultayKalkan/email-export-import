from datetime import datetime

from email_export_import.spool import MessageSpool


def test_put_get_roundtrip(tmp_path):
    spool = MessageSpool(tmp_path / "spool")
    when = datetime(2021, 3, 4, 5, 6, 7)
    spool.put("INBOX", 7, "<m@x>", b"raw body", (b"\\Seen",), when)

    got = spool.get("INBOX", 7, "<m@x>")
    assert got is not None
    assert got.body == b"raw body"
    assert got.flags == (b"\\Seen",)
    assert got.internaldate == when


def test_get_missing_returns_none(tmp_path):
    spool = MessageSpool(tmp_path / "spool")
    assert spool.get("INBOX", 1, "<m@x>") is None


def test_get_with_different_message_id_returns_none(tmp_path):
    """UIDs are only unique per UIDVALIDITY generation — a spooled body is
    only trusted when its recorded Message-ID matches the live one."""
    spool = MessageSpool(tmp_path / "spool")
    spool.put("INBOX", 7, "<old@x>", b"old body", (), None)
    assert spool.get("INBOX", 7, "<new@x>") is None


def test_get_corrupt_meta_returns_none(tmp_path):
    spool = MessageSpool(tmp_path / "spool")
    spool.put("INBOX", 7, "<m@x>", b"body", (), None)
    for meta in (tmp_path / "spool").glob("*/*.json"):
        meta.write_text("{not json")
    assert spool.get("INBOX", 7, "<m@x>") is None


def test_discard_and_pending_count(tmp_path):
    spool = MessageSpool(tmp_path / "spool")
    spool.put("INBOX", 1, "<a@x>", b"a", (), None)
    spool.put("Gelen Kutusu/Alt.Klasör", 2, "<b@x>", b"b", (), None)
    assert spool.pending_count() == 2
    spool.discard("INBOX", 1)
    assert spool.pending_count() == 1
    spool.discard("INBOX", 1)  # idempotent
    assert spool.pending_count() == 1


def test_none_message_id_roundtrip(tmp_path):
    spool = MessageSpool(tmp_path / "spool")
    spool.put("INBOX", 3, None, b"body", (), None)
    got = spool.get("INBOX", 3, None)
    assert got is not None
    assert got.body == b"body"
    assert got.internaldate is None
