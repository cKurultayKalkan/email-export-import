import sys

import pytest

from email_export_import.gui import updater
from email_export_import.gui.updater import UpdateInfo, check_for_update, is_newer, parse_version


def test_parse_version_variants():
    assert parse_version("v1.2.3") == (1, 2, 3)
    assert parse_version("1.2") == (1, 2, 0)
    assert parse_version("v2.0.0-beta1") == (2, 0, 0)
    assert parse_version("v0.1.0+build9") == (0, 1, 0)


def test_is_newer():
    assert is_newer("v0.2.0", "0.1.0")
    assert is_newer("v1.0.0", "0.9.9")
    assert not is_newer("v0.1.0", "0.1.0")
    assert not is_newer("v0.1.0", "0.2.0")


def _release(tag, assets):
    return {"tag_name": tag, "assets": assets}


def _asset(name, url="https://x/dl"):
    return {"name": name, "browser_download_url": url}


def test_check_returns_info_when_newer(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    release = _release("v0.2.0", [
        _asset("email-export-import-macos.zip", "https://x/mac.zip"),
        _asset("email-export-import-windows.zip"),
        _asset("SHA256SUMS.txt", "https://x/sums"),
    ])
    sums = "abc123  email-export-import-macos.zip\ndef456  email-export-import-windows.zip\n"
    info = check_for_update("0.1.0", fetch=lambda url: release, fetch_text=lambda url: sums)
    assert info == UpdateInfo(version="v0.2.0", asset_url="https://x/mac.zip",
                              asset_name="email-export-import-macos.zip", sha256="abc123")


def test_check_none_when_up_to_date(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    release = _release("v0.1.0", [_asset("email-export-import-macos.zip"),
                                  _asset("SHA256SUMS.txt")])
    assert check_for_update("0.1.0", fetch=lambda url: release, fetch_text=lambda url: "") is None


def test_check_none_when_no_platform_asset(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    release = _release("v0.2.0", [_asset("email-export-import-macos.zip"),
                                  _asset("SHA256SUMS.txt")])
    assert check_for_update("0.1.0", fetch=lambda url: release, fetch_text=lambda url: "x") is None


def test_check_none_on_fetch_error(monkeypatch):
    def boom(url):
        raise OSError("offline")

    assert check_for_update("0.1.0", fetch=boom, fetch_text=lambda url: "") is None


def test_check_none_when_sha_missing(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    release = _release("v0.2.0", [_asset("email-export-import-windows.zip"),
                                  _asset("SHA256SUMS.txt")])
    sums = "abc  some-other-file.zip\n"  # no line for the windows asset
    assert check_for_update("0.1.0", fetch=lambda url: release, fetch_text=lambda url: sums) is None


import hashlib
import io

from email_export_import.gui.updater import ChecksumMismatch, download_asset


def test_download_verifies_and_writes(tmp_path):
    payload = b"installer-bytes" * 1000
    sha = hashlib.sha256(payload).hexdigest()
    info = UpdateInfo(version="v0.2.0", asset_url="https://x/a",
                      asset_name="email-export-import-linux.zip", sha256=sha)
    dest = download_asset(info, tmp_path, opener=lambda url: io.BytesIO(payload))
    assert dest.read_bytes() == payload
    assert dest.name == "email-export-import-linux.zip"


def test_download_rejects_bad_checksum(tmp_path):
    payload = b"tampered"
    info = UpdateInfo(version="v0.2.0", asset_url="https://x/a",
                      asset_name="email-export-import-linux.zip", sha256="0" * 64)
    with pytest.raises(ChecksumMismatch):
        download_asset(info, tmp_path, opener=lambda url: io.BytesIO(payload))
    assert not (tmp_path / "email-export-import-linux.zip").exists()
