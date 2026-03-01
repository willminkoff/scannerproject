"""Favorites-builder browse queries for HomePatrol DB."""
from __future__ import annotations

import sqlite3
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_DB_PATH = str((_REPO_ROOT / "data" / "homepatrol.db").resolve())
_COUNTRY_NAMES = {
    1: "USA",
    2: "Canada",
}
_SCOPE_NATIONWIDE = "nationwide"
_SCOPE_STATEWIDE = "statewide"
_SCOPE_COUNTY = "county"
_VALID_SCOPES = {_SCOPE_NATIONWIDE, _SCOPE_STATEWIDE, _SCOPE_COUNTY}


def _hz_to_mhz(value: int) -> float:
    return round(float(value) / 1_000_000.0, 6)


def _token_sort(value: str) -> tuple[int, str]:
    token = str(value or "").strip()
    if token.isdigit():
        return (0, f"{int(token):010d}")
    return (1, token.lower())


def _normalize_scope(scope: str) -> str:
    token = str(scope or "").strip().lower()
    if token in _VALID_SCOPES:
        return token
    return _SCOPE_STATEWIDE


class HPFavoritesWizard:
    def __init__(self, db_path: str = _DEFAULT_DB_PATH):
        self.db_path = str(Path(db_path).expanduser().resolve())

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def get_countries(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT country_id
                FROM states
                WHERE country_id IN (1,2)
                ORDER BY country_id
                """
            ).fetchall()
        out: list[dict] = []
        for row in rows:
            country_id = int(row["country_id"])
            out.append(
                {
                    "country_id": country_id,
                    "name": _COUNTRY_NAMES.get(country_id, f"Country {country_id}"),
                }
            )
        return out

    def get_states(self, country_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT state_id, name, abbr
                FROM states
                WHERE country_id = ?
                  AND state_id > 0
                ORDER BY name COLLATE NOCASE
                """,
                (int(country_id),),
            ).fetchall()
        return [
            {
                "state_id": int(row["state_id"]),
                "name": str(row["name"] or "").strip(),
                "abbr": str(row["abbr"] or "").strip(),
            }
            for row in rows
        ]

    def get_counties(self, state_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT county_id, name
                FROM counties
                WHERE state_id = ?
                ORDER BY name COLLATE NOCASE
                """,
                (int(state_id),),
            ).fetchall()
        out = [
            {
                "county_id": int(row["county_id"]),
                "name": str(row["name"] or "").strip(),
            }
            for row in rows
        ]
        out.insert(0, {"county_id": 0, "name": "All Counties"})
        return out

    def get_digital_systems(
        self,
        state_id: int,
        county_id: int = 0,
        scope: str = _SCOPE_STATEWIDE,
        text_filter: str = "",
    ) -> list[dict]:
        filter_token = f"%{str(text_filter or '').strip().lower()}%"
        state_id = int(state_id)
        county_id = int(county_id or 0)
        scope_token = _normalize_scope(scope)
        with self._connect() as conn:
            state_county_rows = conn.execute(
                """
                SELECT county_id
                FROM counties
                WHERE state_id = ?
                """,
                (state_id,),
            ).fetchall()
            state_county_ids: set[int] = set()
            for row in state_county_rows:
                try:
                    value = int(row["county_id"])
                except Exception:
                    continue
                if value > 0:
                    state_county_ids.add(value)

            rows = conn.execute(
                """
                SELECT DISTINCT
                    ts.trunk_id,
                    ts.system_name,
                    ts.protocol,
                    ts.source_file,
                    ts.state_id
                FROM trunk_systems ts
                LEFT JOIN entity_areas ea
                  ON ea.entity_kind = 'TrunkId'
                 AND ea.entity_id = ts.trunk_id
                LEFT JOIN counties c
                  ON c.county_id = ea.county_id
                WHERE lower(ts.system_name) LIKE ?
                  AND (
                      ts.state_id = ?
                      OR ts.state_id = 0
                      OR (ea.record_type = 'AreaState' AND ea.state_id = ?)
                      OR (
                          ea.record_type = 'AreaCounty'
                          AND (
                              ea.county_id = 0
                              OR (? > 0 AND ea.county_id = ?)
                              OR (? <= 0 AND c.state_id = ?)
                          )
                      )
                  )
                ORDER BY ts.system_name COLLATE NOCASE, ts.trunk_id
                """,
                (filter_token, state_id, state_id, county_id, county_id, county_id, state_id),
            ).fetchall()

            trunk_ids: list[int] = []
            for row in rows:
                try:
                    tid = int(row["trunk_id"])
                except Exception:
                    continue
                if tid > 0:
                    trunk_ids.append(tid)
            if not trunk_ids:
                return []

            placeholders = ",".join("?" for _ in trunk_ids)
            area_rows = conn.execute(
                f"""
                SELECT
                    entity_id AS trunk_id,
                    record_type,
                    state_id,
                    county_id
                FROM entity_areas
                WHERE entity_kind = 'TrunkId'
                  AND entity_id IN ({placeholders})
                """,
                trunk_ids,
            ).fetchall()

        area_by_trunk: dict[int, list[tuple[str, int | None, int | None]]] = {}
        for row in area_rows:
            try:
                tid = int(row["trunk_id"])
            except Exception:
                continue
            record_type = str(row["record_type"] or "").strip()
            try:
                state_val = int(row["state_id"]) if row["state_id"] is not None else None
            except Exception:
                state_val = None
            try:
                county_val = int(row["county_id"]) if row["county_id"] is not None else None
            except Exception:
                county_val = None
            area_by_trunk.setdefault(tid, []).append((record_type, state_val, county_val))

        out: list[dict] = []
        for row in rows:
            try:
                trunk_id = int(row["trunk_id"])
            except Exception:
                continue
            if trunk_id <= 0:
                continue
            system_name = str(row["system_name"] or "").strip()
            protocol = str(row["protocol"] or "").strip()
            source_file = str(row["source_file"] or "").strip().lower()
            try:
                ts_state_id = int(row["state_id"]) if row["state_id"] is not None else None
            except Exception:
                ts_state_id = None

            areas = area_by_trunk.get(trunk_id) or []
            area_states = {a_state for a_type, a_state, _ in areas if a_type == "AreaState" and a_state is not None}
            area_counties = {
                a_county
                for a_type, _, a_county in areas
                if a_type == "AreaCounty" and a_county is not None and a_county > 0
            }
            has_global_county = any(
                a_type == "AreaCounty" and int(a_county or 0) == 0
                for a_type, _, a_county in areas
            )

            if county_id > 0:
                county_scope_match = county_id in area_counties
            else:
                county_scope_match = bool(area_counties & state_county_ids)

            has_state = bool(
                (ts_state_id == state_id)
                or (state_id in area_states)
                or bool(area_counties & state_county_ids)
            )
            statewide_scope_match = has_state
            is_nationwide = bool(
                source_file == "_multiplestates.hpd"
                or ts_state_id == 0
                or has_global_county
            )
            nationwide_scope_match = is_nationwide

            if scope_token == _SCOPE_COUNTY and not county_scope_match:
                continue
            if scope_token == _SCOPE_STATEWIDE and not statewide_scope_match:
                continue
            if scope_token == _SCOPE_NATIONWIDE and not nationwide_scope_match:
                continue

            out.append(
                {
                    "id": trunk_id,
                    "key": f"TrunkId:{trunk_id}",
                    "name": system_name,
                    "protocol": protocol,
                    "system_type": "digital",
                    "scope": scope_token,
                }
            )
        return out

    def get_analog_systems(
        self,
        state_id: int,
        county_id: int = 0,
        scope: str = _SCOPE_STATEWIDE,
        text_filter: str = "",
    ) -> list[dict]:
        filter_token = f"%{str(text_filter or '').strip().lower()}%"
        state_id = int(state_id)
        county_id = int(county_id or 0)
        scope_token = _normalize_scope(scope)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    system_key,
                    system_name,
                    category,
                    county_id,
                    source_file
                FROM conventional_systems
                WHERE state_id = ?
                  AND lower(system_name) LIKE ?
                ORDER BY system_name COLLATE NOCASE, system_key
                """,
                (state_id, filter_token),
            ).fetchall()

        out: list[dict] = []
        for row in rows:
            system_key = str(row["system_key"] or "").strip()
            if not system_key:
                continue
            try:
                row_county_id = int(row["county_id"]) if row["county_id"] is not None else None
            except Exception:
                row_county_id = None
            source_file = str(row["source_file"] or "").strip().lower()

            if row_county_id is not None and row_county_id > 0:
                if county_id > 0 and row_county_id != county_id:
                    continue
                system_scope = _SCOPE_COUNTY
            elif source_file == "_multiplestates.hpd":
                system_scope = _SCOPE_NATIONWIDE
            else:
                system_scope = _SCOPE_STATEWIDE

            if system_scope != scope_token:
                continue

            out.append(
                {
                    "id": system_key,
                    "key": system_key,
                    "name": str(row["system_name"] or "").strip(),
                    "category": str(row["category"] or "").strip(),
                    "county_id": row_county_id,
                    "system_type": "analog",
                    "scope": system_scope,
                }
            )
        return out

    def get_systems(
        self,
        state_id: int,
        county_id: int = 0,
        system_type: str = "digital",
        scope: str = _SCOPE_STATEWIDE,
        text_filter: str = "",
    ) -> list[dict]:
        token = str(system_type or "").strip().lower()
        if token == "analog":
            return self.get_analog_systems(
                state_id=state_id,
                county_id=county_id,
                scope=scope,
                text_filter=text_filter,
            )
        return self.get_digital_systems(
            state_id=state_id,
            county_id=county_id,
            scope=scope,
            text_filter=text_filter,
        )

    def _digital_control_channels(self, trunk_id: int) -> list[float]:
        with self._connect() as conn:
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
        out: list[float] = []
        seen: set[float] = set()
        for row in rows:
            try:
                hz = int(row["freq_hz"])
            except Exception:
                continue
            if hz <= 0:
                continue
            mhz = _hz_to_mhz(hz)
            if mhz in seen:
                continue
            seen.add(mhz)
            out.append(mhz)
        out.sort()
        return out

    def get_digital_channels(self, trunk_id: int, text_filter: str = "") -> tuple[str, list[dict]]:
        trunk_id = int(trunk_id)
        filter_token = f"%{str(text_filter or '').strip().lower()}%"
        with self._connect() as conn:
            system_row = conn.execute(
                """
                SELECT system_name
                FROM trunk_systems
                WHERE trunk_id = ?
                """,
                (trunk_id,),
            ).fetchone()
            if not system_row:
                return "", []
            system_name = str(system_row["system_name"] or "").strip()
            rows = conn.execute(
                """
                SELECT
                    t.dec_tgid,
                    t.alpha_tag,
                    t.service_tag,
                    t.mode,
                    tg.group_name
                FROM talkgroups t
                JOIN trunk_groups tg ON tg.tgroup_id = t.tgroup_id
                WHERE tg.trunk_id = ?
                  AND t.dec_tgid IS NOT NULL
                  AND (
                      lower(t.alpha_tag) LIKE ?
                      OR lower(t.dec_tgid) LIKE ?
                      OR lower(tg.group_name) LIKE ?
                  )
                ORDER BY CAST(t.dec_tgid AS INTEGER), t.alpha_tag COLLATE NOCASE
                """,
                (trunk_id, filter_token, filter_token, filter_token),
            ).fetchall()
        controls = self._digital_control_channels(trunk_id)
        out: list[dict] = []
        seen_ids: set[str] = set()
        for row in rows:
            talkgroup = str(row["dec_tgid"] or "").strip()
            if not talkgroup.isdigit():
                continue
            alpha_tag = str(row["alpha_tag"] or "").strip()
            group_name = str(row["group_name"] or "").strip()
            service_tag = int(row["service_tag"] or 0)
            channel_id = f"tgid:{trunk_id}:{talkgroup}"
            if channel_id in seen_ids:
                continue
            seen_ids.add(channel_id)
            out.append(
                {
                    "id": channel_id,
                    "kind": "trunked",
                    "system_id": trunk_id,
                    "system_name": system_name,
                    "department_name": group_name or system_name,
                    "alpha_tag": alpha_tag or f"TGID {talkgroup}",
                    "talkgroup": int(talkgroup),
                    "service_tag": service_tag,
                    "mode": str(row["mode"] or "").strip(),
                    "control_channels": controls,
                }
            )
        out.sort(key=lambda row: (int(row.get("talkgroup") or 0), str(row.get("alpha_tag") or "").lower()))
        return system_name, out

    @staticmethod
    def _parse_system_key(system_key: str) -> tuple[str, int] | None:
        token = str(system_key or "").strip()
        if ":" not in token:
            return None
        key, value = token.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key not in {"AgencyId", "CountyId"} or not value.isdigit():
            return None
        return key, int(value)

    def get_analog_channels(self, system_key: str, text_filter: str = "") -> tuple[str, list[dict]]:
        parsed = self._parse_system_key(system_key)
        if not parsed:
            return "", []
        parent_key, parent_id = parsed
        filter_token = f"%{str(text_filter or '').strip().lower()}%"
        with self._connect() as conn:
            system_row = conn.execute(
                """
                SELECT system_name
                FROM conventional_systems
                WHERE system_key = ?
                """,
                (str(system_key),),
            ).fetchone()
            if not system_row:
                return "", []
            system_name = str(system_row["system_name"] or "").strip()
            rows = conn.execute(
                """
                SELECT
                    cf.cfreq_id,
                    cf.alpha_tag,
                    cf.freq_hz,
                    cf.service_tag,
                    cf.mode,
                    cg.group_name
                FROM conventional_groups cg
                JOIN conventional_freqs cf ON cf.cgroup_id = cg.cgroup_id
                WHERE cg.parent_key = ?
                  AND cg.parent_id = ?
                  AND cf.freq_hz IS NOT NULL
                  AND (
                      lower(cf.alpha_tag) LIKE ?
                      OR lower(cg.group_name) LIKE ?
                      OR CAST(cf.freq_hz AS TEXT) LIKE ?
                  )
                ORDER BY cg.group_name COLLATE NOCASE, cf.freq_hz, cf.alpha_tag COLLATE NOCASE
                """,
                (parent_key, parent_id, filter_token, filter_token, filter_token),
            ).fetchall()
        out: list[dict] = []
        for row in rows:
            try:
                cfreq_id = int(row["cfreq_id"])
                freq_hz = int(row["freq_hz"])
            except Exception:
                continue
            if freq_hz <= 0:
                continue
            group_name = str(row["group_name"] or "").strip()
            alpha_tag = str(row["alpha_tag"] or "").strip()
            out.append(
                {
                    "id": f"freq:{system_key}:{cfreq_id}",
                    "kind": "conventional",
                    "system_key": str(system_key),
                    "system_name": system_name,
                    "department_name": group_name or system_name,
                    "alpha_tag": alpha_tag or group_name or f"{_hz_to_mhz(freq_hz):.4f}",
                    "frequency": _hz_to_mhz(freq_hz),
                    "service_tag": int(row["service_tag"] or 0),
                    "mode": str(row["mode"] or "").strip(),
                }
            )
        out.sort(key=lambda row: (_token_sort(str(row.get("department_name") or "")), float(row.get("frequency") or 0)))
        return system_name, out

    def get_channels(
        self,
        system_type: str,
        system_id: str,
        text_filter: str = "",
    ) -> tuple[str, list[dict]]:
        token = str(system_type or "").strip().lower()
        if token == "analog":
            return self.get_analog_channels(system_key=str(system_id), text_filter=text_filter)
        return self.get_digital_channels(trunk_id=int(system_id), text_filter=text_filter)
