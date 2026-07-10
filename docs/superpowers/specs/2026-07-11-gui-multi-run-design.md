# GUI v2 — Concurrent Migrations + Non-Blocking UI — Design Spec

**Date:** 2026-07-11
**Status:** Approved
**Builds on:** `2026-07-10-desktop-gui-design.md` (extends the Flet GUI; engine unchanged)

## Purpose

Rework the desktop GUI so migrations run as independent, concurrent background
jobs surfaced on a live dashboard, and no blocking IMAP call ever runs on the
Flet UI thread. This fixes three reported defects and adds the requested
multi-migration UX.

## Reported defects this fixes

1. **Certificate "continue without verification" does nothing.** The retry runs
   a blocking `test_connection` inside the dialog's button handler on the UI
   thread; the modal never cleanly dismisses and the UI freezes.
2. **Cancel appears not to work.** Cancel sets the event correctly, but there is
   no immediate visual feedback and the single progress screen stays until the
   worker thread fully stops.
3. **Cannot navigate during a migration.** `go_progress` clears `page.views` and
   owns a terminal poll loop, locking the app to one run.

Root cause is shared: blocking IMAP work (`test_connection`, `build_plan`,
cancel feedback) executes on the Flet event thread. The redesign moves all
blocking work off the UI thread and decouples runs from screens.

## Decisions

| Question | Decision |
|---|---|
| Concurrency | Multiple migrations run truly concurrently, all shown on a dashboard. |
| Pause semantics | Pause = graceful stop (workers finish current message, connections close, state flushed). Resume = reconnect and continue from disk state (done messages skipped). No true mid-message freeze. |
| Rate limiting | With more than one run active, per-run worker default drops 4→2 to cap total connection pressure; user can override. |
| State/spool | Unchanged; same files as the CLI, shared and resumable across CLI/GUI. |

## Architecture

Three focused units, each independently testable headless (no Flet import in
`run_manager.py` or `async_ops.py`):

```
email_export_import/gui/
  run_manager.py   # Run + RunManager (concurrency, lifecycle, snapshots)
  async_ops.py     # run a blocking callable off-thread, deliver result via callback
  controller.py    # (existing) single-shot ops: test_connection, build_plan; reused by Run
  views.py         # dashboard cards, wizard screens, detail screen
  app.py           # routing, background poll, wiring
```

### `Run` (in `run_manager.py`)

Represents one migration. Owns its own `migrate()` thread, cancel event, and
lock-guarded counters. Deliberately Flet-free.

- Construction: `Run(key: str, src_conn, dst_conn, plans, state, workers, total, skip, spool_enabled, state_dir)`.
- `key = f"{src_email}__{dst_email}"` — the session identity, matching the state
  file name.
- State machine: `queued → running → (paused | done | error)`; `paused →
  running` (resume) re-enters. `cancelled` is terminal.
- `start()` — spawn daemon thread running `migrate(..., cancel=self._cancel,
  spool=...)`, saving session config first (same keys the CLI/GUI already write,
  plus `spool`). On clean finish marks state completed → status `done`; on
  `QuotaExceeded` → status `error` kind `quota`; other exception → status
  `error` kind `fatal`.
- `pause()` — set the cancel event (graceful stop); when the thread ends,
  status becomes `paused`, not `done` (state left resumable). Distinguished from
  cancel by an internal `_pausing` flag checked in the finally.
- `resume()` — rebuild connections from saved config (asking passwords is the
  UI's job before calling), then `start()` again over the same state; dedup
  skips done messages.
- `cancel()` — set the cancel event with `_pausing=False`; terminal status
  `cancelled`; state remains on disk but the run is removed from the active
  dashboard on user dismissal.
- `snapshot() -> RunSnapshot` — thread-safe: `key, title, status, processed,
  total, current_folder, error_kind, error_message, result, spool_pending`.

### `RunManager` (in `run_manager.py`)

- `dict[str, Run]` keyed by session key; at most one Run per key.
- `load_resumable(state_dir)` — on startup, wrap each `MigrationState.list_resumable`
  entry as a `paused` Run (no thread, no live connections yet) so unfinished
  migrations appear on the dashboard immediately.
- `add(run)`, `get(key)`, `runs() -> list[Run]` (stable order: active first,
  then paused, then finished), `remove(key)` (dismiss a finished/cancelled run
  from the dashboard; state file untouched).
- `active_count()` — number of `running` runs; used to pick the per-run worker
  default.
- `snapshot_all() -> list[RunSnapshot]` — one lock-guarded read per run for the
  dashboard poll.

### `async_ops` (in `async_ops.py`)

Keeps blocking IMAP work off the UI thread.

- `run_async(fn: Callable[[], T], on_done: Callable[[T], None], on_error:
  Callable[[Exception], None]) -> None` — runs `fn` on a daemon thread; delivers
  the result or exception to the callback. The UI passes callbacks that marshal
  back onto the Flet page (via `page.run_thread`-style update). Never raises on
  the caller's thread.
- Used for `test_connection` and `build_plan` in the wizard, and for `resume`'s
  reconnect.

## Screens

1. **Dashboard (home).** One card per run: source→destination title, status
   badge (running / paused / done / error), inline progress bar + `N/M` counter.
   Card actions by status: running → Pause, Cancel, Detail; paused → Resume,
   Cancel, Detail; done/error → Detail, Dismiss. A "+ New migration" button opens
   the wizard. A single app-level background poll (~200 ms) refreshes every card
   from `RunManager.snapshot_all()`; the dashboard is always current when shown.
2. **Wizard (source → destination → plan).** The existing flow, but every
   blocking step goes through `async_ops`: "Test connection" shows a spinner,
   runs off-thread, then advances / shows a field error / opens the certificate
   dialog. Plan building is off-thread too. Finishing the wizard creates a Run,
   adds it to the manager, starts it, and returns to the dashboard (the run
   continues there).
3. **Detail.** One run's large progress view: bar, `N/M`, current folder,
   failure list, and status-appropriate actions (Pause/Resume/Cancel). Reached
   from a card's Detail button; Back returns to the dashboard with the run still
   running in the background.

## Certificate dialog (defect 1 fix)

The dialog's "continue without verification" handler sets `verify_ssl=False`
and re-tests **through `async_ops`**, not inline. The handler's only synchronous
work is `pop_dialog()` + starting the async retry, so the modal dismisses
cleanly and the UI stays responsive.

## Cancel (defect 2 fix)

Pause/Cancel set the run's cancel event (non-blocking) and immediately update
the card/detail to "Stopping…"; the user can navigate away at once. The Run's
worker threads stop at the next message boundary; because reconnect backoff is
already cancel-aware, the only remaining blocking wait is a socket timeout
(≤60 s), and the UI reflects the pending stop throughout.

## Data flow

The UI never calls IMAP directly. UI → RunManager / async_ops → background
thread → snapshot or callback → UI poll / marshalled update. Each Run's counters
and status are guarded by one `threading.Lock` (the existing Controller
pattern). State files and the message spool are unchanged and shared with the
CLI.

## Error handling

Each run's failure is isolated to its own card (badge + message) and never
affects other runs. Quota → card shows "mailbox full — free space and resume".
Certificate failure → wizard dialog. Connection/auth failure → wizard field
error. A background poll callback that hits an unmounted control stops that
poll only (page closed), never the runs.

## Testing

- **run_manager (headless, FakeIMAPClient):** multiple runs started
  concurrently complete independently; pause → snapshot status `paused` and the
  state file stays resumable; resume completes the remainder exactly once (no
  duplicates); cancel is terminal; one run raising does not disturb another;
  `load_resumable` surfaces unfinished sessions as paused runs; worker-default
  selection drops to 2 when another run is active.
- **async_ops (headless):** a blocking callable does not block the calling
  thread and its result/exception reaches the right callback; an exception in
  `fn` goes to `on_error`, never the caller.
- **views/app:** thin; dashboard card render and route/controls regression
  (guarding the Flet positional-arg bug class); no UI automation (Flet tooling
  immature) — the manual smoke checklist is extended for the dashboard, pause,
  resume, cancel-during-run, and concurrent-runs cases.

## Out of scope (v1)

True mid-message pause; shared connection pooling across runs; mobile; changes
to the migration engine, state, or spool modules.
