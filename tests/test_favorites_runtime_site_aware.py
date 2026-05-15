import unittest
from unittest import mock

from ui import favorites_runtime


class FavoritesRuntimeSiteAwareTests(unittest.TestCase):
    def test_empty_analog_pool_preserves_managed_active_profile(self):
        profiles = [
            {"id": "none_airband", "path": "/tmp/none_airband.conf"},
            {"id": "hp3_favorites_airband", "path": "/tmp/hp3_favorites_airband.conf"},
        ]

        with mock.patch.object(
            favorites_runtime,
            "_current_profile_id_for_target",
            return_value="hp3_favorites_airband",
        ), mock.patch.object(
            favorites_runtime,
            "_select_fallback_profile",
            return_value="none_airband",
        ) as select_fallback:
            result = favorites_runtime._desired_analog_profile_for_empty_result(
                profiles,
                "airband",
                managed_profile_id="hp3_favorites_airband",
            )

        self.assertEqual("hp3_favorites_airband", result)
        select_fallback.assert_not_called()

    def test_normalize_digital_pool_emits_site_aware_systems_for_multiple_sites(self):
        pool = {
            "trunked_sites": [
                {
                    "system_id": 7078,
                    "system_name": "MTRTRS",
                    "site_id": 18863,
                    "site_name": "Davidson County Simulcast",
                    "latitude": 36.17,
                    "longitude": -86.78,
                    "radius": 20.0,
                    "department_name": "Vanderbilt University",
                    "control_channels": [856.9375, 857.4375],
                    "talkgroups": [3207],
                    "talkgroup_labels": {"3207": "Police Dispatch"},
                    "talkgroup_groups": {"3207": "Vanderbilt University"},
                },
                {
                    "system_id": 7078,
                    "system_name": "MTRTRS",
                    "site_id": 41154,
                    "site_name": "Davidson County Services",
                    "latitude": 36.15,
                    "longitude": -86.81,
                    "radius": 20.0,
                    "department_name": "Vanderbilt University",
                    "control_channels": [855.9125, 856.9375],
                    "talkgroups": [3207],
                    "talkgroup_labels": {"3207": "Police Dispatch"},
                    "talkgroup_groups": {"3207": "Vanderbilt University"},
                },
            ],
            "conventional": [],
        }

        systems, talkgroups, controls_flat, summary = favorites_runtime._normalize_digital_pool(pool)

        self.assertEqual(1, len(systems))
        self.assertEqual("MTRTRS", systems[0]["name"])
        self.assertEqual("7078", systems[0]["system_id"])
        self.assertEqual(["41154", "18863"], [site["site_id"] for site in systems[0]["sites"]])
        self.assertEqual(
            [855912500, 856937500],
            systems[0]["sites"][0]["control_channels_hz"],
        )
        self.assertEqual(
            [856937500, 857437500],
            systems[0]["sites"][1]["control_channels_hz"],
        )
        self.assertEqual(["855.9125", "856.9375", "857.4375"], controls_flat)
        self.assertEqual(1, len(talkgroups))
        self.assertEqual({"systems": 1, "talkgroups": 1, "control_channels": 3}, summary)

    def test_normalize_digital_pool_orders_candidate_sites_by_radius(self):
        pool = {
            "trunked_sites": [
                {
                    "system_id": 7078,
                    "system_name": "MTRTRS",
                    "site_id": 1,
                    "site_name": "Alpha Small",
                    "radius": 5.0,
                    "distance_miles": 1.0,
                    "control_channels": [851.1],
                    "talkgroups": [3207],
                },
                {
                    "system_id": 7078,
                    "system_name": "MTRTRS",
                    "site_id": 2,
                    "site_name": "Zulu Wide",
                    "radius": 25.0,
                    "distance_miles": 10.0,
                    "control_channels": [852.2],
                    "talkgroups": [3207],
                },
            ],
            "conventional": [],
        }

        systems, _talkgroups, _controls_flat, _summary = favorites_runtime._normalize_digital_pool(pool)

        self.assertEqual(["2", "1"], [site["site_id"] for site in systems[0]["sites"]])

    def test_normalize_digital_pool_synthesizes_stable_site_id_when_missing(self):
        pool = {
            "trunked_sites": [
                {
                    "system_id": 6355,
                    "system_name": "TACN",
                    "department_name": "District 3",
                    "control_channels": [769.83125],
                    "talkgroups": [47008],
                    "talkgroup_labels": {"47008": "District 3: Dispatch 1"},
                    "talkgroup_groups": {"47008": "District 3"},
                }
            ],
            "conventional": [],
        }

        systems, _talkgroups, controls_flat, _summary = favorites_runtime._normalize_digital_pool(pool)

        self.assertEqual(1, len(systems))
        sites = systems[0]["sites"]
        self.assertEqual(1, len(sites))
        self.assertTrue(str(sites[0]["site_id"]).startswith("fav:"))
        self.assertEqual("District 3", sites[0]["site_name"])
        self.assertEqual([769831250], sites[0]["control_channels_hz"])
        self.assertEqual(["769.83125"], controls_flat)

    def test_normalize_digital_pool_orders_systems_by_primary_site_distance(self):
        # Three systems, primary sites at 22 / 4 / 11 miles. Expect closest-first
        # cross-system ordering; the alphabetical "Alpha" system is FARTHEST so
        # it must not float to the top.
        pool = {
            "trunked_sites": [
                {
                    "system_id": 1001,
                    "system_name": "Alpha",
                    "site_id": 11,
                    "site_name": "Alpha Primary",
                    "radius": 25.0,
                    "distance_miles": 22.0,
                    "control_channels": [851.1],
                    "talkgroups": [100],
                },
                {
                    "system_id": 1002,
                    "system_name": "Bravo",
                    "site_id": 22,
                    "site_name": "Bravo Primary",
                    "radius": 25.0,
                    "distance_miles": 4.0,
                    "control_channels": [852.2],
                    "talkgroups": [200],
                },
                {
                    "system_id": 1003,
                    "system_name": "Charlie",
                    "site_id": 33,
                    "site_name": "Charlie Primary",
                    "radius": 25.0,
                    "distance_miles": 11.0,
                    "control_channels": [853.3],
                    "talkgroups": [300],
                },
            ],
            "conventional": [],
        }

        systems, _talkgroups, _controls_flat, _summary = favorites_runtime._normalize_digital_pool(pool)

        self.assertEqual(["Bravo", "Charlie", "Alpha"], [s["name"] for s in systems])

    def test_normalize_digital_pool_uses_largest_radius_site_for_primary_distance(self):
        # Davidson Services-vs-Simulcast trap: two systems each with a tiny
        # close site and a large far site. The picker's primary is the
        # largest-radius site, so the SYSTEM-level distance must reflect THAT.
        # System "Far" should sort after "Near" using the simulcast distance
        # (20 vs 6), not the services distance (1 vs 2).
        pool = {
            "trunked_sites": [
                {
                    "system_id": 7001,
                    "system_name": "Near",
                    "site_id": 1,
                    "site_name": "Near Services",
                    "radius": 5.0,
                    "distance_miles": 1.0,
                    "control_channels": [851.1],
                    "talkgroups": [100],
                },
                {
                    "system_id": 7001,
                    "system_name": "Near",
                    "site_id": 2,
                    "site_name": "Near Simulcast",
                    "radius": 25.0,
                    "distance_miles": 6.0,
                    "control_channels": [852.2],
                    "talkgroups": [100],
                },
                {
                    "system_id": 7002,
                    "system_name": "Far",
                    "site_id": 3,
                    "site_name": "Far Services",
                    "radius": 5.0,
                    "distance_miles": 2.0,
                    "control_channels": [853.3],
                    "talkgroups": [200],
                },
                {
                    "system_id": 7002,
                    "system_name": "Far",
                    "site_id": 4,
                    "site_name": "Far Simulcast",
                    "radius": 25.0,
                    "distance_miles": 20.0,
                    "control_channels": [854.4],
                    "talkgroups": [200],
                },
            ],
            "conventional": [],
        }

        systems, _talkgroups, _controls_flat, _summary = favorites_runtime._normalize_digital_pool(pool)

        self.assertEqual(["Near", "Far"], [s["name"] for s in systems])
        self.assertEqual("Near Simulcast", systems[0]["sites"][0]["site_name"])
        self.assertEqual("Far Simulcast", systems[1]["sites"][0]["site_name"])

    def test_empty_digital_pool_does_not_persist_no_dongles_assignment(self):
        pool = {"trunked_sites": [], "conventional": []}

        with mock.patch.object(favorites_runtime, "allocate_dongles") as allocate_dongles:
            result = favorites_runtime.sync_scan_pool_to_digital_runtime(
                force=True,
                mode="hp",
                pool=pool,
            )

        allocate_dongles.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertEqual("no digital targets in active scan pool", result["reason"])
        self.assertEqual(0, result["system_count"])


if __name__ == "__main__":
    unittest.main()
