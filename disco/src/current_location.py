"""Current scanner location, derived from SB3's HPState.

Disco runs on the same Micro as SB3 (`airband-ui`), so the cheapest way to
follow Will's Travel Mode is to read SB3's persisted state file directly.
The Travel Mode push endpoint at `POST /api/hp/location/push` mutates
`HPState.zip/lat/lon` whenever the iPhone Shortcut fires; this module
surfaces that state to Disco's `interpret.py`, `uls.py`, and `cdbs.py`
without coupling to SB3's Python code.

Reads are TTL-cached (60s default) so the classifier loop doesn't hot-spot
the filesystem. Failures (file missing, mid-write, malformed JSON) fall
back silently to the Nashville home defaults — Disco keeps working in any
scenario where SB3 isn't there or the state file is briefly inconsistent.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import NamedTuple


_DEFAULT_HP_STATE_PATH = "/home/ubuntu/scannerproject/data/hp_state.json"
_DEFAULT_CACHE_TTL_SEC = 60.0

# Home fallback. Used when the HPState file is missing, malformed, or has
# blank/invalid values for zip/lat/lon. Matches SB3's ui/config.py HOME_*.
HOME_ZIP = "37221"
HOME_LAT = 36.0662
HOME_LON = -86.9639
HOME_LABEL = "Nashville, TN"

# Coarse label table for common metros — ZIP first-3 prefix → human label.
# Used to give Claude a city name when the ZIP isn't one of Will's regulars.
# Falls back to "ZIP {zip}" for unknown prefixes. Intentionally small; this
# is for prompt context, not a geocoder.
_ZIP_PREFIX_LABELS = {
    "372": "Nashville, TN",
    "370": "Nashville, TN",
    "371": "Nashville, TN",
    "100": "New York, NY",
    "101": "New York, NY",
    "190": "Philadelphia, PA",
    "191": "Philadelphia, PA",
    "200": "Washington, DC",
    "300": "Atlanta, GA",
    "301": "Atlanta, GA",
    "303": "Atlanta, GA",
    "606": "Chicago, IL",
    "750": "Dallas, TX",
    "770": "Houston, TX",
    "900": "Los Angeles, CA",
    "941": "San Francisco, CA",
    "981": "Seattle, WA",
    "021": "Boston, MA",
    "022": "Boston, MA",
    "331": "Miami, FL",
}


class Location(NamedTuple):
    zip: str
    lat: float
    lon: float
    label: str


_HOME_LOCATION = Location(HOME_ZIP, HOME_LAT, HOME_LON, HOME_LABEL)


_CACHE_LOCK = threading.Lock()
_CACHE: dict = {
    "ts": 0.0,
    "location": _HOME_LOCATION,
    "version": 0,
}


def _label_for_zip(zip_code: str) -> str:
    """Return a human-readable city label for a 5-digit ZIP, or 'ZIP NNNNN' fallback."""
    z = str(zip_code or "").strip()
    if len(z) >= 3 and z[:3] in _ZIP_PREFIX_LABELS:
        return _ZIP_PREFIX_LABELS[z[:3]]
    if z:
        return f"ZIP {z}"
    return HOME_LABEL


def _read_hp_state(path: str) -> dict | None:
    """Read SB3's HPState JSON from disk. Returns None on any failure.

    Failure modes handled silently:
      - file missing (FileNotFoundError)
      - mid-write atomic-rename window (rare but possible — read returns
        partial bytes or empty, json.loads raises JSONDecodeError)
      - permission denied
      - any other I/O or parse error
    Caller falls back to HOME_LOCATION when this returns None.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        if not raw.strip():
            return None
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(parsed, dict):
            return None
        return parsed
    except (FileNotFoundError, PermissionError, json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None
    except Exception:
        return None


def _location_from_hp_state(payload: dict) -> Location:
    """Build a Location from a parsed HPState dict, with field-by-field fallback."""
    raw_zip = payload.get("zip") or payload.get("postal_code") or ""
    z = str(raw_zip).strip()
    # Treat empty / non-5-digit ZIPs as missing; fall back to home ZIP for that
    # field rather than failing the whole read — partial state is still useful.
    if len(z) != 5 or not z.isdigit():
        z = HOME_ZIP

    try:
        lat = float(payload.get("lat", HOME_LAT))
    except (TypeError, ValueError):
        lat = HOME_LAT
    try:
        lon = float(payload.get("lon", HOME_LON))
    except (TypeError, ValueError):
        lon = HOME_LON

    # Clamp clearly-invalid coords to home rather than passing them through to
    # spatial filters that assume sane values.
    if not (-90.0 <= lat <= 90.0):
        lat = HOME_LAT
    if not (-180.0 <= lon <= 180.0):
        lon = HOME_LON

    return Location(z, lat, lon, _label_for_zip(z))


def get_current_location(*, force_refresh: bool = False) -> Location:
    """Return the current scanner location, cached for DISCO_LOCATION_CACHE_TTL_SEC.

    Source of truth: SB3's hp_state.json (path overridable via env var
    `DISCO_HP_STATE_PATH`). Failures fall back to the Nashville home tuple.
    """
    ttl = _cache_ttl()
    now = time.monotonic()
    with _CACHE_LOCK:
        if not force_refresh and (now - _CACHE["ts"]) < ttl:
            return _CACHE["location"]

    path = os.getenv("DISCO_HP_STATE_PATH", _DEFAULT_HP_STATE_PATH)
    payload = _read_hp_state(path)
    if payload is None:
        new_location = _HOME_LOCATION
    else:
        new_location = _location_from_hp_state(payload)

    with _CACHE_LOCK:
        prior = _CACHE["location"]
        _CACHE["ts"] = now
        _CACHE["location"] = new_location
        if new_location != prior:
            _CACHE["version"] += 1
        return new_location


def get_location_version() -> int:
    """Monotonic counter; increments on each cache miss where the value changed.

    Available for callers that want to bust their own caches when location
    moves, though `get_location_bucket()` is usually sufficient since cache
    keys naturally differ across buckets.
    """
    with _CACHE_LOCK:
        return _CACHE["version"]


def get_location_bucket() -> str:
    """Coarse regional bucket suitable for inclusion in cache keys.

    Uses the ZIP's first 3 digits (Sectional Center Facility) so all ZIPs in
    the same metro share the same bucket. This is intentional: Claude
    interpretations for the same modulation class don't change meaningfully
    when Will moves across town, but they DO change when he flies to Philly.
    """
    loc = get_current_location()
    z = str(loc.zip or "").strip()
    if len(z) >= 3 and z[:3].isdigit():
        return z[:3]
    return HOME_ZIP[:3]


def reset_cache_for_tests() -> None:
    """Force the next get_current_location() to re-read from disk.

    Test helper only. Not called from production code. Uses a large-negative
    timestamp so the cache-check `now - ts < ttl` evaluates False even when
    monotonic clock values are small (e.g., shortly after process start on
    some platforms — Linux can return values near 0).
    """
    with _CACHE_LOCK:
        _CACHE["ts"] = float("-inf")
        _CACHE["location"] = _HOME_LOCATION
        _CACHE["version"] = 0


def _cache_ttl() -> float:
    """Cache TTL in seconds. Env var DISCO_LOCATION_CACHE_TTL_SEC overrides."""
    raw = os.getenv("DISCO_LOCATION_CACHE_TTL_SEC")
    if raw is None:
        return _DEFAULT_CACHE_TTL_SEC
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return _DEFAULT_CACHE_TTL_SEC
