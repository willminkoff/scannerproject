"""disco/src/multimon.py — multimon-ng specialist decoder for paging.

When disco detects a narrowband FSK signal in a paging allocation
(152.84 / 158.7 / 450–454 / 929–932 MHz), this module pipes the captured
IQ slice through multimon-ng to decode POCSAG / FLEX page content. A
decoded page carries a capcode (recipient ID) and, often, message text —
the "hospital pager / utility SCADA / restaurant buzzer?" answer for the
paging band.

Mirrors disco/src/rtl433.py's contract exactly (do-no-harm):
  - NEVER raises into the caller — every path returns a safe default.
  - Single env-var kill switch: ``DISCO_MULTIMON_ENABLED=0``. Default
    enabled, independently gated by ``is_available()``.
  - Every invocation logged: ``[multimon] freq=<MHz> slice=<base>
    result=<match|no-match|error:…>``.
  - Counters persisted to a JSON stats file the dashboard reads for
    /api/status.
  - subprocess stderr → append-mode FILE, never PIPE (the unbounded-stderr
    MemoryError pattern we deliberately avoid). stdout PIPE bounded by a
    short timeout + tiny slice.

Format caveat (documented in docs/disco-multimon.md): multimon-ng expects
*demodulated audio* (raw 16-bit signed, ~22050 Hz), not complex IQ. disco
slices are complex float32. The invocation here follows the requested
command shape; until an FM-demod pre-stage is added, many slices will not
decode and lookup_multimon() returns None (fail-open). This is the known
limitation that would justify a dedicated audio-feed path, analogous to
rtl_433's slice sample-rate gap.
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

LOG = logging.getLogger("disco.multimon")

MULTIMON_BIN = os.environ.get("DISCO_MULTIMON_BIN", "multimon-ng")

STATS_PATH = os.environ.get(
    "DISCO_MULTIMON_STATS_PATH", "/run/scannerproject/disco/multimon_stats.json"
)
STDERR_LOG_PATH = os.environ.get(
    "DISCO_MULTIMON_STDERR_LOG", "/run/scannerproject/disco/multimon.stderr.log"
)
DEFAULT_TIMEOUT_S = float(os.environ.get("DISCO_MULTIMON_TIMEOUT_S", "5.0"))

# Paging allocations where POCSAG / FLEX decoding is worth attempting.
PAGING_RANGES_HZ = (
    (152_700_000, 153_000_000),   # 152.84 MHz VHF paging
    (158_600_000, 158_800_000),   # 158.7 MHz VHF paging
    (450_000_000, 454_000_000),   # 450–454 MHz UHF paging
    (929_000_000, 932_000_000),   # 929–932 MHz paging
)

# Decoders to try. POCSAG at the three common baud rates + FLEX.
_DECODERS = ("POCSAG512", "POCSAG1200", "POCSAG2400", "FLEX_NEXT")

_STATS = {
    "invocations": 0,
    "matches": 0,
    "errors": 0,
    "last_match_ts": 0.0,
    "last_match_capcode": "",
}

# multimon-ng line, e.g.:
#   POCSAG1200: Address:  1234567  Function: 0  Alpha:   HELLO WORLD
#   FLEX: 2009-... 1600/2/K 12.34  [001234567] ALN  message text
_POCSAG_RE = re.compile(
    r"POCSAG\d+:\s*Address:\s*(?P<capcode>\d+).*?"
    r"(?:Alpha:\s*(?P<alpha>.*)|Numeric:\s*(?P<numeric>.*))?$"
)
_FLEX_RE = re.compile(r"FLEX[:|].*?\[(?P<capcode>\d+)\]\s*\w*\s*(?P<msg>.*)$")


def is_available() -> bool:
    """True if the multimon-ng binary is on PATH. Never raises."""
    try:
        return shutil.which(MULTIMON_BIN) is not None
    except Exception:
        return False


def is_enabled() -> bool:
    """Kill switch. ``DISCO_MULTIMON_ENABLED=0`` (or false/no/off) disables."""
    raw = os.environ.get("DISCO_MULTIMON_ENABLED", "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def is_paging_band(freq_hz: float) -> bool:
    """True if ``freq_hz`` falls in a paging allocation multimon-ng covers."""
    try:
        f = float(freq_hz)
    except (TypeError, ValueError):
        return False
    return any(lo <= f <= hi for (lo, hi) in PAGING_RANGES_HZ)


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
        fd, tmp = tempfile.mkstemp(dir=d or None, prefix=".multimon_stats.", suffix=".tmp")
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
        LOG.debug("multimon stats write failed: %s", e)


def read_stats() -> dict:
    """Read the persisted counter snapshot for /api/status. Never raises."""
    base = {
        "multimon_available": is_available(),
        "multimon_enabled": is_enabled(),
        "multimon_invocations_total": 0,
        "multimon_matches_total": 0,
        "multimon_errors_total": 0,
        "multimon_last_match_capcode": "",
        "multimon_last_match_ts": 0.0,
    }
    try:
        with open(STATS_PATH) as f:
            s = json.load(f)
        base["multimon_available"] = bool(s.get("available", base["multimon_available"]))
        base["multimon_enabled"] = bool(s.get("enabled", base["multimon_enabled"]))
        base["multimon_invocations_total"] = int(s.get("invocations", 0))
        base["multimon_matches_total"] = int(s.get("matches", 0))
        base["multimon_errors_total"] = int(s.get("errors", 0))
        base["multimon_last_match_capcode"] = str(s.get("last_match_capcode", ""))
        base["multimon_last_match_ts"] = float(s.get("last_match_ts", 0.0))
    except FileNotFoundError:
        pass
    except Exception as e:
        LOG.debug("multimon stats read failed: %s", e)
    return base


def _parse_page(stdout: str) -> Optional[dict]:
    """Extract the first POCSAG/FLEX page (capcode + message) from output."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("POCSAG"):
            m = _POCSAG_RE.search(line)
            if m and m.group("capcode"):
                msg = (m.group("alpha") or m.group("numeric") or "").strip()
                proto = line.split(":", 1)[0]
                return {"protocol": proto, "capcode": m.group("capcode"), "message": msg}
        elif line.startswith("FLEX"):
            m = _FLEX_RE.search(line)
            if m and m.group("capcode"):
                return {"protocol": "FLEX", "capcode": m.group("capcode"),
                        "message": (m.group("msg") or "").strip()}
    return None


def _format_page(page: dict) -> str:
    cap = page.get("capcode", "?")
    proto = page.get("protocol", "Page")
    msg = (page.get("message") or "").strip()
    label = f"{proto} capcode {cap}"
    if msg:
        snippet = msg if len(msg) <= 48 else msg[:47] + "…"
        label = f"{label}: {snippet}"
    return label


def lookup_multimon(
    slice_path,
    freq_hz: float,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Optional[dict]:
    """Decode a paging slice via multimon-ng; return the top page or None.

    Returns ``None`` for: kill switch off, binary missing, slice missing,
    subprocess timeout, no decode, or any error. Never raises.

    On a decode returns::

        {"device_name": "POCSAG1200 capcode 1234567: HELLO",
         "protocol": "POCSAG1200", "capcode": "1234567",
         "message": "HELLO", "confidence": 0.85}
    """
    _STATS["invocations"] += 1
    basename = os.path.basename(str(slice_path))
    freq_mhz = (float(freq_hz) / 1e6) if freq_hz else 0.0

    def _log(result: str) -> None:
        LOG.info("[multimon] freq=%.4f slice=%s result=%s", freq_mhz, basename, result)

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

        cmd = [MULTIMON_BIN]
        for dec in _DECODERS:
            cmd += ["-a", dec]
        cmd += ["-f", "alpha", str(p)]

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
        page = _parse_page(stdout)
        if not page:
            _log("no-match")
            _write_stats()
            return None

        label = _format_page(page)
        result = {
            "device_name": label,
            "protocol": page.get("protocol"),
            "capcode": page.get("capcode"),
            "message": page.get("message"),
            "confidence": 0.85,
        }
        _STATS["matches"] += 1
        _STATS["last_match_ts"] = time.time()
        _STATS["last_match_capcode"] = str(page.get("capcode") or "")
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
