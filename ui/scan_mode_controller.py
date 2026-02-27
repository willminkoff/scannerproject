"""Scan mode controller for HP and Expert pool modes."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .hp_scan_pool import ScanPoolBuilder


_VALID_MODES = {"hp", "expert", "legacy"}
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_DB_PATH = str((_REPO_ROOT / "data" / "homepatrol.db").resolve())


def _empty_pool() -> dict[str, list]:
    return {
        "trunked_sites": [],
        "conventional": [],
    }


class ScanModeController:
    def __init__(self, db_path: str = _DEFAULT_DB_PATH):
        self.mode = "hp"
        self._db_path = str(Path(db_path).expanduser().resolve())
        self._hp_builder = ScanPoolBuilder(self._db_path)
        self._expert_config: dict[str, Any] = {
            "manual_trunked": [],
            "manual_conventional": [],
        }
        self._lock = threading.Lock()

    def set_mode(self, mode: str):
        next_mode = str(mode or "").strip().lower()
        if next_mode == "profile":
            next_mode = "legacy"
        if next_mode not in _VALID_MODES:
            raise ValueError("mode must be 'hp', 'expert', or 'legacy'")
        with self._lock:
            self.mode = next_mode

    def get_mode(self) -> str:
        with self._lock:
            return str(self.mode)

    def set_expert_config(self, config: dict):
        payload = config if isinstance(config, dict) else {}
        with self._lock:
            self._expert_config = {
                "manual_trunked": list(payload.get("manual_trunked") or []),
                "manual_conventional": list(payload.get("manual_conventional") or []),
            }

    def get_scan_pool(self):
        mode = self.get_mode()
        if mode == "expert":
            from .expert_scan_pool import ExpertPoolBuilder

            with self._lock:
                cfg = {
                    "manual_trunked": list(self._expert_config.get("manual_trunked") or []),
                    "manual_conventional": list(self._expert_config.get("manual_conventional") or []),
                }
            return ExpertPoolBuilder().build_pool(cfg)

        if mode == "legacy":
            return _empty_pool()

        try:
            from .hp_state import HPState
        except Exception:
            return _empty_pool()

        state = HPState.load()
        if str(state.mode).strip().lower() != "full_database":
            return _empty_pool()
        if not bool(state.use_location):
            return _empty_pool()

        service_tags = [int(v) for v in (state.enabled_service_tags or []) if str(v).strip()]
        if not service_tags:
            try:
                from .service_types import get_default_enabled_service_types

                service_tags = list(get_default_enabled_service_types(db_path=self._db_path))
            except Exception:
                service_tags = []
        if not service_tags:
            return _empty_pool()

        return self._hp_builder.build_full_database_pool(
            lat=float(state.lat),
            lon=float(state.lon),
            range_miles=float(state.range_miles),
            service_tags=service_tags,
        )


_SCAN_MODE_CONTROLLER: ScanModeController | None = None
_SCAN_MODE_LOCK = threading.Lock()


def get_scan_mode_controller(db_path: str = _DEFAULT_DB_PATH) -> ScanModeController:
    global _SCAN_MODE_CONTROLLER
    with _SCAN_MODE_LOCK:
        if _SCAN_MODE_CONTROLLER is None:
            _SCAN_MODE_CONTROLLER = ScanModeController(db_path=db_path)
        return _SCAN_MODE_CONTROLLER
