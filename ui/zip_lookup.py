"""ZIP/postal lookup helpers for HP location input."""
from __future__ import annotations

import json
import math
import os
import re
import ssl
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_US_ZIP_RE = re.compile(r"^\d{5}(?:-\d{4})?$")
_CA_POSTAL_RE = re.compile(r"^[A-Za-z]\d[A-Za-z][ -]?\d[A-Za-z]\d$")

_CACHE: dict[tuple[str, str], tuple[float, float]] = {}
_SSL_NO_VERIFY = ssl._create_unverified_context()

# PR #35 — reverse-lookup index for Owntracks Travel Mode. Path can be
# overridden via env for tests / alternate deployments.
_REVERSE_INDEX_PATH = os.environ.get(
    "HP_US_ZIP_LAT_LON_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "us_zip_lat_lon.json"),
)
_REVERSE_INDEX_CACHE: Optional[list[tuple[str, float, float]]] = None
_REVERSE_INDEX_PATH_USED: Optional[str] = None


def _normalize_postal(code: str, country: str) -> str:
    raw = str(code or "").strip()
    cc = str(country or "US").strip().upper()
    if cc == "US":
        if not _US_ZIP_RE.match(raw):
            return ""
        return raw.split("-", 1)[0]
    if cc == "CA":
        if not _CA_POSTAL_RE.match(raw):
            return ""
        return raw.replace(" ", "").upper()
    return ""


def resolve_postal_to_lat_lon(
    postal_code: str,
    country_code: str = "US",
    timeout_sec: float = 3.0,
) -> tuple[float, float] | None:
    """Resolve ZIP/postal code to (lat, lon) via zippopotam.us."""
    cc = str(country_code or "US").strip().upper()
    normalized = _normalize_postal(postal_code, cc)
    if not normalized:
        return None

    key = (cc, normalized)
    cached = _CACHE.get(key)
    if cached:
        return cached

    urls = [
        f"https://api.zippopotam.us/{cc.lower()}/{normalized}",
        f"http://api.zippopotam.us/{cc.lower()}/{normalized}",
    ]
    body = ""
    req_headers = {"User-Agent": "scannerproject-hp3/1.0"}
    for url in urls:
        req = Request(url, headers=req_headers)
        try:
            with urlopen(req, timeout=float(timeout_sec)) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                if body.strip():
                    break
        except URLError as exc:
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                try:
                    with urlopen(req, timeout=float(timeout_sec), context=_SSL_NO_VERIFY) as resp:
                        body = resp.read().decode("utf-8", errors="replace")
                        if body.strip():
                            break
                except (HTTPError, URLError, TimeoutError, ValueError):
                    pass
        except (HTTPError, TimeoutError, ValueError):
            pass
    if not body.strip():
        return None

    try:
        payload = json.loads(body)
    except Exception:
        return None

    places = payload.get("places")
    if not isinstance(places, list) or not places:
        return None
    first = places[0] if isinstance(places[0], dict) else {}
    try:
        lat = float(str(first.get("latitude") or "").strip())
        lon = float(str(first.get("longitude") or "").strip())
    except Exception:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    out = (lat, lon)
    _CACHE[key] = out
    return out


def _load_reverse_index(path: Optional[str] = None) -> list[tuple[str, float, float]]:
    """Load the (zip, lat, lon) reverse-lookup index from disk, cached.

    Reads from ``HP_US_ZIP_LAT_LON_PATH`` (or the bundled ui/data file by
    default). Returns an empty list when the file is missing or malformed
    — every caller defends with an empty-string fallback so missing data
    doesn't break the request path. PR #35.
    """
    global _REVERSE_INDEX_CACHE, _REVERSE_INDEX_PATH_USED
    target = path or _REVERSE_INDEX_PATH
    if _REVERSE_INDEX_CACHE is not None and _REVERSE_INDEX_PATH_USED == target:
        return _REVERSE_INDEX_CACHE
    try:
        with open(target) as f:
            data = json.load(f)
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            _REVERSE_INDEX_CACHE = []
        else:
            out: list[tuple[str, float, float]] = []
            for row in entries:
                if isinstance(row, list) and len(row) >= 3:
                    try:
                        z = str(row[0]).strip().zfill(5)
                        la = float(row[1])
                        lo = float(row[2])
                    except (TypeError, ValueError):
                        continue
                    out.append((z, la, lo))
            _REVERSE_INDEX_CACHE = out
    except FileNotFoundError:
        _REVERSE_INDEX_CACHE = []
    except Exception:
        _REVERSE_INDEX_CACHE = []
    _REVERSE_INDEX_PATH_USED = target
    return _REVERSE_INDEX_CACHE


def reset_reverse_index_cache() -> None:
    """Test hook: drop the cached index so the next lookup re-reads disk."""
    global _REVERSE_INDEX_CACHE, _REVERSE_INDEX_PATH_USED
    _REVERSE_INDEX_CACHE = None
    _REVERSE_INDEX_PATH_USED = None


def nearest_zip(lat: float, lon: float, *, index_path: Optional[str] = None) -> str:
    """Return the US ZIP code whose centroid is closest to ``(lat, lon)``.

    Returns ``""`` when the index is missing/empty or when the inputs are
    out of range. The lookup is a brute-force scan over the ~33k ZIP
    centroids in the Census ZCTA Gazetteer (under 10 ms on modern CPUs —
    well within the iPhone push budget).

    Distance uses an equirectangular approximation: ZIP centroids are
    coarse to start with, and the closest-centroid choice is what matters
    — not the metric distance. This avoids the haversine cost on every
    one of 33k rows. PR #35.
    """
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return ""
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
        return ""

    index = _load_reverse_index(index_path)
    if not index:
        return ""

    cos_lat = math.cos(math.radians(lat_f))
    best_d2 = float("inf")
    best_zip = ""
    for (zc, la, lo) in index:
        dlat = la - lat_f
        dlon = (lo - lon_f) * cos_lat
        d2 = dlat * dlat + dlon * dlon
        if d2 < best_d2:
            best_d2 = d2
            best_zip = zc
    return best_zip
