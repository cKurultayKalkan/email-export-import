from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_BASE_DIR = Path.home() / ".email-export-import"


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
        if path.exists():
            raw = json.loads(path.read_text())
            for name, f in raw.get("folders", {}).items():
                self._folders[name] = {
                    "uidvalidity": f["uidvalidity"],
                    "message_ids": set(f["message_ids"]),
                    "uids": set(f["uids"]),
                }

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

    def _folder(self, folder: str) -> dict:
        return self._folders.setdefault(
            folder, {"uidvalidity": None, "message_ids": set(), "uids": set()}
        )

    def set_uidvalidity(self, folder: str, uidvalidity: int) -> None:
        f = self._folder(folder)
        if f["uidvalidity"] is not None and f["uidvalidity"] != uidvalidity:
            f["uids"] = set()  # old-generation UIDs are meaningless now
        f["uidvalidity"] = uidvalidity

    def is_migrated(self, folder: str, message_id: str | None, uid: int) -> bool:
        f = self._folders.get(folder)
        if f is None:
            return False
        if message_id is not None:
            return message_id in f["message_ids"]
        return uid in f["uids"]

    def mark_migrated(self, folder: str, message_id: str | None, uid: int) -> None:
        f = self._folder(folder)
        if message_id is not None:
            f["message_ids"].add(message_id)
        else:
            f["uids"].add(uid)

    def flush(self) -> None:
        raw = {
            "folders": {
                name: {
                    "uidvalidity": f["uidvalidity"],
                    "message_ids": sorted(f["message_ids"]),
                    "uids": sorted(f["uids"]),
                }
                for name, f in self._folders.items()
            }
        }
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(raw))
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)
