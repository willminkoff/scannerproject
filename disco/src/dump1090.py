"""disco/src/dump1090.py — dump1090 specialist decoder for ADS-B (1090 MHz).

When disco's sweep captures activity at 1090 MHz (ADS-B), this module
replays the IQ slice through dump1090 in static-file decode mode to extract
aircraft identification: ICAO 24-bit hex address, flight/callsign,
altitude, and (when present) speed/position. Result is labelled
``id_source="dump1090"`` at high confidence.

Mirrors disco/src/rtl433.py's do-no-harm contract exactly:
  - NEVER raises into the caller — every path returns a safe default.
  - Single env-var kill switch: ``DISCO_DUMP1090_ENABLED=0``. Default
    enabled, independently gated by ``is_available()``.
  - Every invocation logged: ``[dump1090] freq=<MHz> slice=<base>
    result=<match|no-match|error:…>``.
  - Counters persisted to a JSON stats file for /api/status.
  - subprocess stderr → append-mode FILE, never PIPE. stdout PIPE bounded
    by a short timeout + tiny slice.

Format / bandwidth caveat (documented in docs/disco-dump1090.md): ADS-B is
a 2 Mbit/s Manchester signal that decoders sample at ~2.4 MHz, and dump1090
expects 8-bit/16-bit complex IQ. disco's slices are complex float32
decimated to ~50 kHz — far too narrow to carry an ADS-B frame. The
invocation here follows the requested shape; until a wideband 1090 MHz
capture path exists, decodes will be rare/absent and lookup_dump1090()
returns None (fail-open). This is the known limitation that would justify a
dedicated wideband ADS-B feed.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("disco.dump1090")

DUMP1090_BIN = os.environ.get("DISCO_DUMP1090_BIN", "dump1090")

STATS_PATH = os.environ.get(
    "DISCO_DUMP1090_STATS_PATH", "/run/scannerproject/disco/dump1090_stats.json"
)
STDERR_LOG_PATH = os.environ.get(
    "DISCO_DUMP1090_STDERR_LOG", "/run/scannerproject/disco/dump1090.stderr.log"
)
DEFAULT_TIMEOUT_S = float(os.environ.get("DISCO_DUMP1090_TIMEOUT_S", "5.0"))

# ADS-B downlink. Tolerance ±1 MHz around 1090.
ADSB_FREQ_HZ = 1_090_000_000
ADSB_TOL_HZ = 1_000_000

_STATS = {
    "invocations": 0,
    "matches": 0,
    "errors": 0,
    "last_match_ts": 0.0,
    "last_match_icao": "",
}

_ICAO_RE = re.compile(r"ICAO Address:\s*([0-9a-fA-F]{6})")
_FLIGHT_RE = re.compile(r"(?:Ident|Flight|Callsign)\s*:?\s*([A-Z0-9]{2,8})")
_ALT_RE = re.compile(r"Altitude:\s*(-?\d+)")


def is_available() -> bool:
    """True if the dump1090 binary is on PATH. Never raises."""
    try:
        return shutil.which(DUMP1090_BIN) is not None
    except Exception:
        return False


def is_enabled() -> bool:
    """Kill switch. ``DISCO_DUMP1090_ENABLED=0`` (or false/no/off) disables."""
    raw = os.environ.get("DISCO_DUMP1090_ENABLED", "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def is_adsb_band(freq_hz: float) -> bool:
    """True if ``freq_hz`` is within ±1 MHz of the 1090 MHz ADS-B downlink."""
    try:
        f = float(freq_hz)
    except (TypeError, ValueError):
        return False
    return abs(f - ADSB_FREQ_HZ) <= ADSB_TOL_HZ


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
        fd, tmp = tempfile.mkstemp(dir=d or None, prefix=".dump1090_stats.", suffix=".tmp")
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
        LOG.debug("dump1090 stats write failed: %s", e)


def read_stats() -> dict:
    """Read the persisted counter snapshot for /api/status. Never raises."""
    base = {
        "dump1090_available": is_available(),
        "dump1090_enabled": is_enabled(),
        "dump1090_invocations_total": 0,
        "dump1090_matches_total": 0,
        "dump1090_errors_total": 0,
        "dump1090_last_match_icao": "",
        "dump1090_last_match_ts": 0.0,
    }
    try:
        with open(STATS_PATH) as f:
            s = json.load(f)
        base["dump1090_available"] = bool(s.get("available", base["dump1090_available"]))
        base["dump1090_enabled"] = bool(s.get("enabled", base["dump1090_enabled"]))
        base["dump1090_invocations_total"] = int(s.get("invocations", 0))
        base["dump1090_matches_total"] = int(s.get("matches", 0))
        base["dump1090_errors_total"] = int(s.get("errors", 0))
        base["dump1090_last_match_icao"] = str(s.get("last_match_icao", ""))
        base["dump1090_last_match_ts"] = float(s.get("last_match_ts", 0.0))
    except FileNotFoundError:
        pass
    except Exception as e:
        LOG.debug("dump1090 stats read failed: %s", e)
    return base


def _parse_aircraft(stdout: str) -> Optional[dict]:
    """Extract the first aircraft (ICAO + optional flight/altitude) decoded."""
    icao = None
    flight = None
    altitude = None
    for line in stdout.splitlines():
        if icao is None:
            m = _ICAO_RE.search(line)
            if m:
                icao = m.group(1).lower()
        if flight is None:
            m = _FLIGHT_RE.search(line)
            if m:
                flight = m.group(1)
        if altitude is None:
            m = _ALT_RE.search(line)
            if m:
                altitude = m.group(1)
    if not icao:
        return None
    return {"icao": icao, "flight": flight, "altitude": altitude}


def _format_aircraft(ac: dict) -> str:
    icao = ac.get("icao", "?")
    label = f"Aircraft ICAO {icao}"
    flight = (ac.get("flight") or "").strip()
    if flight:
        label = f"{label} ({flight})"
    alt = ac.get("altitude")
    if alt not in (None, ""):
        label = f"{label} @ {alt} ft"
    return label


def lookup_dump1090(
    slice_path,
    freq_hz: float,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Optional[dict]:
    """Decode an ADS-B slice via dump1090; return the top aircraft or None.

    Returns ``None`` for: kill switch off, binary missing, slice missing,
    subprocess timeout, no decode, or any error. Never raises.

    On a decode returns::

        {"device_name": "Aircraft ICAO 4840d6 (KLM123) @ 35000 ft",
         "icao": "4840d6", "flight": "KLM123", "altitude": "35000",
         "confidence": 0.95}
    """
    _STATS["invocations"] += 1
    basename = os.path.basename(str(slice_path))
    freq_mhz = (float(freq_hz) / 1e6) if freq_hz else 0.0

    def _log(result: str) -> None:
        LOG.info("[dump1090] freq=%.4f slice=%s result=%s", freq_mhz, basename, result)

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

        # Static-file decode. --ifile reads the slice; --iformat declares the
        # sample layout (disco slices are interleaved complex float32). The
        # exact flags vary by dump1090 fork (mutability / fa) — DISCO_DUMP1090_BIN
        # plus DISCO_DUMP1090_ARGS can override if a fork needs different ones.
        extra = os.environ.get("DISCO_DUMP1090_ARGS", "").split()
        cmd = [DUMP1090_BIN, "--ifile", str(p), "--iformat", "SC16",
               "--quiet", "--no-interactive"] + extra

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

        stdout = (proc.stdout or b"").decode("utf-8", errors="ignore")
        ac = _parse_aircraft(stdout)
        if not ac:
            _log("no-match")
            _write_stats()
            return None

        label = _format_aircraft(ac)
        result = {
            "device_name": label,
            "icao": ac.get("icao"),
            "flight": ac.get("flight"),
            "altitude": ac.get("altitude"),
            "confidence": 0.95,
        }
        _STATS["matches"] += 1
        _STATS["last_match_ts"] = time.time()
        _STATS["last_match_icao"] = str(ac.get("icao") or "")
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
