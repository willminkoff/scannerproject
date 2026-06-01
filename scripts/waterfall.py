"""Live 2-dongle stitched RTL-SDR spectrum backend (Phase 6a).

Owns 2 RTL-SDR devices via SoapySDR (opened by serial), parks them at
adjacent center frequencies, FFTs each every ~500 ms, and stitches the
two outputs into a continuous ~5 MHz spectrum view.

Per-dongle health watchdog: after 3 consecutive bad/short reads a dongle
is marked "down", we keep serving from the surviving dongle, and try to
reconnect with exponential backoff (5s -> 60s).  The watchdog/force-exit
shutdown pattern mirrors disco/src/sweep.py so a wedged SoapySDR
closeStream() can't hang systemd at unit-stop time.

State is written atomically (write tmp, fsync, rename) to
/run/scannerproject/waterfall/state.json.  Retune commands are read by
mtime-polling /run/scannerproject/waterfall/config.json — a write of
{"center_mhz": 462.0} retunes BOTH dongles so the stitched window
centers on that frequency (A goes to center - half_spacing, B to
center + half_spacing, preserving the 2.4 MHz dongle spacing).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from typing import Optional

import numpy as np
import SoapySDR
from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32

# ---------------------------------------------------------------------
# Constants & defaults
# ---------------------------------------------------------------------
STATE_DIR = os.environ.get(
    "WATERFALL_STATE_DIR", "/run/scannerproject/waterfall"
)
STATE_PATH = os.path.join(STATE_DIR, "state.json")
CONFIG_PATH = os.path.join(STATE_DIR, "config.json")

# Dongle assignment (Phase 6a):
#   A = Nooelec SMArt v5   (serial 70613472, port 1-4.1.4)
#   B = RTL-SDR Blog V4    (serial 83241970, port 1-4.1.2 — best DR)
SERIAL_A = os.environ.get("WATERFALL_SERIAL_A", "70613472")
SERIAL_B = os.environ.get("WATERFALL_SERIAL_B", "83241970")

# Default center frequencies — stitched window covers roughly
# 121.3 .. 126.1 MHz (most of civilian airband + a margin).
DEFAULT_CENTER_MHZ = 123.7
HALF_SPACING_MHZ = 1.2   # A = center-1.2, B = center+1.2 (2.4 MHz apart)

SAMPLE_RATE_HZ = 2_400_000   # 2.4 MS/s per dongle
FFT_SIZE = 1024
FRAME_PERIOD_SEC = 0.5
# 1024 bins per dongle stitched edge-to-edge with the 2.4 MHz spacing
# gives ~2048 bins of unique spectrum.  The overlap region is averaged
# (see _stitch_bins) — see comment in _stitch_bins for the rationale.

# Watchdog: 3 consecutive short/failed reads -> mark dongle down.
WATCHDOG_BAD_READS = 3
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

    def __init__(self, label: str, serial: str, initial_center_hz: float):
        self.label = label
        self.serial = serial
        self.center_hz = float(initial_center_hz)
        self._next_center_hz = float(initial_center_hz)
        self._lock = threading.Lock()
        self._latest: Optional[tuple] = None
        self._thread: Optional[threading.Thread] = None
        self.state = "down"   # flips to "ok" once first frame lands
        self.last_bus_path = ""
        self._sdr = None
        self._stream = None
        self._bad_reads = 0

    # ---- public ---------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name=f"wf-dongle-{self.label}", daemon=True
        )
        self._thread.start()

    def request_center_hz(self, hz: float) -> None:
        with self._lock:
            self._next_center_hz = float(hz)

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
            self._sdr.setSampleRate(SOAPY_SDR_RX, 0, SAMPLE_RATE_HZ)
            try:
                self._sdr.setBandwidth(
                    SOAPY_SDR_RX, 0, SAMPLE_RATE_HZ * 0.8
                )
            except Exception:
                pass
            # Modest fixed gain — auto-gain on RTL-SDR is hit-or-miss.
            try:
                self._sdr.setGainMode(SOAPY_SDR_RX, 0, False)
                self._sdr.setGain(SOAPY_SDR_RX, 0, 30.0)
            except Exception:
                pass
            with self._lock:
                center = self._next_center_hz
                self.center_hz = center
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
                SAMPLE_RATE_HZ / 1e6,
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
        if wanted == self.center_hz:
            return
        try:
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

    def _read_frame(self) -> Optional[np.ndarray]:
        """Read FFT_SIZE samples; return the complex64 buffer or None."""
        buf = np.zeros(FFT_SIZE, dtype=np.complex64)
        pos = 0
        t0 = time.time()
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
    mag_a, center_a_hz: float,
    mag_b, center_b_hz: float,
    sample_rate_hz: float,
):
    """Stitch two FFT outputs into a continuous bin array.

    Returns (bins, freq_min_hz, freq_max_hz).

    Strategy: each FFT covers [center - sr/2, center + sr/2].  We map both
    onto a shared frequency grid spanning [min(low_a, low_b),
    max(high_a, high_b)] at the same per-bin Hz as the input FFTs.  Where
    the two windows overlap we average the dBFS values — averaging is the
    simplest defensible reduction when both dongles are healthy; if either
    is contributing garbage the higher noise floor will pull the average
    up which is fine for a visual waterfall (it just makes the overlap
    region a touch noisier; the surrounding non-overlapping bins are
    untouched and look clean).
    """
    n = FFT_SIZE
    bin_hz = sample_rate_hz / n
    low_a = center_a_hz - sample_rate_hz / 2.0
    high_a = center_a_hz + sample_rate_hz / 2.0
    low_b = center_b_hz - sample_rate_hz / 2.0
    high_b = center_b_hz + sample_rate_hz / 2.0
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

    initial_center = DEFAULT_CENTER_MHZ * 1e6
    a = DongleWorker(
        "A", SERIAL_A, initial_center - HALF_SPACING_MHZ * 1e6,
    )
    b = DongleWorker(
        "B", SERIAL_B, initial_center + HALF_SPACING_MHZ * 1e6,
    )
    a.start()
    b.start()

    config_poller = ConfigPoller(CONFIG_PATH)

    LOG.info(
        "waterfall up: A=%s @ %.3f MHz, B=%s @ %.3f MHz; default center=%.3f MHz",
        SERIAL_A, (initial_center - HALF_SPACING_MHZ * 1e6) / 1e6,
        SERIAL_B, (initial_center + HALF_SPACING_MHZ * 1e6) / 1e6,
        DEFAULT_CENTER_MHZ,
    )

    current_center_hz = initial_center
    last_state_write = 0.0
    STATE_WRITE_PERIOD = 0.25   # 4 Hz cap

    while not _STOP.is_set():
        # Retune?
        cfg = config_poller.poll()
        if cfg is not None:
            try:
                new_center_mhz = float(cfg.get("center_mhz", DEFAULT_CENTER_MHZ))
                # Sanity-clamp to the RTL-SDR range (24 MHz - 1.7 GHz).
                if 24.0 <= new_center_mhz <= 1700.0:
                    current_center_hz = new_center_mhz * 1e6
                    a.request_center_hz(
                        current_center_hz - HALF_SPACING_MHZ * 1e6
                    )
                    b.request_center_hz(
                        current_center_hz + HALF_SPACING_MHZ * 1e6
                    )
                    LOG.info(
                        "config retune: center=%.3f MHz (A=%.3f, B=%.3f)",
                        new_center_mhz,
                        (current_center_hz - HALF_SPACING_MHZ * 1e6) / 1e6,
                        (current_center_hz + HALF_SPACING_MHZ * 1e6) / 1e6,
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
            mag_a, a.center_hz,
            mag_b, b.center_hz,
            SAMPLE_RATE_HZ,
        )

        a_ok = (a.state == "ok") and (age_a_ms is not None) and (age_a_ms < 5000)
        b_ok = (b.state == "ok") and (age_b_ms is not None) and (age_b_ms < 5000)
        if a_ok and b_ok:
            top_state = "ok"
        elif a_ok or b_ok:
            top_state = "degraded"
        else:
            top_state = "down"

        center_mhz = (f_min_hz + f_max_hz) / 2.0 / 1e6
        bw_mhz = (f_max_hz - f_min_hz) / 1e6

        ages_present = [v for v in (age_a_ms, age_b_ms) if v is not None]
        flat_last_age = min(ages_present) if ages_present else 0

        state = {
            "updated_ts": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ),
            "state": top_state,
            "center_mhz": round(center_mhz, 4),
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
                    "bus": a.last_bus_path or "",
                    "label": "A",
                },
                {
                    "serial": SERIAL_B,
                    "state": "ok" if b_ok else b.state,
                    "last_frame_age_ms": age_b_ms if age_b_ms is not None else -1,
                    "center_mhz": round(b.center_hz / 1e6, 4),
                    "bus": b.last_bus_path or "",
                    "label": "B",
                },
            ],
            "config_age_sec": round(config_poller.age_sec(), 1),
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

        # Pace the outer loop — workers are doing the heavy lifting; the
        # stitch/write loop just needs to be faster than the UI's poll.
        if _STOP.wait(0.1):
            break

    LOG.info("main loop exiting; stopping workers")
    a.stop_and_join(timeout=3.0)
    b.stop_and_join(timeout=3.0)
    LOG.info("waterfall shut down cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
