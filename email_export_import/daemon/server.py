"""Headless daemon: owns a RunManager and exposes it over a token-guarded
loopback HTTP API so the GUI (a separate process that may come and go) can
drive migrations that outlive it.

Standard library only — no extra dependency ships in the packaged app. The
API is intentionally tiny and JSON-in/JSON-out; the GUI's DaemonClient is the
only intended caller.
"""
from __future__ import annotations

import dataclasses
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..gui.run_manager import RunManager


def _snapshot_dict(snap) -> dict:
    d = dataclasses.asdict(snap) if dataclasses.is_dataclass(snap) else dict(snap)
    # TransferProgress isn't JSON-serialisable and the wire only needs a
    # summary; drop the live object and expose the counts the client uses.
    result = d.pop("result", None)
    if result is not None:
        d["result"] = {
            "migrated": getattr(result, "migrated", 0),
            "skipped": getattr(result, "skipped", 0),
            "failed": getattr(result, "failed", 0),
            "failures": list(getattr(result, "failures", []))[:5],
        }
    return d


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence stderr access logging
        pass

    @property
    def _server(self) -> "DaemonServer":
        return self.server.daemon  # type: ignore[attr-defined]

    def _authed(self) -> bool:
        return self.headers.get("X-Auth-Token") == self._server.token

    def _send(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode() or "{}")
        except ValueError:
            return {}

    def do_GET(self) -> None:
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        m = self._server.manager
        if self.path == "/ping":
            from .. import __version__

            return self._send(200, {"ok": True, "version": __version__})
        if self.path == "/runs":
            return self._send(200, {"runs": [_snapshot_dict(s)
                                             for s in m.snapshot_all()]})
        if self.path == "/settings":
            return self._send(200, {"max_active": m.max_active,
                                    "workers": m.workers,
                                    "rate_limit": m.rate_limit})
        return self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        m = self._server.manager
        body = self._read_json()
        parts = self.path.strip("/").split("/")

        if self.path == "/settings":
            for field in ("max_active", "workers", "rate_limit"):
                if field in body:
                    setattr(m, field, int(body[field]))
            return self._send(200, {"ok": True})

        if self.path == "/shutdown":
            self._send(200, {"ok": True})
            self._server.request_stop()
            return

        # /runs/<key>/<action>
        if len(parts) == 3 and parts[0] == "runs":
            key, action = parts[1], parts[2]
            if action == "dismiss":
                m.remove(key)
                return self._send(200, {"ok": True})
            run = m.get(key)
            if run is None:
                return self._send(404, {"error": "no such run"})
            if action == "pause":
                run.pause()
            elif action == "cancel":
                run.cancel()
            else:
                return self._send(400, {"error": "unknown action"})
            return self._send(200, {"ok": True})

        return self._send(404, {"error": "not found"})


class DaemonServer:
    """Wraps a RunManager in a threaded loopback HTTP server."""

    def __init__(self, manager: RunManager, host: str = "127.0.0.1",
                 port: int = 0, token: str = "") -> None:
        self.manager = manager
        self.token = token
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.daemon = self  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None
        self._on_stop = None  # optional callback (daemon main loop wakeup)

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        """Ask the owner to shut down (from inside a request handler, where
        calling shutdown() directly would deadlock the serving thread)."""
        if self._on_stop is not None:
            self._on_stop()
        else:
            threading.Thread(target=self.stop, daemon=True).start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
