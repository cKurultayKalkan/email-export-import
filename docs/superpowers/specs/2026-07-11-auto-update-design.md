# Auto-update + Release CI — Design Spec

**Date:** 2026-07-11
**Status:** Approved

## Purpose

Let the desktop app tell users when a newer version exists and install it with
one click, and make the CI publish signed-later, checksummed release bundles to
GitHub Releases on every version tag. Together these give a real update path for
non-technical users.

## Decisions

| Question | Decision |
|---|---|
| Update mechanism | Download the platform installer asset and open it (the OS installer completes). No silent self-replacement. |
| Version source | The git tag (`vX.Y.Z`) drives the release; the package `__version__` matches it. |
| Update check | Automatic on GUI startup (async, non-blocking, fails silently) plus a manual "Check for updates" button in Settings. |
| Integrity | The downloaded asset's SHA256 is verified against the release's `SHA256SUMS.txt` before it is opened. Mandatory. |
| Scope | GUI only. CLI users update via source/uv. |

## Part 1 — Release CI

`.github/workflows/build-gui.yml` gains, on tag `v*` push:

- `permissions: contents: write` (needed to create a Release).
- After the three matrix builds, compute a SHA256 for each produced bundle.
- Publish a single GitHub Release for the tag with all three bundles and a
  `SHA256SUMS.txt` (one `<sha256>␠␠<filename>` line per bundle) attached, using
  `softprops/action-gh-release`.
- The release body lists the assets; the tag name is the version.
- `workflow_dispatch` still builds (for dry-runs) but only a tag push publishes a
  Release.

Asset naming is stable per platform so the updater can select by suffix:
`*-macos.dmg` (or `.app.zip` if flet doesn't emit a dmg), `*-windows.exe` (or
`.msi`), `*-linux.AppImage`. The exact packaging is settled in the plan against
what `flet build` actually emits; the updater matches by `sys.platform` →
expected suffix.

## Part 2 — In-app updater

### `email_export_import/__init__.py`
- `__version__ = "0.1.0"` — the single source of truth, kept in sync with the tag
  and `pyproject`.

### `email_export_import/gui/updater.py` (headless, no flet import)

- `RELEASES_API = "https://api.github.com/repos/cKurultayKalkan/email-export-import/releases/latest"`
- `UpdateInfo(version: str, asset_url: str, asset_name: str, sha256: str)` — dataclass.
- `parse_version(tag: str) -> tuple[int, int, int]` — strips a leading `v`, splits
  on `.`, ignores pre-release suffixes.
- `is_newer(latest: str, current: str) -> bool` — semver tuple comparison.
- `platform_asset_suffix() -> str` — from `sys.platform`: `darwin` → `.dmg`,
  `win32` → `.exe`, else `.AppImage`.
- `check_for_update(current_version: str, *, fetch=urlopen_json, fetch_text=urlopen_text) -> UpdateInfo | None`
  — GETs the latest release; if its tag is newer than `current_version`, finds the
  asset whose name ends with the platform suffix, reads its SHA256 from the
  `SHA256SUMS.txt` asset, and returns `UpdateInfo`. Returns `None` when up to date,
  no matching asset, or on any network/parse error (never raises).
- `download_asset(info: UpdateInfo, dest_dir: Path, *, opener=urlopen_bytes) -> Path`
  — streams the asset to `dest_dir/asset_name`, computes SHA256 while writing,
  raises `ChecksumMismatch` (and deletes the partial file) if it doesn't match
  `info.sha256`; returns the path on success.
- `open_installer(path: Path) -> None` — `darwin`: `open`; `win32`:
  `os.startfile`; linux: `chmod +x` then `xdg-open` (AppImage).
- The `fetch`/`opener` seams exist so tests inject fakes; production defaults use
  `urllib.request` over HTTPS.

### GUI wiring (`gui/app.py`, `gui/views.py`)

- On startup, after the dashboard renders, an async `check_for_update(__version__)`
  runs through `async_ops.run_async` (marshalled back via the `ui()` helper). If it
  returns an `UpdateInfo`, a dismissible banner/dialog shows "Version X available"
  with **Update** and **Later**.
- **Update** → async `download_asset` (progress/spinner) → on success
  `open_installer` and show a "closing to install" note; on `ChecksumMismatch` or
  download error → error dialog.
- The Settings page shows the current version and a **Check for updates** button
  that runs the same check and reports "up to date" or shows the banner.
- All network/file work is off the UI thread; the UI never blocks and offline use
  is unaffected (a failed check simply shows nothing).

## Security

- Only HTTPS URLs from the GitHub API/releases are fetched.
- The installer is **never opened** unless its SHA256 matches the release's
  published checksum — a tampered or truncated download is rejected and deleted.
- The user always clicks **Update**; nothing downloads or opens automatically.
- No credentials or personal data are sent; the check is an anonymous GET.

## Testing

- **updater (headless, no network):** inject fake fetch/opener functions —
  newer/older/equal version detection; platform asset selection; SHA256 read from
  `SHA256SUMS.txt`; `download_asset` success and `ChecksumMismatch` (partial file
  removed); malformed/missing release returns `None` not an exception; a release
  with no matching-platform asset returns `None`.
- **version:** `parse_version`/`is_newer` edge cases (`v1.2.3`, missing `v`,
  pre-release suffix, unequal lengths).
- **GUI:** the Settings "current version" line and the update-available banner
  build headlessly (FakePage e2e): a stubbed `check_for_update` returning an
  `UpdateInfo` shows the banner with an Update button; returning `None` shows none.
- **CI:** YAML validates; the release step is present with `contents: write`.

## Out of scope (v1)

Silent self-replacement / auto-relaunch; delta/patch updates; update channels
(beta/stable); code signing and notarization (a separate release task — the
checksummed release path here is signing-ready).
