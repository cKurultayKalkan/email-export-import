"""Read/merge/write the GUI preferences file (``gui.json``).

A single writer so setting one preference never clobbers another (the old
locale writer replaced the whole file, wiping any other key). 0600 because it
sits in the user's home and may sit next to other app data.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def load_prefs(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}  # missing / unreadable / malformed → no prefs


def save_pref(path: Path, key: str, value) -> None:
    path = Path(path)
    data = load_prefs(path)
    data[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(data))
    os.chmod(path, 0o600)
