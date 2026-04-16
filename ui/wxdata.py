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
from dataclasses import dataclass, asdict, field
from typing import Optional, List, Dict, Tuple

logger = logging.getLogger(__name__)

try:
    from .config import ACARS_OUTPUT_PATH, VDL2_OUTPUT_PATH, RADIOSONDE_UDP_HOST, RADIOSONDE_UDP_PORT
except ImportError:
    from ui.config import ACARS_OUTPUT_PATH, VDL2_OUTPUT_PATH, RADIOSONDE_UDP_HOST, RADIOSONDE_UDP_PORT


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
    raw: dict = field(default_factory=dict)
    decode_meta: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Atmosphere utilities
# ---------------------------------------------------------------------------

def altitude_to_pressure(altitude_ft: float) -> float:
    """Convert altitude (feet) to pressure (mb / hPa — numerically identical) using ISA."""
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
_AMDAR_LABELS = {"H1", "H2", "4A", "44", "SA", "21", "22"}
_ATIS_LABELS = {"Q0", "83", "8L", "D1"}
_POSITION_LABELS = {"10", "11", "13", "21", "22", "52", "54", "H1", "H2", "PR"}
_WEATHER_LABELS = {"44", "4A", "A0", "AA", "E1", "E2", "F3", "SA"}
_LOGON_LABELS = {"RA", "80", "1L", "1M"}
_CPDLC_LABELS = {"5U", "5V", "5W", "5X", "5Y"}
_ADSC_LABELS = {"5Z", "E9"}

_ACARS_LABEL_NAMES = {
    "10": "Position Report",
    "11": "Arrival Report",
    "13": "Departure Report",
    "21": "Position Report",
    "22": "Position Report",
    "44": "Meteorological Report",
    "4A": "AMDAR Report",
    "52": "Out/Off/On/In Report",
    "54": "OOOI Position",
    "5U": "CPDLC Connect",
    "5V": "CPDLC Request",
    "5W": "CPDLC Uplink",
    "5X": "CPDLC Downlink",
    "5Y": "CPDLC Disconnect",
    "5Z": "ADS-C",
    "80": "Unit Availability",
    "83": "ATIS Download",
    "8L": "ATIS/Clearance",
    "A0": "Weather Report",
    "AA": "Weather/PIREP",
    "D1": "Digital ATIS",
    "E1": "Prefile PIREP",
    "E2": "Winds Aloft",
    "E9": "ADS Report",
    "F3": "Position/Weather",
    "H1": "OOOI/Position (ARINC 633)",
    "H2": "Fuel/Position (ARINC 633)",
    "PR": "Position Report",
    "Q0": "ATIS",
    "RA": "ACARS Logon",
    "SA": "Winds/Temperature",
}

_ACARS_LABEL_EXPLAINERS = {
    "H1": "OOOI means Out / Off / On / In. This family is commonly used for movement milestones and position updates.",
    "H2": "Often used for fuel and position status updates sent by the aircraft.",
    "Q0": "ATIS means Automatic Terminal Information Service: airport weather and operational information.",
    "83": "This usually carries an ATIS download or request response.",
    "8L": "This is usually airport information or clearance text.",
    "44": "A weather-related ACARS message, often with meteorological content.",
    "4A": "AMDAR is aircraft-based weather reporting.",
    "5Z": "ADS-C is contract-based position and surveillance reporting over datalink.",
    "SA": "Usually a winds and temperature report from the aircraft.",
    "RA": "A datalink logon or session-setup message.",
}

_XID_TYPE_NAMES = {
    "XID_CMD_LCR": "Link capability negotiation",
    "XID_CMD_HO_REQ": "Handover request",
    "XID_CMD_HO_CFM": "Handover confirm",
    "XID_CMD_HO_CPL": "Handover complete",
}

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
# Variant: #M1B/HDQDLUA.POSN36085W088340,... (routing prefix before POSN)
# Fields: lat(5)NS lon(6), wpt, time, FL, wpt2, eta, [wpt3], temp, wind(5), [turb]
_RE_MBPOSN = re.compile(
    r'#M\dB[^P]*POSN(\d{5})([NEWS])(\d{5,6})'  # lat, hemisphere, lon
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

# --- #DFB*WXR weather report format ---
# Example: #DFB*WXRN31548W0842301136340-53324302700 N32405W0845371143340-53...
# Each obs: [NS]lat5[EW]lon6 + skip(4) + FL(3) + temp(signed 2-3) + wdir(3) + wspd(2)
_RE_WXR_POS = re.compile(r'([NS])(\d{5})([EW])(\d{6})')  # position only

# --- Label 22 position format (e.g. Frontier/Spirit) ---
# Example: N 351210W 855840,-------,120842,30148, , , ,M 45,24940 21, 100,
_RE_LABEL22 = re.compile(
    r'([NS])\s*(\d{6})([EW])\s*(\d{6,7})'  # lat DDMMSS, lon DDDMMSS
)

# --- NW/DL POSN comma format (VDL2-heavy) ---
# Example: POSN36363W086581,JONIL,192821,350,FFISK,193128,JNKNS,M57,28344,...
_RE_POSN_COMMA = re.compile(
    r"POSN(\d{5})W(\d{6}),"        # position
    r"[A-Z0-9]{1,6},"              # fix
    r"(\d{6}),"                    # HHMMSS
    r"(\d{3}),"                    # flight level
    r"[A-Z0-9]{1,6},"              # next fix
    r"\d{6},"                      # next eta
    r"[A-Z0-9]{1,6},"              # enroute fix
    r"M(\d{2,3}),"                 # temp (M = minus)
    r"(\d{3})(\d{2,3})"            # wind dir(3) + speed(2-3)
)

# --- WN/OO 02E16 VDL2 position format ---
# Example: 02E16KMDWKBNA\nN36123W086451 19213700 M54 27040...
_RE_WN_02E16 = re.compile(
    r"02E16[A-Z]{4,8}\s*\r?\n?\s*"  # header + orig/dest
    r"N(\d{5})W(\d{6})\s+"          # position
    r"(\d{4})(\d{4})\s+"            # HHMM + alt4 (FL370 stored as 3700)
    r"M(\d{2,3})\s+"                # temp (ARINC minus)
    r"(\d{3})(\d{3})"               # wind dir + speed
)

# --- DL compact ACARS cruise report ---
# Format: LAT4 -LON4 FL3 -TAT -SAT DIR3 SPD2 [750511|AA-###]
# Trailing anchor disambiguates wind speed from the fuel/misc trailer.
_RE_DL_COMPACT = re.compile(
    r"(?<![0-9])(\d{4})\s*-(\d{4})\s*(\d{3})\s*-(\d{2,3})\s*-(\d{2,3})\s*"
    r"(\d{3})\s*(\d{2,3})\s*(?:750511|AA-\d{3})"
)


def _parse_m_temp(s: str) -> Optional[float]:
    """Parse compressed temp like 'M5' → -5.0, 'P10' → 10.0, '5' → 5.0."""
    s = s.strip()
    if not s:
        return None


def _decode_meta(protocol_family: str, title: str, body: str,
                 note: str = "", confidence: str = "medium") -> dict:
    meta = {
        "protocol_family": protocol_family,
        "title": title,
        "body": body,
        "confidence": confidence,
    }
    if note:
        meta["note"] = note
    return meta


def _format_position_phrase(obs_list: List['MetObservation']) -> str:
    if not obs_list:
        return ""
    obs = obs_list[0]
    pieces = []
    if obs.lat or obs.lon:
        lat_hemi = "N" if obs.lat >= 0 else "S"
        lon_hemi = "E" if obs.lon >= 0 else "W"
        pieces.append(f"near {abs(obs.lat):.3f}{lat_hemi} {abs(obs.lon):.3f}{lon_hemi}")
    if obs.altitude_ft > 0:
        if obs.altitude_ft >= 18000:
            pieces.append(f"at FL{int(round(obs.altitude_ft / 100.0))}")
        else:
            pieces.append(f"at {int(round(obs.altitude_ft)):,} ft")
    if not pieces:
        return ""
    return " Position " + " ".join(pieces) + "."


def _classify_acars_decode(label: str, text: str, is_met: bool,
                           obs_list: Optional[List['MetObservation']] = None) -> dict:
    label = (label or "").upper()
    text_upper = (text or "").upper()
    label_name = _ACARS_LABEL_NAMES.get(label, "")
    label_note = _ACARS_LABEL_EXPLAINERS.get(label, "")
    position_phrase = _format_position_phrase(obs_list or [])

    if label in _ATIS_LABELS:
        return _decode_meta(
            "acars_atis",
            "Airport information / ATIS message",
            "This is an airport information message. It usually carries ATIS or related airport operational text.",
            label_note or "ATIS = Automatic Terminal Information Service.",
            "high",
        )

    if label in _POSITION_LABELS:
        if label in {"52", "54"} or "OOOI" in text_upper:
            return _decode_meta(
                "acars_oooi",
                "OOOI milestone / movement status",
                "This is an aircraft movement milestone in the Out / Off / On / In family. It usually marks gate departure, takeoff, landing, or gate arrival.",
                label_note or "Movement milestone traffic rather than free-text dispatch content.",
                "high",
            )

        if label == "H2":
            return _decode_meta(
                "acars_fuel_position",
                "Fuel / position status update",
                "This is an ARINC 633 fuel and position status message." + position_phrase,
                label_note or "Often used for fuel and position status updates sent by the aircraft.",
                "high",
            )

        if label == "11":
            return _decode_meta(
                "acars_arrival_status",
                "Arrival status message",
                "This is an arrival-side milestone or progress message in the aircraft movement family.",
                label_note or "Movement/status traffic rather than free-text dispatch content.",
                "high",
            )

        if label == "13":
            return _decode_meta(
                "acars_departure_status",
                "Departure status message",
                "This is a departure-side milestone or progress message in the aircraft movement family.",
                label_note or "Movement/status traffic rather than free-text dispatch content.",
                "high",
            )

        if label in {"H1", "21", "22", "PR", "10"} or "POSN" in text_upper:
            return _decode_meta(
                "acars_enroute_position",
                "Enroute position update",
                "This is an aircraft position update in the movement-report family." + position_phrase,
                label_note or "Position/status traffic rather than a free-text operational message.",
                "high",
            )

        return _decode_meta(
            "acars_position",
            "Position / movement report",
            "This is an operational aircraft movement or position report. These messages commonly track gate, takeoff, landing, or progress milestones.",
            label_note or "Operational aircraft status traffic.",
            "high",
        )

    if label in _WEATHER_LABELS or is_met:
        return _decode_meta(
            "acars_weather",
            "Weather-related aircraft message",
            "This is a weather or atmosphere-related aircraft report. It is the family most likely to feed the sounding and winds/temperature workflow.",
            label_note or "Weather labels often carry winds, temperature, position, or observation data.",
            "high" if label in _WEATHER_LABELS else "medium",
        )

    if label in _LOGON_LABELS:
        return _decode_meta(
            "acars_logon",
            "Datalink session / logon message",
            "This is session-management traffic used to bring the datalink up or verify connectivity, not the operational flight message itself.",
            label_note or "Useful for connectivity context more than flight operations.",
            "high",
        )

    if label in _CPDLC_LABELS:
        return _decode_meta(
            "acars_cpdlc",
            "Controller-pilot datalink control message",
            "This is CPDLC control traffic rather than a voice transmission. It belongs to controller-pilot text communications.",
            label_note or "CPDLC = Controller-Pilot Data Link Communications.",
            "high",
        )

    if label in _ADSC_LABELS:
        return _decode_meta(
            "acars_adsc",
            "ADS-C surveillance / contract report",
            "This is ADS-C datalink traffic, typically used for surveillance or position contract reporting rather than plain airline text.",
            label_note or "ADS-C = Automatic Dependent Surveillance-Contract.",
            "high",
        )

    if label or label_name:
        return _decode_meta(
            "acars_labeled",
            label_name or "Labeled datalink message",
            f"This ACARS message is in label family {label or 'unknown'}. The family is identified, but the content is not fully interpreted here yet.",
            label_note or "Use the raw payload when you need the exact protocol content.",
            "medium",
        )

    return _decode_meta(
        "acars_unknown",
        "Unclassified datalink traffic",
        "This is aircraft datalink traffic, but it does not map cleanly to one of the common ACARS families yet.",
        "Use the raw payload for the exact frame structure and message fields.",
        "low",
    )


def _summarize_vdl2_non_acars_frame(frame: dict) -> tuple[str, dict]:
    vdl2 = frame.get("vdl2", {})
    avlc = vdl2.get("avlc", {}) if isinstance(vdl2, dict) else {}
    frame_type = (avlc.get("frame_type") or vdl2.get("frame_type") or "").upper()

    if not isinstance(avlc, dict):
        return "VDL2 frame", _decode_meta(
            "vdl2_unknown",
            "Unclassified VDL2 traffic",
            "This is a VDL2 frame, but the decoder did not expose enough structure to classify it further here.",
            "Use the raw payload for the exact frame structure.",
            "low",
        )

    if "xid" in avlc:
        xid = avlc.get("xid", {}) if isinstance(avlc.get("xid"), dict) else {}
        xid_type = xid.get("type", "")
        xid_name = _XID_TYPE_NAMES.get(xid_type, xid_type or "Link negotiation")
        return xid_name, _decode_meta(
            "vdl2_xid",
            "VDL2 link setup / negotiation",
            "This is link-management traffic used to establish or maintain the VDL2 radio/data session, not the operational airline message itself.",
            f"{xid_name}. Not an operational message.",
            "high",
        )

    if "x25" in avlc:
        x25 = avlc.get("x25", {}) if isinstance(avlc.get("x25"), dict) else {}
        x25_keys = ", ".join(sorted(k for k in x25.keys() if isinstance(k, str))[:4])
        note = "Higher-layer VDL2 network payload."
        if x25_keys:
            note = f"Higher-layer keys present: {x25_keys}."
        return "ATN / CPDLC data", _decode_meta(
            "vdl2_x25",
            "ATN / CPDLC-style network payload",
            "This is higher-layer VDL2 network traffic. It often carries controller-pilot data, ADS-C, or other ATN application data rather than plain ACARS text.",
            note,
            "medium",
        )

    if "gsif" in avlc:
        return "Ground-station service info", _decode_meta(
            "vdl2_gsif",
            "Ground-station information",
            "This is service or network information broadcast by a ground station to support VDL2 operations.",
            "Useful for radio-network context more than aircraft operations.",
            "high",
        )

    if frame_type == "I":
        return "Addressed VDL2 data", _decode_meta(
            "vdl2_information",
            "VDL2 addressed data frame",
            "This is a point-to-point VDL2 information frame. It carries data between a specific air and ground endpoint, but the application payload is not decoded here yet.",
            "Common on busy links even when the higher-layer payload is not human-readable.",
            "medium",
        )

    if frame_type == "S":
        return "VDL2 supervisory frame", _decode_meta(
            "vdl2_supervisory",
            "VDL2 supervisory frame",
            "This is link-layer flow-control or acknowledgment traffic. It keeps the radio session synchronized rather than carrying an operator-facing message.",
            "Usually safe to ignore unless you are debugging the link.",
            "high",
        )

    if frame_type == "U":
        return "VDL2 control frame", _decode_meta(
            "vdl2_control",
            "VDL2 control frame",
            "This is a VDL2 link-layer control frame used for management or session control rather than airline message content.",
            "Usually useful only for protocol/debug analysis.",
            "high",
        )

    return "Unclassified VDL2 traffic", _decode_meta(
        "vdl2_unknown",
        "Unclassified VDL2 traffic",
        "This is VDL2 traffic that does not map cleanly to the common ACARS or management families yet.",
        "Use the raw payload for the exact frame structure and message fields.",
        "low",
    )
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

    if altitude_ft <= 0:
        return None

    if temp_c is None:
        temp_c = -9999.0
    pressure = altitude_to_pressure(altitude_ft)
    dewpoint = dewpoint_from_rh(temp_c, humidity) if humidity and temp_c > -9000 else -9999.0

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


def _try_parse_dfb_wxr(text: str, ts: float, flight: str, reg: str) -> List[MetObservation]:
    """Parse #DFB*WXR weather report messages (multi-observation).

    Format: #DFB*WXR{obs1} {obs2} ...
    Each observation: [NS]lat5[EW]lon6 + skip(4) + FL(3) + temp(signed) + wdir(3) + wspd(2)
    Example: N31548W0842301136340-53324302700
      N31548  = 31.548°N
      W084230 = 84.230°W
      1136    = skip (time or distance)
      340     = FL340
      -53     = temp -53°C
      324     = wind from 324°
      30      = wind 30 kt
    """
    upper = text.upper()
    if "*WXR" not in upper:
        return []

    results: List[MetObservation] = []
    for m in _RE_WXR_POS.finditer(text):
        lat_hemi = m.group(1)
        lat_raw = m.group(2)
        lon_hemi = m.group(3)
        lon_raw = m.group(4)

        lat = float(lat_raw[:2]) + float(lat_raw[2:]) / 1000.0
        if lat_hemi == "S":
            lat = -lat
        lon = float(lon_raw[:3]) + float(lon_raw[3:]) / 1000.0
        if lon_hemi == "W":
            lon = -lon

        # After position: skip(4) + FL(3) + temp(signed 2-3) + wdir(3) + wspd(2)
        rest = text[m.end():]
        if len(rest) < 12:
            continue

        try:
            # skip 4 digits
            if not rest[:4].isdigit():
                continue
            fl_str = rest[4:7]
            if not fl_str.isdigit():
                continue
            altitude_ft = float(int(fl_str)) * 100

            # Temperature: signed, starts at offset 7
            # Negative: -NN (2 digits) e.g. -53; positive: NN (2 digits)
            temp_rest = rest[7:]
            if temp_rest[0] == '-':
                # Negative temp: sign + 2 digits
                temp_c = float(temp_rest[:3])  # e.g. "-53"
                after_temp = temp_rest[3:]
            else:
                # Positive temp: 2 digits
                if not temp_rest[:2].isdigit():
                    continue
                temp_c = float(temp_rest[:2])
                after_temp = temp_rest[2:]

            if len(after_temp) < 5:
                continue
            wind_dir = float(after_temp[:3])
            wind_spd = float(after_temp[3:5])
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
            pressure_hpa=round(pressure, 1), temp_c=temp_c,
            dewpoint_c=-9999.0,
            wind_dir_deg=wind_dir, wind_speed_kt=wind_spd,
            humidity_pct=None,
        ))

    return results


def _try_parse_label22(text: str, ts: float, flight: str, reg: str) -> Optional[MetObservation]:
    """Parse label-22 position reports (Frontier/Spirit style).

    Example: N 351210W 855840,-------,120842,30148, , , ,M 45,24940 21, 100,
      N 351210 = 35°12'10" N
      W 855840 = 85°58'40" W
      30148 = altitude (could be 30,148 ft or FL301)
      M 45 = temp -45°C
      24940 = wind 249° at 40 kt
    """
    m = _RE_LABEL22.search(text)
    if not m:
        return None

    lat_hemi = m.group(1)
    lat_raw = m.group(2)  # DDMMSS
    lon_hemi = m.group(3)
    lon_raw = m.group(4)  # DDDMMSS

    lat = float(lat_raw[:2]) + float(lat_raw[2:4]) / 60.0 + float(lat_raw[4:6]) / 3600.0
    if lat_hemi == "S":
        lat = -lat

    if len(lon_raw) >= 7:
        lon = float(lon_raw[:3]) + float(lon_raw[3:5]) / 60.0 + float(lon_raw[5:7]) / 3600.0
    else:
        lon = float(lon_raw[:2]) + float(lon_raw[2:4]) / 60.0 + float(lon_raw[4:6]) / 3600.0
    if lon_hemi == "W":
        lon = -lon

    # Parse remaining comma-separated fields
    rest = text[m.end():]
    parts = [p.strip() for p in rest.split(",")]

    altitude_ft = 0.0
    temp_c = None
    wind_dir = 0.0
    wind_spd = 0.0

    for part in parts:
        if not part:
            continue
        # Altitude: 5-digit number like 30148
        if altitude_ft == 0 and part.isdigit() and len(part) == 5:
            val = int(part)
            if val > 5000:
                altitude_ft = float(val)  # raw feet
            else:
                altitude_ft = float(val) * 100  # FL
            continue
        # Temperature: M ## or P ## format (with possible space)
        if temp_c is None and len(part) >= 2:
            temp_str = part.replace(" ", "")
            parsed = _parse_m_temp(temp_str)
            if parsed is not None and -80 <= parsed <= 50:
                temp_c = parsed
                continue
        # Wind: 5-digit compact (dddss) like 24940 → 249° at 40kt
        # May have trailing text after space (e.g. "24940 21")
        wind_token = part.split()[0] if part else ""
        if wind_token.isdigit() and len(wind_token) == 5:
            wd = int(wind_token[:3])
            ws = int(wind_token[3:])
            if 0 <= wd <= 360 and ws < 300:
                wind_dir = float(wd)
                wind_spd = float(ws)
                continue

    if altitude_ft <= 0:
        return None

    if temp_c is None:
        temp_c = -9999.0
    pressure = altitude_to_pressure(altitude_ft)
    return MetObservation(
        timestamp=ts, source="acars", source_id=flight or reg,
        lat=lat, lon=lon, altitude_ft=altitude_ft,
        pressure_hpa=round(pressure, 1), temp_c=temp_c,
        dewpoint_c=-9999.0,
        wind_dir_deg=wind_dir, wind_speed_kt=wind_spd,
        humidity_pct=None,
    )


def _try_parse_posn_comma(text: str, ts: float, flight: str, reg: str) -> Optional[MetObservation]:
    """Parse NW/DL POSN comma format: POSN#####W######,fix,HHMMSS,FL,...M##,WWWSS,..."""
    m = _RE_POSN_COMMA.search(text)
    if not m:
        return None
    lat = int(m.group(1)) / 1000.0
    lon = -int(m.group(2)) / 1000.0
    if not (20 <= lat <= 55 and -130 <= lon <= -60):
        return None
    altitude_ft = float(int(m.group(4)) * 100)
    temp_c = float(-int(m.group(5)))
    wind_dir = float(int(m.group(6)))
    wind_spd = float(int(m.group(7)))
    if wind_dir > 360 or wind_spd > 300:
        return None
    if altitude_ft <= 0 or altitude_ft > 60000:
        return None
    if temp_c < -80 or temp_c > 50:
        return None
    pressure = altitude_to_pressure(altitude_ft)
    return MetObservation(
        timestamp=ts, source="acars", source_id=flight or reg,
        lat=round(lat, 3), lon=round(lon, 3), altitude_ft=altitude_ft,
        pressure_hpa=round(pressure, 1), temp_c=temp_c,
        dewpoint_c=-9999.0,
        wind_dir_deg=wind_dir, wind_speed_kt=wind_spd,
        humidity_pct=None,
    )


def _try_parse_wn_02e16(text: str, ts: float, flight: str, reg: str) -> Optional[MetObservation]:
    """Parse WN/OO 02E16 VDL2 position format: 02E16[orig][dest]\\nN#####W###### HHMMFL## M## WWWSSS."""
    m = _RE_WN_02E16.search(text)
    if not m:
        return None
    lat = int(m.group(1)) / 1000.0
    lon = -int(m.group(2)) / 1000.0
    if not (20 <= lat <= 55 and -130 <= lon <= -60):
        return None
    altitude_ft = float(int(m.group(4)) * 10)  # FLXXX stored as XXX0 (3700 → 37000ft)
    temp_c = float(-int(m.group(5)))
    wind_dir = float(int(m.group(6)))
    wind_spd = float(int(m.group(7)))
    if wind_dir > 360 or wind_spd > 300:
        return None
    if altitude_ft <= 0 or altitude_ft > 60000:
        return None
    if temp_c < -80 or temp_c > 50:
        return None
    pressure = altitude_to_pressure(altitude_ft)
    return MetObservation(
        timestamp=ts, source="acars", source_id=flight or reg,
        lat=round(lat, 3), lon=round(lon, 3), altitude_ft=altitude_ft,
        pressure_hpa=round(pressure, 1), temp_c=temp_c,
        dewpoint_c=-9999.0,
        wind_dir_deg=wind_dir, wind_speed_kt=wind_spd,
        humidity_pct=None,
    )


def _try_parse_dl_compact(text: str, ts: float, flight: str, reg: str) -> Optional[MetObservation]:
    """Parse DL compact ACARS cruise report: LAT4 -LON4 FL3 -TAT -SAT DIR3 SPD2 [750511|AA-###]."""
    m = _RE_DL_COMPACT.search(text)
    if not m:
        return None
    lat_raw = m.group(1)
    lon_raw = m.group(2)
    # 3627 → 36.27, 08642 is four digits so use same scheme
    lat = int(lat_raw[:2]) + int(lat_raw[2:]) / 100.0
    lon = -(int(lon_raw[:2]) + int(lon_raw[2:]) / 100.0)
    if not (20 <= lat <= 55 and -130 <= lon <= -60):
        return None
    altitude_ft = float(int(m.group(3)) * 100)
    # First is TAT, second is SAT — use SAT (static air temp) for sounding
    temp_c = float(-int(m.group(5)))
    wind_dir = float(int(m.group(6)))
    wind_spd = float(int(m.group(7)))
    if wind_dir > 360 or wind_spd > 300:
        return None
    if altitude_ft <= 0 or altitude_ft > 60000:
        return None
    if temp_c < -80 or temp_c > 50:
        return None
    pressure = altitude_to_pressure(altitude_ft)
    return MetObservation(
        timestamp=ts, source="acars", source_id=flight or reg,
        lat=round(lat, 3), lon=round(lon, 3), altitude_ft=altitude_ft,
        pressure_hpa=round(pressure, 1), temp_c=temp_c,
        dewpoint_c=-9999.0,
        wind_dir_deg=wind_dir, wind_speed_kt=wind_spd,
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
        raw=msg,
    )

    def _finish(obs_list: List[MetObservation]) -> tuple:
        raw.decode_meta = _classify_acars_decode(label, text, raw.is_met, obs_list)
        return raw, obs_list

    # Try every parser — accept partial observations (temp-only, wind-only, etc.)
    # Structured formats first, then generic fallback.

    # Try #DFBD3M descent met profile (multi-observation per message)
    obs_list = _try_parse_dfbd3m(text, ts, flight, reg)
    if obs_list:
        raw.is_met = True
        return _finish(obs_list)

    # Try compressed ARINC 620 #M[12]BPOSN format (most common)
    obs = _try_parse_mbposn(text, ts, flight, reg)
    if obs:
        raw.is_met = True
        return _finish([obs])

    # Try #DFB*WXR weather report (multi-observation)
    obs_list = _try_parse_dfb_wxr(text, ts, flight, reg)
    if obs_list:
        raw.is_met = True
        return _finish(obs_list)

    # Try #DFB REP format (Republic/Delta Connection)
    obs = _try_parse_dfb_rep(text, ts, flight, reg)
    if obs:
        raw.is_met = True
        return _finish([obs])

    # Try label-22 position format (Frontier/Spirit)
    obs = _try_parse_label22(text, ts, flight, reg)
    if obs:
        raw.is_met = True
        return _finish([obs])

    # Try label-21 POSN format (Frontier-style) — accept even without temp
    obs = _try_parse_posn21(text, ts, flight, reg)
    if obs:
        raw.is_met = True
        return _finish([obs])

    # NW/DL POSN comma format (VDL2-heavy)
    obs = _try_parse_posn_comma(text, ts, flight, reg)
    if obs:
        raw.is_met = True
        return _finish([obs])

    # WN/OO 02E16 VDL2 position report
    obs = _try_parse_wn_02e16(text, ts, flight, reg)
    if obs:
        raw.is_met = True
        return _finish([obs])

    # DL compact ACARS cruise report
    obs = _try_parse_dl_compact(text, ts, flight, reg)
    if obs:
        raw.is_met = True
        return _finish([obs])

    # Fall back to legacy plain-text scrape — grab any FL/temp/wind we can find
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

    temp_c = -9999.0
    m = _RE_TEMP.search(upper)
    if m:
        temp_c = float(m.group(1))

    wind_dir, wind_spd = 0.0, 0.0
    m = _RE_WIND.search(upper)
    if m:
        wind_dir = float(m.group(1))
        wind_spd = float(m.group(2))

    humidity = None
    m = _RE_RH.search(upper)
    if m:
        humidity = float(m.group(1))

    # Accept if we have altitude + at least one met field
    has_met = temp_c > -9000 or wind_spd > 0 or humidity is not None
    if altitude_ft <= 0 or not has_met:
        return _finish([])

    dewpoint = dewpoint_from_rh(temp_c, humidity) if humidity and temp_c > -9000 else -9999.0
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
    return _finish([obs])


# ---------------------------------------------------------------------------
# Radiosonde telemetry parsing
# ---------------------------------------------------------------------------

def parse_radiosonde_telemetry(frame: dict) -> tuple:
    """Parse a radiosonde_auto_rx JSON telemetry frame.

    Accepts both raw telemetry format (id/lat/lon/alt/vel_h) and
    payload_summary format (callsign/latitude/longitude/altitude/speed).

    Returns (RawMessage, Optional[MetObservation]).
    """
    # Timestamp: raw uses "datetime" (ISO), payload_summary uses "time" (HH:MM:SS)
    ts = frame.get("datetime", "")
    if isinstance(ts, str) and ts:
        try:
            ts = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            ts = time.time()
    else:
        ts = time.time()

    # payload_summary uses "callsign", raw uses "id"/"serial"
    sonde_id = frame.get("callsign",
                         frame.get("id", frame.get("serial", "unknown")))
    # payload_summary has type="PAYLOAD_SUMMARY" and model="DFM17" etc;
    # raw telemetry has type="DFM17" directly
    sonde_type = frame.get("type", "")
    if sonde_type == "PAYLOAD_SUMMARY" or not sonde_type:
        sonde_type = frame.get("model", sonde_type)
    # payload_summary uses "latitude"/"longitude"/"altitude", raw uses "lat"/"lon"/"alt"
    lat = frame.get("lat", frame.get("latitude", 0.0))
    lon = frame.get("lon", frame.get("longitude", 0.0))
    alt_m = frame.get("alt", frame.get("altitude", 0.0))
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

    # Wind: raw provides vel_h (m/s), payload_summary provides speed (km/h)
    vel_h = frame.get("vel_h", None)
    if vel_h is not None:
        wind_speed_ms = vel_h
    else:
        speed_kmh = frame.get("speed", 0.0)
        wind_speed_ms = speed_kmh / 3.6 if speed_kmh > 0 else 0.0
    heading = frame.get("heading", 0.0)  # degrees, direction of travel
    # Wind direction is opposite to heading (wind blows FROM)
    wind_dir = (heading + 180) % 360
    wind_speed_kt = wind_speed_ms * 1.94384  # m/s to knots

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
            if self._message_count % 100 == 0:
                logger.debug(
                    "MetStore: %d total messages, %d in buffer (cap %d)",
                    self._message_count,
                    len(self._messages),
                    self._messages.maxlen,
                )

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
                        logger.warning("ACARS: malformed JSON discarded (%.80r)", line)
                        continue

                    raw, obs_list = parse_acars_message(msg)
                    logger.debug(
                        "ACARS stored: flight=%r label=%r fields=%d has_freq=%s has_level=%s",
                        msg.get("flight") or msg.get("tail", ""),
                        msg.get("label", ""),
                        len(msg),
                        "freq" in msg,
                        "level" in msg,
                    )
                    store.add_message(raw)
                    for obs in obs_list:
                        store.add_observation(obs)
        except FileNotFoundError:
            stop_event.wait(1)
        except Exception:
            logger.exception("ACARS reader error")
            stop_event.wait(2)

    logger.info("ACARS reader thread stopped")


def _extract_acars_from_vdl2(frame: dict) -> Optional[dict]:
    """Extract ACARS message fields from a dumpvdl2 JSON frame.

    dumpvdl2 nests ACARS inside: vdl2 → avlc → acars.
    Returns a dict compatible with parse_acars_message() or None.
    """
    vdl2 = frame.get("vdl2")
    if not isinstance(vdl2, dict):
        return None
    avlc = vdl2.get("avlc")
    if not isinstance(avlc, dict):
        return None
    acars = avlc.get("acars")
    if not isinstance(acars, dict):
        return None

    # Map dumpvdl2 field names to acarsdec-compatible names
    t = vdl2.get("t", {})
    ts = t.get("sec", 0) if isinstance(t, dict) else 0

    return {
        "timestamp": ts or time.time(),
        "flight": (acars.get("flight") or "").strip(),
        "tail": (acars.get("reg") or acars.get("tail") or "").strip(),
        "label": (acars.get("label") or "").strip(),
        "text": (acars.get("msg_text") or acars.get("message") or "").strip(),
    }


def vdl2_reader_worker(store: MetStore, stop_event: threading.Event) -> None:
    """Tail dumpvdl2 JSON-line output file and parse ACARS messages into store."""
    logger.info("VDL2 reader thread started")
    path = VDL2_OUTPUT_PATH

    while not stop_event.is_set():
        try:
            if not os.path.exists(path):
                stop_event.wait(1)
                continue

            with open(path, "r", encoding="utf-8", errors="ignore") as f:
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
                        frame = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("VDL2: malformed JSON discarded (%.80r)", line)
                        continue

                    # Classify the frame for logging
                    _vdl2_meta = frame.get("vdl2", {})
                    _avlc_meta = _vdl2_meta.get("avlc", {}) if isinstance(_vdl2_meta, dict) else {}
                    if isinstance(_avlc_meta, dict):
                        if "acars" in _avlc_meta:
                            _frame_kind = "ACARS"
                        elif "xid" in _avlc_meta:
                            _frame_kind = "XID"
                        elif "x25" in _avlc_meta:
                            _frame_kind = "CPDLC"
                        elif "gsif" in _avlc_meta:
                            _frame_kind = "GSIF"
                        elif _avlc_meta:
                            _frame_kind = "AVLC"
                        else:
                            _frame_kind = "unknown"
                    else:
                        _frame_kind = "unknown"
                    _freq_hz = _vdl2_meta.get("freq") if isinstance(_vdl2_meta, dict) else None
                    _sig = _vdl2_meta.get("sig_level") if isinstance(_vdl2_meta, dict) else None
                    logger.debug(
                        "VDL2 stored: type=%s freq=%s sig=%s",
                        _frame_kind,
                        f"{_freq_hz/1e6:.3f}MHz" if _freq_hz else "?",
                        f"{_sig:.1f}dBFS" if _sig is not None else "?",
                    )

                    acars_msg = _extract_acars_from_vdl2(frame)
                    if not acars_msg:
                        # Non-ACARS VDL2 frame (e.g. CPDLC, ADS-C) — log as raw
                        vdl2 = frame.get("vdl2", {})
                        t = vdl2.get("t", {})
                        ts = (t.get("sec", 0) if isinstance(t, dict) else 0) or time.time()
                        summary_text, decode_meta = _summarize_vdl2_non_acars_frame(frame)
                        raw = RawMessage(
                            timestamp=ts,
                            source="vdl2",
                            source_id="",
                            text=summary_text,
                            is_met=False,
                            raw=frame,
                            decode_meta=decode_meta,
                        )
                        store.add_message(raw)
                        continue

                    raw, obs_list = parse_acars_message(acars_msg)
                    # Tag source as vdl2 for the live feed; attach full frame for decode view
                    raw.source = "vdl2"
                    raw.raw = frame
                    store.add_message(raw)
                    for obs in obs_list:
                        obs.source = "vdl2"
                        store.add_observation(obs)
        except FileNotFoundError:
            stop_event.wait(1)
        except Exception:
            logger.exception("VDL2 reader error")
            stop_event.wait(2)

    logger.info("VDL2 reader thread stopped")


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
