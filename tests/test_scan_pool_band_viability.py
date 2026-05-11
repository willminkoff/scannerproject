"""Tests for the per-protocol band-viability filter in the site picker.

Background: HomePatrol HP DB occasionally tags a site as belonging to a
P25 Phase 2 (``P25X2_TDMA``) trunk system while the site's control channels
are on VHF — a band that cannot carry P25 P2 in any real deployment. The
band filter drops such sites before the nearest-by-distance pick so that a
viable site at a greater distance wins.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ui import hp_scan_pool, scan_mode_controller
from ui.hp_state import HPState
from ui.protocol_bands import is_site_viable


class ProtocolBandsUnitTests(unittest.TestCase):
    def test_unknown_protocol_allows_anything(self):
        self.assertTrue(is_site_viable(None, [152.0]))
        self.assertTrue(is_site_viable("", [152.0]))
        self.assertTrue(is_site_viable("WeirdNewMode", [152.0]))

    def test_p25_phase2_blocks_vhf(self):
        self.assertFalse(is_site_viable("P25X2_TDMA", [152.0]))
        self.assertFalse(is_site_viable("P25X2_TDMA", [161.825, 152.095]))

    def test_p25_phase2_allows_700_800(self):
        self.assertTrue(is_site_viable("P25X2_TDMA", [770.181]))
        self.assertTrue(is_site_viable("P25X2_TDMA", [851.5]))

    def test_p25_phase2_mixed_freqs_pass_if_any_in_band(self):
        # Real fixture: West Point has [769.606, 769.619, 770.181, 770.531, 771.031].
        # Even if some hypothetical garbage entry creeps in, one good 700 freq wins.
        self.assertTrue(is_site_viable("P25X2_TDMA", [152.0, 770.181]))

    def test_p25_phase2_no_freqs_means_no_viability_claim(self):
        # We have a rule but no evidence — picker treats this as "not viable"
        # so a site with actual freqs takes priority.
        self.assertFalse(is_site_viable("P25X2_TDMA", []))

    def test_p25_standard_allows_all_bands(self):
        for f in (152.0, 460.0, 770.0, 856.0):
            self.assertTrue(is_site_viable("P25Standard", [f]))

    def test_motorola_allows_vhf(self):
        # VHF SmartNet is real (1008 entries in HP DB at time of writing).
        self.assertTrue(is_site_viable("Motorola", [152.0]))

    def test_env_var_disables_filter(self):
        with mock.patch.dict(os.environ, {"HP_DISABLE_BAND_FILTER": "1"}, clear=False):
            self.assertTrue(is_site_viable("P25X2_TDMA", [152.0]))

    def test_env_var_zero_or_empty_is_inactive(self):
        with mock.patch.dict(os.environ, {"HP_DISABLE_BAND_FILTER": "0"}, clear=False):
            self.assertFalse(is_site_viable("P25X2_TDMA", [152.0]))


_SCHEMA_SQL = """
CREATE TABLE trunk_systems (
    trunk_id INTEGER PRIMARY KEY, source_file TEXT, state_id INTEGER,
    system_name TEXT, system_type TEXT, protocol TEXT);
CREATE TABLE trunk_sites (
    site_id INTEGER PRIMARY KEY, source_file TEXT, trunk_id INTEGER,
    site_name TEXT, latitude REAL, longitude REAL, radius REAL);
CREATE TABLE trunk_freqs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source_file TEXT,
    site_id INTEGER, tfreq_id TEXT, freq_hz INTEGER, lcn TEXT);
CREATE TABLE trunk_groups (
    tgroup_id INTEGER PRIMARY KEY, source_file TEXT, trunk_id INTEGER,
    group_name TEXT, latitude REAL, longitude REAL, radius REAL, shape TEXT);
CREATE TABLE talkgroups (
    tid INTEGER PRIMARY KEY, source_file TEXT, tgroup_id INTEGER,
    alpha_tag TEXT, dec_tgid TEXT, mode TEXT, service_tag INTEGER);
CREATE TABLE conventional_groups (
    cgroup_id INTEGER PRIMARY KEY, source_file TEXT, parent_key TEXT,
    parent_id INTEGER, latitude REAL, longitude REAL, radius REAL);
CREATE TABLE conventional_freqs (
    cfreq_id INTEGER PRIMARY KEY, source_file TEXT, cgroup_id INTEGER,
    alpha_tag TEXT, freq_hz INTEGER, mode TEXT, tone TEXT, service_tag INTEGER);
CREATE TABLE entity_areas (
    id INTEGER PRIMARY KEY AUTOINCREMENT, entity_kind TEXT, entity_id INTEGER,
    record_type TEXT, state_id INTEGER, county_id INTEGER);
"""


def _build_lawrenceburg_db(db_path: Path) -> None:
    """Minimal HP DB modeled on the real Lawrenceburg TACN bug.

    Crestview (VHF, 3.6mi nearest) vs West Point (700 MHz, 15.7mi). Both
    are members of TACN, tagged P25X2_TDMA. With the filter, West Point
    must win. Without it (env-var bypass), Crestview wins.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA_SQL)
        conn.execute(
            "INSERT INTO trunk_systems(trunk_id, source_file, system_name, protocol) VALUES (?,?,?,?)",
            (24700, "TN.hpd", "Tennessee Advanced Communications Network (TACN)", "P25X2_TDMA"),
        )
        conn.executemany(
            "INSERT INTO trunk_sites(site_id, source_file, trunk_id, site_name, latitude, longitude, radius) VALUES (?,?,?,?,?,?,?)",
            [
                (24696, "TN.hpd", 24700, "Crestview",  35.297, -87.385, 25.0),
                (24701, "TN.hpd", 24700, "West Point", 35.025, -87.595, 25.0),
            ],
        )
        conn.executemany(
            "INSERT INTO trunk_freqs(source_file, site_id, tfreq_id, freq_hz, lcn) VALUES (?,?,?,?,?)",
            [
                ("TN.hpd", 24696, "1", 152_095_000, "1"),
                ("TN.hpd", 24696, "2", 152_775_000, "2"),
                ("TN.hpd", 24696, "3", 161_825_000, "3"),
                ("TN.hpd", 24701, "1", 769_606_000, "1"),
                ("TN.hpd", 24701, "2", 770_181_000, "2"),
                ("TN.hpd", 24701, "3", 770_531_000, "3"),
            ],
        )
        conn.execute(
            "INSERT INTO trunk_groups(tgroup_id, source_file, trunk_id, group_name, latitude, longitude, radius) VALUES (?,?,?,?,?,?,?)",
            (5001, "TN.hpd", 24700, "TACN Statewide", 35.20, -87.40, 60.0),
        )
        conn.execute(
            "INSERT INTO talkgroups(tid, source_file, tgroup_id, alpha_tag, dec_tgid, mode, service_tag) VALUES (?,?,?,?,?,?,?)",
            (90001, "TN.hpd", 5001, "TACN Dispatch", "1001", "T", 2),
        )
        conn.commit()
    finally:
        conn.close()


class LawrenceburgBugTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "homepatrol.db"
        _build_lawrenceburg_db(self.db_path)
        self.builder = hp_scan_pool.ScanPoolBuilder(str(self.db_path))

    def _pool(self) -> dict:
        # Lawrenceburg coordinates from the bug report.
        return self.builder.build_full_database_pool(
            lat=35.2507,
            lon=-87.3526,
            range_miles=60.0,
            service_tags=[2],
            include_nationwide=False,
            strict_location=False,
        )

    def test_west_point_picked_over_crestview_for_p25_phase2(self):
        pool = self._pool()
        sites = pool["trunked_sites"]
        self.assertEqual(1, len(sites), pool)
        self.assertEqual(24701, sites[0]["site_id"])
        self.assertEqual("West Point", sites[0]["site_name"])

    def test_env_var_bypass_restores_pure_distance_pick(self):
        with mock.patch.dict(os.environ, {"HP_DISABLE_BAND_FILTER": "1"}, clear=False):
            pool = self._pool()
        sites = pool["trunked_sites"]
        self.assertEqual(1, len(sites), pool)
        self.assertEqual(24696, sites[0]["site_id"], "with filter disabled, nearest (Crestview) must win")

    def test_pool_row_carries_protocol_field(self):
        pool = self._pool()
        self.assertEqual("P25X2_TDMA", pool["trunked_sites"][0].get("protocol"))


class FallbackWhenNoViableTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "homepatrol.db"
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(_SCHEMA_SQL)
            conn.execute(
                "INSERT INTO trunk_systems(trunk_id, source_file, system_name, protocol) VALUES (?,?,?,?)",
                (777, "X.hpd", "Allegedly P25 Phase 2", "P25X2_TDMA"),
            )
            conn.execute(
                "INSERT INTO trunk_sites(site_id, source_file, trunk_id, site_name, latitude, longitude, radius) VALUES (?,?,?,?,?,?,?)",
                (1, "X.hpd", 777, "Only Option (VHF)", 35.26, -87.36, 25.0),
            )
            conn.execute(
                "INSERT INTO trunk_freqs(source_file, site_id, tfreq_id, freq_hz, lcn) VALUES (?,?,?,?,?)",
                ("X.hpd", 1, "1", 154_000_000, "1"),
            )
            conn.execute(
                "INSERT INTO trunk_groups(tgroup_id, source_file, trunk_id, group_name, latitude, longitude, radius) VALUES (?,?,?,?,?,?,?)",
                (1, "X.hpd", 777, "Only Group", 35.26, -87.36, 25.0),
            )
            conn.execute(
                "INSERT INTO talkgroups(tid, source_file, tgroup_id, alpha_tag, dec_tgid, mode, service_tag) VALUES (?,?,?,?,?,?,?)",
                (1, "X.hpd", 1, "TG1", "100", "T", 2),
            )
            conn.commit()
        finally:
            conn.close()
        self.builder = hp_scan_pool.ScanPoolBuilder(str(self.db_path))

    def test_no_viable_site_keeps_nearest_and_logs_info(self):
        with self.assertLogs("ui.hp_scan_pool", level="INFO") as captured:
            pool = self.builder.build_full_database_pool(
                lat=35.2507,
                lon=-87.3526,
                range_miles=60.0,
                service_tags=[2],
                include_nationwide=False,
                strict_location=False,
            )
        sites = pool["trunked_sites"]
        self.assertEqual(1, len(sites))
        self.assertEqual(1, sites[0]["site_id"], "fallback keeps the only candidate")
        self.assertTrue(
            any("no band-viable site" in msg for msg in captured.output),
            captured.output,
        )


class DefensiveControllerFilterTests(unittest.TestCase):
    """Scan-mode controller must also enforce the filter on already-built pools."""

    def test_controller_drops_crestview_when_west_point_also_present(self):
        controller = scan_mode_controller.ScanModeController(db_path="/tmp/hpdb-test.db")
        state = HPState.default()
        state.mode = "full_database"
        state.use_location = True
        state.lat = 35.2507
        state.lon = -87.3526
        state.range_miles = 60.0
        state.enabled_service_tags = [2]
        base_pool = {
            "trunked_sites": [
                {
                    "system_id": 24700,
                    "site_id": 24696,
                    "site_name": "Crestview",
                    "distance_miles": 3.6,
                    "protocol": "P25X2_TDMA",
                    "control_channels": [152.095, 161.825],
                    "talkgroups": [1001],
                },
                {
                    "system_id": 24700,
                    "site_id": 24701,
                    "site_name": "West Point",
                    "distance_miles": 15.7,
                    "protocol": "P25X2_TDMA",
                    "control_channels": [770.181],
                    "talkgroups": [1001],
                },
            ],
            "conventional": [],
        }
        with mock.patch("ui.hp_state.HPState.load", return_value=state), mock.patch.object(
            controller, "_resolve_effective_service_tags", return_value=[2]
        ), mock.patch.object(
            controller._hp_builder,
            "build_full_database_pool",
            return_value=base_pool,
        ):
            filtered = controller.get_scan_pool()
        sites = filtered["trunked_sites"]
        self.assertEqual(1, len(sites), filtered)
        self.assertEqual(24701, sites[0]["site_id"])

    def test_controller_keeps_nearest_when_only_implausible_present(self):
        controller = scan_mode_controller.ScanModeController(db_path="/tmp/hpdb-test.db")
        state = HPState.default()
        state.mode = "full_database"
        state.use_location = True
        state.lat = 35.2507
        state.lon = -87.3526
        state.range_miles = 60.0
        state.enabled_service_tags = [2]
        base_pool = {
            "trunked_sites": [
                {
                    "system_id": 24700,
                    "site_id": 24696,
                    "site_name": "Crestview",
                    "distance_miles": 3.6,
                    "protocol": "P25X2_TDMA",
                    "control_channels": [152.095],
                    "talkgroups": [1001],
                },
                {
                    "system_id": 24700,
                    "site_id": 24699,
                    "site_name": "Other VHF",
                    "distance_miles": 8.0,
                    "protocol": "P25X2_TDMA",
                    "control_channels": [152.5],
                    "talkgroups": [1001],
                },
            ],
            "conventional": [],
        }
        with mock.patch("ui.hp_state.HPState.load", return_value=state), mock.patch.object(
            controller, "_resolve_effective_service_tags", return_value=[2]
        ), mock.patch.object(
            controller._hp_builder,
            "build_full_database_pool",
            return_value=base_pool,
        ), self.assertLogs("ui.scan_mode_controller", level="INFO") as captured:
            filtered = controller.get_scan_pool()
        sites = filtered["trunked_sites"]
        self.assertEqual(1, len(sites), filtered)
        self.assertEqual(24696, sites[0]["site_id"], "no viable → fallback to nearest implausible")
        self.assertTrue(
            any("no band-viable site" in msg for msg in captured.output),
            captured.output,
        )

    def test_controller_no_op_for_unknown_protocol(self):
        controller = scan_mode_controller.ScanModeController(db_path="/tmp/hpdb-test.db")
        state = HPState.default()
        state.mode = "full_database"
        state.use_location = True
        state.lat = 35.2507
        state.lon = -87.3526
        state.range_miles = 60.0
        state.enabled_service_tags = [2]
        # Two sites with no protocol info — should behave like pre-filter logic.
        base_pool = {
            "trunked_sites": [
                {
                    "system_id": 100,
                    "site_id": 10,
                    "distance_miles": 9.5,
                    "control_channels": [152.0],
                    "talkgroups": [1001],
                },
                {
                    "system_id": 100,
                    "site_id": 11,
                    "distance_miles": 2.2,
                    "control_channels": [770.0],
                    "talkgroups": [1001],
                },
            ],
            "conventional": [],
        }
        with mock.patch("ui.hp_state.HPState.load", return_value=state), mock.patch.object(
            controller, "_resolve_effective_service_tags", return_value=[2]
        ), mock.patch.object(
            controller._hp_builder,
            "build_full_database_pool",
            return_value=base_pool,
        ):
            filtered = controller.get_scan_pool()
        sites = filtered["trunked_sites"]
        self.assertEqual(1, len(sites))
        self.assertEqual(11, sites[0]["site_id"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    unittest.main()
