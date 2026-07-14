"""Adapter that lets the GUI drive the daemon through the same shapes it used
for the in-process RunManager.

The wire sends plain dicts; the views layer wants RunSnapshot objects with a
`.config` for the side panel. This class reconstructs those, and turns the
GUI's control calls into daemon HTTP calls. Building it as a standalone
adapter (not woven into app.py) keeps the working in-process path untouched
until the swap is wired and proven.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..daemon.client import DaemonClient, DaemonError
from .run_manager import RunSnapshot


@dataclass
class _WireResult:
    """Stands in for TransferProgress: the side panel reads migrated/skipped/
    failed/failures off `snap.result`, nothing more."""
    migrated: int = 0
    skipped: int = 0
    failed: int = 0
    failures: tuple = ()


def _snapshot_from_wire(d: dict) -> RunSnapshot:
    r = d.get("result")
    result = None
    if r:
        result = _WireResult(
            migrated=r.get("migrated", 0), skipped=r.get("skipped", 0),
            failed=r.get("failed", 0), failures=tuple(r.get("failures", [])),
        )
    return RunSnapshot(
        key=d["key"], title=d["title"], status=d["status"],
        processed=d.get("processed", 0), total=d.get("total", 0),
        current_folder=d.get("current_folder"),
        error_kind=d.get("error_kind"), error_message=d.get("error_message"),
        result=result, spool_pending=d.get("spool_pending"),
        duration_seconds=d.get("duration_seconds"),
    )


class DaemonBackend:
    """RunManager-shaped facade over a DaemonClient.

    Snapshots are cached from the last poll so config lookups and per-key reads
    don't each cost a round-trip; refresh() (called by the poll) updates them.
    """

    def __init__(self, client: DaemonClient) -> None:
        self._client = client
        self._last: list[dict] = []
        # Settings are read once and mirrored locally; setters write through.
        s = self._safe_settings()
        self.max_active = s.get("max_active", 2)
        self.workers = s.get("workers", 4)
        self.rate_limit = s.get("rate_limit", 0)

    def _safe_settings(self) -> dict:
        try:
            return self._client.get_settings()
        except DaemonError:
            return {}

    # ---- reads (RunManager-compatible) ----
    def refresh(self) -> list[RunSnapshot]:
        self._last = self._client.runs()
        return [_snapshot_from_wire(d) for d in self._last]

    def snapshot_all(self) -> list[RunSnapshot]:
        return [_snapshot_from_wire(d) for d in self._last]

    def _cached(self, key: str) -> dict | None:
        for d in self._last:
            if d["key"] == key:
                return d
        return None

    def config_for(self, key: str) -> dict | None:
        d = self._cached(key)
        return d.get("config") if d else None

    def folder_counts(self, key: str) -> dict | None:
        d = self._cached(key)
        return d.get("folder_counts") if d else None

    def last_run(self, key: str) -> dict | None:
        d = self._cached(key)
        return d.get("last_run") if d else None

    def save_config(self, key: str, config: dict) -> None:
        self._client.save_config(key, config)

    def mark_failed(self, key: str, message: str) -> None:
        self._client.mark_failed(key, message)

    def active_count(self) -> int:
        return sum(1 for d in self._last
                   if d.get("status") in ("running", "stopping"))

    def default_workers(self) -> int:
        if self.active_count() > 0:
            return max(1, self.workers // 2)
        return self.workers

    # ---- controls ----
    def pause(self, key: str) -> None:
        self._client.pause(key)

    def cancel(self, key: str) -> None:
        self._client.cancel(key)

    def remove(self, key: str) -> None:
        self._client.dismiss(key)

    def set_max_active(self, n: int) -> None:
        self.max_active = n
        self._client.set_settings({"max_active": n})

    def set_workers(self, n: int) -> None:
        self.workers = n
        self._client.set_settings({"workers": n})

    def set_rate_limit(self, n: int) -> None:
        self.rate_limit = n
        self._client.set_settings({"rate_limit": n})

    # ---- start flow (daemon owns the connections) ----
    def test_connection(self, account: dict) -> dict:
        """Validate credentials. Returns {ok, kind, message} — the same shape
        LocalBackend returns, so the wizard treats both identically."""
        return self._client.test_connection(account)

    def add_placeholder(self, src_email: str, dst_email: str) -> str:
        """A queued card for a bulk account before its connect starts."""
        return self._client.add_placeholder(src_email, dst_email)

    def plan(self, src: dict, dst: dict, skip: list) -> dict:
        """{plan_id, total, folders:[{source,dest,count}]} — normalized so the
        plan view renders from either backend unchanged."""
        return self._client.plan(src, dst, skip)

    def start(self, plan_id: str, skip: list, workers: int,
              spool: bool = False, title: str | None = None) -> str:
        return self._client.start(plan_id, skip, workers, spool)["key"]
