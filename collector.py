"""
Nashville Severe Weather Broadcast Engine - Data Collector
Polls NWS/SPC APIs and caches data as JSON for the renderer.
"""

import json
import logging
import os
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR = Path(r"C:\nsw-broadcast")
CACHE_DIR = BASE_DIR / "cache"
LOG_DIR = BASE_DIR / "logs"

CACHE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "collector.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("collector")

USER_AGENT = "NashvilleSevereWeatherBroadcast/1.0 (broadcast@nashvilleweather.local)"

# ─── Forecast anchor point ────────────────────────────────────────────────────
# /points/{lat,lon} resolves to a single ~2.5km NWS gridpoint. Both
# forecast_hourly (12-hr broadcast page) and forecast_daily pull from this
# single cell. Resolved once at startup and cached to disk so we don't hit
# /points every run. Override with NSW_BROADCAST_FORECAST_LATLON="lat,lon"
# to revert without a code edit.
FORECAST_LAT = 36.0331   # Brentwood, TN
FORECAST_LON = -86.7828
GRIDPOINT_CACHE = CACHE_DIR / "gridpoint.json"
GRIDPOINT_CACHE_MAX_AGE_DAYS = 30
# Last-resort fallback if /points is unreachable AND no usable cache exists.
# Matches the pre-Brentwood hardcoded grid so the broadcast keeps rendering.
GRIDPOINT_FALLBACK = ("OHX", 53, 77)


def _forecast_grid_url(office: str, gx: int, gy: int, kind: str) -> str:
    return f"https://api.weather.gov/gridpoints/{office}/{gx},{gy}/{kind}"


# ENDPOINTS URLs are seeded with the fallback grid so module import is
# always safe. resolve_forecast_grid() rewrites them at the top of main().
ENDPOINTS = {
    "forecast_hourly": {
        "url": _forecast_grid_url(*GRIDPOINT_FALLBACK, "forecast/hourly"),
        "cache": CACHE_DIR / "forecast_hourly.json",
        "interval": 900,
    },
    "forecast_daily": {
        "url": _forecast_grid_url(*GRIDPOINT_FALLBACK, "forecast"),
        "cache": CACHE_DIR / "forecast_daily.json",
        "interval": 900,
    },
}

# Alerts: NWS JSON primary + IEM mirror as truly-independent fallback infra.
# Both feeds are folded into a deduplicated set (VTEC string is the canonical
# key; falls back to alert id). NWS shape is canonical for the cache so the
# renderer/api consume both sources transparently.
#
# IEM endpoint: /api/1/vtec/events_status.json filtered by wfo=OHX (Nashville
# Old Hickory WFO). The UGC param is ignored by this endpoint, so we filter
# the returned rows in code by matching "Davidson" or "Williamson" in the
# locations text. (NWS JSON tier filters tightly to TNC037,TNC187 via URL.)
ALERTS_NWS_URL       = "https://api.weather.gov/alerts/active?zone=TNC037,TNC187"
ALERTS_IEM_URL       = "https://mesonet.agron.iastate.edu/api/1/vtec/events_status.json?wfo=OHX"
ALERTS_IEM_COUNTIES  = ("Davidson", "Williamson")
ALERTS_CACHE         = CACHE_DIR / "alerts.json"
ALERTS_INTERVAL      = 60

# SPC GeoJSON categorical outlook — easier than HTML parsing
SPC_OUTLOOK_URL = "https://www.spc.noaa.gov/products/outlook/day1otlk_cat.nolyr.geojson"
SPC_CACHE = CACHE_DIR / "spc.json"
SPC_INTERVAL = 1800

# GOES-19 GEOCOLOR — Southern Mississippi Valley sector
SATELLITE_URL      = "https://cdn.star.nesdis.noaa.gov/GOES19/ABI/SECTOR/smv/GEOCOLOR/latest.jpg"
SATELLITE_CACHE    = CACHE_DIR / "satellite.jpg"
SATELLITE_INTERVAL = 300   # 5 minutes

# EPA hourly UV index — Nashville ZIP 37203
EPA_UV_URL    = "https://data.epa.gov/efservice/getEnvirofactsUVHOURLY/ZIP/37203/JSON"
UV_CACHE      = CACHE_DIR / "uv.json"
UV_INTERVAL   = 1800       # 30 minutes

# Conditions: NWS 5-minute ASOS observations (vs hourly METAR /latest endpoint).
# /observations?limit=1 returns a FeatureCollection where features[0] is the
# most recent 5-min ob; /observations/latest only returns hourly METAR/SPECI.
CONDITIONS_URL      = "https://api.weather.gov/stations/KBNA/observations?limit=1"
CONDITIONS_CACHE    = CACHE_DIR / "conditions.json"
CONDITIONS_INTERVAL = 60

# Fallback: Iowa Environmental Mesonet (IEM) re-publishes METARs independently
# of NWS api.weather.gov. When NWS goes hourly (db backlog / API stress), IEM
# usually still has fresh METAR. We compare timestamps and use whichever is
# fresher; IEM fields are mapped into the NWS feature shape so downstream
# (api.py + renderer.py) is unchanged.
IEM_CONDITIONS_URL  = "https://mesonet.agron.iastate.edu/json/current.py?station=BNA&network=TN_ASOS"
NWS_STALE_THRESHOLD_MIN = 15

# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC-SOURCE-ONLY: WeatherDash sounding feed.
#
# This poller MUST only ingest from publicly accessible HTTPS endpoints
# (OU SHARP today, MADIS public-anonymous HTTPS when added). DO NOT add
# the scanner project's upper-air feeds to this path — acarsdec /
# dumpvdl2 raw output (wxdata.MetStore on Micro, /run/acars_output.json,
# /run/vdl2_output.json) lives in a separate subsystem and must stay
# isolated from the WeatherDash sounding pipeline.
# ─────────────────────────────────────────────────────────────────────────────
# OU SHARP ACARS sounding — hourly when traffic, sometimes every 10-30 min.
# BNA: active poll window 10 UTC – 05 UTC (5 AM – midnight CT) since BNA
# traffic is sparse overnight. ATL: 24/7 because Atlanta is dense round-clock.
ACARS_SOUND_BASE   = "https://sharp.weather.ou.edu/soundings/acars"
ACARS_BNA_CACHE    = CACHE_DIR / "bna_sounding.json"
ACARS_ATL_CACHE    = CACHE_DIR / "atl_sounding.json"
ACARS_SOUND_INTERVAL = 900   # 15 minutes

NASHVILLE_LAT = 36.16
NASHVILLE_LON = -86.78


def fetch_json(url: str, retries: int = 3, backoff: float = 5.0) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            log.warning("HTTP %s fetching %s (attempt %d/%d)", e.code, url, attempt, retries)
        except urllib.error.URLError as e:
            log.warning("URL error fetching %s: %s (attempt %d/%d)", url, e.reason, attempt, retries)
        except Exception as e:
            log.warning("Error fetching %s: %s (attempt %d/%d)", url, e, attempt, retries)
        if attempt < retries:
            time.sleep(backoff * attempt)
    return None


def fetch_text(url: str, retries: int = 2, backoff: float = 3.0) -> str | None:
    """Fetch a URL and decode as UTF-8. Returns None on persistent failure."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            log.warning("HTTP %s fetching %s (attempt %d/%d)", e.code, url, attempt, retries)
        except Exception as e:
            log.warning("Error fetching %s: %s (attempt %d/%d)", url, e, attempt, retries)
        if attempt < retries:
            time.sleep(backoff * attempt)
    return None


def fetch_bytes(url: str, retries: int = 3, backoff: float = 5.0) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except Exception as e:
            log.warning("Error fetching %s: %s (attempt %d/%d)", url, e, attempt, retries)
        if attempt < retries:
            time.sleep(backoff * attempt)
    return None


def write_cache(path: Path, data: dict) -> None:
    data["_cached_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def write_cache_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _env_latlon_override() -> tuple[float, float] | None:
    raw = os.environ.get("NSW_BROADCAST_FORECAST_LATLON", "").strip()
    if not raw:
        return None
    try:
        a, b = raw.split(",", 1)
        return float(a.strip()), float(b.strip())
    except Exception:
        log.warning("Ignoring malformed NSW_BROADCAST_FORECAST_LATLON=%r", raw)
        return None


def _read_gridpoint_cache(lat: float, lon: float) -> tuple[str, int, int] | None:
    """Return (office, gx, gy) if cache is fresh AND matches lat/lon, else None."""
    try:
        if not GRIDPOINT_CACHE.exists():
            return None
        d = json.loads(GRIDPOINT_CACHE.read_text(encoding="utf-8"))
        if abs(float(d.get("lat", 0)) - lat) > 1e-4 or abs(float(d.get("lon", 0)) - lon) > 1e-4:
            return None
        cached_at = datetime.fromisoformat(d["cached_at"])
        age = datetime.now(timezone.utc) - cached_at
        if age > timedelta(days=GRIDPOINT_CACHE_MAX_AGE_DAYS):
            return None
        return str(d["office"]), int(d["gx"]), int(d["gy"])
    except Exception as e:
        log.warning("Gridpoint cache unreadable: %s", e)
        return None


def _write_gridpoint_cache(lat: float, lon: float, office: str, gx: int, gy: int) -> None:
    try:
        payload = {
            "lat": lat, "lon": lon,
            "office": office, "gx": gx, "gy": gy,
            "cached_at": datetime.now(timezone.utc).isoformat(),
        }
        GRIDPOINT_CACHE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("Failed to write gridpoint cache: %s", e)


def resolve_forecast_grid() -> tuple[str, int, int]:
    """Return (office, gridX, gridY) for the forecast anchor point.

    Resolution order:
      1. NSW_BROADCAST_FORECAST_LATLON env override (else FORECAST_LAT/LON)
      2. Fresh disk cache matching the chosen lat/lon
      3. Live /points lookup; write cache on success
      4. Last-known cache (even if stale or lat/lon-mismatched)
      5. GRIDPOINT_FALLBACK hardcoded constant

    Never raises. Always returns a usable triple so the broadcast keeps
    rendering even if NWS /points is completely unreachable.
    """
    override = _env_latlon_override()
    lat, lon = override if override else (FORECAST_LAT, FORECAST_LON)

    cached = _read_gridpoint_cache(lat, lon)
    if cached is not None:
        log.info("Forecast gridpoint from cache: %s/%d,%d (lat=%.4f lon=%.4f)",
                 cached[0], cached[1], cached[2], lat, lon)
        return cached

    url = f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
    data = fetch_json(url)
    if data is not None:
        try:
            p = data["properties"]
            office = str(p["gridId"])
            gx = int(p["gridX"])
            gy = int(p["gridY"])
            _write_gridpoint_cache(lat, lon, office, gx, gy)
            log.info("Forecast gridpoint resolved: %s/%d,%d (lat=%.4f lon=%.4f)",
                     office, gx, gy, lat, lon)
            return office, gx, gy
        except (KeyError, ValueError, TypeError) as e:
            log.warning("Malformed /points response: %s", e)

    try:
        if GRIDPOINT_CACHE.exists():
            d = json.loads(GRIDPOINT_CACHE.read_text(encoding="utf-8"))
            office = str(d["office"]); gx = int(d["gx"]); gy = int(d["gy"])
            log.warning("Using last-known forecast gridpoint cache: %s/%d,%d "
                        "(may be stale or for different lat/lon)", office, gx, gy)
            return office, gx, gy
    except Exception:
        pass

    log.error("Forecast gridpoint unresolved; falling back to hardcoded %s/%d,%d",
              *GRIDPOINT_FALLBACK)
    return GRIDPOINT_FALLBACK


def apply_forecast_grid_to_endpoints() -> None:
    office, gx, gy = resolve_forecast_grid()
    ENDPOINTS["forecast_hourly"]["url"] = _forecast_grid_url(office, gx, gy, "forecast/hourly")
    ENDPOINTS["forecast_daily"]["url"] = _forecast_grid_url(office, gx, gy, "forecast")


def point_in_polygon(lon: float, lat: float, polygon: list) -> bool:
    """Ray-casting point-in-polygon test."""
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


SPC_TIER = {
    "No Severe Weather Risk": 0,
    "General Thunderstorms Risk": 1,
    "Marginal Risk": 2,
    "Slight Risk": 3,
    "Enhanced Risk": 4,
    "Moderate Risk": 5,
    "High Risk": 6,
}

# Short-form fallback (SPC GeoJSON LABEL field). Used if LABEL2 is empty —
# falls back to LABEL so a missing long-form value doesn't silently demote
# a high-tier polygon to rank 0. Long-form name returned in display.
SPC_TIER_SHORT = {
    "TSTM": (1, "General Thunderstorms Risk"),
    "MRGL": (2, "Marginal Risk"),
    "SLGT": (3, "Slight Risk"),
    "ENH":  (4, "Enhanced Risk"),
    "MDT":  (5, "Moderate Risk"),
    "HIGH": (6, "High Risk"),
}


def _spc_rank_label(raw_label: str) -> tuple:
    """Resolve a SPC feature label to (rank, display_label). Tries long form
    first, then short form. Returns (0, raw_label) on no match — never
    silently rounds up."""
    if not raw_label:
        return (0, "No Severe Weather Risk")
    if raw_label in SPC_TIER:
        return (SPC_TIER[raw_label], raw_label)
    short = raw_label.strip().upper().split()[0] if raw_label.strip() else ""
    if short in SPC_TIER_SHORT:
        return SPC_TIER_SHORT[short]
    return (0, raw_label)


def extract_spc_risk(geojson: dict) -> str:
    """Return the highest-tier SPC categorical risk whose polygon contains
    Nashville. SPC orders features least-severe first, so naive first-match
    returns the broadest (least severe) polygon. Scan all polygons and pick
    the highest-rank label. Logs matched labels + choice for audit."""
    best = "No Severe Weather Risk"
    best_rank = 0
    matched = []
    try:
        for feature in geojson.get("features", []):
            label = feature.get("properties", {}).get("LABEL2", "") or feature.get("properties", {}).get("LABEL", "")
            geometry = feature.get("geometry", {})
            geo_type = geometry.get("type", "")
            coords_list = geometry.get("coordinates", [])

            polys = []
            if geo_type == "Polygon":
                polys = [coords_list[0]]
            elif geo_type == "MultiPolygon":
                for part in coords_list:
                    polys.append(part[0])

            for poly in polys:
                if point_in_polygon(NASHVILLE_LON, NASHVILLE_LAT, poly):
                    rank, display = _spc_rank_label(label)
                    matched.append(display)
                    if rank > best_rank:
                        best_rank = rank
                        best = display
                    break
    except Exception as e:
        log.warning("SPC parse error: %s", e)
    log.info("SPC matched: %s -> chose %s", matched, best)
    return best


def poll_nws(name: str, cfg: dict) -> None:
    log.info("Polling %s", name)
    data = fetch_json(cfg["url"])
    if data:
        write_cache(cfg["cache"], data)
        log.info("Cached %s (%d bytes)", name, cfg["cache"].stat().st_size)
    elif cfg["cache"].exists():
        log.warning("Using stale cache for %s", name)
    else:
        log.error("No data and no cache for %s", name)


def poll_spc() -> None:
    log.info("Polling SPC Day 1 outlook")
    data = fetch_json(SPC_OUTLOOK_URL)
    if data:
        risk = extract_spc_risk(data)
        summary = {
            "risk_level": risk,
            "raw": data,
        }
        write_cache(SPC_CACHE, summary)
        log.info("SPC risk for Nashville: %s", risk)
    elif SPC_CACHE.exists():
        log.warning("Using stale SPC cache")
    else:
        log.error("No SPC data and no cache")


def poll_uv() -> None:
    log.info("Fetching EPA hourly UV index")
    data = fetch_json(EPA_UV_URL)
    if isinstance(data, list):
        wrapped = {"hours": data}
        write_cache(UV_CACHE, wrapped)
        # Surface peak UV value in the log for visibility
        try:
            peak = max((int(r.get("UV_VALUE", 0)) for r in data), default=0)
        except Exception:
            peak = "?"
        log.info("UV cached (%d hours, peak %s)", len(data), peak)
    elif UV_CACHE.exists():
        log.warning("Using stale UV cache")
    else:
        log.error("No UV data and no cache")


def poll_satellite() -> None:
    log.info("Fetching GOES-19 GEOCOLOR satellite image")
    data = fetch_bytes(SATELLITE_URL)
    if data:
        write_cache_bytes(SATELLITE_CACHE, data)
        log.info("Satellite image cached (%d bytes)", len(data))
    elif SATELLITE_CACHE.exists():
        log.warning("Using stale satellite cache")
    else:
        log.error("No satellite data and no cache")


def poll_acars_sounding(station: str, cache_path: Path, active_24_7: bool = False) -> None:
    """Walk back through hour buckets on OU SHARP and grab the freshest profile
    for `station` (e.g. "BNA", "ATL"). When `active_24_7` is False, skip outside
    the 10 UTC - 05 UTC active window (used for BNA where traffic is sparse
    overnight). Writes parsed levels to `cache_path`."""
    import re
    now = datetime.now(timezone.utc)
    h_utc = now.hour
    if not active_24_7 and not (h_utc >= 10 or h_utc < 5):
        log.info("ACARS %s sounding skipped (outside active window, %02dZ)", station, h_utc)
        return
    log.info("Polling OU SHARP for %s ACARS sounding", station)
    chosen_name = None
    chosen_txt  = None
    chosen_valid = None
    file_pat = re.compile(rf"{re.escape(station)}_\d+\.txt")
    name_prefix = f"{station}_"
    for offset in range(0, 6):
        h = now - timedelta(hours=offset)
        bucket = h.strftime("%Y/%m/%d/%H")
        idx = fetch_text(f"{ACARS_SOUND_BASE}/{bucket}/")
        if not idx:
            continue
        files = sorted(set(file_pat.findall(idx)))
        if not files:
            continue
        newest = files[-1]
        url = f"{ACARS_SOUND_BASE}/{bucket}/{newest}"
        body = fetch_text(url)
        if not body or "%RAW%" not in body:
            continue
        hhmm = newest.replace(name_prefix, "").replace(".txt", "")
        try:
            valid = h.replace(hour=int(hhmm[:2]), minute=int(hhmm[2:]),
                              second=0, microsecond=0)
        except ValueError:
            continue
        chosen_name = newest
        chosen_txt = body
        chosen_valid = valid
        break
    if chosen_name is None:
        if cache_path.exists():
            log.warning("Using stale ACARS %s sounding cache", station)
        else:
            log.error("No ACARS %s sounding available and no cache", station)
        return
    levels = []
    in_raw = False
    for line in chosen_txt.splitlines():
        s = line.strip()
        if s.startswith("%RAW%"):
            in_raw = True
            continue
        if s.startswith("%END%"):
            break
        if not in_raw or not s:
            continue
        parts = [p.strip() for p in s.split(",")]
        if len(parts) != 6:
            continue
        try:
            levels.append({
                "p_hpa":    float(parts[0]),
                "z_m":      float(parts[1]),
                "t_c":      float(parts[2]),
                "td_c":     float(parts[3]),
                "wdir_deg": float(parts[4]),
                "wspd_kt":  float(parts[5]),
            })
        except ValueError:
            continue
    if len(levels) < 5:
        log.warning("ACARS %s sounding parse yielded only %d levels — skipping",
                    station, len(levels))
        return
    payload = {
        "profile_id": chosen_name.replace(".txt", ""),
        "valid_time": chosen_valid.isoformat(),
        "station":    f"K{station}",
        "levels":     levels,
    }
    write_cache(cache_path, payload)
    log.info("Cached ACARS %s sounding %s (%d levels, valid %s)",
             station, chosen_name, len(levels), chosen_valid.isoformat())


def _ts_age_min(iso: str | None) -> float | None:
    """Return age of an ISO timestamp in minutes, or None if unparseable."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    except Exception:
        return None


def _iem_to_nws_feature(iem: dict) -> dict | None:
    """Map IEM `last_ob` payload into an NWS-style feature dict so the
    renderer/api path can consume it unchanged. Returns None if mapping
    fails or essential fields are missing."""
    last = (iem or {}).get("last_ob") or {}
    utc_valid = last.get("utc_valid")
    if not utc_valid:
        return None

    def _F_to_C(f):
        return None if f is None else round((float(f) - 32.0) * 5.0 / 9.0, 2)

    def _kt_to_kmh(kt):
        return None if kt is None else round(float(kt) * 1.852, 3)

    def _mb_to_Pa(mb):
        return None if mb is None else round(float(mb) * 100.0, 2)

    def _mile_to_m(mi):
        return None if mi is None else round(float(mi) * 1609.344, 2)

    def _rh_from_T_Td(t_c, td_c):
        if t_c is None or td_c is None:
            return None
        # Magnus formula
        import math
        es = 6.112 * math.exp((17.67 * t_c) / (t_c + 243.5))
        e = 6.112 * math.exp((17.67 * td_c) / (td_c + 243.5))
        return round(max(0.0, min(100.0, 100.0 * e / es)), 2)

    t_c = _F_to_C(last.get("airtemp[F]"))
    td_c = _F_to_C(last.get("dewpointtemp[F]"))
    rh = _rh_from_T_Td(t_c, td_c)

    # Sky cover -> textDescription (METAR codes: CLR/SKC/FEW/SCT/BKN/OVC/VV)
    sky_codes = last.get("skycover[code]") or []
    primary_sky = sky_codes[0] if sky_codes else None
    sky_text = {
        "CLR": "Clear", "SKC": "Clear", "FEW": "Few Clouds",
        "SCT": "Partly Cloudy", "BKN": "Mostly Cloudy", "OVC": "Cloudy",
        "VV": "Obscured",
    }.get(primary_sky)
    pwx_codes = last.get("presentwx") or []
    text_desc = sky_text or (pwx_codes[0] if pwx_codes else "")

    return {
        "id": f"iem://stations/KBNA/observations/{utc_valid}",
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-86.68, 36.13]},
        "properties": {
            "@id": f"iem://stations/KBNA/observations/{utc_valid}",
            "@type": "wx:ObservationStation",
            "elevation": {"unitCode": "wmoUnit:m", "value": 180},
            "station": "https://api.weather.gov/stations/KBNA",
            "stationId": "KBNA",
            "stationName": "Nashville, Nashville International Airport",
            "timestamp": utc_valid,
            "rawMessage": last.get("raw", ""),
            "textDescription": text_desc,
            "icon": None,
            "presentWeather": pwx_codes,
            "temperature":         {"unitCode": "wmoUnit:degC",            "value": t_c,                       "qualityControl": "V"},
            "dewpoint":            {"unitCode": "wmoUnit:degC",            "value": td_c,                      "qualityControl": "V"},
            "windDirection":       {"unitCode": "wmoUnit:degree_(angle)",  "value": last.get("winddirection[deg]"), "qualityControl": "V"},
            "windSpeed":           {"unitCode": "wmoUnit:km_h-1",          "value": _kt_to_kmh(last.get("windspeed[kt]")), "qualityControl": "V"},
            "windGust":            {"unitCode": "wmoUnit:km_h-1",          "value": _kt_to_kmh(last.get("gust[kt]")), "qualityControl": "V"},
            "barometricPressure":  {"unitCode": "wmoUnit:Pa",              "value": _mb_to_Pa(last.get("mslp[mb]")), "qualityControl": "V"},
            "seaLevelPressure":    {"unitCode": "wmoUnit:Pa",              "value": _mb_to_Pa(last.get("mslp[mb]")), "qualityControl": "V"},
            "visibility":          {"unitCode": "wmoUnit:m",               "value": _mile_to_m(last.get("visibility[mile]")), "qualityControl": "C"},
            "maxTemperatureLast24Hours": {"unitCode": "wmoUnit:degC", "value": _F_to_C(last.get("max_dayairtemp[F]"))},
            "minTemperatureLast24Hours": {"unitCode": "wmoUnit:degC", "value": _F_to_C(last.get("min_dayairtemp[F]"))},
            "precipitationLast3Hours":   {"unitCode": "wmoUnit:mm",   "value": None, "qualityControl": "Z"},
            "relativeHumidity":          {"unitCode": "wmoUnit:percent", "value": rh, "qualityControl": "V"},
            "windChill":   {"unitCode": "wmoUnit:degC", "value": None, "qualityControl": "V"},
            "heatIndex":   {"unitCode": "wmoUnit:degC", "value": None, "qualityControl": "V"},
            "cloudLayers": [],
        },
    }


def _nws_observation_usable(feat: dict | None) -> bool:
    """An NWS observation is usable if temperature, relativeHumidity, and at
    least one of windSpeed.value / windDirection.value / windGust.value are
    populated. NWS occasionally publishes records with required fields null/
    QC=Z; those are upstream-broken and not worth rendering even if fresh.

    Gust counts as a wind signal: ASOS sometimes publishes gust-only records
    (speed/direction null, gust present). Treating those as unusable forced
    the IEM fallback to serve much older complete-wind observations — e.g.
    the 2026-05-18 incident where NWS had 16:10Z gust-only but the cache
    froze on IEM's 15:53Z while waiting for IEM to catch up."""
    if not feat:
        return False
    p = feat.get("properties") or {}
    t  = (p.get("temperature") or {}).get("value")
    rh = (p.get("relativeHumidity") or {}).get("value")
    ws = (p.get("windSpeed") or {}).get("value")
    wd = (p.get("windDirection") or {}).get("value")
    wg = (p.get("windGust") or {}).get("value")
    has_wind = ws is not None or wd is not None or wg is not None
    usable = t is not None and rh is not None and has_wind
    if not usable:
        wind_val = ws if ws is not None else (wd if wd is not None else wg)
        missing = [name for name, val in (("temperature", t), ("relativeHumidity", rh), ("wind", wind_val)) if val is None]
        log.warning("NWS observation unusable, missing fields: %s", ", ".join(missing))
    return usable


def poll_conditions() -> None:
    """Pull the most recent KBNA observation. Try NWS first; if NWS is stale
    (>15 min old) OR unusable (wind fields null), try IEM. Use whichever is
    fresher; if NWS is unusable, prefer IEM regardless of age. Mapped IEM
    data is shaped exactly like NWS so the renderer is agnostic."""
    log.info("Polling conditions (5-min ASOS)")
    data = fetch_json(CONDITIONS_URL)
    nws_feat = None
    nws_age_min = None
    if isinstance(data, dict):
        feats = data.get("features") or []
        if feats:
            nws_feat = feats[0]
            ts = (nws_feat.get("properties") or {}).get("timestamp")
            nws_age_min = _ts_age_min(ts)

    # Try IEM if NWS is stale, missing, or unusable (wind fields null)
    iem_feat = None
    iem_age_min = None
    nws_stale = nws_age_min is None or nws_age_min > NWS_STALE_THRESHOLD_MIN
    nws_unusable = nws_feat is not None and not _nws_observation_usable(nws_feat)
    if nws_stale or nws_unusable:
        if nws_unusable and not nws_stale:
            reason = "fresh but wind fields null/missing"
        elif nws_age_min is not None:
            reason = f"stale ({nws_age_min:.1f}m)"
        else:
            reason = "missing"
        log.info("NWS conditions %s; trying IEM fallback", reason)
        iem_data = fetch_json(IEM_CONDITIONS_URL)
        if isinstance(iem_data, dict):
            iem_feat = _iem_to_nws_feature(iem_data)
            if iem_feat:
                iem_ts = (iem_feat.get("properties") or {}).get("timestamp")
                iem_age_min = _ts_age_min(iem_ts)

    # Decide which to use: prefer fresher source, BUT if NWS is unusable,
    # always prefer IEM (any age) over a wind-null NWS record.
    use_iem = False
    if iem_feat is not None and iem_age_min is not None:
        if nws_age_min is None or iem_age_min < nws_age_min:
            use_iem = True
        elif nws_unusable:
            use_iem = True
            log.info("NWS observation unusable (wind null); using IEM at age %.1fm despite NWS being %.1fm",
                     iem_age_min, nws_age_min)

    if use_iem:
        chosen = iem_feat
        source = "iem"
    elif nws_feat is not None:
        chosen = nws_feat
        source = "nws"
    else:
        if CONDITIONS_CACHE.exists():
            log.warning("Both NWS and IEM unavailable; keeping stale cache")
        else:
            log.error("No conditions data from any source and no cache")
        return

    chosen["_source"] = source
    if nws_age_min is not None:
        chosen["_nws_age_min"] = round(nws_age_min, 1)
    if iem_age_min is not None:
        chosen["_iem_age_min"] = round(iem_age_min, 1)

    write_cache(CONDITIONS_CACHE, chosen)
    ts = (chosen.get("properties") or {}).get("timestamp", "?")
    log.info("Cached conditions (%d bytes, source=%s, ts %s, nws_age=%s, iem_age=%s)",
             CONDITIONS_CACHE.stat().st_size, source, ts,
             f"{nws_age_min:.1f}m" if nws_age_min is not None else "n/a",
             f"{iem_age_min:.1f}m" if iem_age_min is not None else "n/a")


# ── Alerts: NWS + IEM fallback ─────────────────────────────────────────────
def _alert_dedup_key(feat: dict) -> str | None:
    """VTEC string when present; else NWS id; else None.
    VTEC covers ~all warnings/watches/advisories; id is the safety net."""
    props = feat.get("properties") or {}
    vtec_list = (props.get("parameters") or {}).get("VTEC") or []
    if isinstance(vtec_list, list) and vtec_list:
        v = vtec_list[0]
        if isinstance(v, str) and v.strip():
            return v.strip()
    aid = feat.get("id") or props.get("@id") or props.get("id")
    if isinstance(aid, str) and aid.strip():
        return aid.strip()
    return None


def _iem_to_nws_alert_feature(row: dict) -> dict | None:
    """Map an IEM events_status.json row into NWS-shape alert feature so the
    renderer/api path consumes both transparently. IEM uses a flat-row schema
    (wfo / phenomena / significance / eventid / locations / issue / expire /
    status / event_label / etc.). Returns None if the row is already expired
    or doesn't touch our counties (Davidson / Williamson)."""
    expire_iso = row.get("expire")
    if expire_iso:
        try:
            exp = datetime.fromisoformat(str(expire_iso).replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > exp:
                return None
        except Exception:
            pass

    locations = row.get("locations") or ""
    if not any(c in locations for c in ALERTS_IEM_COUNTIES):
        return None

    wfo = row.get("wfo") or ""
    phen = row.get("phenomena") or ""
    sig = row.get("significance") or ""
    eventid = row.get("eventid")
    year = row.get("year")
    issue = row.get("issue") or row.get("updated") or ""
    expires = expire_iso or ""
    status_code = row.get("status") or "NEW"
    event_label = row.get("event_label") or f"{phen}.{sig}"

    # VTEC: 2026-O-NEW-KOHX-SV-W-0042 (action varies — use status as the
    # action component; if status looks like CON/EXP/UPG/CAN, use that)
    try:
        eid_str = f"{int(eventid):04d}" if eventid is not None else "0000"
    except Exception:
        eid_str = "0000"
    action = status_code if status_code in ("NEW", "CON", "EXP", "UPG",
                                            "CAN", "EXT", "EXA", "EXB",
                                            "ROU", "COR") else "NEW"
    vtec_str = f"O.{action}.K{wfo}.{phen}.{sig}.{eid_str}"
    aid = (row.get("issue_product_id")
           or f"iem://{year}.{wfo}.{phen}.{sig}.{eid_str}")

    severity_map = {"W": "Severe", "A": "Moderate", "Y": "Minor", "S": "Minor"}
    urgency_map  = {"W": "Immediate", "A": "Expected", "Y": "Expected", "S": "Expected"}

    return {
        "id": aid,
        "type": "Feature",
        "geometry": None,  # events_status doesn't carry polygon
        "properties": {
            "@id": aid,
            "@type": "wx:Alert",
            "id": aid,
            "areaDesc": locations,
            "geocode": {"UGC": [], "SAME": []},
            "sent":      issue,
            "effective": issue,
            "onset":     issue,
            "expires":   expires,
            "ends":      expires,
            "status":      "Actual",
            "messageType": "Alert" if action == "NEW" else "Update",
            "category":    "Met",
            "severity":    severity_map.get(sig, "Unknown"),
            "certainty":   "Observed" if action == "NEW" else "Likely",
            "urgency":     urgency_map.get(sig, "Unknown"),
            "event":       event_label,
            "sender":      f"K{wfo}",
            "senderName":  f"NWS {wfo}",
            "headline":    event_label,
            "description": "",  # not in events_status — would need a follow-up fetch
            "instruction": "",
            "response":    "Execute",
            "parameters":  {"VTEC": [vtec_str]},
        },
    }


def poll_alerts() -> None:
    """Two-tier alerts pull: NWS JSON primary + IEM mirror as fully
    independent infrastructure. Folds both into a VTEC-deduplicated set
    so an alert appearing in only one source still surfaces. NWS-canonical
    shape preferred when both have the same alert (no field translation)."""
    log.info("Polling alerts")

    # Tier 1: NWS JSON
    nws_data = fetch_json(ALERTS_NWS_URL)
    nws_feats: list = []
    nws_ok = isinstance(nws_data, dict)
    if nws_ok:
        nws_feats = (nws_data.get("features") or [])

    # Tier 2: IEM events_status (filtered to OHX WFO + Davidson/Williamson)
    iem_feats: list = []
    iem_ok = False
    try:
        iem_raw = fetch_json(ALERTS_IEM_URL)
        if isinstance(iem_raw, dict):
            for row in (iem_raw.get("data") or []):
                mapped = _iem_to_nws_alert_feature(row)
                if mapped is not None:
                    iem_feats.append(mapped)
            iem_ok = True
    except Exception as e:
        log.warning("IEM alerts fallback error: %s", e)

    # Dedup-merge: NWS first (canonical shape), IEM fills gaps
    merged: dict[str, dict] = {}
    per_alert_source: dict[str, str] = {}
    for feat in nws_feats:
        k = _alert_dedup_key(feat)
        if k:
            merged[k] = feat
            per_alert_source[k] = "nws"
        else:
            # Alert without a dedup key — include but key by python id
            merged[f"_unkey_nws_{id(feat)}"] = feat
    for feat in iem_feats:
        k = _alert_dedup_key(feat)
        if k and k not in merged:
            merged[k] = feat
            per_alert_source[k] = "iem"

    sources_ok = [s for s, ok in (("nws", nws_ok), ("iem", iem_ok)) if ok]

    if not sources_ok:
        if ALERTS_CACHE.exists():
            log.critical("ALL alerts sources failed — keeping last-known cache")
        else:
            log.error("All alerts sources failed and no cache exists")
        return

    out = {
        "type": "FeatureCollection",
        "features": list(merged.values()),
        "_sources": sources_ok,
        "_per_alert_source": per_alert_source,
        "_nws_count": len(nws_feats),
        "_iem_count": len(iem_feats),
    }
    write_cache(ALERTS_CACHE, out)
    log.info("Cached alerts (%d unique, nws=%d iem=%d, sources=%s)",
             len(merged), len(nws_feats), len(iem_feats), sources_ok)


def run_poller(name: str, fn, interval: int) -> None:
    """Run fn on a fixed interval, catching all errors."""
    while True:
        try:
            fn()
        except Exception as e:
            log.exception("Unhandled error in %s poller: %s", name, e)
        time.sleep(interval)


def main() -> None:
    log.info("Nashville Severe Weather Collector starting")

    apply_forecast_grid_to_endpoints()

    # Initial fetch — populate cache before API server starts serving
    poll_alerts()
    for name, cfg in ENDPOINTS.items():
        poll_nws(name, cfg)
    poll_spc()
    poll_satellite()
    poll_uv()
    poll_conditions()
    poll_acars_sounding("BNA", ACARS_BNA_CACHE, active_24_7=False)
    poll_acars_sounding("ATL", ACARS_ATL_CACHE, active_24_7=True)

    threads = []

    # Alerts get a dedicated poller (not poll_nws) — runs the two-tier
    # NWS + IEM merge.
    alerts_thread = threading.Thread(
        target=run_poller,
        args=("alerts", poll_alerts, ALERTS_INTERVAL),
        name="poller-alerts",
        daemon=True,
    )
    alerts_thread.start()
    threads.append(alerts_thread)

    for name, cfg in ENDPOINTS.items():
        t = threading.Thread(
            target=run_poller,
            args=(name, lambda cfg=cfg, name=name: poll_nws(name, cfg), cfg["interval"]),
            name=f"poller-{name}",
            daemon=True,
        )
        threads.append(t)
        t.start()

    spc_thread = threading.Thread(
        target=run_poller,
        args=("spc", poll_spc, SPC_INTERVAL),
        name="poller-spc",
        daemon=True,
    )
    spc_thread.start()
    threads.append(spc_thread)

    sat_thread = threading.Thread(
        target=run_poller,
        args=("satellite", poll_satellite, SATELLITE_INTERVAL),
        name="poller-satellite",
        daemon=True,
    )
    sat_thread.start()
    threads.append(sat_thread)

    uv_thread = threading.Thread(
        target=run_poller,
        args=("uv", poll_uv, UV_INTERVAL),
        name="poller-uv",
        daemon=True,
    )
    uv_thread.start()
    threads.append(uv_thread)

    cond_thread = threading.Thread(
        target=run_poller,
        args=("conditions", poll_conditions, CONDITIONS_INTERVAL),
        name="poller-conditions",
        daemon=True,
    )
    cond_thread.start()
    threads.append(cond_thread)

    sound_thread_bna = threading.Thread(
        target=run_poller,
        args=("acars_sounding_bna",
              lambda: poll_acars_sounding("BNA", ACARS_BNA_CACHE, active_24_7=False),
              ACARS_SOUND_INTERVAL),
        name="poller-acars-sounding-bna",
        daemon=True,
    )
    sound_thread_bna.start()
    threads.append(sound_thread_bna)

    sound_thread_atl = threading.Thread(
        target=run_poller,
        args=("acars_sounding_atl",
              lambda: poll_acars_sounding("ATL", ACARS_ATL_CACHE, active_24_7=True),
              ACARS_SOUND_INTERVAL),
        name="poller-acars-sounding-atl",
        daemon=True,
    )
    sound_thread_atl.start()
    threads.append(sound_thread_atl)

    log.info("All pollers running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        log.info("Collector stopped.")


if __name__ == "__main__":
    main()
