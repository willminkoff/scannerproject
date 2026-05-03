import unittest
from unittest import mock

from ui import favorites_runtime


class FavoritesRuntimeSiteAwareTests(unittest.TestCase):
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
