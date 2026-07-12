# Bulk migration — design

**Date:** 2026-07-12
**Status:** approved

## Goal

Let a user migrate several mailboxes in one action: enter many accounts that
share one source provider and one destination server, then start all of them.
Runs start under a concurrency cap so a rate-limiting destination (the live
Courier server) is not hit by every login at once.

## Decisions (user-approved)

- **Entry shape:** shared source provider + shared destination server chosen
  once; a row per account carries `email`, source password, dest password
  (dest email defaults to the source email).
- **Concurrency:** capped auto-queue. At most `cap` runs active at a time
  (default 2); the rest wait as queued cards and auto-start when a slot frees.
  `cap` is adjustable in Settings and persisted.
- **Folders:** auto — migrate all folders except the source preset's defaults
  (Trash/Spam). No per-account plan screen. Folders can still be excluded
  per-run afterward via a card's detail page.

## Components

### 1. `AccountSpec`
`dataclass(src: Account, dst: Account)` with derived `key` (`src.email__dst.email`)
and `title` (`src.email → dst.email`). One per row. App-layer (gui) type.

### 2. `views.build_bulk(i18n, presets, on_start, on_back) -> (View, handles)`
- Source: provider preset dropdown + host/port/ssl (auto-filled from preset,
  editable). Destination: host/port/ssl/verify entered once.
- Dynamic rows: `email`, source password, dest password; `+ add row` and a
  remove control per row. Buttons: **Start all**, **Back**.
- `handles.collect() -> list[AccountSpec]`: parses/validates the shared source
  and dest fields plus each row; blank rows are skipped; invalid port or empty
  required fields raise a visible validation error (no runs started).
- `handles.preset_key()` — the chosen source preset (for auto-skip defaults).
- The preset auto-fill logic currently inside `build_account` is extracted into
  a shared helper reused by both `build_account` and `build_bulk` (only
  refactor in scope; the bulk screen genuinely needs it).

### 3. Bulk coordinator (in `app.py`)
State: `bulk_pending: list[AccountSpec]`, `bulk_starting: set[str]`.

- **Start all** (`start_bulk(specs, preset_key)`): for each spec add a queued
  placeholder `Run` to the manager (cards appear immediately), store the specs
  in `bulk_pending`, and return to the dashboard.
- **`pump_bulk()`** — called from the existing poll tick (already on the event
  loop): while `manager.active_count() + len(bulk_starting) < manager.max_active`
  and `bulk_pending` is non-empty: pop a spec, add its key to `bulk_starting`,
  and `run_async(connect_and_plan(spec, preset_key), on_done=ui(...), on_error=ui(...))`.
  - `connect_and_plan`: `controller.test_connection(src)` +
    `controller.test_connection(dst)` + `controller.build_plan(src, dst, skip)`
    where `skip = controller.default_skip(preset_key)`. Runs off-loop.
  - on success: build the real `Run` (open conns + plan + auto skip),
    `manager.add(run)` (replaces the placeholder — placeholder is not active),
    `run.start()`, discard from `bulk_starting`.
  - on error: `placeholder.mark_failed(message)` so the card shows the error;
    discard from `bulk_starting`. Other specs are unaffected.

Cap counts active **plus** in-flight connects (`bulk_starting`), so no more than
`cap` logins happen concurrently.

### 4. `Run.mark_failed(message)`
Small method: set `_status = "error"`, `_error = ("fatal", message)` under the
lock. Used to fail a queued placeholder whose connect/plan failed.

### 5. Concurrency cap
`RunManager.max_active: int` (default 2). Settings gains a dropdown (1–4) that
updates it live and persists to `gui.json` alongside the locale.

## Data flow

Start all → N queued cards + `bulk_pending`. Each poll tick pumps up to `cap`
connects; each connected spec becomes a running card; on finish the next queued
spec is pumped. Everything downstream reuses `Run` / `RunManager` / `transfer`
unchanged.

## Error handling

- Per-account connect/plan failure → that card errors (message visible); others
  continue.
- Duplicate `src→dst` already active → spec skipped, existing card highlighted
  (same guard as `start_migration`).
- Passwords held in memory only for the lifetime of the connect; never written
  to disk (consistent with the no-store policy). Placeholders never flush, so a
  failed account leaves no state file.

## Testing

- **Unit** (`build_bulk.collect()`): valid rows → specs; blank rows skipped;
  invalid port / missing field → validation error, no specs.
- **e2e** (FakePage): add 2 rows → Start all → with `cap=2` both migrate;
  with `cap=1` one runs while the other stays queued, then both complete.
- **Coordinator**: `pump_bulk` never exceeds `cap` active+starting.

## Out of scope

Fully independent per-row servers, CSV import, "Test all" pre-flight, retry of
failed accounts (user re-adds the row). Password persistence.
