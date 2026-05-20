"""Tests for disco/src/hpdb.py — HomePatrol curated-label lookups.

Builds a small fixture SQLite DB with the same schema as the production
HPDB (`data/homepatrol.db`) and exercises the lookup function end-to-end:
- conventional channel lookups (alpha_tag + group_name + service_type)
- trunked control-channel lookups (system_name — site_name + protocol)
- Combined ranking by distance
- Missing-DB graceful fallback
- Empty result when freq doesn't match
- Radius filter excludes far sites
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_DISCO_SRC = str(Path(__file__).resolve().parents[1] / "disco" / "src")
if _DISCO_SRC not in sys.path:
    sys.path.insert(0, _DISCO_SRC)

import hpdb  # noqa: E402


def _build_fixture_db(path: str) -> None:
    """Create a minimal HPDB fixture with the same schema as production."""
    c = sqlite3.connect(path)
    c.executescript(
        """
        CREATE TABLE conventional_freqs (
            cfreq_id INTEGER, source_file TEXT, cgroup_id INTEGER,
            alpha_tag TEXT, freq_hz INTEGER, mode TEXT, tone TEXT,
            service_tag INTEGER
        );
        CREATE TABLE conventional_groups (
            cgroup_id INTEGER, source_file TEXT, parent_key TEXT, parent_id TEXT,
            group_name TEXT, latitude REAL, longitude REAL, radius REAL, shape TEXT
        );
        CREATE TABLE service_types (
            service_tag INTEGER, name TEXT, enabled_by_default INTEGER, is_custom INTEGER
        );
        CREATE TABLE trunk_freqs (
            id INTEGER, source_file TEXT, site_id INTEGER, tfreq_id TEXT,
            freq_hz INTEGER, lcn TEXT
        );
        CREATE TABLE trunk_sites (
            site_id INTEGER, source_file TEXT, trunk_id INTEGER, site_name TEXT,
            latitude REAL, longitude REAL, radius REAL, site_mode TEXT,
            bandplan TEXT, width REAL, shape TEXT
        );
        CREATE TABLE trunk_systems (
            trunk_id INTEGER, source_file TEXT, state_id INTEGER,
            system_name TEXT, system_type TEXT, protocol TEXT
        );
        """
    )

    c.execute("INSERT INTO service_types VALUES (15, 'Aircraft', 1, 0)")
    c.execute("INSERT INTO service_types VALUES (2, 'Law Dispatch', 1, 0)")

    # Conventional: 121.5 MHz aviation emergency near Nashville
    c.execute("INSERT INTO conventional_groups VALUES "
              "(1, 'tn.hpd', 'agency', '4801', 'Nashville International Airport — Aircraft', "
              "36.124, -86.6782, 35.0, NULL)")
    c.execute("INSERT INTO conventional_freqs VALUES "
              "(1001, 'tn.hpd', 1, 'BNA Emergency', 121500000, 'AM', NULL, 15)")

    # Conventional: same freq but in Atlanta (far — should be filtered out
    # when looking from Nashville).
    c.execute("INSERT INTO conventional_groups VALUES "
              "(2, 'ga.hpd', 'agency', '4802', 'Hartsfield-Jackson Atlanta — Aircraft', "
              "33.640, -84.4277, 35.0, NULL)")
    c.execute("INSERT INTO conventional_freqs VALUES "
              "(1002, 'ga.hpd', 2, 'ATL Emergency', 121500000, 'AM', NULL, 15)")

    # Trunked: TACN West Nashville control channel (real-world freq + lat/lon)
    c.execute("INSERT INTO trunk_systems VALUES "
              "(100, 'tn.hpd', 42, 'Tennessee Advanced Communications Network (TACN)', "
              "'Trunked', 'P25X2_TDMA')")
    c.execute("INSERT INTO trunk_sites VALUES "
              "(200, 'tn.hpd', 100, 'West Nashville', 36.11067, -86.96691, "
              "25.0, 'Simulcast', '800MHz', 12.5, NULL)")
    c.execute("INSERT INTO trunk_freqs VALUES "
              "(1, 'tn.hpd', 200, '0', 769456250, '1')")

    # Trunked: same freq but a far-away system (Utah). Should be filtered out.
    c.execute("INSERT INTO trunk_systems VALUES "
              "(101, 'ut.hpd', 49, 'Utah Communications Authority', 'Trunked', 'Motorola')")
    c.execute("INSERT INTO trunk_sites VALUES "
              "(201, 'ut.hpd', 101, 'Mount Ogden', 41.19994, -111.88189, "
              "30.0, 'Standard', '800MHz', 12.5, NULL)")
    c.execute("INSERT INTO trunk_freqs VALUES "
              "(2, 'ut.hpd', 201, '0', 769456250, '2')")

    # Trunked entry with NULL coords — should be excluded by the spatial filter.
    c.execute("INSERT INTO trunk_systems VALUES "
              "(102, 'na.hpd', 0, 'Mystery System', 'Trunked', 'P25Standard')")
    c.execute("INSERT INTO trunk_sites VALUES "
              "(202, 'na.hpd', 102, 'Unknown Site', NULL, NULL, NULL, NULL, NULL, NULL, NULL)")
    c.execute("INSERT INTO trunk_freqs VALUES "
              "(3, 'na.hpd', 202, '0', 769456250, '1')")

    c.commit()
    c.close()


class HpdbLookupTests(unittest.TestCase):
    NASHVILLE_LAT = 36.0662
    NASHVILLE_LON = -86.9639

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = os.path.join(self._tmp.name, "homepatrol.db")
        _build_fixture_db(self.db_path)

        # Reset hpdb's thread-local cached connection between tests since they
        # all use different DB paths.
        if hasattr(hpdb._local, "conn") and hpdb._local.conn is not None:
            try:
                hpdb._local.conn.close()
            except Exception:
                pass
            hpdb._local.conn = None
            hpdb._local.path = None

    # ---- Conventional ----------------------------------------------------

    def test_conventional_hit_returns_alpha_tag_and_service(self):
        results = hpdb.lookup_hpdb(
            121500000,
            lat_dd=self.NASHVILLE_LAT,
            lon_dd=self.NASHVILLE_LON,
            db_path=self.db_path,
        )
        # BNA is the close one (~25 km); ATL is far (~340 km) and filtered out.
        self.assertEqual(1, len(results))
        r = results[0]
        self.assertEqual("BNA Emergency", r["alpha_tag"])
        self.assertEqual("conventional", r["source_table"])
        self.assertEqual("Aircraft", r["service_type"])
        self.assertEqual("Nashville International Airport — Aircraft", r["group_name"])
        self.assertIsNone(r["system_name"])
        self.assertLess(r["distance_km"], 50)

    def test_conventional_radius_filter_drops_far_sites(self):
        # With a 100 km radius, ATL (340 km away) should still be excluded.
        results = hpdb.lookup_hpdb(
            121500000,
            lat_dd=self.NASHVILLE_LAT,
            lon_dd=self.NASHVILLE_LON,
            radius_km=100,
            db_path=self.db_path,
        )
        self.assertEqual(1, len(results))
        self.assertEqual("BNA Emergency", results[0]["alpha_tag"])

    def test_conventional_radius_filter_admits_far_with_huge_radius(self):
        # 500 km radius → ATL (340 km) included.
        results = hpdb.lookup_hpdb(
            121500000,
            lat_dd=self.NASHVILLE_LAT,
            lon_dd=self.NASHVILLE_LON,
            radius_km=500,
            db_path=self.db_path,
            limit=5,
        )
        alpha_tags = sorted([r["alpha_tag"] for r in results])
        self.assertEqual(["ATL Emergency", "BNA Emergency"], alpha_tags)
        # BNA closer → first in distance-ascending sort
        self.assertEqual("BNA Emergency", results[0]["alpha_tag"])

    # ---- Trunked control channels ---------------------------------------

    def test_trunked_control_channel_returns_system_and_site(self):
        results = hpdb.lookup_hpdb(
            769456250,
            lat_dd=self.NASHVILLE_LAT,
            lon_dd=self.NASHVILLE_LON,
            db_path=self.db_path,
        )
        # Three rows in fixture: West Nashville (5 km), Utah (3000+ km, filtered),
        # and a NULL-coords site (filtered). Only West Nashville should land.
        self.assertEqual(1, len(results))
        r = results[0]
        self.assertEqual("trunk_control", r["source_table"])
        self.assertIn("Tennessee Advanced Communications Network", r["alpha_tag"])
        self.assertIn("West Nashville", r["alpha_tag"])
        self.assertEqual("Tennessee Advanced Communications Network (TACN)", r["system_name"])
        self.assertEqual("West Nashville", r["group_name"])
        self.assertEqual("P25X2_TDMA", r["service_type"])
        self.assertLess(r["distance_km"], 10)

    def test_trunked_null_coords_excluded(self):
        # The Mystery System (no lat/lon) should never be returned.
        results = hpdb.lookup_hpdb(
            769456250,
            lat_dd=self.NASHVILLE_LAT,
            lon_dd=self.NASHVILLE_LON,
            radius_km=20000,  # essentially worldwide
            db_path=self.db_path,
            limit=10,
        )
        sysnames = [r["system_name"] for r in results]
        self.assertNotIn("Mystery System", sysnames)

    # ---- Combined ranking + edge cases ----------------------------------

    def test_combined_results_sorted_by_distance(self):
        # Use a huge radius so both conventional + trunked match if they hit
        # the same freq. (In the fixture they don't share a freq, so this
        # just sanity-checks the sort path on each separately.)
        results = hpdb.lookup_hpdb(
            121500000,
            lat_dd=self.NASHVILLE_LAT,
            lon_dd=self.NASHVILLE_LON,
            radius_km=10000,
            db_path=self.db_path,
            limit=10,
        )
        distances = [r["distance_km"] for r in results]
        self.assertEqual(distances, sorted(distances))

    def test_no_match_returns_empty_list(self):
        # Random freq that isn't in the fixture.
        results = hpdb.lookup_hpdb(
            462562500,
            lat_dd=self.NASHVILLE_LAT,
            lon_dd=self.NASHVILLE_LON,
            db_path=self.db_path,
        )
        self.assertEqual([], results)

    def test_freq_zero_or_none_returns_empty_list(self):
        self.assertEqual([], hpdb.lookup_hpdb(0, db_path=self.db_path))
        self.assertEqual([], hpdb.lookup_hpdb(-1, db_path=self.db_path))
        self.assertEqual([], hpdb.lookup_hpdb(None, db_path=self.db_path))

    def test_missing_db_returns_empty_list(self):
        results = hpdb.lookup_hpdb(
            121500000,
            lat_dd=self.NASHVILLE_LAT,
            lon_dd=self.NASHVILLE_LON,
            db_path="/nonexistent/homepatrol.db",
        )
        self.assertEqual([], results)

    def test_env_var_overrides_default_db_path(self):
        with mock.patch.dict(os.environ, {"DISCO_HPDB_PATH": self.db_path}):
            results = hpdb.lookup_hpdb(
                121500000,
                lat_dd=self.NASHVILLE_LAT,
                lon_dd=self.NASHVILLE_LON,
                # No db_path argument → falls through to env var.
            )
        self.assertEqual(1, len(results))
        self.assertEqual("BNA Emergency", results[0]["alpha_tag"])

    def test_best_match_returns_top_or_none(self):
        m = hpdb.best_match(
            769456250,
            lat_dd=self.NASHVILLE_LAT,
            lon_dd=self.NASHVILLE_LON,
            db_path=self.db_path,
        )
        self.assertIsNotNone(m)
        self.assertEqual("trunk_control", m["source_table"])

        no_hit = hpdb.best_match(
            462562500,
            lat_dd=self.NASHVILLE_LAT,
            lon_dd=self.NASHVILLE_LON,
            db_path=self.db_path,
        )
        self.assertIsNone(no_hit)


class HpdbHaversineSmokeTest(unittest.TestCase):
    """Distance helper sanity check — Nashville ↔ Atlanta is ~340 km."""

    def test_nashville_to_atlanta_distance(self):
        d = hpdb.haversine_km(36.0662, -86.9639, 33.6407, -84.4277)
        self.assertGreater(d, 300)
        self.assertLess(d, 380)


if __name__ == "__main__":
    unittest.main()
