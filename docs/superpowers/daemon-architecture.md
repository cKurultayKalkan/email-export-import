# Daemon architecture — migrations independent of the GUI

## Problem
Today the GUI process owns the transfer engine. Closing the window can only
minimize/hide the same process; if the app quits, transfers stop. The user
expects a real background service: transfers keep running with **no window,
no Dock icon, no visible process**, and reopening the app just reconnects to
what is already happening.

## Shape

```
┌────────────────────────┐        localhost HTTP + token        ┌───────────────────────────┐
│  GUI (flet, packaged)  │ ───────────────────────────────────▶ │  eei-daemon (headless)    │
│  – dashboard/views     │   /runs /start /pause /resume ...    │  – RunManager + engine    │
│  – no engine inside    │ ◀─────────────────────────────────── │  – owns IMAP connections  │
└────────────────────────┘         JSON snapshots               │  – state dir as today     │
        CLI ────────────────────────────────────────────────────▶ same API (or standalone)  │
                                                                └───────────────────────────┘
```

- **eei-daemon**: a separate headless binary (PyInstaller, console-less) built
  in CI per OS and shipped inside the app bundle (macOS: `Contents/Resources/`,
  Windows: install dir). Contains engine + state only — no flet.
- **Transport**: HTTP on `127.0.0.1:<random port>`; port + a random token are
  written 0600 into the state dir. Every request must carry the token.
  (Simple, debuggable, cross-platform; no firewall prompts for loopback.)
- **Passwords** never touch disk (unchanged): the GUI sends them per
  start/resume request; the daemon holds them in memory. Daemon restart →
  resume asks for passwords again, exactly like an app restart today.

## API (v1)

| Endpoint | Body → Result |
|---|---|
| `GET  /runs` | → list of RunSnapshot JSON |
| `POST /test-connection` | account → ok/kind/message |
| `POST /plan` | src+dst accounts (+skip) → folder plan |
| `POST /runs` | accounts+plan+workers+spool → run key (start) |
| `POST /runs/{key}/pause` · `/resume` · `/cancel` · `/sync` | |
| `DELETE /runs/{key}` | dismiss |
| `GET/PUT /settings` | max_active, workers, rate_limit |
| `POST /shutdown` | stop when idle (used by "Quit completely") |

## GUI side
`DaemonClient` implements the same surface the views use today
(`snapshot_all`, `active_count`, pause/resume/cancel...). `app.py` swaps
`RunManager` for the client; the 0.2s poll polls `GET /runs`. Controller's
connect/plan moves server-side; the wizard posts credentials instead of
opening IMAP connections in-process.

## Lifecycle
- GUI start: read port file → ping → not running? spawn the bundled daemon.
- Window closed: **GUI process exits entirely.** Daemon keeps transferring.
  The menu-bar icon lives in the *daemon*? No — status items need a UI loop.
  V1: menu-bar presence comes from the GUI in accessory mode OR we accept:
  no window = nothing visible, reopening the app shows live state. (Decision
  point below.)
- **Start at login: always on** (user decision 2026-07-13). First GUI launch
  installs the login item (macOS: launchd agent plist in
  `~/Library/LaunchAgents`; Windows: HKCU Run key; Linux: XDG autostart).
  Settings shows it with an off switch for the exceptional user.
- Daemon stays resident (no idle exit) — it is the thing that makes
  "background" true.
- While the daemon runs, the **menu-bar icon** is the visible handle
  (user decision): status line, "Pencereyi göster", "Tamamen çık"
  (shuts daemon down too).

## CI / packaging
- New job step: `pyinstaller --onefile` for the daemon per OS; macOS binary
  goes through the same codesign+notarize pass (it is inside the bundle, the
  existing sign-everything step picks it up).
- CLI keeps working standalone (imports engine directly) — no regression.

## Test strategy (TDD)
- Engine untouched → existing 200+ tests stand.
- Daemon: API tests with the in-memory IMAP fake (spawn app in-process via
  test client, no real socket needed for logic; one socket smoke test).
- GUI: FakeDaemonClient in the headless harness mirrors today's FakePage
  pattern; e2e tests keep driving real handlers.

## Build status (2026-07-14)

Done and tested (20 daemon tests, all green):
- `daemon/server.py` — RunManager + Controller behind a token-guarded
  loopback `ThreadingHTTPServer`. Endpoints: `GET /ping /runs /settings`;
  `POST /settings /shutdown /plan /start` and
  `/runs/<key>/{pause,cancel,dismiss}`. `/plan` connects with in-memory
  credentials and holds the live connections; `/start` turns a held plan
  into a running Run.
- `daemon/client.py` — `DaemonClient` (read surface mirrors RunManager +
  controls + plan/start).
- `daemon/__main__.py` — `python -m email_export_import.daemon`: random
  loopback port + token written 0600 to `daemon.json` (rendezvous), loads
  resumable/completed runs, serves until SIGTERM/`/shutdown`. Honours
  `EEI_BASE_DIR`.
- `daemon/lifecycle.py` — `connect_or_spawn()`: reuse live daemon, respawn
  on stale rendezvous, cold-spawn otherwise; frozen build execs the
  `eei-daemon` sidecar.

Not done yet (the remaining integration, best done as its own session with
the user testing each step — it changes the app's core runtime model, so
it must not destabilise the working GUI the pending migrations depend on):
1. **GUI swap** — app.py still uses an in-process RunManager. Replace with
   `connect_or_spawn()` + DaemonClient; poll `GET /runs`; wizard/resume/bulk
   post credentials to `/plan`+`/start` instead of opening IMAP in-process.
   Passwords stay per-request (keychain autofill already in place).
2. **PyInstaller sidecar** — `pyinstaller --onefile` the daemon per OS in
   CI, drop `eei-daemon` into the bundle (macOS `Contents/MacOS`, Windows
   install dir); the existing macOS sign-everything step must cover it.
3. **Autostart at login** — install a launchd agent / HKCU Run key on first
   GUI launch (user chose always-on, Settings can disable).

## Risks
- PyInstaller binary size (~15-25 MB/OS) and one more moving part in CI.
- Two processes to keep honest about versions (daemon refuses mismatched
  client with a clear error → GUI offers restart).
