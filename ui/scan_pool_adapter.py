"""Read-only adapter for scheduler scan-pool retrieval."""
from __future__ import annotations

from .scan_mode_controller import get_scan_mode_controller


def get_active_scan_pool() -> dict:
    try:
        controller = get_scan_mode_controller()
        payload = controller.get_scan_pool()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("trunked_sites", [])
    payload.setdefault("conventional", [])
    return payload
