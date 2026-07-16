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


def _lock_path(base_dir: Path | None = None) -> Path:
    return (base_dir or DEFAULT_BASE_DIR) / "daemon.lock"


def acquire_singleton_lock(base_dir: Path | None = None) -> int | None:
    """Take the daemon's lifetime lock.

    Returns an OPEN file descriptor on success — the caller MUST keep it open
    for the whole process life; the OS releases the lock on exit or crash (no
    stale PID files to reap). Returns None if another daemon already holds it.

    This is the daemon's single-instance guard: a second daemon that cannot get
    the lock exits before it ever creates a rival tray icon. It is a separate
    lock from anything the GUI/connect_or_spawn holds, so there is no
    parent-holds-while-child-waits deadlock.
    """
    base = base_dir if base_dir is not None else DEFAULT_BASE_DIR
    try:
        base.mkdir(parents=True, exist_ok=True)
        os.chmod(base, 0o700)
    except OSError:
        pass
    try:
        fd = os.open(_lock_path(base), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None
    try:
        if sys.platform == "win32":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None  # already held by a live daemon
    except Exception:  # noqa: BLE001
        # Never let an odd-platform locking quirk block the daemon from running
        # — degrade to no-lock rather than refuse to start.
        return fd
    return fd


def _alive_retry(client: DaemonClient, tries: int = 3, delay: float = 0.15) -> bool:
    """Ping a daemon a few times before writing it off: one dropped tick must
    not condemn a healthy daemon as stale, which would spawn a rival daemon
    (that then can't get the lock and dies, leaving the GUI unable to connect)."""
    for i in range(tries):
        if client.is_alive():
            return True
        if i < tries - 1:
            time.sleep(delay)
    return False


def _macos_bundle_id(app: str) -> str | None:
    """The CFBundleIdentifier of a .app bundle, or None if unreadable."""
    try:
        import plistlib
        with open(Path(app) / "Contents" / "Info.plist", "rb") as fh:
            bid = plistlib.load(fh).get("CFBundleIdentifier")
        return bid or None
    except Exception:
        return None


def gui_command() -> list[str]:
    """The argv the daemon uses to open the GUI (tray → Show window).

    On macOS the daemon runs OUTSIDE the .app (see daemon_command). We launch by
    BUNDLE ID (`open -b <id>`) rather than by path: a quarantined app first runs
    App-Translocated from an ephemeral /private/.../AppTranslocation/ path, and
    re-opening that now-stale path renders a GRAY window. `open -b` always does a
    fresh LaunchServices launch of the registered app (the /Applications copy),
    so Show window renders every time. Falls back to `open <path>` if the bundle
    id can't be read. The app path is passed in via EEI_GUI_APP when the daemon
    is spawned. From source, re-exec this interpreter into the GUI entry."""
    if sys.platform == "darwin":
        app = os.environ.get("EEI_GUI_APP")
        if not app:
            exe = Path(sys.executable)
            for p in exe.parents:
                if p.suffix == ".app":
                    app = str(p)
                    break
        if app:
            bid = _macos_bundle_id(app)
            if bid:
                return ["open", "-b", bid]
            return ["open", str(app)]
    # Non-macOS packaged app: the GUI handed the daemon its own frozen launcher
    # via EEI_GUI_EXE, so Show window relaunches the REAL GUI. Without it the
    # fallbacks below hit the frozen daemon binary (sys.executable here), which
    # ignores `-c` and just re-runs the daemon — so the window never appears.
    gui_exe = os.environ.get("EEI_GUI_EXE")
    if gui_exe and Path(gui_exe).exists():
        return [gui_exe]
    exe = Path(sys.executable)
    # The GUI entry point — NOT `email-export-import`, which is the CLI wizard.
    gui_name = ("email-export-import-gui.exe" if sys.platform == "win32"
                else "email-export-import-gui")
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
    # Tell the (out-of-bundle) daemon how to relaunch the GUI for tray "Show
    # window". macOS: the .app path (→ open -b <bundle id>). Windows/Linux: the
    # GUI's own frozen launcher, so the daemon doesn't fall back to re-running
    # its own binary. Skip a plain python interpreter (source runs), which is not
    # the GUI and would open a bare REPL.
    exe = Path(sys.executable)
    if sys.platform == "darwin":
        for p in exe.parents:
            if p.suffix == ".app":
                env["EEI_GUI_APP"] = str(p)
                break
    elif "python" not in exe.name.lower():
        env["EEI_GUI_EXE"] = str(exe)
    cmd = daemon_command(base_dir)
    applog.log("gui", f"spawning daemon cmd={cmd} gui_app={env.get('EEI_GUI_APP')} "
               f"gui_exe={env.get('EEI_GUI_EXE')}", base_dir)
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
        if _alive_retry(client):
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
