r"""Register (or unregister) the migration daemon to start at user login.

The daemon is what keeps a long migration alive after the GUI window is
closed, so "start at login" makes an interrupted run resume itself without the
user having to reopen the app. Each OS has its own per-user autostart
mechanism; we drive the native one directly rather than bundle a helper:

- macOS  -> a launchd LaunchAgent plist under ~/Library/LaunchAgents
- Windows -> an HKCU ...\CurrentVersion\Run registry value
- Linux (and other X/desktop unices) -> an XDG ~/.config/autostart .desktop

Every entry launches exactly what lifecycle._spawn launches (see
daemon_command), so a login-started daemon is indistinguishable from a
GUI-spawned one.

Design rule, matching secrets_store.py: all three public verbs FAIL CLOSED.
Autostart is a convenience, never load-bearing; a locked registry or an
unwritable HOME must degrade to "not installed", never crash the app. Hence
every OS call is wrapped and returns False/False/False on error.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Stable identifiers. These name the on-disk artefacts, so changing them would
# orphan an already-installed entry — keep them constant across releases.
LABEL = "com.ckurultaykalkan.email-export-import.daemon"
WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WIN_VALUE_NAME = "EmailExportImportDaemon"
LINUX_DESKTOP_NAME = "email-export-import-daemon.desktop"


def daemon_command() -> list[str]:
    """The argv that launches the daemon — delegated to lifecycle so autostart
    and GUI-spawn always agree (sidecar-existence detection)."""
    from .lifecycle import daemon_command as _cmd

    return _cmd()


# ---- path helpers (home injectable so tests never touch the real HOME) -------

def _plist_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _macos_gui_app() -> str | None:
    """The .app bundle enclosing this process, if any, so a login-launched
    daemon can open the GUI with `open <app>` (its tray "Show window"). Returns
    None from source (no bundle) — the tray falls back to gui_command's sibling."""
    exe = Path(sys.executable)
    for p in exe.parents:
        if p.suffix == ".app":
            return str(p)
    return None


def _desktop_path(home: Path) -> Path:
    return home / ".config" / "autostart" / LINUX_DESKTOP_NAME


# ---- macOS: launchd LaunchAgent ---------------------------------------------

def _macos_install(home: Path) -> bool:
    import plistlib

    path = _plist_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": daemon_command(),
        "RunAtLoad": True,
        # We do not want launchd resurrecting a daemon that exited on purpose
        # (e.g. after finishing / a clean shutdown); RunAtLoad alone is enough.
        "KeepAlive": False,
    }
    # launchd launches the daemon with a bare environment, so without this the
    # login-started daemon has no EEI_GUI_APP and its tray "Show window" can't
    # `open <app>`. Inject it when we can resolve the enclosing bundle.
    app = _macos_gui_app()
    if app:
        plist["EnvironmentVariables"] = {"EEI_GUI_APP": app}
    with open(path, "wb") as fh:
        plistlib.dump(plist, fh)
    # Best effort: load it now so it also runs this session. A failure here
    # (already loaded, no launchd, headless) is non-fatal — RunAtLoad still
    # takes effect at next login.
    try:
        subprocess.run(["launchctl", "load", str(path)],  # noqa: S603, S607
                       capture_output=True, timeout=10)
    except Exception:
        pass
    return True


def _macos_remove(home: Path) -> bool:
    path = _plist_path(home)
    try:
        subprocess.run(["launchctl", "unload", str(path)],  # noqa: S603, S607
                       capture_output=True, timeout=10)
    except Exception:
        pass
    path.unlink(missing_ok=True)
    return True


def _macos_is_installed(home: Path) -> bool:
    return _plist_path(home).exists()


# ---- Windows: HKCU Run registry value ---------------------------------------
# winreg only exists on Windows; guard the import so this module still imports
# (and its ops fail closed) on every other platform.

def _winreg():
    try:
        import winreg

        return winreg
    except Exception:
        return None


def _win_command_string() -> str:
    """The Run value is a single command line, so quote each argv element that
    may contain spaces (paths under 'Program Files', etc.)."""
    return " ".join(f'"{part}"' for part in daemon_command())


def _windows_install(_home: Path) -> bool:
    winreg = _winreg()
    if winreg is None:
        return False
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY, 0,
                         winreg.KEY_SET_VALUE)
    try:
        winreg.SetValueEx(key, WIN_VALUE_NAME, 0, winreg.REG_SZ,
                          _win_command_string())
    finally:
        winreg.CloseKey(key)
    return True


def _windows_remove(_home: Path) -> bool:
    winreg = _winreg()
    if winreg is None:
        return False
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY, 0,
                         winreg.KEY_SET_VALUE)
    try:
        try:
            winreg.DeleteValue(key, WIN_VALUE_NAME)
        except FileNotFoundError:
            pass  # already absent — removal is idempotent
    finally:
        winreg.CloseKey(key)
    return True


def _windows_is_installed(_home: Path) -> bool:
    winreg = _winreg()
    if winreg is None:
        return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY, 0,
                             winreg.KEY_QUERY_VALUE)
    except FileNotFoundError:
        return False
    try:
        winreg.QueryValueEx(key, WIN_VALUE_NAME)
        return True
    except FileNotFoundError:
        return False
    finally:
        winreg.CloseKey(key)


# ---- Linux / other: XDG autostart .desktop ----------------------------------

def _linux_install(home: Path) -> bool:
    path = _desktop_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    exec_line = " ".join(daemon_command())
    path.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Email Export Import Daemon\n"
        f"Exec={exec_line}\n"
        "X-GNOME-Autostart-enabled=true\n",
        encoding="utf-8",
    )
    return True


def _linux_remove(home: Path) -> bool:
    _desktop_path(home).unlink(missing_ok=True)
    return True


def _linux_is_installed(home: Path) -> bool:
    return _desktop_path(home).exists()


# ---- dispatch ---------------------------------------------------------------

def _backend():
    """(install, remove, is_installed) implementations for this platform."""
    if sys.platform == "darwin":
        return _macos_install, _macos_remove, _macos_is_installed
    if sys.platform == "win32":
        return _windows_install, _windows_remove, _windows_is_installed
    # Everything else is treated as a freedesktop/XDG desktop (Linux, *BSD).
    return _linux_install, _linux_remove, _linux_is_installed


def install(home: Path | None = None) -> bool:
    """Register the daemon to run at login for the current user. Idempotent
    (installing twice just rewrites the entry). Returns True on success, False
    if unsupported or the OS call failed — never raises."""
    home = home if home is not None else Path.home()
    try:
        return _backend()[0](home)
    except Exception:
        return False


def remove(home: Path | None = None) -> bool:
    """Unregister the login entry. Idempotent (removing when absent still
    returns True). Never raises."""
    home = home if home is not None else Path.home()
    try:
        return _backend()[1](home)
    except Exception:
        return False


def is_installed(home: Path | None = None) -> bool:
    """True when a login entry is currently registered. Never raises."""
    home = home if home is not None else Path.home()
    try:
        return _backend()[2](home)
    except Exception:
        return False
