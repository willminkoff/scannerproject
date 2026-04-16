"""Shared libacars-backed normalization bridge for WX ingestion.

This module intentionally does not depend on any page/UI code. It exposes a
small API for normalizing structured libacars-style decode payloads into the
WX pipeline's RawMessage / MetObservation types.

Default behavior is a safe no-op when no backend is configured.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shlex
import subprocess
import time
from typing import Any

logger = logging.getLogger(__name__)

_OBSERVATION_CONTAINER_KEYS = {
    "observations",
    "levels",
    "profile",
    "profiles",
    "reports",
    "met",
    "weather",
    "normalized",
    "adsc",
    "wx",
    "sounding",
    "vertical_profile",
    "observation",
    "level",
    "report",
}
_OBSERVATION_DIRECT_KEYS = {
    "altitude",
    "altitude_ft",
    "altitude_m",
    "alt_ft",
    "alt_m",
    "height",
    "height_m",
    "flight_level",
    "fl",
    "temp",
    "temp_c",
    "temp_f",
    "temperature",
    "temperature_c",
    "temperature_f",
    "dewpoint",
    "dewpoint_c",
    "dewpoint_f",
    "dew_point",
    "humidity",
    "humidity_pct",
    "relative_humidity",
    "rh",
    "wind",
    "wind_dir",
    "wind_dir_deg",
    "wind_direction",
    "wind_speed",
    "wind_speed_kt",
    "wind_speed_ms",
    "pressure",
    "pressure_hpa",
    "pressure_mb",
    "lat",
    "latitude",
    "lon",
    "longitude",
    "lng",
}
_TEXT_CANDIDATE_KEYS = {
    "text",
    "message",
    "msg_text",
    "summary",
    "description",
    "body",
    "content",
    "decoded_text",
    "raw_text",
    "payload_text",
}
_SIGNED_TEMP_RE = re.compile(
    r"\b([MP])\s*(\d{1,3}(?:\.\d+)?)\b"
    r"|\b([+-]\d{1,3}(?:\.\d+)?)\b"
    r"|\b([+-]?\d{1,3}(?:\.\d+)?)\s*(?:C|DEGC)\b",
    re.I,
)
_WIND_RE = re.compile(
    r"\b(?:(?:WND|WIND|WS|SPD)[ :=-]*)?(\d{3})\s*(?:/|,|\s)\s*(\d{2,3})\b"
    r"|\b(\d{3})(\d{2,3})\b"
)
_RH_RE = re.compile(r"\b(?:RH|HUM|H)\s*[:= -]?\s*(\d{1,3}(?:\.\d+)?)\b", re.I)
_DP_RE = re.compile(r"\b(?:DP|DPT|DWPT|DEW(?:POINT)?)\s*[:= -]?\s*([MP]?\d{1,3}(?:\.\d+)?|[+-]?\d{1,3}(?:\.\d+)?)\b", re.I)
_ALT_RE = re.compile(
    r"\bFL\s*([0-9]{2,3})\b"
    r"|\bALT(?:ITUDE)?\s*[:= -]?\s*([0-9]{3,5})\s*(FT|FEET|M|METERS?)?\b"
    r"|\bA\s*([0-9]{4,5})\b"
    r"|\b([0-9]{5})\s*FT\b",
    re.I,
)
_LATLON_RE = re.compile(
    r"(\d{1,2}(?:\.\d+)?)\s*([NS])[^0-9A-Z]+(\d{1,3}(?:\.\d+)?)\s*([EW])",
    re.I,
)
_LATLON_COMPACT_RE = re.compile(r"(\d{5})([NS])?(\d{5,6})([EW])", re.I)
_DFB_SLASH_RE = re.compile(r"#DFB(?:/[^\r\n]+)+", re.I)


class _UnavailableBackend:
    name = "unavailable"
    available = False

    def __init__(self, reason: str):
        self.reason = str(reason or "unconfigured")

    def decode_message(self, msg: dict) -> dict | None:
        del msg
        return None

    def decode_vdl2_frame(self, frame: dict) -> dict | None:
        del frame
        return None


class _SubprocessBackend:
    name = "subprocess"
    available = True

    def __init__(self, command: list[str]):
        self._command = [str(part) for part in command if str(part).strip()]
        self.reason = ""

    def _invoke(self, mode: str, payload: dict) -> dict | None:
        if not self._command:
            return None
        try:
            proc = subprocess.run(
                self._command + [mode],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                timeout=5.0,
                check=False,
            )
        except Exception:
            logger.debug("libacars bridge subprocess failed for mode=%s", mode, exc_info=True)
            return None
        if proc.returncode != 0:
            stderr = str(proc.stderr or "").strip()
            logger.debug(
                "libacars bridge subprocess rc=%s mode=%s stderr=%r",
                proc.returncode,
                mode,
                stderr[:200],
            )
            return None
        stdout = str(proc.stdout or "").strip()
        if not stdout:
            return None
        try:
            decoded = json.loads(stdout)
        except Exception:
            logger.debug("libacars bridge subprocess emitted invalid JSON for mode=%s", mode, exc_info=True)
            return None
        return decoded if isinstance(decoded, dict) else None

    def decode_message(self, msg: dict) -> dict | None:
        return self._invoke("message", msg)

    def decode_vdl2_frame(self, frame: dict) -> dict | None:
        return self._invoke("vdl2", frame)


_BACKEND: Any = None


def _build_backend() -> Any:
    command = str(os.getenv("LIBACARS_BRIDGE_CMD", "")).strip()
    if command:
        return _SubprocessBackend(shlex.split(command))
    return _UnavailableBackend("no libacars bridge backend configured")


def _get_backend() -> Any:
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = _build_backend()
    return _BACKEND


def _load_wx_types():
    try:
        from .wxdata import MetObservation, RawMessage, altitude_to_pressure, dewpoint_from_rh
    except ImportError:
        from ui.wxdata import MetObservation, RawMessage, altitude_to_pressure, dewpoint_from_rh
    return MetObservation, RawMessage, altitude_to_pressure, dewpoint_from_rh


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
    else:
        text = str(value).strip()
        if not text:
            return None
        hemi = text[-1:].upper()
        if hemi in {"N", "S", "E", "W"}:
            number = _coerce_float(text[:-1])
            if number is None:
                return None
            if hemi in {"S", "W"}:
                number = -abs(number)
            return number
        try:
            parsed = float(text)
        except Exception:
            return None
    if math.isnan(parsed) or math.isinf(parsed):
        return None
    return parsed


def _first_value(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            value = mapping.get(key)
            if value is not None and str(value).strip() != "":
                return value
    return None


def _first_recursive_value(node: Any, *keys: str, _depth: int = 0) -> Any:
    if _depth > 5:
        return None
    if isinstance(node, dict):
        value = _first_value(node, *keys)
        if value is not None:
            return value
        for child in node.values():
            value = _first_recursive_value(child, *keys, _depth=_depth + 1)
            if value is not None:
                return value
    elif isinstance(node, list):
        for child in node:
            value = _first_recursive_value(child, *keys, _depth=_depth + 1)
            if value is not None:
                return value
    return None


def _first_child_dict(mapping: dict[str, Any], *keys: str) -> dict[str, Any] | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, dict):
            return value
    return None


def _coerce_timestamp(value: Any, fallback: float) -> float:
    if value is None:
        return float(fallback)
    if isinstance(value, (int, float)):
        parsed = float(value)
        if parsed > 1e12:
            parsed = parsed / 1000.0
        return parsed if parsed > 0 else float(fallback)
    text = str(value).strip()
    if not text:
        return float(fallback)
    try:
        parsed = float(text)
        if parsed > 1e12:
            parsed = parsed / 1000.0
        return parsed if parsed > 0 else float(fallback)
    except Exception:
        return float(fallback)


def _normalize_altitude_ft(block: dict[str, Any]) -> float:
    direct = _coerce_float(
        _first_value(block, "altitude_ft", "alt_ft", "flight_level_ft")
    )
    if direct and direct > 0:
        return float(direct)

    flight_level = _coerce_float(_first_value(block, "flight_level", "fl"))
    if flight_level and flight_level > 0:
        return float(flight_level) * 100.0

    meters = _coerce_float(_first_value(block, "altitude_m", "alt_m", "height_m"))
    if meters and meters > 0:
        return float(meters) / 0.3048

    alt_block = _first_child_dict(block, "altitude", "alt", "height", "level")
    if isinstance(alt_block, dict):
        direct = _coerce_float(_first_value(alt_block, "ft", "feet", "altitude_ft", "value_ft"))
        if direct and direct > 0:
            return float(direct)
        flight_level = _coerce_float(_first_value(alt_block, "fl", "flight_level", "level"))
        if flight_level and flight_level > 0:
            return float(flight_level) * 100.0
        meters = _coerce_float(_first_value(alt_block, "m", "meters", "metres", "altitude_m", "value_m"))
        if meters and meters > 0:
            return float(meters) / 0.3048
        generic = _coerce_float(_first_value(alt_block, "value", "altitude", "alt", "height"))
        if generic and generic > 0:
            units = str(_first_value(alt_block, "unit", "units") or "").strip().lower()
            if units in {"m", "meter", "meters", "metre", "metres"}:
                return float(generic) / 0.3048
            if units in {"fl", "flight_level", "flightlevel"}:
                return float(generic) * 100.0
            if generic <= 650.0 and float(generic).is_integer():
                return float(generic) * 100.0
            return float(generic)

    generic = _coerce_float(_first_value(block, "altitude", "alt", "height"))
    if not generic or generic <= 0:
        return 0.0
    units = str(_first_value(block, "altitude_unit", "altitude_units", "alt_unit", "units") or "").strip().lower()
    if units in {"m", "meter", "meters", "metre", "metres"}:
        return float(generic) / 0.3048
    if generic <= 650.0 and float(generic).is_integer():
        return float(generic) * 100.0
    return float(generic)


def _normalize_pressure_hpa(block: dict[str, Any], altitude_ft: float) -> float:
    _, _, altitude_to_pressure, _ = _load_wx_types()
    pressure = _coerce_float(_first_value(block, "pressure_hpa", "pressure_mb", "pressure", "press"))
    if pressure and pressure > 0:
        units = str(_first_value(block, "pressure_unit", "pressure_units") or "").strip().lower()
        if units in {"pa", "pascal", "pascals"}:
            pressure = pressure / 100.0
        elif units in {"kpa"}:
            pressure = pressure * 10.0
        return round(float(pressure), 1)
    pressure_block = _first_child_dict(block, "pressure")
    if isinstance(pressure_block, dict):
        pressure = _coerce_float(_first_value(pressure_block, "hpa", "mb", "value", "pressure"))
        if pressure and pressure > 0:
            units = str(_first_value(pressure_block, "unit", "units") or "").strip().lower()
            if units in {"pa", "pascal", "pascals"}:
                pressure = pressure / 100.0
            elif units in {"kpa"}:
                pressure = pressure * 10.0
            return round(float(pressure), 1)
    if altitude_ft > 0:
        return round(float(altitude_to_pressure(altitude_ft)), 1)
    return 0.0


def _normalize_temperature_c(block: dict[str, Any]) -> float:
    value = _coerce_float(_first_value(block, "temp_c", "temperature_c", "temperature", "temp"))
    if value is None:
        units = str(_first_value(block, "temp_unit", "temperature_unit", "temperature_units") or "").strip().lower()
        value_f = _coerce_float(_first_value(block, "temp_f", "temperature_f"))
        if value_f is not None:
            value = (value_f - 32.0) * (5.0 / 9.0)
        elif units in {"f", "degf", "fahrenheit"}:
            raw = _coerce_float(_first_value(block, "temperature", "temp"))
            if raw is not None:
                value = (raw - 32.0) * (5.0 / 9.0)
    if value is None:
        temp_block = _first_child_dict(block, "temperature", "temp", "air_temperature")
        if isinstance(temp_block, dict):
            value = _coerce_float(_first_value(temp_block, "c", "temp_c", "value", "temperature"))
            units = str(_first_value(temp_block, "unit", "units") or "").strip().lower()
            if value is None:
                value = _coerce_float(_first_value(temp_block, "f", "temp_f", "temperature_f"))
                if value is not None:
                    value = (value - 32.0) * (5.0 / 9.0)
            elif units in {"f", "degf", "fahrenheit"}:
                value = (value - 32.0) * (5.0 / 9.0)
    return float(value) if value is not None else -9999.0


def _normalize_dewpoint_c(block: dict[str, Any], temp_c: float) -> float:
    _, _, _, dewpoint_from_rh = _load_wx_types()
    dewpoint = _coerce_float(_first_value(block, "dewpoint_c", "dew_point_c", "dewpoint", "dew_point"))
    if dewpoint is not None:
        units = str(_first_value(block, "dewpoint_unit", "dew_point_unit") or "").strip().lower()
        if units in {"f", "degf", "fahrenheit"}:
            dewpoint = (dewpoint - 32.0) * (5.0 / 9.0)
        return float(dewpoint)
    dew_block = _first_child_dict(block, "dewpoint", "dew_point")
    if isinstance(dew_block, dict):
        dewpoint = _coerce_float(_first_value(dew_block, "c", "dewpoint_c", "value", "dewpoint"))
        units = str(_first_value(dew_block, "unit", "units") or "").strip().lower()
        if dewpoint is None:
            dewpoint = _coerce_float(_first_value(dew_block, "f", "dewpoint_f"))
            if dewpoint is not None:
                dewpoint = (dewpoint - 32.0) * (5.0 / 9.0)
        elif units in {"f", "degf", "fahrenheit"}:
            dewpoint = (dewpoint - 32.0) * (5.0 / 9.0)
        if dewpoint is not None:
            return float(dewpoint)
    humidity = _normalize_humidity_pct(block)
    if humidity is not None and temp_c > -9000:
        return round(float(dewpoint_from_rh(temp_c, humidity)), 1)
    return -9999.0


def _normalize_wind_dir_deg(block: dict[str, Any]) -> float:
    direction = _coerce_float(_first_value(block, "wind_dir_deg", "wind_direction_deg", "wind_dir", "wind_direction"))
    if direction is None:
        wind_block = _first_child_dict(block, "wind")
        if isinstance(wind_block, dict):
            direction = _coerce_float(
                _first_value(wind_block, "direction_deg", "direction", "dir_deg", "dir", "heading_from")
            )
    if direction is None:
        return 0.0
    return float(direction) % 360.0


def _normalize_wind_speed_kt(block: dict[str, Any]) -> float:
    speed = _coerce_float(_first_value(block, "wind_speed_kt", "wind_speed", "wind_speed_kts"))
    units = str(_first_value(block, "wind_speed_unit", "wind_units", "units") or "").strip().lower()
    if speed is not None:
        if units in {"m/s", "mps", "ms"}:
            speed = speed * 1.94384
        elif units in {"km/h", "kmh", "kph"}:
            speed = speed * 0.539957
        return float(speed)

    wind_block = _first_child_dict(block, "wind")
    if isinstance(wind_block, dict):
        speed = _coerce_float(
            _first_value(wind_block, "speed_kt", "speed_kts", "kt", "kts", "knots", "speed", "value")
        )
        units = str(_first_value(wind_block, "unit", "units") or "").strip().lower()
        if speed is not None:
            if units in {"m/s", "mps", "ms"}:
                speed = speed * 1.94384
            elif units in {"km/h", "kmh", "kph"}:
                speed = speed * 0.539957
            return float(speed)

    speed_ms = _coerce_float(_first_value(block, "wind_speed_ms", "wind_ms"))
    if speed_ms is not None:
        return float(speed_ms) * 1.94384
    speed_kmh = _coerce_float(_first_value(block, "wind_speed_kmh", "wind_kmh"))
    if speed_kmh is not None:
        return float(speed_kmh) * 0.539957
    return 0.0


def _normalize_humidity_pct(block: dict[str, Any]) -> float | None:
    humidity = _coerce_float(_first_value(block, "humidity_pct", "humidity", "relative_humidity", "rh"))
    if humidity is None:
        humidity_block = _first_child_dict(block, "humidity", "relative_humidity", "rh")
        if isinstance(humidity_block, dict):
            humidity = _coerce_float(_first_value(humidity_block, "pct", "percent", "value", "humidity"))
            units = str(_first_value(humidity_block, "unit", "units") or "").strip().lower()
            if humidity is not None and units in {"fraction", "ratio"}:
                humidity = humidity * 100.0
        if humidity is None:
            return None
    units = str(_first_value(block, "humidity_unit", "rh_unit") or "").strip().lower()
    if units in {"fraction", "ratio"} or (0.0 <= humidity <= 1.0):
        humidity = humidity * 100.0
    return float(humidity)


def _normalize_lat_lon(block: dict[str, Any]) -> tuple[float, float]:
    lat = _coerce_float(_first_value(block, "lat", "latitude"))
    lon = _coerce_float(_first_value(block, "lon", "longitude", "lng"))
    if lat is None or lon is None:
        position_block = _first_child_dict(block, "position", "location", "coords", "coordinates")
        if isinstance(position_block, dict):
            if lat is None:
                lat = _coerce_float(_first_value(position_block, "lat", "latitude"))
            if lon is None:
                lon = _coerce_float(_first_value(position_block, "lon", "longitude", "lng"))
    return float(lat or 0.0), float(lon or 0.0)


def _collect_observation_blocks(decoded: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[int] = set()

    def _append_dict(item: Any, parent_key: str = "") -> None:
        if not isinstance(item, dict):
            return
        marker = id(item)
        if marker in seen:
            return
        keys = {str(key).strip().lower() for key in item.keys()}
        should_keep = bool(keys & _OBSERVATION_DIRECT_KEYS) or parent_key in _OBSERVATION_CONTAINER_KEYS
        if not should_keep:
            should_keep = any(
                isinstance(value, dict) and ({str(key).strip().lower() for key in value.keys()} & _OBSERVATION_DIRECT_KEYS)
                for value in item.values()
            )
        if not should_keep:
            return
        seen.add(marker)
        candidates.append(item)

    def _walk(node: Any, depth: int = 0, parent_key: str = "") -> None:
        if depth > 3:
            return
        if isinstance(node, list):
            for item in node:
                _walk(item, depth + 1, parent_key)
            return
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            key_token = str(key).strip().lower()
            if isinstance(value, list):
                for item in value:
                    _append_dict(item, key_token)
                    _walk(item, depth + 1, key_token)
            elif isinstance(value, dict):
                _append_dict(value, key_token)
                _walk(value, depth + 1, key_token)
        _append_dict(node, parent_key)

    _walk(decoded, parent_key="")
    return candidates


def _extract_text_candidates(node: Any, _depth: int = 0) -> list[str]:
    if _depth > 5:
        return []
    texts: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            key_token = str(key).strip().lower()
            if key_token in _TEXT_CANDIDATE_KEYS and isinstance(value, str) and value.strip():
                texts.append(value.strip())
            texts.extend(_extract_text_candidates(value, _depth + 1))
    elif isinstance(node, list):
        for value in node:
            texts.extend(_extract_text_candidates(value, _depth + 1))
    elif isinstance(node, str):
        text = node.strip()
        if text and "#DFB/" in text.upper():
            texts.append(text)
    return texts


def _parse_compact_temp(token: str) -> float | None:
    text = str(token or "").strip()
    if not text:
        return None
    match = _SIGNED_TEMP_RE.search(text)
    if not match:
        return None
    if match.group(1) and match.group(2):
        sign = -1.0 if match.group(1).upper() == "M" else 1.0
        return sign * float(match.group(2))
    if match.group(3):
        return float(match.group(3))
    if match.group(4):
        return float(match.group(4))
    return None


def _parse_altitude_from_text(text: str) -> float:
    match = _ALT_RE.search(text or "")
    if not match:
        return 0.0
    if match.group(1):
        return float(match.group(1)) * 100.0
    if match.group(2):
        value = float(match.group(2))
        units = str(match.group(3) or "").strip().lower()
        if units in {"m", "meter", "meters"}:
            return value / 0.3048
        return value
    if match.group(4):
        return float(match.group(4))
    if match.group(5):
        return float(match.group(5))
    return 0.0


def _parse_wind_from_text(text: str) -> tuple[float, float]:
    match = _WIND_RE.search(text or "")
    if not match:
        return 0.0, 0.0
    if match.group(1) and match.group(2):
        return float(match.group(1)), float(match.group(2))
    if match.group(3) and match.group(4):
        return float(match.group(3)), float(match.group(4))
    return 0.0, 0.0


def _parse_humidity_from_text(text: str) -> float | None:
    match = _RH_RE.search(text or "")
    if not match:
        return None
    return float(match.group(1))


def _parse_dewpoint_from_text(text: str) -> float:
    match = _DP_RE.search(text or "")
    if not match:
        return -9999.0
    value = _parse_compact_temp(match.group(1))
    return float(value) if value is not None else -9999.0


def _parse_lat_lon_from_text(text: str) -> tuple[float, float]:
    match = _LATLON_RE.search(text or "")
    if match:
        lat = float(match.group(1))
        lon = float(match.group(3))
        if match.group(2).upper() == "S":
            lat = -lat
        if match.group(4).upper() == "W":
            lon = -lon
        return lat, lon
    match = _LATLON_COMPACT_RE.search(text or "")
    if match:
        lat = float(match.group(1)) / 1000.0
        lon = float(match.group(3)) / 1000.0
        if (match.group(2) or "").upper() == "S":
            lat = -lat
        if match.group(4).upper() == "W":
            lon = -lon
        return lat, lon
    return 0.0, 0.0


def _dfb_slash_levels_from_text(text: str, *, source: str, source_id: str, timestamp: float):
    MetObservation, _, _, dewpoint_from_rh = _load_wx_types()
    payloads = _DFB_SLASH_RE.findall(text or "")
    observations = []
    seen: set[tuple[Any, ...]] = set()
    for payload in payloads:
        blocks = [part.strip() for part in re.split(r"/(?=[A-Z#])", payload) if part.strip()]
        context_lat, context_lon = 0.0, 0.0
        for block in blocks:
            upper = block.upper()
            if upper.startswith("#DFB"):
                continue
            lat, lon = _parse_lat_lon_from_text(block)
            if lat or lon:
                context_lat, context_lon = lat, lon
            if not upper.startswith(("S", "T")):
                continue
            altitude_ft = _parse_altitude_from_text(block)
            if altitude_ft <= 0:
                continue
            temp_c = _parse_compact_temp(block)
            if temp_c is None:
                temp_c = -9999.0
            dewpoint_c = _parse_dewpoint_from_text(block)
            wind_dir_deg, wind_speed_kt = _parse_wind_from_text(block)
            humidity_pct = _parse_humidity_from_text(block)
            if dewpoint_c <= -9000.0 and humidity_pct is not None and temp_c > -9000.0:
                dewpoint_c = round(float(dewpoint_from_rh(temp_c, humidity_pct)), 1)
            has_met = (
                temp_c > -9000.0
                or dewpoint_c > -9000.0
                or humidity_pct is not None
                or wind_speed_kt > 0.0
            )
            if not has_met:
                continue
            marker = (
                round(altitude_ft, 1),
                round(temp_c, 1),
                round(dewpoint_c, 1),
                round(wind_dir_deg, 1),
                round(wind_speed_kt, 1),
                round(humidity_pct or -1.0, 1),
            )
            if marker in seen:
                continue
            seen.add(marker)
            observations.append(
                MetObservation(
                    timestamp=float(timestamp),
                    source=str(source),
                    source_id=str(source_id),
                    lat=float(lat or context_lat),
                    lon=float(lon or context_lon),
                    altitude_ft=float(altitude_ft),
                    pressure_hpa=float(_normalize_pressure_hpa({}, altitude_ft)),
                    temp_c=float(temp_c),
                    dewpoint_c=float(dewpoint_c),
                    wind_dir_deg=float(wind_dir_deg),
                    wind_speed_kt=float(wind_speed_kt),
                    humidity_pct=humidity_pct,
                )
            )
    return observations


def _normalize_observation_block(
    block: dict[str, Any],
    *,
    source: str,
    source_id: str,
    timestamp: float,
):
    MetObservation, _, _, _ = _load_wx_types()
    altitude_ft = _normalize_altitude_ft(block)
    if altitude_ft <= 0:
        return None
    temp_c = _normalize_temperature_c(block)
    dewpoint_c = _normalize_dewpoint_c(block, temp_c)
    wind_dir_deg = _normalize_wind_dir_deg(block)
    wind_speed_kt = _normalize_wind_speed_kt(block)
    humidity_pct = _normalize_humidity_pct(block)
    has_met = (
        temp_c > -9000.0
        or dewpoint_c > -9000.0
        or wind_speed_kt > 0.0
        or humidity_pct is not None
    )
    if not has_met:
        return None
    lat, lon = _normalize_lat_lon(block)
    pressure_hpa = _normalize_pressure_hpa(block, altitude_ft)
    return MetObservation(
        timestamp=float(timestamp),
        source=str(source),
        source_id=str(source_id),
        lat=lat,
        lon=lon,
        altitude_ft=float(altitude_ft),
        pressure_hpa=float(pressure_hpa),
        temp_c=float(temp_c),
        dewpoint_c=float(dewpoint_c),
        wind_dir_deg=float(wind_dir_deg),
        wind_speed_kt=float(wind_speed_kt),
        humidity_pct=humidity_pct,
    )


def _normalize_source_id(decoded: dict[str, Any], payload: dict[str, Any]) -> str:
    token = _first_recursive_value(
        decoded,
        "source_id",
        "flight",
        "tail",
        "reg",
        "callsign",
        "station",
        "aircraft_id",
    )
    if token is None:
        token = _first_recursive_value(
            payload,
            "source_id",
            "flight",
            "tail",
            "reg",
            "callsign",
            "station",
            "aircraft_id",
        )
    return str(token or "").strip()


def _bridge_summary_text(decoded: dict[str, Any], source: str, source_id: str) -> str:
    summary = str(
        _first_recursive_value(decoded, "summary", "text", "title", "description", "message", "msg_text") or ""
    ).strip()
    if summary:
        return summary
    prefix = "VDL2" if source == "vdl2" else "ACARS"
    if source_id:
        return f"{prefix} normalized met payload: {source_id}"
    return f"{prefix} normalized met payload"


def _bridge_decode_meta(decoded: dict[str, Any], backend_name: str) -> dict[str, Any]:
    title = str(_first_recursive_value(decoded, "title", "summary") or "libacars normalized payload").strip()
    body = str(
        _first_recursive_value(
            decoded,
            "description",
            "body",
            "summary",
            "message",
            "msg_text",
        ) or "Structured libacars decode normalized into sounding observations."
    ).strip()
    return {
        "protocol_family": "libacars_bridge",
        "title": title,
        "body": body,
        "confidence": "medium",
        "backend": backend_name,
    }


def _normalize_decoded_payload(payload: dict[str, Any], decoded: dict[str, Any], *, source: str, backend_name: str):
    _, RawMessage, _, _ = _load_wx_types()
    fallback_ts = _coerce_timestamp(payload.get("timestamp"), time.time())
    timestamp = _coerce_timestamp(
        _first_recursive_value(decoded, "timestamp", "time", "time_ms", "observed_at", "sec", "epoch"),
        fallback_ts,
    )
    source_id = _normalize_source_id(decoded, payload)
    observations = []
    seen: set[tuple[Any, ...]] = set()
    for block in _collect_observation_blocks(decoded):
        obs = _normalize_observation_block(
            block,
            source=source,
            source_id=source_id,
            timestamp=timestamp,
        )
        if obs is not None:
            marker = (
                round(float(obs.altitude_ft), 1),
                round(float(obs.temp_c), 1),
                round(float(obs.dewpoint_c), 1),
                round(float(obs.wind_dir_deg), 1),
                round(float(obs.wind_speed_kt), 1),
                round(float(obs.humidity_pct or -1.0), 1),
            )
            if marker in seen:
                continue
            seen.add(marker)
            observations.append(obs)
    for text in _extract_text_candidates(decoded) + _extract_text_candidates(payload):
        for obs in _dfb_slash_levels_from_text(text, source=source, source_id=source_id, timestamp=timestamp):
            marker = (
                round(float(obs.altitude_ft), 1),
                round(float(obs.temp_c), 1),
                round(float(obs.dewpoint_c), 1),
                round(float(obs.wind_dir_deg), 1),
                round(float(obs.wind_speed_kt), 1),
                round(float(obs.humidity_pct or -1.0), 1),
            )
            if marker in seen:
                continue
            seen.add(marker)
            observations.append(obs)
    if not observations:
        return None, []
    raw = RawMessage(
        timestamp=float(timestamp),
        source=str(source),
        source_id=str(source_id),
        text=_bridge_summary_text(decoded, source, source_id),
        is_met=True,
        raw=payload,
        decode_meta=_bridge_decode_meta(decoded, backend_name),
    )
    return raw, observations


def _decode_local_text_payload(payload: dict[str, Any], *, source: str) -> tuple[Any | None, list[Any]]:
    raw, observations = _normalize_decoded_payload(
        payload,
        payload,
        source=source,
        backend_name="local_text",
    )
    if observations:
        logger.debug(
            "libacars bridge local text parser produced %d %s observation(s)",
            len(observations),
            source.upper(),
        )
        return raw, observations
    return None, []


def decode_message_to_observations(msg: dict) -> tuple[Any | None, list[Any]]:
    backend = _get_backend()
    if getattr(backend, "available", False):
        decoded = backend.decode_message(msg)
        if isinstance(decoded, dict):
            raw, observations = _normalize_decoded_payload(msg, decoded, source="acars", backend_name=str(backend.name))
            if observations:
                logger.debug("libacars bridge produced %d ACARS observation(s)", len(observations))
                return raw, observations
            logger.debug("libacars bridge decode succeeded for acars message but yielded no met fields")
        else:
            logger.debug("libacars bridge unsupported for acars message")
    else:
        logger.debug("libacars bridge unavailable for acars message: %s", getattr(backend, "reason", "unknown"))
    return _decode_local_text_payload(msg, source="acars")


def decode_vdl2_frame_to_observations(frame: dict) -> tuple[Any | None, list[Any]]:
    backend = _get_backend()
    if getattr(backend, "available", False):
        decoded = backend.decode_vdl2_frame(frame)
        if isinstance(decoded, dict):
            raw, observations = _normalize_decoded_payload(frame, decoded, source="vdl2", backend_name=str(backend.name))
            if observations:
                logger.debug("libacars bridge produced %d VDL2 observation(s)", len(observations))
                return raw, observations
            logger.debug("libacars bridge decode succeeded for vdl2 frame but yielded no met fields")
        else:
            logger.debug("libacars bridge unsupported for vdl2 frame")
    else:
        logger.debug("libacars bridge unavailable for vdl2 frame: %s", getattr(backend, "reason", "unknown"))
    return _decode_local_text_payload(frame, source="vdl2")
