"""Scan mode controller for HP and Expert pool modes."""
from __future__ import annotations

import math
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .hp_scan_pool import ScanPoolBuilder, haversine_miles


_VALID_MODES = {"hp", "expert"}
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_DB_PATH = str((_REPO_ROOT / "data" / "homepatrol.db").resolve())


def _normalize_mode_token(mode: str) -> str:
    token = str(mode or "").strip().lower()
    if token in {"hp3", "hp"}:
        return "hp"
    if token in {"sb3", "expert"}:
        return "expert"
    if token in {"profile", "legacy"}:
        return "expert"
    return token


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
        self._lock = threading.Lock()

    def set_mode(self, mode: str):
        next_mode = _normalize_mode_token(mode)
        if next_mode not in _VALID_MODES:
            raise ValueError("mode must be HP3 or SB3 (aliases: hp/expert)")
        with self._lock:
            self.mode = next_mode

    def get_mode(self) -> str:
        with self._lock:
            return str(self.mode)

    def set_expert_config(self, config: dict):
        # Manual expert pool tuning was retired; keep API compatibility.
        del config
        return

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
    def _normalize_service_tags(cls, values) -> list[int]:
        raw = values if isinstance(values, list) else []
        seen: set[int] = set()
        out: list[int] = []
        for item in raw:
            parsed = cls._parse_int(item)
            if parsed is None:
                continue
            if parsed in seen:
                continue
            seen.add(parsed)
            out.append(parsed)
        return out

    def _resolve_effective_service_tags(self, state) -> list[int]:
        service_tags = self._normalize_service_tags(getattr(state, "enabled_service_tags", []) or [])
        if service_tags:
            return service_tags
        try:
            from .service_types import get_default_enabled_service_types

            return self._normalize_service_tags(get_default_enabled_service_types(db_path=self._db_path))
        except Exception:
            return []

    @staticmethod
    def _normalize_text_token(value) -> str:
        return str(value or "").strip().lower()

    @classmethod
    def _entry_matches_service_tags(cls, entry: dict, allowed_tags: set[int]) -> bool:
        if not allowed_tags:
            return False
        tag = cls._parse_int(entry.get("service_tag"))
        if tag is None or tag <= 0:
            return False
        return tag in allowed_tags

    @classmethod
    def _within_location_threshold(
        cls,
        *,
        center_lat: float,
        center_lon: float,
        target_lat: float,
        target_lon: float,
        target_radius: float,
        lat_miles_per_degree: float,
        lon_miles_per_degree: float,
        range_miles: float,
    ) -> bool:
        threshold = max(0.0, float(range_miles)) + max(0.0, float(target_radius))
        if abs(target_lat - center_lat) * lat_miles_per_degree > threshold:
            return False
        if abs(target_lon - center_lon) * lon_miles_per_degree > threshold:
            return False
        distance = haversine_miles(center_lat, center_lon, target_lat, target_lon)
        return distance <= threshold

    def _load_nearby_trunk_systems(
        self,
        conn: sqlite3.Connection,
        *,
        center_lat: float,
        center_lon: float,
        range_miles: float,
        include_nationwide: bool,
    ) -> tuple[set[int], set[str]]:
        rows = conn.execute(
            """
            SELECT
                ts.trunk_id,
                ts.source_file,
                ts.latitude,
                ts.longitude,
                ts.radius,
                sys.system_name
            FROM trunk_sites ts
            LEFT JOIN trunk_systems sys ON sys.trunk_id = ts.trunk_id
            ORDER BY ts.trunk_id, ts.site_id
            """
        ).fetchall()
        lat_miles_per_degree = 69.0
        lon_miles_per_degree = max(1e-6, 69.0 * abs(math.cos(math.radians(center_lat))))
        nearby_ids: set[int] = set()
        nearby_names: set[str] = set()
        for row in rows:
            source_file = str(row["source_file"] or "").strip().lower()
            if source_file == "_multiplestates.hpd" and not include_nationwide:
                continue
            trunk_id = self._parse_int(row["trunk_id"])
            target_lat = self._parse_float(row["latitude"])
            target_lon = self._parse_float(row["longitude"])
            target_radius = self._parse_float(row["radius"]) or 0.0
            if trunk_id is None or trunk_id <= 0:
                continue
            if target_lat is None or target_lon is None:
                continue
            if not self._within_location_threshold(
                center_lat=center_lat,
                center_lon=center_lon,
                target_lat=target_lat,
                target_lon=target_lon,
                target_radius=target_radius,
                lat_miles_per_degree=lat_miles_per_degree,
                lon_miles_per_degree=lon_miles_per_degree,
                range_miles=range_miles,
            ):
                continue
            nearby_ids.add(int(trunk_id))
            system_name = self._normalize_text_token(row["system_name"])
            if system_name:
                nearby_names.add(system_name)
        return nearby_ids, nearby_names

    def _load_nearby_conventional_systems(
        self,
        conn: sqlite3.Connection,
        *,
        center_lat: float,
        center_lon: float,
        range_miles: float,
        include_nationwide: bool,
    ) -> tuple[set[str], set[str]]:
        rows = conn.execute(
            """
            SELECT DISTINCT
                (cg.parent_key || ':' || CAST(cg.parent_id AS TEXT)) AS system_key,
                cs.system_name,
                cg.source_file,
                cg.latitude,
                cg.longitude,
                cg.radius
            FROM conventional_groups cg
            LEFT JOIN conventional_systems cs
              ON cs.system_key = (cg.parent_key || ':' || CAST(cg.parent_id AS TEXT))
            ORDER BY system_key
            """
        ).fetchall()
        lat_miles_per_degree = 69.0
        lon_miles_per_degree = max(1e-6, 69.0 * abs(math.cos(math.radians(center_lat))))
        nearby_keys: set[str] = set()
        nearby_names: set[str] = set()
        for row in rows:
            source_file = str(row["source_file"] or "").strip().lower()
            if source_file == "_multiplestates.hpd" and not include_nationwide:
                continue
            system_key = str(row["system_key"] or "").strip()
            target_lat = self._parse_float(row["latitude"])
            target_lon = self._parse_float(row["longitude"])
            target_radius = self._parse_float(row["radius"]) or 0.0
            if not system_key:
                continue
            if target_lat is None or target_lon is None:
                continue
            if not self._within_location_threshold(
                center_lat=center_lat,
                center_lon=center_lon,
                target_lat=target_lat,
                target_lon=target_lon,
                target_radius=target_radius,
                lat_miles_per_degree=lat_miles_per_degree,
                lon_miles_per_degree=lon_miles_per_degree,
                range_miles=range_miles,
            ):
                continue
            nearby_keys.add(system_key)
            system_name = self._normalize_text_token(row["system_name"])
            if system_name:
                nearby_names.add(system_name)
        return nearby_keys, nearby_names

    def _filter_favorites_entries(self, entries: list[dict], state, service_tags: list[int]) -> list[dict]:
        raw_entries = [row for row in (entries or []) if isinstance(row, dict)]
        tag_set = set(self._normalize_service_tags(service_tags))
        if not tag_set:
            return []

        filtered_by_service = [
            row for row in raw_entries if self._entry_matches_service_tags(row, tag_set)
        ]
        if not bool(getattr(state, "use_location", False)):
            return filtered_by_service

        center_lat = self._parse_float(getattr(state, "lat", None))
        center_lon = self._parse_float(getattr(state, "lon", None))
        if center_lat is None or center_lon is None:
            return []

        range_miles = max(0.0, float(self._parse_float(getattr(state, "range_miles", 0.0)) or 0.0))
        include_nationwide = bool(getattr(state, "nationwide_systems", False))

        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            nearby_trunk_ids, nearby_trunk_names = self._load_nearby_trunk_systems(
                conn,
                center_lat=center_lat,
                center_lon=center_lon,
                range_miles=range_miles,
                include_nationwide=include_nationwide,
            )
            nearby_conv_keys, nearby_conv_names = self._load_nearby_conventional_systems(
                conn,
                center_lat=center_lat,
                center_lon=center_lon,
                range_miles=range_miles,
                include_nationwide=include_nationwide,
            )
        except Exception:
            return filtered_by_service
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass

        filtered_by_location: list[dict] = []
        for row in filtered_by_service:
            kind = str(row.get("kind") or "").strip().lower()
            if kind == "trunked":
                system_id = self._parse_int(row.get("system_id"))
                system_name = self._normalize_text_token(row.get("system_name"))
                if (
                    (system_id is not None and system_id in nearby_trunk_ids)
                    or (system_name and system_name in nearby_trunk_names)
                ):
                    filtered_by_location.append(row)
                continue
            if kind == "conventional":
                system_key = str(row.get("system_key") or "").strip()
                system_name = self._normalize_text_token(row.get("system_name"))
                if (
                    (system_key and system_key in nearby_conv_keys)
                    or (system_name and system_name in nearby_conv_names)
                ):
                    filtered_by_location.append(row)
                continue
            filtered_by_location.append(row)
        return filtered_by_location

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
        if not out:
            return out

        out.sort()

        # HPDB trunk-site frequency exports can include very large mixed-band
        # sets (ex: VHF + 700/800). Keep dominant bands and trim long tails so
        # scheduler control lists stay realistic for trunk following.
        band_counts: dict[int, int] = {}
        for mhz in out:
            band = int(mhz // 100)
            band_counts[band] = band_counts.get(band, 0) + 1

        if len(band_counts) > 2:
            total = len(out)
            min_count = max(5, int(total * 0.15))
            keep_bands = {band for band, count in band_counts.items() if count >= min_count}
            if not keep_bands:
                keep_bands = {
                    band
                    for band, _count in sorted(
                        band_counts.items(),
                        key=lambda item: item[1],
                        reverse=True,
                    )[:2]
                }
            out = [mhz for mhz in out if int(mhz // 100) in keep_bands]

        max_controls = 64
        if len(out) > max_controls:
            out = out[:max_controls]
        return out

    @classmethod
    def _build_custom_favorites_pool(cls, entries: list[dict]) -> dict[str, list]:
        raw_entries = entries if isinstance(entries, list) else []
        trunk_rows: dict[tuple[int, str, str, tuple[float, ...]], dict[str, Any]] = {}
        conventional_rows: dict[tuple[str, float, int, str], dict[str, Any]] = {}

        for item in raw_entries:
            row = item if isinstance(item, dict) else {}
            kind = str(row.get("kind") or "").strip().lower()
            if kind == "trunked":
                controls = cls._normalize_control_channels(row.get("control_channels"))
                tgid = cls._parse_int(row.get("talkgroup") or row.get("tgid"))
                if not controls or tgid is None or tgid <= 0:
                    continue
                system_id = cls._parse_int(row.get("system_id")) or 0
                system_name = str(row.get("system_name") or "").strip() or "Custom Trunked"
                department_name = str(row.get("department_name") or "").strip()
                alpha_tag = str(row.get("alpha_tag") or row.get("channel_name") or "").strip()
                key = (int(system_id), system_name.lower(), department_name.lower(), tuple(controls))
                bucket = trunk_rows.get(key)
                if bucket is None:
                    bucket = {
                        "system_id": int(system_id),
                        "system_name": system_name,
                        "site_name": department_name or system_name,
                        "department_name": department_name or system_name,
                        "control_channels": controls,
                        "talkgroups": set(),
                        "talkgroup_labels": {},
                        "talkgroup_groups": {},
                    }
                    trunk_rows[key] = bucket
                if int(bucket.get("system_id") or 0) <= 0 and system_id > 0:
                    bucket["system_id"] = int(system_id)
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
                system_key = str(row.get("system_key") or "").strip()
                system_name = str(row.get("system_name") or "").strip()
                alpha_tag = str(row.get("alpha_tag") or row.get("channel_name") or "").strip()
                service_tag = cls._parse_int(row.get("service_tag"))
                conventional_rows[
                    (
                        system_key.lower(),
                        round(frequency, 6),
                        int(service_tag or 0),
                        alpha_tag.lower(),
                    )
                ] = {
                    "system_key": system_key,
                    "system_name": system_name,
                    "frequency": round(frequency, 6),
                    "alpha_tag": alpha_tag,
                    "service_tag": int(service_tag or 0),
                }

        ordered_trunk_keys = sorted(trunk_rows.keys())
        system_id_by_name: dict[str, int] = {}
        next_site_id = 1
        trunked_sites: list[dict] = []
        for key in ordered_trunk_keys:
            data = trunk_rows[key]
            system_name = str(data.get("system_name") or "").strip() or "Custom Trunked"
            explicit_system_id = int(data.get("system_id") or 0)
            if explicit_system_id > 0:
                system_id = explicit_system_id
            else:
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

        conventional = sorted(
            conventional_rows.values(),
            key=lambda item: (
                float(item.get("frequency") or 0.0),
                int(item.get("service_tag") or 0),
                str(item.get("alpha_tag") or "").lower(),
                str(item.get("system_name") or "").lower(),
            ),
        )
        return {
            "trunked_sites": trunked_sites,
            "conventional": conventional,
        }

    @classmethod
    def _resolve_active_favorites_entries(cls, state) -> list[dict]:
        favorites = list(getattr(state, "favorites", []) or [])
        active_name = str(getattr(state, "favorites_name", "") or "").strip().lower()

        selected: dict[str, Any] | None = None
        for item in favorites:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip().lower()
            if not label:
                continue
            if label == active_name:
                selected = item
                break

        if selected is None:
            for item in favorites:
                if not isinstance(item, dict):
                    continue
                if bool(item.get("enabled")):
                    selected = item
                    break

        if selected is not None:
            custom = selected.get("custom_favorites")
            if isinstance(custom, list):
                return list(custom)

        return list(getattr(state, "custom_favorites", []) or [])

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
        if mode not in _VALID_MODES:
            return _empty_pool()

        try:
            from .hp_state import HPState
        except Exception:
            return _empty_pool()

        state = HPState.load()
        state_mode = str(state.mode).strip().lower()
        service_tags = self._resolve_effective_service_tags(state)
        if not service_tags:
            return _empty_pool()
        if state_mode == "favorites":
            entries = self._resolve_active_favorites_entries(state)
            filtered_entries = self._filter_favorites_entries(entries, state, service_tags)
            pool = self._build_custom_favorites_pool(filtered_entries)
        elif state_mode == "full_database":
            if not bool(state.use_location):
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
