from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from ..connection import MailConnection
from ..errors import AuthFailed, CertificateVerifyFailed, ConnectionFailed, QuotaExceeded
from ..folders import build_folder_plan
from ..models import Account, FolderPlan, TransferProgress
from ..providers import get_preset
from ..spool import MessageSpool
from ..state import MigrationState
from ..transfer import migrate


@dataclass
class ConnectionResult:
    ok: bool
    kind: str | None = None  # "auth" | "cert" | "connection"
    message: str | None = None
    conn: MailConnection | None = None


@dataclass
class PlanResult:
    plans: list[FolderPlan]
    counts: dict[str, int]
    total: int


@dataclass
class RunSnapshot:
    processed: int
    total: int
    current_folder: str | None
    running: bool
    result: TransferProgress | None = None
    error_kind: str | None = None  # "quota" | "fatal"
    error_message: str | None = None
    spool_pending: int | None = None  # messages kept on disk for retry (None = spool off)


class Controller:
    """All GUI decisions live here; views only render what this returns.

    Deliberately flet-free so every path is unit-testable headless.
    """

    def __init__(self, state_dir: Path | None = None) -> None:
        self.state_dir = state_dir
        self._run_lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._processed = 0
        self._total = 0
        self._current_folder: str | None = None
        self._result: TransferProgress | None = None
        self._error: tuple[str, str] | None = None
        self._spool_pending: int | None = None

    def list_sessions(self) -> list[MigrationState]:
        return MigrationState.list_resumable(base_dir=self.state_dir)

    @staticmethod
    def default_skip(preset_key: str | None) -> set[str]:
        """A source preset's default skip set (e.g. Gmail's duplicate label views)."""
        if preset_key is None:
            return set()
        try:
            return set(get_preset(preset_key).skip_folders)
        except KeyError:
            return set()

    def test_connection(self, account: Account) -> ConnectionResult:
        conn = MailConnection(account)
        try:
            conn.connect()
        except CertificateVerifyFailed as exc:
            return ConnectionResult(ok=False, kind="cert", message=str(exc))
        except AuthFailed as exc:
            return ConnectionResult(ok=False, kind="auth", message=str(exc))
        except ConnectionFailed as exc:
            return ConnectionResult(ok=False, kind="connection", message=str(exc))
        return ConnectionResult(ok=True, conn=conn)

    @staticmethod
    def _namespace_prefix(conn: MailConnection) -> str:
        try:
            prefix, _sep = conn.with_retry(lambda c: c.namespace()).personal[0]
        except Exception:
            return ""
        if isinstance(prefix, bytes):
            prefix = prefix.decode()
        return prefix or ""

    def build_plan(
        self,
        src_conn: MailConnection,
        dst_conn: MailConnection,
        skip: set[str],
    ) -> PlanResult:
        plans = build_folder_plan(
            src_conn.with_retry(lambda c: c.list_folders()),
            dst_conn.with_retry(lambda c: c.list_folders()),
            skip,
            dst_prefix=self._namespace_prefix(dst_conn),
        )
        counts: dict[str, int] = {}
        for p in plans:
            try:
                counts[p.source] = src_conn.with_retry(
                    lambda c, n=p.source: c.folder_status(n, [b"MESSAGES"])
                )[b"MESSAGES"]
            except Exception:
                counts[p.source] = 0
        return PlanResult(plans=plans, counts=counts, total=sum(counts.values()))

    def start(
        self,
        src_conn: MailConnection,
        dst_conn: MailConnection,
        plans: list[FolderPlan],
        state: MigrationState,
        workers: int,
        total: int,
        skip: set[str] | None = None,
        spool: bool = False,
    ) -> None:
        if self._thread is not None and self._thread.is_alive():
            return  # a run is already in flight; ignore double-fire

        with self._run_lock:
            self._cancel = threading.Event()
            self._processed = 0
            self._total = total
            self._current_folder = None
            self._result = None
            self._error = None
            self._spool_pending = None

        state.set_config(
            {
                "src": self._account_config(src_conn.account),
                "dst": self._account_config(dst_conn.account),
                "skip": sorted(skip or set()),
                "workers": workers,
                "spool": spool,
            }
        )
        state.flush()

        message_spool = (
            MessageSpool.for_pair(
                src_conn.account.email, dst_conn.account.email, base_dir=self.state_dir
            )
            if spool
            else None
        )

        def on_message(folder: str, uid: int) -> None:
            with self._run_lock:
                self._processed += 1
                self._current_folder = folder

        def run() -> None:
            try:
                result = migrate(
                    src_conn, dst_conn, plans, state,
                    on_message=on_message, workers=workers, cancel=self._cancel,
                    spool=message_spool,
                )
                if not self._cancel.is_set():
                    state.mark_completed()
                    state.flush()
                with self._run_lock:
                    self._result = result
            except QuotaExceeded as exc:
                with self._run_lock:
                    self._error = ("quota", str(exc))
            except Exception as exc:
                with self._run_lock:
                    self._error = ("fatal", str(exc))
            finally:
                if message_spool is not None:
                    with self._run_lock:
                        self._spool_pending = message_spool.pending_count()
                src_conn.close()
                dst_conn.close()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    @staticmethod
    def _account_config(account: Account) -> dict:
        return {
            "host": account.host,
            "port": account.port,
            "ssl": account.ssl,
            "verify_ssl": account.verify_ssl,
            "email": account.email,
        }

    def snapshot(self) -> RunSnapshot:
        with self._run_lock:
            running = self._thread is not None and self._thread.is_alive()
            error_kind, error_message = self._error or (None, None)
            return RunSnapshot(
                processed=self._processed,
                total=self._total,
                current_folder=self._current_folder,
                running=running,
                result=self._result,
                error_kind=error_kind,
                error_message=error_message,
                spool_pending=self._spool_pending,
            )

    def cancel(self) -> None:
        self._cancel.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)
