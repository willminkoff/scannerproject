#!/usr/bin/env python3
"""Tiny HTTP server that exposes SDRTrunk log tails + event log listings.

Endpoints:
  GET /sdrtrunk_app.log?tail=200000    → last N bytes of the app log
  GET /event_logs/                     → JSON list of event log files (name + size + mtime)
  GET /event_logs/<name>               → raw bytes of the given event log
  GET /health                          → {"ok": true}

Designed for sb3-ui on Neptune to fetch. Read-only.
"""
from __future__ import annotations
import os, json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

APP_LOG = Path(os.environ.get("SDRTRUNK_APP_LOG", "/home/willminkoff/SDRTrunk/logs/sdrtrunk_app.log"))
EVENT_LOG_DIR = Path(os.environ.get("SDRTRUNK_EVENT_LOG_DIR", "/home/willminkoff/SDRTrunk/event_logs"))
PORT = int(os.environ.get("SDRTRUNK_LOG_SERVER_PORT", "9200"))

def _tail_bytes(path: Path, n: int) -> bytes:
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > n:
                f.seek(size - n)
                f.readline()  # drop partial first line
            return f.read()
    except OSError:
        return b""

class H(BaseHTTPRequestHandler):
    server_version = "sdrtrunk-log-server/1.0"
    def log_message(self, fmt, *args): pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/health":
            body = json.dumps({"ok": True, "app_log_present": APP_LOG.is_file()}).encode()
            self._send(200, "application/json", body); return
        if u.path == "/sdrtrunk_app.log":
            n = int(q.get("tail", ["200000"])[0])
            body = _tail_bytes(APP_LOG, n)
            self._send(200, "text/plain", body); return
        if u.path == "/event_logs/":
            items = []
            if EVENT_LOG_DIR.is_dir():
                for p in sorted(EVENT_LOG_DIR.glob("*.log")):
                    st = p.stat()
                    items.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
            body = json.dumps({"items": items}).encode()
            self._send(200, "application/json", body); return
        if u.path.startswith("/event_logs/"):
            name = u.path[len("/event_logs/"):]
            p = EVENT_LOG_DIR / name
            if p.is_file() and p.resolve().parent == EVENT_LOG_DIR.resolve():
                n = int(q.get("tail", ["200000"])[0])
                body = _tail_bytes(p, n)
                self._send(200, "text/plain", body); return
            self._send(404, "text/plain", b"not found"); return
        self._send(404, "text/plain", b"not found")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print(f"sdrtrunk-log-server listening on :{PORT}, app_log={APP_LOG}")
    srv.serve_forever()

if __name__ == "__main__":
    main()
