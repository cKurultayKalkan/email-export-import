"""GitHub-Releases auto-update: version check, verified download, OS open.

Headless (no flet). Network/file work goes through injectable seams so tests
run without a network; production defaults use urllib over HTTPS.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

RELEASES_API = (
    "https://api.github.com/repos/cKurultayKalkan/email-export-import/releases/latest"
)


class ChecksumMismatch(Exception):
    """A downloaded asset's SHA256 did not match the published checksum."""


@dataclass
class UpdateInfo:
    version: str
    asset_url: str
    asset_name: str
    sha256: str


def parse_version(tag: str) -> tuple[int, int, int]:
    core = tag.lstrip("vV").split("-")[0].split("+")[0]
    parts = (core.split(".") + ["0", "0", "0"])[:3]
    out = []
    for p in parts:
        digits = "".join(c for c in p if c.isdigit())
        out.append(int(digits) if digits else 0)
    return out[0], out[1], out[2]


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


def platform_asset_suffix() -> str:
    if sys.platform == "darwin":
        return "-macos.zip"
    if sys.platform == "win32":
        return "-windows.zip"
    return "-linux.zip"


def _urlopen_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 (https only)
        return json.loads(r.read().decode())


def _urlopen_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=15) as r:  # noqa: S310
        return r.read().decode()


def _urlopen_bytes(url: str):
    return urllib.request.urlopen(url, timeout=60)  # noqa: S310


def _sha_for(sums_text: str, name: str) -> str | None:
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == name:
            return parts[0].lower()
    return None


def check_for_update(
    current_version: str, *, fetch=_urlopen_json, fetch_text=_urlopen_text
) -> UpdateInfo | None:
    try:
        release = fetch(RELEASES_API)
        tag = release.get("tag_name", "")
        if not tag or not is_newer(tag, current_version):
            return None
        assets = release.get("assets", [])
        suffix = platform_asset_suffix()
        asset = next((a for a in assets if a.get("name", "").endswith(suffix)), None)
        sums = next((a for a in assets if a.get("name") == "SHA256SUMS.txt"), None)
        if asset is None or sums is None:
            return None
        sha = _sha_for(fetch_text(sums["browser_download_url"]), asset["name"])
        if sha is None:
            return None
        return UpdateInfo(
            version=tag,
            asset_url=asset["browser_download_url"],
            asset_name=asset["name"],
            sha256=sha,
        )
    except Exception:
        return None


def download_asset(info: UpdateInfo, dest_dir: Path, *, opener=_urlopen_bytes) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / info.asset_name
    h = hashlib.sha256()
    with opener(info.asset_url) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            h.update(chunk)
            f.write(chunk)
    if h.hexdigest().lower() != info.sha256.lower():
        dest.unlink(missing_ok=True)
        raise ChecksumMismatch(f"SHA256 mismatch for {info.asset_name}")
    return dest


def open_installer(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    elif sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
    else:
        subprocess.run(["xdg-open", str(path)], check=False)
