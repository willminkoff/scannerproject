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
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..gitdeploy import deploy_root
from ..state import State
from . import routes

DEFAULT_PORT = int(os.environ.get("SB3_UI_PORT", "5050"))

# The frozen SB3 UI, served verbatim from the repo/deploy checkout — never
# copied or duplicated (single source of truth).
SB3_HTML = deploy_root() / "ui" / "sb3.html"

_SCAN_RE = re.compile(r"^/api/scan/(start|stop)$")


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
        if p == "/healthz":
            return self._json({"ok": True, "service": "sb3-ui"})
        # Unknown GET /api/* → empty-but-ok so the defensive UI degrades quietly.
        if p.startswith("/api/"):
            return self._json({"ok": False, "error": "not-implemented-in-3.1",
                               "path": p})
        self._json({"error": "not found", "path": p}, 404)

    def do_POST(self):
        p = self.path.split("?", 1)[0]
        # Seed routes from scannerctl — wired for real in Phase 3.2.
        if _SCAN_RE.match(p):
            return self._json(routes.not_wired("scan"), 501)
        if p == "/api/squelch":
            return self._json(routes.not_wired("squelch"), 501)
        if p == "/api/digital/restart":
            return self._json(routes.not_wired("digital/restart"), 501)
        if p.startswith("/api/"):
            return self._json({"ok": False, "error": "not-implemented-in-3.1",
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
