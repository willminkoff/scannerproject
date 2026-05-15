import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from ui.hp_scan_pool import ScanPoolBuilder


class HpScanPoolRadiusGateTests(unittest.TestCase):
    @staticmethod
    def _create_db(path: str) -> None:
        with sqlite3.connect(path) as conn:
            conn.executescript(
                """
                CREATE TABLE trunk_systems (
                    trunk_id INTEGER PRIMARY KEY,
                    system_name TEXT
                );
                CREATE TABLE trunk_sites (
                    site_id INTEGER PRIMARY KEY,
                    source_file TEXT,
                    trunk_id INTEGER,
                    site_name TEXT,
                    latitude REAL,
                    longitude REAL,
                    radius REAL
                );
                CREATE TABLE trunk_freqs (
                    site_id INTEGER,
                    freq_hz INTEGER,
                    lcn TEXT
                );
                CREATE TABLE trunk_groups (
                    tgroup_id INTEGER PRIMARY KEY,
                    trunk_id INTEGER,
                    group_name TEXT,
                    latitude REAL,
                    longitude REAL,
                    radius REAL
                );
                CREATE TABLE talkgroups (
                    tid INTEGER PRIMARY KEY,
                    tgroup_id INTEGER,
                    dec_tgid TEXT,
                    alpha_tag TEXT,
                    service_tag INTEGER
                );
                CREATE TABLE conventional_groups (
                    cgroup_id INTEGER PRIMARY KEY,
                    source_file TEXT,
                    parent_key TEXT,
                    parent_id INTEGER,
                    latitude REAL,
                    longitude REAL,
                    radius REAL
                );
                CREATE TABLE conventional_freqs (
                    cfreq_id INTEGER PRIMARY KEY,
                    cgroup_id INTEGER,
                    freq_hz INTEGER,
                    alpha_tag TEXT,
                    service_tag INTEGER
                );
                """
            )
            conn.execute("INSERT INTO trunk_systems(trunk_id, system_name) VALUES (?, ?)", (100, "Radius Test"))
            conn.executemany(
                """
                INSERT INTO trunk_sites(site_id, source_file, trunk_id, site_name, latitude, longitude, radius)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (1, "TN.hpd", 100, "Small Nearby", 0.0, 0.01, 2.0),
                    (2, "TN.hpd", 100, "Wide Simulcast", 0.0, 0.20, 20.0),
                    (3, "TN.hpd", 100, "Outside Radius", 0.0, 0.30, 5.0),
                    (4, "TN.hpd", 100, "Unknown Radius", 0.0, 1.00, 0.0),
                ],
            )
            conn.executemany(
                "INSERT INTO trunk_freqs(site_id, freq_hz, lcn) VALUES (?, ?, ?)",
                [
                    (1, 851100000, "1"),
                    (2, 852200000, "1"),
                    (3, 853300000, "1"),
                    (4, 854400000, "1"),
                ],
            )
            conn.execute(
                "INSERT INTO trunk_groups(tgroup_id, trunk_id, group_name, latitude, longitude, radius) VALUES (?, ?, ?, ?, ?, ?)",
                (10, 100, "Dispatch", None, None, None),
            )
            conn.execute(
                "INSERT INTO talkgroups(tid, tgroup_id, dec_tgid, alpha_tag, service_tag) VALUES (?, ?, ?, ?, ?)",
                (1, 10, "3207", "Dispatch", 2),
            )
            conn.commit()

    def test_radius_gate_keeps_in_radius_sites_and_caps_by_radius(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "hp.db")
            self._create_db(db_path)
            builder = ScanPoolBuilder(db_path)

            with mock.patch.dict(os.environ, {"HP_TRUNK_MAX_SITES_PER_SYSTEM": "2"}, clear=False):
                pool = builder.build_full_database_pool(
                    lat=0.0,
                    lon=0.0,
                    range_miles=1.0,
                    service_tags=[2],
                )

            trunked = pool.get("trunked_sites") or []
            self.assertEqual([2, 1], [int(row["site_id"]) for row in trunked])
            self.assertEqual(["Wide Simulcast", "Small Nearby"], [row["site_name"] for row in trunked])
            self.assertNotIn(3, {int(row["site_id"]) for row in trunked})
            self.assertEqual(20.0, trunked[0]["radius"])
            self.assertAlmostEqual(0.20, trunked[0]["longitude"])
            self.assertEqual([3207], trunked[0]["talkgroups"])

    def test_zero_radius_sites_fail_open_when_cap_allows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "hp.db")
            self._create_db(db_path)
            builder = ScanPoolBuilder(db_path)

            with mock.patch.dict(os.environ, {"HP_TRUNK_MAX_SITES_PER_SYSTEM": "5"}, clear=False):
                pool = builder.build_full_database_pool(
                    lat=0.0,
                    lon=0.0,
                    range_miles=1.0,
                    service_tags=[2],
                )

            site_ids = [int(row["site_id"]) for row in pool.get("trunked_sites") or []]
            self.assertEqual([2, 1, 4], site_ids)


if __name__ == "__main__":
    unittest.main()
