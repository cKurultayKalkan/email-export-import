"""Find a running daemon or spawn one, and hand back a connected client.

The GUI calls connect_or_spawn() at startup: if a healthy daemon is already
serving (its rendezvous file points at a live, correctly-tokened server) it
reuses it; otherwise it launches one and waits for the rendezvous to appear.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .. import applog
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
    """The argv the daemon uses to open the GUI (tray → Show window).

    On macOS the daemon runs OUTSIDE the .app (see daemon_command), so the app
    bundle is not "already running" and a plain `open <app>` launches the GUI
    (or focuses it if it's already open) — no blank window, native single
    instance. The app path is passed in via EEI_GUI_APP when the daemon is
    spawned. From source, re-exec this interpreter into the GUI entry."""
    if sys.platform == "darwin":
        app = os.environ.get("EEI_GUI_APP")
        if not app:
            exe = Path(sys.executable)
            for p in exe.parents:
                if p.suffix == ".app":
                    app = str(p)
                    break
        if app:
            return ["open", str(app)]
    exe = Path(sys.executable)
    gui_name = ("email-export-import.exe" if sys.platform == "win32"
                else "email-export-import")
    sibling = exe.with_name(gui_name)
    if sibling.exists() and sibling != exe:
        return [str(sibling)]
    return [sys.executable, "-c",
            "from email_export_import.gui.app import main; main()"]


def _external_daemon_path(base_dir: Path | None) -> Path:
    base = base_dir if base_dir is not None else DEFAULT_BASE_DIR
    name = "eei-daemon.exe" if sys.platform == "win32" else "eei-daemon"
    return base / "bin" / name


def _ensure_external_copy(src: Path, dst: Path) -> None:
    """Copy the bundled daemon out of the .app (idempotent; re-copies when the
    build changes, detected by size). The copy is what runs, so LaunchServices
    doesn't tie the daemon to the .app bundle."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_size == src.stat().st_size:
        return  # same build already staged
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.copy2(src, tmp)
    tmp.chmod(0o755)
    os.replace(tmp, dst)


def daemon_command(base_dir: Path | None = None) -> list[str]:
    """The argv to launch the daemon. Detect a packaged build by the presence
    of the bundled `eei-daemon` sidecar next to the executable. On macOS, run a
    COPY placed outside the .app so the bundle isn't seen as running (which
    would block launching the GUI). From source, re-exec -m ...daemon."""
    sidecar = Path(sys.executable).with_name(
        "eei-daemon.exe" if sys.platform == "win32" else "eei-daemon"
    )
    if sidecar.exists():
        if sys.platform == "darwin":
            try:
                ext = _external_daemon_path(base_dir)
                _ensure_external_copy(sidecar, ext)
                return [str(ext)]
            except Exception as exc:  # noqa: BLE001
                applog.log("gui", f"external daemon copy failed: {exc}", base_dir)
                return [str(sidecar)]  # fall back to in-bundle
        return [str(sidecar)]
    return [sys.executable, "-m", "email_export_import.daemon"]


def _spawn(base_dir: Path) -> None:
    """Launch a DETACHED daemon process that outlives the GUI (packaged sidecar
    or source module)."""
    env = dict(os.environ)
    if base_dir is not None:
        env["EEI_BASE_DIR"] = str(base_dir)
    # Tell the (out-of-bundle) daemon where the GUI app is, so its tray can
    # launch/focus it with `open <app>`.
    if sys.platform == "darwin":
        exe = Path(sys.executable)
        for p in exe.parents:
            if p.suffix == ".app":
                env["EEI_GUI_APP"] = str(p)
                break
    cmd = daemon_command(base_dir)
    applog.log("gui", f"spawning daemon cmd={cmd} gui_app={env.get('EEI_GUI_APP')}",
               base_dir)
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
            applog.log("gui", "connected to existing daemon", base)
            return client
        # Stale file: the daemon it named is gone. Fall through to spawn.
        applog.log("gui", "stale rendezvous; respawning daemon", base)

    _spawn(base)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = _read_rendezvous(base)
        if info is not None:
            client = _client_for(info)
            if client.is_alive():
                applog.log("gui", "daemon spawned and reachable", base)
                return client
        time.sleep(0.05)
    applog.log("gui", "daemon did not come up within timeout", base)
    return None
