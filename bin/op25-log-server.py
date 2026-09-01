#!/usr/bin/env python3
"""HTTP server exposing op25 log tail and per-instance status for remote sb3-ui.

Endpoints:
  GET /op25.log?tail=N   → last N bytes of op25.log
  GET /instances         → contents of instances.json (audio ports, http status ports, per system)
  GET /status/<idx>      → passthrough of op25 HTTP status API for instance idx
  GET /health            → {"ok": true}
"""
from __future__ import annotations
import os, json, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

APP_LOG = Path(os.environ.get("OP25_APP_LOG", "/var/log/op25/op25.log"))
INSTANCES_JSON = Path(os.environ.get("OP25_INSTANCES_PATH", "/run/scannerproject/op25/instances.json"))
PORT = int(os.environ.get("OP25_LOG_SERVER_PORT", "9200"))

def _tail_bytes(path: Path, n: int) -> bytes:
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > n:
                f.seek(size - n)
                f.readline()
            return f.read()
    except OSError:
        return b""

class H(BaseHTTPRequestHandler):
    server_version = "op25-log-server/1.0"
    def log_message(self, fmt, *args): pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/health":
            body = json.dumps({"ok": True, "app_log_present": APP_LOG.is_file(), "instances_present": INSTANCES_JSON.is_file()}).encode()
            self._send(200, "application/json", body); return
        if u.path == "/op25.log":
            n = int(q.get("tail", ["200000"])[0])
            body = _tail_bytes(APP_LOG, n)
            self._send(200, "text/plain", body); return
        if u.path == "/instances":
            try: body = INSTANCES_JSON.read_bytes()
            except OSError: body = b"[]"
            self._send(200, "application/json", body); return
        if u.path.startswith("/status/"):
            idx = u.path[len("/status/"):]
            try:
                inst = json.loads(INSTANCES_JSON.read_text())
                port = None
                for item in inst:
                    if str(item.get("process_index")) == idx or str(item.get("channel_name")) == idx:
                        port = item.get("http_status_port"); break
                if not port:
                    self._send(404, "application/json", b"{\"error\":\"instance not found\"}"); return
                try:
                    resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
                    body = resp.read()
                except Exception as e:
                    body = json.dumps({"error": str(e)}).encode()
                self._send(200, "application/json", body); return
            except Exception as e:
                self._send(500, "application/json", json.dumps({"error": str(e)}).encode()); return
        self._send(404, "text/plain", b"not found")

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers(); self.wfile.write(body)

if __name__ == "__main__":
    print(f"op25-log-server listening on :{PORT} (log={APP_LOG})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
