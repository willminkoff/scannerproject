import os
import tempfile
import unittest
from unittest import mock

import combined_config
from ui import combined_status, v3_preflight


_PROFILE_TEMPLATE = """airband = {airband};

devices:
({{
  type = "rtlsdr";
  serial = "{serial}";
  index = {index};
  mode = "scan";
  gain = 22.900;

  channels:
  (
    {{
      freqs = ({freq});
      modulation = "{modulation}";
      bandwidth = 12000;
      squelch_threshold = -60;

      outputs:
      (
        {{
          type = "icecast";
          mountpoint = "GND.mp3";
          bitrate = 32;
        }}
      );
    }}
  );
}});
"""


class CombinedConfigDeviceMappingTests(unittest.TestCase):
    def test_airband_uses_index_zero_and_ground_uses_index_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            airband_path = os.path.join(tmp, "airband.conf")
            ground_path = os.path.join(tmp, "ground.conf")
            with open(airband_path, "w", encoding="utf-8") as handle:
                handle.write(
                    _PROFILE_TEMPLATE.format(
                        airband="true",
                        serial="orig-air",
                        index=7,
                        freq="118.6000",
                        modulation="am",
                    )
                )
            with open(ground_path, "w", encoding="utf-8") as handle:
                handle.write(
                    _PROFILE_TEMPLATE.format(
                        airband="false",
                        serial="orig-ground",
                        index=3,
                        freq="162.5500",
                        modulation="nfm",
                    )
                )

            with mock.patch.object(
                combined_config,
                "AIRBAND_DEVICE_SERIAL",
                "air-serial",
            ), mock.patch.object(
                combined_config,
                "GROUND_DEVICE_SERIAL",
                "ground-serial",
            ):
                rendered = combined_config.build_combined_config(
                    airband_path=airband_path,
                    ground_path=ground_path,
                    mixer_name="combined",
                )

        self.assertIn('serial = "air-serial";', rendered)
        self.assertIn('serial = "ground-serial";', rendered)
        self.assertIn("index = 0;", rendered)
        self.assertIn("index = 1;", rendered)
        self.assertLess(rendered.find('serial = "air-serial";'), rendered.find('serial = "ground-serial";'))

    def test_airband_only_profile_keeps_airband_on_index_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            airband_path = os.path.join(tmp, "airband.conf")
            ground_path = os.path.join(tmp, "ground.conf")
            with open(airband_path, "w", encoding="utf-8") as handle:
                handle.write(
                    _PROFILE_TEMPLATE.format(
                        airband="true",
                        serial="orig-air",
                        index=9,
                        freq="118.6000",
                        modulation="am",
                    )
                )
            with open(ground_path, "w", encoding="utf-8") as handle:
                handle.write("airband = false;\nui_disabled = true;\n")

            with mock.patch.object(
                combined_config,
                "AIRBAND_DEVICE_SERIAL",
                "air-serial",
            ):
                rendered = combined_config.build_combined_config(
                    airband_path=airband_path,
                    ground_path=ground_path,
                    mixer_name="combined",
                )

        self.assertIn('serial = "air-serial";', rendered)
        self.assertIn("index = 0;", rendered)
        self.assertNotIn("index = 1;", rendered)

    def test_combined_status_reports_index_mismatch_detail(self):
        combined_text = """devices:
({
  type = "rtlsdr";
  serial = "air-serial";
  index = 1;
  channels:
  (
    {
      freqs = (118.6000);
    }
  );
},
{
  type = "rtlsdr";
  serial = "ground-serial";
  index = 0;
  channels:
  (
    {
      freqs = (162.5500);
    }
  );
});
"""
        with tempfile.TemporaryDirectory() as tmp:
            combined_path = os.path.join(tmp, "combined.conf")
            with open(combined_path, "w", encoding="utf-8") as handle:
                handle.write(combined_text)
            with mock.patch.object(combined_status, "AIRBAND_RTL_SERIAL", "air-serial"), mock.patch.object(
                combined_status, "GROUND_RTL_SERIAL", "ground-serial"
            ):
                summary = combined_status.combined_device_summary(combined_path)

        self.assertEqual(summary["expected_indices"], {"airband": 0, "ground": 1})
        self.assertEqual(
            summary["index_mismatch_detail"],
            [
                {"device": "airband", "expected": 0, "actual": 1, "reason": "airband index mismatch"},
                {"device": "ground", "expected": 1, "actual": 0, "reason": "ground index mismatch"},
            ],
        )

    def test_analog_preflight_blocks_combined_index_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            airband_path = os.path.join(tmp, "airband.conf")
            ground_path = os.path.join(tmp, "ground.conf")
            with open(airband_path, "w", encoding="utf-8") as handle:
                handle.write("airband = true;\n")
            with open(ground_path, "w", encoding="utf-8") as handle:
                handle.write("airband = false;\n")

            with mock.patch.object(v3_preflight, "V3_STRICT_PREFLIGHT", True), mock.patch.object(
                v3_preflight, "load_compiled_state", return_value={}
            ), mock.patch.object(
                v3_preflight,
                "read_rtl_dongle_health",
                return_value={"status": "healthy", "missing_expected_serials": [], "slow_expected_serials": []},
            ), mock.patch.object(
                v3_preflight, "read_active_config_path", return_value=airband_path
            ), mock.patch.object(
                v3_preflight, "GROUND_CONFIG_PATH", ground_path
            ), mock.patch.object(
                v3_preflight,
                "combined_device_summary",
                return_value={
                    "airband": {"index": 1},
                    "ground": {"index": 0},
                    "expected_indices": {"airband": 0, "ground": 1},
                },
            ):
                gate = v3_preflight.evaluate_analog_preflight("airband", strict=True)

        self.assertEqual(gate["state"], "failed")
        self.assertTrue(gate["would_block"])
        self.assertIn("ANALOG_COMBINED_INDEX_MISMATCH", {reason["code"] for reason in gate["reasons"]})


if __name__ == "__main__":
    unittest.main()
