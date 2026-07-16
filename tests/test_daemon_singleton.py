"""The daemon must be a strict singleton and own its rendezvous file.

These cover the coordination layer that was the real cause of "multiple tray
icons" / orphaned daemons: a lifetime lock so a second daemon can never take
over, and an ownership-guarded rendezvous so a dying process only ever deletes
its own file.
"""
import json
import os

from email_export_import.daemon import lifecycle
from email_export_import.daemon.__main__ import (
    _unlink_if_mine,
    rendezvous_path,
)


def test_singleton_lock_is_exclusive_and_releases_on_close(tmp_path):
    fd1 = lifecycle.acquire_singleton_lock(tmp_path)
    assert fd1 is not None, "first daemon must acquire the lock"

    fd2 = lifecycle.acquire_singleton_lock(tmp_path)
    assert fd2 is None, "a second daemon must NOT acquire the held lock"

    os.close(fd1)  # first daemon exits -> lock released
    fd3 = lifecycle.acquire_singleton_lock(tmp_path)
    assert fd3 is not None, "lock must be re-acquirable once released"
    os.close(fd3)


def test_main_bails_without_side_effects_when_lock_is_held(tmp_path):
    from email_export_import.daemon.__main__ import main

    held = lifecycle.acquire_singleton_lock(tmp_path)
    assert held is not None
    try:
        # A second daemon on a locked base dir must return immediately: no
        # rendezvous written, no server, no tray.
        main(base_dir=tmp_path)
        assert not rendezvous_path(tmp_path).exists(), \
            "a bailed second daemon must not write the rendezvous"
    finally:
        os.close(held)


def test_unlink_if_mine_only_removes_own_rendezvous(tmp_path):
    rp = rendezvous_path(tmp_path)
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(json.dumps({"port": 5, "token": "t", "pid": 4242}))

    _unlink_if_mine(rp, pid=9999)  # a different daemon must not delete it
    assert rp.exists(), "a foreign pid must not unlink another daemon's file"

    _unlink_if_mine(rp, pid=4242)  # the owner may
    assert not rp.exists(), "the owning pid must unlink its own file"


def test_unlink_if_mine_is_safe_when_file_missing_or_garbage(tmp_path):
    rp = rendezvous_path(tmp_path)
    rp.parent.mkdir(parents=True, exist_ok=True)
    _unlink_if_mine(rp, pid=1)  # missing file: no raise

    rp.write_text("not json")
    _unlink_if_mine(rp, pid=1)  # garbage: no raise, left as-is
    assert rp.exists()
