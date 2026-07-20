"""sb3.ui.server — stdlib http.server hosting sb3.html + the /api/* shim.

Run:  SB3_UI_PORT=5050 python3 -m sb3.ui

Idempotent by construction: it holds NO state of its own. Every request reads
live backend state through sb3.backends, so a restart (e.g. after `sb3-ctl kill`
→ `resume`) immediately reflects reality with nothing to rebuild. That is what
makes it safe to be an SB3-owned agent that dies on kill and comes back on
resume.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..gitdeploy import deploy_root
from ..state import State
from . import routes

DEFAULT_PORT = int(os.environ.get("SB3_UI_PORT", "5050"))

# The frozen SB3 UI, served verbatim from the repo/deploy checkout — never
# copied or duplicated (single source of truth).
SB3_HTML = deploy_root() / "ui" / "sb3.html"



class Handler(BaseHTTPRequestHandler):
    server_version = "sb3-ui/3.1"

    # quiet default logging (launchd captures stdout separately)
    def log_message(self, fmt, *args):
        pass

    # -- helpers ----------------------------------------------------------

    def _json(self, obj, code: int = 200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, path: Path):
        try:
            data = path.read_bytes()
        except OSError:
            self._json({"error": f"UI file missing: {path}"}, 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    @property
    def _state(self) -> State:
        return State()

    # -- routing ----------------------------------------------------------

    def do_GET(self):
        p = self.path.split("?", 1)[0]
        if p in ("/", "/sb3", "/index.html"):
            return self._html(SB3_HTML)
        if p == "/api/status":
            return self._json(routes.build_status(self._state))
        if p == "/api/heartbeat":
            return self._json(routes.build_heartbeat(self._state))
        if p == "/api/profiles":
            return self._json(routes.build_profiles(self._state))
        if p == "/api/hits":
            return self._json(routes.hits(self._state))
        if p == "/api/digital/scheduler":
            return self._json(routes.digital_scheduler(self._state))
        if p == "/api/digital/preflight":
            return self._json(routes.digital_preflight(self._state))
        if p == "/api/digital/profiles":
            return self._json(routes.digital_profiles(self._state))
        if p == "/healthz":
            return self._json({"ok": True, "service": "sb3-ui"})
        # Unknown GET /api/* → empty-but-ok so the defensive UI degrades quietly.
        if p.startswith("/api/"):
            return self._json({"ok": False, "error": "not-implemented-in-3.1",
                               "path": p})
        self._json({"error": "not found", "path": p}, 404)

    def _form(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode() if length else ""
        # postAPI sends application/x-www-form-urlencoded; take the last value.
        return {k: v[-1] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def do_POST(self):
        p = self.path.split("?", 1)[0]
        state = self._state

        # Fail fast if a kill is in progress — never write to a backend the
        # control plane is tearing away from (kill switch invariant).
        if state.is_killed():
            return self._json({"ok": False, "error": "sb3-killed",
                               "note": "SB3 is killed; writes refused"}, 409)

        try:
            form = self._form()
        except Exception as exc:
            return self._json({"ok": False, "error": f"bad body: {exc}"}, 400)

        try:
            if p in ("/api/apply", "/api/apply-batch"):
                return self._json(routes.apply_controls(
                    form, state, with_filter=(p == "/api/apply-batch")))
            if p == "/api/filter":
                return self._json(routes.apply_filter(form, state))
            if p == "/api/tune":
                return self._json(routes.tune(form, state))
            if p == "/api/volume":
                return self._json(routes.volume(form, state))
        except routes.WriteError as we:
            return self._json({"ok": False, "error": we.msg}, we.code)
        except Exception as exc:   # never hang the UI on an unexpected fault
            return self._json({"ok": False, "error": f"internal: {exc}"}, 500)

        if p.startswith("/api/"):
            return self._json({"ok": False, "error": "not-implemented",
                               "path": p}, 501)
        self._json({"error": "not found", "path": p}, 404)


def make_server(port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("0.0.0.0", port), Handler)


def main() -> int:
    port = DEFAULT_PORT
    srv = make_server(port)
    print(f"sb3-ui up on 0.0.0.0:{port} — serving {SB3_HTML}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
