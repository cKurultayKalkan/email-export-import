"""Login-autostart registration: writes the right native artefact per OS,
round-trips install/remove idempotently, and fails closed (never raises).

HOME is injected via the `home=` param so these never touch the real user's
LaunchAgents / autostart dir. subprocess.run is stubbed so `launchctl` is
never actually invoked."""
import sys

import pytest

from email_export_import.daemon import autostart


@pytest.fixture(autouse=True)
def _no_launchctl(monkeypatch):
    # Neutralise every subprocess (launchctl load/unload) so the tests stay
    # hermetic regardless of the host platform.
    monkeypatch.setattr(autostart.subprocess, "run", lambda *a, **k: None)


def test_daemon_command_source_form(monkeypatch):
    # From source (no sys.frozen) the daemon is re-exec'd via -m.
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert autostart.daemon_command() == [
        sys.executable, "-m", "email_export_import.daemon"
    ]


def test_macos_install_remove_round_trip(tmp_path, monkeypatch):
    import plistlib

    monkeypatch.setattr(sys, "platform", "darwin")

    assert autostart.is_installed(home=tmp_path) is False
    assert autostart.install(home=tmp_path) is True
    assert autostart.is_installed(home=tmp_path) is True

    plist = tmp_path / "Library" / "LaunchAgents" / f"{autostart.LABEL}.plist"
    assert plist.exists()
    with open(plist, "rb") as fh:
        data = plistlib.load(fh)
    assert data["Label"] == autostart.LABEL
    assert data["ProgramArguments"] == autostart.daemon_command()
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] is False

    # install twice is fine (idempotent), file still there.
    assert autostart.install(home=tmp_path) is True
    assert autostart.is_installed(home=tmp_path) is True

    assert autostart.remove(home=tmp_path) is True
    assert autostart.is_installed(home=tmp_path) is False
    # removing when already absent still succeeds.
    assert autostart.remove(home=tmp_path) is True


def test_macos_plist_carries_gui_app_env_when_known(tmp_path, monkeypatch):
    # A login-launched daemon has no EEI_GUI_APP unless the plist sets it, which
    # breaks its tray "Show window". When the app path is discoverable, the
    # plist must carry it as an environment variable.
    import plistlib

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(autostart, "_macos_gui_app",
                        lambda: "/Applications/Email Export Import Tool.app")
    assert autostart.install(home=tmp_path) is True

    plist = tmp_path / "Library" / "LaunchAgents" / f"{autostart.LABEL}.plist"
    with open(plist, "rb") as fh:
        data = plistlib.load(fh)
    assert data["EnvironmentVariables"]["EEI_GUI_APP"] == \
        "/Applications/Email Export Import Tool.app"


def test_macos_plist_omits_gui_app_env_when_unknown(tmp_path, monkeypatch):
    import plistlib

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(autostart, "_macos_gui_app", lambda: None)
    assert autostart.install(home=tmp_path) is True

    plist = tmp_path / "Library" / "LaunchAgents" / f"{autostart.LABEL}.plist"
    with open(plist, "rb") as fh:
        data = plistlib.load(fh)
    assert "EnvironmentVariables" not in data  # nothing to inject from source


def test_linux_install_remove_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    assert autostart.is_installed(home=tmp_path) is False
    assert autostart.install(home=tmp_path) is True
    assert autostart.is_installed(home=tmp_path) is True

    desktop = tmp_path / ".config" / "autostart" / autostart.LINUX_DESKTOP_NAME
    assert desktop.exists()
    text = desktop.read_text()
    assert "[Desktop Entry]" in text
    assert "Type=Application" in text
    assert "X-GNOME-Autostart-enabled=true" in text
    assert f"Exec={' '.join(autostart.daemon_command())}" in text

    assert autostart.remove(home=tmp_path) is True
    assert autostart.is_installed(home=tmp_path) is False
    assert autostart.remove(home=tmp_path) is True  # idempotent


def test_install_fails_closed_on_error(tmp_path, monkeypatch):
    # Any OS-level failure must degrade to False, never propagate.
    monkeypatch.setattr(sys, "platform", "linux")

    def boom():
        raise OSError("disk full")

    monkeypatch.setattr(autostart, "daemon_command", boom)
    assert autostart.install(home=tmp_path) is False  # no exception escapes
