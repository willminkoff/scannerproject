import os
import tempfile
import time
import unittest
from unittest import mock

from ui import scanner


_PROFILE_TEMPLATE = """airband = {airband};

devices:
({{
  type = "rtlsdr";
  channels:
  (
    {{
      freqs = ({freqs});
      labels = ({labels});
    }}
  );
}});
"""


class ScannerHitPathTests(unittest.TestCase):
    def setUp(self):
        scanner._reset_live_analog_hit_state()

    def tearDown(self):
        scanner._reset_live_analog_hit_state()

    def _write_profile(self, path: str, *, airband: bool, freqs: list[float], labels: list[str]) -> None:
        freqs_text = ", ".join(f"{freq:.4f}" for freq in freqs)
        labels_text = ", ".join(f'"{label}"' for label in labels)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                _PROFILE_TEMPLATE.format(
                    airband="true" if airband else "false",
                    freqs=freqs_text,
                    labels=labels_text,
                )
            )

    def _write_last_hit(self, path: str, value: str, when_ts: float) -> None:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
        ns = int(when_ts * 1_000_000_000)
        os.utime(path, ns=(ns, ns))

    def _write_stats(self, path: str, counters: dict[str, int], when_ts: float) -> None:
        with open(path, "w", encoding="utf-8") as handle:
            for freq, count in counters.items():
                handle.write(
                    f'channel_activity_counter{{freq="{freq}",label="Test {freq}"}}\t{int(count)}\n'
                )
        ns = int(when_ts * 1_000_000_000)
        os.utime(path, ns=(ns, ns))

    def test_live_hit_tracker_only_keeps_active_profile_frequencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            air_conf = os.path.join(tmp, "air.conf")
            ground_conf = os.path.join(tmp, "ground.conf")
            air_last = os.path.join(tmp, "air_last.txt")
            ground_last = os.path.join(tmp, "ground_last.txt")
            self._write_profile(air_conf, airband=True, freqs=[118.6], labels=["Tower"])
            self._write_profile(ground_conf, airband=False, freqs=[162.55], labels=["WX"])

            base = time.time()
            with mock.patch.object(scanner, "read_active_config_path", return_value=air_conf), mock.patch.object(
                scanner, "GROUND_CONFIG_PATH", ground_conf
            ), mock.patch.object(
                scanner, "LAST_HIT_AIRBAND_PATH", air_last
            ), mock.patch.object(
                scanner, "LAST_HIT_GROUND_PATH", ground_last
            ), mock.patch.object(
                scanner, "read_hit_list_for_unit", return_value=[]
            ):
                self._write_last_hit(air_last, "119.1000", base - 1.0)
                scanner.refresh_analog_hit_state(now=base - 0.5)
                self.assertEqual(scanner.read_hit_list(limit=5), [])

                self._write_last_hit(air_last, "118.6000", base - 0.2)
                scanner.refresh_analog_hit_state(now=base - 0.1)
                items = scanner.read_hit_list(limit=5)
                last_hit_airband = scanner.read_last_hit_airband()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["freq"], "118.6000")
        self.assertEqual(items[0]["source"], "airband")
        self.assertEqual(last_hit_airband, "118.6000")

    def test_mark_analog_hit_cutoff_clears_stale_live_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            air_conf = os.path.join(tmp, "air.conf")
            ground_conf = os.path.join(tmp, "ground.conf")
            air_last = os.path.join(tmp, "air_last.txt")
            ground_last = os.path.join(tmp, "ground_last.txt")
            self._write_profile(air_conf, airband=True, freqs=[118.6], labels=["Tower"])
            self._write_profile(ground_conf, airband=False, freqs=[162.55], labels=["WX"])

            base = time.time()
            with mock.patch.object(scanner, "read_active_config_path", return_value=air_conf), mock.patch.object(
                scanner, "GROUND_CONFIG_PATH", ground_conf
            ), mock.patch.object(
                scanner, "LAST_HIT_AIRBAND_PATH", air_last
            ), mock.patch.object(
                scanner, "LAST_HIT_GROUND_PATH", ground_last
            ), mock.patch.object(
                scanner, "read_hit_list_for_unit", return_value=[]
            ):
                self._write_last_hit(air_last, "118.6000", base - 0.2)
                scanner.refresh_analog_hit_state(now=base - 0.1)
                self.assertTrue(scanner.read_hit_list(limit=5))

                scanner.mark_analog_hit_cutoff("airband", at_ts=base + 0.1)
                scanner.refresh_analog_hit_state(now=base + 0.2)
                items = scanner.read_hit_list(limit=5)

        self.assertEqual(items, [])

    def test_stats_tracker_only_keeps_active_profile_frequencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            air_conf = os.path.join(tmp, "air.conf")
            ground_conf = os.path.join(tmp, "ground.conf")
            stats_path = os.path.join(tmp, "stats.txt")
            self._write_profile(air_conf, airband=True, freqs=[118.4], labels=["Approach"])
            self._write_profile(ground_conf, airband=False, freqs=[162.55], labels=["WX"])

            base = time.time()
            with mock.patch.object(scanner, "read_active_config_path", return_value=air_conf), mock.patch.object(
                scanner, "GROUND_CONFIG_PATH", ground_conf
            ), mock.patch.object(
                scanner, "LAST_HIT_AIRBAND_PATH", os.path.join(tmp, "missing-air.txt")
            ), mock.patch.object(
                scanner, "LAST_HIT_GROUND_PATH", os.path.join(tmp, "missing-ground.txt")
            ), mock.patch.object(
                scanner, "ANALOG_AUTO_SQUELCH_STATS_PATH", stats_path
            ), mock.patch.object(
                scanner, "read_hit_list_for_unit", return_value=[]
            ):
                self._write_stats(stats_path, {"118.4000": 0, "119.1000": 0}, base - 1.0)
                scanner.refresh_analog_hit_state(now=base - 0.8)
                self.assertEqual(scanner.read_hit_list(limit=5), [])

                self._write_stats(stats_path, {"118.4000": 120, "119.1000": 25}, base - 0.2)
                scanner.refresh_analog_hit_state(now=base - 0.1)
                items = scanner.read_hit_list(limit=5)
                last_hit_airband = scanner.read_last_hit_airband()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["freq"], "118.4000")
        self.assertEqual(items[0]["source"], "airband")
        self.assertEqual(last_hit_airband, "118.4000")

    def test_stats_tracker_keeps_same_frequency_live_across_stats_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            air_conf = os.path.join(tmp, "air.conf")
            ground_conf = os.path.join(tmp, "ground.conf")
            stats_path = os.path.join(tmp, "stats.txt")
            self._write_profile(air_conf, airband=True, freqs=[118.4], labels=["Approach"])
            self._write_profile(ground_conf, airband=False, freqs=[162.55], labels=["WX"])

            base = time.time()
            with mock.patch.object(scanner, "read_active_config_path", return_value=air_conf), mock.patch.object(
                scanner, "GROUND_CONFIG_PATH", ground_conf
            ), mock.patch.object(
                scanner, "LAST_HIT_AIRBAND_PATH", os.path.join(tmp, "missing-air.txt")
            ), mock.patch.object(
                scanner, "LAST_HIT_GROUND_PATH", os.path.join(tmp, "missing-ground.txt")
            ), mock.patch.object(
                scanner, "ANALOG_AUTO_SQUELCH_STATS_PATH", stats_path
            ), mock.patch.object(
                scanner, "read_hit_list_for_unit", return_value=[]
            ):
                self._write_stats(stats_path, {"118.4000": 0}, base - 40.0)
                scanner.refresh_analog_hit_state(now=base - 39.5)

                self._write_stats(stats_path, {"118.4000": 120}, base - 18.0)
                scanner.refresh_analog_hit_state(now=base - 17.5)
                self.assertEqual(len(scanner.read_hit_list(limit=5)), 1)

                self._write_stats(stats_path, {"118.4000": 240}, base - 4.0)
                scanner.refresh_analog_hit_state(now=base - 3.5)
                items = scanner.read_hit_list(limit=5)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["freq"], "118.4000")
        self.assertGreaterEqual(int(items[0]["duration"]), 13)

    def test_scan_health_flags_monopolized_frequency(self):
        with tempfile.TemporaryDirectory() as tmp:
            air_conf = os.path.join(tmp, "air.conf")
            ground_conf = os.path.join(tmp, "ground.conf")
            stats_path = os.path.join(tmp, "stats.txt")
            self._write_profile(
                air_conf,
                airband=True,
                freqs=[118.4, 118.6, 124.75],
                labels=["East", "Tower", "Final"],
            )
            self._write_profile(ground_conf, airband=False, freqs=[162.55], labels=["WX"])

            base = time.time()
            with mock.patch.object(scanner, "read_active_config_path", return_value=air_conf), mock.patch.object(
                scanner, "GROUND_CONFIG_PATH", ground_conf
            ), mock.patch.object(
                scanner, "LAST_HIT_AIRBAND_PATH", os.path.join(tmp, "missing-air.txt")
            ), mock.patch.object(
                scanner, "LAST_HIT_GROUND_PATH", os.path.join(tmp, "missing-ground.txt")
            ), mock.patch.object(
                scanner, "ANALOG_AUTO_SQUELCH_STATS_PATH", stats_path
            ), mock.patch.object(
                scanner, "read_hit_list_for_unit", return_value=[]
            ):
                self._write_stats(
                    stats_path,
                    {"118.4000": 0, "118.6000": 0, "124.7500": 0},
                    base - 20.0,
                )
                scanner.refresh_analog_hit_state(now=base - 19.5)

                self._write_stats(
                    stats_path,
                    {"118.4000": 120, "118.6000": 0, "124.7500": 0},
                    base - 10.0,
                )
                scanner.refresh_analog_hit_state(now=base - 9.5)

                self._write_stats(
                    stats_path,
                    {"118.4000": 240, "118.6000": 0, "124.7500": 0},
                    base - 1.0,
                )
                health = scanner.get_analog_scan_health(now=base)

        self.assertTrue(health["airband"]["monopolized"])
        self.assertEqual("118.4000", health["airband"]["dominant_frequency"])
        self.assertEqual(3, health["airband"]["profile_frequency_count"])
        self.assertEqual(240, health["airband"]["activity_by_frequency"][0]["activity"])


if __name__ == "__main__":
    unittest.main()
