"""PyInstaller entry point for the packaged daemon binary (eei-daemon).

A standalone script run by PyInstaller executes as `__main__`, which breaks
the daemon package's relative imports. This launcher imports the package
absolutely instead, so the same code works both as
`python -m email_export_import.daemon` and as the frozen sidecar.
"""
import os
from pathlib import Path

from email_export_import.daemon.__main__ import main

if __name__ == "__main__":
    base = os.environ.get("EEI_BASE_DIR")
    main(base_dir=Path(base) if base else None)
