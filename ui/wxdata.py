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
from typing import Optional, List, Dict

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


# ---------------------------------------------------------------------------
# ACARS message parsing
# ---------------------------------------------------------------------------

# AMDAR meteorological labels — H1 is most common
_AMDAR_LABELS = {"H1", "H2", "4A", "44", "SA"}

# Pattern for AMDAR-style met reports in message text
_RE_FL = re.compile(r'FL\s*(\d{2,3})')
_RE_TEMP = re.compile(r'([+-]?\d{1,3}(?:\.\d)?)\s*C\b')
_RE_WIND = re.compile(r'(\d{3})\s*/\s*(\d{1,3})\s*(?:KT|KTS)')
_RE_RH = re.compile(r'RH\s*(\d{1,3})')
_RE_LAT = re.compile(r'(\d{1,2}(?:\.\d+)?)\s*([NS])')
_RE_LON = re.compile(r'(\d{1,3}(?:\.\d+)?)\s*([EW])')
_RE_ALT = re.compile(r'(\d{4,5})\s*(?:FT|M)')


def parse_acars_message(msg: dict) -> tuple:
    """Parse an acarsdec JSON message. Returns (RawMessage, Optional[MetObservation])."""
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

    # Check if this is a meteorological message
    if label not in _AMDAR_LABELS:
        return raw, None

    upper = text.upper()

    # Try to extract met data
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

    # Flight level → altitude
    altitude_ft = 0.0
    m = _RE_FL.search(upper)
    if m:
        altitude_ft = float(m.group(1)) * 100
    else:
        m = _RE_ALT.search(upper)
        if m:
            altitude_ft = float(m.group(1))

    if altitude_ft <= 0:
        return raw, None

    # Temperature
    temp_c = -9999.0
    m = _RE_TEMP.search(upper)
    if m:
        temp_c = float(m.group(1))

    if temp_c == -9999.0:
        return raw, None

    # Wind
    wind_dir, wind_spd = 0.0, 0.0
    m = _RE_WIND.search(upper)
    if m:
        wind_dir = float(m.group(1))
        wind_spd = float(m.group(2))

    # Humidity
    humidity = None
    m = _RE_RH.search(upper)
    if m:
        humidity = float(m.group(1))

    # Dewpoint
    dewpoint = dewpoint_from_rh(temp_c, humidity) if humidity else -9999.0

    pressure = altitude_to_pressure(altitude_ft)

    raw.is_met = True
    obs = MetObservation(
        timestamp=ts,
        source="acars",
        source_id=flight or reg,
        lat=lat,
        lon=lon,
        altitude_ft=altitude_ft,
        pressure_hpa=round(pressure, 1),
        temp_c=temp_c,
        dewpoint_c=round(dewpoint, 1),
        wind_dir_deg=wind_dir,
        wind_speed_kt=wind_spd,
        humidity_pct=humidity,
    )
    return raw, obs


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
        self._last_message_time = 0.0

    def add_message(self, msg: RawMessage) -> None:
        with self._lock:
            self._messages.append(msg)
            self._message_count += 1
            self._last_message_time = msg.timestamp

    def add_observation(self, obs: MetObservation) -> None:
        with self._lock:
            self._met_obs.append(obs)
            self._met_count += 1

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
        obs.sort(key=lambda o: o.altitude_ft)
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
            return {
                "collecting": self.collecting,
                "active_decoder": self.active_decoder,
                "message_count": self._message_count,
                "met_count": self._met_count,
                "last_message_time": self._last_message_time,
            }

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

                    raw, obs = parse_acars_message(msg)
                    store.add_message(raw)
                    if obs:
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
