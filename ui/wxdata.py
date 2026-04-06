"""Weather sounding data collection: ACARS and radiosonde decoding.

Provides a unified MetStore for meteorological observations from both
acarsdec (ACARS/AMDAR) and radiosonde_auto_rx (weather balloon) decoders.
Reader threads tail decoder output and parse into the store.
"""
import json
import math
import os
import re
import socket
import threading
import time
import logging
from collections import deque
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Tuple

logger = logging.getLogger(__name__)

try:
    from .config import ACARS_OUTPUT_PATH, RADIOSONDE_UDP_HOST, RADIOSONDE_UDP_PORT
except ImportError:
    from ui.config import ACARS_OUTPUT_PATH, RADIOSONDE_UDP_HOST, RADIOSONDE_UDP_PORT


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class MetObservation:
    """A single meteorological observation at one altitude."""
    timestamp: float          # epoch seconds
    source: str               # "acars" or "radiosonde"
    source_id: str            # flight number or sonde serial
    lat: float                # decimal degrees
    lon: float                # decimal degrees
    altitude_ft: float
    pressure_hpa: float
    temp_c: float
    dewpoint_c: float
    wind_dir_deg: float
    wind_speed_kt: float
    humidity_pct: Optional[float] = None


@dataclass
class RawMessage:
    """A raw decoded message (ACARS or radiosonde telemetry frame)."""
    timestamp: float
    source: str
    source_id: str
    text: str
    is_met: bool = False


# ---------------------------------------------------------------------------
# Atmosphere utilities
# ---------------------------------------------------------------------------

def altitude_to_pressure(altitude_ft: float) -> float:
    """Convert altitude (feet) to pressure (hPa) using the ISA model."""
    alt_m = altitude_ft * 0.3048
    if alt_m <= 11000:
        # Troposphere: T = 288.15 - 0.0065 * h
        return 1013.25 * (1 - 0.0065 * alt_m / 288.15) ** 5.2561
    else:
        # Lower stratosphere (isothermal at 216.65 K)
        p_11 = 1013.25 * (1 - 0.0065 * 11000 / 288.15) ** 5.2561
        return p_11 * math.exp(-9.80665 * (alt_m - 11000) / (287.058 * 216.65))


def dewpoint_from_rh(temp_c: float, rh_pct: float) -> float:
    """Compute dewpoint from temperature and relative humidity (Magnus formula)."""
    if rh_pct is None or rh_pct <= 0:
        return -9999.0
    a, b = 17.27, 237.7
    alpha = (a * temp_c) / (b + temp_c) + math.log(rh_pct / 100.0)
    return (b * alpha) / (a - alpha)


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles between two lat/lon points."""
    R_NM = 3440.065  # Earth radius in nautical miles
    lat1_r, lon1_r = math.radians(lat1), math.radians(lon1)
    lat2_r, lon2_r = math.radians(lat2), math.radians(lon2)
    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    return 2 * R_NM * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# ACARS message parsing
# ---------------------------------------------------------------------------

# Labels that may carry meteorological data
_AMDAR_LABELS = {"H1", "H2", "4A", "44", "SA", "21"}

# --- Plain-text AMDAR patterns (legacy) ---
_RE_FL = re.compile(r'FL\s*(\d{2,3})')
_RE_TEMP = re.compile(r'([+-]?\d{1,3}(?:\.\d)?)\s*C\b')
_RE_WIND = re.compile(r'(\d{3})\s*/\s*(\d{1,3})\s*(?:KT|KTS)')
_RE_RH = re.compile(r'RH\s*(\d{1,3})')
_RE_LAT = re.compile(r'(\d{1,2}(?:\.\d+)?)\s*([NS])')
_RE_LON = re.compile(r'(\d{1,3}(?:\.\d+)?)\s*([EW])')
_RE_ALT = re.compile(r'(\d{4,5})\s*(?:FT|M)')

# --- Compressed ARINC 620 #M[12]BPOSN format ---
# Example: #M2BPOSN36363W086581,JONIL,192821,140,FFISK,193128,JNKNS,M5,28344,74,/TS...
# Fields: lat(5)NS lon(6), wpt, time, FL, wpt2, eta, [wpt3], temp, wind(5), [turb]
_RE_MBPOSN = re.compile(
    r'#M\dBPOSN(\d{5})([NEWS])(\d{5,6})'  # lat, hemisphere, lon
)

# --- #DFB...REP format (Republic/Delta connection) ---
# Example: ...N36817W 87992350-27-54257 92T 0512 126
_RE_DFB_POS = re.compile(
    r'[NS](\d{5})[EW]\s*(\d{4,5})'  # lat(5) lon(4-5)
    r'(\d{3,4})'                      # FL (3-4 digits, e.g. 2350 = FL235)
    r'([+-]?\d{1,3})'                 # temp
    r'([+-]?\d{1,3})'                 # dewpoint or second temp
    r'(\d{3})\s*'                     # wind dir
    r'(\d{1,3})'                      # wind speed
)

# --- Label 21 POSN format (Frontier-style) ---
# Example: POSN 36.252W 86.790, 136,193036,8931,31280, 31, 2,194006,KBNA
_RE_POSN21 = re.compile(
    r'POSN\s+(\d+\.\d+)([NEWS])\s+(\d+\.\d+)'  # lat, hemisphere, lon
    r',\s*(\d{2,3})'                              # FL
)


def _parse_m_temp(s: str) -> Optional[float]:
    """Parse compressed temp like 'M5' → -5.0, 'P10' → 10.0, '5' → 5.0."""
    s = s.strip()
    if not s:
        return None
    if s.startswith("M") or s.startswith("m"):
        try:
            return -float(s[1:])
        except ValueError:
            return None
    if s.startswith("P") or s.startswith("p"):
        try:
            return float(s[1:])
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def _try_parse_mbposn(text: str, ts: float, flight: str, reg: str) -> Optional[MetObservation]:
    """Try to parse #M[12]BPOSN compressed ARINC 620 position reports.

    Format: POSN{lat5}{hemi}{lon5-6} where hemi is the LONGITUDE hemisphere
    (N/E = positive lat/lon, S/W = negative).
    Example: POSN36363W086581 → lat=36.363°N, lon=86.581°W
    The latitude hemisphere is implicit (positive unless S appears elsewhere).
    """
    m = _RE_MBPOSN.search(text)
    if not m:
        return None

    lat_raw = m.group(1)  # e.g. "36363"
    hemi = m.group(2)     # N/S/E/W — this is the longitude hemisphere
    lon_raw = m.group(3)  # e.g. "086581"

    lat = float(lat_raw) / 1000.0
    lon = float(lon_raw) / 1000.0
    # The single hemisphere character between lat and lon indicates lon direction
    if hemi in ("W", "w"):
        lon = -lon
    elif hemi in ("S", "s"):
        # Southern hemisphere latitude (rare in CONUS but handle it)
        lat = -lat

    # Parse remaining comma-separated fields after the position
    # Find where position match ends and split rest by commas
    rest = text[m.end():]
    parts = [p.strip() for p in rest.split(",")]
    # parts[0] = "" (empty before first comma) or waypoint
    # Typical: ,wpt,time,FL,wpt2,eta,[wpt3],temp,wind5,turb,/TS...
    # Find FL (3-digit number < 500) and temp (M## or P##) in parts

    altitude_ft = 0.0
    temp_c = None
    wind_dir = 0.0
    wind_spd = 0.0
    humidity = None

    for i, part in enumerate(parts):
        if not part:
            continue
        # Flight level: 2-3 digit number typically 10-500
        if altitude_ft == 0 and part.isdigit() and 10 <= int(part) <= 500:
            altitude_ft = float(part) * 100
            continue
        # Temperature: M## or P## format
        if temp_c is None and len(part) >= 2 and part[0] in ("M", "P", "m", "p"):
            parsed = _parse_m_temp(part)
            if parsed is not None and -80 <= parsed <= 50:
                temp_c = parsed
                continue
        # Wind: 5-digit number (first 3 = dir, last 2+ = speed)
        if part.isdigit() and len(part) == 5:
            wd = int(part[:3])
            ws = int(part[3:])
            if 0 <= wd <= 360 and ws < 300:
                wind_dir = float(wd)
                wind_spd = float(ws)
                continue

    if altitude_ft <= 0 or temp_c is None:
        return None

    pressure = altitude_to_pressure(altitude_ft)
    dewpoint = dewpoint_from_rh(temp_c, humidity) if humidity else -9999.0

    return MetObservation(
        timestamp=ts, source="acars", source_id=flight or reg,
        lat=lat, lon=lon, altitude_ft=altitude_ft,
        pressure_hpa=round(pressure, 1), temp_c=temp_c,
        dewpoint_c=round(dewpoint, 1),
        wind_dir_deg=wind_dir, wind_speed_kt=wind_spd,
        humidity_pct=humidity,
    )


def _try_parse_dfbd3m(text: str, ts: float, flight: str, reg: str) -> List[MetObservation]:
    """Parse #DFBD3M descent meteorological profile messages.

    Format: #DFBD3M{id}{orig} {dest} {obs1}{obs2}...{trailer}
    Each observation: {position:13}{alt:5}{skip:3}{temp:4}{wdir:3}{wspd:3}{turb:5}
      position = N/S + 5 digits lat + E/W + 5-6 digits lon
      alt = 5 digits in tens of feet (00590 = 5,900 ft)
      skip = 3 digits (ground speed or Mach)
      temp = P/M + 3 digits in tenths of °C (P007 = +0.7°C)
      wdir = 3 digits wind direction
      wspd = 3 digits wind speed (kt)
      turb = 1 char + 4 digits (EDR turbulence)
    """
    if "#DFBD3M" not in text.upper():
        return []

    results: List[MetObservation] = []
    # Find all position anchors: N/S + 5 digits + E/W + 5-6 digits
    pos_re = re.compile(r'([NS])(\d{5})([EW])(\d{5,6})')
    for m in pos_re.finditer(text):
        lat_hemi = m.group(1)
        lat_raw = m.group(2)
        lon_hemi = m.group(3)
        lon_raw = m.group(4)

        # DDMMM = DD degrees + MM.M minutes → DD + MMM/600
        lat = float(lat_raw[:2]) + float(lat_raw[2:]) / 600.0
        if lat_hemi == "S":
            lat = -lat

        lon_digits = lon_raw
        lon = float(lon_digits[:3]) + float(lon_digits[3:]) / 600.0
        if lon_hemi == "W":
            lon = -lon

        # Data block starts right after the position match
        data = text[m.end():]
        if len(data) < 21:  # need at least alt(5)+skip(3)+temp(4)+wdir(3)+wspd(3)+turb(1+2)
            continue

        try:
            alt_raw = data[:5]
            if not alt_raw.isdigit():
                continue
            altitude_ft = int(alt_raw) * 10.0  # tens of feet

            # Skip 3 chars (ground speed / Mach)
            temp_str = data[8:12]  # P/M + 3 digits
            if temp_str[0] not in ("P", "M", "p", "m"):
                continue
            temp_sign = -1.0 if temp_str[0] in ("M", "m") else 1.0
            temp_c = temp_sign * float(temp_str[1:]) / 10.0

            wind_dir = float(data[12:15])
            wind_speed = float(data[15:18])
        except (ValueError, IndexError):
            continue

        if altitude_ft <= 0 or altitude_ft > 60000:
            continue
        if temp_c < -80 or temp_c > 50:
            continue
        if wind_dir > 360:
            continue

        pressure = altitude_to_pressure(altitude_ft)

        results.append(MetObservation(
            timestamp=ts, source="acars", source_id=flight or reg,
            lat=lat, lon=lon, altitude_ft=altitude_ft,
            pressure_hpa=round(pressure, 1), temp_c=round(temp_c, 1),
            dewpoint_c=-9999.0,
            wind_dir_deg=wind_dir, wind_speed_kt=wind_speed,
            humidity_pct=None,
        ))

    return results


def _try_parse_dfb_rep(text: str, ts: float, flight: str, reg: str) -> Optional[MetObservation]:
    """Try to parse #DFB...REP format (Republic/Delta Connection ACARS reports)."""
    upper = text.upper()
    if "#DFB" not in upper and "#DF" not in upper:
        return None
    # Skip #DFBD3M descent met profiles — handled by _try_parse_dfbd3m
    if "#DFBD3M" in upper or "#DFBD" in upper:
        return None
    m = _RE_DFB_POS.search(text)
    if not m:
        return None

    lat = float(m.group(1)) / 1000.0
    lon = float(m.group(2)) / (1000.0 if len(m.group(2)) == 5 else 100.0)
    # Assume CONUS = North lat, West lon
    if "W" in text[m.start() - 2:m.start() + 20]:
        lon = -lon

    fl_raw = m.group(3)
    # FL can be 3 or 4 digits — if 4, first 3 are FL (e.g. 2350 → FL235)
    if len(fl_raw) == 4:
        altitude_ft = float(fl_raw[:3]) * 100
    else:
        altitude_ft = float(fl_raw) * 100

    temp_c = float(m.group(4))
    dewpoint_c = float(m.group(5))
    wind_dir = float(m.group(6))
    wind_spd = float(m.group(7))

    if altitude_ft <= 0 or altitude_ft > 60000:
        return None
    if temp_c < -80 or temp_c > 50:
        return None
    if dewpoint_c < -80 or dewpoint_c > 50:
        return None
    if wind_dir > 360:
        return None

    pressure = altitude_to_pressure(altitude_ft)

    return MetObservation(
        timestamp=ts, source="acars", source_id=flight or reg,
        lat=lat, lon=lon, altitude_ft=altitude_ft,
        pressure_hpa=round(pressure, 1), temp_c=temp_c,
        dewpoint_c=round(dewpoint_c, 1),
        wind_dir_deg=wind_dir, wind_speed_kt=wind_spd,
        humidity_pct=None,
    )


def _try_parse_posn21(text: str, ts: float, flight: str, reg: str) -> Optional[MetObservation]:
    """Try to parse label-21 POSN format (Frontier-style position reports)."""
    m = _RE_POSN21.search(text)
    if not m:
        return None

    lat = float(m.group(1))
    if m.group(2) in ("S", "W"):
        lat = -lat
    lon = float(m.group(3))
    # In CONUS, lon is always West
    if lon > 0:
        lon = -lon

    fl = int(m.group(4))
    altitude_ft = float(fl) * 100

    if altitude_ft <= 0:
        return None

    # Remaining fields after FL are comma-separated; try to extract temp/wind
    rest = text[m.end():]
    parts = [p.strip() for p in rest.split(",")]

    pressure = altitude_to_pressure(altitude_ft)

    return MetObservation(
        timestamp=ts, source="acars", source_id=flight or reg,
        lat=lat, lon=lon, altitude_ft=altitude_ft,
        pressure_hpa=round(pressure, 1), temp_c=-9999.0,
        dewpoint_c=-9999.0,
        wind_dir_deg=0.0, wind_speed_kt=0.0,
        humidity_pct=None,
    )


def parse_acars_message(msg: dict) -> tuple:
    """Parse an acarsdec JSON message.

    Returns (RawMessage, List[MetObservation]).  The list is empty when
    the message carries no meteorological data and may contain multiple
    observations for multi-point descent profiles (#DFBD3M).
    """
    ts = msg.get("timestamp", time.time())
    if isinstance(ts, str):
        try:
            ts = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            ts = time.time()

    flight = (msg.get("flight") or msg.get("tail") or "").strip()
    label = (msg.get("label") or "").strip()
    text = (msg.get("text") or msg.get("message") or "").strip()
    reg = (msg.get("tail") or "").strip()

    raw = RawMessage(
        timestamp=ts,
        source="acars",
        source_id=flight or reg,
        text=f"[{label}] {flight}: {text}",
        is_met=False,
    )

    # Check if this label can carry met data
    if label not in _AMDAR_LABELS:
        return raw, []

    # Try #DFBD3M descent met profile (multi-observation per message)
    obs_list = _try_parse_dfbd3m(text, ts, flight, reg)
    if obs_list:
        raw.is_met = True
        return raw, obs_list

    # Try compressed ARINC 620 #M[12]BPOSN format (most common)
    obs = _try_parse_mbposn(text, ts, flight, reg)
    if obs:
        raw.is_met = True
        return raw, [obs]

    # Try #DFB REP format (Republic/Delta Connection)
    obs = _try_parse_dfb_rep(text, ts, flight, reg)
    if obs:
        raw.is_met = True
        return raw, [obs]

    # Try label-21 POSN format (Frontier-style)
    obs = _try_parse_posn21(text, ts, flight, reg)
    if obs and obs.temp_c > -9000:
        raw.is_met = True
        return raw, [obs]

    # Fall back to legacy plain-text AMDAR parsing
    upper = text.upper()

    lat, lon = 0.0, 0.0
    m = _RE_LAT.search(upper)
    if m:
        lat = float(m.group(1))
        if m.group(2) == "S":
            lat = -lat
    m = _RE_LON.search(upper)
    if m:
        lon = float(m.group(1))
        if m.group(2) == "W":
            lon = -lon

    altitude_ft = 0.0
    m = _RE_FL.search(upper)
    if m:
        altitude_ft = float(m.group(1)) * 100
    else:
        m = _RE_ALT.search(upper)
        if m:
            altitude_ft = float(m.group(1))

    if altitude_ft <= 0:
        return raw, []

    temp_c = -9999.0
    m = _RE_TEMP.search(upper)
    if m:
        temp_c = float(m.group(1))

    if temp_c == -9999.0:
        return raw, []

    wind_dir, wind_spd = 0.0, 0.0
    m = _RE_WIND.search(upper)
    if m:
        wind_dir = float(m.group(1))
        wind_spd = float(m.group(2))

    humidity = None
    m = _RE_RH.search(upper)
    if m:
        humidity = float(m.group(1))

    dewpoint = dewpoint_from_rh(temp_c, humidity) if humidity else -9999.0

    pressure = altitude_to_pressure(altitude_ft)

    raw.is_met = True
    obs = MetObservation(
        timestamp=ts, source="acars", source_id=flight or reg,
        lat=lat, lon=lon, altitude_ft=altitude_ft,
        pressure_hpa=round(pressure, 1), temp_c=temp_c,
        dewpoint_c=round(dewpoint, 1),
        wind_dir_deg=wind_dir, wind_speed_kt=wind_spd,
        humidity_pct=humidity,
    )
    return raw, [obs]


# ---------------------------------------------------------------------------
# Radiosonde telemetry parsing
# ---------------------------------------------------------------------------

def parse_radiosonde_telemetry(frame: dict) -> tuple:
    """Parse a radiosonde_auto_rx JSON telemetry frame.

    Returns (RawMessage, Optional[MetObservation]).
    """
    ts = frame.get("datetime", "")
    if isinstance(ts, str) and ts:
        try:
            ts = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            ts = time.time()
    else:
        ts = time.time()

    sonde_id = frame.get("id", frame.get("serial", "unknown"))
    sonde_type = frame.get("type", "")
    lat = frame.get("lat", 0.0)
    lon = frame.get("lon", 0.0)
    alt_m = frame.get("alt", 0.0)
    temp_c = frame.get("temp", -9999.0)
    humidity = frame.get("humidity", None)
    pressure = frame.get("pressure", None)

    altitude_ft = alt_m / 0.3048

    # Build a readable summary
    summary = f"[{sonde_type}] {sonde_id}: {alt_m:.0f}m {temp_c:.1f}C"
    if humidity is not None:
        summary += f" RH{humidity:.0f}%"

    raw = RawMessage(
        timestamp=ts,
        source="radiosonde",
        source_id=str(sonde_id),
        text=summary,
        is_met=False,
    )

    if temp_c == -9999.0 or alt_m <= 0:
        return raw, None

    # Pressure: use reported if available, else derive from altitude
    if pressure is None or pressure <= 0:
        pressure = altitude_to_pressure(altitude_ft)

    # Wind: radiosonde_auto_rx provides vel_h (horizontal speed m/s) and heading (degrees)
    vel_h = frame.get("vel_h", 0.0)  # m/s
    heading = frame.get("heading", 0.0)  # degrees, direction of travel
    # Wind direction is opposite to heading (wind blows FROM)
    wind_dir = (heading + 180) % 360
    wind_speed_kt = vel_h * 1.94384  # m/s to knots

    # Dewpoint
    dewpoint = dewpoint_from_rh(temp_c, humidity) if humidity else -9999.0

    raw.is_met = True
    obs = MetObservation(
        timestamp=ts,
        source="radiosonde",
        source_id=str(sonde_id),
        lat=lat,
        lon=lon,
        altitude_ft=round(altitude_ft, 0),
        pressure_hpa=round(pressure, 1),
        temp_c=temp_c,
        dewpoint_c=round(dewpoint, 1),
        wind_dir_deg=round(wind_dir, 0),
        wind_speed_kt=round(wind_speed_kt, 1),
        humidity_pct=humidity,
    )
    return raw, obs


# ---------------------------------------------------------------------------
# Unified met data store
# ---------------------------------------------------------------------------

class MetStore:
    """Thread-safe store for meteorological observations from any decoder."""

    def __init__(self, max_messages: int = 2000, max_met: int = 500):
        self._lock = threading.Lock()
        self._messages: deque = deque(maxlen=max_messages)
        self._met_obs: deque = deque(maxlen=max_met)
        self.collecting = False
        self.active_decoder: Optional[str] = None
        self._message_count = 0
        self._met_count = 0
        self._filtered_count = 0  # observations rejected by spatial filter
        self._last_message_time = 0.0
        # Spatial filter: cylinder centered on station
        self._filter_lat: Optional[float] = None
        self._filter_lon: Optional[float] = None
        self._filter_radius_nm: float = 10.0   # default 10 nm = 20 mi diameter
        self._filter_ceiling_ft: float = 40000.0
        self._filter_enabled: bool = False
        self._filter_user_set: bool = False  # True when user explicitly set via API

    def set_spatial_filter(self, lat: float, lon: float,
                           radius_nm: float = 10.0,
                           ceiling_ft: float = 40000.0,
                           user_set: bool = False) -> None:
        """Configure the collection cylinder. Rejects obs outside it."""
        with self._lock:
            self._filter_lat = lat
            self._filter_lon = lon
            self._filter_radius_nm = radius_nm
            self._filter_ceiling_ft = ceiling_ft
            self._filter_enabled = (lat != 0.0 or lon != 0.0)
            if user_set:
                self._filter_user_set = True

    @property
    def filter_user_set(self) -> bool:
        """Whether the user has explicitly configured the filter."""
        with self._lock:
            return self._filter_user_set

    def clear_spatial_filter(self) -> None:
        """Disable spatial filtering — accept all observations."""
        with self._lock:
            self._filter_enabled = False
            self._filter_lat = None
            self._filter_lon = None
            self._filter_user_set = True  # User explicitly cleared = user choice

    def _passes_filter(self, obs: 'MetObservation') -> bool:
        """Check if an observation falls inside the collection cylinder.
        Must be called under self._lock."""
        if not self._filter_enabled:
            return True
        if obs.altitude_ft > self._filter_ceiling_ft:
            return False
        if obs.lat == 0.0 and obs.lon == 0.0:
            return False  # no position — can't verify
        dist = haversine_nm(self._filter_lat, self._filter_lon, obs.lat, obs.lon)
        return dist <= self._filter_radius_nm

    def add_message(self, msg: RawMessage) -> None:
        with self._lock:
            self._messages.append(msg)
            self._message_count += 1
            self._last_message_time = msg.timestamp

    def add_observation(self, obs: MetObservation) -> bool:
        """Add an observation if it passes the spatial filter. Returns True if accepted."""
        with self._lock:
            if not self._passes_filter(obs):
                self._filtered_count += 1
                return False
            self._met_obs.append(obs)
            self._met_count += 1
            return True

    def get_messages(self, limit: int = 50, source: Optional[str] = None) -> List[dict]:
        with self._lock:
            msgs = list(self._messages)
        if source:
            msgs = [m for m in msgs if m.source == source]
        return [asdict(m) for m in msgs[-limit:]]

    def get_sounding_data(self) -> dict:
        """Return observations sorted by altitude (ascending) for plotting."""
        with self._lock:
            obs = list(self._met_obs)
        obs.sort(key=lambda o: o.altitude_ft, reverse=True)
        return {
            "observations": len(obs),
            "active_decoder": self.active_decoder,
            "levels": [asdict(o) for o in obs],
        }

    def get_sounding_spc(self, station: str = "SB3") -> str:
        """Export sounding in SHARPpy SPC format."""
        with self._lock:
            obs = list(self._met_obs)
        obs.sort(key=lambda o: -o.pressure_hpa)  # decreasing pressure

        now = time.strftime("%y%m%d/%H%M", time.gmtime())
        lines = [
            "%TITLE%",
            f" {station}   {now}",
            "",
            "   LEVEL       HGHT       TEMP       DWPT       WDIR       WSPD",
            "-------------------------------------------------------------------",
            "%RAW%",
        ]
        for o in obs:
            hght_m = o.altitude_ft * 0.3048
            dp = o.dewpoint_c if o.dewpoint_c != -9999.0 else -9999.0
            lines.append(
                f"{o.pressure_hpa:>8.1f},{hght_m:>10.1f},{o.temp_c:>10.1f},"
                f"{dp:>10.1f},{o.wind_dir_deg:>10.0f},{o.wind_speed_kt:>10.1f}"
            )
        lines.append("%END%")
        return "\n".join(lines) + "\n"

    def get_status(self) -> dict:
        with self._lock:
            status = {
                "collecting": self.collecting,
                "active_decoder": self.active_decoder,
                "message_count": self._message_count,
                "met_count": self._met_count,
                "filtered_count": self._filtered_count,
                "last_message_time": self._last_message_time,
                "spatial_filter": self._filter_enabled,
            }
            if self._filter_enabled:
                status["filter_lat"] = self._filter_lat
                status["filter_lon"] = self._filter_lon
                status["filter_radius_nm"] = self._filter_radius_nm
                status["filter_ceiling_ft"] = self._filter_ceiling_ft
            return status

    def clear(self, source: Optional[str] = None) -> None:
        with self._lock:
            if source:
                self._messages = deque(
                    (m for m in self._messages if m.source != source),
                    maxlen=self._messages.maxlen,
                )
                self._met_obs = deque(
                    (o for o in self._met_obs if o.source != source),
                    maxlen=self._met_obs.maxlen,
                )
            else:
                self._messages.clear()
                self._met_obs.clear()
                self._message_count = 0
                self._met_count = 0
                self._filtered_count = 0
                self._last_message_time = 0.0


# ---------------------------------------------------------------------------
# Reader worker threads
# ---------------------------------------------------------------------------

def acars_reader_worker(store: MetStore, stop_event: threading.Event) -> None:
    """Tail acarsdec JSON-line output file and parse into store."""
    logger.info("ACARS reader thread started")
    path = ACARS_OUTPUT_PATH

    while not stop_event.is_set():
        try:
            # Wait for output file to exist
            if not os.path.exists(path):
                stop_event.wait(1)
                continue

            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                # Seek to end so we only read new lines
                f.seek(0, 2)
                while not stop_event.is_set():
                    line = f.readline()
                    if not line:
                        stop_event.wait(0.2)
                        continue
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    raw, obs_list = parse_acars_message(msg)
                    store.add_message(raw)
                    for obs in obs_list:
                        store.add_observation(obs)
        except FileNotFoundError:
            stop_event.wait(1)
        except Exception:
            logger.exception("ACARS reader error")
            stop_event.wait(2)

    logger.info("ACARS reader thread stopped")


def radiosonde_reader_worker(store: MetStore, stop_event: threading.Event) -> None:
    """Listen for radiosonde_auto_rx UDP JSON telemetry and parse into store."""
    logger.info("Radiosonde reader thread started")

    while not stop_event.is_set():
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((RADIOSONDE_UDP_HOST, RADIOSONDE_UDP_PORT))
            sock.settimeout(2.0)

            while not stop_event.is_set():
                try:
                    data, _ = sock.recvfrom(65536)
                except socket.timeout:
                    continue
                if not data:
                    continue
                try:
                    frame = json.loads(data.decode("utf-8", errors="ignore"))
                except json.JSONDecodeError:
                    continue

                raw, obs = parse_radiosonde_telemetry(frame)
                store.add_message(raw)
                if obs:
                    store.add_observation(obs)
        except OSError as e:
            logger.warning("Radiosonde UDP bind error: %s, retrying...", e)
            stop_event.wait(3)
        except Exception:
            logger.exception("Radiosonde reader error")
            stop_event.wait(2)
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

    logger.info("Radiosonde reader thread stopped")
