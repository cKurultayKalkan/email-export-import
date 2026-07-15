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

    # Localise the tray menu to the user's saved language.
    open_label, quit_label = "Show window", "Quit"
    status_tmpl = "{count} migrations running"
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
    except Exception:
        pass

    def _status() -> str:
        n = manager.active_count()
        if n:
            try:
                return status_tmpl.format(count=n)
            except Exception:
                return f"{n} running"
        return APP_TITLE

    def _open_gui() -> None:
        # A GUI already alive (possibly just hidden behind its close button):
        # reveal it rather than spawning a second, blank instance. Only
        # cold-launch when none is running.
        if server.gui_alive():
            server.request_show()
            return
        try:
            subprocess.Popen(gui_command(), stdin=subprocess.DEVNULL,  # noqa: S603
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _quit() -> None:
        # Ask a running GUI to close too, and give its poll a moment to see the
        # flag before the daemon (and this server) go away.
        server.request_quit_gui()
        if server.gui_alive():
            time.sleep(0.5)
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
                          open_label=open_label, quit_label=quit_label)
        if not ran:
            while not stop.is_set():
                time.sleep(0.5)
    finally:
        server.stop()
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    _base = os.environ.get("EEI_BASE_DIR")
    main(base_dir=Path(_base) if _base else None)
