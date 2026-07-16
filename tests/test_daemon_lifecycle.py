"""The GUI finds a running daemon or spawns one, then gets a connected client."""
import json

import pytest

from email_export_import.daemon import lifecycle
from email_export_import.daemon.__main__ import rendezvous_path


def test_connect_uses_a_live_daemon_without_spawning(tmp_path, monkeypatch):
    # A rendezvous file pointing at a live daemon must be reused, not respawned.
    from email_export_import.daemon.server import DaemonServer
    from email_export_import.gui.run_manager import RunManager

    server = DaemonServer(RunManager(state_dir=tmp_path), token="tok")
    server.start()
    try:
        rp = rendezvous_path(tmp_path)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(json.dumps({"port": server.port, "token": "tok", "pid": 1}))

        spawned = []
        monkeypatch.setattr(lifecycle, "_spawn", lambda base: spawned.append(base))
        client = lifecycle.connect_or_spawn(base_dir=tmp_path)
        assert client is not None and client.is_alive()
        assert spawned == [], "a live daemon must not be respawned"
    finally:
        server.stop()


def test_connect_spawns_when_no_daemon(tmp_path, monkeypatch):
    # No rendezvous file -> spawn, then wait for the file the daemon writes.
    from email_export_import.daemon.server import DaemonServer
    from email_export_import.gui.run_manager import RunManager

    server = DaemonServer(RunManager(state_dir=tmp_path), token="tok2")

    def fake_spawn(base):
        server.start()
        rp = rendezvous_path(base)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(json.dumps({"port": server.port, "token": "tok2", "pid": 2}))

    monkeypatch.setattr(lifecycle, "_spawn", fake_spawn)
    try:
        client = lifecycle.connect_or_spawn(base_dir=tmp_path, timeout=3)
        assert client is not None and client.is_alive()
    finally:
        server.stop()


def test_connect_respawns_when_rendezvous_is_stale(tmp_path, monkeypatch):
    # A leftover rendezvous file whose daemon is gone must trigger a respawn.
    from email_export_import.daemon.server import DaemonServer
    from email_export_import.gui.run_manager import RunManager

    rp = rendezvous_path(tmp_path)
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(json.dumps({"port": 1, "token": "dead", "pid": 999999}))  # nothing there

    server = DaemonServer(RunManager(state_dir=tmp_path), token="fresh")

    def fake_spawn(base):
        server.start()
        rendezvous_path(base).write_text(
            json.dumps({"port": server.port, "token": "fresh", "pid": 3}))

    monkeypatch.setattr(lifecycle, "_spawn", fake_spawn)
    try:
        client = lifecycle.connect_or_spawn(base_dir=tmp_path, timeout=3)
        assert client is not None and client.is_alive()
    finally:
        server.stop()


def test_connect_returns_none_if_spawn_never_appears(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "_spawn", lambda base: None)  # spawn does nothing
    assert lifecycle.connect_or_spawn(base_dir=tmp_path, timeout=0.5) is None


def test_connect_retries_ping_before_declaring_stale(tmp_path, monkeypatch):
    # A single transient ping miss must NOT respawn: the live daemon is re-pinged
    # before the rendezvous is judged stale (a needless spawn would race the lock
    # and leave the GUI unable to connect).
    from email_export_import.daemon.client import DaemonClient
    from email_export_import.daemon.server import DaemonServer
    from email_export_import.gui.run_manager import RunManager

    server = DaemonServer(RunManager(state_dir=tmp_path), token="tok")
    server.start()
    try:
        rp = rendezvous_path(tmp_path)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(json.dumps({"port": server.port, "token": "tok", "pid": 1}))

        calls = {"n": 0}
        real_alive = DaemonClient.is_alive

        def flaky_alive(self):
            calls["n"] += 1
            return False if calls["n"] == 1 else real_alive(self)

        monkeypatch.setattr(DaemonClient, "is_alive", flaky_alive)
        spawned = []
        monkeypatch.setattr(lifecycle, "_spawn", lambda base: spawned.append(base))

        client = lifecycle.connect_or_spawn(base_dir=tmp_path)
        assert client is not None
        assert spawned == [], "a transient ping miss must not respawn"
        assert calls["n"] >= 2, "the ping must be retried before giving up"
    finally:
        server.stop()


def test_ensure_external_copy_is_idempotent_and_updates_on_new_build(tmp_path):
    # The daemon is copied out of the .app; a same-size copy is left alone, a
    # different-size (new build) is refreshed.
    src = tmp_path / "eei-daemon"
    src.write_bytes(b"BINARYv1")  # 8 bytes
    dst = tmp_path / "bin" / "eei-daemon"

    lifecycle._ensure_external_copy(src, dst)
    assert dst.read_bytes() == b"BINARYv1"

    dst.write_bytes(b"TAMPERED")  # same 8-byte size -> must be left as-is
    lifecycle._ensure_external_copy(src, dst)
    assert dst.read_bytes() == b"TAMPERED"

    src.write_bytes(b"BINARYv1-LONGER")  # different size -> refresh
    lifecycle._ensure_external_copy(src, dst)
    assert dst.read_bytes() == b"BINARYv1-LONGER"


def test_gui_command_opens_the_app_on_macos(monkeypatch):
    # On macOS the tray launches the GUI with `open <app>` (app path from env).
    monkeypatch.setattr(lifecycle.sys, "platform", "darwin")
    monkeypatch.setenv("EEI_GUI_APP", "/Applications/Email Export Import Tool.app")
    assert lifecycle.gui_command() == [
        "open", "/Applications/Email Export Import Tool.app"]
