import json
import unittest

from ui import profile_editor


class ProfileEditorSiteAwareTests(unittest.TestCase):
    def test_parse_systems_json_text_accepts_site_aware_systems(self):
        systems, flattened, canonical = profile_editor._parse_systems_json_text(
            json.dumps(
                {
                    "systems": [
                        {
                            "name": "MTRTRS",
                            "system_id": "7078",
                            "sites": [
                                {
                                    "site_id": "18863",
                                    "site_name": "Davidson County Simulcast",
                                    "control_channels_hz": [856937500, 857437500],
                                    "latitude": 36.17,
                                    "longitude": -86.78,
                                    "radius": 20.0,
                                    "enabled": True,
                                },
                                {
                                    "site_id": "41154",
                                    "site_name": "Davidson County Services",
                                    "control_channels_hz": [855912500, 856937500],
                                    "enabled": True,
                                },
                            ],
                        }
                    ]
                }
            )
        )

        self.assertEqual(["855.9125", "856.9375", "857.4375"], flattened)
        self.assertEqual("MTRTRS", systems[0]["name"])
        self.assertEqual("7078", systems[0]["system_id"])
        self.assertEqual("18863", systems[0]["sites"][0]["site_id"])
        self.assertIn('"sites"', canonical)
        self.assertIn('"control_channels_hz"', canonical)


if __name__ == "__main__":
    unittest.main()
