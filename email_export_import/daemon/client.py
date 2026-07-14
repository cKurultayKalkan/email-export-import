"""GUI-side client for the daemon's loopback API. Standard library only.

Exposes a small surface the GUI can use in place of talking to a RunManager
directly; the read methods mirror RunManager (runs/active_count) so swapping
one for the other is mechanical.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class DaemonError(Exception):
    """Any non-2xx response or transport failure from the daemon."""


class DaemonClient:
    def __init__(self, base_url: str, token: str, timeout: float = 5.0) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self._base + path, data=data, method=method,
            headers={"X-Auth-Token": self._token,
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:  # noqa: S310
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            raise DaemonError(f"{exc.code} {exc.reason}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise DaemonError(str(exc)) from exc

    # ---- reads ----
    def ping(self) -> dict:
        return self._request("GET", "/ping")

    def is_alive(self) -> bool:
        try:
            return bool(self.ping().get("ok"))
        except DaemonError:
            return False

    def runs(self) -> list[dict]:
        return self._request("GET", "/runs").get("runs", [])

    def active_count(self) -> int:
        return sum(1 for r in self.runs()
                   if r.get("status") in ("running", "stopping"))

    def get_settings(self) -> dict:
        return self._request("GET", "/settings")

    # ---- controls ----
    def pause(self, key: str) -> None:
        self._request("POST", f"/runs/{key}/pause")

    def cancel(self, key: str) -> None:
        self._request("POST", f"/runs/{key}/cancel")

    def dismiss(self, key: str) -> None:
        self._request("POST", f"/runs/{key}/dismiss")

    def set_settings(self, settings: dict) -> None:
        self._request("POST", "/settings", settings)

    def plan(self, src: dict, dst: dict, skip: list) -> dict:
        """Connect + build a folder plan. src/dst carry the password in memory
        only — the daemon holds the live connections until start()."""
        return self._request("POST", "/plan",
                             {"src": src, "dst": dst, "skip": skip})

    def start(self, plan_id: str, skip: list, workers: int,
              spool: bool = False) -> dict:
        return self._request("POST", "/start",
                             {"plan_id": plan_id, "skip": skip,
                              "workers": workers, "spool": spool})

    def shutdown(self) -> None:
        self._request("POST", "/shutdown")
