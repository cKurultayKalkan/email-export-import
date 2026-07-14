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
from ..throttle import RateLimiter
from ..transfer import migrate


@dataclass
class RunSnapshot:
    key: str
    title: str
    status: str  # queued|running|paused|done|error|cancelled
    processed: int
    total: int
    current_folder: str | None
    error_kind: str | None = None  # "quota" | "fatal" | "incomplete"
    error_message: str | None = None
    result: TransferProgress | None = None
    spool_pending: int | None = None
    duration_seconds: float | None = None  # first start → final completion


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
        rate_limit: int = 0,
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
        # Bytes/sec ceiling on uploads; 0 = unlimited.
        self._rate_limit = rate_limit

        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._pausing = False
        self._stop_requested = False
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
        run._status = "done" if state.status == "completed" else "paused"
        # done_uids counts every handled message (migrated, deduplicated,
        # already-there, vanished) — the honest numerator that converges on
        # the plan total. migrated_count() undershoots by the skip margin and
        # made complete runs look unfinished (e.g. 2611/2621).
        run._processed = state.processed_count() or state.migrated_count()
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
            self._stop_requested = False
            self._cancel = threading.Event()
            self._result = None
            self._error = None

        src_email = self._src_conn.account.email
        dst_email = self._dst_conn.account.email
        # A run that is starting is, by definition, running — this also reopens
        # a completed session being synced for newly-arrived mail, so an
        # interrupted sync stays resumable instead of being filed as finished.
        self._state.reopen()
        self._state.mark_started()  # opens the duration clock (idempotent)
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
                    throttle=(
                        RateLimiter(self._rate_limit) if self._rate_limit > 0 else None
                    ),
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
                    elif result is not None and result.failed:
                        # A run that finished with failures is NOT done —
                        # messages are missing. Stay red and resumable instead
                        # of hiding the loss behind a green tick.
                        self._error = (
                            "incomplete",
                            f"{result.failed} messages were not transferred: "
                            + "; ".join(result.failures[:3]),
                        )
                        self._status = "error"
                    else:
                        self._status = "done"
                if result is not None:
                    # Persist the run's outcome so the results panel survives
                    # an app restart instead of evaporating with the session.
                    self._state.last_run = {
                        "migrated": result.migrated,
                        "skipped": result.skipped,
                        "failed": result.failed,
                        "failures": list(result.failures[:5]),
                    }
                if self._status == "done":
                    self._state.mark_completed()
                if result is not None or self._status == "done":
                    self._state.flush()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def pause(self) -> None:
        with self._lock:
            if self._status != "running" or self._stop_requested:
                return
            self._pausing = True
            self._stop_requested = True
        self._cancel.set()

    def mark_failed(self, message: str) -> None:
        """Fail a queued placeholder (its connect/plan raised). The card shows
        the error; other bulk accounts are unaffected."""
        with self._lock:
            self._status = "error"
            self._error = ("fatal", message)

    def cancel(self) -> None:
        with self._lock:
            if self._status in ("done", "error", "cancelled"):
                return  # terminal — nothing to cancel
            self._pausing = False
            self._stop_requested = True
            if not self.is_active:
                self._status = "cancelled"
        self._cancel.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def snapshot(self) -> RunSnapshot:
        with self._lock:
            status = self._status
            if status == "running" and self._stop_requested:
                status = "stopping"
            error_kind, error_message = self._error or (None, None)
            return RunSnapshot(
                key=self.key,
                title=self.title,
                status=status,
                processed=self._processed,
                total=self._total,
                current_folder=self._current_folder,
                error_kind=error_kind,
                error_message=error_message,
                result=self._result,
                spool_pending=self._spool_pending,
                duration_seconds=self._state.duration_seconds(),
            )


def _account_config(account) -> dict:
    return {
        "host": account.host,
        "port": account.port,
        "ssl": account.ssl,
        "verify_ssl": account.verify_ssl,
        "email": account.email,
    }


_STATUS_ORDER = {
    "running": 0,
    "stopping": 1,
    "queued": 2,
    "paused": 3,
    "error": 4,
    "done": 5,
    "cancelled": 6,
}


class RunManager:
    """Keyed collection of Runs backing the dashboard."""

    def __init__(
        self,
        state_dir: Path | None = None,
        max_active: int = 2,
        workers: int = 4,
        rate_limit: int = 0,
    ) -> None:
        self.state_dir = state_dir
        # Cap on simultaneously-active bulk runs (protects a rate-limiting
        # destination). Counts in-flight connects too; see app.pump_bulk.
        self.max_active = max_active
        # Parallel connections per run. Lowering this reduces sustained
        # concurrent TCP writes — worth doing on a machine whose network stack
        # falls over under bulk upload.
        self.workers = workers
        # Bytes/sec ceiling on uploads (0 = unlimited). Sustained bulk TCP writes
        # are what stress a machine's network send path — not CPU or RAM — so
        # this is the knob that actually bounds that pressure.
        self.rate_limit = rate_limit
        self._runs: dict[str, Run] = {}
        self._lock = threading.Lock()

    def _load_placeholders(self, states) -> None:
        for state in states:
            key = state.path.stem
            with self._lock:
                if key not in self._runs:
                    self._runs[key] = Run.placeholder(state, state_dir=self.state_dir)

    def load_resumable(self) -> None:
        self._load_placeholders(MigrationState.list_resumable(base_dir=self.state_dir))

    def load_completed(self) -> None:
        """Surface finished migrations as done cards so they stay visible."""
        self._load_placeholders(MigrationState.list_completed(base_dir=self.state_dir))

    def add(self, run: Run) -> bool:
        with self._lock:
            existing = self._runs.get(run.key)
            if existing is not None and existing.is_active:
                return False
            self._runs[run.key] = run
            return True

    def get(self, key: str) -> "Run | None":
        with self._lock:
            return self._runs.get(key)

    def _snapshot_pairs(self):
        with self._lock:
            values = list(self._runs.values())
        pairs = [(r, r.snapshot()) for r in values]
        pairs.sort(key=lambda rs: _STATUS_ORDER.get(rs[1].status, 9))
        return pairs

    def runs(self) -> list["Run"]:
        return [r for r, _ in self._snapshot_pairs()]

    def remove(self, key: str) -> None:
        with self._lock:
            run = self._runs.pop(key, None)
        if run is not None and run.snapshot().status == "cancelled":
            run.state.mark_cancelled()
            run.state.flush()

    def active_count(self) -> int:
        with self._lock:
            values = list(self._runs.values())
        return sum(1 for r in values if r.snapshot().status in ("running", "stopping"))

    def default_workers(self) -> int:
        # Concurrent runs multiply connection pressure on rate-limiting
        # servers; halve the per-run default when another run is live.
        if self.active_count() > 0:
            return max(1, self.workers // 2)
        return self.workers

    def snapshot_all(self) -> list[RunSnapshot]:
        return [s for _, s in self._snapshot_pairs()]
