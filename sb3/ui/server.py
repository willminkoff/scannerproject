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
        if p == "/api/system":
            return self._json(routes.build_system(self._state))
        if p == "/api/hp/state":
            return self._json(routes.hp_state(self._state))
        if p == "/api/hits":
            return self._json(routes.hits(self._state))
        if p == "/api/digital/scheduler":
            return self._json(routes.digital_scheduler(self._state))
        if p == "/api/digital/preflight":
            return self._json(routes.digital_preflight(self._state))
        if p == "/api/digital/profiles":
            return self._json(routes.digital_profiles(self._state))
        # Favorites Builder wizard (location → systems → channels). Static
        # countries/states always; counties/systems/channels real when the
        # HomePatrol dump is present, else an empty list + note.
        if p == "/api/scan/state":
            return self._json(routes.wizard_scan_state(self._state))
        if p == "/api/scan/devices":
            return self._json(routes.wizard_devices())
        if p.startswith("/api/scan/favorites-wizard/"):
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1]
                                      if "?" in self.path else "")
            def _qi(key, default=0):
                try:
                    return int(q.get(key, [default])[-1])
                except (TypeError, ValueError):
                    return default
            def _qs(key, default=""):
                return q.get(key, [default])[-1]
            leaf = p.rsplit("/", 1)[-1]
            if leaf == "countries":
                return self._json(routes.wizard_countries())
            if leaf == "states":
                return self._json(routes.wizard_states(_qi("country_id", 1)))
            if leaf == "counties":
                return self._json(routes.wizard_counties(_qi("state_id", 0)))
            if leaf == "systems":
                return self._json(routes.wizard_systems(
                    _qi("state_id", 0), _qi("county_id", 0),
                    _qs("system_type", "digital"), _qs("scope", "statewide")))
            if leaf == "channels":
                return self._json(routes.wizard_channels(
                    _qs("system_type", "digital"), _qs("system_id", ""),
                    _qi("limit", 5000)))
        if p == "/healthz":
            return self._json({"ok": True, "service": "sb3-ui"})
        # Unknown GET /api/* → empty-but-ok so the defensive UI degrades quietly.
        if p.startswith("/api/"):
            return self._json({"ok": False, "error": "not-implemented-in-3.1",
                               "path": p})
        self._json({"error": "not found", "path": p}, 404)

    def _json_body(self) -> dict:
        """Parse a JSON request body. {} if absent or unparseable."""
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode())
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

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

        # /api/scan/state carries JSON; everything else is form-encoded. The
        # body can only be read once, so choose by path before consuming it.
        self._scan_body = None
        if p == "/api/scan/state":
            try:
                self._scan_body = self._json_body()
                form = {}
            except Exception as exc:
                return self._json({"ok": False, "error": f"bad body: {exc}"}, 400)
        else:
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
            if p == "/api/scan/state":
                # Two different callers POST here. The wizard's Save sends a
                # profile request ({name, device_serial, channels}); the older
                # favorites/filters panels send a favorites-state blob. Dispatch
                # on SHAPE rather than assuming — misreading a filters save as a
                # profile would write a garbage file, and answering "not
                # implemented" to a real Save would be the old silent failure.
                body = getattr(self, "_scan_body", None)
                if body is None:
                    body = {}
                if body.get("name") and isinstance(body.get("channels"), list):
                    return self._json(routes.wizard_save_profile(body, state))
                return self._json({
                    "ok": False,
                    "error": "favorites-list persistence is not implemented in "
                             "SB3 yet — only the wizard's analog profile save "
                             "is wired (send name + channels).",
                }, 501)
            if p == "/api/vfo/mute":
                # Accept the flag from the query string OR the form body — the
                # endpoint is specified as ?state=on|off, and postAPI sends a
                # body; supporting both means neither caller has to care.
                merged = dict(form)
                if "?" in self.path:
                    q = urllib.parse.parse_qs(self.path.split("?", 1)[1])
                    for k, v in q.items():
                        merged.setdefault(k, v[-1])
                return self._json(routes.vfo_mute(merged, state))
            # Ask Claude is not wired into SB3 — answer 200 with a graceful
            # marker the chat panel renders as a message (not a raw HTTP error).
            if p == "/api/ask-claude":
                return self._json(routes.ask_claude_stub(form, state))
            if p == "/api/ask-claude/reset":
                return self._json({"ok": True})
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
