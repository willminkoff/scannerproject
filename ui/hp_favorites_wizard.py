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


def _hz_to_mhz(value: int) -> float:
    return round(float(value) / 1_000_000.0, 6)


def _token_sort(value: str) -> tuple[int, str]:
    token = str(value or "").strip()
    if token.isdigit():
        return (0, f"{int(token):010d}")
    return (1, token.lower())


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

    def get_digital_systems(self, state_id: int, county_id: int = 0, text_filter: str = "") -> list[dict]:
        filter_token = f"%{str(text_filter or '').strip().lower()}%"
        state_id = int(state_id)
        county_id = int(county_id or 0)
        with self._connect() as conn:
            if county_id > 0:
                rows = conn.execute(
                    """
                    SELECT DISTINCT ts.trunk_id, ts.system_name, ts.protocol
                    FROM trunk_systems ts
                    JOIN entity_areas ea
                      ON ea.entity_kind = 'TrunkId'
                     AND ea.entity_id = ts.trunk_id
                    WHERE (ea.state_id = ? OR ts.state_id = ?)
                      AND ea.county_id = ?
                      AND lower(ts.system_name) LIKE ?
                    ORDER BY ts.system_name COLLATE NOCASE, ts.trunk_id
                    """,
                    (state_id, state_id, county_id, filter_token),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT DISTINCT ts.trunk_id, ts.system_name, ts.protocol
                    FROM trunk_systems ts
                    WHERE (
                        ts.state_id = ?
                        OR EXISTS (
                            SELECT 1
                            FROM entity_areas ea
                            WHERE ea.entity_kind = 'TrunkId'
                              AND ea.entity_id = ts.trunk_id
                              AND ea.state_id = ?
                        )
                    )
                      AND lower(ts.system_name) LIKE ?
                    ORDER BY ts.system_name COLLATE NOCASE, ts.trunk_id
                    """,
                    (state_id, state_id, filter_token),
                ).fetchall()
        return [
            {
                "id": int(row["trunk_id"]),
                "key": f"TrunkId:{int(row['trunk_id'])}",
                "name": str(row["system_name"] or "").strip(),
                "protocol": str(row["protocol"] or "").strip(),
                "system_type": "digital",
            }
            for row in rows
        ]

    def get_analog_systems(self, state_id: int, county_id: int = 0, text_filter: str = "") -> list[dict]:
        filter_token = f"%{str(text_filter or '').strip().lower()}%"
        state_id = int(state_id)
        county_id = int(county_id or 0)
        with self._connect() as conn:
            if county_id > 0:
                rows = conn.execute(
                    """
                    SELECT system_key, system_name, category, county_id
                    FROM conventional_systems
                    WHERE state_id = ?
                      AND (county_id = ? OR county_id IS NULL)
                      AND lower(system_name) LIKE ?
                    ORDER BY system_name COLLATE NOCASE, system_key
                    """,
                    (state_id, county_id, filter_token),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT system_key, system_name, category, county_id
                    FROM conventional_systems
                    WHERE state_id = ?
                      AND lower(system_name) LIKE ?
                    ORDER BY system_name COLLATE NOCASE, system_key
                    """,
                    (state_id, filter_token),
                ).fetchall()
        return [
            {
                "id": str(row["system_key"] or "").strip(),
                "key": str(row["system_key"] or "").strip(),
                "name": str(row["system_name"] or "").strip(),
                "category": str(row["category"] or "").strip(),
                "county_id": int(row["county_id"]) if row["county_id"] is not None else None,
                "system_type": "analog",
            }
            for row in rows
            if str(row["system_key"] or "").strip()
        ]

    def get_systems(
        self,
        state_id: int,
        county_id: int = 0,
        system_type: str = "digital",
        text_filter: str = "",
    ) -> list[dict]:
        token = str(system_type or "").strip().lower()
        if token == "analog":
            return self.get_analog_systems(state_id=state_id, county_id=county_id, text_filter=text_filter)
        return self.get_digital_systems(state_id=state_id, county_id=county_id, text_filter=text_filter)

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
