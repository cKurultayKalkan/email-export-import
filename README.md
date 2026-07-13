<div align="center">

<img src="assets/icon.png" alt="Email Export Import Tool icon" width="128">

# Email Export Import Tool

**Move a whole mailbox from one IMAP server to another — folders, read/starred
flags, original dates, and attachments preserved.**

[![Latest release](https://img.shields.io/github/v/release/cKurultayKalkan/email-export-import?label=release)](https://github.com/cKurultayKalkan/email-export-import/releases/latest)
[![Build](https://img.shields.io/github/actions/workflow/status/cKurultayKalkan/email-export-import/build-gui.yml?label=build)](https://github.com/cKurultayKalkan/email-export-import/actions/workflows/build-gui.yml)
[![Downloads](https://img.shields.io/github/downloads/cKurultayKalkan/email-export-import/total)](https://github.com/cKurultayKalkan/email-export-import/releases)
![Platforms](https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)

[**⬇ Download the app**](#download-the-app-no-coding-required) ·
[Documentation](#contents) ·
[Changelog](CHANGELOG.md) ·
[Report a bug](https://github.com/cKurultayKalkan/email-export-import/issues/new) ·
[Request a feature](https://github.com/cKurultayKalkan/email-export-import/issues/new)

</div>

---

A desktop app for everyone, and a scriptable CLI for developers. Interruptible
and resumable: run it again and it continues where it stopped, without
re-copying what already landed.

Built for real migrations: large mailboxes, rate-limiting servers, self-signed
certificates, and providers that hide their quirks (Gmail label folders,
Courier/`INBOX.` namespaces, app-password-only logins).

- **Two front-ends, one engine.** A scriptable CLI and a Flet desktop app share
  the same transfer engine and the same on-disk state — start a migration in one
  and finish it in the other.
- **Preserves structure.** Messages are copied byte-for-byte (RFC822), so MIME
  parts, attachments, and headers survive intact; flags and internal dates are
  re-applied; folder hierarchy is recreated (with delimiter and SPECIAL-USE
  mapping).
- **Resumable & dedup-safe.** Progress is written to disk continuously and keyed
  by Message-ID, so an interrupted run resumes without duplicating messages.
- **Concurrent.** Transfers run on parallel IMAP connections; big folders are
  chunked so even one huge folder parallelises.
- **Honest about risk.** Passwords are never written to disk; TLS is verified by
  default; certificate and quota problems surface with a clear explanation.

---

## Contents

- [Download the app (no coding required)](#download-the-app-no-coding-required)
- [Install from source (developers)](#install-from-source-developers)
- [Quick start (CLI)](#quick-start-cli)
- [Desktop app](#desktop-app)
- [What gets preserved](#what-gets-preserved)
- [Resume & safety](#resume--safety)
- [Provider & server notes](#provider--server-notes)
- [Performance](#performance)
- [Security](#security)
- [How it works](#how-it-works)
- [Development](#development)

---

## Download the app (no coding required)

Every release ships a ready-to-run desktop app for macOS, Windows, and Linux —
no Python, no terminal, nothing to install first.

**[⬇ Go to the latest release](https://github.com/cKurultayKalkan/email-export-import/releases/latest)**
and download the file for your system:

| Your system | Download | Then |
|---|---|---|
| **macOS** | `email-export-import-macos.dmg` | Open it, drag the app into **Applications** |
| **Windows** | `email-export-import-windows-setup.exe` | Run it — installs with a Start-menu shortcut and uninstaller |
| **Linux** | `email-export-import-linux.zip` | Unzip, run the `email-export-import` executable inside |

Portable zips (`-macos.zip`, `-windows.zip`) are also attached to every release
if you prefer no installer. **Extract the whole zip first** — running the exe
from inside the zip window (or copying it out alone) leaves its DLLs behind and
Windows reports "DLL not found".

**First launch.**

- **macOS**: opens with no warnings — the app is code-signed and notarized by
  Apple (from v0.1.9 on).
- **Windows**: the installer is not code-signed yet, so SmartScreen shows
  "Windows protected your PC" once — click **More info → Run anyway**.
- **Linux**: no warning; if the file isn't executable, `chmod +x` it. Requires
  GTK 3 (preinstalled on most desktops).

**Verify your download (optional).** Each release includes `SHA256SUMS.txt`;
compare with `shasum -a 256 <file>` (macOS/Linux) or
`certutil -hashfile <file> SHA256` (Windows).

**Updates are automatic.** The app checks GitHub Releases on demand
(Settings → Check for updates), downloads over HTTPS, and verifies the
checksum before offering to update — you never have to come back here.

## Install from source (developers)

Requires [git](https://git-scm.com/), Python 3.11+, and
[uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`).

```bash
git clone https://github.com/cKurultayKalkan/email-export-import.git
cd email-export-import

uv sync                # CLI only
uv sync --extra gui    # CLI + desktop app (adds Flet)
```

Two console scripts are installed into the project venv:
`email-export-import` (CLI) and `email-export-import-gui` (desktop). Run them
with `uv run`:

```bash
uv run email-export-import        # CLI wizard
uv run email-export-import-gui    # desktop app
```

## Quick start (CLI)

### Interactive CLI wizard

```bash
uv run email-export-import
```

Pick the old provider (Gmail, Outlook/Office365, Yahoo, iCloud, Yandex, or a
Custom host/port), enter the email address and an **app password**, repeat for
the new server, review the folder plan, and confirm. If a previous run was
interrupted, the wizard offers to resume it — you only re-enter the passwords.

### Non-interactive (scripts / CI)

Passwords come from environment variables so they never appear in `ps` or shell
history:

```bash
EEI_SRC_PASSWORD=... EEI_DST_PASSWORD=... uv run email-export-import \
  --src-preset gmail --src-email old@gmail.com \
  --dst-host imap.newserver.com --dst-email new@newserver.com \
  --workers 4 --yes
```

Useful flags: `--src-preset/--dst-preset`, `--src-host/--dst-host`,
`--src-port/--dst-port`, `--src-email/--dst-email`, `--skip "Folder A,Folder B"`,
`--workers N`, `--spool/--no-spool`, `--no-src-verify-ssl/--no-dst-verify-ssl`,
`--state-dir PATH`, `--yes`. Run `--help` for the full list.

## Desktop app

[Download it packaged](#download-the-app-no-coding-required) for your OS, or
run it from source:

```bash
uv sync --extra gui
uv run email-export-import-gui
```

The desktop app is a **dashboard of migrations**, not a one-shot wizard:

- **Run several migrations side by side.** Each is a card showing a live status
  badge (running / paused / done / error), a progress bar, and `N/M` counter.
  The dashboard updates continuously while you navigate.
- **Pause & resume.** Pause stops a run gracefully and saves its state; Resume
  reconnects (asking only for passwords) and continues without duplicates.
- **Choose folders right before transfer.** Resuming or starting a migration
  opens the plan screen with a checkbox per folder, plus worker count and the
  optional disk spool — so you decide exactly what moves.
- **Edit connections.** A migration's detail page shows its source and
  destination; for a paused run you can edit host / port / SSL / certificate
  verification and save it for the next resume.
- **Bulk migration.** Pick one source provider and one destination server, then
  add a row per account (email + both passwords) and start them all. Runs begin
  under a concurrency cap — the rest wait as **queued** cards and start
  automatically as slots free, so a rate-limiting server isn't hit by every
  login at once.
- **Completed migrations stay visible** as "done" cards — nothing silently
  disappears — and a done card offers **Sync new mail**, which re-runs the
  migration and copies only messages that arrived since. Nothing is duplicated.
- **Runs in the background.** Closing the window while migrations are running
  offers to keep them going: the app stays minimized in the Dock / task bar and
  clicking its icon brings the window back. Quitting instead is always safe —
  progress is saved and resume picks up without duplicates.
- **Settings page**: language (Turkish 🇹🇷 / English 🇬🇧, English by default),
  parallel connections per transfer, max simultaneous transfers, update check,
  and where your data is stored.

The desktop app reads and writes the **same state files as the CLI**, so the two
are interchangeable mid-migration.

> Packaged bundles are built by CI on every tag push and attached to the
> [release](https://github.com/cKurultayKalkan/email-export-import/releases).
> macOS bundles are code-signed and notarized; Windows bundles are unsigned
> for now.

## What gets preserved

| Preserved | How |
|---|---|
| Message content & attachments | Raw RFC822 body copied verbatim (`BODY.PEEK[]` → `APPEND`) |
| Read / starred / answered / draft flags | Fetched and re-applied on append (`\Recent` is never set) |
| Original date | Source `INTERNALDATE` passed to `APPEND` |
| Folder hierarchy | Recreated on the destination, with delimiter translation (`/` ↔ `.`) |
| Special folders (Sent/Drafts/Trash/Junk/Archive) | Matched by RFC 6154 SPECIAL-USE attributes, not just by name |

## Resume & safety

- **State** lives in `~/.email-export-import/state/` (override with
  `--state-dir`). It is written continuously and keyed by Message-ID, so
  re-running skips everything already copied.
- **Interrupt anytime** with Ctrl-C (CLI) or Pause (GUI) — at most the in-flight
  message is lost, and it is re-copied on resume.
- **Resume continues from where it stopped.** Each migrated message's UID is
  recorded, and finished UIDs are dropped from the work list *before* any fetch,
  so a resume costs work proportional to what is left — it does not re-scan the
  mailbox. If the server bumps `UIDVALIDITY` those UIDs become meaningless, so
  they're discarded and dedup falls back to Message-IDs (still no duplicates).
- **No duplicates across runs.** The one exception: if a connection drops in the
  *middle* of a single upload, that one message may be duplicated. Resume state
  prevents duplicates across runs, not within a dropped upload.
- **Deleting the state directory** makes the tool re-copy everything (duplicates
  on the destination). Keep it until you're satisfied the migration is complete.

## Provider & server notes

- **App passwords required.** Gmail, Outlook, Yahoo, iCloud, and Yandex reject
  normal passwords over IMAP. The wizard links to each provider's app-password
  page before asking.
- **Gmail label folders.** `[Gmail]/All Mail`, `[Gmail]/Important`, and
  `[Gmail]/Starred` are skipped by default — they are label views that would
  duplicate every message. Adjust with `--skip` or the plan screen.
- **Courier / `INBOX.` namespaces.** Servers that keep every folder under
  `INBOX.` (Courier, many cPanel/Roundcube hosts) are handled automatically: the
  destination NAMESPACE is read and created folders are prefixed accordingly.
- **Folder visibility.** Created destination folders are also SUBSCRIBEd —
  webmail such as Roundcube only lists subscribed folders, so without this a
  migrated folder would exist but stay invisible.
- **Login rate limits.** Servers that throttle logins (fail2ban / per-IP caps)
  can reject a reconnect even with the right password. Once a connection has
  authenticated successfully, a later rejection is treated as transient and
  retried with a long backoff. On such servers prefer a lower `--workers` count.
- **Self-signed certificates.** If a server's TLS certificate can't be verified,
  the wizard explains the risk and asks whether to continue; scripts pass
  `--no-src-verify-ssl` / `--no-dst-verify-ssl`. The connection stays encrypted
  but loses man-in-the-middle protection. Prefer fixing the server (a real
  certificate, or point Python at its CA with `SSL_CERT_FILE`); use the flags
  only for servers you own on networks you trust.
- **Quota.** If the destination fills up, the run aborts immediately with a clear
  message — free space and re-run to resume.

## Performance

- **Parallel connections.** Transfers use 4 workers by default (2 when another
  migration is already running, to spare rate-limited servers). Tune with
  `--workers N` (1 = serial, max 16). Large folders are split into chunks so a
  single huge folder still parallelises.
- **Disk spool (optional).** By default messages stream through memory — nothing
  touches disk. With `--spool` (or the plan-screen checkbox) each downloaded
  message is held in `~/.email-export-import/spool/` until its upload succeeds,
  so failed uploads are retried from disk next run without re-downloading. Only
  failed uploads accumulate; a clean run leaves the spool empty.
- **Socket timeout.** A 60-second per-read timeout turns a half-dead connection
  into a fast reconnect instead of an indefinite hang.

## Security

- Passwords are read from a masked prompt or `EEI_SRC_PASSWORD` /
  `EEI_DST_PASSWORD` and held in memory only — never written to state, spool,
  logs, or argv.
- State and spool directories are `0700`, files `0600`.
- TLS is verified by default on port 993; verification is only skipped when you
  explicitly opt in for a server you trust.

## How it works

The engine is UI-agnostic and each module has one job:

```
email_export_import/
  providers.py   provider presets (host/port/SSL, app-password hints, skip-lists)
  connection.py  IMAPClient wrapper: friendly errors, reconnect, retry/backoff
  folders.py     folder listing, delimiter translation, SPECIAL-USE mapping
  transfer.py    the engine: fetch → append, dedup, quota abort, per-message tolerance
  state.py       resume state: atomic JSON, Message-ID dedup, UIDVALIDITY invalidation
  spool.py       optional write-through disk spool for failed uploads
  models.py      dataclasses (Account, ProviderPreset, FolderPlan, TransferProgress)
  errors.py      typed exceptions
  cli.py         Typer/Rich interactive wizard + non-interactive flags
  gui/           Flet desktop app (dashboard, run manager, off-thread IMAP)
```

A migration copies each folder by fetching cheap metadata for the whole folder
once, then streaming message bodies one at a time (so a 50 MB attachment costs
50 MB of memory, not the whole folder), appending each verbatim with its flags
and date, and recording it in the resume state before moving on.

## Development

```bash
git clone https://github.com/cKurultayKalkan/email-export-import.git
cd email-export-import
uv sync --extra gui
uv run pytest            # full suite (209 tests, no network — uses an in-memory IMAP fake)
```

Tests are behavior-focused and network-free: an in-memory `FakeIMAPClient`
stands in for the server, and the desktop app has a headless end-to-end harness
that drives the real click handlers. Contributions follow TDD; see
`docs/superpowers/` for the design specs and implementation plans.
