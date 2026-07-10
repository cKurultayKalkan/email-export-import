# Desktop GUI (Flet) — Design Spec

**Date:** 2026-07-10
**Status:** Approved
**Builds on:** `2026-07-09-email-migration-cli-design.md` (engine is reused as-is)

## Purpose

A cross-platform desktop application for the existing IMAP→IMAP migration
engine, aimed at non-technical end users. Same guarantees as the CLI
(folder/flag/date/attachment preservation, resume, dedup) behind a
point-and-click wizard.

## Decisions

| Question | Decision |
|---|---|
| Audience | Non-technical end users (distributable product) |
| Platforms | Windows, macOS, Linux |
| Framework | Flet (Flutter renderer, pure Python) — engine imported directly |
| Mobile | Out of scope v1; Flet can build apk/ipa later from the same code, with OS background-execution caveats documented |
| UI language | Turkish + English (JSON dictionaries, system-locale default, manual toggle) |
| Repo | Same repo, `email_export_import/gui/` package |
| Passwords | Never stored (same policy as CLI); typed per run |

## Architecture

```
email_export_import/
  (engine: unchanged, except one addition — see Engine Touch Points)
  gui/
    __init__.py
    app.py         # flet entry point, screen routing
    controller.py  # GUI ↔ engine bridge: background thread, event queue
    i18n.py        # t(key, **fmt), locale detection, manual toggle
    views.py       # screen builders (welcome/account/plan/progress/done)
  locales/
    tr.json
    en.json
```

- `flet` is an **optional dependency**: `[project.optional-dependencies] gui = ["flet>=0.24"]`. CLI installs stay lean.
- New console script: `email-export-import-gui = "email_export_import.gui.app:main"`.
- Views are thin (layout only); all decisions and engine calls live in
  `controller.py`, which is unit-testable without a display.

## Screen Flow

1. **Welcome** — lists unfinished sessions via `MigrationState.list_resumable()`
   (same state files as the CLI — a migration started in the CLI can be
   resumed in the GUI and vice versa). Buttons: *Resume* (asks only the two
   passwords) / *New migration*.
2. **Source account** — preset dropdown (Gmail/Outlook/Yahoo/iCloud/Yandex/
   Custom), host/port/SSL fields (pre-filled by preset, editable), email,
   password, app-password hint text. *Test connection* button. Certificate
   verification failure opens a dialog with the same risk explanation as the
   CLI and an explicit opt-in to continue unverified.
3. **Destination account** — same form; email pre-filled from source.
4. **Plan** — folder table (source → destination, message counts) built from
   `build_folder_plan` with namespace prefix; checkbox per folder to skip
   (preset skip-list pre-checked); workers selector (1–16, default 4);
   total message count.
5. **Progress** — overall bar + `N/M` counter + current folder name; *Cancel*
   button (graceful: sets the cancel event, state stays consistent, session
   remains resumable).
6. **Done** — migrated/skipped/failed summary; failure list; quota and
   interrupt outcomes explained with "you can resume later" hint.

## Controller / Threading

- `migrate()` runs in a worker thread started by the controller.
- Progress events flow through a `queue.Queue`; the UI drains it on a timer
  (~100 ms) and updates widgets in batches — 8k+ `on_message` events must
  not flood the Flet update loop.
- Controller exposes: `list_sessions()`, `test_connection(account)`,
  `build_plan(...)`, `start(plan, workers, on_event)`, `cancel()`.
- Errors surface as typed events (`ConnectionFailed`, `AuthFailed`,
  `CertificateVerifyFailed`, `QuotaExceeded`) that views map to dialogs.

## Engine Touch Points

Exactly one engine change: `transfer.migrate()` gains an optional
`cancel: threading.Event | None = None` parameter. Workers already check an
internal stop event per message; the external event is OR'ed into that
check. No other engine code changes.

## i18n

- `i18n.t(key, **fmt)` reads from the active locale dict; missing key falls
  back to English, then to the key itself.
- Locale default from the OS (`locale.getlocale()`), manual toggle in the
  UI header, persisted in `~/.email-export-import/gui.json`.
- A test asserts `tr.json` and `en.json` have identical key sets.

## Packaging & Distribution

- `flet build macos | windows | linux` in a GitHub Actions matrix produces
  dmg / exe / AppImage artifacts per release tag.
- v1 ships unsigned builds for testing; Apple notarization and Windows code
  signing are release-blocking tasks tracked separately (certificates
  required).

## Testing

- **Controller:** unit tests with the existing `FakeIMAPClient` fixtures —
  session listing, connection testing (including cert-failure path), plan
  building, start/cancel lifecycle, event batching.
- **i18n:** key-parity test between locales; formatting smoke test.
- **Views:** kept thin; no UI automation in v1 (Flet test tooling is
  immature). Manual smoke checklist in the plan's final task.
- Engine change (cancel event) gets an engine-level test: cancel mid-run →
  workers stop, state flushed, session resumable.

## Out of Scope (v1)

Mobile builds, auto-update, keyring storage, OAuth2, code signing
(tracked as release task), localization beyond TR/EN.
