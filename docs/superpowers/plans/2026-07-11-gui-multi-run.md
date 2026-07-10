# GUI v2 — Concurrent Migrations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dashboard of independent concurrent migrations with pause/resume/cancel, all blocking IMAP work off the Flet UI thread.

**Architecture:** New headless `run_manager.py` (Run = one migration lifecycle in its own thread; RunManager = keyed collection + resumable loading) and `async_ops.py` (off-thread execution with callbacks). Views/app rewritten around a live dashboard; old single-run Controller runner methods removed once the new app lands. One minimal state addition: `mark_cancelled`.

**Tech Stack:** Python 3.11+, Flet >=0.85,<0.86 (installed 0.85.3 — use `ft.app`, `page.views`, `View(route=, controls=)`, `show_dialog`/`pop_dialog`, Dropdown `on_select`, `page.run_thread`), existing engine, pytest, uv.

**Spec:** `docs/superpowers/specs/2026-07-11-gui-multi-run-design.md`

## Global Constraints

- Engine/spool untouched. Sole exception: `MigrationState.mark_cancelled()` + `list_resumable` excluding `cancelled`.
- `run_manager.py` and `async_ops.py` MUST NOT import flet (headless-testable). flet only in `views.py`/`app.py`.
- Passwords never persisted (session config, gui.json, logs). State/spool files shared with the CLI, unchanged formats plus the new optional `"total"` config key.
- Locale files keep identical key sets (existing parity test enforces).
- Run status strings exactly: `queued`, `running`, `paused`, `done`, `error`, `cancelled`.
- Worker default at wizard plan screen: 2 if another run is `running`, else 4; explicit user choice always wins.
- All tests via `uv run pytest`; gui-app tests keep `pytest.importorskip("flet")`.
- Commit after every task.

---

### Task 1: State — cancelled status

**Files:**
- Modify: `email_export_import/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Consumes: existing `MigrationState` (`status`, `mark_completed`, `list_resumable`).
- Produces: `MigrationState.mark_cancelled() -> None` (sets `status = "cancelled"`, persisted by `flush()`); `list_resumable` returns only sessions whose status is neither `"completed"` nor `"cancelled"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_state.py`:

```python
def test_mark_cancelled_roundtrip(tmp_path):
    path = tmp_path / "s.json"
    s = MigrationState(path)
    s.set_config({"src": {"host": "h"}})
    s.mark_cancelled()
    s.flush()
    assert MigrationState(path).status == "cancelled"


def test_list_resumable_excludes_cancelled(tmp_path):
    base = tmp_path / "base"
    cancelled = MigrationState.for_pair("a@x", "b@y", base_dir=base)
    cancelled.set_config({"src": {"host": "h"}})
    cancelled.mark_cancelled()
    cancelled.flush()

    running = MigrationState.for_pair("c@x", "d@y", base_dir=base)
    running.set_config({"src": {"host": "h2"}})
    running.flush()

    resumable = MigrationState.list_resumable(base_dir=base)
    assert len(resumable) == 1
    assert resumable[0].config == {"src": {"host": "h2"}}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_state.py -k cancelled -v`
Expected: 2 FAIL — `AttributeError: ... 'mark_cancelled'`.

- [ ] **Step 3: Implement**

In `email_export_import/state.py`, next to `mark_completed`:

```python
    def mark_cancelled(self) -> None:
        self.status = "cancelled"
```

In `list_resumable`, change the filter line to:

```python
            if s.status not in ("completed", "cancelled") and s.config is not None:
                out.append(s)
```

- [ ] **Step 4: Run tests, full suite, commit**

Run: `uv run pytest tests/test_state.py -v` then `uv run pytest`
Expected: all PASS.

```bash
git add email_export_import/state.py tests/test_state.py
git commit -m "feat: add cancelled session status excluded from resume"
```

---

### Task 2: async_ops

**Files:**
- Create: `email_export_import/gui/async_ops.py`
- Test: `tests/test_async_ops.py`

**Interfaces:**
- Consumes: stdlib only.
- Produces: `async_ops.run_async(fn: Callable[[], T], on_done: Callable[[T], None], on_error: Callable[[Exception], None]) -> threading.Thread` — runs `fn` on a daemon thread; result to `on_done`, any exception to `on_error`; never raises on the caller's thread; returns the thread (tests join it).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_async_ops.py`:

```python
import threading
import time

from email_export_import.gui.async_ops import run_async


def test_result_reaches_on_done():
    done = []
    t = run_async(lambda: 41 + 1, done.append, lambda e: done.append(("err", e)))
    t.join(timeout=5)
    assert done == [42]


def test_exception_reaches_on_error_only():
    outcomes = []

    def boom():
        raise ValueError("nope")

    t = run_async(boom, lambda r: outcomes.append(("done", r)),
                  lambda e: outcomes.append(("err", type(e).__name__, str(e))))
    t.join(timeout=5)
    assert outcomes == [("err", "ValueError", "nope")]


def test_caller_thread_is_not_blocked():
    release = threading.Event()
    finished = []

    def slow():
        release.wait(timeout=5)
        return "ok"

    start = time.monotonic()
    t = run_async(slow, finished.append, lambda e: finished.append(e))
    took = time.monotonic() - start
    assert took < 0.5  # returned immediately, fn still parked
    assert finished == []
    release.set()
    t.join(timeout=5)
    assert finished == ["ok"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_async_ops.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `email_export_import/gui/async_ops.py`:

```python
"""Run blocking work off the UI thread, delivering the outcome via callbacks.

The Flet event thread must never run IMAP calls; views hand them to
run_async() and update controls from the callback (Flet control updates are
thread-safe; a RuntimeError from an unmounted control means the page closed).
"""
from __future__ import annotations

import threading
from typing import Callable, TypeVar

T = TypeVar("T")


def run_async(
    fn: Callable[[], T],
    on_done: Callable[[T], None],
    on_error: Callable[[Exception], None],
) -> threading.Thread:
    def worker() -> None:
        try:
            result = fn()
        except Exception as exc:
            on_error(exc)
            return
        on_done(result)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread
```

- [ ] **Step 4: Run tests, full suite, commit**

Run: `uv run pytest tests/test_async_ops.py -v` then `uv run pytest`
Expected: all PASS.

```bash
git add email_export_import/gui/async_ops.py tests/test_async_ops.py
git commit -m "feat: add off-thread executor for GUI blocking work"
```

---

### Task 3: Run

**Files:**
- Create: `email_export_import/gui/run_manager.py`
- Test: `tests/test_run_manager.py`

**Interfaces:**
- Consumes: `MailConnection`, `migrate(..., workers, cancel, spool)`, `MigrationState`, `MessageSpool.for_pair`, `TransferProgress`, `QuotaExceeded`, `FolderPlan`; tests use `tests.fakes` + `Controller.test_connection/build_plan` to build inputs.
- Produces:
  - `run_manager.RunSnapshot(key: str, title: str, status: str, processed: int, total: int, current_folder: str | None, error_kind: str | None = None, error_message: str | None = None, result: TransferProgress | None = None, spool_pending: int | None = None)` — dataclass.
  - `run_manager.Run(key, title, src_conn, dst_conn, plans, state, workers, total, skip, spool_enabled, state_dir=None)` — single-shot lifecycle.
  - `Run.placeholder(state, state_dir=None) -> Run` — classmethod for disk-loaded paused sessions (no thread/conns); key from state.path stem, title from config emails, processed from `state.migrated_count()`, total from `config.get("total", 0)`.
  - `Run.start()`, `Run.pause()`, `Run.cancel()`, `Run.join(timeout=None)`, `Run.snapshot() -> RunSnapshot`, `Run.is_active -> bool` (thread alive).
  - Status transitions: start→`running`; thread end → `done` (clean, state marked completed) / `paused` (pause requested) / `cancelled` (cancel requested) / `error` (kind `quota` or `fatal`, state untouched).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_run_manager.py`:

```python
import threading

import pytest
from imapclient.exceptions import IMAPClientError

from email_export_import import connection
from email_export_import.gui.controller import Controller
from email_export_import.gui.run_manager import Run, RunSnapshot
from email_export_import.models import Account
from email_export_import.state import MigrationState
from tests.fakes import FakeIMAPClient, make_message

SRC = Account(host="src.test", port=993, ssl=True, email="a@x", password="p")
DST = Account(host="dst.test", port=993, ssl=True, email="b@y", password="p")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(connection.time, "sleep", lambda s: None)


def wire(monkeypatch, src_data, dst_fake):
    def factory(host, port=993, ssl=True, **kw):
        if host == "src.test":
            return FakeIMAPClient(folders=src_data)
        return dst_fake

    monkeypatch.setattr(connection, "IMAPClient", factory)


def build_run(monkeypatch, tmp_path, src_data, dst_fake, key="a@x__b@y", **kw):
    wire(monkeypatch, src_data, dst_fake)
    c = Controller(state_dir=tmp_path)
    src_conn = c.test_connection(SRC).conn
    dst_conn = c.test_connection(DST).conn
    plan = c.build_plan(src_conn, dst_conn, skip=set())
    state = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)
    return Run(
        key=key, title="a@x → b@y", src_conn=src_conn, dst_conn=dst_conn,
        plans=plan.plans, state=state, workers=kw.get("workers", 1),
        total=plan.total, skip=set(), spool_enabled=kw.get("spool", False),
        state_dir=tmp_path,
    )


def test_run_completes_and_marks_done(monkeypatch, tmp_path):
    src_data = {"INBOX": [make_message(uid=i, message_id=f"<m{i}@x>") for i in (1, 2, 3)]}
    dst = FakeIMAPClient(folders={"INBOX": []})
    run = build_run(monkeypatch, tmp_path, src_data, dst)
    run.start()
    run.join(timeout=10)
    snap = run.snapshot()
    assert snap.status == "done"
    assert snap.result.migrated == 3
    assert snap.processed == 3
    assert MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path).status == "completed"
    assert MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path).config["total"] == 3


def test_pause_leaves_state_resumable(monkeypatch, tmp_path):
    src_data = {"INBOX": [make_message(uid=i, message_id=f"<m{i}@x>") for i in range(1, 30)]}
    dst = FakeIMAPClient(folders={"INBOX": []})
    gate = threading.Event()
    real_append = dst.append

    def gated(folder, body, flags=(), msg_time=None):
        gate.wait(timeout=5)
        return real_append(folder, body, flags=flags, msg_time=msg_time)

    dst.append = gated
    run = build_run(monkeypatch, tmp_path, src_data, dst)
    run.start()
    run.pause()
    gate.set()
    run.join(timeout=10)
    snap = run.snapshot()
    assert snap.status == "paused"
    reloaded = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)
    assert reloaded.status == "running"  # still resumable on disk
    assert snap.result.migrated < 29


def test_cancel_is_terminal_status(monkeypatch, tmp_path):
    src_data = {"INBOX": [make_message(uid=i, message_id=f"<m{i}@x>") for i in range(1, 30)]}
    dst = FakeIMAPClient(folders={"INBOX": []})
    gate = threading.Event()
    real_append = dst.append

    def gated(folder, body, flags=(), msg_time=None):
        gate.wait(timeout=5)
        return real_append(folder, body, flags=flags, msg_time=msg_time)

    dst.append = gated
    run = build_run(monkeypatch, tmp_path, src_data, dst)
    run.start()
    run.cancel()
    gate.set()
    run.join(timeout=10)
    assert run.snapshot().status == "cancelled"


def test_quota_becomes_error_status(monkeypatch, tmp_path):
    src_data = {"INBOX": [make_message(uid=1, message_id="<m1@x>")]}
    dst = FakeIMAPClient(folders={"INBOX": []})
    dst.append_error = IMAPClientError("APPEND failed [OVERQUOTA]")
    run = build_run(monkeypatch, tmp_path, src_data, dst)
    run.start()
    run.join(timeout=10)
    snap = run.snapshot()
    assert snap.status == "error"
    assert snap.error_kind == "quota"
    assert MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path).status == "running"


def test_placeholder_from_disk_session(tmp_path):
    state = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)
    state.set_config({
        "src": {"host": "h", "email": "a@x"},
        "dst": {"host": "h2", "email": "b@y"},
        "total": 100,
    })
    state.mark_migrated("INBOX", "<m1@x>", 1)
    state.flush()

    run = Run.placeholder(state, state_dir=tmp_path)
    snap = run.snapshot()
    assert snap.status == "paused"
    assert snap.key == "a@x__b@y"
    assert "a@x" in snap.title and "b@y" in snap.title
    assert snap.processed == 1
    assert snap.total == 100
    assert run.is_active is False


def test_placeholder_without_total_shows_zero_total(tmp_path):
    state = MigrationState.for_pair("c@x", "d@y", base_dir=tmp_path)
    state.set_config({"src": {"email": "c@x", "host": "h"}, "dst": {"email": "d@y", "host": "h2"}})
    state.flush()
    snap = Run.placeholder(state, state_dir=tmp_path).snapshot()
    assert snap.total == 0  # CLI-written session: M unknown, UI shows only N
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_run_manager.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `email_export_import/gui/run_manager.py`:

```python
"""One migration = one Run (own thread, own cancel event, lock-guarded
snapshot). Deliberately flet-free so every path is unit-testable headless."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from ..connection import MailConnection
from ..errors import QuotaExceeded
from ..models import FolderPlan, TransferProgress
from ..spool import MessageSpool
from ..state import MigrationState
from ..transfer import migrate


@dataclass
class RunSnapshot:
    key: str
    title: str
    status: str  # queued|running|paused|done|error|cancelled
    processed: int
    total: int
    current_folder: str | None
    error_kind: str | None = None  # "quota" | "fatal"
    error_message: str | None = None
    result: TransferProgress | None = None
    spool_pending: int | None = None


class Run:
    """Single-shot migration lifecycle. Resume constructs a fresh Run for the
    same key (dedup lives in the shared state file, not in this object)."""

    def __init__(
        self,
        key: str,
        title: str,
        src_conn: MailConnection | None,
        dst_conn: MailConnection | None,
        plans: list[FolderPlan] | None,
        state: MigrationState,
        workers: int,
        total: int,
        skip: set[str] | None = None,
        spool_enabled: bool = False,
        state_dir: Path | None = None,
    ) -> None:
        self.key = key
        self.title = title
        self._src_conn = src_conn
        self._dst_conn = dst_conn
        self._plans = plans or []
        self._state = state
        self._workers = workers
        self._skip = skip or set()
        self._spool_enabled = spool_enabled
        self._state_dir = state_dir

        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._pausing = False
        self._thread: threading.Thread | None = None
        self._status = "queued"
        self._processed = 0
        self._total = total
        self._current_folder: str | None = None
        self._result: TransferProgress | None = None
        self._error: tuple[str, str] | None = None
        self._spool_pending: int | None = None

    @classmethod
    def placeholder(cls, state: MigrationState, state_dir: Path | None = None) -> "Run":
        cfg = state.config or {}
        src_email = cfg.get("src", {}).get("email", "?")
        dst_email = cfg.get("dst", {}).get("email", "?")
        run = cls(
            key=state.path.stem,
            title=f"{src_email} → {dst_email}",
            src_conn=None,
            dst_conn=None,
            plans=None,
            state=state,
            workers=cfg.get("workers", 4),
            total=cfg.get("total", 0),
            skip=set(cfg.get("skip", [])),
            spool_enabled=cfg.get("spool", False),
            state_dir=state_dir,
        )
        run._status = "paused"
        run._processed = state.migrated_count()
        return run

    @property
    def is_active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def state(self) -> MigrationState:
        return self._state

    def start(self) -> None:
        if self.is_active or self._src_conn is None or self._dst_conn is None:
            return
        with self._lock:
            self._status = "running"
            self._pausing = False
            self._cancel = threading.Event()
            self._result = None
            self._error = None

        src_email = self._src_conn.account.email
        dst_email = self._dst_conn.account.email
        self._state.set_config(
            {
                "src": _account_config(self._src_conn.account),
                "dst": _account_config(self._dst_conn.account),
                "skip": sorted(self._skip),
                "workers": self._workers,
                "spool": self._spool_enabled,
                "total": self._total,
            }
        )
        self._state.flush()

        spool = (
            MessageSpool.for_pair(src_email, dst_email, base_dir=self._state_dir)
            if self._spool_enabled
            else None
        )

        def on_message(folder: str, uid: int) -> None:
            with self._lock:
                self._processed += 1
                self._current_folder = folder

        def run() -> None:
            error: tuple[str, str] | None = None
            result: TransferProgress | None = None
            try:
                result = migrate(
                    self._src_conn, self._dst_conn, self._plans, self._state,
                    on_message=on_message, workers=self._workers,
                    cancel=self._cancel, spool=spool,
                )
            except QuotaExceeded as exc:
                error = ("quota", str(exc))
            except Exception as exc:
                error = ("fatal", str(exc))
            finally:
                if spool is not None:
                    with self._lock:
                        self._spool_pending = spool.pending_count()
                self._src_conn.close()
                self._dst_conn.close()
                with self._lock:
                    self._result = result
                    if error is not None:
                        self._error = error
                        self._status = "error"
                    elif self._cancel.is_set():
                        self._status = "paused" if self._pausing else "cancelled"
                    else:
                        self._status = "done"
                if self._status == "done":
                    self._state.mark_completed()
                    self._state.flush()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def pause(self) -> None:
        with self._lock:
            if self._status != "running":
                return
            self._pausing = True
        self._cancel.set()

    def cancel(self) -> None:
        with self._lock:
            self._pausing = False
            if not self.is_active:
                self._status = "cancelled"
        self._cancel.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def snapshot(self) -> RunSnapshot:
        with self._lock:
            error_kind, error_message = self._error or (None, None)
            return RunSnapshot(
                key=self.key,
                title=self.title,
                status=self._status,
                processed=self._processed,
                total=self._total,
                current_folder=self._current_folder,
                error_kind=error_kind,
                error_message=error_message,
                result=self._result,
                spool_pending=self._spool_pending,
            )


def _account_config(account) -> dict:
    return {
        "host": account.host,
        "port": account.port,
        "ssl": account.ssl,
        "verify_ssl": account.verify_ssl,
        "email": account.email,
    }
```

- [ ] **Step 4: Run tests, full suite, commit**

Run: `uv run pytest tests/test_run_manager.py -v` then `uv run pytest`
Expected: all PASS.

```bash
git add email_export_import/gui/run_manager.py tests/test_run_manager.py
git commit -m "feat: add Run lifecycle for GUI migrations"
```

---

### Task 4: RunManager

**Files:**
- Modify: `email_export_import/gui/run_manager.py`
- Test: `tests/test_run_manager.py`

**Interfaces:**
- Consumes: `Run`, `MigrationState.list_resumable`.
- Produces (appended to `run_manager.py`):
  - `RunManager(state_dir: Path | None = None)`
  - `load_resumable() -> None` — wraps each resumable session as a placeholder Run (skips keys already present).
  - `add(run: Run) -> bool` — False (and no insert) if an ACTIVE run with the same key exists; a non-active same-key run is replaced.
  - `get(key) -> Run | None`; `runs() -> list[Run]` ordered running→queued→paused→error→done→cancelled, insertion-stable within a status.
  - `remove(key) -> None` — if the run's status is `cancelled`, calls `state.mark_cancelled()` + `flush()` so it stays dismissed across launches.
  - `active_count() -> int` (status running); `default_workers() -> int` — 2 if `active_count() > 0` else 4.
  - `snapshot_all() -> list[RunSnapshot]` in `runs()` order.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_run_manager.py`:

```python
def test_manager_concurrent_runs_are_independent(monkeypatch, tmp_path):
    from email_export_import.gui.run_manager import RunManager

    dst_ok = FakeIMAPClient(folders={"INBOX": []})
    dst_bad = FakeIMAPClient(folders={"INBOX": []})
    dst_bad.append_error = IMAPClientError("APPEND failed [OVERQUOTA]")

    def factory(host, port=993, ssl=True, **kw):
        if host == "src.test":
            return FakeIMAPClient(
                folders={"INBOX": [make_message(uid=1, message_id="<m1@x>")]}
            )
        return dst_ok if host == "dst.test" else dst_bad

    monkeypatch.setattr(connection, "IMAPClient", factory)
    c = Controller(state_dir=tmp_path)

    def make_run(dst_host, dst_email, key):
        src_conn = c.test_connection(SRC).conn
        dst_acc = Account(host=dst_host, port=993, ssl=True, email=dst_email, password="p")
        dst_conn = c.test_connection(dst_acc).conn
        plan = c.build_plan(src_conn, dst_conn, skip=set())
        state = MigrationState.for_pair("a@x", dst_email, base_dir=tmp_path)
        return Run(key=key, title=key, src_conn=src_conn, dst_conn=dst_conn,
                   plans=plan.plans, state=state, workers=1, total=plan.total,
                   skip=set(), spool_enabled=False, state_dir=tmp_path)

    m = RunManager(state_dir=tmp_path)
    ok_run = make_run("dst.test", "b@y", "a@x__b@y")
    bad_run = make_run("bad.test", "q@z", "a@x__q@z")
    assert m.add(ok_run) and m.add(bad_run)
    ok_run.start()
    bad_run.start()
    ok_run.join(timeout=10)
    bad_run.join(timeout=10)

    statuses = {s.key: s.status for s in m.snapshot_all()}
    assert statuses["a@x__b@y"] == "done"
    assert statuses["a@x__q@z"] == "error"


def test_manager_duplicate_key_guard(monkeypatch, tmp_path):
    from email_export_import.gui.run_manager import RunManager

    src_data = {"INBOX": [make_message(uid=i, message_id=f"<m{i}@x>") for i in range(1, 20)]}
    dst = FakeIMAPClient(folders={"INBOX": []})
    gate = threading.Event()
    real_append = dst.append
    dst.append = lambda f, b, flags=(), msg_time=None: (gate.wait(timeout=5), real_append(f, b, flags=flags, msg_time=msg_time))[1]

    m = RunManager(state_dir=tmp_path)
    run1 = build_run(monkeypatch, tmp_path, src_data, dst)
    assert m.add(run1) is True
    run1.start()
    run2 = build_run(monkeypatch, tmp_path, src_data, dst)
    assert m.add(run2) is False  # active run with same key
    gate.set()
    run1.join(timeout=10)
    assert m.add(run2) is True  # replace once inactive


def test_manager_load_resumable_and_remove_cancelled(tmp_path):
    from email_export_import.gui.run_manager import RunManager

    state = MigrationState.for_pair("a@x", "b@y", base_dir=tmp_path)
    state.set_config({"src": {"email": "a@x", "host": "h"},
                      "dst": {"email": "b@y", "host": "h2"}, "total": 5})
    state.flush()

    m = RunManager(state_dir=tmp_path)
    m.load_resumable()
    assert [s.status for s in m.snapshot_all()] == ["paused"]

    run = m.get("a@x__b@y")
    run.cancel()  # placeholder → immediate terminal
    assert run.snapshot().status == "cancelled"
    m.remove("a@x__b@y")
    assert m.snapshot_all() == []
    # dismissed-cancelled stays gone across launches
    m2 = RunManager(state_dir=tmp_path)
    m2.load_resumable()
    assert m2.snapshot_all() == []


def test_manager_default_workers(monkeypatch, tmp_path):
    from email_export_import.gui.run_manager import RunManager

    src_data = {"INBOX": [make_message(uid=i, message_id=f"<m{i}@x>") for i in range(1, 20)]}
    dst = FakeIMAPClient(folders={"INBOX": []})
    gate = threading.Event()
    real_append = dst.append
    dst.append = lambda f, b, flags=(), msg_time=None: (gate.wait(timeout=5), real_append(f, b, flags=flags, msg_time=msg_time))[1]

    m = RunManager(state_dir=tmp_path)
    assert m.default_workers() == 4
    run = build_run(monkeypatch, tmp_path, src_data, dst)
    m.add(run)
    run.start()
    assert m.default_workers() == 2
    gate.set()
    run.join(timeout=10)
    assert m.default_workers() == 4
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_run_manager.py -k manager -v`
Expected: FAIL — `ImportError: cannot import name 'RunManager'`.

- [ ] **Step 3: Implement**

Append to `email_export_import/gui/run_manager.py`:

```python
_STATUS_ORDER = {"running": 0, "queued": 1, "paused": 2, "error": 3, "done": 4, "cancelled": 5}


class RunManager:
    """Keyed collection of Runs backing the dashboard."""

    def __init__(self, state_dir: Path | None = None) -> None:
        self.state_dir = state_dir
        self._runs: dict[str, Run] = {}

    def load_resumable(self) -> None:
        for state in MigrationState.list_resumable(base_dir=self.state_dir):
            key = state.path.stem
            if key not in self._runs:
                self._runs[key] = Run.placeholder(state, state_dir=self.state_dir)

    def add(self, run: Run) -> bool:
        existing = self._runs.get(run.key)
        if existing is not None and existing.is_active:
            return False
        self._runs[run.key] = run
        return True

    def get(self, key: str) -> Run | None:
        return self._runs.get(key)

    def runs(self) -> list[Run]:
        return sorted(
            self._runs.values(),
            key=lambda r: _STATUS_ORDER.get(r.snapshot().status, 9),
        )

    def remove(self, key: str) -> None:
        run = self._runs.pop(key, None)
        if run is not None and run.snapshot().status == "cancelled":
            run.state.mark_cancelled()
            run.state.flush()

    def active_count(self) -> int:
        return sum(1 for r in self._runs.values() if r.snapshot().status == "running")

    def default_workers(self) -> int:
        # Concurrent runs multiply connection pressure on rate-limiting
        # servers; halve the per-run default when another run is live.
        return 2 if self.active_count() > 0 else 4

    def snapshot_all(self) -> list[RunSnapshot]:
        return [r.snapshot() for r in self.runs()]
```

- [ ] **Step 4: Run tests, full suite, commit**

Run: `uv run pytest tests/test_run_manager.py -v` then `uv run pytest`
Expected: all PASS.

```bash
git add email_export_import/gui/run_manager.py tests/test_run_manager.py
git commit -m "feat: add RunManager with resumable loading and worker throttling"
```

---

### Task 5: Views v2 (dashboard, detail, password dialog, async wizard affordances)

**Files:**
- Modify: `email_export_import/gui/views.py`
- Modify: `email_export_import/locales/en.json`, `email_export_import/locales/tr.json`
- Test: `tests/test_gui_app.py`

**Interfaces:**
- Consumes: `RunSnapshot` (Task 3), `I18n`, existing `build_account`/`build_plan` (kept; `build_account` handles gain `set_busy: Callable[[bool], None]`).
- Produces:
  - `views.build_dashboard(i18n, snapshots: list[RunSnapshot], on_new, on_pause, on_resume, on_cancel, on_detail, on_dismiss, on_locale, highlight_key: str | None = None) -> ft.View` — route `/`; per-key callbacks take the run key.
  - `views.build_detail(i18n, snap: RunSnapshot, on_pause, on_resume, on_cancel, on_back) -> ft.View` — route `/detail`; includes summary counts, failure list, spool-pending line (reusing existing `done.*` keys).
  - `views.build_password_dialog(i18n, title: str, on_submit: Callable[[str, str], None], on_cancel: Callable[[], None]) -> ft.AlertDialog` — two password fields (source, destination).
  - `build_welcome` and `build_done` DELETED (dashboard/detail replace them).
  - New locale keys (BOTH files): `status.running`, `status.queued`, `status.paused`, `status.done`, `status.error`, `status.cancelled`, `status.stopping`, `dash.heading`, `dash.empty`, `dash.new`, `dash.pause`, `dash.resume`, `dash.cancel`, `dash.detail`, `dash.dismiss`, `dash.migrated_only`, `dash.duplicate`, `resume.title`, `resume.src_password`, `resume.dst_password`, `resume.go`, `detail.back`. Deleted keys: `welcome.heading`, `welcome.resume_heading`, `welcome.resume`, `welcome.new`, `welcome.migrated_count`, `done.close`, `done.resume_hint` stays (used in detail).

- [ ] **Step 1: Update locale files**

In `email_export_import/locales/en.json` remove `welcome.heading`, `welcome.resume_heading`, `welcome.resume`, `welcome.new`, `welcome.migrated_count`, `done.close`; add:

```json
{
  "status.running": "Running",
  "status.queued": "Queued",
  "status.paused": "Paused",
  "status.done": "Finished",
  "status.error": "Error",
  "status.cancelled": "Cancelled",
  "status.stopping": "Stopping…",
  "dash.heading": "Migrations",
  "dash.empty": "No migrations yet — start one below.",
  "dash.new": "+ New migration",
  "dash.pause": "Pause",
  "dash.resume": "Resume",
  "dash.cancel": "Cancel",
  "dash.detail": "Details",
  "dash.dismiss": "Dismiss",
  "dash.migrated_only": "{count} messages moved",
  "dash.duplicate": "A migration for this account pair already exists",
  "resume.title": "Enter passwords to resume",
  "resume.src_password": "Source password",
  "resume.dst_password": "Destination password",
  "resume.go": "Resume",
  "detail.back": "Back"
}
```

In `email_export_import/locales/tr.json` remove the same keys; add:

```json
{
  "status.running": "Çalışıyor",
  "status.queued": "Sırada",
  "status.paused": "Duraklatıldı",
  "status.done": "Tamamlandı",
  "status.error": "Hata",
  "status.cancelled": "İptal edildi",
  "status.stopping": "Durduruluyor…",
  "dash.heading": "Taşımalar",
  "dash.empty": "Henüz taşıma yok — aşağıdan başlatın.",
  "dash.new": "+ Yeni taşıma",
  "dash.pause": "Duraklat",
  "dash.resume": "Devam et",
  "dash.cancel": "İptal",
  "dash.detail": "Detay",
  "dash.dismiss": "Kaldır",
  "dash.migrated_only": "{count} mesaj taşındı",
  "dash.duplicate": "Bu hesap çifti için zaten bir taşıma var",
  "resume.title": "Devam etmek için parolaları girin",
  "resume.src_password": "Kaynak parolası",
  "resume.dst_password": "Hedef parolası",
  "resume.go": "Devam et",
  "detail.back": "Geri"
}
```

- [ ] **Step 2: Extend the view regression test (failing first)**

In `tests/test_gui_app.py`, replace the `build_welcome` block inside `test_view_builders_set_route_and_controls` and extend:

```python
def test_view_builders_set_route_and_controls():
    from email_export_import.gui import views
    from email_export_import.gui.controller import PlanResult
    from email_export_import.gui.i18n import I18n
    from email_export_import.gui.run_manager import RunSnapshot

    i18n = I18n(locale="en")
    noop = lambda *a, **k: None

    snap = RunSnapshot(
        key="a__b", title="a → b", status="running", processed=3, total=10,
        current_folder="INBOX",
    )
    dash = views.build_dashboard(
        i18n, [snap], noop, noop, noop, noop, noop, noop, noop
    )
    assert dash.route == "/" and isinstance(dash.controls, list)

    plan = views.build_plan(
        i18n, PlanResult(plans=[], counts={}, total=0), set(), 4, False,
        noop, noop, noop, noop, noop,
    )
    assert plan.route == "/plan" and isinstance(plan.controls, list)

    detail = views.build_detail(i18n, snap, noop, noop, noop, noop)
    assert detail.route == "/detail" and isinstance(detail.controls, list)


def test_dashboard_shows_statuses_and_password_dialog_builds():
    import flet as ft

    from email_export_import.gui import views
    from email_export_import.gui.i18n import I18n
    from email_export_import.gui.run_manager import RunSnapshot

    i18n = I18n(locale="en")
    noop = lambda *a, **k: None
    snaps = [
        RunSnapshot(key=f"k{i}", title=f"t{i}", status=s, processed=1, total=0,
                    current_folder=None)
        for i, s in enumerate(["running", "paused", "done", "error", "cancelled"])
    ]
    dash = views.build_dashboard(i18n, snaps, noop, noop, noop, noop, noop, noop, noop)
    assert isinstance(dash.controls, list) and len(dash.controls) >= 5

    dlg = views.build_password_dialog(i18n, "a → b", noop, noop)
    assert isinstance(dlg, ft.AlertDialog)
```

Also remove the `build_welcome` import/use anywhere else in the test file.

Run: `uv run pytest tests/test_gui_app.py -v`
Expected: FAIL — `AttributeError: ... 'build_dashboard'`.

- [ ] **Step 3: Implement views**

In `email_export_import/gui/views.py`: delete `build_welcome` and `build_done`; add (imports: `from .run_manager import RunSnapshot`; keep existing imports):

```python
_STATUS_COLOR = {
    "running": ft.Colors.BLUE,
    "queued": ft.Colors.GREY,
    "paused": ft.Colors.AMBER,
    "done": ft.Colors.GREEN,
    "error": ft.Colors.RED,
    "cancelled": ft.Colors.GREY,
}


def _progress_line(i18n: I18n, snap: RunSnapshot) -> ft.Control:
    if snap.total > 0:
        return ft.Column(
            [
                ft.ProgressBar(value=snap.processed / snap.total),
                ft.Text(f"{snap.processed} / {snap.total}", size=12),
            ],
            spacing=4,
        )
    return ft.Text(i18n.t("dash.migrated_only", count=snap.processed), size=12)


def _card_actions(
    i18n: I18n, snap: RunSnapshot, on_pause, on_resume, on_cancel, on_detail, on_dismiss
) -> list[ft.Control]:
    key = snap.key
    actions: list[ft.Control] = []
    if snap.status == "running":
        actions.append(ft.TextButton(i18n.t("dash.pause"), on_click=lambda e, k=key: on_pause(k)))
        actions.append(ft.TextButton(i18n.t("dash.cancel"), on_click=lambda e, k=key: on_cancel(k)))
    elif snap.status == "paused":
        actions.append(ft.FilledButton(i18n.t("dash.resume"), on_click=lambda e, k=key: on_resume(k)))
        actions.append(ft.TextButton(i18n.t("dash.cancel"), on_click=lambda e, k=key: on_cancel(k)))
    elif snap.status in ("done", "error", "cancelled"):
        actions.append(ft.TextButton(i18n.t("dash.dismiss"), on_click=lambda e, k=key: on_dismiss(k)))
    actions.append(ft.TextButton(i18n.t("dash.detail"), on_click=lambda e, k=key: on_detail(k)))
    return actions


def build_dashboard(
    i18n: I18n,
    snapshots: list[RunSnapshot],
    on_new: Callable[[], None],
    on_pause: Callable[[str], None],
    on_resume: Callable[[str], None],
    on_cancel: Callable[[str], None],
    on_detail: Callable[[str], None],
    on_dismiss: Callable[[str], None],
    on_locale: Callable[[str], None],
    highlight_key: str | None = None,
) -> ft.View:
    cards: list[ft.Control] = []
    for snap in snapshots:
        badge = ft.Text(
            i18n.t(f"status.{snap.status}"),
            color=_STATUS_COLOR.get(snap.status),
            weight=ft.FontWeight.BOLD,
            size=12,
        )
        body = [
            ft.Row([ft.Text(snap.title, weight=ft.FontWeight.BOLD, expand=True), badge]),
            _progress_line(i18n, snap),
        ]
        if snap.error_kind == "quota":
            body.append(ft.Text(i18n.t("done.quota"), color=ft.Colors.RED, size=12))
        elif snap.error_kind == "fatal":
            body.append(ft.Text(snap.error_message or "", color=ft.Colors.RED, size=12))
        body.append(
            ft.Row(
                _card_actions(i18n, snap, on_pause, on_resume, on_cancel, on_detail, on_dismiss),
                alignment=ft.MainAxisAlignment.END,
            )
        )
        cards.append(
            ft.Card(
                content=ft.Container(ft.Column(body, spacing=8), padding=12),
                color=ft.Colors.PRIMARY_CONTAINER if snap.key == highlight_key else None,
            )
        )
    if not cards:
        cards.append(ft.Text(i18n.t("dash.empty")))

    controls: list[ft.Control] = [
        _title_bar(i18n, on_locale),
        ft.Text(i18n.t("dash.heading"), size=18, weight=ft.FontWeight.BOLD),
        ft.Column(cards, scroll=ft.ScrollMode.AUTO, expand=True, spacing=8),
        ft.FilledButton(i18n.t("dash.new"), on_click=lambda e: on_new()),
    ]
    return ft.View(route="/", controls=controls, padding=24, spacing=16)


def build_detail(
    i18n: I18n,
    snap: RunSnapshot,
    on_pause: Callable[[str], None],
    on_resume: Callable[[str], None],
    on_cancel: Callable[[str], None],
    on_back: Callable[[], None],
) -> ft.View:
    controls: list[ft.Control] = [
        ft.Text(snap.title, size=18, weight=ft.FontWeight.BOLD),
        ft.Text(i18n.t(f"status.{snap.status}"), color=_STATUS_COLOR.get(snap.status)),
        _progress_line(i18n, snap),
        ft.Text(snap.current_folder or "", size=12),
    ]
    if snap.error_kind == "quota":
        controls.append(ft.Text(i18n.t("done.quota"), color=ft.Colors.RED))
    elif snap.error_kind == "fatal":
        controls.append(ft.Text(snap.error_message or "", color=ft.Colors.RED))
    if snap.result is not None:
        controls.append(
            ft.Row(
                [
                    ft.Text(f"{i18n.t('done.migrated')}: {snap.result.migrated}"),
                    ft.Text(f"{i18n.t('done.skipped')}: {snap.result.skipped}"),
                    ft.Text(f"{i18n.t('done.failed')}: {snap.result.failed}"),
                ],
                spacing=24,
            )
        )
        if snap.result.failures:
            controls.append(ft.Text(i18n.t("done.failures_heading"), weight=ft.FontWeight.BOLD))
            controls.append(
                ft.Column(
                    [ft.Text(line, size=12) for line in snap.result.failures[:50]],
                    scroll=ft.ScrollMode.AUTO,
                    height=180,
                )
            )
    if snap.spool_pending:
        controls.append(ft.Text(i18n.t("done.spool_pending", count=snap.spool_pending), size=12))
    controls.append(ft.Text(i18n.t("done.resume_hint"), size=12))
    controls.append(
        ft.Row(
            _card_actions(i18n, snap, on_pause, on_resume, on_cancel, lambda k: None, lambda k: None)[:-1]
            + [ft.TextButton(i18n.t("detail.back"), on_click=lambda e: on_back())],
            alignment=ft.MainAxisAlignment.END,
        )
    )
    return ft.View(route="/detail", controls=controls, padding=24, spacing=12)


def build_password_dialog(
    i18n: I18n,
    title: str,
    on_submit: Callable[[str, str], None],
    on_cancel: Callable[[], None],
) -> ft.AlertDialog:
    src_pw = ft.TextField(
        label=i18n.t("resume.src_password"), password=True, can_reveal_password=True
    )
    dst_pw = ft.TextField(
        label=i18n.t("resume.dst_password"), password=True, can_reveal_password=True
    )
    return ft.AlertDialog(
        modal=True,
        title=ft.Text(i18n.t("resume.title")),
        content=ft.Column([ft.Text(title, size=12), src_pw, dst_pw], tight=True, spacing=8),
        actions=[
            ft.TextButton(i18n.t("cert.cancel"), on_click=lambda e: on_cancel()),
            ft.FilledButton(
                i18n.t("resume.go"),
                on_click=lambda e: on_submit(src_pw.value or "", dst_pw.value or ""),
            ),
        ],
    )
```

In `build_account`, add a busy indicator: create `busy = ft.ProgressRing(width=18, height=18, visible=False)` next to `status_text` in the controls row, and extend the returned handles dict with:

```python
    def set_busy(value: bool) -> None:
        busy.visible = value
        try:
            busy.update()
        except RuntimeError:
            pass
```
(handles: `{"account": ..., "preset_key": ..., "set_busy": set_busy}`; place `busy` in the same Row as the Test button.)

- [ ] **Step 4: Run tests, full suite, commit**

Run: `uv run pytest tests/test_gui_app.py tests/test_i18n.py -v` then `uv run pytest`
Expected: gui-app tests pass with flet installed (importorskip otherwise); i18n parity green; full suite green EXCEPT `app.py` may fail to import if it still references `build_welcome`/`build_done` — Task 6 fixes app.py; if the import breakage trips a test in this task, apply the minimal app.py edits from Task 6's Step 2 in THIS commit instead of leaving the suite red (note it in the report).

```bash
git add email_export_import/gui/views.py email_export_import/locales/ tests/test_gui_app.py
git commit -m "feat: add dashboard, detail, and password-dialog views"
```

---

### Task 6: app.py v2 — routing, global poll, wiring; Controller runner removal

**Files:**
- Rewrite: `email_export_import/gui/app.py`
- Modify: `email_export_import/gui/controller.py` (remove runner: `RunSnapshot`, `start`, `snapshot`, `cancel`, `join`, `_account_config`, runner fields; keep `ConnectionResult`, `PlanResult`, `list_sessions`, `test_connection`, `build_plan`, `default_skip`, `_namespace_prefix`)
- Modify: `tests/test_gui_controller.py` (delete the runner tests: `test_runner_*`, `test_snapshot_polls_during_live_run`, `test_runner_with_parallel_workers`, `test_overlapping_start_is_ignored`, `test_start_with_spool_*`, `test_start_without_spool_*` — equivalents live in `tests/test_run_manager.py`)
- Test: `tests/test_gui_app.py`

**Interfaces:**
- Consumes: everything from Tasks 2–5; `Controller` (slimmed), `RunManager`/`Run`, `async_ops.run_async`, views.
- Produces: `app.main()`; `WizardState` (unchanged fields minus `resume_session`, plus nothing new — resume no longer goes through the wizard).

- [ ] **Step 1: Adjust tests (failing first)**

In `tests/test_gui_app.py`, update `test_wizard_state_defaults`:

```python
def test_wizard_state_defaults():
    from email_export_import.gui.app import WizardState

    ws = WizardState()
    assert ws.workers == 4
    assert ws.skip == set()
    assert ws.spool is False
    assert not hasattr(ws, "resume_session")
```

In `tests/test_gui_controller.py` delete the runner tests listed above (their coverage moved to `tests/test_run_manager.py` in Tasks 3–4).

Run: `uv run pytest tests/test_gui_app.py::test_wizard_state_defaults -v`
Expected: FAIL (`resume_session` still present).

- [ ] **Step 2: Rewrite app.py**

Replace `email_export_import/gui/app.py` with:

```python
from __future__ import annotations

import time
from dataclasses import dataclass, field

import flet as ft

from ..models import Account
from ..state import MigrationState
from . import views
from .async_ops import run_async
from .controller import Controller
from .i18n import I18n
from .run_manager import Run, RunManager


@dataclass
class WizardState:
    src_account: Account | None = None
    dst_account: Account | None = None
    src_conn: object = None
    dst_conn: object = None
    plan: object = None
    skip: set[str] = field(default_factory=set)
    workers: int = 4
    spool: bool = False


def main() -> None:
    ft.app(target=_page_main)


def _page_main(page: ft.Page) -> None:
    i18n = I18n()
    controller = Controller()
    manager = RunManager()
    manager.load_resumable()
    ws = WizardState()
    highlight: list[str | None] = [None]
    page.title = i18n.t("app.title")
    page.window.width = 820
    page.window.height = 680

    def close_window() -> None:
        page.run_task(page.window.close)

    def safe_update(control) -> None:
        try:
            control.update()
        except RuntimeError:
            pass  # page closed / control unmounted

    # ---- dashboard ------------------------------------------------------

    def set_locale(locale: str) -> None:
        i18n.set_locale(locale)
        show_dashboard()

    def show_dashboard() -> None:
        page.views.clear()
        page.views.append(_dashboard_view())
        page.update()

    def _dashboard_view() -> ft.View:
        return views.build_dashboard(
            i18n, manager.snapshot_all(),
            on_new=start_wizard, on_pause=do_pause, on_resume=ask_resume,
            on_cancel=do_cancel, on_detail=show_detail, on_dismiss=do_dismiss,
            on_locale=set_locale, highlight_key=highlight[0],
        )

    def do_pause(key: str) -> None:
        run = manager.get(key)
        if run is not None:
            run.pause()
        refresh_current()

    def do_cancel(key: str) -> None:
        run = manager.get(key)
        if run is not None:
            run.cancel()
        refresh_current()

    def do_dismiss(key: str) -> None:
        manager.remove(key)
        show_dashboard()

    # ---- resume ---------------------------------------------------------

    def ask_resume(key: str) -> None:
        run = manager.get(key)
        if run is None:
            return
        cfg = run.state.config or {}

        def submit(src_pw: str, dst_pw: str) -> None:
            page.pop_dialog()
            run_async(
                lambda: _reconnect_and_build(cfg, src_pw, dst_pw),
                on_done=lambda built: _start_resumed(key, run, cfg, built),
                on_error=lambda exc: _show_error(str(exc)),
            )

        page.show_dialog(
            views.build_password_dialog(i18n, run.title, submit, lambda: page.pop_dialog())
        )

    def _reconnect_and_build(cfg: dict, src_pw: str, dst_pw: str):
        src = _account_from_cfg(cfg["src"], src_pw, "EEI_SRC_PASSWORD")
        dst = _account_from_cfg(cfg["dst"], dst_pw, "EEI_DST_PASSWORD")
        src_result = controller.test_connection(src)
        if not src_result.ok:
            raise RuntimeError(src_result.message or "source connection failed")
        dst_result = controller.test_connection(dst)
        if not dst_result.ok:
            raise RuntimeError(dst_result.message or "destination connection failed")
        plan = controller.build_plan(
            src_result.conn, dst_result.conn, set(cfg.get("skip", []))
        )
        return src_result.conn, dst_result.conn, plan

    def _start_resumed(key: str, old_run: Run, cfg: dict, built) -> None:
        src_conn, dst_conn, plan = built
        run = Run(
            key=key, title=old_run.title, src_conn=src_conn, dst_conn=dst_conn,
            plans=plan.plans, state=old_run.state, workers=cfg.get("workers", 2),
            total=plan.total, skip=set(cfg.get("skip", [])),
            spool_enabled=cfg.get("spool", False),
        )
        manager.add(run)
        run.start()
        show_dashboard()

    def _account_from_cfg(cfg: dict, password: str, env_var: str) -> Account:
        import os

        return Account(
            host=cfg["host"], port=cfg["port"], ssl=cfg["ssl"], email=cfg["email"],
            password=password or os.environ.get(env_var, ""),
            verify_ssl=cfg.get("verify_ssl", True),
        )

    def _show_error(message: str) -> None:
        page.show_dialog(
            ft.AlertDialog(title=ft.Text(i18n.t("status.error")), content=ft.Text(message))
        )

    # ---- detail ---------------------------------------------------------

    detail_key: list[str | None] = [None]

    def show_detail(key: str) -> None:
        detail_key[0] = key
        run = manager.get(key)
        if run is None:
            show_dashboard()
            return
        page.views.clear()
        page.views.append(
            views.build_detail(
                i18n, run.snapshot(), on_pause=do_pause, on_resume=ask_resume,
                on_cancel=do_cancel, on_back=back_to_dashboard,
            )
        )
        page.update()

    def back_to_dashboard() -> None:
        detail_key[0] = None
        show_dashboard()

    def refresh_current() -> None:
        if detail_key[0] is not None:
            show_detail(detail_key[0])
        else:
            show_dashboard()

    # ---- background poll ------------------------------------------------

    def poll() -> None:
        while True:
            time.sleep(0.2)
            try:
                route = page.views[-1].route if page.views else None
                if route == "/":
                    page.views[-1] = _dashboard_view()
                    page.update()
                elif route == "/detail" and detail_key[0] is not None:
                    run = manager.get(detail_key[0])
                    if run is not None:
                        page.views[-1] = views.build_detail(
                            i18n, run.snapshot(), on_pause=do_pause,
                            on_resume=ask_resume, on_cancel=do_cancel,
                            on_back=back_to_dashboard,
                        )
                        page.update()
            except RuntimeError:
                return  # page closed

    # ---- wizard ---------------------------------------------------------

    def start_wizard() -> None:
        nonlocal ws
        ws = WizardState()
        go_account("source")

    def go_account(role: str, prefill: dict | None = None) -> None:
        status = ft.Text("")
        initial = dict(prefill or {})
        if role == "dest" and ws.src_account and not initial.get("email"):
            initial["email"] = ws.src_account.email

        handles: dict = {}

        def on_test(account: Account) -> None:
            status.value = i18n.t("account.testing")
            safe_update(status)
            handles["set_busy"](True)
            run_async(
                lambda: controller.test_connection(account),
                on_done=lambda result: _test_done(account, result),
                on_error=lambda exc: _test_done(account, None, exc),
            )

        def _test_done(account: Account, result, exc: Exception | None = None) -> None:
            handles["set_busy"](False)
            if exc is not None:
                status.value = str(exc)
                safe_update(status)
                return
            if result.ok:
                status.value = i18n.t("account.connected")
                safe_update(status)
                if role == "source":
                    ws.src_account, ws.src_conn = account, result.conn
                    ws.skip = controller.default_skip(handles["preset_key"]())
                    go_account("dest")
                else:
                    ws.dst_account, ws.dst_conn = account, result.conn
                    go_plan()
            elif result.kind == "cert":
                _cert_dialog(account)
            else:
                status.value = i18n.t(f"error.{result.kind}")
                safe_update(status)

        def _cert_dialog(account: Account) -> None:
            def retry_unverified(e) -> None:
                page.pop_dialog()
                account.verify_ssl = False
                on_test(account)  # async again — no UI freeze

            dialog = ft.AlertDialog(
                modal=True,
                title=ft.Text(i18n.t("cert.title")),
                content=ft.Text(i18n.t("cert.body")),
                actions=[
                    ft.TextButton(i18n.t("cert.cancel"), on_click=lambda e: page.pop_dialog()),
                    ft.FilledButton(i18n.t("cert.continue"), on_click=retry_unverified),
                ],
            )
            page.show_dialog(dialog)

        def on_back() -> None:
            back_to_dashboard() if role == "source" else go_account("source")

        view, view_handles = views.build_account(i18n, role, initial, on_test, on_back, status)
        handles.update(view_handles)
        page.views.clear()
        page.views.append(view)
        page.update()

    def go_plan() -> None:
        ws.workers = manager.default_workers()

        def plan_ready(plan) -> None:
            ws.plan = plan
            _render_plan()

        run_async(
            lambda: controller.build_plan(ws.src_conn, ws.dst_conn, ws.skip),
            on_done=plan_ready,
            on_error=lambda exc: _show_error(str(exc)),
        )

    def _render_plan() -> None:
        def on_toggle(source: str, included: bool) -> None:
            (ws.skip.discard if included else ws.skip.add)(source)
            # counts change → rebuild
            page.views[-1] = _plan_view()
            page.update()

        def on_workers(n: int) -> None:
            ws.workers = n

        def on_spool(enabled: bool) -> None:
            ws.spool = enabled

        def _plan_view() -> ft.View:
            return views.build_plan(
                i18n, ws.plan, ws.skip, ws.workers, ws.spool,
                on_toggle, on_workers, on_spool, start_migration,
                lambda: go_account("dest"),
            )

        page.views.clear()
        page.views.append(_plan_view())
        page.update()

    def start_migration() -> None:
        key = f"{ws.src_account.email}__{ws.dst_account.email}"
        existing = manager.get(key)
        if existing is not None and existing.is_active:
            highlight[0] = key
            back_to_dashboard()
            return
        active_plans = [p for p in ws.plan.plans if p.source not in ws.skip]
        total = sum(ws.plan.counts.get(p.source, 0) for p in active_plans)
        state = MigrationState.for_pair(ws.src_account.email, ws.dst_account.email)
        run = Run(
            key=key, title=f"{ws.src_account.email} → {ws.dst_account.email}",
            src_conn=ws.src_conn, dst_conn=ws.dst_conn, plans=active_plans,
            state=state, workers=ws.workers, total=total, skip=ws.skip,
            spool_enabled=ws.spool,
        )
        manager.add(run)
        run.start()
        highlight[0] = key
        back_to_dashboard()

    show_dashboard()
    page.run_thread(poll)


if __name__ == "__main__":
    main()
```

Accepted simplification vs the spec: a certificate failure during RESUME
surfaces as the error dialog (with the cert message), not the interactive
cert dialog — a resumed self-signed session already carries
`verify_ssl=False` in its config, so the interactive path is only reachable
when a previously-verified server turns self-signed mid-life. Note this in
your report.

- [ ] **Step 3: Slim controller.py**

Remove from `email_export_import/gui/controller.py`: the `RunSnapshot` dataclass, `start`, `snapshot`, `cancel`, `join`, `_account_config`, the runner fields in `__init__` (`_run_lock`, `_cancel`, `_thread`, `_processed`, `_total`, `_current_folder`, `_result`, `_error`, `_spool_pending`), and the now-unused imports (`threading`, `MessageSpool`, `TransferProgress`, `QuotaExceeded`, `migrate`). Keep `ConnectionResult`, `PlanResult`, `list_sessions`, `test_connection`, `build_plan`, `default_skip`, `_namespace_prefix`.

- [ ] **Step 4: Run tests, manual launch, full suite, commit**

Run: `uv run pytest tests/test_gui_app.py tests/test_gui_controller.py tests/test_run_manager.py -v`
Then: `uv run email-export-import-gui` — dashboard opens (empty state or paused cards); open wizard, back to dashboard; close. Note results honestly.
Then: `uv run pytest`
Expected: all PASS.

```bash
git add email_export_import/gui/ tests/
git commit -m "feat: rewrite GUI around concurrent-run dashboard"
```

---

### Task 7: Smoke checklist + README

**Files:**
- Modify: `docs/superpowers/gui-smoke-checklist.md`
- Modify: `README.md`

**Interfaces:** docs only.

- [ ] **Step 1: Replace the checklist**

Replace the checklist items in `docs/superpowers/gui-smoke-checklist.md` with:

```markdown
# GUI manual smoke checklist

Run before each release, on at least one platform:

- [ ] `uv run email-export-import-gui` opens the dashboard (empty state or paused cards)
- [ ] Language toggle switches every visible string (TR ↔ EN)
- [ ] An unfinished CLI session appears as a paused card; Resume asks only the two passwords
- [ ] "+ New migration" wizard: preset fills host/port/SSL; Custom stays editable
- [ ] "Test connection" shows a spinner and the UI stays responsive while it runs
- [ ] Wrong password shows the auth error text (no crash, no traceback)
- [ ] Self-signed server raises the certificate dialog; Continue retries without freezing
- [ ] Plan screen counts match the mailbox; unchecking a folder lowers the total
- [ ] Starting a migration returns to the dashboard with a live progress card
- [ ] A second migration can be started while the first runs; both cards update
- [ ] Pause stops the run within a few seconds (longer only if the server is rate-limiting reconnects); card shows Paused
- [ ] Resume after pause continues without duplicates
- [ ] Cancel is terminal; Dismiss removes the card and it stays gone on relaunch
- [ ] Detail screen shows live progress and failures; Back keeps the run going
- [ ] Killing the app mid-run and relaunching shows the run as paused; resume completes without duplicates
```

- [ ] **Step 2: Update README**

In `README.md`, replace the "Desktop app (experimental)" paragraph's closing sentence with:

```markdown
Same engine, same resume files as the CLI — start a migration in one and
finish it in the other. Multiple migrations run side by side on a live
dashboard with pause/resume. Turkish and English UI.
```

- [ ] **Step 3: Full suite, commit**

Run: `uv run pytest`
Expected: all PASS.

```bash
git add docs/superpowers/gui-smoke-checklist.md README.md
git commit -m "docs: update GUI smoke checklist and README for the dashboard"
```
