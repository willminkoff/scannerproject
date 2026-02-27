"""Scan mode controller for HP and Expert pool modes."""
from __future__ import annotations

import math
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
        self._hp_avoided_systems: set[str] = set()
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

    @staticmethod
    def _normalize_system_token(value: str) -> str:
        return str(value or "").strip().lower()

    @classmethod
    def _pool_system_tokens(cls, row: dict) -> set[str]:
        if not isinstance(row, dict):
            return set()
        system_id = str(row.get("system_id") or "").strip()
        site_id = str(row.get("site_id") or "").strip()
        out: set[str] = set()
        if system_id and site_id:
            out.add(cls._normalize_system_token(f"{system_id}:{site_id}"))
        if system_id:
            out.add(cls._normalize_system_token(system_id))
        if site_id:
            out.add(cls._normalize_system_token(f"site:{site_id}"))
        return {token for token in out if token}

    @staticmethod
    def _parse_int(value) -> int | None:
        try:
            return int(str(value).strip())
        except Exception:
            return None

    @staticmethod
    def _parse_float(value) -> float | None:
        try:
            parsed = float(str(value).strip())
        except Exception:
            return None
        if not math.isfinite(parsed):
            return None
        return parsed

    @classmethod
    def _normalize_control_channels(cls, value) -> list[float]:
        raw = value if isinstance(value, list) else [value]
        seen: set[float] = set()
        out: list[float] = []
        for item in raw:
            parsed = cls._parse_float(item)
            if parsed is None or parsed <= 0:
                continue
            mhz = round(parsed, 6)
            if mhz in seen:
                continue
            seen.add(mhz)
            out.append(mhz)
        out.sort()
        return out

    @classmethod
    def _build_custom_favorites_pool(cls, entries: list[dict]) -> dict[str, list]:
        raw_entries = entries if isinstance(entries, list) else []
        trunk_rows: dict[tuple[str, str, tuple[float, ...]], dict[str, Any]] = {}
        conventional_set: set[tuple[float, str, int]] = set()

        for item in raw_entries:
            row = item if isinstance(item, dict) else {}
            kind = str(row.get("kind") or "").strip().lower()
            if kind == "trunked":
                controls = cls._normalize_control_channels(row.get("control_channels"))
                tgid = cls._parse_int(row.get("talkgroup") or row.get("tgid"))
                if not controls or tgid is None or tgid <= 0:
                    continue
                system_name = str(row.get("system_name") or "").strip() or "Custom Trunked"
                department_name = str(row.get("department_name") or "").strip()
                alpha_tag = str(row.get("alpha_tag") or row.get("channel_name") or "").strip()
                key = (system_name.lower(), department_name.lower(), tuple(controls))
                bucket = trunk_rows.get(key)
                if bucket is None:
                    bucket = {
                        "system_name": system_name,
                        "site_name": department_name or system_name,
                        "department_name": department_name or system_name,
                        "control_channels": controls,
                        "talkgroups": set(),
                        "talkgroup_labels": {},
                        "talkgroup_groups": {},
                    }
                    trunk_rows[key] = bucket
                bucket["talkgroups"].add(int(tgid))
                if alpha_tag:
                    bucket["talkgroup_labels"][str(int(tgid))] = alpha_tag
                if department_name:
                    bucket["talkgroup_groups"][str(int(tgid))] = department_name
                continue

            if kind == "conventional":
                frequency = cls._parse_float(row.get("frequency"))
                if frequency is None or frequency <= 0:
                    continue
                alpha_tag = str(row.get("alpha_tag") or row.get("channel_name") or "").strip()
                service_tag = cls._parse_int(row.get("service_tag"))
                conventional_set.add((round(frequency, 6), alpha_tag, int(service_tag or 0)))

        ordered_trunk_keys = sorted(trunk_rows.keys())
        system_id_by_name: dict[str, int] = {}
        next_site_id = 1
        trunked_sites: list[dict] = []
        for key in ordered_trunk_keys:
            data = trunk_rows[key]
            system_name = str(data.get("system_name") or "").strip() or "Custom Trunked"
            system_token = system_name.lower()
            if system_token not in system_id_by_name:
                system_id_by_name[system_token] = len(system_id_by_name) + 1
            system_id = system_id_by_name[system_token]
            talkgroups = sorted(int(value) for value in (data.get("talkgroups") or set()) if int(value) > 0)
            if not talkgroups:
                continue
            talkgroup_labels = data.get("talkgroup_labels") if isinstance(data.get("talkgroup_labels"), dict) else {}
            talkgroup_groups = data.get("talkgroup_groups") if isinstance(data.get("talkgroup_groups"), dict) else {}
            trunked_sites.append(
                {
                    "system_id": int(system_id),
                    "site_id": int(next_site_id),
                    "system_name": system_name,
                    "site_name": str(data.get("site_name") or "").strip() or system_name,
                    "department_name": str(data.get("department_name") or "").strip() or system_name,
                    "control_channels": list(data.get("control_channels") or []),
                    "talkgroups": talkgroups,
                    "talkgroup_labels": {str(k): str(v) for k, v in talkgroup_labels.items() if str(v).strip()},
                    "talkgroup_groups": {str(k): str(v) for k, v in talkgroup_groups.items() if str(v).strip()},
                }
            )
            next_site_id += 1

        conventional = [
            {
                "frequency": frequency,
                "alpha_tag": alpha_tag,
                "service_tag": service_tag,
            }
            for frequency, alpha_tag, service_tag in sorted(
                conventional_set,
                key=lambda item: (item[0], item[2], item[1].lower()),
            )
        ]
        return {
            "trunked_sites": trunked_sites,
            "conventional": conventional,
        }

    def add_hp_avoid_system(self, system_token: str) -> bool:
        token = self._normalize_system_token(system_token)
        if not token:
            return False
        with self._lock:
            self._hp_avoided_systems.add(token)
        return True

    def clear_hp_avoids(self):
        with self._lock:
            self._hp_avoided_systems.clear()

    def remove_hp_avoid_system(self, system_token: str) -> bool:
        token = self._normalize_system_token(system_token)
        if not token:
            return False
        with self._lock:
            if token not in self._hp_avoided_systems:
                return False
            self._hp_avoided_systems.remove(token)
        return True

    def get_hp_avoids(self) -> list[str]:
        with self._lock:
            return sorted(self._hp_avoided_systems)

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
        state_mode = str(state.mode).strip().lower()
        if state_mode == "favorites":
            pool = self._build_custom_favorites_pool(list(getattr(state, "custom_favorites", []) or []))
        elif state_mode == "full_database":
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

            pool = self._hp_builder.build_full_database_pool(
                lat=float(state.lat),
                lon=float(state.lon),
                range_miles=float(state.range_miles),
                service_tags=service_tags,
                include_nationwide=bool(getattr(state, "nationwide_systems", False)),
            )
        else:
            return _empty_pool()
        with self._lock:
            avoids = set(self._hp_avoided_systems)
        if not avoids:
            return pool

        trunked_sites = pool.get("trunked_sites")
        if not isinstance(trunked_sites, list):
            return pool

        filtered_sites: list[dict] = []
        for row in trunked_sites:
            tokens = self._pool_system_tokens(row if isinstance(row, dict) else {})
            if tokens and (tokens & avoids):
                continue
            filtered_sites.append(row)
        pool["trunked_sites"] = filtered_sites
        return pool


_SCAN_MODE_CONTROLLER: ScanModeController | None = None
_SCAN_MODE_LOCK = threading.Lock()


def get_scan_mode_controller(db_path: str = _DEFAULT_DB_PATH) -> ScanModeController:
    global _SCAN_MODE_CONTROLLER
    with _SCAN_MODE_LOCK:
        if _SCAN_MODE_CONTROLLER is None:
            _SCAN_MODE_CONTROLLER = ScanModeController(db_path=db_path)
        return _SCAN_MODE_CONTROLLER
