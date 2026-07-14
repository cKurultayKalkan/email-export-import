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
import threading
import time
from pathlib import Path

from ..state import DEFAULT_BASE_DIR
from ..gui.run_manager import RunManager
from .server import DaemonServer

RENDEZVOUS = "daemon.json"


def rendezvous_path(base_dir: Path | None = None) -> Path:
    return (base_dir or DEFAULT_BASE_DIR) / RENDEZVOUS


def _write_rendezvous(path: Path, port: int, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
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
    server._on_stop = stop.set  # /shutdown also ends the serve loop
    try:
        while not stop.is_set():
            time.sleep(0.5)
    finally:
        server.stop()
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
