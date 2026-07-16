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
import sys
import threading
import time
from pathlib import Path

from .. import applog
from ..gui.run_manager import RunManager
from . import trayapp
from .lifecycle import acquire_singleton_lock, gui_command, rendezvous_path
from .server import DaemonServer

APP_TITLE = "Email Export Import Tool"


def _write_rendezvous(path: Path, port: int, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"port": port, "token": token, "pid": os.getpid()}, fh)
    os.chmod(path, 0o600)


def _unlink_if_mine(path: Path, pid: int) -> None:
    """Remove the rendezvous ONLY if it still names this daemon. Deleting a
    file that now points at a different, live daemon would orphan it and trigger
    a rival spawn — the exact multiple-tray-icon cascade we are closing."""
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if info.get("pid") == pid:
        path.unlink(missing_ok=True)


def _fmt_duration(secs: float) -> str:
    """Human-readable elapsed time: 42m / 3h 5m / 2d 4h."""
    m = int(max(0.0, secs) // 60)
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m"
    d, h = divmod(h, 24)
    return f"{d}d {h}h"


def _build_run_lines(snaps, started_ats: dict, now: float, line_fmt: str) -> list[str]:
    """One tray line per ACTIVE run — 'dest · done/total · elapsed' — so the user
    sees progress by clicking the tray, without opening the window. Shows only
    the DESTINATION (enough to tell runs apart) and a human-readable elapsed time
    counted from THIS session's start (started_ats[key], the Run's own clock, not
    the pause-spanning first-start). Pure/for-testing."""
    lines: list[str] = []
    for s in snaps:
        if s.status not in ("running", "stopping", "queued"):
            continue
        started = started_ats.get(s.key)
        dur = _fmt_duration(now - started) if started else "—"
        dest = s.title.split("→")[-1].strip() if "→" in s.title else s.title
        total = f"{s.total:,}" if s.total else "?"
        try:
            lines.append(line_fmt.format(dest=dest, done=f"{s.processed:,}",
                                         total=total, dur=dur))
        except Exception:
            lines.append(f"{dest} {s.processed}/{s.total}")
    return lines


def main(base_dir: Path | None = None) -> None:
    # Strict single-instance: hold a lifetime lock for the whole process. A
    # second daemon on the same base dir can't get it and exits here, BEFORE
    # creating a rival tray icon or clobbering the rendezvous.
    lock_fd = acquire_singleton_lock(base_dir)
    if lock_fd is None:
        applog.log("daemon", "another daemon already owns this base dir; exiting",
                   base_dir)
        return

    manager = RunManager(state_dir=base_dir)
    manager.load_resumable()
    manager.load_completed()

    token = secrets.token_urlsafe(24)
    server = DaemonServer(manager, host="127.0.0.1", port=0, token=token)
    server.start()

    path = rendezvous_path(base_dir)
    _write_rendezvous(path, server.port, token)
    applog.log("daemon", f"started exe={sys.executable} port={server.port} "
               f"gui_app={os.environ.get('EEI_GUI_APP')}", base_dir)

    stop = threading.Event()
    # signal handlers can only be installed from the main thread; tests drive
    # main() on a worker thread and stop it via the rendezvous /shutdown.
    # request_stop() routes through _on_stop, which the tray wires to icon.stop
    # — a plain stop.set() would be ignored while the tray's icon.run() blocks
    # the main thread (SIGTERM then never actually stops the daemon).
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *a: server.request_stop())
    except ValueError:
        pass

    # Localise the tray menu to the user's saved language.
    open_label, quit_label = "Show window", "Quit"
    status_tmpl = "{count} migrations running"
    idle_text = APP_TITLE
    line_fmt = "{dest} · {done}/{total} · {dur}"
    try:
        from ..gui.i18n import I18n
        i18n = I18n()
        # A translation table that failed to ship (e.g. locale JSONs missing
        # from a frozen build) makes t() echo the key back — never surface a
        # raw "tray.show" in the menu; keep the English default instead.
        if (v := i18n.t("tray.show")) != "tray.show":
            open_label = v
        if (v := i18n.t("menu.quit")) != "menu.quit":
            quit_label = v
        if (v := i18n.t("tray.status")) != "tray.status":
            status_tmpl = v
        if (v := i18n.t("tray.idle")) != "tray.idle":
            idle_text = v
        if (v := i18n.t("tray.run_line")) != "tray.run_line":
            line_fmt = v
    except Exception:
        pass

    def _status() -> str:
        n = manager.active_count()
        if n:
            try:
                return status_tmpl.format(count=n)
            except Exception:
                return f"{n} running"
        return idle_text

    def _status_lines() -> list[str]:
        # One line per active run (progress + elapsed), refreshed each menu open.
        # Uses the Run's own session clock (started_wall), not state.started_at
        # which spans pauses and over-counts after a resume.
        snaps = manager.snapshot_all()
        started = {s.key: (r.started_wall if (r := manager.get(s.key)) else None)
                   for s in snaps}
        return _build_run_lines(snaps, started, time.time(), line_fmt)

    def _open_gui() -> None:
        # Closing a daemon-backed GUI fully exits its process (it is never just
        # hidden), so there is never a window-less GUI to "reveal" — always
        # launch. The daemon runs outside the .app, so `open <app>` renders a
        # real GUI, and macOS coalesces it to a focus when one is already open.
        cmd = gui_command()
        applog.log("daemon", f"tray Show: launch/focus GUI cmd={cmd}", base_dir)
        try:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL,  # noqa: S603
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:  # noqa: BLE001
            applog.log("daemon", f"tray Show: launch failed: {exc}", base_dir)

    def _quit() -> None:
        # Ask a running GUI to close too, then WAIT for it to actually go away
        # before tearing the server down. A fixed 0.5s sleep raced the GUI's
        # poll: if the daemon stopped first, the GUI missed the quit latch and
        # lingered window-less. Poll gui_alive (capped) so the common case is a
        # clean, ordered shutdown; the GUI's daemon-lost backstop covers the rest.
        applog.log("daemon", "tray Quit", base_dir)
        server.request_quit_gui()
        if server.gui_alive():
            time.sleep(0.3)  # let the GUI poll read the quit latch and os._exit
            deadline = time.monotonic() + 3.0
            while server.gui_alive(within=0.6) and time.monotonic() < deadline:
                time.sleep(0.1)
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
        ran = trayapp.run(APP_TITLE, _status, _open_gui, _quit, on_ready=_on_ready,
                          open_label=open_label, quit_label=quit_label,
                          run_lines=_status_lines)
        if not ran:
            while not stop.is_set():
                time.sleep(0.5)
    finally:
        applog.log("daemon", "stopping", base_dir)
        server.stop()
        _unlink_if_mine(path, os.getpid())  # never delete a live daemon's file
        try:
            os.close(lock_fd)  # release the singleton lock
        except OSError:
            pass


if __name__ == "__main__":
    _base = os.environ.get("EEI_BASE_DIR")
    main(base_dir=Path(_base) if _base else None)
