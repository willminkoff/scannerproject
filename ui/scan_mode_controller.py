"""Scan mode controller for HP and Expert pool modes."""
from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .config import HPDB_DB_PATH, HP_AVOIDS_PATH
from .hp_scan_pool import ScanPoolBuilder, haversine_miles
from .zip_lookup import resolve_postal_to_lat_lon


_VALID_MODES = {"hp", "expert"}
_DEFAULT_DB_PATH = str(Path(HPDB_DB_PATH).expanduser().resolve())
_DEFAULT_HP_AVOIDS_PATH = str(Path(HP_AVOIDS_PATH).expanduser())


def _sites_per_system_limit() -> int:
    try:
        parsed = int(os.getenv("HP_TRUNK_SITES_PER_SYSTEM", "1"))
    except Exception:
        parsed = 1
    return max(1, int(parsed))


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
    def __init__(
        self,
        db_path: str = _DEFAULT_DB_PATH,
        avoids_path: str = _DEFAULT_HP_AVOIDS_PATH,
    ):
        self.mode = "hp"
        self._db_path = str(Path(db_path).expanduser().resolve())
        self._hp_avoids_path = str(Path(avoids_path).expanduser())
        self._hp_builder = ScanPoolBuilder(self._db_path)
        self._hp_avoided_systems: set[str] = set()
        self._multistate_localized_trunk_ids: set[int] | None = None
        self._multistate_localized_conv_system_keys: set[str] | None = None
        self._lock = threading.Lock()
        self._load_hp_avoids_from_disk()

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
    def _normalize_agency_token_part(value: str) -> str:
        return " ".join(str(value or "").strip().lower().split())

    @classmethod
    def _trunked_agency_token(cls, system_id: int, agency_name: str) -> str:
        sid = cls._parse_int(system_id)
        if sid is None or sid <= 0:
            return ""
        agency = cls._normalize_agency_token_part(agency_name)
        if not agency:
            return ""
        return cls._normalize_system_token(f"agency:{sid}:{agency}")

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

    def _load_hp_avoids_from_disk(self) -> None:
        path = str(self._hp_avoids_path or "").strip()
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return
        except Exception:
            return
        raw_avoids = []
        if isinstance(payload, dict):
            raw_avoids = payload.get("avoids") or []
        elif isinstance(payload, list):
            raw_avoids = payload
        if not isinstance(raw_avoids, list):
            raw_avoids = []
        normalized: set[str] = set()
        for item in raw_avoids:
            token = self._normalize_system_token(item)
            if token:
                normalized.add(token)
        with self._lock:
            self._hp_avoided_systems = set(normalized)

    def _persist_hp_avoids_locked(self) -> None:
        path = str(self._hp_avoids_path or "").strip()
        if not path:
            return
        payload = {
            "avoids": sorted(self._hp_avoided_systems),
        }
        try:
            parent = os.path.dirname(path) or "."
            os.makedirs(parent, exist_ok=True)
            tmp_path = f"{path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp_path, path)
        except Exception:
            # In-memory avoids still apply even if persistence fails.
            return

    @classmethod
    def _has_usable_location(cls, lat: float | None, lon: float | None) -> bool:
        if lat is None or lon is None:
            return False
        if not (-90.0 <= float(lat) <= 90.0 and -180.0 <= float(lon) <= 180.0):
            return False
        # Treat default/uninitialized 0,0 as missing location for HP workflows.
        if abs(float(lat)) < 1e-9 and abs(float(lon)) < 1e-9:
            return False
        return True

    def _resolve_location_center(self, state) -> tuple[float, float] | None:
        if not bool(getattr(state, "use_location", False)):
            return None

        lat = self._parse_float(getattr(state, "lat", None))
        lon = self._parse_float(getattr(state, "lon", None))
        if self._has_usable_location(lat, lon):
            return float(lat), float(lon)

        postal = str(getattr(state, "zip", "") or "").strip()
        if not postal:
            return None
        try:
            resolved = resolve_postal_to_lat_lon(postal, "US")
        except Exception:
            return None
        if not resolved or len(resolved) < 2:
            return None
        resolved_lat = self._parse_float(resolved[0])
        resolved_lon = self._parse_float(resolved[1])
        if not self._has_usable_location(resolved_lat, resolved_lon):
            return None
        return float(resolved_lat), float(resolved_lon)

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
        strict_location: bool = False,
    ) -> bool:
        if strict_location:
            threshold = max(0.0, float(range_miles))
        else:
            threshold = max(0.0, float(range_miles)) + max(0.0, float(target_radius))
        if abs(target_lat - center_lat) * lat_miles_per_degree > threshold:
            return False
        if abs(target_lon - center_lon) * lon_miles_per_degree > threshold:
            return False
        distance = haversine_miles(center_lat, center_lon, target_lat, target_lon)
        return distance <= threshold

    def _load_multistate_scope_overrides(self, conn: sqlite3.Connection) -> tuple[set[int], set[str]]:
        cached_trunk_ids = self._multistate_localized_trunk_ids
        cached_conv_keys = self._multistate_localized_conv_system_keys
        if cached_trunk_ids is not None and cached_conv_keys is not None:
            return cached_trunk_ids, cached_conv_keys

        trunk_ids: set[int] = set()
        conv_system_keys: set[str] = set()
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT entity_id
                FROM entity_areas
                WHERE entity_kind = 'TrunkId'
                  AND (
                    (record_type = 'AreaState' AND COALESCE(state_id, 0) > 0)
                    OR (record_type = 'AreaCounty' AND COALESCE(county_id, 0) > 0)
                  )
                """
            ).fetchall()
            for row in rows:
                trunk_id = self._parse_int(row["entity_id"])
                if trunk_id is not None and trunk_id > 0:
                    trunk_ids.add(int(trunk_id))

            rows = conn.execute(
                """
                SELECT DISTINCT cs.system_key
                FROM conventional_systems cs
                JOIN entity_areas ea
                  ON ea.entity_kind = 'AgencyId'
                 AND ea.entity_id = CAST(substr(cs.system_key, instr(cs.system_key, ':') + 1) AS INTEGER)
                WHERE lower(cs.source_file) = '_multiplestates.hpd'
                  AND (
                    (ea.record_type = 'AreaState' AND COALESCE(ea.state_id, 0) > 0)
                    OR (ea.record_type = 'AreaCounty' AND COALESCE(ea.county_id, 0) > 0)
                  )
                """
            ).fetchall()
            for row in rows:
                token = str(row["system_key"] or "").strip().lower()
                if token:
                    conv_system_keys.add(token)
        except Exception:
            # If scope metadata cannot be read, keep legacy behavior by returning empty override sets.
            return set(), set()

        self._multistate_localized_trunk_ids = trunk_ids
        self._multistate_localized_conv_system_keys = conv_system_keys
        return trunk_ids, conv_system_keys

    def _load_nearby_trunk_systems(
        self,
        conn: sqlite3.Connection,
        *,
        center_lat: float,
        center_lon: float,
        range_miles: float,
        include_nationwide: bool,
        strict_location: bool = False,
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
        localized_trunk_ids: set[int] = set()
        if not include_nationwide:
            localized_trunk_ids, _ = self._load_multistate_scope_overrides(conn)
        lat_miles_per_degree = 69.0
        lon_miles_per_degree = max(1e-6, 69.0 * abs(math.cos(math.radians(center_lat))))
        nearby_ids: set[int] = set()
        nearby_names: set[str] = set()
        for row in rows:
            source_file = str(row["source_file"] or "").strip().lower()
            trunk_id = self._parse_int(row["trunk_id"])
            if (
                source_file == "_multiplestates.hpd"
                and not include_nationwide
                and (trunk_id is None or trunk_id not in localized_trunk_ids)
            ):
                continue
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
                strict_location=strict_location,
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
        strict_location: bool = False,
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
        localized_conv_keys: set[str] = set()
        if not include_nationwide:
            _, localized_conv_keys = self._load_multistate_scope_overrides(conn)
        lat_miles_per_degree = 69.0
        lon_miles_per_degree = max(1e-6, 69.0 * abs(math.cos(math.radians(center_lat))))
        nearby_keys: set[str] = set()
        nearby_names: set[str] = set()
        for row in rows:
            source_file = str(row["source_file"] or "").strip().lower()
            system_key = str(row["system_key"] or "").strip()
            if (
                source_file == "_multiplestates.hpd"
                and not include_nationwide
                and system_key.lower() not in localized_conv_keys
            ):
                continue
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
                strict_location=strict_location,
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

        center = self._resolve_location_center(state)
        if center is None:
            # Fallback to service-tag filtering if no usable center can be resolved.
            return filtered_by_service
        center_lat, center_lon = center

        range_miles = max(0.0, float(self._parse_float(getattr(state, "range_miles", 0.0)) or 0.0))
        include_nationwide = bool(getattr(state, "nationwide_systems", False))
        strict_location = bool(getattr(state, "strict_location", False))

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
                strict_location=strict_location,
            )
            nearby_conv_keys, nearby_conv_names = self._load_nearby_conventional_systems(
                conn,
                center_lat=center_lat,
                center_lon=center_lon,
                range_miles=range_miles,
                include_nationwide=include_nationwide,
                strict_location=strict_location,
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

    def _fallback_trunk_control_channels(self, *, system_id: int = 0, system_name: str = "") -> list[float]:
        """Backfill control channels from HPDB when legacy/custom rows omit them."""
        trunk_ids: list[int] = []
        if int(system_id or 0) > 0:
            trunk_ids.append(int(system_id))
        elif str(system_name or "").strip():
            name_token = str(system_name).strip().lower()
            try:
                with sqlite3.connect(self._db_path) as conn:
                    rows = conn.execute(
                        """
                        SELECT trunk_id
                        FROM trunk_systems
                        WHERE lower(system_name) = ?
                        ORDER BY trunk_id
                        LIMIT 8
                        """,
                        (name_token,),
                    ).fetchall()
                for row in rows:
                    try:
                        trunk_id = int(row[0])
                    except Exception:
                        continue
                    if trunk_id > 0:
                        trunk_ids.append(trunk_id)
            except Exception:
                trunk_ids = []

        if not trunk_ids:
            return []

        for trunk_id in trunk_ids:
            try:
                with sqlite3.connect(self._db_path) as conn:
                    rows = conn.execute(
                        """
                        SELECT DISTINCT tf.freq_hz
                        FROM trunk_sites ts
                        JOIN trunk_freqs tf ON tf.site_id = ts.site_id
                        WHERE ts.trunk_id = ?
                          AND tf.freq_hz IS NOT NULL
                        ORDER BY tf.freq_hz
                        """,
                        (int(trunk_id),),
                    ).fetchall()
            except Exception:
                continue
            controls_mhz: list[float] = []
            for row in rows:
                try:
                    hz = int(row[0])
                except Exception:
                    continue
                if hz <= 0:
                    continue
                controls_mhz.append(round(float(hz) / 1_000_000.0, 6))
            normalized = self._normalize_control_channels(controls_mhz)
            if normalized:
                return normalized
        return []

    def _build_custom_favorites_pool(self, entries: list[dict]) -> dict[str, list]:
        raw_entries = entries if isinstance(entries, list) else []
        trunk_rows: dict[tuple[int, str, str, tuple[float, ...]], dict[str, Any]] = {}
        conventional_rows: dict[tuple[str, float, int, str], dict[str, Any]] = {}

        for item in raw_entries:
            row = item if isinstance(item, dict) else {}
            kind = str(row.get("kind") or "").strip().lower()
            if kind == "trunked":
                system_id = self._parse_int(row.get("system_id")) or 0
                system_name = str(row.get("system_name") or "").strip() or "Custom Trunked"
                controls = self._normalize_control_channels(row.get("control_channels"))
                if not controls:
                    controls = self._fallback_trunk_control_channels(
                        system_id=int(system_id),
                        system_name=system_name,
                    )
                tgid = self._parse_int(row.get("talkgroup") or row.get("tgid"))
                if not controls or tgid is None or tgid <= 0:
                    continue
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
                frequency = self._parse_float(row.get("frequency"))
                if frequency is None or frequency <= 0:
                    continue
                system_key = str(row.get("system_key") or "").strip()
                system_name = str(row.get("system_name") or "").strip()
                alpha_tag = str(row.get("alpha_tag") or row.get("channel_name") or "").strip()
                service_tag = self._parse_int(row.get("service_tag"))
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

    def _favorite_nearest_controls_by_system(
        self,
        *,
        system_ids: list[int],
        center_lat: float,
        center_lon: float,
        range_miles: float,
        include_nationwide: bool,
        strict_location: bool = False,
    ) -> dict[int, dict[str, Any]]:
        if not system_ids:
            return {}

        lat_miles_per_degree = 69.0
        lon_miles_per_degree = max(1e-6, 69.0 * abs(math.cos(math.radians(center_lat))))
        site_limit = _sites_per_system_limit()

        out: dict[int, dict[str, Any]] = {}
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            localized_trunk_ids: set[int] = set()
            if not include_nationwide:
                localized_trunk_ids, _ = self._load_multistate_scope_overrides(conn)

            for system_id in system_ids:
                if system_id <= 0:
                    continue
                rows = conn.execute(
                    """
                    SELECT
                        ts.site_id,
                        ts.source_file,
                        ts.latitude,
                        ts.longitude,
                        ts.radius
                    FROM trunk_sites ts
                    WHERE ts.trunk_id = ?
                    ORDER BY ts.site_id
                    """,
                    (int(system_id),),
                ).fetchall()

                in_range: list[tuple[float, int]] = []
                all_candidates: list[tuple[float, int]] = []
                for row in rows:
                    source_file = str(row["source_file"] or "").strip().lower()
                    if (
                        source_file == "_multiplestates.hpd"
                        and not include_nationwide
                        and system_id not in localized_trunk_ids
                    ):
                        continue
                    site_id = self._parse_int(row["site_id"])
                    site_lat = self._parse_float(row["latitude"])
                    site_lon = self._parse_float(row["longitude"])
                    if site_id is None or site_id <= 0:
                        continue
                    if site_lat is None or site_lon is None:
                        continue
                    distance = haversine_miles(center_lat, center_lon, site_lat, site_lon)
                    all_candidates.append((float(distance), int(site_id)))
                    target_radius = max(0.0, float(self._parse_float(row["radius"]) or 0.0))
                    if self._within_location_threshold(
                        center_lat=center_lat,
                        center_lon=center_lon,
                        target_lat=site_lat,
                        target_lon=site_lon,
                        target_radius=target_radius,
                        lat_miles_per_degree=lat_miles_per_degree,
                        lon_miles_per_degree=lon_miles_per_degree,
                        range_miles=range_miles,
                        strict_location=strict_location,
                    ):
                        in_range.append((float(distance), int(site_id)))

                if not all_candidates:
                    continue
                ranked_in = sorted(in_range, key=lambda item: (float(item[0]), int(item[1])))
                ranked_all = sorted(all_candidates, key=lambda item: (float(item[0]), int(item[1])))
                keep: list[tuple[float, int]] = []
                seen_site_ids: set[int] = set()
                for distance, site_id in ranked_in:
                    if site_id in seen_site_ids:
                        continue
                    keep.append((float(distance), int(site_id)))
                    seen_site_ids.add(int(site_id))
                    if len(keep) >= site_limit:
                        break
                if len(keep) < site_limit:
                    for distance, site_id in ranked_all:
                        if site_id in seen_site_ids:
                            continue
                        keep.append((float(distance), int(site_id)))
                        seen_site_ids.add(int(site_id))
                        if len(keep) >= site_limit:
                            break
                if not keep:
                    continue
                keep_site_ids = [int(site_id) for _distance, site_id in keep if int(site_id) > 0]
                if not keep_site_ids:
                    continue

                placeholders = ",".join("?" for _ in keep_site_ids)
                freq_rows = conn.execute(
                    f"""
                    SELECT DISTINCT freq_hz
                    FROM trunk_freqs
                    WHERE site_id IN ({placeholders})
                      AND freq_hz IS NOT NULL
                    ORDER BY freq_hz
                    """,
                    keep_site_ids,
                ).fetchall()
                controls_mhz: list[float] = []
                for freq_row in freq_rows:
                    freq_hz = self._parse_int(freq_row["freq_hz"])
                    if freq_hz is None or freq_hz <= 0:
                        continue
                    controls_mhz.append(round(float(freq_hz) / 1_000_000.0, 6))
                controls = self._normalize_control_channels(controls_mhz)
                if not controls:
                    continue
                out[int(system_id)] = {
                    "controls": controls,
                    "distance_miles": float(keep[0][0]),
                    "site_ids": [int(item[1]) for item in keep],
                }
        except Exception:
            return {}
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
        return out

    def _trim_favorites_pool_to_nearest_sites(self, pool: dict[str, Any], state) -> dict[str, Any]:
        if not isinstance(pool, dict):
            return _empty_pool()
        trunked = pool.get("trunked_sites")
        if not isinstance(trunked, list) or not trunked:
            return pool
        if not bool(getattr(state, "use_location", False)):
            return pool

        center = self._resolve_location_center(state)
        if center is None:
            return pool
        center_lat, center_lon = center

        system_ids = sorted(
            {
                int(self._parse_int((row or {}).get("system_id")) or 0)
                for row in trunked
                if int(self._parse_int((row or {}).get("system_id")) or 0) > 0
            }
        )
        if not system_ids:
            return pool

        nearest_by_system = self._favorite_nearest_controls_by_system(
            system_ids=system_ids,
            center_lat=float(center_lat),
            center_lon=float(center_lon),
            range_miles=max(0.0, float(self._parse_float(getattr(state, "range_miles", 0.0)) or 0.0)),
            include_nationwide=bool(getattr(state, "nationwide_systems", False)),
            strict_location=bool(getattr(state, "strict_location", False)),
        )
        if not nearest_by_system:
            return pool

        trimmed: list[dict[str, Any]] = []
        for raw in trunked:
            row = raw if isinstance(raw, dict) else {}
            system_id = int(self._parse_int(row.get("system_id")) or 0)
            nearest = nearest_by_system.get(system_id)
            if not nearest:
                if row:
                    trimmed.append(dict(row))
                continue
            controls = nearest.get("controls")
            if not isinstance(controls, list) or not controls:
                if row:
                    trimmed.append(dict(row))
                continue
            patched = dict(row)
            patched["control_channels"] = list(controls)
            patched["distance_miles"] = float(nearest.get("distance_miles") or 0.0)
            trimmed.append(patched)

        pool["trunked_sites"] = trimmed
        return pool

    def _prefer_nearest_site_per_system(self, pool: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(pool, dict):
            return _empty_pool()
        trunked_sites = pool.get("trunked_sites")
        if not isinstance(trunked_sites, list) or len(trunked_sites) <= 1:
            return pool

        best_by_system: dict[int, dict[str, Any]] = {}
        passthrough_rows: list[dict[str, Any]] = []
        for raw in trunked_sites:
            row = raw if isinstance(raw, dict) else {}
            system_id = self._parse_int(row.get("system_id")) or 0
            if system_id <= 0:
                if row:
                    passthrough_rows.append(row)
                continue
            candidate = best_by_system.get(system_id)
            if candidate is None:
                best_by_system[system_id] = row
                continue
            cand_dist = self._parse_float(candidate.get("distance_miles"))
            row_dist = self._parse_float(row.get("distance_miles"))
            cand_site = self._parse_int(candidate.get("site_id")) or 0
            row_site = self._parse_int(row.get("site_id")) or 0
            cand_key = (
                float(cand_dist) if cand_dist is not None else float("inf"),
                cand_site,
            )
            row_key = (
                float(row_dist) if row_dist is not None else float("inf"),
                row_site,
            )
            if row_key < cand_key:
                best_by_system[system_id] = row

        ordered_best = sorted(
            best_by_system.values(),
            key=lambda item: (
                float(self._parse_float(item.get("distance_miles")) or float("inf")),
                int(self._parse_int(item.get("system_id")) or 0),
                int(self._parse_int(item.get("site_id")) or 0),
            ),
        )
        ordered_passthrough = sorted(
            passthrough_rows,
            key=lambda item: (
                float(self._parse_float(item.get("distance_miles")) or float("inf")),
                int(self._parse_int(item.get("site_id")) or 0),
            ),
        )
        pool["trunked_sites"] = [*ordered_best, *ordered_passthrough]
        return pool

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
            self._persist_hp_avoids_locked()
        return True

    def clear_hp_avoids(self):
        with self._lock:
            self._hp_avoided_systems.clear()
            self._persist_hp_avoids_locked()

    def remove_hp_avoid_system(self, system_token: str) -> bool:
        token = self._normalize_system_token(system_token)
        if not token:
            return False
        with self._lock:
            if token not in self._hp_avoided_systems:
                return False
            self._hp_avoided_systems.remove(token)
            self._persist_hp_avoids_locked()
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
            pool = self._trim_favorites_pool_to_nearest_sites(pool, state)
        elif state_mode == "full_database":
            if not bool(state.use_location):
                return _empty_pool()
            center = self._resolve_location_center(state)
            if center is None:
                return _empty_pool()
            center_lat, center_lon = center

            pool = self._hp_builder.build_full_database_pool(
                lat=float(center_lat),
                lon=float(center_lon),
                range_miles=float(state.range_miles),
                service_tags=service_tags,
                include_nationwide=bool(getattr(state, "nationwide_systems", False)),
                strict_location=bool(getattr(state, "strict_location", False)),
            )
            pool = self._prefer_nearest_site_per_system(pool)
        else:
            return _empty_pool()
        with self._lock:
            avoids = set(self._hp_avoided_systems)
        if not avoids:
            return pool

        trunked_sites = pool.get("trunked_sites")
        if isinstance(trunked_sites, list):
            filtered_sites: list[dict] = []
            for row in trunked_sites:
                row_dict = row if isinstance(row, dict) else {}
                tokens = self._pool_system_tokens(row_dict)
                if tokens and (tokens & avoids):
                    continue
                system_id = self._parse_int(row_dict.get("system_id")) or 0
                if system_id <= 0:
                    filtered_sites.append(row)
                    continue
                talkgroups_raw = row_dict.get("talkgroups")
                if not isinstance(talkgroups_raw, list) or not talkgroups_raw:
                    filtered_sites.append(row)
                    continue
                groups_map = row_dict.get("talkgroup_groups") if isinstance(row_dict.get("talkgroup_groups"), dict) else {}
                labels_map = row_dict.get("talkgroup_labels") if isinstance(row_dict.get("talkgroup_labels"), dict) else {}
                default_agency = str(
                    row_dict.get("department_name")
                    or row_dict.get("site_name")
                    or row_dict.get("system_name")
                    or ""
                ).strip()
                kept_talkgroups: list[int] = []
                kept_groups: dict[str, str] = {}
                kept_labels: dict[str, str] = {}
                for raw_tgid in talkgroups_raw:
                    tgid = self._parse_int(raw_tgid)
                    if tgid is None or tgid <= 0:
                        continue
                    tgid_key = str(int(tgid))
                    agency_name = str(groups_map.get(tgid_key) or default_agency or "").strip()
                    agency_token = self._trunked_agency_token(system_id, agency_name)
                    if agency_token and agency_token in avoids:
                        continue
                    kept_talkgroups.append(int(tgid))
                    label = str(labels_map.get(tgid_key) or "").strip()
                    if label:
                        kept_labels[tgid_key] = label
                    group = str(groups_map.get(tgid_key) or "").strip()
                    if group:
                        kept_groups[tgid_key] = group
                if not kept_talkgroups:
                    continue
                patched_row = dict(row_dict)
                patched_row["talkgroups"] = kept_talkgroups
                patched_row["talkgroup_labels"] = kept_labels
                patched_row["talkgroup_groups"] = kept_groups
                filtered_sites.append(patched_row)
            pool["trunked_sites"] = filtered_sites

        conventional = pool.get("conventional")
        if isinstance(conventional, list):
            filtered_conventional: list[dict] = []
            for row in conventional:
                if not isinstance(row, dict):
                    filtered_conventional.append(row)
                    continue
                row_tokens: set[str] = set()
                system_key = self._normalize_system_token(row.get("system_key"))
                if system_key:
                    row_tokens.add(system_key)
                freq = self._parse_float(row.get("frequency"))
                if freq is not None and freq > 0:
                    row_tokens.add(self._normalize_system_token(f"convfreq:{float(freq):.6f}"))
                if row_tokens and (row_tokens & avoids):
                    continue
                filtered_conventional.append(row)
            pool["conventional"] = filtered_conventional
        return pool


_SCAN_MODE_CONTROLLER: ScanModeController | None = None
_SCAN_MODE_LOCK = threading.Lock()


def get_scan_mode_controller(db_path: str = _DEFAULT_DB_PATH) -> ScanModeController:
    global _SCAN_MODE_CONTROLLER
    with _SCAN_MODE_LOCK:
        if _SCAN_MODE_CONTROLLER is None:
            _SCAN_MODE_CONTROLLER = ScanModeController(db_path=db_path)
        return _SCAN_MODE_CONTROLLER
