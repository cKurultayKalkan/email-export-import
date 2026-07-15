"""Tiny append-only diagnostic log.

The packaged desktop app and daemon have no console, so process-lifecycle
problems (window not showing, daemon not launching) are otherwise invisible.
Each component appends timestamped lines to `<base>/<component>.log` (0600).
No secrets are ever logged — only lifecycle events and paths.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from .state import DEFAULT_BASE_DIR


def log(component: str, msg: str, base_dir: Path | None = None) -> None:
    """Append one line to <base>/<component>.log. Never raises."""
    try:
        base = Path(base_dir) if base_dir is not None else DEFAULT_BASE_DIR
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{component}.log"
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} pid={os.getpid()} {msg}\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        pass
