from __future__ import annotations

import json
import os
from pathlib import Path

# EEI_BASE_DIR overrides the state directory (the daemon already honors it;
# reading it here keeps the GUI/CLI in the same place — handy for an isolated
# local test run without touching the installed app's ~/.email-export-import).
DEFAULT_BASE_DIR = Path(os.environ.get("EEI_BASE_DIR") or (Path.home() / ".email-export-import"))


class MigrationState:
    """Per (source, destination) resume state.

    Records, per source folder, which messages already landed on the
    destination — by Message-ID when the message has one, by source UID
    otherwise. UID entries are only trusted while the folder's UIDVALIDITY
    is unchanged (RFC 3501: a UIDVALIDITY bump means all old UIDs are void).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        # {folder: {"uidvalidity": int|None, "message_ids": set[str], "uids": set[int]}}
        self._folders: dict[str, dict] = {}
        # Session settings (hosts, emails, skip list, workers — never passwords)
        # so an interrupted run can be resumed without re-entering everything.
        self.config: dict | None = None
        self.status: str = "running"
        # Wall-clock bookkeeping for the results view. Spans pauses: it is
        # "first started" → "finally completed", not active transfer time.
        self.started_at: float | None = None
        self.finished_at: float | None = None
        # Summary of the most recent run (migrated/skipped/failed + first
        # failure lines) — persisted so the results survive an app restart.
        self.last_run: dict | None = None
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            for name, f in raw.get("folders", {}).items():
                self._folders[name] = {
                    "uidvalidity": f["uidvalidity"],
                    "message_ids": set(f["message_ids"]),
                    "uids": set(f["uids"]),
                    # Absent in states written before the resume fast-path
                    # existed — they simply earn it on their next pass.
                    "done_uids": set(f.get("done_uids", [])),
                }
            self.config = raw.get("config")
            self.status = raw.get("status", "running")
            self.started_at = raw.get("started_at")
            self.finished_at = raw.get("finished_at")
            self.last_run = raw.get("last_run")

    @classmethod
    def for_pair(
        cls, src_email: str, dst_email: str, base_dir: Path | None = None
    ) -> "MigrationState":
        base = base_dir or DEFAULT_BASE_DIR
        state_dir = base / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(base, 0o700)
        os.chmod(state_dir, 0o700)
        return cls(state_dir / f"{src_email}__{dst_email}.json")

    @classmethod
    def list_resumable(cls, base_dir: Path | None = None) -> list["MigrationState"]:
        """Interrupted sessions (status running, with a saved config), oldest
        path first. Unreadable or pre-session-format files are skipped."""
        state_dir = (base_dir or DEFAULT_BASE_DIR) / "state"
        if not state_dir.is_dir():
            return []
        out: list[MigrationState] = []
        for path in sorted(state_dir.glob("*.json")):
            try:
                s = cls(path)
            except Exception:
                continue
            if s.status not in ("completed", "cancelled") and s.config is not None:
                out.append(s)
        return out

    @classmethod
    def list_completed(cls, base_dir: Path | None = None) -> list["MigrationState"]:
        """Finished sessions (status completed, with a saved config) so the
        dashboard can keep them visible as done cards instead of appearing to
        have vanished."""
        state_dir = (base_dir or DEFAULT_BASE_DIR) / "state"
        if not state_dir.is_dir():
            return []
        out: list[MigrationState] = []
        for path in sorted(state_dir.glob("*.json")):
            try:
                s = cls(path)
            except Exception:
                continue
            if s.status == "completed" and s.config is not None:
                out.append(s)
        return out

    def set_config(self, config: dict) -> None:
        self.config = config

    def mark_completed(self) -> None:
        import time

        self.status = "completed"
        self.finished_at = time.time()

    def mark_started(self) -> None:
        import time

        if self.started_at is None:
            self.started_at = time.time()
        self.finished_at = None  # a re-run (sync/resume) reopens the clock

    def duration_seconds(self) -> float | None:
        if self.started_at is not None and self.finished_at is not None:
            return max(0.0, self.finished_at - self.started_at)
        return None

    def reopen(self) -> None:
        """Put a finished/cancelled session back in the running state.

        Syncing a completed pair re-runs it to pick up mail that arrived since
        (dedup skips everything already moved). Without this the state would
        still say "completed", so a sync interrupted midway would be filed as
        finished instead of resumable.
        """
        self.status = "running"

    def mark_cancelled(self) -> None:
        self.status = "cancelled"

    def migrated_count(self) -> int:
        return sum(
            len(f["message_ids"]) + len(f["uids"]) for f in self._folders.values()
        )

    def processed_count(self) -> int:
        """Every UID handled (migrated, deduplicated, already-there, or
        vanished). This is the honest progress numerator: it converges on the
        plan total, where migrated_count() undershoots it by the dedup/skip
        margin and reads as an unfinished run."""
        return sum(len(f["done_uids"]) for f in self._folders.values())

    def folder_done_counts(self) -> dict[str, int]:
        """Per-folder handled-message counts, for the results breakdown."""
        return {name: len(f["done_uids"]) for name, f in self._folders.items()}

    def mark_processed(self, folder: str, uid: int) -> None:
        """Record a UID that was handled without producing a migrated message
        (e.g. expunged from the source mid-run): resumes must not retry it and
        counters must include it."""
        self._folder(folder)["done_uids"].add(uid)

    def _folder(self, folder: str) -> dict:
        return self._folders.setdefault(
            folder,
            {"uidvalidity": None, "message_ids": set(), "uids": set(), "done_uids": set()},
        )

    def set_uidvalidity(self, folder: str, uidvalidity: int) -> None:
        f = self._folder(folder)
        if f["uidvalidity"] is not None and f["uidvalidity"] != uidvalidity:
            # Old-generation UIDs are meaningless now — drop every UID-keyed
            # record so dedup falls back to Message-IDs.
            f["uids"] = set()
            f["done_uids"] = set()
        f["uidvalidity"] = uidvalidity

    def migrated_uids(self, folder: str) -> set[int]:
        """Every UID already migrated in this folder, in the current UIDVALIDITY
        generation. A resume drops these from the UID list *before* any FETCH,
        so it costs work proportional to what is LEFT rather than re-scanning
        the whole folder's metadata. set_uidvalidity() empties this on a bump,
        which correctly forces the Message-ID path.
        """
        f = self._folders.get(folder)
        return set(f["done_uids"]) if f is not None else set()

    def is_migrated(self, folder: str, message_id: str | None, uid: int) -> bool:
        f = self._folders.get(folder)
        if f is None:
            return False
        if uid in f["done_uids"]:
            return True
        if message_id is not None:
            return message_id in f["message_ids"]
        return uid in f["uids"]

    def mark_migrated(self, folder: str, message_id: str | None, uid: int) -> None:
        f = self._folder(folder)
        if message_id is not None:
            f["message_ids"].add(message_id)
        else:
            # No Message-ID to dedup on — the UID is the only identity we have.
            f["uids"].add(uid)
        # Recorded for every message regardless: this is what lets a resume skip
        # a finished message without fetching its metadata just to re-read a
        # Message-ID it has already seen. Kept separate from "uids" so it never
        # distorts migrated_count().
        f["done_uids"].add(uid)

    def flush(self) -> None:
        raw = {
            "folders": {
                name: {
                    "uidvalidity": f["uidvalidity"],
                    "message_ids": sorted(f["message_ids"]),
                    "uids": sorted(f["uids"]),
                    "done_uids": sorted(f["done_uids"]),
                }
                for name, f in self._folders.items()
            },
            "config": self.config,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "last_run": self.last_run,
        }
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(raw))
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)
