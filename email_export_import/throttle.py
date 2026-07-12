"""Bound how fast message bodies are pushed at the network.

Sustained, high-rate bulk TCP writes are what stress a machine's send path —
that, not CPU or RAM, is the variable worth limiting (a migration is nowhere
near CPU- or memory-bound). A rate cap is also plainly useful on a weak uplink
or against a server that throttles clients.

Implemented as a virtual-time pacer rather than a token bucket: each caller
reserves the slot at which its bytes are allowed to go out, so a message of any
size is accounted for exactly and a message larger than one second of budget can
never deadlock the way a capped bucket would.
"""
from __future__ import annotations

import threading
import time
from typing import Callable


class RateLimiter:
    """Thread-safe, shared by every worker of a run. `bytes_per_sec <= 0`
    means unlimited and makes acquire() a no-op."""

    def __init__(
        self,
        bytes_per_sec: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.bytes_per_sec = bytes_per_sec
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = 0.0  # virtual time the next send is allowed at

    def acquire(self, n_bytes: int, cancel: threading.Event | None = None) -> None:
        """Block until *n_bytes* may be sent. Returns early if *cancel* is set,
        so a paused/cancelled run never sits waiting on the limiter."""
        if self.bytes_per_sec <= 0 or n_bytes <= 0:
            return

        with self._lock:
            now = self._clock()
            # Never let an idle period bank unlimited credit: the next slot is
            # at the earliest "now".
            start = self._next if self._next > now else now
            self._next = start + n_bytes / self.bytes_per_sec

        while True:
            remaining = start - self._clock()
            if remaining <= 0:
                return
            if cancel is not None and cancel.is_set():
                return
            self._sleep(min(remaining, 0.1))  # short slices keep cancel responsive
