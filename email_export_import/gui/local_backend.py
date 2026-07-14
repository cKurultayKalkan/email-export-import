"""In-process backend presenting the DaemonBackend interface over a local
RunManager + Controller.

app.py codes against one backend shape and swaps Local<->Daemon by a single
line. LocalBackend keeps today's behaviour: it holds the live IMAP connections
itself (keyed by a plan_id) between plan() and start(), where the daemon would
have shipped them over HTTP. Every method mirrors DaemonBackend so the wizard,
dashboard and side panel render from either backend unchanged.
"""
from __future__ import annotations

import secrets

from ..models import Account
from .controller import Controller
from .run_manager import Run, RunManager, RunSnapshot
from ..state import MigrationState


def _account(cfg: dict) -> Account:
    """Tolerant dict->Account (same defaults the daemon applies on the wire)."""
    return Account(
        host=cfg["host"], port=int(cfg.get("port", 993)),
        ssl=bool(cfg.get("ssl", True)), email=cfg["email"],
        password=cfg.get("password", ""),
        verify_ssl=bool(cfg.get("verify_ssl", True)),
    )


class LocalBackend:
    """RunManager-shaped facade that runs migrations in this process.

    Snapshots come straight from the RunManager (no wire, no reconstruction),
    so config lookups and per-key reads are just dict access. Held connections
    for a pending plan live in `_pending`, awaiting start().
    """

    def __init__(self, manager: RunManager, controller: Controller) -> None:
        self._manager = manager
        self._controller = controller
        # plan_id -> (src_conn, dst_conn, PlanResult, skip, src_email, dst_email)
        # awaiting start(); the connections stay open in-process the whole time.
        self._pending: dict = {}
        # Settings are mirrored locally so the panel reads them without a call;
        # setters write through to the manager (below).
        self.max_active = manager.max_active
        self.workers = manager.workers
        self.rate_limit = manager.rate_limit

    # ---- reads (RunManager-compatible) ----
    def refresh(self) -> list[RunSnapshot]:
        return self._manager.snapshot_all()

    def snapshot_all(self) -> list[RunSnapshot]:
        return self._manager.snapshot_all()

    def config_for(self, key: str) -> dict | None:
        run = self._manager.get(key)
        return run.state.config if run else None

    def folder_counts(self, key: str) -> dict | None:
        run = self._manager.get(key)
        return run.state.folder_done_counts() if run else None

    def last_run(self, key: str) -> dict | None:
        run = self._manager.get(key)
        return run.state.last_run if run else None

    def save_config(self, key: str, config: dict) -> None:
        run = self._manager.get(key)
        if run is not None:
            run.state.set_config(config)
            run.state.flush()

    def mark_failed(self, key: str, message: str) -> None:
        run = self._manager.get(key)
        if run is not None:
            run.mark_failed(message)

    def shutdown_daemon(self) -> None:
        """No out-of-process daemon in local mode — nothing to stop."""
        return None

    def active_count(self) -> int:
        return self._manager.active_count()

    def default_workers(self) -> int:
        return self._manager.default_workers()

    # ---- controls ----
    def pause(self, key: str) -> None:
        run = self._manager.get(key)
        if run is not None:
            run.pause()

    def cancel(self, key: str) -> None:
        run = self._manager.get(key)
        if run is not None:
            run.cancel()

    def remove(self, key: str) -> None:
        self._manager.remove(key)

    def set_max_active(self, n: int) -> None:
        self.max_active = n
        self._manager.max_active = n

    def set_workers(self, n: int) -> None:
        self.workers = n
        self._manager.workers = n

    def set_rate_limit(self, n: int) -> None:
        self.rate_limit = n
        self._manager.rate_limit = n

    # ---- start flow (this process owns the connections) ----
    def test_connection(self, account: dict) -> dict:
        """Validate credentials only: connect, then close. The real connect for
        a migration happens in plan(). Same {ok, kind, message} shape the daemon
        returns so the wizard treats both backends identically."""
        res = self._controller.test_connection(_account(account))
        if res.ok:
            res.conn.close()
            return {"ok": True}
        return {"ok": False, "kind": res.kind, "message": res.message}

    def add_placeholder(self, src_email: str, dst_email: str) -> str:
        """A queued card for a bulk account before its connect starts. Mirrors
        the daemon: idempotent per pair (no-op if a run is already active for
        the key), returns the key the poll will pump onto a real run."""
        key = f"{src_email}__{dst_email}"
        run = self._manager.get(key)
        if run is None or not run.is_active:
            state = MigrationState.for_pair(
                src_email, dst_email, base_dir=self._manager.state_dir
            )
            self._manager.add(Run.placeholder(state, state_dir=self._manager.state_dir))
        return key

    def plan(self, src: dict, dst: dict, skip: list) -> dict:
        """Connect both accounts, build the folder plan, and hold the live
        connections under a plan_id until start(). Returns the normalized
        {plan_id, total, folders:[{source,dest,count}]} the plan view renders."""
        src_res = self._controller.test_connection(_account(src))
        if not src_res.ok:
            raise RuntimeError(src_res.message)
        dst_res = self._controller.test_connection(_account(dst))
        if not dst_res.ok:
            src_res.conn.close()  # don't leak the source connection
            raise RuntimeError(dst_res.message)
        skip_set = set(skip)
        plan = self._controller.build_plan(src_res.conn, dst_res.conn, skip_set)
        plan_id = secrets.token_urlsafe(12)
        self._pending[plan_id] = (
            src_res.conn, dst_res.conn, plan, skip_set,
            _account(src).email, _account(dst).email,
        )
        return {
            "plan_id": plan_id,
            "total": plan.total,
            "folders": [{"source": p.source, "dest": p.dest,
                         "count": plan.counts.get(p.source, 0)}
                        for p in plan.plans],
        }

    def start(self, plan_id: str, skip: list, workers: int,
              spool: bool = False, title: str | None = None) -> str:
        held = self._pending.pop(plan_id, None)
        if held is None:
            raise RuntimeError("unknown or expired plan")
        src_conn, dst_conn, plan, _skip, src_email, dst_email = held
        skip_set = set(skip)
        active_plans = [p for p in plan.plans if p.source not in skip_set]
        total = sum(plan.counts.get(p.source, 0) for p in active_plans)
        state = MigrationState.for_pair(
            src_email, dst_email, base_dir=self._manager.state_dir
        )
        key = f"{src_email}__{dst_email}"
        run = Run(
            key=key, title=title or f"{src_email} → {dst_email}",
            src_conn=src_conn, dst_conn=dst_conn, plans=active_plans,
            state=state, workers=workers, total=total, skip=skip_set,
            spool_enabled=spool, rate_limit=self._manager.rate_limit,
            state_dir=self._manager.state_dir,
        )
        self._manager.add(run)
        run.start()
        return key
