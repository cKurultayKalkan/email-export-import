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


# Connect / login / folder-scan run behind a spinner and can legitimately take
# tens of seconds on a slow link or a rate-limiting server (Yandex login
# backoff). A short HTTP timeout would give up early and mislabel it "timed
# out", even masking the real error (e.g. a bad password the daemon would have
# reported). These operations get a generous ceiling; the daemon still enforces
# its own per-socket IMAP timeouts underneath.
SLOW_OP_TIMEOUT = 180.0


class DaemonClient:
    def __init__(self, base_url: str, token: str, timeout: float = 5.0) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None,
                 timeout: float | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self._base + path, data=data, method=method,
            headers={"X-Auth-Token": self._token,
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(  # noqa: S310
                    req, timeout=self._timeout if timeout is None else timeout) as r:
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

    def events(self) -> dict:
        """Heartbeat + one-shot tray requests: {show, quit}. Polled ~5x/sec on
        the GUI event loop, so use a short timeout: a wedged daemon must not
        freeze the UI for the full default timeout every tick."""
        return self._request("GET", "/events", timeout=1.5)

    def gui_alive(self) -> bool:
        try:
            return bool(self._request("GET", "/gui-alive").get("alive"))
        except DaemonError:
            return False

    def request_show(self) -> None:
        self._request("POST", "/request-show")

    # ---- controls ----
    def pause(self, key: str) -> None:
        self._request("POST", f"/runs/{key}/pause")

    def cancel(self, key: str) -> None:
        self._request("POST", f"/runs/{key}/cancel")

    def dismiss(self, key: str) -> None:
        self._request("POST", f"/runs/{key}/dismiss")

    def set_settings(self, settings: dict) -> None:
        self._request("POST", "/settings", settings)

    def test_connection(self, account: dict) -> dict:
        return self._request("POST", "/test-connection", {"account": account},
                             timeout=SLOW_OP_TIMEOUT)

    def add_placeholder(self, src_email: str, dst_email: str) -> str:
        return self._request("POST", "/placeholder",
                             {"src_email": src_email, "dst_email": dst_email})["key"]

    def save_config(self, key: str, config: dict) -> None:
        self._request("POST", f"/runs/{key}/config", {"config": config})

    def mark_failed(self, key: str, message: str) -> None:
        self._request("POST", f"/runs/{key}/fail", {"message": message})

    def plan(self, src: dict, dst: dict, skip: list) -> dict:
        """Connect + build a folder plan. src/dst carry the password in memory
        only — the daemon holds the live connections until start()."""
        return self._request("POST", "/plan",
                             {"src": src, "dst": dst, "skip": skip},
                             timeout=SLOW_OP_TIMEOUT)

    def start(self, plan_id: str, skip: list, workers: int,
              spool: bool = False) -> dict:
        return self._request("POST", "/start",
                             {"plan_id": plan_id, "skip": skip,
                              "workers": workers, "spool": spool})

    def shutdown(self) -> None:
        self._request("POST", "/shutdown")
