"""Optional write-through disk spool for downloaded messages.

A message is written to the spool right after it is downloaded and removed
right after it is uploaded, so only messages whose upload failed accumulate.
The next run uploads those straight from disk instead of re-downloading them.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

DEFAULT_BASE_DIR = Path.home() / ".email-export-import"


@dataclass
class SpooledMessage:
    body: bytes
    flags: tuple[bytes, ...]
    internaldate: datetime | None


class MessageSpool:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)  # raw mail content — owner only

    @classmethod
    def for_pair(
        cls, src_email: str, dst_email: str, base_dir: Path | None = None
    ) -> "MessageSpool":
        base = base_dir or DEFAULT_BASE_DIR
        spool_dir = base / "spool"
        spool_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(base, 0o700)
        os.chmod(spool_dir, 0o700)
        return cls(spool_dir / f"{src_email}__{dst_email}")

    def _folder_dir(self, folder: str) -> Path:
        # quote() keeps the name readable while making it filesystem-safe
        # (slashes in IMAP folder names would otherwise nest directories).
        return self.path / quote(folder, safe="")

    def put(
        self,
        folder: str,
        uid: int,
        message_id: str | None,
        body: bytes,
        flags: tuple[bytes, ...],
        internaldate: datetime | None,
    ) -> None:
        d = self._folder_dir(folder)
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        eml = d / f"{uid}.eml"
        eml.write_bytes(body)
        os.chmod(eml, 0o600)
        meta = d / f"{uid}.json"
        meta.write_text(
            json.dumps(
                {
                    "message_id": message_id,
                    "flags": [f.decode("ascii", "replace") for f in flags],
                    "internaldate": internaldate.isoformat() if internaldate else None,
                }
            ),
            encoding="utf-8",
        )
        os.chmod(meta, 0o600)

    def get(self, folder: str, uid: int, message_id: str | None) -> SpooledMessage | None:
        """The spooled copy for (folder, uid), or None when absent or stale.

        UIDs are only unique within one UIDVALIDITY generation, so the copy
        is trusted only when its recorded Message-ID matches the live one.
        Corrupt or half-written entries also return None — the message is
        then simply downloaded again.
        """
        d = self._folder_dir(folder)
        try:
            meta = json.loads((d / f"{uid}.json").read_text(encoding="utf-8"))
            if meta.get("message_id") != message_id:
                return None
            body = (d / f"{uid}.eml").read_bytes()
            flags = tuple(f.encode("ascii") for f in meta.get("flags", []))
            raw_date = meta.get("internaldate")
            internaldate = datetime.fromisoformat(raw_date) if raw_date else None
        except (OSError, ValueError, KeyError):
            return None
        return SpooledMessage(body=body, flags=flags, internaldate=internaldate)

    def discard(self, folder: str, uid: int) -> None:
        d = self._folder_dir(folder)
        (d / f"{uid}.eml").unlink(missing_ok=True)
        (d / f"{uid}.json").unlink(missing_ok=True)

    def pending_count(self) -> int:
        if not self.path.is_dir():
            return 0
        return sum(1 for _ in self.path.glob("*/*.eml"))
