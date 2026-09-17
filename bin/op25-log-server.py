#!/usr/bin/env python3
"""HTTP server exposing op25 log tail, per-instance status, and profile-apply.

Endpoints:
  GET  /op25.log?tail=N   — last N bytes of op25.log
  GET  /instances         — contents of instances.json
  GET  /status/<idx>      — passthrough of op25 HTTP status API for instance idx
  GET  /hits?limit=N      — parsed recent voice_update events as {ts,tg,rid,freq_mhz,slot,prio}
  GET  /profiles          — list profile dirs + active
  GET  /health            — {"ok": true}
  POST /apply-profile     — accept a digital profile blob, write it, restart op25
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

APP_LOG = Path(os.environ.get("OP25_APP_LOG", "/var/log/op25/op25.log"))
CHIRP_AIRBAND_LOG = Path(os.environ.get("CHIRP_AIRBAND_LOG", "/home/willminkoff/.local/share/chirp/logs/airband.err.log"))
INSTANCES_JSON = Path(os.environ.get("OP25_INSTANCES_PATH", "/run/scannerproject/op25/instances.json"))
PROFILES_DIR = Path(os.environ.get("OP25_PROFILES_DIR", "/etc/scannerproject/digital/profiles"))
ACTIVE_LINK = Path(os.environ.get("OP25_ACTIVE_LINK", "/etc/scannerproject/digital/active"))
DONGLE_ASSIGNMENTS_PATH = Path(os.environ.get(
    "OP25_DONGLE_ASSIGNMENTS_PATH",
    str(Path.home() / ".local" / "state" / "scannerproject" / "airband_ui_dongle_assignments.json"),
))
OP25_SERVICE = os.environ.get("OP25_SERVICE_NAME", "scanner-digital-op25")
PORT = int(os.environ.get("OP25_LOG_SERVER_PORT", "9200"))

_TS_RE = re.compile(r"^(\d{2}/\d{2}/\d{2})\s+(\d{2}:\d{2}:\d{2})\.(\d+)")
_VOICE_RE = re.compile(
    r"voice update:\s+tg\((\d+)\),\s+rid\((\d+)\),\s+freq\(([\d.]+)\),\s+slot\((\S+)\),\s+prio\((\d+)\)"
)


def _tail_bytes(path, n):
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > n:
                f.seek(size - n)
                f.readline()
            return f.read()
    except OSError:
        return b""


def _epoch(mmdd, hhmmss, ms):
    try:
        month, day, year = mmdd.split("/")
        year = int(year) + 2000 if int(year) < 100 else int(year)
        struct = time.strptime(f"{year}-{month}-{day} {hhmmss}", "%Y-%m-%d %H:%M:%S")
        return time.mktime(struct) + (int(ms) / 10 ** len(ms) if ms else 0)
    except Exception:
        return None


def _parse_hits(log_bytes, limit):
    hits = []
    for raw in log_bytes.splitlines():
        line = raw.decode("utf-8", errors="ignore")
        vm = _VOICE_RE.search(line)
        if not vm:
            continue
        m = _TS_RE.match(line)
        ts = _epoch(*m.groups()) if m else None
        hits.append({
            "ts": ts,
            "tg": int(vm.group(1)),
            "rid": int(vm.group(2)),
            "freq_mhz": float(vm.group(3)),
            "slot": vm.group(4),
            "prio": int(vm.group(5)),
        })
    return hits[-limit:]


def _write_profile(body):
    name = str(body.get("name") or "").strip()
    if not name or "/" in name or name.startswith("."):
        return {"ok": False, "error": "invalid name"}
    prof_dir = PROFILES_DIR / name
    prof_dir.mkdir(parents=True, exist_ok=True)

    systems = body.get("systems") or []
    (prof_dir / "systems.json").write_text(json.dumps({"systems": systems}, indent=2) + "\n")

    overrides = body.get("op25_overrides") or {}
    (prof_dir / "op25_system_config.json").write_text(json.dumps(overrides, indent=2) + "\n")

    tgs = body.get("talkgroups") or []
    lines = ["DEC,HEX,Mode,Alpha Tag,Description"]
    for tg in tgs:
        dec = str(tg.get("dec") or "").strip()
        hex_ = str(tg.get("hex") or "").strip()
        mode = str(tg.get("mode") or "D").strip()
        alpha = str(tg.get("alpha") or "").replace(",", " ").strip()
        desc = str(tg.get("description") or "").replace(",", " ").strip()
        if dec:
            lines.append(f"{dec},{hex_},{mode},{alpha},{desc}")
    (prof_dir / "talkgroups.csv").write_text("\n".join(lines) + "\n")

    ccs = []
    for s in systems:
        for f in s.get("control_channels_mhz", []) or []:
            ccs.append(str(f))
    (prof_dir / "control_channels.txt").write_text("\n".join(ccs) + "\n")

    result = {"ok": True, "name": name, "profile_dir": str(prof_dir)}

    dongles = body.get("dongle_assignments")
    if isinstance(dongles, list) and dongles:
        DONGLE_ASSIGNMENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        DONGLE_ASSIGNMENTS_PATH.write_text(json.dumps({"assignments": dongles}, indent=2) + "\n")
        result["dongle_assignments"] = str(DONGLE_ASSIGNMENTS_PATH)

    if not body.get("activate", True):
        result["activated"] = False
        result["restarted"] = False
        return result

    try:
        if ACTIVE_LINK.is_symlink() or ACTIVE_LINK.exists():
            ACTIVE_LINK.unlink()
        ACTIVE_LINK.symlink_to(prof_dir)
        result["activated"] = True
    except OSError as exc:
        result["ok"] = False
        result["error"] = f"symlink failed: {exc!r}"
        return result

    try:
        # Kickstart sdrplay first to release stale handles that cause
        # MTRTRS Master to fail with no sdrplay device matches on cold
        # restart. Two calls: sdrplay first, then op25.
        subprocess.run(["sudo", "-n", "systemctl", "restart", "sdrplay"],
                       capture_output=True, text=True, timeout=15)
        import time as _t; _t.sleep(5)
        r = subprocess.run(
            ["sudo", "-n", "systemctl", "restart", OP25_SERVICE],
            capture_output=True, text=True, timeout=60,
        )
        result["restarted"] = (r.returncode == 0)
        if r.returncode != 0:
            result["restart_stderr"] = r.stderr.strip()[:400]
        # Bridge tends to wedge its priority-gate ring buffer around op25
        # restarts (silent mp3 output while real UDP audio flows). Always
        # bounce it after an apply so the mount comes back clean.
        import time as _t; _t.sleep(3)
        rb = subprocess.run(
            ["sudo", "-n", "systemctl", "restart", "scanner-digital-op25-audio"],
            capture_output=True, text=True, timeout=30,
        )
        result["audio_bridge_restarted"] = (rb.returncode == 0)
        if rb.returncode != 0:
            result["audio_bridge_stderr"] = rb.stderr.strip()[:400]
    except subprocess.TimeoutExpired:
        result["restarted"] = False
        result["restart_stderr"] = "timeout"

    return result


_POWER_OFF_SERVICES = [
    "scanner-chirp-airband",
    "scanner-digital-op25-audio",
    "scanner-digital-op25",
    "disco-interpret",
    "disco-classifier",
    "disco-dashboard",
]
_POWER_ON_SERVICES_ORDER = [
    "sdrplay",
    "scanner-digital-op25",
    "scanner-digital-op25-audio",
    "scanner-chirp-airband",
    "disco-classifier",
    "disco-interpret",
    "disco-dashboard",
]


def _run(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"cmd": " ".join(cmd), "rc": r.returncode, "out": (r.stdout or "").strip()[-400:], "err": (r.stderr or "").strip()[-400:]}
    except Exception as exc:
        return {"cmd": " ".join(cmd), "rc": -1, "err": repr(exc)}


def _power_off():
    lines = []
    # Stop each app service (best-effort; skip missing units).
    for svc in _POWER_OFF_SERVICES:
        lines.append(_run(["sudo", "systemctl", "stop", svc]))
    # Stop disco-sweep@* dynamically.
    lines.append(_run(["bash", "-c",
                        "sudo systemctl stop 'disco-sweep@*' 2>/dev/null || true"]))
    # Finally stop sdrplay so RSPduo firmware sleeps.
    lines.append(_run(["sudo", "systemctl", "stop", "sdrplay"]))
    return lines


def _power_on():
    lines = []
    for svc in _POWER_ON_SERVICES_ORDER:
        lines.append(_run(["sudo", "systemctl", "start", svc]))
        # Small gap between starts to let sdrplay register.
        if svc == "sdrplay":
            import time as _t; _t.sleep(4)
    # Bring disco sweep back for HackRF.
    lines.append(_run(["bash", "-c",
                        "for u in $(systemctl list-unit-files 'disco-sweep@*' 2>/dev/null | awk '{print $1}' | grep -v UNIT); do sudo systemctl start \"$u\"; done"]))
    return lines


class H(BaseHTTPRequestHandler):
    server_version = "op25-log-server/2.0"

    def log_message(self, fmt, *args):
        pass



    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/health":
            body = json.dumps({
                "ok": True,
                "app_log_present": APP_LOG.is_file(),
                "instances_present": INSTANCES_JSON.is_file(),
                "profiles_dir": str(PROFILES_DIR),
                "active_profile": str(ACTIVE_LINK.readlink()) if ACTIVE_LINK.is_symlink() else "",
            }).encode()
            return self._send(200, "application/json", body)
        if u.path == "/op25.log":
            n = int(q.get("tail", ["200000"])[0])
            return self._send(200, "text/plain", _tail_bytes(APP_LOG, n))
        if u.path == "/chirp/airband.out.log":
            n = int(q.get("tail", ["200000"])[0])
            return self._send(200, "text/plain", _tail_bytes(CHIRP_AIRBAND_LOG, n))
        if u.path == "/instances":
            try:
                body = INSTANCES_JSON.read_bytes()
            except OSError:
                body = b"[]"
            return self._send(200, "application/json", body)
        if u.path == "/hits":
            limit = int(q.get("limit", ["100"])[0])
            hits = _parse_hits(_tail_bytes(APP_LOG, 500_000), limit)
            body = json.dumps({"ok": True, "items": hits}).encode()
            return self._send(200, "application/json", body)
        if u.path == "/profiles":
            names = []
            if PROFILES_DIR.is_dir():
                names = sorted(p.name for p in PROFILES_DIR.iterdir() if p.is_dir())
            body = json.dumps({
                "ok": True,
                "profiles": names,
                "active": ACTIVE_LINK.readlink().name if ACTIVE_LINK.is_symlink() else "",
            }).encode()
            return self._send(200, "application/json", body)
        if u.path.startswith("/status/"):
            idx = u.path[len("/status/"):]
            try:
                inst = json.loads(INSTANCES_JSON.read_text())
                port = None
                for item in inst:
                    if str(item.get("process_index")) == idx or str(item.get("channel_name")) == idx:
                        port = item.get("http_status_port")
                        break
                if not port:
                    return self._send(404, "application/json", b'{"error":"instance not found"}')
                try:
                    resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
                    body = resp.read()
                except Exception as exc:
                    body = json.dumps({"error": str(exc)}).encode()
                return self._send(200, "application/json", body)
            except Exception as exc:
                return self._send(500, "application/json", json.dumps({"error": str(exc)}).encode())
        if u.path == "/power/status":
            state = "on" if _run(["systemctl", "is-active", "sdrplay"])["out"] == "active" else "off"
            body = json.dumps({"ok": True, "state": state}).encode()
            return self._send(200, "application/json", body)
        return self._send(404, "text/plain", b"not found")

    def do_POST(self):
        import traceback as _tb
        u = urlparse(self.path)
        if u.path == "/power/off":
            lines = _power_off()
            body = json.dumps({"ok": True, "state": "off", "lines": lines}).encode()
            return self._send(200, "application/json", body)
        if u.path == "/power/on":
            lines = _power_on()
            body = json.dumps({"ok": True, "state": "on", "lines": lines}).encode()
            return self._send(200, "application/json", body)
        if u.path != "/apply-profile":
            return self._send(404, "text/plain", b"not found")
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            return self._send(400, "application/json",
                              json.dumps({"ok": False, "error": f"bad JSON: {exc}"}).encode())
        try:
            result = _write_profile(body)
        except Exception as exc:
            tb = _tb.format_exc().splitlines()
            return self._send(500, "application/json",
                              json.dumps({"ok": False, "error": repr(exc), "traceback": tb[-6:]}).encode())
        return self._send(200, "application/json", json.dumps(result).encode())

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    print(f"op25-log-server listening on :{PORT} (log={APP_LOG}, profiles={PROFILES_DIR})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
