# Auto-update + Release CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The desktop app checks GitHub Releases for a newer version and installs it with one click (download + open, SHA256-verified); CI publishes checksummed platform bundles to GitHub Releases on version tags.

**Architecture:** A headless `gui/updater.py` (no flet) does the GitHub API check, verified download, and OS open, with injectable fetch/opener seams for tests. `gui/app.py` runs the check async on startup and from Settings. `build-gui.yml` gains a release job that publishes the bundles + `SHA256SUMS.txt`.

**Tech Stack:** Python 3.11+ stdlib (`urllib`, `hashlib`), Flet (GUI only), GitHub Actions, `softprops/action-gh-release`.

**Spec:** `docs/superpowers/specs/2026-07-11-auto-update-design.md`

## Global Constraints

- `updater.py` MUST NOT import flet (headless-testable); flet only in `app.py`/`views.py`.
- Release assets are per-platform ZIP archives named `email-export-import-macos.zip`, `email-export-import-windows.zip`, `email-export-import-linux.zip`, plus `SHA256SUMS.txt`. The updater matches by the `-<platform>.zip` suffix. (This is the plan's concrete choice over the spec's illustrative `.dmg/.exe/.AppImage`, made to match what `flet build` reliably emits; true signed installers are a later task.)
- Repo for the API: `cKurultayKalkan/email-export-import`.
- The installer/archive is NEVER opened unless its SHA256 matches the release's published checksum.
- The check runs off the UI thread and fails silently offline; nothing downloads/opens without a user click.
- `__version__` in `email_export_import/__init__.py` is the single version source, kept equal to `pyproject` version and the git tag.
- All tests via `uv run pytest`; GUI tests use `pytest.importorskip("flet")`.
- Commit after every task.

---

### Task 1: Package version constant

**Files:**
- Modify: `email_export_import/__init__.py`
- Test: `tests/test_version.py`

**Interfaces:**
- Produces: `email_export_import.__version__: str` (e.g. `"0.1.0"`), equal to the `[project] version` in `pyproject.toml`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_version.py`:

```python
import tomllib
from pathlib import Path

import email_export_import


def test_version_present_and_matches_pyproject():
    assert isinstance(email_export_import.__version__, str)
    assert email_export_import.__version__
    data = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())
    assert email_export_import.__version__ == data["project"]["version"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_version.py -v`
Expected: FAIL — `AttributeError: module 'email_export_import' has no attribute '__version__'`.

- [ ] **Step 3: Implement**

Set `email_export_import/__init__.py` to exactly:

```python
__version__ = "0.1.0"
```

- [ ] **Step 4: Run tests, full suite, commit**

Run: `uv run pytest tests/test_version.py -v` then `uv run pytest`
Expected: all PASS.

```bash
git add email_export_import/__init__.py tests/test_version.py
git commit -m "feat: add package __version__ constant"
```

---

### Task 2: Updater — version compare + release check

**Files:**
- Create: `email_export_import/gui/updater.py`
- Test: `tests/test_updater.py`

**Interfaces:**
- Consumes: stdlib only.
- Produces:
  - `updater.UpdateInfo(version: str, asset_url: str, asset_name: str, sha256: str)` — dataclass.
  - `updater.ChecksumMismatch(Exception)`.
  - `updater.parse_version(tag: str) -> tuple[int, int, int]`
  - `updater.is_newer(latest: str, current: str) -> bool`
  - `updater.platform_asset_suffix() -> str` — `-macos.zip` / `-windows.zip` / `-linux.zip` from `sys.platform`.
  - `updater.check_for_update(current_version: str, *, fetch=..., fetch_text=...) -> UpdateInfo | None` — never raises.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_updater.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_updater.py -v`
Expected: FAIL — `ModuleNotFoundError: ... updater`.

- [ ] **Step 3: Implement**

Create `email_export_import/gui/updater.py`:

```python
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
```

- [ ] **Step 4: Run tests, full suite, commit**

Run: `uv run pytest tests/test_updater.py -v` then `uv run pytest`
Expected: all PASS.

```bash
git add email_export_import/gui/updater.py tests/test_updater.py
git commit -m "feat: add updater version-check against GitHub Releases"
```

---

### Task 3: Updater — verified download

**Files:**
- Test: `tests/test_updater.py` (append; `download_asset`/`ChecksumMismatch` already implemented in Task 2)

**Interfaces:**
- Consumes: `UpdateInfo`, `download_asset`, `ChecksumMismatch` (Task 2).
- Produces: test coverage proving the download verifies SHA256 and rejects a mismatch.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_updater.py`:

```python
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
    assert not (tmp_path / "email-export-import-linux.zip").exists()  # partial removed
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_updater.py -k download -v`
Expected: 2 PASS (the implementation landed in Task 2; these lock the behavior). If either fails, fix `download_asset` — not the test.

Note: `io.BytesIO` is a context manager and supports `read(size)`, so it stands in for the HTTPS response object.

- [ ] **Step 3: Run full suite and commit**

Run: `uv run pytest`
Expected: all PASS.

```bash
git add tests/test_updater.py
git commit -m "test: cover verified asset download and checksum rejection"
```

---

### Task 4: GUI wiring — startup check + Settings

**Files:**
- Modify: `email_export_import/gui/app.py`
- Modify: `email_export_import/gui/views.py`
- Modify: `email_export_import/locales/en.json`, `email_export_import/locales/tr.json`
- Test: `tests/test_gui_e2e.py`

**Interfaces:**
- Consumes: `updater.check_for_update/download_asset/open_installer/UpdateInfo/ChecksumMismatch` (Tasks 2–3), `__version__`, `async_ops.run_async`, `I18n`.
- Produces:
  - `views.build_settings(...)` gains a `version: str` argument and a `on_check_update: Callable[[], None]` argument; renders the version and a "Check for updates" button.
  - `app.py`: startup async update check that shows an update dialog; Settings "Check for updates" button; the dialog's Update triggers a verified download then `open_installer`.
  - New locale keys (BOTH files): `settings.version`, `settings.check_updates`, `update.available`, `update.now`, `update.later`, `update.checking`, `update.up_to_date`, `update.downloading`, `update.failed`, `update.ready`.

- [ ] **Step 1: Update locale files**

Add to `email_export_import/locales/en.json`:

```json
{
  "settings.version": "Version",
  "settings.check_updates": "Check for updates",
  "update.available": "Version {version} is available.",
  "update.now": "Update",
  "update.later": "Later",
  "update.checking": "Checking for updates…",
  "update.up_to_date": "You are on the latest version.",
  "update.downloading": "Downloading update…",
  "update.failed": "Update failed — please download it manually from the releases page.",
  "update.ready": "The installer has been opened. Close this app to finish updating."
}
```

Add to `email_export_import/locales/tr.json`:

```json
{
  "settings.version": "Sürüm",
  "settings.check_updates": "Güncellemeleri denetle",
  "update.available": "{version} sürümü mevcut.",
  "update.now": "Güncelle",
  "update.later": "Sonra",
  "update.checking": "Güncellemeler denetleniyor…",
  "update.up_to_date": "En son sürümü kullanıyorsunuz.",
  "update.downloading": "Güncelleme indiriliyor…",
  "update.failed": "Güncelleme başarısız — lütfen sürümler sayfasından elle indirin.",
  "update.ready": "Kurulum açıldı. Güncellemeyi tamamlamak için uygulamayı kapatın."
}
```

- [ ] **Step 2: Extend build_settings (failing test first)**

In `tests/test_gui_app.py`, replace `test_build_settings_view` with:

```python
def test_build_settings_view():
    from email_export_import.gui import views
    from email_export_import.gui.i18n import I18n

    i18n = I18n(locale="en")
    noop = lambda *a, **k: None
    view = views.build_settings(i18n, "/home/x/.email-export-import", noop, noop,
                                version="1.2.3", on_check_update=noop)
    assert view.route == "/settings"
    labels = []

    def walk(c):
        v = getattr(c, "content", None) or getattr(c, "text", None) or getattr(c, "value", None)
        if isinstance(v, str):
            labels.append(v)
        for ch in getattr(c, "controls", []) or []:
            walk(ch)
    for c in view.controls:
        walk(c)
    assert any("1.2.3" in x for x in labels)
    assert i18n.t("settings.check_updates") in labels
```

Run: `uv run pytest tests/test_gui_app.py::test_build_settings_view -v`
Expected: FAIL — `build_settings() got an unexpected keyword argument 'version'`.

- [ ] **Step 3: Implement build_settings change**

In `email_export_import/gui/views.py`, change `build_settings`'s signature and body:

```python
def build_settings(
    i18n: I18n,
    data_dir: str,
    on_locale: Callable[[str], None],
    on_back: Callable[[], None],
    version: str = "",
    on_check_update: Callable[[], None] | None = None,
) -> ft.View:
    language = ft.Dropdown(
        label=i18n.t("settings.language"),
        value=i18n.locale,
        width=280,
        options=[
            ft.dropdown.Option("tr", "Türkçe"),
            ft.dropdown.Option("en", "English"),
        ],
        on_select=lambda e: on_locale(e.control.value),
    )
    controls: list[ft.Control] = [
        ft.Text(i18n.t("settings.title"), size=20, weight=ft.FontWeight.BOLD),
        language,
        ft.Divider(),
        ft.Text(f"{i18n.t('settings.version')}: {version}", size=13),
        ft.TextButton(
            i18n.t("settings.check_updates"),
            icon=ft.Icons.SYSTEM_UPDATE,
            on_click=lambda e: on_check_update() if on_check_update else None,
        ),
        ft.Divider(),
        ft.Text(i18n.t("settings.data_location"), weight=ft.FontWeight.BOLD, size=13),
        ft.Text(data_dir, size=12, selectable=True),
        ft.Text(i18n.t("settings.data_note"), size=12),
        ft.Row(
            [ft.TextButton(i18n.t("detail.back"), on_click=lambda e: on_back())],
            alignment=ft.MainAxisAlignment.END,
        ),
    ]
    return ft.View(route="/settings", controls=controls, padding=24, spacing=14)
```

- [ ] **Step 4: Wire the updater into app.py**

In `email_export_import/gui/app.py`:

Add imports near the top (with the other package imports):

```python
from pathlib import Path

from .. import __version__
from . import updater
```

Replace `show_settings` so it passes the version + check callback:

```python
    def show_settings() -> None:
        from ..state import DEFAULT_BASE_DIR

        page.views.clear()
        page.views.append(
            views.build_settings(
                i18n, str(DEFAULT_BASE_DIR), on_locale=set_locale,
                on_back=show_dashboard, version=__version__,
                on_check_update=lambda: _check_updates(manual=True),
            )
        )
        page.update()
```

Add the update logic (place it near `_show_error`):

```python
    def _check_updates(manual: bool) -> None:
        if manual:
            _info_dialog(i18n.t("update.checking"))
        run_async(
            lambda: updater.check_for_update(__version__),
            on_done=ui(lambda info: _on_update_checked(info, manual)),
            on_error=ui(lambda exc: _on_update_checked(None, manual)),
        )

    def _on_update_checked(info, manual: bool) -> None:
        if info is None:
            if manual:
                _info_dialog(i18n.t("update.up_to_date"))
            return
        page.show_dialog(
            ft.AlertDialog(
                modal=True,
                title=ft.Text(i18n.t("app.title")),
                content=ft.Text(i18n.t("update.available", version=info.version)),
                actions=[
                    ft.TextButton(i18n.t("update.later"), on_click=lambda e: page.pop_dialog()),
                    ft.FilledButton(i18n.t("update.now"), on_click=lambda e: _do_update(info)),
                ],
            )
        )

    def _do_update(info) -> None:
        page.pop_dialog()
        _info_dialog(i18n.t("update.downloading"))
        run_async(
            lambda: updater.download_asset(info, Path.home() / "Downloads"),
            on_done=ui(lambda path: _update_downloaded(path)),
            on_error=ui(lambda exc: _info_dialog(i18n.t("update.failed"))),
        )

    def _update_downloaded(path) -> None:
        updater.open_installer(path)
        _info_dialog(i18n.t("update.ready"))

    def _info_dialog(message: str) -> None:
        page.show_dialog(
            ft.AlertDialog(
                title=ft.Text(i18n.t("app.title")),
                content=ft.Text(message),
                actions=[ft.TextButton(i18n.t("update.later"), on_click=lambda e: page.pop_dialog())],
            )
        )
```

At the end of `_page_main`, after `show_dashboard()` and before/after `page.run_thread(poll)`, add the silent startup check:

```python
    show_dashboard()
    page.run_thread(poll)
    _check_updates(manual=False)
```

- [ ] **Step 5: Write the GUI e2e test**

Append to `tests/test_gui_e2e.py`:

```python
def test_update_banner_shown_when_newer_available(monkeypatch, tmp_path):
    from email_export_import.gui import app as app_module
    from email_export_import.gui import updater as updater_mod
    from email_export_import.gui.updater import UpdateInfo

    fake = UpdateInfo(version="v9.9.9", asset_url="https://x/a",
                      asset_name="email-export-import-macos.zip", sha256="abc")
    monkeypatch.setattr(updater_mod, "check_for_update", lambda *a, **k: fake)
    monkeypatch.setattr(app_module.updater, "check_for_update", lambda *a, **k: fake)

    page = _run_page()
    # startup check runs async and shows the update dialog
    assert _wait(lambda: page.dialog is not None
                 and "9.9.9" in _dialog_content(page.dialog)), \
        "update dialog not shown for a newer release"
    # the dialog offers Update
    labels = [lbl for lbl, _ in _clickables(page.dialog)]
    assert EN("update.now") in labels


def test_no_update_dialog_when_up_to_date(monkeypatch, tmp_path):
    from email_export_import.gui import app as app_module

    monkeypatch.setattr(app_module.updater, "check_for_update", lambda *a, **k: None)
    page = _run_page()
    # give the async startup check a moment; no dialog should appear
    import time as _t
    _t.sleep(0.3)
    assert page.dialog is None


def _dialog_content(dlg):
    content = getattr(dlg, "content", None)
    return getattr(content, "value", "") or ""
```

- [ ] **Step 6: Run tests, manual launch, full suite, commit**

Run: `uv run pytest tests/test_gui_e2e.py tests/test_gui_app.py tests/test_i18n.py -v`
Then: `uv run email-export-import-gui` (opens; check Settings shows the version + button; close).
Then: `uv run pytest`
Expected: all PASS (i18n parity green, e2e green).

```bash
git add email_export_import/gui/ email_export_import/locales/ tests/
git commit -m "feat: surface auto-update in the GUI (startup check + settings)"
```

---

### Task 5: Release CI — publish checksummed bundles

**Files:**
- Modify: `.github/workflows/build-gui.yml`

**Interfaces:**
- Consumes: the matrix build outputs.
- Produces: on a `v*` tag, a GitHub Release with `email-export-import-<platform>.zip` (×3) and `SHA256SUMS.txt`.

- [ ] **Step 1: Rewrite the workflow**

Replace `.github/workflows/build-gui.yml` with:

```yaml
name: build-gui
on:
  push:
    tags: ["v*"]
  workflow_dispatch:

permissions:
  contents: write

jobs:
  build:
    timeout-minutes: 60
    strategy:
      fail-fast: false
      matrix:
        include:
          - os: macos-latest
            target: macos
          - os: ubuntu-latest
            target: linux
          - os: windows-latest
            target: windows
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - name: Install Flutter (required by flet build)
        uses: subosito/flutter-action@v2
        with:
          channel: stable
      - name: Install Linux desktop build dependencies
        if: runner.os == 'Linux'
        run: |
          sudo apt-get update
          sudo apt-get install -y clang cmake ninja-build pkg-config libgtk-3-dev
      - name: Build
        run: |
          uv sync --extra gui
          uv run flet build ${{ matrix.target }} --project email-export-import
      - name: Package bundle
        shell: bash
        run: |
          cd build/${{ matrix.target }}
          zip -r "../../email-export-import-${{ matrix.target }}.zip" .
      - uses: actions/upload-artifact@v4
        with:
          name: bundle-${{ matrix.target }}
          path: email-export-import-${{ matrix.target }}.zip

  release:
    needs: build
    if: startsWith(github.ref, 'refs/tags/v')
    runs-on: ubuntu-latest
    steps:
      - uses: actions/download-artifact@v4
        with:
          pattern: bundle-*
          merge-multiple: true
      - name: Generate checksums
        run: sha256sum email-export-import-*.zip > SHA256SUMS.txt
      - name: Publish release
        uses: softprops/action-gh-release@v2
        with:
          files: |
            email-export-import-*.zip
            SHA256SUMS.txt
          generate_release_notes: true
```

Notes: builds are unsigned (signing is a later task). `windows` runners have `zip` via Git Bash (`shell: bash`). `sha256sum` output format (`<hash>␠␠<name>`) matches `updater._sha_for`.

- [ ] **Step 2: Validate the workflow**

Run: `uv run python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/build-gui.yml')); print('yaml ok')"`
(If PyYAML isn't present, review the indentation manually and note which you did.)
Expected: `yaml ok`, and by inspection: `permissions: contents: write`, a `release` job with `needs: build` gated on a tag, a `SHA256SUMS.txt` step, and `softprops/action-gh-release`.

- [ ] **Step 3: Full suite, commit**

Run: `uv run pytest`
Expected: all PASS (no code changed).

```bash
git add .github/workflows/build-gui.yml
git commit -m "ci: publish checksummed release bundles to GitHub Releases on tags"
```

---

## Post-merge operational note

The release workflow has never run on a real runner. Before advertising updates:
tag a test version (e.g. `v0.1.1`) or use `workflow_dispatch`, confirm all three
bundles build and the Release is created with `SHA256SUMS.txt`, then verify the
in-app check finds it. Bump `__version__` and `pyproject` version together with
each tag.
