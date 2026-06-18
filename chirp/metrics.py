"""chirp.metrics -- stdlib-only in-process Prometheus exporter.

SB6 Phase 1 (chirp self-instrumentation). Deliberately dependency-free to
match the Phase-0 ``monitoring/icecast_exporter.py`` house style: the chirp
daemon runs in the apt-installed GNU Radio 3.10 Python where
``pip install --break-system-packages`` is fragile, so we do NOT pull in
``prometheus_client``. The metric set is tiny (6 series families) and the
text exposition format is simple enough to render by hand.

WHAT THIS EXPOSES (see docs / SB6 arch plan Sec 9):
  chirp_config_load_status{daemon}        gauge 0/1  -- THE voice-as-noise canary
  chirp_config_path{daemon,path}          gauge 1    -- info-style resolved path
  chirp_flowgraph_alive_seconds_total{daemon}  counter (resets on restart)
  chirp_daemon_restart_total{daemon,reason}    counter (process-local; weak --
                                               the alive-counter reset is the
                                               better restart detector)
  chirp_cmd_bus_request_seconds{command}  histogram  -- dispatch() latency
  chirp_audio_bytes_published_total{mount} counter    -- icecast bytes pushed

PULL vs PUSH:
  - Scalars that reflect live daemon state (alive_seconds, audio bytes) are
    refreshed by *collector callbacks* run at scrape time, so the value is
    always current regardless of scrape cadence.
  - config_* and restart_total are *set once* at startup.
  - cmd_bus_request_seconds is *observed* per dispatch call.

ROLLBACK: the whole exporter is gated by ``CHIRP_METRICS_ENABLED`` (default 1)
in daemon.main(); set it to 0 to run with no /metrics endpoint and zero
behavior change. This module has no import-time side effects.
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, Optional, Sequence, Tuple

log = logging.getLogger("chirp.metrics")

# Default histogram buckets (seconds) for cmd-bus request latency. A dispatch
# call is normally sub-millisecond (state mutation under a lock); reset_radios
# and add_channel storms can run longer, so the tail reaches a few seconds.
DEFAULT_LATENCY_BUCKETS: Tuple[float, ...] = (
    0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0,
)

# A label set is stored as a sorted tuple of (key, value) pairs so the same
# logical series always hashes identically regardless of kwargs order.
LabelSet = Tuple[Tuple[str, str], ...]


def _labelset(labels: Optional[Dict[str, str]]) -> LabelSet:
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _esc(val: str) -> str:
    """Escape a label value per the Prometheus text exposition format."""
    return val.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt_labels(ls: LabelSet, extra: Optional[Tuple[str, str]] = None) -> str:
    pairs = list(ls)
    if extra is not None:
        pairs = pairs + [extra]
    if not pairs:
        return ""
    inner = ",".join(f'{k}="{_esc(v)}"' for k, v in pairs)
    return "{" + inner + "}"


def _fmt_value(v: float) -> str:
    # Render whole numbers without a trailing ".0" to keep counters tidy,
    # but never lose precision on fractional gauges/sums.
    if v == int(v):
        return str(int(v))
    return repr(v)


class MetricsRegistry:
    """Minimal thread-safe metric store with Prometheus text rendering.

    Not a general-purpose client -- it implements exactly the metric types
    chirp Phase 1 needs (gauge, counter, histogram) and nothing else.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # name -> ("gauge"|"counter"|"histogram")
        self._types: Dict[str, str] = {}
        # name -> help text
        self._help: Dict[str, str] = {}
        # (name, labelset) -> float, for gauge + counter
        self._scalars: Dict[Tuple[str, LabelSet], float] = {}
        # name -> bucket bounds (histograms)
        self._hist_buckets: Dict[str, Tuple[float, ...]] = {}
        # (name, labelset) -> {"counts": [per-bucket], "sum": float, "count": int}
        self._hists: Dict[Tuple[str, LabelSet], dict] = {}
        # collector callbacks run at scrape time (pull metrics)
        self._callbacks: list[Callable[["MetricsRegistry"], None]] = []

    # -- declaration ------------------------------------------------------

    def _declare(self, name: str, mtype: str, help_text: str) -> None:
        # First declaration wins for type/help; later set/inc calls reuse it.
        self._types.setdefault(name, mtype)
        if help_text:
            self._help.setdefault(name, help_text)

    # -- gauges / counters ------------------------------------------------

    def set_gauge(self, name: str, value: float,
                  labels: Optional[Dict[str, str]] = None,
                  help: str = "",
                  clear_others: bool = False) -> None:
        """Set a gauge series. ``clear_others`` drops any other label sets for
        this metric first (used for info-style single-series gauges whose label
        value changes, e.g. config_path)."""
        with self._lock:
            self._declare(name, "gauge", help)
            if clear_others:
                for key in [k for k in self._scalars if k[0] == name]:
                    del self._scalars[key]
            self._scalars[(name, _labelset(labels))] = float(value)

    def set_counter(self, name: str, value: float,
                    labels: Optional[Dict[str, str]] = None,
                    help: str = "") -> None:
        """Set a counter to an absolute (caller-monotonic) value. Used by pull
        callbacks reading a monotonic source (uptime, icecast bytes_sent)."""
        with self._lock:
            self._declare(name, "counter", help)
            self._scalars[(name, _labelset(labels))] = float(value)

    def inc_counter(self, name: str, amount: float = 1.0,
                    labels: Optional[Dict[str, str]] = None,
                    help: str = "") -> None:
        with self._lock:
            self._declare(name, "counter", help)
            key = (name, _labelset(labels))
            self._scalars[key] = self._scalars.get(key, 0.0) + float(amount)

    # -- histograms -------------------------------------------------------

    def observe(self, name: str, value: float,
                labels: Optional[Dict[str, str]] = None,
                help: str = "",
                buckets: Sequence[float] = DEFAULT_LATENCY_BUCKETS) -> None:
        with self._lock:
            self._declare(name, "histogram", help)
            self._hist_buckets.setdefault(name, tuple(buckets))
            bounds = self._hist_buckets[name]
            key = (name, _labelset(labels))
            h = self._hists.get(key)
            if h is None:
                h = {"counts": [0] * len(bounds), "sum": 0.0, "count": 0}
                self._hists[key] = h
            for i, b in enumerate(bounds):
                if value <= b:
                    h["counts"][i] += 1
            h["sum"] += float(value)
            h["count"] += 1

    # -- pull callbacks ---------------------------------------------------

    def register_callback(self, fn: Callable[["MetricsRegistry"], None]) -> None:
        """Register a collector run at render() time. Use for live state
        (uptime, byte counters) so the scrape always reads a fresh value."""
        with self._lock:
            self._callbacks.append(fn)

    # -- rendering --------------------------------------------------------

    def render(self) -> str:
        # Run pull callbacks outside the lock (they call set_* which re-locks).
        for fn in list(self._callbacks):
            try:
                fn(self)
            except Exception:  # noqa: BLE001 -- one bad collector must not 500 the scrape
                log.exception("metrics collector callback failed")
        with self._lock:
            lines: list[str] = []
            for name in sorted(self._types):
                mtype = self._types[name]
                help_text = self._help.get(name, name)
                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} {mtype}")
                if mtype in ("gauge", "counter"):
                    rows = sorted(
                        ((ls, v) for (n, ls), v in self._scalars.items() if n == name),
                        key=lambda x: x[0],
                    )
                    for ls, v in rows:
                        lines.append(f"{name}{_fmt_labels(ls)} {_fmt_value(v)}")
                elif mtype == "histogram":
                    bounds = self._hist_buckets.get(name, ())
                    rows = sorted(
                        ((ls, h) for (n, ls), h in self._hists.items() if n == name),
                        key=lambda x: x[0],
                    )
                    for ls, h in rows:
                        cumulative = 0
                        for i, b in enumerate(bounds):
                            cumulative = h["counts"][i]
                            le = "+Inf" if b == float("inf") else _fmt_value(b)
                            lines.append(
                                f"{name}_bucket{_fmt_labels(ls, ('le', le))} "
                                f"{_fmt_value(cumulative)}"
                            )
                        # +Inf bucket == total count
                        lines.append(
                            f"{name}_bucket{_fmt_labels(ls, ('le', '+Inf'))} "
                            f"{_fmt_value(h['count'])}"
                        )
                        lines.append(f"{name}_sum{_fmt_labels(ls)} {_fmt_value(h['sum'])}")
                        lines.append(f"{name}_count{_fmt_labels(ls)} {_fmt_value(h['count'])}")
            return "\n".join(lines) + "\n"


# Module-global registry. The daemon uses this single instance; tests can
# construct their own MetricsRegistry() for isolation.
REGISTRY = MetricsRegistry()


class _Handler(BaseHTTPRequestHandler):
    # Bound at serve() time.
    registry: MetricsRegistry = REGISTRY

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] not in ("/metrics", "/"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            body = self.registry.render().encode("utf-8")
        except Exception:  # noqa: BLE001
            log.exception("metrics render failed")
            self.send_response(500)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # noqa: D401 -- silence default stderr spam
        return


def serve(port: int, bind: str = "127.0.0.1",
          registry: MetricsRegistry = REGISTRY) -> ThreadingHTTPServer:
    """Start the /metrics HTTP server on a daemon thread. Returns the server
    so the caller can shut it down. Loopback bind by default -- Prometheus
    runs on the same Micro."""
    handler = type("_BoundHandler", (_Handler,), {"registry": registry})
    httpd = ThreadingHTTPServer((bind, port), handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, name="chirp-metrics",
                         daemon=True)
    t.start()
    log.info("chirp metrics endpoint on http://%s:%d/metrics", bind, port)
    return httpd
