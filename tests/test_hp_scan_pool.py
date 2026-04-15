from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from ui.hp_scan_pool import ScanPoolBuilder


class FullDatabaseEncryptionFilterTests(unittest.TestCase):
    def _build_db(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        try:
            conn.executescript(
                """
                CREATE TABLE trunk_sites (
                    site_id INTEGER,
                    trunk_id INTEGER,
                    source_file TEXT,
                    latitude REAL,
                    longitude REAL,
                    radius REAL,
                    site_name TEXT
                );
                CREATE TABLE trunk_systems (
                    trunk_id INTEGER,
                    system_name TEXT
                );
                CREATE TABLE trunk_freqs (
                    site_id INTEGER,
                    freq_hz INTEGER,
                    lcn TEXT
                );
                CREATE TABLE trunk_groups (
                    tgroup_id INTEGER,
                    trunk_id INTEGER,
                    group_name TEXT,
                    latitude REAL,
                    longitude REAL,
                    radius REAL
                );
                CREATE TABLE talkgroups (
                    tid INTEGER,
                    tgroup_id INTEGER,
                    alpha_tag TEXT,
                    dec_tgid TEXT,
                    mode TEXT,
                    service_tag INTEGER
                );
                CREATE TABLE conventional_groups (
                    cgroup_id INTEGER,
                    source_file TEXT,
                    parent_key TEXT,
                    parent_id INTEGER,
                    latitude REAL,
                    longitude REAL,
                    radius REAL
                );
                CREATE TABLE conventional_freqs (
                    cfreq_id INTEGER,
                    cgroup_id INTEGER,
                    freq_hz INTEGER,
                    alpha_tag TEXT,
                    service_tag INTEGER
                );
                """
            )
            conn.execute(
                "INSERT INTO trunk_systems(trunk_id, system_name) VALUES (?, ?)",
                (7078, "MTRTRS"),
            )
            conn.execute(
                """
                INSERT INTO trunk_sites(site_id, trunk_id, source_file, latitude, longitude, radius, site_name)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (41154, 7078, "Tennessee.hpd", 36.17, -86.78, 20.0, "Davidson County Services"),
            )
            conn.execute(
                "INSERT INTO trunk_freqs(site_id, freq_hz, lcn) VALUES (?, ?, ?)",
                (41154, 856937500, "1"),
            )
            conn.execute(
                """
                INSERT INTO trunk_groups(tgroup_id, trunk_id, group_name, latitude, longitude, radius)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (9001, 7078, "Police", 36.17, -86.78, 20.0),
            )
            conn.execute(
                """
                INSERT INTO talkgroups(tid, tgroup_id, alpha_tag, dec_tgid, mode, service_tag)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (1, 9001, "Police Dispatch", "3207", "D", 1),
            )
            conn.execute(
                """
                INSERT INTO talkgroups(tid, tgroup_id, alpha_tag, dec_tgid, mode, service_tag)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (2, 9001, "Police Encrypted", "3208", "DE", 1),
            )
            conn.commit()
        finally:
            conn.close()

    def test_build_full_database_pool_filters_encrypted_talkgroups(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "hp.sqlite"
            self._build_db(db_path)
            builder = ScanPoolBuilder(str(db_path))

            pool = builder.build_full_database_pool(
                lat=36.17,
                lon=-86.78,
                range_miles=10.0,
                service_tags=[1],
                include_nationwide=False,
                strict_location=False,
            )

            trunked = pool.get("trunked_sites") or []
            self.assertEqual(1, len(trunked))
            self.assertEqual([3207], trunked[0]["talkgroups"])
            self.assertEqual({"3207": "Police Dispatch"}, trunked[0]["talkgroup_labels"])


if __name__ == "__main__":
    unittest.main()
