"""Host checks worth surfacing to the user.

Kept tiny and failure-tolerant: a probe that cannot answer returns None and the
UI simply says nothing. Never let a diagnostic break the app.
"""
from __future__ import annotations

import subprocess
import sys

# Turning TSO off costs a little CPU and throughput; it does not touch the app.
TSO_DISABLE_COMMAND = "sudo sysctl -w net.inet.tcp.tso=0"


def tso_enabled() -> bool | None:
    """macOS: is TCP Segmentation Offload on?

    With TSO the kernel hands the NIC oversized blocks to split up, building
    large mbuf chains to do it. Sustained bulk upload — exactly what a migration
    does — has been observed panicking the macOS send path while copying those
    chains (`m_copym_with_hdrs ... copy overflow`). Disabling it makes the kernel
    segment normally and avoids that code path entirely.

    Returns None on any other OS, or when the value cannot be read.
    """
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(
            ["sysctl", "-n", "net.inet.tcp.tso"],
            capture_output=True, text=True, timeout=2, check=True,
        )
    except Exception:
        return None
    value = out.stdout.strip()
    return value == "1" if value in ("0", "1") else None
