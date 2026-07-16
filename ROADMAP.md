# Roadmap

Where **Email Export / Import Tool** is headed. This is a living document — we
tick items off as they ship, and **contributions are very welcome**. If you want
to pick something up, open an issue (or comment on an existing one) so we don't
double up, then send a PR.

**Legend:** ✅ shipped · 🚧 in progress · 📋 planned · 💡 idea / needs discussion ·
🙌 good first / help-wanted

The engine is deliberately UI-agnostic (one core, three front-ends: CLI, desktop
GUI, headless daemon), so most features below land in the engine once and surface
everywhere. See `CLAUDE.md` for architecture.

---

## ✅ Shipped

- ✅ IMAP → IMAP mailbox migration with **folders, attachments, read/flagged
  flags and original dates preserved**
- ✅ **Resume** an interrupted migration from where it stopped (Message-ID dedup —
  never re-copies a message)
- ✅ Quota-aware: stops cleanly when the destination is full
- ✅ Three front-ends sharing on-disk state: **CLI wizard**, **desktop app**
  (Flet), **headless background daemon** (migrations survive closing the window)
- ✅ Provider **presets** — Yandex, Gmail, Outlook, Yahoo, iCloud (host/port/SSL
  filled in)
- ✅ **Bulk** migration — many accounts at once, with a concurrency ceiling
- ✅ OS **keychain** integration for saved passwords (never written to disk in
  plaintext)
- ✅ Multi-platform packaged builds — Windows, macOS (signed + notarized), Linux
- ✅ Menu-bar / system-tray control with live per-migration progress
- ✅ Localization: **10 languages** (English, Turkish, Spanish, French, German,
  Portuguese, Russian, Chinese, Arabic, Hindi), **auto-detected from the OS**

---

## 🚧 In progress

- 🚧 Cross-platform desktop polish (macOS App-Translocation, Windows console /
  relaunch, first-click "Show window") — ongoing from real-world testing
- 🚧 Faster first launch — the daemon sidecar's `--onefile` self-extraction is
  slow on first run (worst on Windows + Defender); move to `--onedir`

---

## 📋 Planned

### Protocols, backup & restore
- 📋 **POP3 as a source** — for accounts that only offer POP3. INBOX-only, no
  folders/flags (protocol limitation) — surfaced with a clear warning 🙌
- 📋 **Export to local backup** — download a mailbox to standard `.mbox` / `.eml`
  so users can keep an offline copy 🙌
- 📋 **Import from local backup** — read `.mbox` / `.eml` / Maildir (and ideally
  Outlook `.pst`) and push it to an IMAP account 🙌
- 💡 Direct provider APIs where IMAP is limited or throttled (e.g. Gmail API,
  Microsoft Graph) — faster, higher limits, no app passwords

### Beyond email
- 📋 **Contacts** migration (CardDAV / vCard `.vcf`) 🙌
- 📋 **Calendar** migration (CalDAV / iCalendar `.ics`) 🙌

### In-app mailbox viewer & selective migration
- 📋 **Browse the mailbox in-app** — folders and messages, before/after a
  migration, without opening a separate mail client
- 📋 **Filter what gets migrated** — by folder, date range, sender/recipient,
  read/unread, flagged, size, has-attachment. "Move these, skip those." 🙌
- 💡 Saved filter presets (e.g. "last 2 years only", "skip Spam & Trash")

### Diagnostics & health
- 📋 **Mailbox health check** — connection + auth diagnostics, quota usage,
  folder counts, duplicate detection, oversized-attachment report 🙌
- 💡 Pre-migration **dry run / preview** — show exactly what would transfer (and
  how big) before committing
- 💡 Post-migration **verification report** — confirm every source message
  landed, export a summary

### Localization & accessibility
- ✅ **10 UI languages** — EN, TR, ES, FR, DE, PT, RU, ZH, AR, HI. New locales
  are just a JSON file 🙌 (**native-speaker review** of the machine-assisted
  translations is very welcome — open a PR against `locales/<code>.json`)
- ✅ **Auto-detect the UI language from the OS locale** on first launch, with a
  manual override in Settings
- 💡 Right-to-left (RTL) layout polish for Arabic

### Setup guides (docs)
Clear, screenshot-led guides — a huge amount of user pain is just provider setup:
- 📋 Yandex — **how to enable IMAP** access 🙌
- 📋 Yandex — **how to create an app password** 🙌
- 📋 Gmail — app password / (later) OAuth sign-in 🙌
- 📋 Outlook / Microsoft 365 — app password / modern auth 🙌
- 📋 iCloud — app-specific password 🙌
- 📋 Yahoo — app password 🙌

### Reliability & UX
- 💡 **OAuth2 sign-in** for Gmail & Outlook — skip app passwords entirely
- 💡 Bandwidth limit / **scheduling** (run overnight, pause on metered networks)
- 💡 Progress **ETA** and throughput display
- 💡 Migration history / audit log the user can export

---

## 💡 Ideas / discussion

Not committed — feedback wanted. Open an issue if any of these matter to you:

- Server-to-server without a local hop (where both endpoints allow it)
- Deduplicate an existing mailbox in place
- Scheduled / recurring sync (keep two accounts mirrored)
- Team / admin mode (migrate many users from a CSV)

---

## Contributing

- **Pick an item**, open or comment on an issue, then PR. 🙌 items are the
  friendliest starting points (a new locale or a setup guide needs no deep
  engine knowledge).
- Development is **test-first** — `uv run pytest` is the only gate, and it's
  network-free (see `tests/fakes.py`). Add a failing test, make it pass.
- New engine features should live in the UI-agnostic core so the CLI, GUI and
  daemon all get them.

This roadmap reflects current thinking and will change as we learn from real
migrations. Priorities are driven by what users actually hit — file an issue.
