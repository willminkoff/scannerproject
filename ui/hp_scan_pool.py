"""HomePatrol full-database scan pool builder."""
from __future__ import annotations

import math
import sqlite3
from pathlib import Path


_EARTH_RADIUS_MILES = 3958.7613


def haversine_miles(lat1, lon1, lat2, lon2) -> float:
    """Return great-circle distance in miles between two WGS84 points."""
    lat1r = math.radians(float(lat1))
    lon1r = math.radians(float(lon1))
    lat2r = math.radians(float(lat2))
    lon2r = math.radians(float(lon2))
    dlat = lat2r - lat1r
    dlon = lon2r - lon1r
    a = (
        math.sin(dlat / 2.0) ** 2
        + math.cos(lat1r) * math.cos(lat2r) * math.sin(dlon / 2.0) ** 2
    )
    c = 2.0 * math.asin(math.sqrt(a))
    return _EARTH_RADIUS_MILES * c


def _hz_to_mhz(freq_hz: int) -> float:
    return round(float(freq_hz) / 1_000_000.0, 6)


class ScanPoolBuilder:
    """Build scanner pools from HomePatrol SQLite data."""

    def __init__(self, db_path: str):
        self.db_path = str(Path(db_path).expanduser().resolve())
        self._indexes_ready = False
        self._bootstrap_indexes()

    @staticmethod
    def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
        try:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        except Exception:
            return False
        for row in rows:
            if str(row[1]).strip().lower() == str(column).strip().lower():
                return True
        return False

    def _ensure_indexes(self, conn: sqlite3.Connection) -> None:
        # Some requested index keys use "system_id", but this DB stores trunk IDs as "trunk_id".
        # Build compatible equivalents when system_id columns are absent.
        if self._table_has_column(conn, "trunk_sites", "system_id"):
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trunk_sites_system_id ON trunk_sites(system_id)"
            )
        elif self._table_has_column(conn, "trunk_sites", "trunk_id"):
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trunk_sites_system_id ON trunk_sites(trunk_id)"
            )

        if self._table_has_column(conn, "trunk_sites", "latitude") and self._table_has_column(
            conn, "trunk_sites", "longitude"
        ):
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trunk_sites_lat_lon ON trunk_sites(latitude, longitude)"
            )

        if self._table_has_column(conn, "talkgroups", "system_id"):
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_talkgroups_system_service "
                "ON talkgroups(system_id, service_tag)"
            )
        elif self._table_has_column(conn, "talkgroups", "tgroup_id"):
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_talkgroups_system_service "
                "ON talkgroups(tgroup_id, service_tag)"
            )

        if self._table_has_column(conn, "conventional_freqs", "service_tag"):
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conventional_freqs_service_tag "
                "ON conventional_freqs(service_tag)"
            )

        if self._table_has_column(conn, "conventional_groups", "latitude") and self._table_has_column(
            conn, "conventional_groups", "longitude"
        ):
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conventional_groups_lat_lon "
                "ON conventional_groups(latitude, longitude)"
            )

    def _bootstrap_indexes(self) -> None:
        if self._indexes_ready:
            return
        conn = sqlite3.connect(self.db_path)
        try:
            self._ensure_indexes(conn)
            conn.commit()
            self._indexes_ready = True
        finally:
            conn.close()

    @staticmethod
    def _normalize_service_tags(service_tags: list[int]) -> list[int]:
        seen: set[int] = set()
        out: list[int] = []
        for raw in service_tags or []:
            try:
                value = int(raw)
            except Exception:
                continue
            if value in seen:
                continue
            seen.add(value)
            out.append(value)
        return out

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

    def build_full_database_pool(
        self,
        lat: float,
        lon: float,
        range_miles: float,
        service_tags: list[int],
    ) -> dict:
        center_lat = float(lat)
        center_lon = float(lon)
        user_range = max(0.0, float(range_miles))
        lat_miles_per_degree = 69.0
        lon_miles_per_degree = max(
            1e-6,
            69.0 * abs(math.cos(math.radians(center_lat))),
        )
        tags = self._normalize_service_tags(service_tags)
        if not tags:
            return {"trunked_sites": [], "conventional": []}

        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            if not self._indexes_ready:
                self._ensure_indexes(conn)
                conn.commit()
                self._indexes_ready = True

            tag_placeholders = ",".join("?" for _ in tags)

            site_rows = conn.execute(
                """
                SELECT
                    ts.site_id,
                    ts.trunk_id,
                    ts.latitude,
                    ts.longitude,
                    ts.radius,
                    ts.site_name,
                    sys.system_name
                FROM trunk_sites ts
                LEFT JOIN trunk_systems sys ON sys.trunk_id = ts.trunk_id
                ORDER BY ts.trunk_id, ts.site_id
                """
            ).fetchall()

            selected_sites: list[dict[str, object]] = []
            selected_by_system: dict[int, set[int]] = {}
            for row in site_rows:
                site_id = self._parse_int(row["site_id"])
                system_id = self._parse_int(row["trunk_id"])
                if site_id is None or system_id is None:
                    continue
                site_lat = self._parse_float(row["latitude"])
                site_lon = self._parse_float(row["longitude"])
                if site_lat is None or site_lon is None:
                    continue
                site_radius = self._parse_float(row["radius"])
                if site_radius is None:
                    site_radius = 0.0
                site_radius = max(0.0, float(site_radius))
                threshold = site_radius + user_range
                if abs(site_lat - center_lat) * lat_miles_per_degree > threshold:
                    continue
                if abs(site_lon - center_lon) * lon_miles_per_degree > threshold:
                    continue
                distance = haversine_miles(center_lat, center_lon, site_lat, site_lon)
                if distance > threshold:
                    continue
                selected_sites.append(
                    {
                        "system_id": int(system_id),
                        "site_id": int(site_id),
                        "system_name": str(row["system_name"] or "").strip(),
                        "site_name": str(row["site_name"] or "").strip(),
                    }
                )
                selected_by_system.setdefault(system_id, set()).add(site_id)

            control_channels_by_site: dict[int, list[float]] = {}
            if selected_sites:
                site_ids = sorted(
                    {
                        int(row.get("site_id") or 0)
                        for row in selected_sites
                        if int(row.get("site_id") or 0) > 0
                    }
                )
                site_placeholders = ",".join("?" for _ in site_ids)
                freq_rows = conn.execute(
                    f"""
                    SELECT site_id, freq_hz
                    FROM trunk_freqs
                    WHERE site_id IN ({site_placeholders})
                      AND freq_hz IS NOT NULL
                    ORDER BY site_id, freq_hz, COALESCE(lcn, '')
                    """,
                    site_ids,
                ).fetchall()
                bucket: dict[int, set[float]] = {}
                for row in freq_rows:
                    site_id = self._parse_int(row["site_id"])
                    freq_hz = self._parse_int(row["freq_hz"])
                    if site_id is None or freq_hz is None or freq_hz <= 0:
                        continue
                    bucket.setdefault(site_id, set()).add(_hz_to_mhz(freq_hz))
                control_channels_by_site = {
                    site_id: sorted(list(values))
                    for site_id, values in bucket.items()
                }

            talkgroups_by_system: dict[int, list[int]] = {}
            talkgroup_labels_by_system: dict[int, dict[int, str]] = {}
            talkgroup_groups_by_system: dict[int, dict[int, str]] = {}
            if selected_by_system:
                system_ids = sorted(selected_by_system.keys())
                system_placeholders = ",".join("?" for _ in system_ids)
                tgid_rows = conn.execute(
                    f"""
                    SELECT
                        tg.trunk_id,
                        t.dec_tgid,
                        t.alpha_tag,
                        tg.group_name
                    FROM talkgroups t
                    JOIN trunk_groups tg ON tg.tgroup_id = t.tgroup_id
                    WHERE t.service_tag IN ({tag_placeholders})
                      AND tg.trunk_id IN ({system_placeholders})
                    ORDER BY tg.trunk_id, t.dec_tgid, t.tid
                    """,
                    [*tags, *system_ids],
                ).fetchall()
                tg_bucket: dict[int, set[int]] = {}
                for row in tgid_rows:
                    system_id = self._parse_int(row["trunk_id"])
                    if system_id is None or system_id not in selected_by_system:
                        continue
                    dec_text = str(row["dec_tgid"] or "").strip()
                    if not dec_text.isdigit():
                        continue
                    dec_tgid = int(dec_text)
                    tg_bucket.setdefault(system_id, set()).add(dec_tgid)
                    alpha_tag = str(row["alpha_tag"] or "").strip()
                    if alpha_tag:
                        per_system_labels = talkgroup_labels_by_system.setdefault(system_id, {})
                        if dec_tgid not in per_system_labels:
                            per_system_labels[dec_tgid] = alpha_tag
                    group_name = str(row["group_name"] or "").strip()
                    if group_name:
                        per_system_groups = talkgroup_groups_by_system.setdefault(system_id, {})
                        if dec_tgid not in per_system_groups:
                            per_system_groups[dec_tgid] = group_name
                talkgroups_by_system = {
                    system_id: sorted(list(values))
                    for system_id, values in tg_bucket.items()
                }

            trunked_sites: list[dict] = []
            for row in sorted(
                selected_sites,
                key=lambda item: (
                    int(item.get("system_id") or 0),
                    int(item.get("site_id") or 0),
                ),
            ):
                system_id = int(row.get("system_id") or 0)
                site_id = int(row.get("site_id") or 0)
                control_channels = list(control_channels_by_site.get(site_id) or [])
                talkgroups = list(talkgroups_by_system.get(system_id) or [])
                if not control_channels and not talkgroups:
                    continue
                labels_map = talkgroup_labels_by_system.get(system_id) or {}
                groups_map = talkgroup_groups_by_system.get(system_id) or {}
                talkgroup_labels: dict[str, str] = {}
                talkgroup_groups: dict[str, str] = {}
                for tgid in talkgroups:
                    label = str(labels_map.get(tgid) or "").strip()
                    group = str(groups_map.get(tgid) or "").strip()
                    if label:
                        talkgroup_labels[str(tgid)] = label
                    if group:
                        talkgroup_groups[str(tgid)] = group
                trunked_sites.append(
                    {
                        "system_id": int(system_id),
                        "site_id": int(site_id),
                        "system_name": str(row.get("system_name") or "").strip(),
                        "site_name": str(row.get("site_name") or "").strip(),
                        "department_name": str(row.get("site_name") or "").strip(),
                        "control_channels": control_channels,
                        "talkgroups": talkgroups,
                        "talkgroup_labels": talkgroup_labels,
                        "talkgroup_groups": talkgroup_groups,
                    }
                )

            conv_rows = conn.execute(
                f"""
                SELECT
                    cf.freq_hz,
                    cf.alpha_tag,
                    cf.service_tag,
                    cg.latitude,
                    cg.longitude,
                    cg.radius
                FROM conventional_freqs cf
                JOIN conventional_groups cg ON cg.cgroup_id = cf.cgroup_id
                WHERE cf.service_tag IN ({tag_placeholders})
                  AND cf.freq_hz IS NOT NULL
                ORDER BY cf.cgroup_id, cf.freq_hz, cf.cfreq_id
                """,
                tags,
            ).fetchall()

            conventional_set: set[tuple[float, str, int]] = set()
            for row in conv_rows:
                freq_hz = self._parse_int(row["freq_hz"])
                service_tag = self._parse_int(row["service_tag"])
                group_lat = self._parse_float(row["latitude"])
                group_lon = self._parse_float(row["longitude"])
                group_radius = self._parse_float(row["radius"])
                if (
                    freq_hz is None
                    or freq_hz <= 0
                    or service_tag is None
                    or group_lat is None
                    or group_lon is None
                ):
                    continue
                radius = max(0.0, float(group_radius or 0.0))
                threshold = radius + user_range
                if abs(group_lat - center_lat) * lat_miles_per_degree > threshold:
                    continue
                if abs(group_lon - center_lon) * lon_miles_per_degree > threshold:
                    continue
                distance = haversine_miles(center_lat, center_lon, group_lat, group_lon)
                if distance > threshold:
                    continue
                alpha_tag = str(row["alpha_tag"] or "").strip()
                conventional_set.add((_hz_to_mhz(freq_hz), alpha_tag, service_tag))

            conventional = [
                {
                    "frequency": freq,
                    "alpha_tag": alpha,
                    "service_tag": service_tag,
                }
                for freq, alpha, service_tag in sorted(
                    conventional_set,
                    key=lambda item: (item[0], item[2], item[1].lower()),
                )
            ]

            return {
                "trunked_sites": trunked_sites,
                "conventional": conventional,
            }
        finally:
            conn.close()
