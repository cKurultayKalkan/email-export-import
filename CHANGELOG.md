# Changelog

All notable changes to this project are documented here.

## v0.1.13 — 2026-07-14

### Changed
- **Single-window desktop model, completed.** New migration, Bulk migration
  and the folder plan now open as modal dialogs over the main window instead
  of navigating to separate pages; resume reuses the same plan dialog. The
  app is one master-detail window with dialogs, the way a desktop tool
  behaves — no page stack, nothing to navigate "back" through.

## v0.1.12 — 2026-07-14

### Fixed
- **A run can no longer call itself done while messages are missing.** A
  field incident left 22 mails across two "completed" accounts silently
  behind a green tick (a mid-resume SELECT hiccup put three folders into
  the failure list, and the run finished "done" anyway). Now: a finished
  run with any failure lands in red "incomplete" with the counts and
  folders named, stays resumable, and an end-of-run completeness check
  proves every message seen at planning time was actually handled.
- **Oversized messages no longer stall forever**: APPEND now gets a socket
  timeout scaled to the message size (a 30 MB mail at the safety ceiling
  needs minutes, and some servers block while fsyncing — the fixed 60 s
  timeout kept killing and restarting the same upload).
- Honest progress numbers: restored runs count every handled message
  (including deduplicated and already-present ones), so a fully migrated
  mailbox reads 2621/2621, not 2611/2621.

### Added
- **Hard, non-configurable safety ceilings** against the macOS kernel
  panic seen under sustained upload with TSO + content-filter software:
  every connection writes in 64 KB slices with drain pauses (2 MB/s per
  connection), and all connections together share an 8 MB/s process-wide
  budget. No setting can raise these; the user rate limit only lowers them.
- **Results that survive restarts**: each run persists its outcome
  (migrated / skipped / failed with first failure lines); the side panel
  shows them plus a per-folder breakdown and the run's duration.

## v0.1.11 — 2026-07-13

### Changed
- **Desktop redesign, part one.** The dashboard-of-cards and separate detail
  page are replaced by a single desktop-style window: a dense migration list
  on the left (status dot, account pair, live progress per row), a properties
  panel for the selection on the right, a real menu bar (Migration / View /
  Help), an icon toolbar whose middle group follows the selected run's state,
  and a status bar (state left, version right). Settings now opens as a
  modal dialog; editing a run's connection opens a dialog editor.
- **Close truly backgrounds the app (macOS).** The close button hides every
  window *and* the Dock icon; the app lives on as the envelope icon in the
  menu bar (right side, near the clock) with a live status line, "Show
  window" and a guarded Quit. Transfers keep running the whole time.
- The macOS bundle is now named **Email Export Import Tool.app** (Finder
  shows the file name, not the display name).

### Fixed
- Rows restored from old state files (no total recorded) showed an endless
  "connecting"-style sweep animation and a `N / 0` counter.
- Dialog buttons (e.g. the cancel confirmation) could be unresponsive while
  live progress was updating: the 5x/second refresh now pauses whenever a
  modal dialog is open.
- Settings content scrolls on short windows instead of being cut off.

## v0.1.10 — 2026-07-13

### Fixed
- **The packaged desktop app now actually starts.** Three stacked packaging
  faults, uncovered layer by layer once macOS Gatekeeper stopped masking
  them, are fixed:
  - `flet` itself was never bundled (it lived in an optional extra, which
    `flet build` ignores) — the app died at `import flet`.
  - `certifi` was missing — the flet bootstrap imports it before anything
    else runs.
  - The app package shipped the entire repo directory (a second Python's
    `.venv`, the full `.git` history, tests, docs): 29 MB / 3,305 files where
    0.5 MB / 62 files of app source belongs. `flet build` now gets an
    explicit exclude list.
- Verified end to end with a local build before this release: fresh install
  launches, UI renders, no startup errors.

## v0.1.9 — 2026-07-13

### Changed
- **macOS builds are now code-signed and notarized.** CI signs every embedded
  binary with a Developer ID certificate (hardened runtime) and staples
  Apple's notarization ticket, so the download opens with no "damaged" /
  Gatekeeper warning — no `xattr` workaround needed. Windows builds remain
  unsigned for now (SmartScreen still warns once).

## v0.1.8 — 2026-07-13

### Added
- **Proper installers.** Windows releases now ship
  `email-export-import-windows-setup.exe` — a real installer (Start-menu
  shortcut, uninstaller, in-place upgrades, English + Turkish) that ends the
  "DLL not found" failures caused by running the exe outside its extracted
  folder. macOS releases ship `email-export-import-macos.dmg` (open, drag to
  Applications). The portable zips stay attached with unchanged names so
  pre-0.1.8 auto-update keeps working; the in-app updater now prefers the
  installer for its platform.

## v0.1.7 — 2026-07-13

### Added
- **Close-to-background**: closing the window while migrations are running (or
  queued) no longer kills them. The app asks whether to keep working in the
  background — it stays minimized in the Dock / task bar and clicking its icon
  restores the window — or to quit (progress is saved either way; resuming
  continues without duplicates). With nothing active, close quits as before.

## v0.1.6 — 2026-07-13

### Changed
- **New app icon**: an email envelope with export/import arrows replaces the
  default Flet icon on every platform.
- **New product name**: the app is now called **Email Export Import Tool**
  (bundle name and window title, in every language).

## v0.1.5 — 2026-07-13

### Fixed
- **macOS kernel panic during sustained bulk upload.** Long migrations could
  crash the whole machine (purple screen, `panic: m_copym_with_hdrs ... copy
  overflow`). Root cause: macOS auto-grows a socket's send buffer up to 4 MB
  and builds one enormous mbuf chain out of it; copying that chain in the
  kernel send path — aggravated by TSO and third-party content-filter
  software (observed with Check Point) — overflows. The app now pins each
  IMAP socket's send buffer (`SO_SNDBUF`) to 256 KB, which disables the
  auto-growth: uploads simply loop in buffer-sized pieces, on every platform,
  with no configuration and no measurable slowdown (the destination server is
  the bottleneck, not this buffer).
- Validated in production conditions: a 4,036-message migration ran 5 h 46 m
  of sustained TLS upload with a content filter active — zero failures, zero
  panics. The same workload previously panicked the machine three times.

## v0.1.4 — 2026-07-13

Controls to keep long transfers gentle on the host machine, added while
investigating the kernel panics fixed in v0.1.5:

### Added
- **Transfer speed limit** (Settings): caps upload throughput
  (10/5/2/1 MB/s or unlimited). Implemented as a virtual-time pacer shared
  across workers, so an oversized message can never deadlock the limiter.
- **Safe mode** (Settings): one connection, one transfer at a time, 2 MB/s —
  slowest but gentlest preset, applied with one click.
- **TSO warning on macOS**: when TCP Segmentation Offload is enabled, Settings
  shows what it is, why sustained bulk upload with it has been seen crashing
  the macOS kernel, and the command to turn it off.
- **Max simultaneous transfers** and **parallel connections per transfer**
  are now user-configurable and persisted across restarts.

## v0.1.3 — 2026-07-13

### Added
- **Bulk migration**: enter many accounts at once (same source provider, same
  destination server); transfers start in one go and queue automatically,
  never exceeding the "max simultaneous transfers" setting.
- **Sync new mail**: finished migrations get a button that re-runs the
  transfer and copies only messages that arrived since — nothing is
  duplicated.
- **Loading overlay**: a translucent layer with a spinner during connect and
  folder-scan phases, so the UI never appears frozen while working.

### Fixed
- **Dead buttons** (no hover cursor, no click ripple, no action). Root cause:
  UI updates were issued from worker threads, but Flet's update queue is only
  safe on its event loop — updates were silently lost. All UI mutation is now
  marshalled onto the event loop, and a regression test pins the behaviour.
  Buttons also show a pointer cursor on hover now.

### Performance
- **Resume no longer rescans**: already-migrated messages are skipped before
  any download. Resuming a half-finished migration now does work proportional
  to what is left, not to what was already done (previously a resume of a
  large mailbox re-checked every message one by one).

## v0.1.2 — 2026-07-12

### Added
- Cancelling a migration now asks for confirmation and explains that progress
  is kept.
- Cancelled runs are resumable: a cancelled migration keeps its state and can
  continue later from where it stopped, without duplicates.

## v0.1.1 — 2026-07-12

- Version bump only, to exercise the auto-update path end to end (an app on
  v0.1.0 is offered v0.1.1). No functional change.

## v0.1.0 — 2026-07-12

Initial release.

- IMAP-to-IMAP migration preserving folders, read/starred flags, dates and
  attachments.
- Desktop GUI (macOS/Linux/Windows) and CLI.
- Resume without duplicates: progress is checkpointed per message; running
  again continues where it left off.
- Optional disk spool: failed uploads retry from disk without re-downloading.
- Parallel transfer workers with per-operation socket timeouts, transparent
  reconnect and retry (including fail2ban-style login-ban backoff), and
  cancel-aware backoff so Cancel is always responsive.
- Self-signed certificate support with an explicit, informed-consent warning.
- Auto-update: the app checks GitHub Releases, downloads over HTTPS and
  verifies against SHA256SUMS before offering the update.
- CI builds signed-checksum bundles for macOS, Linux and Windows on every
  release tag.
