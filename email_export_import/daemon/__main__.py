"""Run the migration daemon: `python -m email_export_import.daemon`.

Binds a random loopback port, writes {port, token, pid} 0600 into the state
dir so a GUI can find and authenticate to it, loads any resumable/completed
runs, and serves until asked to shut down. Passwords are never involved here
— they arrive per start/resume request and stay in memory only.
"""
from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import threading
import time
from pathlib import Path

from ..gui.run_manager import RunManager
from . import trayapp
from .lifecycle import gui_command, rendezvous_path
from .server import DaemonServer

APP_TITLE = "Email Export Import Tool"


def _write_rendezvous(path: Path, port: int, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"port": port, "token": token, "pid": os.getpid()}, fh)
    os.chmod(path, 0o600)


def main(base_dir: Path | None = None) -> None:
    manager = RunManager(state_dir=base_dir)
    manager.load_resumable()
    manager.load_completed()

    token = secrets.token_urlsafe(24)
    server = DaemonServer(manager, host="127.0.0.1", port=0, token=token)
    server.start()

    path = rendezvous_path(base_dir)
    _write_rendezvous(path, server.port, token)

    stop = threading.Event()
    # signal handlers can only be installed from the main thread; tests drive
    # main() on a worker thread and stop it via the rendezvous /shutdown.
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *a: stop.set())
    except ValueError:
        pass

    def _status() -> str:
        n = manager.active_count()
        return f"{APP_TITLE} — {n} migrating" if n else f"{APP_TITLE} — idle"

    def _open_gui() -> None:
        try:
            subprocess.Popen(gui_command(), stdin=subprocess.DEVNULL,  # noqa: S603
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _quit() -> None:
        stop.set()

    def _on_ready(icon) -> None:
        # A /shutdown request (from the GUI's full-quit) must also end the tray
        # loop; icon.stop() is thread-safe.
        server._on_stop = icon.stop

    try:
        # The tray runs on THIS (main) thread and is the daemon's persistent
        # handle; the HTTP server is already serving on its own thread. If no
        # tray backend is available (headless), fall back to a plain wait.
        server._on_stop = stop.set  # until the tray wires its own stop
        ran = trayapp.run(APP_TITLE, _status, _open_gui, _quit, on_ready=_on_ready)
        if not ran:
            while not stop.is_set():
                time.sleep(0.5)
    finally:
        server.stop()
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    _base = os.environ.get("EEI_BASE_DIR")
    main(base_dir=Path(_base) if _base else None)
