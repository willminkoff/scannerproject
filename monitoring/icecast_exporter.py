#!/usr/bin/env python3
"""
icecast_exporter -- Prometheus sidecar for Icecast2.

Icecast2 does not speak Prometheus. This sidecar polls the Icecast admin
stats XML endpoint (/admin/stats) on a background timer, parses it, and
exposes the metrics in Prometheus text exposition format on /metrics.

SB6 Phase 0 (telemetry stack). See docs/sb6-phase0-install.md.

WHY A BACKGROUND POLL LOOP (instead of polling Icecast on each scrape):
  icecast_listener_byte_rate is a DERIVED rate. We compute it from the delta
  of total_bytes_sent between two polls a known interval apart, so the value
  is stable regardless of how often -- or how many -- Prometheus servers
  scrape /metrics. Each scrape returns the most recent cached computation.
  This also means a slow/hung Icecast never stalls a Prometheus scrape.

CONFIG -- entirely via environment variables (no secrets committed):

  ICECAST_STATS_URL          default http://localhost:8000/admin/stats
  ICECAST_ADMIN_USER         default "source"   (Icecast <admin-user>)
  ICECAST_ADMIN_PASSWORD     REQUIRED           (Icecast <admin-password>)
  ICECAST_EXPORTER_PORT      default 9146
  ICECAST_EXPORTER_BIND      default 127.0.0.1  (loopback; Prometheus is local)
  ICECAST_POLL_INTERVAL_SEC  default 15
  ICECAST_HTTP_TIMEOUT_SEC   default 5
  ICECAST_EXPORTER_DEBUG     default 0          (1 = verbose stderr logging)

ROLLBACK: this is a single stdlib-only file. `systemctl disable --now
icecast-exporter` stops it; nothing else on the host is touched.
"""

import base64
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# --------------------------------------------------------------------------
# Config (env-var driven -- see module docstring)
# --------------------------------------------------------------------------

def _env(name, default):
    val = os.environ.get(name)
    return default if val is None or val == "" else val


STATS_URL = _env("ICECAST_STATS_URL", "http://localhost:8000/admin/stats")
ADMIN_USER = _env("ICECAST_ADMIN_USER", "source")
ADMIN_PASSWORD = os.environ.get("ICECAST_ADMIN_PASSWORD", "")
EXPORTER_PORT = int(_env("ICECAST_EXPORTER_PORT", "9146"))
EXPORTER_BIND = _env("ICECAST_EXPORTER_BIND", "127.0.0.1")
POLL_INTERVAL_SEC = float(_env("ICECAST_POLL_INTERVAL_SEC", "15"))
HTTP_TIMEOUT_SEC = float(_env("ICECAST_HTTP_TIMEOUT_SEC", "5"))
DEBUG = _env("ICECAST_EXPORTER_DEBUG", "0") not in ("0", "false", "False", "")


def log(msg):
    print(f"[icecast_exporter] {msg}", file=sys.stderr, flush=True)


def dbg(msg):
    if DEBUG:
        log(f"DEBUG {msg}")


# --------------------------------------------------------------------------
# Shared state -- written by the poll thread, read by HTTP handlers
# --------------------------------------------------------------------------

class State:
    def __init__(self):
        self.lock = threading.Lock()
        # Latest rendered metrics text (full exposition payload).
        self.metrics_text = "# icecast_exporter starting up\n"
        # Per-mount running state for rate computation:
        #   mount -> {"bytes": int, "ts": float}
        self.prev = {}


STATE = State()


# --------------------------------------------------------------------------
# Prometheus text rendering helpers
# --------------------------------------------------------------------------

def _esc_label(value):
    """Escape a Prometheus label value."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


class MetricsBuilder:
    """Accumulates Prometheus metric families and renders the payload.

    Emits one HELP/TYPE header per family, then all samples for that family,
    so the output is valid even when families are built incrementally.
    """

    def __init__(self):
        self._families = []        # ordered list of family names
        self._meta = {}            # name -> (type, help)
        self._samples = {}         # name -> list of (labels_dict, value)

    def family(self, name, mtype, help_text):
        if name not in self._meta:
            self._families.append(name)
            self._meta[name] = (mtype, help_text)
            self._samples[name] = []

    def add(self, name, value, labels=None):
        self._samples[name].append((labels or {}, value))

    def render(self):
        lines = []
        for name in self._families:
            mtype, help_text = self._meta[name]
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {mtype}")
            for labels, value in self._samples[name]:
                if labels:
                    label_str = ",".join(
                        f'{k}="{_esc_label(v)}"' for k, v in labels.items()
                    )
                    lines.append(f"{name}{{{label_str}}} {_fmt_value(value)}")
                else:
                    lines.append(f"{name} {_fmt_value(value)}")
        return "\n".join(lines) + "\n"


def _fmt_value(value):
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        # Avoid scientific notation surprises; Prometheus accepts plain floats.
        if value.is_integer():
            return str(int(value))
        return repr(value)
    return str(value)


# --------------------------------------------------------------------------
# Icecast fetch + parse
# --------------------------------------------------------------------------

def fetch_stats():
    """Fetch /admin/stats and return the parsed XML root, or raise."""
    req = urllib.request.Request(STATS_URL)
    if ADMIN_PASSWORD:
        token = base64.b64encode(
            f"{ADMIN_USER}:{ADMIN_PASSWORD}".encode("utf-8")
        ).decode("ascii")
        req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
        raw = resp.read()
    return ET.fromstring(raw)


def _child_int(source, tag, default=0):
    el = source.find(tag)
    if el is None or el.text is None:
        return default
    try:
        return int(el.text.strip())
    except (ValueError, AttributeError):
        return default


def _root_int(root, tag, default=0):
    el = root.find(tag)
    if el is None or el.text is None:
        return default
    try:
        return int(el.text.strip())
    except (ValueError, AttributeError):
        return default


def build_metrics(root, poll_duration, ok):
    """Build the full Prometheus payload from a parsed /admin/stats root."""
    mb = MetricsBuilder()

    # ---- exporter / scrape health ----
    mb.family("icecast_up", "gauge",
              "1 if the last Icecast /admin/stats poll succeeded, else 0.")
    mb.add("icecast_up", 1 if ok else 0)

    mb.family("icecast_scrape_duration_seconds", "gauge",
              "Duration of the last Icecast /admin/stats poll, in seconds.")
    mb.add("icecast_scrape_duration_seconds", round(poll_duration, 6))

    if not ok or root is None:
        return mb.render()

    # ---- server-wide gauges ----
    mb.family("icecast_clients", "gauge",
              "Total client connections currently served by Icecast.")
    mb.add("icecast_clients", _root_int(root, "clients"))

    mb.family("icecast_sources", "gauge",
              "Number of connected source mounts.")
    mb.add("icecast_sources", _root_int(root, "sources"))

    mb.family("icecast_listeners", "gauge",
              "Total listeners across all mounts.")
    mb.add("icecast_listeners", _root_int(root, "listeners"))

    # ---- per-mount families (declared once, before the loop) ----
    mb.family("icecast_source_connected", "gauge",
              "1 if a source is currently connected to this mount.")
    mb.family("icecast_listener_count", "gauge",
              "Current number of listeners on this mount.")
    mb.family("icecast_listener_peak", "gauge",
              "Peak listener count observed on this mount.")
    mb.family("icecast_audio_bitrate_bps", "gauge",
              "Advertised source audio bitrate for this mount, bits/sec.")
    mb.family("icecast_total_bytes_sent_total", "counter",
              "Cumulative bytes sent to listeners on this mount.")
    mb.family("icecast_total_bytes_read_total", "counter",
              "Cumulative bytes read from the source on this mount.")
    mb.family("icecast_listener_byte_rate", "gauge",
              "Bytes/sec sent to listeners on this mount, computed from the "
              "delta of total_bytes_sent over the poll interval. A mount with "
              "listeners but a near-zero byte rate indicates a wedged consumer.")

    now = time.time()
    seen_mounts = set()

    for source in root.findall("source"):
        mount = source.get("mount")
        if not mount:
            continue
        seen_mounts.add(mount)
        labels = {"mount": mount}

        bytes_sent = _child_int(source, "total_bytes_sent")
        bytes_read = _child_int(source, "total_bytes_read")
        listeners = _child_int(source, "listeners")
        peak = _child_int(source, "listener_peak")
        bitrate = _child_int(source, "audio_bitrate",
                             _child_int(source, "bitrate", 0))

        mb.add("icecast_source_connected", 1, labels)
        mb.add("icecast_listener_count", listeners, labels)
        mb.add("icecast_listener_peak", peak, labels)
        mb.add("icecast_audio_bitrate_bps", bitrate, labels)
        mb.add("icecast_total_bytes_sent_total", bytes_sent, labels)
        mb.add("icecast_total_bytes_read_total", bytes_read, labels)

        # Derived byte rate (the VLC-wedge canary).
        with STATE.lock:
            prev = STATE.prev.get(mount)
            STATE.prev[mount] = {"bytes": bytes_sent, "ts": now}
        byte_rate = 0.0
        if prev is not None:
            dt = now - prev["ts"]
            dbytes = bytes_sent - prev["bytes"]
            if dt > 0 and dbytes >= 0:
                byte_rate = dbytes / dt
            # dbytes < 0 means the source restarted (counter reset) -> 0 this tick.
        mb.add("icecast_listener_byte_rate", round(byte_rate, 3), labels)
        dbg(f"mount={mount} listeners={listeners} "
            f"bytes_sent={bytes_sent} byte_rate={byte_rate:.1f}")

    # Forget rate state for mounts that disappeared (source disconnected), so a
    # later reconnect starts a fresh rate baseline instead of a bogus spike.
    with STATE.lock:
        for mount in list(STATE.prev.keys()):
            if mount not in seen_mounts:
                del STATE.prev[mount]

    return mb.render()


# --------------------------------------------------------------------------
# Poll loop
# --------------------------------------------------------------------------

def poll_loop():
    while True:
        start = time.time()
        ok = True
        root = None
        try:
            root = fetch_stats()
        except (urllib.error.URLError, urllib.error.HTTPError,
                ET.ParseError, OSError) as exc:
            ok = False
            log(f"poll failed: {type(exc).__name__}: {exc}")
        duration = time.time() - start
        try:
            text = build_metrics(root, duration, ok)
        except Exception as exc:  # never let the poll thread die
            ok = False
            log(f"metric build failed: {type(exc).__name__}: {exc}")
            text = (
                "# HELP icecast_up 1 if the last poll succeeded.\n"
                "# TYPE icecast_up gauge\nicecast_up 0\n"
            )
        with STATE.lock:
            STATE.metrics_text = text
        elapsed = time.time() - start
        time.sleep(max(0.5, POLL_INTERVAL_SEC - elapsed))


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, content_type="text/plain; charset=utf-8"):
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path in ("/metrics", "/"):
            with STATE.lock:
                text = STATE.metrics_text
            self._send(200, text,
                       "text/plain; version=0.0.4; charset=utf-8")
        elif self.path == "/healthz":
            self._send(200, "ok\n")
        else:
            self._send(404, "not found\n")

    def log_message(self, fmt, *args):  # quiet by default; honor DEBUG
        if DEBUG:
            log("http " + (fmt % args))


def main():
    if not ADMIN_PASSWORD:
        log("WARNING: ICECAST_ADMIN_PASSWORD is empty. /admin/stats will "
            "likely return 401 and icecast_up will be 0.")
    log(f"polling {STATS_URL} every {POLL_INTERVAL_SEC}s as user "
        f"'{ADMIN_USER}'; serving /metrics on "
        f"{EXPORTER_BIND}:{EXPORTER_PORT}")

    t = threading.Thread(target=poll_loop, name="poll", daemon=True)
    t.start()

    server = ThreadingHTTPServer((EXPORTER_BIND, EXPORTER_PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
