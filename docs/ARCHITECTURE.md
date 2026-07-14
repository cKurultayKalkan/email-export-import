# Architecture

A reference to how the tool is put together. It is deliberately generic — no
accounts, servers, or deployment specifics — so it stays true as those change.

## What it is

An IMAP-to-IMAP mailbox migrator that preserves folders, read/starred flags,
original dates and attachments. Two front-ends — a scriptable CLI and a Flet
desktop app — share one transfer engine and one on-disk state format, so a
migration started in one can be finished in the other.

## Layout

```
email_export_import/
  providers.py    provider presets (host/port/SSL, app-password hints, skip-lists)
  connection.py   IMAPClient wrapper: friendly errors, reconnect, retry/backoff,
                  and the upload safety ceilings (see below)
  folders.py      folder listing, delimiter translation, SPECIAL-USE mapping
  transfer.py     the engine: fetch → append, dedup, quota abort, per-message
                  tolerance, end-of-run completeness check
  state.py        resume state: atomic JSON, Message-ID + UID dedup, UIDVALIDITY
                  invalidation, per-run statistics
  spool.py        optional write-through disk spool for failed uploads
  secrets_store.py optional OS-keychain password storage (opt-in)
  models.py       dataclasses (Account, ProviderPreset, FolderPlan, TransferProgress)
  errors.py       typed exceptions
  cli.py          Typer/Rich interactive wizard + non-interactive flags
  gui/            Flet desktop app
    app.py           the single-window UI, coded against one Backend interface
    views.py         view/dialog builders (master-detail, wizard, plan, settings)
    run_manager.py   Run lifecycle + RunSnapshot (in-process orchestration)
    controller.py    connect + build-plan against live IMAP connections
    local_backend.py  Backend impl: in-process (wraps RunManager + Controller)
    daemon_backend.py Backend impl: talks to the daemon over HTTP
    updater.py       GitHub-Releases auto-update (verified download)
  daemon/         headless out-of-process daemon
    server.py        RunManager behind a loopback HTTP + token API
    client.py        DaemonClient (the HTTP client)
    lifecycle.py     connect-or-spawn; sidecar-vs-source command resolution
    autostart.py     run-at-login install/remove (launchd / HKCU Run / XDG)
    __main__.py      the daemon entry: rendezvous file, serve loop
```

## The migration engine (`transfer.py`)

Each folder is migrated by fetching cheap metadata for the whole folder once,
then streaming message bodies one at a time (so a 50 MB attachment costs 50 MB
of memory, not the whole folder), appending each verbatim (RFC822) with its
flags and internal date, and recording it in the resume state before moving on.

- **Resume is proportional to what's left.** Already-migrated UIDs are dropped
  from the work list *before* any fetch. A UIDVALIDITY bump invalidates UID
  records and dedup falls back to Message-IDs.
- **A run cannot report "done" while messages are missing.** Any per-message
  failure, plus an end-of-run completeness check (every planned UID must be
  accounted for), forces an `error`/`incomplete` status that stays resumable —
  never a green tick over a short migration.
- **Honest counts.** Progress counts every *handled* message (migrated,
  deduplicated, already-present, or vanished-at-source), so a fully migrated
  mailbox reads N/N rather than undershooting by the skip margin.

## Upload safety ceilings (`connection.py`)

Sustained bulk TLS upload can drive the OS network send path into a kernel
panic on some machines (large auto-grown socket buffers building one oversized
mbuf chain, aggravated by TCP segmentation offload and content-filter
software). Two independent, **non-configurable** layers prevent it:

1. `SO_SNDBUF` is pinned per socket, disabling send-buffer auto-growth.
2. Every write goes out in small slices with a drain pause after each — a hard
   per-connection rate ceiling — and all connections share a single
   process-wide byte budget on top, so many concurrent transfers can't
   multiply into a flood.

The user-facing rate limit can only lower these further; nothing raises them.
A migration tool must never be able to take the host down.

## State & security model

- **Passwords are never written to disk by the engine.** They live in memory
  only; state and spool files carry no credentials and are safe to copy.
- **Optional keychain storage** (`secrets_store.py`) is opt-in per account and
  uses the OS secure store — macOS via the built-in `security` tool, Windows
  Credential Manager / Linux Secret Service via `keyring`. It fails closed:
  no secure store → the app just keeps prompting.
- TLS is verified by default; verification is only skipped on explicit,
  informed opt-in for a server the user trusts.
- Directories are `0700`, files `0600`.

## The GUI backend abstraction

`app.py` never touches a RunManager or a live IMAP connection directly. It
codes against **one Backend interface** with two implementations that expose
an identical method surface (parity is enforced):

- **LocalBackend** — in-process: wraps RunManager + Controller, holds live
  connections itself keyed by a plan id between `plan()` and `start()`.
- **DaemonBackend** — talks to the out-of-process daemon over HTTP; snapshots
  arrive as JSON and are reconstructed into the same objects the views expect.

At startup `_make_backend` prefers the daemon (via `connect_or_spawn`) and
falls back to LocalBackend if it can't be reached, so the app always works.

## The daemon

A headless process that owns the RunManager so migrations outlive the GUI.

- **Transport:** a standard-library HTTP server bound to `127.0.0.1` on a
  random port, guarded by a per-run token. Port + token are written `0600` to
  a rendezvous file in the state dir; the GUI reads it to connect.
- **API:** `GET /ping /runs /settings`; `POST /settings /plan /start
  /test-connection /placeholder /shutdown` and
  `/runs/<key>/{pause,cancel,dismiss,config,fail}`. Credentials cross only in
  `/test-connection` and `/plan` bodies, over loopback, and are held in memory.
- **Lifecycle:** `connect_or_spawn` reuses a live daemon, respawns on a stale
  rendezvous, or cold-spawns. The launch command is the bundled `eei-daemon`
  sidecar when it exists next to the executable, else `python -m
  email_export_import.daemon` from source.
- **Autostart:** installed at login (launchd agent / HKCU Run / XDG autostart);
  a Settings switch governs it.
- **Close vs quit:** closing the window hides to the menu-bar icon and the
  daemon keeps migrating; a full quit shuts the daemon down too.

## Packaging & CI

`flet build` produces the desktop bundle per OS. CI additionally builds the
daemon as a one-file PyInstaller binary (`eei_daemon.py` entry) and places it
next to the app executable. On macOS every embedded Mach-O — including the
sidecar and the packaged Python — is signed inside-out with a hardened runtime
and notarized, so downloads open without a Gatekeeper warning. Windows ships a
setup installer; portable zips are attached too. Auto-update checks GitHub
Releases, downloads over HTTPS and verifies SHA256 before offering the update.

## Testing

Behavior-focused and network-free: an in-memory `FakeIMAPClient` stands in for
the server. The desktop app has a headless end-to-end harness that drives the
real click handlers (catching wiring/threading bugs the unit tests can't); the
daemon suites exercise the real HTTP layer over a loopback socket; backend
parity is asserted so the two implementations can't drift.
