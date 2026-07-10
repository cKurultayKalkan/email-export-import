from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..connection import MailConnection
from ..errors import AuthFailed, CertificateVerifyFailed, ConnectionFailed
from ..folders import build_folder_plan
from ..models import Account, FolderPlan
from ..state import MigrationState


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


class Controller:
    """All GUI decisions live here; views only render what this returns.

    Deliberately flet-free so every path is unit-testable headless.
    """

    def __init__(self, state_dir: Path | None = None) -> None:
        self.state_dir = state_dir

    def list_sessions(self) -> list[MigrationState]:
        return MigrationState.list_resumable(base_dir=self.state_dir)

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
