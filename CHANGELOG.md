# Changelog

All notable changes to this project are documented here.

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
