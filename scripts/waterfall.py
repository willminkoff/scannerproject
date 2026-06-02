"""Live 2-dongle stitched RTL-SDR spectrum backend (Phase 6a).

Owns 2 RTL-SDR devices via SoapySDR (opened by serial), parks them at
adjacent center frequencies, FFTs each every ~100 ms, and stitches the
two outputs into a continuous ~5 MHz spectrum view.

Per-dongle health watchdog: after 3 consecutive bad/short reads a dongle
is marked "down", we keep serving from the surviving dongle, and try to
reconnect with exponential backoff (5s -> 60s).  The watchdog/force-exit
shutdown pattern mirrors disco/src/sweep.py so a wedged SoapySDR
closeStream() can't hang systemd at unit-stop time.

State is written atomically (write tmp, fsync, rename) to
/run/scannerproject/waterfall/state.json.  Retune commands are read by
mtime-polling /run/scannerproject/waterfall/config.json — a write of
{"center_mhz": 462.0, "bw_mhz": 3.0} retunes BOTH dongles so the
stitched window centers on that frequency at that width (A goes to
center - half_spacing, B to center + half_spacing).  bw_mhz is optional
(a missing key preserves the current width); the requested width is
planned into a valid per-dongle sample rate + half-spacing by
_plan_band(), capped at the gap-free 4.8 MHz the two dongles can cover
at 2.4 MS/s.  The active band is persisted to data/waterfall_band.json
so it survives a reboot (/run is tmpfs).

Phase R2: SOAPY_SDR_OVERFLOW (-4) on readStream is treated as benign —
we read far slower than the 2.4 MS/s stream fills, so the ring overflows
routinely; we just re-read the fresh post-overflow samples and surface a
cumulative overflow count in state.json rather than dropping the frame.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import struct
import sys
import threading
import time
from typing import Optional

import numpy as np
import SoapySDR
from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32, SOAPY_SDR_OVERFLOW

# ---------------------------------------------------------------------
# Constants & defaults
# ---------------------------------------------------------------------
STATE_DIR = os.environ.get(
    "WATERFALL_STATE_DIR", "/run/scannerproject/waterfall"
)
STATE_PATH = os.path.join(STATE_DIR, "state.json")
CONFIG_PATH = os.path.join(STATE_DIR, "config.json")

# Persistent last-known band (survives reboot — /run is tmpfs).  Seeded at
# startup and rewritten whenever a retune is applied.  Defaults to
# <repo>/data/waterfall_band.json (repo root = parent of this scripts dir).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAND_PERSIST_PATH = os.environ.get(
    "WATERFALL_BAND_PATH", os.path.join(_REPO_ROOT, "data", "waterfall_band.json")
)

# Dongle assignment (Phase 6a):
#   A = Nooelec SMArt v5   (serial 70613472, port 1-4.1.4)
#   B = RTL-SDR Blog V4    (serial 83241970, port 1-4.1.2 — best DR)
SERIAL_A = os.environ.get("WATERFALL_SERIAL_A", "70613472")
SERIAL_B = os.environ.get("WATERFALL_SERIAL_B", "83241970")

# Default center frequencies — stitched window covers roughly
# 121.3 .. 126.1 MHz (most of civilian airband + a margin).
DEFAULT_CENTER_MHZ = 123.7
DEFAULT_BW_MHZ = 4.8     # default stitched window width (2x 2.4 MS/s dongles)

# Tunable bandwidth (Phase R2).  The stitched window width is
#   total_bw = per_dongle_rate + 2 * half_spacing
# so for a requested bw we pick a per-dongle sample rate near bw/2 (with a
# little overlap headroom) and set the half-spacing to fill the rest.  The
# per-dongle rate is clamped to the RTL-SDR's usable high band so we never
# command an unsupported rate; 4.8 MHz (2 x 2.4 MS/s, half-spacing 1.2)
# remains the gap-free maximum and is the unchanged default.
MIN_BW_MHZ = 1.0
MAX_BW_MHZ = 4.8
RTL_RATE_MIN_HZ = 960_000      # stay above the RTL-SDR ~900 kHz low-band edge
RTL_RATE_MAX_HZ = 2_400_000    # 2.4 MS/s — proven-good per-dongle rate

SAMPLE_RATE_HZ = 2_400_000   # default/max per-dongle rate (2.4 MS/s)
FFT_SIZE = 1024
# Phase 6a.1: 100ms per-dongle FFT pacing (10 Hz).  readStream + window
# + FFT(1024) typically runs well under 50ms on the Pi-class host, so
# the loop has ample headroom at 100ms.  If CPU saturates in the field,
# back off to 0.15 / 0.2.  Retunes are still idempotent in _maybe_retune
# (a no-op when wanted == self.center_hz), so faster pacing only causes
# more setFrequency calls when the commanded center actually changes.
FRAME_PERIOD_SEC = 0.033  # Phase 6a.2: 30 Hz (was 0.1 / 10 Hz)
# 1024 bins per dongle stitched edge-to-edge with the 2.4 MHz spacing
# gives ~2048 bins of unique spectrum.  The overlap region is averaged
# (see _stitch_bins) — see comment in _stitch_bins for the rationale.

# Watchdog: 3 consecutive short/failed reads -> mark dongle down.
WATCHDOG_BAD_READS = 3

# Overflow handling (Phase R2).  We consume ~10k samples/s (1024 every
# ~100ms) from a 2.4 MS/s stream, so the driver ring overflows routinely —
# SOAPY_SDR_OVERFLOW (-4).  That is BENIGN for a snapshot FFT: the
# post-overflow samples are contiguous and fresh, so we just re-read.  We
# cap re-reads per frame so a genuinely stuck stream still falls through to
# the watchdog, and surface a cumulative count in state.json for health.
MAX_OVERFLOW_RETRIES_PER_FRAME = 16
RECONNECT_BACKOFF_INIT_S = 5.0
RECONNECT_BACKOFF_MAX_S = 60.0
RECONNECT_BACKOFF_GROWTH = 1.6

# Bounded graceful shutdown — mirrors disco/src/sweep.py.  Healthy
# closes complete in <1s; this only fires if SoapySDR's closeStream()
# is actually wedged.
FORCE_EXIT_TIMEOUT_SEC = 5.0

LOG = logging.getLogger("scanner.waterfall")
_STOP = threading.Event()
_FORCE_EXIT_ARMED = False

# ---------------------------------------------------------------------
# Direct IPC: SOCK_DGRAM publish channel to ws_spectrum (and any other
# in-process consumer that wants live frames without mtime-polling
# state.json).  state.json keeps being written — it is still the
# /api/waterfall payload and the heartbeat freshness probe — so this
# socket is an *additive* low-latency path, not a replacement.
#
# Wire layout per datagram (matches ws_spectrum's WS binary frame):
#   offset  size  field
#   ------  ----  --------------------------------
#   0       1     u8   pane_id (0 = waterfall)
#   1       4     f32  center_mhz   (little-endian)
#   5       4     f32  span_mhz     (little-endian)
#   9       2     u16  n_bins       (little-endian)
#   11      4*N   f32[] bins (dBFS, little-endian)
#
# 2048 bins -> 8203 bytes; well under the kernel's default AF_UNIX
# datagram cap, so SOCK_DGRAM (one frame == one message) is the right
# choice here.
SPEC_SOCKET_PATH = os.path.join(STATE_DIR, "spec.sock")
SPEC_PANE_WATERFALL = 0
# Subscribers must heartbeat at least once inside this window or they
# get pruned (so we don't pile sendto() retries onto a dead socket).
SUBSCRIBER_IDLE_TIMEOUT_S = 5.0
# Single-byte register/heartbeat magic. Any well-formed packet from a
# bound subscriber renews liveness, but the magic lets us drop random
# UDP-style noise without trying to interpret it.
SUBSCRIBER_HELLO_BYTES = b"SUB\x01"
# Subscriber control frames are tiny by design (4 bytes today). 64
# is the defence-in-depth cap.
PUBLISHER_RECV_BUFSIZE = 64


def _setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("WATERFALL_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _arm_force_exit_watchdog(timeout_sec: float) -> None:
    global _FORCE_EXIT_ARMED
    if _FORCE_EXIT_ARMED:
        return
    _FORCE_EXIT_ARMED = True

    def _killer():
        time.sleep(timeout_sec)
        LOG.warning(
            "graceful shutdown exceeded %.1fs (likely SoapySDR hang); "
            "force-exiting",
            timeout_sec,
        )
        os._exit(0)

    threading.Thread(
        target=_killer, daemon=True, name="waterfall-force-exit"
    ).start()


def _handle_stop(signum, frame):
    LOG.info("stopping on signal %s", signum)
    _STOP.set()
    _arm_force_exit_watchdog(FORCE_EXIT_TIMEOUT_SEC)


# ---------------------------------------------------------------------
# Atomic writer
# ---------------------------------------------------------------------
def _write_state_atomic(state: dict, path: str) -> None:
    """Write JSON via tmp + fsync + rename so readers never see torn writes."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


# ---------------------------------------------------------------------
# Direct-IPC spectrum publisher
# ---------------------------------------------------------------------
class SpectrumPublisher:
    """SOCK_DGRAM fan-out to ws_spectrum (and any other live consumer).

    Subscribers register by sending ``SUBSCRIBER_HELLO_BYTES`` from a
    bound AF_UNIX address; we recvfrom() to harvest the address and
    keep them in ``self.subscribers`` with a monotonic last-seen.
    Anyone who goes quiet for ``SUBSCRIBER_IDLE_TIMEOUT_S`` is dropped.

    The bind is best-effort: if it fails (e.g. /run not writable yet,
    or a stale socket file can't be unlinked) we log and continue
    without publishing.  state.json keeps working, so the UI degrades
    cleanly to its existing mtime-poll path.
    """

    def __init__(self, path: str):
        self.path = path
        self.sock: Optional[socket.socket] = None
        self.subscribers: dict = {}  # peer_addr -> last_seen monotonic
        self._open()

    def _open(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
            s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            s.setblocking(False)
            s.bind(self.path)
            os.chmod(self.path, 0o666)
            self.sock = s
            LOG.info("spectrum publisher bound to %s", self.path)
        except OSError as e:
            LOG.warning("spectrum publisher bind failed: %s", e)
            self.sock = None

    def poll(self) -> None:
        """Drain pending hellos/heartbeats and prune idle subscribers."""
        if self.sock is None:
            return
        while True:
            try:
                data, peer = self.sock.recvfrom(PUBLISHER_RECV_BUFSIZE)
            except BlockingIOError:
                break
            except OSError:
                break
            if not peer or not data.startswith(SUBSCRIBER_HELLO_BYTES[:3]):
                continue
            self.subscribers[peer] = time.monotonic()
        now = time.monotonic()
        stale = [p for p, ts in self.subscribers.items()
                 if (now - ts) > SUBSCRIBER_IDLE_TIMEOUT_S]
        for p in stale:
            self.subscribers.pop(p, None)

    def publish(self, frame: bytes) -> None:
        if self.sock is None or not self.subscribers:
            return
        for peer in list(self.subscribers):
            try:
                self.sock.sendto(frame, peer)
            except OSError:
                # Peer's bound path vanished (their process died). Drop
                # it now so we don't churn sendto() failures every frame
                # until the idle prune catches up.
                self.subscribers.pop(peer, None)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        try:
            os.unlink(self.path)
        except OSError:
            pass


def _build_spectrum_datagram(pane_id: int, center_mhz: float, span_mhz: float,
                             bins: np.ndarray) -> bytes:
    """Pack one FFT frame into the wire format ws_spectrum expects."""
    arr = np.ascontiguousarray(bins, dtype=np.float32)
    n = int(arr.size)
    header = struct.pack("<BffH", pane_id & 0xFF,
                         float(center_mhz), float(span_mhz), n)
    return header + arr.tobytes()


# ---------------------------------------------------------------------
# Band planning + persistence (Phase R2)
# ---------------------------------------------------------------------
def _plan_band(bw_mhz: float) -> tuple[float, float, float]:
    """Plan the 2-dongle stitch for a requested total bandwidth.

    Returns (clamped_bw_mhz, per_dongle_rate_hz, half_spacing_hz) such that
    ``per_dongle_rate + 2*half_spacing == clamped_bw`` (gap-free).  The
    per-dongle rate is chosen near bw/2 with ~5% overlap headroom and
    clamped to the RTL-SDR's usable high band, so narrower bandwidths are a
    genuine zoom (lower rate, tighter spacing) and the 4.8 MHz default maps
    back to the original 2.4 MS/s / 1.2 MHz spacing exactly.
    """
    bw = min(MAX_BW_MHZ, max(MIN_BW_MHZ, float(bw_mhz)))
    rate_hz = bw / 2.0 * 1.05 * 1e6
    rate_hz = min(RTL_RATE_MAX_HZ, max(RTL_RATE_MIN_HZ, rate_hz))
    half_spacing_hz = max(0.0, (bw * 1e6 - rate_hz) / 2.0)
    return bw, rate_hz, half_spacing_hz


def _seam_offset_hz(rate_hz: float) -> float:
    """Hz to pull BOTH dongle centers down so the stitch seam misses the tune freq.

    The two dongles meet at the midpoint of their centers.  With symmetric
    placement (A = center - half_spacing, B = center + half_spacing) that
    midpoint IS the user's tune freq — so the one frequency the user cares
    about lands squarely on the stitch seam (and on A's upper / B's lower band
    edge), exactly where signal gets mangled.

    We instead pull BOTH centers down by rate/4.  That moves the seam to
    (center - rate/4) and parks the tune freq dead-center in dongle B's lower
    clean half-band: rate/4 above the seam and rate/4 below B's DC spike (the
    two artifacts that bracket that sub-band), i.e. the maximum possible
    clearance from both.  We pull *down* (not up) on purpose so the tune freq
    lands on dongle B — the RTL-SDR Blog V4, our best-dynamic-range radio.

    The resulting A/B placement is asymmetric ON PURPOSE.  Do not "tidy" it
    back to symmetric center +/- half_spacing or the seam returns to the tune
    freq and this whole fix is undone.
    """
    return rate_hz / 4.0


def _load_persisted_band() -> tuple[float, float]:
    """Read the last-applied band, or fall back to the defaults."""
    try:
        with open(BAND_PERSIST_PATH) as f:
            data = json.load(f)
        center = float(data["center_mhz"])
        bw = float(data["bw_mhz"])
        if 24.0 <= center <= 1700.0:
            LOG.info(
                "restored persisted band: center=%.3f MHz bw=%.3f MHz",
                center, bw,
            )
            return center, bw
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        LOG.info("no usable persisted band (%s); using defaults", e)
    return DEFAULT_CENTER_MHZ, DEFAULT_BW_MHZ


def _persist_band(center_mhz: float, bw_mhz: float) -> None:
    """Best-effort atomic write of the active band for restart persistence."""
    try:
        os.makedirs(os.path.dirname(BAND_PERSIST_PATH), exist_ok=True)
        _write_state_atomic(
            {"center_mhz": round(center_mhz, 4), "bw_mhz": round(bw_mhz, 4)},
            BAND_PERSIST_PATH,
        )
    except OSError as e:
        LOG.warning("band persist failed: %s", e)


# ---------------------------------------------------------------------
# Config mtime polling — used to detect /sb5 retune commands.
# ---------------------------------------------------------------------
class ConfigPoller:
    """Polls config.json mtime; returns the parsed dict on change, or None.

    Tracks ts of the last successfully loaded config so the state JSON can
    surface `config_age_sec` (how stale the active retune command is).
    """

    def __init__(self, path: str):
        self.path = path
        self._last_mtime: float = 0.0
        self._last_loaded_ts: float = 0.0

    def prime(self) -> None:
        """Adopt the current config.json mtime without applying it.

        Called at startup so a stale retune command left in /run (which
        survives a service restart) is NOT replayed over the persisted
        band — the persisted band is authoritative on startup, and
        config.json only signals genuinely new commands thereafter.
        """
        try:
            self._last_mtime = os.stat(self.path).st_mtime
        except OSError:
            self._last_mtime = 0.0

    def poll(self) -> Optional[dict]:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return None
        except OSError as e:
            LOG.warning("config stat failed: %s", e)
            return None
        if st.st_mtime <= self._last_mtime:
            return None
        self._last_mtime = st.st_mtime
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            LOG.warning("config read failed: %s", e)
            return None
        if not isinstance(data, dict):
            LOG.warning("config not a dict: %r", type(data).__name__)
            return None
        self._last_loaded_ts = time.time()
        return data

    def age_sec(self) -> float:
        if self._last_loaded_ts <= 0.0:
            return 0.0
        return max(0.0, time.time() - self._last_loaded_ts)


# ---------------------------------------------------------------------
# Per-dongle worker — owns one SoapySDR device, FFTs each frame, posts
# the latest spectrum to a shared slot read by the stitch loop.
# ---------------------------------------------------------------------
class DongleWorker:
    """Owns one RTL-SDR.  Public surface:

      .serial            — RTL-SDR serial (string)
      .label             — "A" / "B" for log correlation
      .center_hz         — current center freq (commanded)
      .request_center_hz — sets the next center freq (thread-safe)
      .latest_frame()    — returns (mag_db, ts, frame_age_ms) or None
      .state             — "ok" | "down"
      .last_bus_path     — best-effort USB bus/port path for dmesg correlation
      .start() / .stop()
    """

    def __init__(
        self,
        label: str,
        serial: str,
        initial_center_hz: float,
        initial_rate_hz: float = SAMPLE_RATE_HZ,
    ):
        self.label = label
        self.serial = serial
        self.center_hz = float(initial_center_hz)
        self._next_center_hz = float(initial_center_hz)
        self.sample_rate_hz = float(initial_rate_hz)
        self._next_rate_hz = float(initial_rate_hz)
        self._lock = threading.Lock()
        self._latest: Optional[tuple] = None
        self._thread: Optional[threading.Thread] = None
        self.state = "down"   # flips to "ok" once first frame lands
        self.last_bus_path = ""
        self._sdr = None
        self._stream = None
        self._bad_reads = 0
        self.overflow_count = 0   # cumulative SOAPY_SDR_OVERFLOW (Phase R2)

    # ---- public ---------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name=f"wf-dongle-{self.label}", daemon=True
        )
        self._thread.start()

    def request_center_hz(self, hz: float) -> None:
        with self._lock:
            self._next_center_hz = float(hz)

    def request_band(self, center_hz: float, rate_hz: float) -> None:
        """Thread-safe: command a new center AND per-dongle sample rate."""
        with self._lock:
            self._next_center_hz = float(center_hz)
            self._next_rate_hz = float(rate_hz)

    def latest_frame(self):
        with self._lock:
            if self._latest is None:
                return None
            mag, ts = self._latest
            age_ms = int(max(0.0, (time.time() - ts) * 1000.0))
            return mag, ts, age_ms

    # ---- internal -------------------------------------------------------
    def _resolve_bus_path(self) -> str:
        """Best-effort: find the USB bus/port path for this serial.

        We walk /sys/bus/usb/devices and match on the 'serial' attribute.
        Failure is non-fatal — we just log a blank bus path.
        """
        try:
            base = "/sys/bus/usb/devices"
            for entry in os.listdir(base):
                serial_file = os.path.join(base, entry, "serial")
                try:
                    with open(serial_file) as f:
                        s = f.read().strip()
                except OSError:
                    continue
                if s == self.serial:
                    return entry
        except OSError:
            pass
        return ""

    def _open(self) -> bool:
        """Open & configure the device.  Returns True on success."""
        try:
            args = {"driver": "rtlsdr", "serial": self.serial}
            self._sdr = SoapySDR.Device(args)
            with self._lock:
                center = self._next_center_hz
                rate = self._next_rate_hz
                self.center_hz = center
                self.sample_rate_hz = rate
            self._sdr.setSampleRate(SOAPY_SDR_RX, 0, rate)
            try:
                self._sdr.setBandwidth(SOAPY_SDR_RX, 0, rate * 0.8)
            except Exception:
                pass
            # Fixed gain — auto-gain on RTL-SDR is hit-or-miss.  44 dB sits
            # high in the R820T2's range (~0-49.6 dB) so weak bursts punch
            # clearly above the noise floor in the waterfall; the adaptive
            # color-stretch in the UI rides the floor from here.  We measured
            # the curve live against the strong 160-161 MHz emitter: noise
            # floor tracks gain ~linearly up to 44 (≈-59 dBFS @36 -> ≈-52 @44)
            # then PLATEAUS — 48 dB added no floor/peak improvement (within
            # frame noise) while sitting in the front-end's compression region
            # with that strong in-band signal present.  44 is the knee: maximum
            # useful sensitivity, no upside past it, lower overload risk.
            try:
                self._sdr.setGainMode(SOAPY_SDR_RX, 0, False)
                self._sdr.setGain(SOAPY_SDR_RX, 0, 44.0)
            except Exception:
                pass
            self._sdr.setFrequency(SOAPY_SDR_RX, 0, center)
            self._stream = self._sdr.setupStream(
                SOAPY_SDR_RX, SOAPY_SDR_CF32, [0]
            )
            self._sdr.activateStream(self._stream)
            self.last_bus_path = self._resolve_bus_path()
            LOG.info(
                "[%s] opened serial=%s bus=%s center=%.3f MHz rate=%.3f MS/s",
                self.label,
                self.serial,
                self.last_bus_path or "?",
                center / 1e6,
                rate / 1e6,
            )
            self._bad_reads = 0
            self.state = "ok"
            return True
        except Exception as e:
            LOG.warning(
                "[%s] open failed serial=%s bus=%s err=%s",
                self.label, self.serial, self.last_bus_path or "?", e,
            )
            self._safe_close()
            self.state = "down"
            return False

    def _safe_close(self) -> None:
        try:
            if self._stream is not None and self._sdr is not None:
                try:
                    self._sdr.deactivateStream(self._stream)
                except Exception as e:
                    LOG.warning(
                        "[%s] deactivateStream err serial=%s: %s",
                        self.label, self.serial, e,
                    )
                try:
                    self._sdr.closeStream(self._stream)
                except Exception as e:
                    LOG.warning(
                        "[%s] closeStream err serial=%s: %s",
                        self.label, self.serial, e,
                    )
        finally:
            self._stream = None
            self._sdr = None
            LOG.info(
                "[%s] closed serial=%s bus=%s",
                self.label, self.serial, self.last_bus_path or "?",
            )

    def _maybe_retune(self) -> None:
        with self._lock:
            wanted = self._next_center_hz
            wanted_rate = self._next_rate_hz
        rate_changed = abs(wanted_rate - self.sample_rate_hz) > 1.0
        if wanted == self.center_hz and not rate_changed:
            return
        try:
            if rate_changed:
                # RTL-SDR sample-rate changes are safest with the stream
                # quiesced — bracket the change in deactivate/reactivate so
                # the async reader restarts cleanly at the new rate.
                self._sdr.deactivateStream(self._stream)
                self._sdr.setSampleRate(SOAPY_SDR_RX, 0, wanted_rate)
                try:
                    self._sdr.setBandwidth(SOAPY_SDR_RX, 0, wanted_rate * 0.8)
                except Exception:
                    pass
                self._sdr.setFrequency(SOAPY_SDR_RX, 0, wanted)
                self._sdr.activateStream(self._stream)
                LOG.info(
                    "[%s] reband serial=%s center %.3f->%.3f MHz "
                    "rate %.3f->%.3f MS/s",
                    self.label, self.serial,
                    self.center_hz / 1e6, wanted / 1e6,
                    self.sample_rate_hz / 1e6, wanted_rate / 1e6,
                )
                self.sample_rate_hz = wanted_rate
                self.center_hz = wanted
            else:
                self._sdr.setFrequency(SOAPY_SDR_RX, 0, wanted)
                LOG.info(
                    "[%s] retuned serial=%s %.3f -> %.3f MHz",
                    self.label, self.serial,
                    self.center_hz / 1e6, wanted / 1e6,
                )
                self.center_hz = wanted
        except Exception as e:
            LOG.warning(
                "[%s] retune failed serial=%s -> %.3f MHz: %s",
                self.label, self.serial, wanted / 1e6, e,
            )
            # A failed reband can leave the stream deactivated — force a
            # reopen so the watchdog path reconfigures cleanly.
            self._safe_close()
            self.state = "down"

    def _read_frame(self) -> Optional[np.ndarray]:
        """Read FFT_SIZE samples; return the complex64 buffer or None."""
        buf = np.zeros(FFT_SIZE, dtype=np.complex64)
        pos = 0
        t0 = time.time()
        overflows = 0
        # SoapyRTL returns small chunks; loop until we've got a full frame
        # or the read goes sideways.
        while pos < FFT_SIZE:
            if _STOP.is_set():
                return None
            chunk = min(8192, FFT_SIZE - pos)
            try:
                sr = self._sdr.readStream(
                    self._stream, [buf[pos:pos + chunk]], chunk,
                    timeoutUs=2_000_000,
                )
            except Exception as e:
                LOG.warning(
                    "[%s] readStream raised serial=%s bus=%s: %s",
                    self.label, self.serial,
                    self.last_bus_path or "?", e,
                )
                return None
            if sr.ret == SOAPY_SDR_OVERFLOW:
                # Benign for a snapshot FFT — the driver ring overflowed
                # because we read far slower than the stream fills.  The
                # post-overflow samples are contiguous and fresh; just
                # re-read.  Bounded so a truly stuck stream still bails.
                self.overflow_count += 1
                overflows += 1
                if overflows > MAX_OVERFLOW_RETRIES_PER_FRAME:
                    LOG.warning(
                        "[%s] overflow flood (%d in one frame) serial=%s; "
                        "bailing to watchdog",
                        self.label, overflows, self.serial,
                    )
                    return None
                if time.time() - t0 > 1.0:
                    return None
                continue
            if sr.ret < 0:
                LOG.warning(
                    "[%s] readStream err serial=%s ret=%s flags=%s",
                    self.label, self.serial, sr.ret, sr.flags,
                )
                return None
            pos += sr.ret
            if time.time() - t0 > 1.0:
                # Hard time bound — RTL-SDRs sometimes return 0-byte chunks
                # forever after a USB hiccup.  Bail and let the watchdog
                # decide whether to reopen.
                LOG.warning(
                    "[%s] read time-bounded at pos=%d/%d serial=%s",
                    self.label, pos, FFT_SIZE, self.serial,
                )
                return None
        return buf

    def _compute_spectrum_db(self, iq: np.ndarray) -> np.ndarray:
        window = np.hanning(FFT_SIZE).astype(np.float32)
        blk = iq * window
        spec = np.fft.fftshift(np.fft.fft(blk))
        mag2 = spec.real * spec.real + spec.imag * spec.imag
        # 10*log10(power) normalized by FFT size -> dBFS-ish
        mag_db = 10.0 * np.log10(mag2 + 1e-30) - 20.0 * np.log10(FFT_SIZE)
        return mag_db.astype(np.float32)

    def _run(self) -> None:
        backoff = RECONNECT_BACKOFF_INIT_S
        while not _STOP.is_set():
            if self._sdr is None:
                if not self._open():
                    LOG.warning(
                        "[%s] reconnect sleep %.1fs serial=%s",
                        self.label, backoff, self.serial,
                    )
                    if _STOP.wait(backoff):
                        break
                    backoff = min(
                        backoff * RECONNECT_BACKOFF_GROWTH,
                        RECONNECT_BACKOFF_MAX_S,
                    )
                    continue
                backoff = RECONNECT_BACKOFF_INIT_S

            self._maybe_retune()

            t_frame_start = time.time()
            iq = self._read_frame()
            if iq is None:
                self._bad_reads += 1
                if self._bad_reads >= WATCHDOG_BAD_READS:
                    LOG.warning(
                        "[%s] watchdog tripped (%d bad reads) serial=%s bus=%s; "
                        "closing for reopen",
                        self.label, self._bad_reads,
                        self.serial, self.last_bus_path or "?",
                    )
                    self._safe_close()
                    self.state = "down"
                # tiny sleep to avoid tight loop on transient errors
                if _STOP.wait(0.1):
                    break
                continue

            self._bad_reads = 0
            mag_db = self._compute_spectrum_db(iq)
            with self._lock:
                self._latest = (mag_db, time.time())
            self.state = "ok"

            # Honor FRAME_PERIOD_SEC pacing.  readStream typically takes
            # well under 50ms so we sleep for the remainder.
            elapsed = time.time() - t_frame_start
            sleep_for = FRAME_PERIOD_SEC - elapsed
            if sleep_for > 0:
                if _STOP.wait(sleep_for):
                    break

        # Graceful shutdown — bounded by _arm_force_exit_watchdog if
        # closeStream hangs.
        self._safe_close()
        LOG.info("[%s] worker exiting serial=%s", self.label, self.serial)

    def stop_and_join(self, timeout: float = 3.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)


# ---------------------------------------------------------------------
# Stitch — two 1024-bin per-dongle FFTs -> single 2048-bin spectrum.
# ---------------------------------------------------------------------
def _stitch_bins(
    mag_a, center_a_hz: float, rate_a_hz: float,
    mag_b, center_b_hz: float, rate_b_hz: float,
):
    """Stitch two FFT outputs into a continuous bin array.

    Returns (bins, freq_min_hz, freq_max_hz).

    Strategy: each FFT covers [center - sr/2, center + sr/2] at its own
    per-dongle sample rate (they share a rate in steady state but may
    differ momentarily mid-reband).  We map both onto a shared frequency
    grid at the finer of the two bin sizes.  Where the two windows overlap
    we average the dBFS values — averaging is the simplest defensible
    reduction when both dongles are healthy; if either is contributing
    garbage the higher noise floor will pull the average up which is fine
    for a visual waterfall (it just makes the overlap region a touch
    noisier; the surrounding non-overlapping bins are untouched and look
    clean).
    """
    n = FFT_SIZE
    # Use the finer (smaller) per-bin Hz so neither dongle is undersampled
    # on the shared grid.
    bin_hz = min(rate_a_hz, rate_b_hz) / n
    low_a = center_a_hz - rate_a_hz / 2.0
    high_a = center_a_hz + rate_a_hz / 2.0
    low_b = center_b_hz - rate_b_hz / 2.0
    high_b = center_b_hz + rate_b_hz / 2.0
    f_min = min(low_a, low_b)
    f_max = max(high_a, high_b)
    total_bins = int(round((f_max - f_min) / bin_hz))
    # safety clamp — guarantees ~2048 for nominal A/B 2.4 MHz spacing
    if total_bins <= 0:
        total_bins = n * 2
    out = np.full(total_bins, np.nan, dtype=np.float32)
    counts = np.zeros(total_bins, dtype=np.int8)

    def _splat(mag, low_hz: float) -> None:
        start_bin = int(round((low_hz - f_min) / bin_hz))
        end_bin = start_bin + n
        if start_bin < 0 or end_bin > total_bins:
            # clamp — shouldn't happen with the construction above
            start_bin = max(0, start_bin)
            end_bin = min(total_bins, end_bin)
            mag = mag[: end_bin - start_bin]
        # add into out (treat NaN as 0 for the running sum)
        slot = out[start_bin:end_bin]
        slot_counts = counts[start_bin:end_bin]
        empty = np.isnan(slot)
        slot[empty] = mag[empty]
        # for non-empty bins, this is the overlap region -> average
        nonempty = ~empty
        if np.any(nonempty):
            slot[nonempty] = (slot[nonempty] * slot_counts[nonempty] +
                              mag[nonempty]) / (slot_counts[nonempty] + 1)
        out[start_bin:end_bin] = slot
        counts[start_bin:end_bin] = slot_counts + 1

    if mag_a is not None:
        _splat(mag_a, low_a)
    if mag_b is not None:
        _splat(mag_b, low_b)

    # Any bin still NaN = no dongle covered it.  Mark with a very low
    # value so the UI shows a gap (rather than 0 dBFS which would look
    # like a giant peak).
    out[np.isnan(out)] = -120.0
    return out, f_min, f_max


# ---------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------
def main() -> int:
    _setup_logging()
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    os.makedirs(STATE_DIR, exist_ok=True)

    # Seed the active band from the persisted last-known band (Phase R2),
    # falling back to the airband defaults on first run / unreadable file.
    current_center_mhz, current_bw_mhz = _load_persisted_band()
    current_bw_mhz, rate_hz, half_spacing_hz = _plan_band(current_bw_mhz)
    current_center_hz = current_center_mhz * 1e6
    seam_offset_hz = _seam_offset_hz(rate_hz)

    a = DongleWorker(
        "A", SERIAL_A,
        current_center_hz - half_spacing_hz - seam_offset_hz, rate_hz,
    )
    b = DongleWorker(
        "B", SERIAL_B,
        current_center_hz + half_spacing_hz - seam_offset_hz, rate_hz,
    )
    a.start()
    b.start()

    config_poller = ConfigPoller(CONFIG_PATH)
    # Persisted band is authoritative on startup — adopt (don't replay) any
    # stale config.json left in /run from a prior session, and materialize
    # the persist file so the current band survives a reboot too.
    config_poller.prime()
    _persist_band(current_center_mhz, current_bw_mhz)

    publisher = SpectrumPublisher(SPEC_SOCKET_PATH)

    LOG.info(
        "waterfall up: A=%s @ %.3f MHz, B=%s @ %.3f MHz; center=%.3f MHz "
        "bw=%.3f MHz (rate=%.3f MS/s, half-spacing=%.3f MHz, "
        "seam-offset=%.3f MHz -> seam @ %.3f MHz)",
        SERIAL_A, (current_center_hz - half_spacing_hz - seam_offset_hz) / 1e6,
        SERIAL_B, (current_center_hz + half_spacing_hz - seam_offset_hz) / 1e6,
        current_center_mhz, current_bw_mhz,
        rate_hz / 1e6, half_spacing_hz / 1e6,
        seam_offset_hz / 1e6, (current_center_hz - seam_offset_hz) / 1e6,
    )

    last_state_write = 0.0
    STATE_WRITE_PERIOD = 0.033  # Phase 6a.2: 30 Hz cap (was 0.1 / 10 Hz)

    while not _STOP.is_set():
        # Retune?  Accepts {center_mhz} and/or {bw_mhz}; missing keys keep
        # the current value so a center-only POST preserves bandwidth.
        cfg = config_poller.poll()
        if cfg is not None:
            try:
                new_center_mhz = float(cfg.get("center_mhz", current_center_mhz))
                new_bw_mhz = float(cfg.get("bw_mhz", current_bw_mhz))
                # Sanity-clamp center to the RTL-SDR range (24 MHz - 1.7 GHz).
                if 24.0 <= new_center_mhz <= 1700.0:
                    current_center_mhz = new_center_mhz
                    current_center_hz = new_center_mhz * 1e6
                    current_bw_mhz, rate_hz, half_spacing_hz = _plan_band(
                        new_bw_mhz
                    )
                    seam_offset_hz = _seam_offset_hz(rate_hz)
                    a.request_band(
                        current_center_hz - half_spacing_hz - seam_offset_hz,
                        rate_hz,
                    )
                    b.request_band(
                        current_center_hz + half_spacing_hz - seam_offset_hz,
                        rate_hz,
                    )
                    _persist_band(current_center_mhz, current_bw_mhz)
                    LOG.info(
                        "config retune: center=%.3f MHz bw=%.3f MHz "
                        "(A=%.3f, B=%.3f, rate=%.3f MS/s, seam @ %.3f)",
                        new_center_mhz, current_bw_mhz,
                        (current_center_hz - half_spacing_hz - seam_offset_hz)
                        / 1e6,
                        (current_center_hz + half_spacing_hz - seam_offset_hz)
                        / 1e6,
                        rate_hz / 1e6,
                        (current_center_hz - seam_offset_hz) / 1e6,
                    )
                else:
                    LOG.warning(
                        "config retune ignored: center_mhz=%.3f out of range",
                        new_center_mhz,
                    )
            except (TypeError, ValueError) as e:
                LOG.warning("config retune ignored: %s", e)

        # Pull latest frames from both dongles.
        frame_a = a.latest_frame()
        frame_b = b.latest_frame()

        mag_a = frame_a[0] if frame_a is not None else None
        mag_b = frame_b[0] if frame_b is not None else None
        age_a_ms = frame_a[2] if frame_a is not None else None
        age_b_ms = frame_b[2] if frame_b is not None else None

        bins, f_min_hz, f_max_hz = _stitch_bins(
            mag_a, a.center_hz, a.sample_rate_hz,
            mag_b, b.center_hz, b.sample_rate_hz,
        )

        a_ok = (a.state == "ok") and (age_a_ms is not None) and (age_a_ms < 5000)
        b_ok = (b.state == "ok") and (age_b_ms is not None) and (age_b_ms < 5000)
        if a_ok and b_ok:
            top_state = "ok"
        elif a_ok or b_ok:
            top_state = "degraded"
        else:
            top_state = "down"

        # center_mhz is the user's COMMANDED tune freq, not the geometric
        # midpoint of the stitched window.  The seam offset deliberately
        # parks the geometric midpoint on the (signal-free) seam, so the
        # midpoint is the wrong thing to advertise — the UI uses center_mhz
        # for the tune marker / drag baseline and freq_{min,max}_mhz for the
        # actual displayed span.
        bw_mhz = (f_max_hz - f_min_hz) / 1e6

        ages_present = [v for v in (age_a_ms, age_b_ms) if v is not None]
        flat_last_age = min(ages_present) if ages_present else 0

        state = {
            "updated_ts": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ),
            "state": top_state,
            "center_mhz": round(current_center_mhz, 4),
            "bw_mhz": round(bw_mhz, 4),
            "bins": [round(float(v), 1) for v in bins.tolist()],
            "freq_min_mhz": round(f_min_hz / 1e6, 4),
            "freq_max_mhz": round(f_max_hz / 1e6, 4),
            "dongles": [
                {
                    "serial": SERIAL_A,
                    "state": "ok" if a_ok else a.state,
                    "last_frame_age_ms": age_a_ms if age_a_ms is not None else -1,
                    "center_mhz": round(a.center_hz / 1e6, 4),
                    "sample_rate_mhz": round(a.sample_rate_hz / 1e6, 4),
                    "overflow_count": a.overflow_count,
                    "bus": a.last_bus_path or "",
                    "label": "A",
                },
                {
                    "serial": SERIAL_B,
                    "state": "ok" if b_ok else b.state,
                    "last_frame_age_ms": age_b_ms if age_b_ms is not None else -1,
                    "center_mhz": round(b.center_hz / 1e6, 4),
                    "sample_rate_mhz": round(b.sample_rate_hz / 1e6, 4),
                    "overflow_count": b.overflow_count,
                    "bus": b.last_bus_path or "",
                    "label": "B",
                },
            ],
            "config_age_sec": round(config_poller.age_sec(), 1),
            # Phase R2 — tunable band + overflow health.  `overflows_total`
            # is cumulative since process start; the heartbeat rolls the
            # dongle rows up, and a steadily-climbing count vs a flat one
            # distinguishes "recovering fine" from "wedged".
            "overflows_total": a.overflow_count + b.overflow_count,
            "min_bw_mhz": MIN_BW_MHZ,
            "max_bw_mhz": MAX_BW_MHZ,
            # Convenience flat fields for the existing /sb5 renderer that
            # already reads `last_frame_age_ms` and `dongle_serials` from
            # the Phase 5a mock payload.
            "last_frame_age_ms": flat_last_age,
            "dongle_serials": [SERIAL_A, SERIAL_B],
        }

        now = time.time()
        if now - last_state_write >= STATE_WRITE_PERIOD:
            try:
                _write_state_atomic(state, STATE_PATH)
                last_state_write = now
            except Exception as e:
                LOG.warning("state write failed: %s", e)
            # Same cadence on the direct-IPC channel as on disk: 30 Hz
            # cap. poll() drains subscriber hellos/heartbeats and prunes
            # the idle ones; publish() is a no-op when no one is
            # subscribed, so this stays cheap on a headless box.
            publisher.poll()
            if publisher.subscribers:
                try:
                    datagram = _build_spectrum_datagram(
                        SPEC_PANE_WATERFALL,
                        current_center_mhz,
                        bw_mhz,
                        bins,
                    )
                    publisher.publish(datagram)
                except (OSError, ValueError) as e:
                    LOG.warning("spectrum publish failed: %s", e)

        # Pace the outer loop — workers are doing the heavy lifting; the
        # stitch/write loop just needs to be faster than the UI's poll.
        if _STOP.wait(0.02):  # Phase 6a.2: 50 Hz outer loop, lets STATE_WRITE_PERIOD set the cap
            break

    LOG.info("main loop exiting; stopping workers")
    a.stop_and_join(timeout=3.0)
    b.stop_and_join(timeout=3.0)
    publisher.close()
    LOG.info("waterfall shut down cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
