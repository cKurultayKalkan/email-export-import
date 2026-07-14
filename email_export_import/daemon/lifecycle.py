"""Find a running daemon or spawn one, and hand back a connected client.

The GUI calls connect_or_spawn() at startup: if a healthy daemon is already
serving (its rendezvous file points at a live, correctly-tokened server) it
reuses it; otherwise it launches one and waits for the rendezvous to appear.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from ..state import DEFAULT_BASE_DIR
from .__main__ import rendezvous_path
from .client import DaemonClient


def _read_rendezvous(base_dir: Path) -> dict | None:
    path = rendezvous_path(base_dir)
    if not path.exists():
        return None
    try:
        info = json.loads(path.read_text())
        if {"port", "token"} <= set(info):
            return info
    except (ValueError, OSError):
        pass
    return None


def _client_for(info: dict) -> DaemonClient:
    return DaemonClient(f"http://127.0.0.1:{info['port']}", token=info["token"])


def _spawn(base_dir: Path) -> None:
    """Launch a detached daemon process. Frozen (packaged) builds run the
    bundled sidecar binary; from source we re-exec this interpreter with
    -m email_export_import.daemon."""
    env = dict(os.environ)
    if base_dir is not None:
        env["EEI_BASE_DIR"] = str(base_dir)
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).with_name("eei-daemon")
        cmd = [str(exe)]
    else:
        cmd = [sys.executable, "-m", "email_export_import.daemon"]
    subprocess.Popen(  # noqa: S603
        cmd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def connect_or_spawn(base_dir: Path | None = None,
                     timeout: float = 10.0) -> DaemonClient | None:
    base = base_dir if base_dir is not None else DEFAULT_BASE_DIR

    info = _read_rendezvous(base)
    if info is not None:
        client = _client_for(info)
        if client.is_alive():
            return client
        # Stale file: the daemon it named is gone. Fall through to spawn.

    _spawn(base)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = _read_rendezvous(base)
        if info is not None:
            client = _client_for(info)
            if client.is_alive():
                return client
        time.sleep(0.05)
    return None
