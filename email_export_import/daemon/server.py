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
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..models import Account
from ..state import MigrationState
from ..gui.controller import Controller
from ..gui.run_manager import Run, RunManager


def _account(cfg: dict) -> Account:
    return Account(
        host=cfg["host"], port=int(cfg.get("port", 993)),
        ssl=bool(cfg.get("ssl", True)), email=cfg["email"],
        password=cfg.get("password", ""),
        verify_ssl=bool(cfg.get("verify_ssl", True)),
    )


def _snapshot_dict(snap, run=None) -> dict:
    d = dataclasses.asdict(snap) if dataclasses.is_dataclass(snap) else dict(snap)
    if run is not None:
        # Everything the side panel reads off a Run's state, shipped with the
        # snapshot so the GUI never reaches into a Run object.
        st = run.state
        d["config"] = st.config
        d["folder_counts"] = st.folder_done_counts()
        d["last_run"] = st.last_run
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
            out = [_snapshot_dict(s, run=m.get(s.key)) for s in m.snapshot_all()]
            return self._send(200, {"runs": out})
        if self.path == "/settings":
            return self._send(200, {"max_active": m.max_active,
                                    "workers": m.workers,
                                    "rate_limit": m.rate_limit})
        if self.path == "/events":
            # The GUI polls this every tick: it doubles as a heartbeat (so the
            # daemon knows a GUI is alive) and delivers tray requests. `show` is
            # one-shot (cleared on read); `quit` latches until the daemon stops,
            # so the GUI can't miss it on a dropped tick.
            self._server.note_gui_seen()
            return self._send(200, self._server.take_events())
        if self.path == "/gui-alive":
            # Used by a second GUI launch to decide whether to bow out; does NOT
            # count as a heartbeat itself.
            return self._send(200, {"alive": self._server.gui_alive()})
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

        if self.path == "/placeholder":
            se, de = body["src_email"], body["dst_email"]
            key = f"{se}__{de}"
            srv_m = self._server.manager
            run = srv_m.get(key)
            if run is None or not run.is_active:
                state = MigrationState.for_pair(se, de, base_dir=srv_m.state_dir)
                srv_m.add(Run.placeholder(state, state_dir=srv_m.state_dir))
            return self._send(200, {"ok": True, "key": key})

        if self.path == "/test-connection":
            return self._do_test(body)
        if self.path == "/plan":
            return self._do_plan(body)
        if self.path == "/start":
            return self._do_start(body)

        if self.path == "/request-show":
            self._server.request_show()
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
            if action == "config":
                run = m.get(key)
                if run is not None:
                    run.state.set_config(body.get("config", {}))
                    run.state.flush()
                return self._send(200, {"ok": True})
            run = m.get(key)
            if run is None:
                return self._send(404, {"error": "no such run"})
            if action == "pause":
                run.pause()
            elif action == "cancel":
                run.cancel()
            elif action == "fail":
                run.mark_failed(body.get("message", "failed"))
            else:
                return self._send(400, {"error": "unknown action"})
            return self._send(200, {"ok": True})

        return self._send(404, {"error": "not found"})

    def _do_test(self, body: dict):
        # Validate credentials without holding the connection: connect, then
        # close. The real connect happens at /plan.
        res = self._server.controller.test_connection(_account(body["account"]))
        if res.ok:
            res.conn.close()
            return self._send(200, {"ok": True})
        return self._send(200, {"ok": False, "kind": res.kind,
                                "message": res.message})

    def _do_plan(self, body: dict):
        srv = self._server
        try:
            src = srv.controller.test_connection(_account(body["src"]))
            if not src.ok:
                return self._send(400, {"error": src.message or "source failed",
                                        "kind": src.kind})
            try:
                dst = srv.controller.test_connection(_account(body["dst"]))
                if not dst.ok:
                    src.conn.close()
                    return self._send(400, {"error": dst.message or "dest failed",
                                            "kind": dst.kind})
                skip = set(body.get("skip", []))
                plan = srv.controller.build_plan(src.conn, dst.conn, skip)
            except Exception:
                src.conn.close()
                raise
        except Exception as exc:  # noqa: BLE001
            return self._send(400, {"error": str(exc)})
        plan_id = secrets.token_urlsafe(12)
        srv.pending_plans[plan_id] = (src.conn, dst.conn, plan, skip)
        return self._send(200, {
            "plan_id": plan_id,
            "total": plan.total,
            "folders": [{"source": p.source, "dest": p.dest,
                         "count": plan.counts.get(p.source, 0),
                         "create": bool(getattr(p, "create", False))}
                        for p in plan.plans],
        })

    def _do_start(self, body: dict):
        srv = self._server
        held = srv.pending_plans.pop(body.get("plan_id", ""), None)
        if held is None:
            return self._send(404, {"error": "unknown or expired plan"})
        src_conn, dst_conn, plan, default_skip = held
        skip = set(body.get("skip", default_skip))
        workers = int(body.get("workers", srv.manager.default_workers()))
        spool = bool(body.get("spool", False))
        active = [p for p in plan.plans if p.source not in skip]
        total = sum(plan.counts.get(p.source, 0) for p in active)
        se, de = src_conn.account.email, dst_conn.account.email
        key = f"{se}__{de}"
        state = MigrationState.for_pair(se, de, base_dir=srv.manager.state_dir)
        run = Run(key=key, title=f"{se} → {de}", src_conn=src_conn, dst_conn=dst_conn,
                  plans=active, state=state, workers=workers, total=total, skip=skip,
                  spool_enabled=spool, rate_limit=srv.manager.rate_limit,
                  state_dir=srv.manager.state_dir)
        srv.manager.add(run)
        run.start()
        return self._send(200, {"ok": True, "key": key})


class DaemonServer:
    """Wraps a RunManager in a threaded loopback HTTP server."""

    def __init__(self, manager: RunManager, host: str = "127.0.0.1",
                 port: int = 0, token: str = "") -> None:
        self.manager = manager
        self.token = token
        self.controller = Controller(state_dir=manager.state_dir)
        # plan_id -> (src_conn, dst_conn, PlanResult, skip) awaiting /start.
        self.pending_plans: dict = {}
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.daemon = self  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None
        self._on_stop = None  # optional callback (daemon main loop wakeup)
        # GUI coordination: last time a GUI heartbeated (/events), and one-shot
        # "reveal the window" / "quit the GUI" requests set by the tray.
        self._gui_seen = 0.0
        self._show_requested = False
        self._quit_requested = False

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    # ---- GUI coordination ----
    def note_gui_seen(self) -> None:
        self._gui_seen = time.monotonic()

    def gui_alive(self, within: float = 4.0) -> bool:
        """True if a GUI heartbeated recently — so a launch/tray-open can reveal
        the existing window instead of spawning another."""
        return (time.monotonic() - self._gui_seen) < within

    def request_show(self) -> None:
        self._show_requested = True

    def request_quit_gui(self) -> None:
        self._quit_requested = True

    def take_events(self) -> dict:
        # `show` is one-shot; `quit` latches (a full-quit must not be missed on
        # a dropped poll, and it only fires when the daemon is stopping anyway).
        show, quit_ = self._show_requested, self._quit_requested
        self._show_requested = False
        return {"show": show, "quit": quit_}

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
