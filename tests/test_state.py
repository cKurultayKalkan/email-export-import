import json

from email_export_import.state import MigrationState


def test_mark_and_lookup_by_message_id(tmp_path):
    s = MigrationState(tmp_path / "s.json")
    assert not s.is_migrated("INBOX", "<a@x>", 1)
    s.mark_migrated("INBOX", "<a@x>", 1)
    assert s.is_migrated("INBOX", "<a@x>", 1)
    # Same Message-ID, different UID: still migrated (dedup is by Message-ID).
    assert s.is_migrated("INBOX", "<a@x>", 999)


def test_mark_and_lookup_by_uid_when_no_message_id(tmp_path):
    s = MigrationState(tmp_path / "s.json")
    s.mark_migrated("INBOX", None, 7)
    assert s.is_migrated("INBOX", None, 7)
    assert not s.is_migrated("INBOX", None, 8)


def test_folders_are_isolated(tmp_path):
    s = MigrationState(tmp_path / "s.json")
    s.mark_migrated("INBOX", "<a@x>", 1)
    assert not s.is_migrated("Sent", "<a@x>", 1)


def test_uidvalidity_change_discards_uids_keeps_message_ids(tmp_path):
    s = MigrationState(tmp_path / "s.json")
    s.set_uidvalidity("INBOX", 100)
    s.mark_migrated("INBOX", "<a@x>", 10)
    s.mark_migrated("INBOX", None, 11)
    s.set_uidvalidity("INBOX", 200)  # server regenerated UIDs
    assert s.is_migrated("INBOX", "<a@x>", 10)  # Message-ID survives
    assert not s.is_migrated("INBOX", None, 11)  # UID entry discarded


def test_same_uidvalidity_keeps_uids(tmp_path):
    s = MigrationState(tmp_path / "s.json")
    s.set_uidvalidity("INBOX", 100)
    s.mark_migrated("INBOX", None, 11)
    s.set_uidvalidity("INBOX", 100)
    assert s.is_migrated("INBOX", None, 11)


def test_flush_and_reload_roundtrip(tmp_path):
    path = tmp_path / "s.json"
    s = MigrationState(path)
    s.set_uidvalidity("INBOX", 100)
    s.mark_migrated("INBOX", "<a@x>", 1)
    s.mark_migrated("INBOX", None, 2)
    s.flush()

    s2 = MigrationState(path)
    assert s2.is_migrated("INBOX", "<a@x>", 1)
    assert s2.is_migrated("INBOX", None, 2)
    s2.set_uidvalidity("INBOX", 100)  # unchanged -> UIDs kept
    assert s2.is_migrated("INBOX", None, 2)


def test_for_pair_creates_secure_paths(tmp_path):
    s = MigrationState.for_pair("a@x.com", "b@y.com", base_dir=tmp_path / "base")
    s.flush()
    state_dir = tmp_path / "base" / "state"
    assert s.path == state_dir / "a@x.com__b@y.com.json"
    assert (state_dir.stat().st_mode & 0o777) == 0o700
    assert (s.path.stat().st_mode & 0o777) == 0o600


def test_is_migrated_does_not_create_folder_entries(tmp_path):
    path = tmp_path / "s.json"
    s = MigrationState(path)
    s.is_migrated("Ghost", "<a@x>", 1)
    s.flush()
    assert json.loads(path.read_text())["folders"] == {}


def test_flush_is_atomic_and_keeps_0600(tmp_path):
    path = tmp_path / "s.json"
    s = MigrationState(path)
    s.mark_migrated("INBOX", "<a@x>", 1)
    s.flush()
    assert (path.stat().st_mode & 0o777) == 0o600
    assert not path.with_suffix(".tmp").exists()


def test_config_and_status_roundtrip(tmp_path):
    path = tmp_path / "s.json"
    s = MigrationState(path)
    assert s.status == "running"
    assert s.config is None
    cfg = {"src": {"host": "a", "port": 993}, "dst": {"host": "b"}, "workers": 4}
    s.set_config(cfg)
    s.flush()

    s2 = MigrationState(path)
    assert s2.config == cfg
    assert s2.status == "running"
    s2.mark_completed()
    s2.flush()
    assert MigrationState(path).status == "completed"


def test_migrated_count(tmp_path):
    s = MigrationState(tmp_path / "s.json")
    s.mark_migrated("INBOX", "<a@x>", 1)
    s.mark_migrated("INBOX", None, 2)
    s.mark_migrated("Sent", "<b@x>", 3)
    assert s.migrated_count() == 3


def test_list_resumable_filters(tmp_path):
    base = tmp_path / "base"

    running = MigrationState.for_pair("a@x", "b@y", base_dir=base)
    running.set_config({"src": {"host": "h1"}})
    running.flush()

    done = MigrationState.for_pair("c@x", "d@y", base_dir=base)
    done.set_config({"src": {"host": "h2"}})
    done.mark_completed()
    done.flush()

    no_config = MigrationState.for_pair("e@x", "f@y", base_dir=base)  # old format
    no_config.flush()

    (base / "state" / "corrupt.json").write_text("{not json")

    resumable = MigrationState.list_resumable(base_dir=base)
    assert len(resumable) == 1
    assert resumable[0].config == {"src": {"host": "h1"}}


def test_old_format_state_still_loads(tmp_path):
    path = tmp_path / "s.json"
    path.write_text('{"folders": {"INBOX": {"uidvalidity": 1, "message_ids": ["<a@x>"], "uids": []}}}')
    s = MigrationState(path)
    assert s.is_migrated("INBOX", "<a@x>", 1)
    assert s.status == "running"
    assert s.config is None


def test_mark_cancelled_roundtrip(tmp_path):
    path = tmp_path / "s.json"
    s = MigrationState(path)
    s.set_config({"src": {"host": "h"}})
    s.mark_cancelled()
    s.flush()
    assert MigrationState(path).status == "cancelled"


def test_list_resumable_excludes_cancelled(tmp_path):
    base = tmp_path / "base"
    cancelled = MigrationState.for_pair("a@x", "b@y", base_dir=base)
    cancelled.set_config({"src": {"host": "h"}})
    cancelled.mark_cancelled()
    cancelled.flush()

    running = MigrationState.for_pair("c@x", "d@y", base_dir=base)
    running.set_config({"src": {"host": "h2"}})
    running.flush()

    resumable = MigrationState.list_resumable(base_dir=base)
    assert len(resumable) == 1
    assert resumable[0].config == {"src": {"host": "h2"}}
