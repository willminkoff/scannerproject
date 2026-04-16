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
import shlex
import subprocess
import time
from typing import Any

logger = logging.getLogger(__name__)


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
    return float(value) if value is not None else -9999.0


def _normalize_dewpoint_c(block: dict[str, Any], temp_c: float) -> float:
    _, _, _, dewpoint_from_rh = _load_wx_types()
    dewpoint = _coerce_float(_first_value(block, "dewpoint_c", "dew_point_c", "dewpoint", "dew_point"))
    if dewpoint is not None:
        units = str(_first_value(block, "dewpoint_unit", "dew_point_unit") or "").strip().lower()
        if units in {"f", "degf", "fahrenheit"}:
            dewpoint = (dewpoint - 32.0) * (5.0 / 9.0)
        return float(dewpoint)
    humidity = _normalize_humidity_pct(block)
    if humidity is not None and temp_c > -9000:
        return round(float(dewpoint_from_rh(temp_c, humidity)), 1)
    return -9999.0


def _normalize_wind_dir_deg(block: dict[str, Any]) -> float:
    direction = _coerce_float(_first_value(block, "wind_dir_deg", "wind_direction_deg", "wind_dir", "wind_direction"))
    if direction is None:
        return 0.0
    return float(direction) % 360.0


def _normalize_wind_speed_kt(block: dict[str, Any]) -> float:
    speed = _coerce_float(_first_value(block, "wind_speed_kt", "wind_speed", "wind_speed_kts"))
    if speed is not None:
        units = str(_first_value(block, "wind_speed_unit", "wind_units", "units") or "").strip().lower()
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
        return None
    units = str(_first_value(block, "humidity_unit", "rh_unit") or "").strip().lower()
    if units in {"fraction", "ratio"} or (0.0 <= humidity <= 1.0):
        humidity = humidity * 100.0
    return float(humidity)


def _normalize_lat_lon(block: dict[str, Any]) -> tuple[float, float]:
    lat = _coerce_float(_first_value(block, "lat", "latitude"))
    lon = _coerce_float(_first_value(block, "lon", "longitude", "lng"))
    return float(lat or 0.0), float(lon or 0.0)


def _collect_observation_blocks(decoded: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[int] = set()

    def _append_dict(item: Any) -> None:
        if not isinstance(item, dict):
            return
        marker = id(item)
        if marker in seen:
            return
        seen.add(marker)
        candidates.append(item)

    def _walk(node: Any, depth: int = 0) -> None:
        if depth > 3:
            return
        if isinstance(node, list):
            for item in node:
                _walk(item, depth + 1)
            return
        if not isinstance(node, dict):
            return
        for key in (
            "observations",
            "levels",
            "profile",
            "profiles",
            "reports",
            "met",
            "weather",
            "normalized",
            "observation",
            "level",
            "report",
        ):
            if key not in node:
                continue
            value = node.get(key)
            if isinstance(value, list):
                for item in value:
                    _append_dict(item)
            elif isinstance(value, dict):
                nested_list = None
                for nested_key in ("observations", "levels", "profile", "reports"):
                    if isinstance(value.get(nested_key), list):
                        nested_list = value.get(nested_key)
                        break
                if nested_list is not None:
                    for item in nested_list:
                        _append_dict(item)
                else:
                    _append_dict(value)
        _append_dict(node)

    _walk(decoded)
    return candidates


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
    token = _first_value(
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
        token = _first_value(
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
        _first_value(decoded, "summary", "text", "title", "description", "message") or ""
    ).strip()
    if summary:
        return summary
    prefix = "VDL2" if source == "vdl2" else "ACARS"
    if source_id:
        return f"{prefix} normalized met payload: {source_id}"
    return f"{prefix} normalized met payload"


def _bridge_decode_meta(decoded: dict[str, Any], backend_name: str) -> dict[str, Any]:
    title = str(_first_value(decoded, "title", "summary") or "libacars normalized payload").strip()
    body = str(
        _first_value(
            decoded,
            "description",
            "body",
            "summary",
            "message",
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
        _first_value(decoded, "timestamp", "time", "time_ms", "observed_at"),
        fallback_ts,
    )
    source_id = _normalize_source_id(decoded, payload)
    observations = []
    for block in _collect_observation_blocks(decoded):
        obs = _normalize_observation_block(
            block,
            source=source,
            source_id=source_id,
            timestamp=timestamp,
        )
        if obs is not None:
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


def decode_message_to_observations(msg: dict) -> tuple[Any | None, list[Any]]:
    backend = _get_backend()
    if not getattr(backend, "available", False):
        logger.debug("libacars bridge unavailable for acars message: %s", getattr(backend, "reason", "unknown"))
        return None, []
    decoded = backend.decode_message(msg)
    if not isinstance(decoded, dict):
        logger.debug("libacars bridge unsupported for acars message")
        return None, []
    raw, observations = _normalize_decoded_payload(msg, decoded, source="acars", backend_name=str(backend.name))
    if observations:
        logger.debug("libacars bridge produced %d ACARS observation(s)", len(observations))
        return raw, observations
    logger.debug("libacars bridge decode succeeded for acars message but yielded no met fields")
    return None, []


def decode_vdl2_frame_to_observations(frame: dict) -> tuple[Any | None, list[Any]]:
    backend = _get_backend()
    if not getattr(backend, "available", False):
        logger.debug("libacars bridge unavailable for vdl2 frame: %s", getattr(backend, "reason", "unknown"))
        return None, []
    decoded = backend.decode_vdl2_frame(frame)
    if not isinstance(decoded, dict):
        logger.debug("libacars bridge unsupported for vdl2 frame")
        return None, []
    raw, observations = _normalize_decoded_payload(frame, decoded, source="vdl2", backend_name=str(backend.name))
    if observations:
        logger.debug("libacars bridge produced %d VDL2 observation(s)", len(observations))
        return raw, observations
    logger.debug("libacars bridge decode succeeded for vdl2 frame but yielded no met fields")
    return None, []
