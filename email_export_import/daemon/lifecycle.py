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
from .client import DaemonClient

RENDEZVOUS = "daemon.json"


def rendezvous_path(base_dir: Path | None = None) -> Path:
    """The 0600 file where the daemon publishes {port, token, pid} so the GUI
    can find and authenticate to it."""
    return (base_dir or DEFAULT_BASE_DIR) / RENDEZVOUS


def _read_rendezvous(base_dir: Path) -> dict | None:
    path = rendezvous_path(base_dir)
    if not path.exists():
        return None
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        if {"port", "token"} <= set(info):
            return info
    except (ValueError, OSError):
        pass
    return None


def _client_for(info: dict) -> DaemonClient:
    return DaemonClient(f"http://127.0.0.1:{info['port']}", token=info["token"])


def gui_command() -> list[str]:
    """The argv the daemon uses to open the GUI (tray → Open). Packaged builds
    launch the sibling GUI executable (via `open <app>` on macOS for a proper
    GUI launch); from source, re-exec this interpreter into the GUI entry."""
    exe = Path(sys.executable)
    if sys.platform == "darwin":
        for p in exe.parents:
            if p.suffix == ".app":
                return ["open", str(p)]
    gui_name = ("email-export-import.exe" if sys.platform == "win32"
                else "email-export-import")
    sibling = exe.with_name(gui_name)
    if sibling.exists() and sibling != exe:
        return [str(sibling)]
    return [sys.executable, "-c",
            "from email_export_import.gui.app import main; main()"]


def daemon_command() -> list[str]:
    """The argv to launch the daemon. Detect a packaged build by the presence
    of the bundled `eei-daemon` sidecar next to the executable — more robust
    than sys.frozen, which serious_python/flet apps don't reliably set. From
    source, re-exec this interpreter with -m email_export_import.daemon."""
    sidecar = Path(sys.executable).with_name(
        "eei-daemon.exe" if sys.platform == "win32" else "eei-daemon"
    )
    if sidecar.exists():
        return [str(sidecar)]
    return [sys.executable, "-m", "email_export_import.daemon"]


def _spawn(base_dir: Path) -> None:
    """Launch a DETACHED daemon process that outlives the GUI (packaged sidecar
    or source module)."""
    env = dict(os.environ)
    if base_dir is not None:
        env["EEI_BASE_DIR"] = str(base_dir)
    cmd = daemon_command()
    kwargs = {}
    if sys.platform == "win32":
        # Detach from the GUI's console/process group so it survives the GUI
        # exiting; CREATE_NO_WINDOW keeps the tray-only daemon from flashing a
        # console window.
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(  # noqa: S603
        cmd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs,
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
