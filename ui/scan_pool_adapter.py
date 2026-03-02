"""Read-only adapter for scheduler scan-pool retrieval."""
from __future__ import annotations

import threading
import time

from .scan_mode_controller import get_scan_mode_controller


_SNAPSHOT_LOCK = threading.Lock()
_POOL_SNAPSHOT: dict = {"trunked_sites": [], "conventional": []}
_POOL_SNAPSHOT_TS_MONOTONIC = 0.0


def _normalize_mode(mode: str) -> str:
    token = str(mode or "").strip().lower()
    if token in {"hp3", "hp"}:
        return "hp"
    if token in {"sb3", "expert"}:
        return "expert"
    if token in {"legacy", "profile"}:
        return "legacy"
    return "legacy"


def get_current_scan_mode() -> str:
    try:
        controller = get_scan_mode_controller()
        mode = controller.get_mode()
    except Exception:
        mode = "legacy"
    return _normalize_mode(mode)


def _normalize_pool(payload: object) -> dict:
    if not isinstance(payload, dict):
        return {"trunked_sites": [], "conventional": []}

    trunked_raw = payload.get("trunked_sites")
    conventional_raw = payload.get("conventional")

    trunked_sites: list[dict] = []
    if isinstance(trunked_raw, list):
        for item in trunked_raw:
            if isinstance(item, dict):
                trunked_sites.append(dict(item))

    conventional: list[dict] = []
    if isinstance(conventional_raw, list):
        for item in conventional_raw:
            if isinstance(item, dict):
                conventional.append(dict(item))

    return {
        "trunked_sites": trunked_sites,
        "conventional": conventional,
    }


def get_active_scan_pool_snapshot(force_refresh: bool = False) -> dict:
    global _POOL_SNAPSHOT
    global _POOL_SNAPSHOT_TS_MONOTONIC

    with _SNAPSHOT_LOCK:
        if not force_refresh and _POOL_SNAPSHOT_TS_MONOTONIC > 0:
            return _normalize_pool(_POOL_SNAPSHOT)

        mode = get_current_scan_mode()
        if mode == "legacy":
            payload = {"trunked_sites": [], "conventional": []}
        else:
            try:
                controller = get_scan_mode_controller()
                payload = controller.get_scan_pool()
            except Exception:
                payload = {}

        _POOL_SNAPSHOT = _normalize_pool(payload)
        _POOL_SNAPSHOT_TS_MONOTONIC = time.monotonic()
        return _normalize_pool(_POOL_SNAPSHOT)


def get_active_scan_pool() -> dict:
    payload = get_active_scan_pool_snapshot(force_refresh=False)
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("trunked_sites", [])
    payload.setdefault("conventional", [])
    return payload
