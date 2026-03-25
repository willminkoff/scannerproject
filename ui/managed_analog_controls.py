"""Persistent control policy for managed HP favorites analog profiles."""
from __future__ import annotations

import json
import math
import os
import time
from typing import Any

try:
    from .config import (
        MANAGED_ANALOG_CONTROLS_PATH,
        MANAGED_ANALOG_DEFAULT_AIRBAND_DBFS,
        MANAGED_ANALOG_DEFAULT_GROUND_DBFS,
    )
    from .profile_config import managed_controls_profile_path, parse_controls, write_controls
except ImportError:
    from ui.config import (
        MANAGED_ANALOG_CONTROLS_PATH,
        MANAGED_ANALOG_DEFAULT_AIRBAND_DBFS,
        MANAGED_ANALOG_DEFAULT_GROUND_DBFS,
    )
    from ui.profile_config import managed_controls_profile_path, parse_controls, write_controls


_DEFAULT_GAIN = 32.8
_DEFAULT_SQUELCH_SNR = 10.0
_DEFAULT_STATE = {"version": 1, "targets": {}}


def _normalize_target(target: str) -> str:
    token = str(target or "").strip().lower()
    if token not in ("airband", "ground"):
        raise ValueError(f"unknown target: {target}")
    return token


def _controls_path() -> str:
    return str(MANAGED_ANALOG_CONTROLS_PATH or "").strip()


def _default_dbfs(target: str) -> float:
    normalized = _normalize_target(target)
    if normalized == "airband":
        return float(MANAGED_ANALOG_DEFAULT_AIRBAND_DBFS)
    return float(MANAGED_ANALOG_DEFAULT_GROUND_DBFS)


def _normalize_float(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = float(fallback)
    if not math.isfinite(parsed):
        parsed = float(fallback)
    return float(parsed)


def _empty_state() -> dict[str, Any]:
    return {"version": 1, "targets": {}}


def _load_state() -> dict[str, Any]:
    path = _controls_path()
    if not path or not os.path.isfile(path):
        return _empty_state()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return _empty_state()
    if not isinstance(payload, dict):
        return _empty_state()
    targets = payload.get("targets")
    if not isinstance(targets, dict):
        payload["targets"] = {}
    payload["version"] = int(payload.get("version") or 1)
    return payload


def _save_state(payload: dict[str, Any]) -> None:
    path = _controls_path()
    if not path:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _same_path(left: str, right: str) -> bool:
    if not left or not right:
        return False
    try:
        return os.path.realpath(left) == os.path.realpath(right)
    except Exception:
        return False


def is_managed_controls_profile(target: str, conf_path: str) -> bool:
    normalized = _normalize_target(target)
    managed_path = managed_controls_profile_path(normalized)
    return bool(managed_path and conf_path and _same_path(managed_path, conf_path))


def _controls_payload(
    *,
    gain: float,
    squelch_mode: str,
    squelch_snr: float,
    squelch_dbfs: float,
) -> dict[str, Any]:
    return {
        "gain": _normalize_float(gain, _DEFAULT_GAIN),
        "squelch_mode": "dbfs" if str(squelch_mode or "").strip().lower() != "snr" else "snr",
        "squelch_snr": max(0.0, _normalize_float(squelch_snr, _DEFAULT_SQUELCH_SNR)),
        "squelch_dbfs": _normalize_float(squelch_dbfs, -70.0),
    }


def _fallback_controls(target: str, conf_path: str) -> dict[str, Any]:
    gain = _DEFAULT_GAIN
    squelch_snr = _DEFAULT_SQUELCH_SNR
    try:
        gain, squelch_snr, _current_dbfs, _mode = parse_controls(conf_path)
    except Exception:
        pass
    payload = _controls_payload(
        gain=gain,
        squelch_mode="dbfs",
        squelch_snr=squelch_snr,
        squelch_dbfs=_default_dbfs(target),
    )
    payload["source"] = "default"
    return payload


def recommended_managed_controls(target: str, conf_path: str) -> dict[str, Any]:
    normalized = _normalize_target(target)
    controls = _fallback_controls(normalized, conf_path)
    state = _load_state()
    record = dict(((state.get("targets") or {}).get(normalized)) or {})
    if not _same_path(str(record.get("profile_path") or ""), conf_path):
        return controls
    override = record.get("override")
    if not isinstance(override, dict):
        return controls
    payload = _controls_payload(
        gain=override.get("gain"),
        squelch_mode=override.get("squelch_mode"),
        squelch_snr=override.get("squelch_snr"),
        squelch_dbfs=override.get("squelch_dbfs"),
    )
    payload["source"] = "override"
    return payload


def apply_managed_profile_controls(target: str, conf_path: str) -> dict[str, Any]:
    normalized = _normalize_target(target)
    if not is_managed_controls_profile(normalized, conf_path):
        return {"managed": False, "changed": False, "source": ""}
    controls = recommended_managed_controls(normalized, conf_path)
    changed = bool(
        write_controls(
            conf_path,
            float(controls.get("gain") or _DEFAULT_GAIN),
            str(controls.get("squelch_mode") or "dbfs"),
            float(controls.get("squelch_snr") or _DEFAULT_SQUELCH_SNR),
            float(controls.get("squelch_dbfs") or _default_dbfs(normalized)),
        )
    )
    return {
        "managed": True,
        "changed": changed,
        "source": str(controls.get("source") or "default"),
        "profile_path": os.path.realpath(conf_path),
        "gain": float(controls.get("gain") or _DEFAULT_GAIN),
        "squelch_mode": str(controls.get("squelch_mode") or "dbfs"),
        "squelch_snr": float(controls.get("squelch_snr") or _DEFAULT_SQUELCH_SNR),
        "squelch_dbfs": float(controls.get("squelch_dbfs") or _default_dbfs(normalized)),
    }


def persist_managed_controls_override(
    target: str,
    conf_path: str,
    *,
    gain: float,
    squelch_mode: str,
    squelch_snr: float,
    squelch_dbfs: float,
) -> bool:
    normalized = _normalize_target(target)
    if not is_managed_controls_profile(normalized, conf_path):
        return False
    state = _load_state()
    targets = state.setdefault("targets", {})
    targets[normalized] = {
        "profile_path": os.path.realpath(conf_path),
        "updated_at_ms": int(time.time() * 1000),
        "override": _controls_payload(
            gain=gain,
            squelch_mode=squelch_mode,
            squelch_snr=squelch_snr,
            squelch_dbfs=squelch_dbfs,
        ),
    }
    _save_state(state)
    return True
