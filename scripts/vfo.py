"""Live tunable RTL-SDR VFO backend (Phase 6b).

Owns one RTL-SDR (Nooelec NESDR SMArt v5 serial 61108285, bus 1-5.1.4) via
SoapySDR.  Demodulates AM / NFM / WFM in-process from a single IQ
stream and publishes:

  1. 32 kbps MP3 audio to icecast mount /VFO.mp3 (via ffmpeg on stdin)
  2. Optional pw-cat side-pipe to the default PipeWire sink (e.g. UE
     BOOM 2) when config.bt_routed=true
  3. A ~256-bin mini-waterfall (2.4 MHz around tuned freq) into
     /run/scannerproject/vfo/state.json

We deliberately implement demod in-process (strategy "b" from the
Phase 6b spec) so a single owner controls the dongle — running rtl_fm
in parallel would race for the USB device.  All three outputs above
are derived from the same FFT_SIZE-sample IQ block, so the per-frame
cost is dominated by the SoapySDR read.

Watchdog / atomic-write / config-poll patterns mirror Phase 6a
(scripts/waterfall.py) and disco/src/sweep.py — see those for the
rationale on the bounded force-exit shutdown timer.

USB/LSB demod is intentionally stubbed (returns silence) — phase 6b.1.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Optional

import numpy as np
import scipy.signal as _sig
import SoapySDR
from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32

# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------
STATE_DIR = os.environ.get("VFO_STATE_DIR", "/run/scannerproject/vfo")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
CONFIG_PATH = os.path.join(STATE_DIR, "config.json")

# Dongle assignment (Phase 6b → 2026-06-08 re-assigned to NooElec):
# VFO = Nooelec NESDR SMArt v5 / R820T2 + TCXO, serial 61108285 on
# bus 1-5.1.4 (previously Realtek 2832U serial 80000003, swapped out
# due to ~10 dB lower SNR + worse PLL stability on weak NFM signals
# like NOAA WX 162.400).  Opened by serial — never by index, which
# can shift across reboots if other RTL-SDRs come and go.
SERIAL = os.environ.get("VFO_SERIAL", "61108285")

# Hard-coded RTL-SDR tuning range; the POST handler validates too.
FREQ_MIN_MHZ = 24.0
FREQ_MAX_MHZ = 1700.0

# Sensible default: civilian airband tower freq.
DEFAULT_FREQ_MHZ = 127.700
DEFAULT_MOD = "am"
DEFAULT_MUTED = False
DEFAULT_BT_ROUTED = False
# Phase 6b.2 — per-VFO squelch + gain knobs.
# Squelch metric: full-window IQ RMS in dBFS (cheap; one np.mean per
# block).  Below the threshold, audio output for the block is zeroed.
# Auto mode tracks a slow noise-floor follower (EMA of the lowest
# observed dBFS in the recent past) and gates when the current block
# is less than AUTO_SQUELCH_MARGIN_DB above that floor.
DEFAULT_SQUELCH_DBFS = -60.0          # one-glance default (open enough to hear most traffic)
DEFAULT_SQUELCH_AUTO = False
SQUELCH_MIN_DBFS = -80.0
SQUELCH_MAX_DBFS = 0.0
AUTO_SQUELCH_MARGIN_DB = 6.0          # how far above the floor we need to be to open
AUTO_SQUELCH_FLOOR_TC_S = 5.0         # floor follower time constant

# Gain: R820T2 valid steps go 0..49.6 dB in SoapySDR.  We expose a
# 0..49 integer dB UI range; setGain() snaps to the nearest valid
# step.  Default of 40 dB matches the design intent for the VFO
# dongle (a hot-but-not-overloaded floor for unknown-amplitude
# traffic).
DEFAULT_GAIN_DB = 40.0
GAIN_MIN_DB = 0.0
GAIN_MAX_DB = 49.0

SAMPLE_RATE_HZ = 2_400_000        # 2.4 MS/s — matches waterfall pattern
AUDIO_SR = 48_000                 # output audio sample rate
DECIMATION = SAMPLE_RATE_HZ // AUDIO_SR   # = 50 (clean integer)

# FFT for mini-waterfall.  We splat each block into a 256
# bin FFT covering the full 2.4 MHz window.
#
# Block size: 24000 IQ samples = 10 ms of IQ at 2.4 MS/s = 480 audio
# samples per block at 48 kHz (50:1 decimation, clean integer).  We
# used to run 2400-sample blocks (1 ms) but that meant the demod loop
# fired 1000x/sec and the per-call ufunc dispatch overhead on
# np.angle / np.abs / lfilter dominated CPU (~80% on one core for
# NFM).  10 ms blocks add negligible audio latency but cut the loop
# overhead ~10x.
FFT_SIZE_AUDIO = 24000            # 10 ms of IQ per block (50:1 dec -> 480 audio samples)
MINI_FFT_BINS = 256
MINI_FFT_PERIOD_SEC = 0.5         # ~2 Hz waterfall refresh

# Watchdog: 3 consecutive bad reads -> close + reopen with exponential
# backoff (5s -> 60s).  Matches Phase 6a waterfall.
WATCHDOG_BAD_READS = 3
RECONNECT_BACKOFF_INIT_S = 5.0
RECONNECT_BACKOFF_MAX_S = 60.0
RECONNECT_BACKOFF_GROWTH = 2.0    # 5 -> 10 -> 20 -> 40 -> 60 (capped)

# Bounded graceful shutdown.
FORCE_EXIT_TIMEOUT_SEC = 5.0

# Icecast publish.
ICECAST_HOST = os.environ.get("ICECAST_HOST", "127.0.0.1")
ICECAST_PORT = int(os.environ.get("ICECAST_PORT", "8000"))
ICECAST_USER = os.environ.get("ICECAST_SOURCE_USER", "source")
ICECAST_PASSWORD = os.environ.get("ICECAST_SOURCE_PASSWORD", "062352")
ICECAST_MOUNT = os.environ.get("VFO_ICECAST_MOUNT", "/VFO.mp3").lstrip("/")
ICECAST_BITRATE_KBPS = 32

LOG = logging.getLogger("scanner.vfo")
_STOP = threading.Event()
_FORCE_EXIT_ARMED = False


def _setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("VFO_LOG_LEVEL", "INFO"),
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
        target=_killer, daemon=True, name="vfo-force-exit"
    ).start()


def _handle_stop(signum, frame):
    LOG.info("stopping on signal %s", signum)
    _STOP.set()
    _arm_force_exit_watchdog(FORCE_EXIT_TIMEOUT_SEC)


# ---------------------------------------------------------------------
# Atomic writer
# ---------------------------------------------------------------------
def _write_state_atomic(state: dict, path: str) -> None:
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
# Config poller — mtime-based.
# ---------------------------------------------------------------------
class ConfigPoller:
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
# Demodulators — all operate on the SAME complex64 IQ block sampled at
# SAMPLE_RATE_HZ and return mono float32 PCM at AUDIO_SR.
# ---------------------------------------------------------------------
class AMDemod:
    """Envelope detector + DC block + decimate."""

    # 1st-order DC block: y[n] = x[n] - x[n-1] + 0.995*y[n-1]
    # As b/a coefficients for scipy.signal.lfilter:
    #   a[0]*y[n] = b[0]*x[n] + b[1]*x[n-1] - a[1]*y[n-1]
    # so b=[1, -1], a=[1, -0.995].
    _DC_B = np.array([1.0, -1.0], dtype=np.float32)
    _DC_A = np.array([1.0, -0.995], dtype=np.float32)

    def __init__(self):
        # Filter state: max(len(a), len(b))-1 = 1 sample.  Persists
        # across blocks so the DC block is continuous.
        self._dc_zi = np.zeros(1, dtype=np.float32)

    def __call__(self, iq: np.ndarray) -> np.ndarray:
        mag = np.abs(iq).astype(np.float32)
        # Vectorized DC block via lfilter — same math as the per-sample
        # Python loop we used to run, but the inner work is in C.  At
        # 24000 samples/block this is the difference between ~0.5 ms
        # per block and ~12 ms per block (i.e. it would single-handedly
        # peg a core if left in Python).
        out, self._dc_zi = _sig.lfilter(
            self._DC_B, self._DC_A, mag, zi=self._dc_zi,
        )
        return _decimate(out.astype(np.float32, copy=False), DECIMATION)


class FMDemod:
    """IQ channel-filter + polar discriminator + optional deemphasis.

    Critical: the discriminator MUST run on channel-filtered IQ, not
    the raw 2.4 MS/s stream — otherwise FM-threshold collapses because
    the discriminator sees 2.4 MHz of noise burden against a 12.5 kHz
    signal (~24 dB SNR penalty).  Filter chain:
      2.4 MS/s IQ  →  stage-1 anti-alias (FIR, decim 10)  →  240 kHz
      240 kHz IQ   →  stage-2 channel filter (FIR, decim 5) → 48 kHz
      48 kHz IQ    →  polar discriminator                  → 48 kHz audio
      48 kHz audio →  optional 75 us deemphasis            → output

    NFM channel BW ≈ 12.5 kHz (cutoff 9 kHz, allows ±5 kHz dev + skirt)
    WFM channel BW ≈ 200 kHz (cap stage-2 cutoff at 22 kHz audio Nyquist;
        wideband mono FM is recovered up to that audio cutoff)
    """

    def __init__(
        self,
        deemph_us: Optional[float] = None,
        channel_bw_hz: int = 12_500,
    ):
        # Stage 1: 2.4 MS/s → 240 kHz, anti-alias at 100 kHz
        self._aa1 = _sig.firwin(
            31, 100_000, fs=SAMPLE_RATE_HZ, window=("kaiser", 5.0),
        ).astype(np.float32)
        # Stage 2: 240 kHz → 48 kHz, channel filter at channel_bw_hz/2
        # Cap cutoff at 22 kHz (audio Nyquist with margin) for wideband WFM.
        cutoff2 = min(channel_bw_hz / 2, 22_000)
        self._aa2 = _sig.firwin(
            63, cutoff2, fs=SAMPLE_RATE_HZ // 10, window=("kaiser", 6.0),
        ).astype(np.float32)
        self._aa2_zi_i = np.zeros(len(self._aa2) - 1, dtype=np.float32)
        self._aa2_zi_q = np.zeros(len(self._aa2) - 1, dtype=np.float32)
        self._aa2_phase = 0

        self._last_sample = np.complex64(1.0 + 0j)

        if deemph_us:
            tau = deemph_us * 1e-6
            a = float(np.exp(-1.0 / (AUDIO_SR * tau)))
            self._deemph_b = np.array([1.0 - a], dtype=np.float32)
            self._deemph_a = np.array([1.0, -a], dtype=np.float32)
            self._deemph_zi = np.zeros(1, dtype=np.float32)
        else:
            self._deemph_b = None
            self._deemph_a = None
            self._deemph_zi = None

    def __call__(self, iq: np.ndarray) -> np.ndarray:
        if iq.size == 0:
            return np.zeros(0, dtype=np.float32)

        # Stage 1: anti-alias decimation 10:1, stateless (transient < 1 sample
        # of audio, inaudible).
        i = iq.real.astype(np.float32, copy=False)
        q = iq.imag.astype(np.float32, copy=False)
        i = _sig.upfirdn(self._aa1, i, up=1, down=10).astype(np.float32, copy=False)
        q = _sig.upfirdn(self._aa1, q, up=1, down=10).astype(np.float32, copy=False)

        # Stage 2: channel-select decimation 5:1, stateful (filter state
        # carries across blocks to avoid ~200 us block-edge artifacts).
        i, self._aa2_zi_i = _sig.lfilter(
            self._aa2, [1.0], i, zi=self._aa2_zi_i,
        )
        q, self._aa2_zi_q = _sig.lfilter(
            self._aa2, [1.0], q, zi=self._aa2_zi_q,
        )
        i = i.astype(np.float32, copy=False)
        q = q.astype(np.float32, copy=False)
        # Decimation phase tracking: continuation of the 5:1 sampling grid
        # across blocks (input lengths aren't guaranteed multiples of 5).
        start = self._aa2_phase
        i_dec = i[start::5]
        q_dec = q[start::5]
        L_in = len(i)
        K_out = len(i_dec)
        if K_out > 0:
            self._aa2_phase = (start + 5 * K_out - L_in) % 5
        else:
            self._aa2_phase = (start - L_in) % 5

        if K_out == 0:
            return np.zeros(0, dtype=np.float32)

        # Polar discriminator on the 48 kHz channel-filtered IQ.
        iq48 = (i_dec + 1j * q_dec).astype(np.complex64)
        audio = np.empty(iq48.shape[0], dtype=np.float32)
        first_prod = iq48[0] * np.conj(self._last_sample)
        audio[0] = np.arctan2(float(first_prod.imag), float(first_prod.real))
        if iq48.shape[0] > 1:
            audio[1:] = np.angle(iq48[1:] * np.conj(iq48[:-1]))
        self._last_sample = iq48[-1]

        # Gain: at 48 kHz output, ±5 kHz NFM deviation gives ±0.654 rad
        # of phase change per sample.  Scale to ~unity peak for typical traffic.
        audio *= 1.5

        if self._deemph_b is not None:
            audio, self._deemph_zi = _sig.lfilter(
                self._deemph_b, self._deemph_a, audio, zi=self._deemph_zi,
            )
            audio = audio.astype(np.float32, copy=False)
        return audio


def _decimate(x: np.ndarray, factor: int) -> np.ndarray:
    """Simple boxcar (averaging) decimator.

    Trim the input to a multiple of `factor` so we don't lose samples
    between blocks; the leftover at the tail of one block becomes a
    rounding error of <1 ms of audio — far below perceptual threshold.
    """
    n = (x.shape[0] // factor) * factor
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    return x[:n].reshape(-1, factor).mean(axis=1).astype(np.float32)


# ---------------------------------------------------------------------
# Audio publish chain: ffmpeg(s16le on stdin) -> icecast /VFO.mp3,
# plus optional pw-cat side-pipe to the default PipeWire sink.
# ---------------------------------------------------------------------
class IcecastPublisher:
    """Manage one long-lived ffmpeg subprocess that pumps to icecast.

    We write float32 -> int16 PCM frames to ffmpeg's stdin.  ffmpeg
    encodes mp3 and POSTs to icecast; on failure (icecast down,
    network glitch), we restart.
    """

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self.last_error: str = ""
        self.publishing: bool = False

    def _spawn(self) -> bool:
        url = (
            f"icecast://{ICECAST_USER}:{ICECAST_PASSWORD}@"
            f"{ICECAST_HOST}:{ICECAST_PORT}/{ICECAST_MOUNT}"
        )
        cmd = [
            "ffmpeg",
            "-hide_banner", "-loglevel", "warning",
            "-f", "s16le", "-ar", str(AUDIO_SR), "-ac", "1",
            "-i", "pipe:0",
            "-codec:a", "libmp3lame",
            "-b:a", f"{ICECAST_BITRATE_KBPS}k",
            "-content_type", "audio/mpeg",
            "-ice_name", "VFO - tunable receiver",
            "-ice_description", "VFO - tunable receiver",
            "-ice_genre", "Scanner",
            "-f", "mp3",
            url,
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            LOG.info(
                "icecast publish started: %s:%d/%s @ %d kbps",
                ICECAST_HOST, ICECAST_PORT, ICECAST_MOUNT,
                ICECAST_BITRATE_KBPS,
            )
            self.publishing = True
            self.last_error = ""
            return True
        except Exception as e:
            LOG.warning("ffmpeg/icecast spawn failed: %s", e)
            self.last_error = str(e)
            self._proc = None
            self.publishing = False
            return False

    def write(self, pcm_int16: bytes) -> None:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                if self._proc is not None:
                    try:
                        rc = self._proc.returncode
                        err = (self._proc.stderr.read().decode(errors="replace")
                               if self._proc.stderr else "")
                        LOG.warning(
                            "ffmpeg exited rc=%s err=%s; respawning",
                            rc, err[:400],
                        )
                        self.last_error = f"ffmpeg exited rc={rc}"
                    except Exception:
                        pass
                if not self._spawn():
                    return
            try:
                self._proc.stdin.write(pcm_int16)
            except (BrokenPipeError, OSError) as e:
                LOG.warning("icecast stdin write failed: %s; will respawn", e)
                self.last_error = str(e)
                try:
                    self._proc.kill()
                except Exception:
                    pass
                self._proc = None
                self.publishing = False

    def close(self) -> None:
        with self._lock:
            if self._proc is not None:
                try:
                    self._proc.stdin.close()
                except Exception:
                    pass
                try:
                    self._proc.wait(timeout=2.0)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
                self._proc = None
                self.publishing = False
                LOG.info("icecast publish closed")


class BTSidePipe:
    """Optional pw-cat side-pipe to the default PipeWire sink.

    If pw-cat can't be spawned or the sink isn't reachable, we set
    .active = False and surface that in state.json so the UI can show
    the requested routing failed.  Never blocks the main publish.
    """

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self.requested: bool = False     # config-requested state
        self.active: bool = False        # actual state
        self.last_error: str = ""

    def set_requested(self, on: bool) -> None:
        with self._lock:
            self.requested = bool(on)
            if not on and self._proc is not None:
                try:
                    self._proc.stdin.close()
                except Exception:
                    pass
                try:
                    self._proc.terminate()
                except Exception:
                    pass
                self._proc = None
                self.active = False
                LOG.info("BT side-pipe disabled (requested off)")

    def _spawn(self) -> bool:
        # pw-cat --playback - reads s16le from stdin and plays to the
        # default sink.  Use --target @DEFAULT_SINK@ explicitly so we
        # follow whatever sink the system has selected (BOOM 2 if paired
        # & active; otherwise the built-in sink, which is fine — the
        # fallback rule says we shouldn't block on BT chain issues).
        cmd = [
            "pw-cat", "--playback", "-",
            "--rate", str(AUDIO_SR),
            "--channels", "1",
            "--format", "s16",
            "--target", "@DEFAULT_SINK@",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            self.active = True
            self.last_error = ""
            LOG.info("BT side-pipe started via pw-cat -> @DEFAULT_SINK@")
            return True
        except FileNotFoundError as e:
            LOG.warning("pw-cat not available: %s", e)
            self.last_error = f"pw-cat not found: {e}"
            self.active = False
            self._proc = None
            return False
        except Exception as e:
            LOG.warning("pw-cat spawn failed: %s", e)
            self.last_error = str(e)
            self.active = False
            self._proc = None
            return False

    def write(self, pcm_int16: bytes) -> None:
        with self._lock:
            if not self.requested:
                return
            if self._proc is None or self._proc.poll() is not None:
                if self._proc is not None:
                    try:
                        rc = self._proc.returncode
                        err = (self._proc.stderr.read().decode(errors="replace")
                               if self._proc.stderr else "")
                        LOG.warning(
                            "pw-cat exited rc=%s err=%s; falling back to icecast-only",
                            rc, err[:200],
                        )
                        self.last_error = f"pw-cat exited rc={rc}"
                    except Exception:
                        pass
                    self._proc = None
                    self.active = False
                # Try to (re)spawn — but if it fails we just stay inactive.
                if not self._spawn():
                    return
            try:
                self._proc.stdin.write(pcm_int16)
            except (BrokenPipeError, OSError) as e:
                LOG.warning("pw-cat write failed: %s; deactivating", e)
                self.last_error = str(e)
                try:
                    self._proc.kill()
                except Exception:
                    pass
                self._proc = None
                self.active = False

    def close(self) -> None:
        with self._lock:
            if self._proc is not None:
                try:
                    self._proc.stdin.close()
                except Exception:
                    pass
                try:
                    self._proc.wait(timeout=1.0)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
                self._proc = None
                self.active = False


# ---------------------------------------------------------------------
# Bus-path resolver
# ---------------------------------------------------------------------
def _resolve_bus_path(serial: str) -> str:
    try:
        base = "/sys/bus/usb/devices"
        for entry in os.listdir(base):
            sf = os.path.join(base, entry, "serial")
            try:
                with open(sf) as f:
                    s = f.read().strip()
            except OSError:
                continue
            if s == serial:
                return entry
    except OSError:
        pass
    return ""


# ---------------------------------------------------------------------
# Mini-waterfall FFT helper.
# ---------------------------------------------------------------------
def _compute_mini_bins(iq: np.ndarray) -> np.ndarray:
    """Return MINI_FFT_BINS dBFS values covering the 2.4 MHz window.

    We FFT the first MINI_FFT_BINS samples (or the whole block, taking
    the dominant magnitude per bin via reshaping).  Keep it cheap —
    this is a visual aid, not an analyzer.
    """
    n = MINI_FFT_BINS
    if iq.shape[0] < n:
        return np.full(n, -120.0, dtype=np.float32)
    # Take a centered slice equal to a power-of-two multiple of n so the
    # reshape + mean averages noisy bins down toward the noise floor.
    take = n
    while take * 2 <= iq.shape[0]:
        take *= 2
    blk = iq[:take]
    win = np.hanning(take).astype(np.float32)
    spec = np.fft.fftshift(np.fft.fft(blk * win))
    mag2 = (spec.real * spec.real + spec.imag * spec.imag).astype(np.float32)
    # Group every (take/n) consecutive bins down to n bins via mean.
    group = take // n
    mag2 = mag2[: group * n].reshape(n, group).mean(axis=1)
    mag_db = 10.0 * np.log10(mag2 + 1e-30) - 20.0 * np.log10(take)
    return mag_db.astype(np.float32)


# ---------------------------------------------------------------------
# DongleWorker — owns the SoapySDR device + demod + audio publish chain.
# ---------------------------------------------------------------------
class VFOWorker:
    def __init__(
        self,
        serial: str,
        icecast: IcecastPublisher,
        bt_pipe: BTSidePipe,
    ):
        self.serial = serial
        self.icecast = icecast
        self.bt_pipe = bt_pipe

        # Live config state (thread-safe via _cfg_lock).
        self._cfg_lock = threading.Lock()
        self._freq_mhz = DEFAULT_FREQ_MHZ
        self._mod = DEFAULT_MOD
        self._muted = DEFAULT_MUTED
        self._bt_routed = DEFAULT_BT_ROUTED
        self._squelch_dbfs = DEFAULT_SQUELCH_DBFS
        self._squelch_auto = DEFAULT_SQUELCH_AUTO
        self._gain_db = DEFAULT_GAIN_DB

        # Applied state (so the outer loop can see what's actually on).
        self.applied_freq_mhz = DEFAULT_FREQ_MHZ
        self.applied_mod = DEFAULT_MOD
        self.applied_muted = DEFAULT_MUTED
        self.applied_gain_db = DEFAULT_GAIN_DB

        # Squelch runtime state — current block RMS dBFS and the
        # auto-squelch noise-floor follower.
        self.last_rms_dbfs = -120.0
        self.last_squelch_open = True
        self._floor_dbfs = -60.0           # EMA-tracked noise floor for auto squelch

        # Health.
        self.state = "down"
        self.last_bus_path = ""
        self.last_frame_age_ms = -1
        self._last_frame_ts = 0.0

        self._sdr = None
        self._stream = None
        self._bad_reads = 0

        # Demod instance pool — recreated on mod change so internal
        # state (filter memory) starts fresh.
        self._demod_am = AMDemod()
        self._demod_nfm = FMDemod(deemph_us=75.0)
        self._demod_wfm = FMDemod(deemph_us=75.0)

        # Mini-waterfall throttle.
        self._mini_last_ts = 0.0
        self.mini_bins: list[float] = [-120.0] * MINI_FFT_BINS

    # ---- public config surface --------------------------------------
    def apply_config(self, cfg: dict) -> None:
        with self._cfg_lock:
            if "freq_mhz" in cfg:
                try:
                    v = float(cfg["freq_mhz"])
                    if FREQ_MIN_MHZ <= v <= FREQ_MAX_MHZ:
                        self._freq_mhz = v
                except (TypeError, ValueError):
                    pass
            if "mod" in cfg:
                m = str(cfg["mod"]).lower()
                if m in ("am", "nfm", "wfm", "usb", "lsb"):
                    self._mod = m
                # USB/LSB intentionally don't synthesize audio (stubs).
            if "muted" in cfg:
                self._muted = bool(cfg["muted"])
            if "bt_routed" in cfg:
                self._bt_routed = bool(cfg["bt_routed"])
                self.bt_pipe.set_requested(self._bt_routed)
            # Phase 6b.2 — squelch + gain pickup.  Both apply live
            # without a restart: gain via setGain() in the runtime
            # tick, squelch via the per-block compare in run().
            if "squelch_dbfs" in cfg:
                try:
                    v = float(cfg["squelch_dbfs"])
                    if v < SQUELCH_MIN_DBFS: v = SQUELCH_MIN_DBFS
                    if v > SQUELCH_MAX_DBFS: v = SQUELCH_MAX_DBFS
                    self._squelch_dbfs = v
                except (TypeError, ValueError):
                    pass
            if "squelch_auto" in cfg:
                self._squelch_auto = bool(cfg["squelch_auto"])
            if "gain_db" in cfg:
                try:
                    v = float(cfg["gain_db"])
                    if v < GAIN_MIN_DB: v = GAIN_MIN_DB
                    if v > GAIN_MAX_DB: v = GAIN_MAX_DB
                    self._gain_db = v
                except (TypeError, ValueError):
                    pass

    def snapshot(self) -> dict:
        with self._cfg_lock:
            return {
                "freq_mhz": self._freq_mhz,
                "mod": self._mod,
                "muted": self._muted,
                "bt_routed": self._bt_routed,
                "squelch_dbfs": self._squelch_dbfs,
                "squelch_auto": self._squelch_auto,
                "gain_db": self._gain_db,
            }

    # ---- device lifecycle -------------------------------------------
    def _open(self) -> bool:
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
            try:
                self._sdr.setGainMode(SOAPY_SDR_RX, 0, False)
                # Apply the configured gain (snaps to nearest R820T2 step).
                with self._cfg_lock:
                    g = float(self._gain_db)
                self._sdr.setGain(SOAPY_SDR_RX, 0, g)
                self.applied_gain_db = g
            except Exception:
                pass
            with self._cfg_lock:
                f_hz = self._freq_mhz * 1e6
                self.applied_freq_mhz = self._freq_mhz
                self.applied_mod = self._mod
                self.applied_muted = self._muted
            self._sdr.setFrequency(SOAPY_SDR_RX, 0, f_hz)
            self._stream = self._sdr.setupStream(
                SOAPY_SDR_RX, SOAPY_SDR_CF32, [0]
            )
            self._sdr.activateStream(self._stream)
            self.last_bus_path = _resolve_bus_path(self.serial)
            LOG.info(
                "opened serial=%s bus=%s freq=%.3f MHz mod=%s rate=%.3f MS/s",
                self.serial, self.last_bus_path or "?",
                self.applied_freq_mhz, self.applied_mod,
                SAMPLE_RATE_HZ / 1e6,
            )
            self._bad_reads = 0
            self.state = "ok"
            return True
        except Exception as e:
            LOG.warning(
                "open failed serial=%s bus=%s err=%s",
                self.serial, self.last_bus_path or "?", e,
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
                        "deactivateStream err serial=%s: %s",
                        self.serial, e,
                    )
                try:
                    self._sdr.closeStream(self._stream)
                except Exception as e:
                    LOG.warning(
                        "closeStream err serial=%s: %s",
                        self.serial, e,
                    )
        finally:
            self._stream = None
            self._sdr = None
            LOG.info(
                "closed serial=%s bus=%s",
                self.serial, self.last_bus_path or "?",
            )

    def _maybe_apply_runtime_changes(self) -> None:
        """Apply freq/mod/gain changes to a live, open device."""
        with self._cfg_lock:
            want_freq = self._freq_mhz
            want_mod = self._mod
            want_muted = self._muted
            want_gain = self._gain_db
        if want_freq != self.applied_freq_mhz and self._sdr is not None:
            try:
                self._sdr.setFrequency(SOAPY_SDR_RX, 0, want_freq * 1e6)
                LOG.info(
                    "retuned serial=%s %.3f -> %.3f MHz",
                    self.serial, self.applied_freq_mhz, want_freq,
                )
                self.applied_freq_mhz = want_freq
            except Exception as e:
                LOG.warning(
                    "retune failed serial=%s -> %.3f MHz: %s",
                    self.serial, want_freq, e,
                )
        if want_mod != self.applied_mod:
            LOG.info(
                "mod change serial=%s %s -> %s",
                self.serial, self.applied_mod, want_mod,
            )
            # Reset demod state by recreating instances.
            self._demod_am = AMDemod()
            self._demod_nfm = FMDemod(deemph_us=75.0)
            self._demod_wfm = FMDemod(deemph_us=75.0)
            self.applied_mod = want_mod
        if want_muted != self.applied_muted:
            self.applied_muted = want_muted
        # Phase 6b.2 — live gain retune.  R820T2's setGain() snaps
        # to nearest valid step; we still update applied_gain_db to
        # the requested value (the snap is transparent to the UI).
        if abs(want_gain - self.applied_gain_db) >= 0.5 and self._sdr is not None:
            try:
                self._sdr.setGain(SOAPY_SDR_RX, 0, float(want_gain))
                LOG.info(
                    "gain change serial=%s %.1f -> %.1f dB",
                    self.serial, self.applied_gain_db, want_gain,
                )
                self.applied_gain_db = float(want_gain)
            except Exception as e:
                LOG.warning(
                    "gain retune failed serial=%s -> %.1f dB: %s",
                    self.serial, want_gain, e,
                )

    # ---- read + demod -----------------------------------------------
    def _read_block(self, n: int) -> Optional[np.ndarray]:
        buf = np.zeros(n, dtype=np.complex64)
        pos = 0
        t0 = time.time()
        while pos < n:
            if _STOP.is_set():
                return None
            chunk = min(8192, n - pos)
            try:
                sr = self._sdr.readStream(
                    self._stream, [buf[pos:pos + chunk]], chunk,
                    timeoutUs=2_000_000,
                )
            except Exception as e:
                LOG.warning(
                    "readStream raised serial=%s bus=%s: %s",
                    self.serial, self.last_bus_path or "?", e,
                )
                return None
            if sr.ret < 0:
                LOG.warning(
                    "readStream err serial=%s ret=%s flags=%s",
                    self.serial, sr.ret, sr.flags,
                )
                return None
            pos += sr.ret
            if time.time() - t0 > 1.0:
                LOG.warning(
                    "read time-bounded at pos=%d/%d serial=%s",
                    pos, n, self.serial,
                )
                return None
        return buf

    def _demod(self, iq: np.ndarray) -> np.ndarray:
        mod = self.applied_mod
        if mod == "am":
            return self._demod_am(iq)
        if mod == "nfm":
            return self._demod_nfm(iq)
        if mod == "wfm":
            return self._demod_wfm(iq)
        # USB/LSB stubs — return silence at the expected output rate.
        return np.zeros(iq.shape[0] // DECIMATION, dtype=np.float32)

    @staticmethod
    def _f32_to_s16_bytes(audio: np.ndarray, muted: bool) -> bytes:
        if muted or audio.size == 0:
            return (np.zeros(audio.shape[0], dtype=np.int16)).tobytes()
        # Soft clip to [-1, +1] then scale to int16.
        clipped = np.clip(audio, -1.0, 1.0)
        return (clipped * 32700.0).astype(np.int16).tobytes()

    # ---- run loop ---------------------------------------------------
    def run(self) -> None:
        backoff = RECONNECT_BACKOFF_INIT_S
        while not _STOP.is_set():
            if self._sdr is None:
                if not self._open():
                    LOG.warning(
                        "reconnect sleep %.1fs serial=%s",
                        backoff, self.serial,
                    )
                    if _STOP.wait(backoff):
                        break
                    backoff = min(
                        backoff * RECONNECT_BACKOFF_GROWTH,
                        RECONNECT_BACKOFF_MAX_S,
                    )
                    continue
                backoff = RECONNECT_BACKOFF_INIT_S

            self._maybe_apply_runtime_changes()

            iq = self._read_block(FFT_SIZE_AUDIO)
            if iq is None:
                self._bad_reads += 1
                if self._bad_reads >= WATCHDOG_BAD_READS:
                    LOG.warning(
                        "watchdog tripped (%d bad reads) serial=%s bus=%s; closing for reopen",
                        self._bad_reads, self.serial,
                        self.last_bus_path or "?",
                    )
                    self._safe_close()
                    self.state = "down"
                if _STOP.wait(0.1):
                    break
                continue

            self._bad_reads = 0
            self._last_frame_ts = time.time()
            self.last_frame_age_ms = 0
            self.state = "ok"

            audio = self._demod(iq)

            # ---- Squelch gate (Phase 6b.2) ----
            # Cheap power metric — mean |z|^2 of the IQ block in
            # dBFS (re: full-scale complex magnitude of 1.0).  This
            # is the whole-window RMS, not per-bin; for VFO use the
            # selected channel dominates the window so it's a fine
            # proxy without an extra FFT.
            power = float(np.mean(iq.real * iq.real + iq.imag * iq.imag))
            rms_dbfs = 10.0 * np.log10(power + 1e-30)
            self.last_rms_dbfs = rms_dbfs
            # EMA noise-floor follower for auto squelch.  ~5s TC at
            # 10 ms blocks => alpha = 1 - exp(-dt/tau).
            alpha = 1.0 - 2.71828 ** (-0.01 / AUTO_SQUELCH_FLOOR_TC_S)
            if rms_dbfs < self._floor_dbfs:
                # Track the floor *down* fast so we converge on quiet.
                self._floor_dbfs = (1.0 - alpha) * self._floor_dbfs + alpha * rms_dbfs
            else:
                # Track *up* slowly so a brief peak doesn't move the floor.
                self._floor_dbfs = (1.0 - alpha * 0.1) * self._floor_dbfs + (alpha * 0.1) * rms_dbfs
            with self._cfg_lock:
                want_auto = self._squelch_auto
                want_thresh = self._squelch_dbfs
            if want_auto:
                open_now = rms_dbfs >= (self._floor_dbfs + AUTO_SQUELCH_MARGIN_DB)
            else:
                open_now = rms_dbfs >= want_thresh
            self.last_squelch_open = bool(open_now)
            # Squelched blocks emit silence so the icecast/BT pipes
            # keep running (clients don't disconnect) but the user
            # hears nothing — same UX as the manual `muted` flag.
            audio_for_out = audio if open_now else np.zeros_like(audio)
            pcm = self._f32_to_s16_bytes(audio_for_out, self.applied_muted)
            self.icecast.write(pcm)
            self.bt_pipe.write(pcm)

            # Mini-waterfall throttle.
            now = time.time()
            if now - self._mini_last_ts >= MINI_FFT_PERIOD_SEC:
                try:
                    bins = _compute_mini_bins(iq)
                    self.mini_bins = [round(float(v), 1) for v in bins.tolist()]
                except Exception as e:
                    LOG.warning("mini fft failed: %s", e)
                self._mini_last_ts = now

        self._safe_close()
        LOG.info("worker exiting serial=%s", self.serial)


# ---------------------------------------------------------------------
# main
# ---------------------------------------------------------------------
def _maybe_seed_default_config() -> None:
    """If config.json doesn't exist yet, write a default so the UI sees
    sensible values on first boot.  Idempotent."""
    if os.path.exists(CONFIG_PATH):
        return
    payload = {
        "freq_mhz": DEFAULT_FREQ_MHZ,
        "mod": DEFAULT_MOD,
        "muted": DEFAULT_MUTED,
        "bt_routed": DEFAULT_BT_ROUTED,
        "squelch_dbfs": DEFAULT_SQUELCH_DBFS,
        "squelch_auto": DEFAULT_SQUELCH_AUTO,
        "gain_db": DEFAULT_GAIN_DB,
    }
    try:
        _write_state_atomic(payload, CONFIG_PATH)
        LOG.info("seeded default config.json")
    except OSError as e:
        LOG.warning("could not seed default config: %s", e)


def main() -> int:
    _setup_logging()
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    os.makedirs(STATE_DIR, exist_ok=True)
    _maybe_seed_default_config()

    icecast = IcecastPublisher()
    bt_pipe = BTSidePipe()
    worker = VFOWorker(SERIAL, icecast, bt_pipe)

    # Apply the on-disk config (if any) before starting the worker thread
    # so the first frame is tuned to the user's last setting.
    poller = ConfigPoller(CONFIG_PATH)
    initial = poller.poll()
    if initial:
        worker.apply_config(initial)

    t = threading.Thread(
        target=worker.run, name="vfo-worker", daemon=True,
    )
    t.start()

    LOG.info(
        "vfo up: serial=%s default_freq=%.3f MHz mod=%s",
        SERIAL, DEFAULT_FREQ_MHZ, DEFAULT_MOD,
    )

    STATE_WRITE_PERIOD = 0.25
    last_state_write = 0.0

    while not _STOP.is_set():
        cfg = poller.poll()
        if cfg is not None:
            LOG.info("config change applied: %s", cfg)
            worker.apply_config(cfg)

        # State.json snapshot.
        snap = worker.snapshot()
        # Compute frame age.
        if worker._last_frame_ts > 0:
            age_ms = int((time.time() - worker._last_frame_ts) * 1000.0)
        else:
            age_ms = -1
        worker.last_frame_age_ms = age_ms

        # Top-level state.  If muted is desired, we still consider the
        # service "ok" as long as the dongle is producing frames — muting
        # is a config choice, not a health signal.
        if worker.state == "ok" and 0 <= age_ms < 5000:
            top_state = "ok"
        elif worker.state == "ok":
            top_state = "degraded"
        else:
            top_state = "down"

        f = snap["freq_mhz"]
        bw = SAMPLE_RATE_HZ / 1e6
        state = {
            "updated_ts": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ),
            "state": top_state,
            "freq_mhz": round(f, 4),
            "mod": snap["mod"],
            "muted": bool(snap["muted"]),
            "bt_routed": bool(snap["bt_routed"]),
            "bt_active": bool(bt_pipe.active),
            "bt_last_error": bt_pipe.last_error,
            "squelch_dbfs": float(snap.get("squelch_dbfs", DEFAULT_SQUELCH_DBFS)),
            "squelch_auto": bool(snap.get("squelch_auto", DEFAULT_SQUELCH_AUTO)),
            "gain_db": float(worker.applied_gain_db),
            "squelch_open": bool(worker.last_squelch_open),
            "rms_dbfs": round(float(worker.last_rms_dbfs), 1),
            "bins": worker.mini_bins,
            "freq_min_mhz": round(f - bw / 2, 4),
            "freq_max_mhz": round(f + bw / 2, 4),
            "dongle": {
                "serial": SERIAL,
                "state": worker.state,
                "last_frame_age_ms": age_ms,
                "bus": worker.last_bus_path or "",
            },
            # Flat alias kept for the existing sb5 renderer fallback.
            "dongle_serial": SERIAL,
            "last_frame_age_ms": age_ms,
            "audio_bitrate_kbps": ICECAST_BITRATE_KBPS,
            "mount": "/" + ICECAST_MOUNT,
            "mount_publishing": bool(icecast.publishing),
            "icecast_last_error": icecast.last_error,
            "config_age_sec": round(poller.age_sec(), 1),
        }

        now = time.time()
        if now - last_state_write >= STATE_WRITE_PERIOD:
            try:
                _write_state_atomic(state, STATE_PATH)
                last_state_write = now
            except Exception as e:
                LOG.warning("state write failed: %s", e)

        if _STOP.wait(0.1):
            break

    LOG.info("main loop exiting; stopping worker + audio chain")
    t.join(timeout=3.0)
    icecast.close()
    bt_pipe.close()
    LOG.info("vfo shut down cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
