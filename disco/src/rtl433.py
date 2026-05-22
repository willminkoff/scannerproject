"""disco/src/rtl433.py — rtl_433 specialist identifier for ISM-band devices.

Option 3 (IQ slice replay): disco's sweep already saves short IQ slices to
``/run/scannerproject/disco/slices/``. For detections in an ISM band
(315 / 433 / 868 / 915 MHz) this module replays the slice through the
``rtl_433`` binary and, if rtl_433 decodes a device packet, returns a
device-level identification (weather sensor, TPMS, doorbell, energy meter,
…; rtl_433 supports 250+ device protocols).

Design contract (do-no-harm — Will's explicit requirements):
  - NEVER raise into the caller. Every entry point catches all exceptions
    and returns a safe default. Disco's identification chain must keep
    working if rtl_433 is missing, misconfigured, times out, or returns
    garbage.
  - Single env-var kill switch: ``DISCO_RTL433_ENABLED=0`` disables the
    layer entirely (default enabled, but the layer is also gated by
    ``is_available()`` so a missing binary is a no-op).
  - Every invocation is logged: ``[rtl_433] freq=<MHz> slice=<basename>
    result=<match|no-match|error:<reason>>``.
  - Counters are persisted to a small JSON stats file so the dashboard
    process (separate from the classifier) can surface them in
    ``/api/status``.
  - subprocess stderr is redirected to a *file*, never a PIPE — an
    unbounded stderr PIPE on a chatty child is the NSW MemoryError pattern
    we are deliberately avoiding. stdout (small line-delimited JSON) uses a
    PIPE because we must capture it, bounded by a short timeout + tiny
    slice.

Slice format note: disco slices are interleaved complex float32 with a
``.iq.f32`` extension. rtl_433 reads that layout as ``cf32`` but won't
auto-detect it from the extension, so we pass ``-r cf32:<path>`` and the
sample rate via ``-s`` (parsed from the slice filename). Most OOK devices
expect ~250 kHz; disco slices are decimated to ~50 kHz, so some devices
will not be decodable from replay — documented in docs/disco-rtl433.md as
the known limitation that would justify the Option 1 (dedicated dongle)
fallback.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("disco.rtl433")

RTL433_BIN = os.environ.get("DISCO_RTL433_BIN", "rtl_433")

# Where the classifier writes invocation counters and the dashboard reads
# them for /api/status. Lives on tmpfs alongside the slices.
STATS_PATH = os.environ.get(
    "DISCO_RTL433_STATS_PATH", "/run/scannerproject/disco/rtl433_stats.json"
)

# rtl_433's own stderr (version banner, warnings) is appended here rather
# than piped — keeps the subprocess from blocking on a full stderr PIPE.
STDERR_LOG_PATH = os.environ.get(
    "DISCO_RTL433_STDERR_LOG", "/run/scannerproject/disco/rtl433.stderr.log"
)

DEFAULT_TIMEOUT_S = float(os.environ.get("DISCO_RTL433_TIMEOUT_S", "5.0"))

# Fallback sample rate when the slice filename can't be parsed. rtl_433's
# common default for OOK device replay.
_FALLBACK_RATE_HZ = 250_000

# ISM allocations where rtl_433 device decoding is worth attempting. Ranges
# (not center±tol) for clarity; cover the real US/EU ISM bands.
_ISM_RANGES_HZ = (
    (314_000_000, 316_000_000),   # 315 MHz — NA garage / TPMS / sensors
    (433_000_000, 435_000_000),   # 433.92 MHz — ISM (weather, doorbells)
    (863_000_000, 870_000_000),   # 868 MHz — EU SRD
    (902_000_000, 928_000_000),   # 915 MHz — NA ISM
)

# PR #31 — "classic" ISM sub-ranges where rtl_433 runs FIRST (before the
# licensee/curated DBs). In these dedicated device bands a decoded device
# packet ("Acurite-606TX, serial 4815") is more useful than a licensee
# lookup. Subset of _ISM_RANGES_HZ, deliberately carved to EXCLUDE the
# 915–920 MHz amateur-concentration sub-band (see _AMATEUR_33CM_HZ) so
# AA0JE-style amateur ULS hits keep winning there.
CLASSIC_ISM_RANGES_HZ = (
    (314_500_000, 315_500_000),   # 315 MHz ISM (US RC)
    (432_920_000, 434_920_000),   # 433.92 MHz center ±1 MHz (RC, weather)
    (867_500_000, 868_500_000),   # 868 MHz EU SRD
    (902_000_000, 915_000_000),   # 902–915 MHz — US 902-928 below amateur
    (920_000_000, 928_000_000),   # 920–928 MHz — US 902-928 above amateur
)

# 33 cm amateur concentration. Within the US 902–928 ISM allocation amateur
# is secondary, but ham activity (repeaters, beacons, digital) clusters
# here. We never invoke rtl_433 in this window so licensee identification
# (ULS amateur) wins — a documented trade-off in docs/disco-rtl433.md.
_AMATEUR_33CM_HZ = (915_000_000, 920_000_000)

# In-process counters. Mirrored to STATS_PATH after every invocation so a
# separate reader (dashboard) sees fresh numbers.
_STATS = {
    "invocations": 0,
    "matches": 0,
    "errors": 0,
    "last_match_ts": 0.0,
    "last_match_service": "",
}


def is_available() -> bool:
    """True if the rtl_433 binary is resolvable on PATH. Never raises."""
    try:
        return shutil.which(RTL433_BIN) is not None
    except Exception:
        return False


def is_enabled() -> bool:
    """Kill switch. ``DISCO_RTL433_ENABLED=0`` (or false/no/off) disables.

    Default enabled — the layer is independently gated by ``is_available()``
    so a host without the binary is already a no-op.
    """
    raw = os.environ.get("DISCO_RTL433_ENABLED", "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def is_ism_band(freq_hz: float) -> bool:
    """True if ``freq_hz`` falls in an ISM band rtl_433 can decode."""
    try:
        f = float(freq_hz)
    except (TypeError, ValueError):
        return False
    return any(lo <= f <= hi for (lo, hi) in _ISM_RANGES_HZ)


def is_in_classic_ism(freq_hz: float) -> bool:
    """True if ``freq_hz`` is in a dedicated ISM sub-range where rtl_433
    should run FIRST (PR #31), ahead of the licensee/curated databases."""
    try:
        f = float(freq_hz)
    except (TypeError, ValueError):
        return False
    return any(lo <= f <= hi for (lo, hi) in CLASSIC_ISM_RANGES_HZ)


def is_amateur_33cm(freq_hz: float) -> bool:
    """True if ``freq_hz`` is in the 33 cm amateur-concentration window
    (915–920 MHz). rtl_433 is never invoked here so amateur ULS wins."""
    try:
        f = float(freq_hz)
    except (TypeError, ValueError):
        return False
    lo, hi = _AMATEUR_33CM_HZ
    return lo <= f < hi


def _rate_from_filename(name: str) -> Optional[int]:
    """Parse the sample rate (field 3) from a disco slice filename.

    Filename: ``{tuner}_{freq_hz}_{bw_hz}_{rate_hz}_{ts}_{uid}.iq.f32``.
    """
    try:
        base = os.path.basename(name)
        if base.endswith(".iq.f32"):
            base = base[: -len(".iq.f32")]
        parts = base.split("_")
        if len(parts) >= 4:
            return int(float(parts[3]))
    except Exception:
        pass
    return None


def _write_stats() -> None:
    """Atomically persist the counter snapshot. Never raises."""
    try:
        snapshot = dict(_STATS)
        snapshot["available"] = is_available()
        snapshot["enabled"] = is_enabled()
        snapshot["updated_ts"] = time.time()
        d = os.path.dirname(STATS_PATH)
        if d:
            os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d or None, prefix=".rtl433_stats.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(snapshot, f)
            os.replace(tmp, STATS_PATH)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
    except Exception as e:  # pragma: no cover - stats are best-effort
        LOG.debug("rtl_433 stats write failed: %s", e)


def read_stats() -> dict:
    """Read the persisted counter snapshot for /api/status. Never raises.

    Returns a dict with all status fields, falling back to live
    availability/enabled checks and zeroed counters when the file is
    missing or unreadable.
    """
    base = {
        "rtl433_available": is_available(),
        "rtl433_enabled": is_enabled(),
        "rtl433_invocations_total": 0,
        "rtl433_matches_total": 0,
        "rtl433_errors_total": 0,
        "rtl433_last_match_ts": 0.0,
        "rtl433_last_match_service": "",
    }
    try:
        with open(STATS_PATH) as f:
            s = json.load(f)
        base["rtl433_available"] = bool(s.get("available", base["rtl433_available"]))
        base["rtl433_enabled"] = bool(s.get("enabled", base["rtl433_enabled"]))
        base["rtl433_invocations_total"] = int(s.get("invocations", 0))
        base["rtl433_matches_total"] = int(s.get("matches", 0))
        base["rtl433_errors_total"] = int(s.get("errors", 0))
        base["rtl433_last_match_ts"] = float(s.get("last_match_ts", 0.0))
        base["rtl433_last_match_service"] = str(s.get("last_match_service", ""))
    except FileNotFoundError:
        pass
    except Exception as e:
        LOG.debug("rtl_433 stats read failed: %s", e)
    return base


def _format_device(packet: dict) -> str:
    """Human-readable label from a decoded rtl_433 JSON packet."""
    model = str(packet.get("model") or "Unknown device").strip()
    dev_id = packet.get("id")
    if dev_id in (None, ""):
        dev_id = packet.get("address")
    chan = packet.get("channel")
    label = model
    if dev_id not in (None, ""):
        label = f"{model} (id {dev_id})"
    if chan not in (None, ""):
        label = f"{label} ch{chan}"
    return label


def lookup_rtl433(
    slice_path,
    freq_hz: float,
    sample_rate_hz: Optional[float] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Optional[dict]:
    """Replay an IQ slice through rtl_433; return the top device match or None.

    Returns ``None`` for: kill switch off, binary missing, slice missing,
    subprocess timeout, malformed/empty output, or any error. Never raises.

    On a match returns::

        {
            "device_name": "Acurite-Tower (id 1234) ch A",
            "device_id":   "1234",
            "metadata":    {<the rtl_433 JSON packet>},
            "confidence":  0.9,
        }
    """
    _STATS["invocations"] += 1
    basename = os.path.basename(str(slice_path))
    freq_mhz = (float(freq_hz) / 1e6) if freq_hz else 0.0

    def _log(result: str) -> None:
        LOG.info("[rtl_433] freq=%.4f slice=%s result=%s", freq_mhz, basename, result)

    try:
        if not is_enabled():
            _log("skipped:disabled")
            _write_stats()
            return None
        if not is_available():
            _log("skipped:binary-missing")
            _write_stats()
            return None

        p = Path(str(slice_path))
        if not p.is_file():
            _STATS["errors"] += 1
            _log("error:slice-missing")
            _write_stats()
            return None

        rate = int(sample_rate_hz or _rate_from_filename(p.name) or _FALLBACK_RATE_HZ)
        cmd = [RTL433_BIN, "-s", str(rate), "-r", f"cf32:{p}", "-F", "json", "-A"]

        # stderr → append-mode file (never PIPE). stdout → PIPE (small,
        # line-delimited JSON; bounded by the timeout + 2048-sample slice).
        stderr_fh = None
        try:
            try:
                os.makedirs(os.path.dirname(STDERR_LOG_PATH) or ".", exist_ok=True)
                stderr_fh = open(STDERR_LOG_PATH, "ab")
            except Exception:
                stderr_fh = subprocess.DEVNULL
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=stderr_fh,
                timeout=timeout_s,
                check=False,
            )
        finally:
            if hasattr(stderr_fh, "close"):
                try:
                    stderr_fh.close()
                except Exception:
                    pass

        packets = []
        for line in (proc.stdout or b"").decode("utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                packets.append(json.loads(line))
            except Exception:
                continue

        if not packets:
            _log("no-match")
            _write_stats()
            return None

        top = packets[0]
        label = _format_device(top)
        result = {
            "device_name": label,
            "device_id": str(top.get("id") if top.get("id") not in (None, "") else top.get("address", "")),
            "metadata": top,
            "confidence": 0.9,
        }
        _STATS["matches"] += 1
        _STATS["last_match_ts"] = time.time()
        _STATS["last_match_service"] = label
        _log(f"match:{label}")
        _write_stats()
        return result

    except subprocess.TimeoutExpired:
        _STATS["errors"] += 1
        _log(f"error:timeout-{timeout_s}s")
        _write_stats()
        return None
    except Exception as e:
        _STATS["errors"] += 1
        _log(f"error:{type(e).__name__}:{e}")
        _write_stats()
        return None
