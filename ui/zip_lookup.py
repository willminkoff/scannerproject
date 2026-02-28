"""ZIP/postal lookup helpers for HP location input."""
from __future__ import annotations

import json
import re
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_US_ZIP_RE = re.compile(r"^\d{5}(?:-\d{4})?$")
_CA_POSTAL_RE = re.compile(r"^[A-Za-z]\d[A-Za-z][ -]?\d[A-Za-z]\d$")

_CACHE: dict[tuple[str, str], tuple[float, float]] = {}
_SSL_NO_VERIFY = ssl._create_unverified_context()


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
