"""Tests for ui.dongle_allocator."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

# Allow import from repo root.
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ui.dongle_allocator import allocate, load_assignments, preferred_tuner_for_system, traffic_pool_serials, invalidate_cache


def _sys(name: str, *channels: str) -> dict:
    return {"name": name, "control_channels_mhz": list(channels)}


class TestAllocate(unittest.TestCase):
    """Core allocation logic."""

    def test_no_dongles(self):
        result = allocate([], [_sys("A", "851.0")], persist=False)
        self.assertEqual(result["strategy"], "no_dongles")
        self.assertEqual(result["assignments"], [])
        self.assertEqual(result["traffic_pool"], [])

    def test_no_systems(self):
        result = allocate(["111", "222", "333"], [], persist=False)
        self.assertEqual(result["strategy"], "no_systems")
        self.assertEqual(result["assignments"], [])
        self.assertEqual(sorted(result["traffic_pool"]), ["111", "222", "333"])

    def test_single_system_three_dongles(self):
        result = allocate(
            ["00000001", "14306619", "56919602"],
            [_sys("TACN", "769.83125")],
            persist=False,
        )
        self.assertEqual(result["strategy"], "single_system")
        self.assertEqual(len(result["assignments"]), 1)
        self.assertEqual(result["assignments"][0]["system_name"], "TACN")
        self.assertEqual(result["assignments"][0]["role"], "control")
        # First serial (sorted) gets control
        self.assertEqual(result["assignments"][0]["preferred_tuner_serial"], "00000001")
        # Remaining two are traffic
        self.assertEqual(sorted(result["traffic_pool"]), ["14306619", "56919602"])

    def test_two_systems_three_dongles(self):
        """The sweet-spot: 2 control + 1 traffic."""
        result = allocate(
            ["00000001", "14306619", "56919602"],
            [_sys("TACN", "769.83125"), _sys("Vanderbilt", "856.9375")],
            persist=False,
        )
        self.assertEqual(result["strategy"], "dedicated_control")
        self.assertEqual(len(result["assignments"]), 2)
        self.assertEqual(result["assignments"][0]["system_name"], "TACN")
        self.assertEqual(result["assignments"][0]["preferred_tuner_serial"], "00000001")
        self.assertEqual(result["assignments"][1]["system_name"], "Vanderbilt")
        self.assertEqual(result["assignments"][1]["preferred_tuner_serial"], "14306619")
        self.assertEqual(result["traffic_pool"], ["56919602"])

    def test_three_systems_three_dongles(self):
        """Exact match: all control, no dedicated traffic."""
        result = allocate(
            ["00000001", "14306619", "56919602"],
            [
                _sys("TACN", "769.83125"),
                _sys("Vanderbilt", "856.9375"),
                _sys("Metro", "851.0125"),
            ],
            persist=False,
        )
        self.assertEqual(result["strategy"], "all_control")
        self.assertEqual(len(result["assignments"]), 3)
        self.assertEqual(result["traffic_pool"], [])

    def test_four_systems_three_dongles(self):
        """More systems than dongles: top 2 get control, 1 traffic, 4th unmonitored."""
        result = allocate(
            ["00000001", "14306619", "56919602"],
            [
                _sys("TACN", "769.83125"),
                _sys("Vanderbilt", "856.9375"),
                _sys("Metro", "851.0125"),
                _sys("Airport", "851.5"),
            ],
            persist=False,
        )
        self.assertEqual(result["strategy"], "dedicated_control")
        self.assertEqual(len(result["assignments"]), 2)
        self.assertEqual(result["assignments"][0]["system_name"], "TACN")
        self.assertEqual(result["assignments"][1]["system_name"], "Vanderbilt")
        self.assertEqual(result["traffic_pool"], ["56919602"])

    def test_five_systems_four_dongles_monitor_four_controls(self):
        """With 4+ systems and 4 dongles, monitor exactly 4 systems on control."""
        result = allocate(
            ["00000001", "14306619", "56919602", "70613472"],
            [
                _sys("TACN", "769.83125"),
                _sys("Vanderbilt", "856.9375"),
                _sys("Metro", "851.0125"),
                _sys("Airport", "851.5"),
                _sys("Lebanon", "852.0"),
            ],
            persist=False,
        )
        self.assertEqual(result["strategy"], "all_control")
        self.assertEqual(len(result["assignments"]), 4)
        self.assertEqual(
            [item["system_name"] for item in result["assignments"]],
            ["TACN", "Vanderbilt", "Metro", "Airport"],
        )
        self.assertEqual(result["traffic_pool"], [])

    def test_five_systems_five_dongles_caps_controls_at_four(self):
        """Overflow above 4 systems stays unmonitored even if a fifth dongle exists."""
        result = allocate(
            ["00000001", "14306619", "56919602", "70613472", "83241970"],
            [
                _sys("TACN", "769.83125"),
                _sys("Vanderbilt", "856.9375"),
                _sys("Metro", "851.0125"),
                _sys("Airport", "851.5"),
                _sys("Lebanon", "852.0"),
            ],
            persist=False,
        )
        self.assertEqual(result["strategy"], "dedicated_control")
        self.assertEqual(len(result["assignments"]), 4)
        self.assertEqual(result["traffic_pool"], ["83241970"])

    def test_deduplicates_serials(self):
        result = allocate(
            ["56919602", "56919602", "00000001"],
            [_sys("TACN", "769.83125")],
            persist=False,
        )
        self.assertEqual(len(result["digital_serials"]), 2)

    def test_deterministic_ordering(self):
        """Same inputs in different order produce same assignments."""
        r1 = allocate(
            ["56919602", "00000001", "14306619"],
            [_sys("B", "856.0"), _sys("A", "769.0")],
            persist=False,
        )
        r2 = allocate(
            ["14306619", "56919602", "00000001"],
            [_sys("B", "856.0"), _sys("A", "769.0")],
            persist=False,
        )
        self.assertEqual(
            [a["preferred_tuner_serial"] for a in r1["assignments"]],
            [a["preferred_tuner_serial"] for a in r2["assignments"]],
        )

    def test_system_priority_order_preserved(self):
        """Systems are assigned in scan-pool priority order, not alphabetically."""
        result = allocate(
            ["00000001", "14306619", "56919602"],
            [_sys("Zebra", "800.0"), _sys("Alpha", "850.0")],
            persist=False,
        )
        self.assertEqual(result["assignments"][0]["system_name"], "Zebra")
        self.assertEqual(result["assignments"][1]["system_name"], "Alpha")

    def test_control_channels_passed_through(self):
        result = allocate(
            ["00000001"],
            [_sys("TACN", "769.83125", "770.25625")],
            persist=False,
        )
        self.assertEqual(
            result["assignments"][0]["control_channels_mhz"],
            ["769.83125", "770.25625"],
        )


class TestPersistAndLoad(unittest.TestCase):
    """Round-trip through disk."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._path = os.path.join(self._tmpdir, "assignments.json")
        # Monkey-patch the module path for test isolation.
        import ui.dongle_allocator as mod
        self._orig_path = mod.DONGLE_ASSIGNMENTS_PATH
        mod.DONGLE_ASSIGNMENTS_PATH = self._path
        invalidate_cache()

    def tearDown(self):
        import ui.dongle_allocator as mod
        mod.DONGLE_ASSIGNMENTS_PATH = self._orig_path
        invalidate_cache()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_persist_and_load(self):
        allocate(
            ["00000001", "56919602"],
            [_sys("TACN", "769.83125")],
            persist=True,
        )
        loaded = load_assignments()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["strategy"], "single_system")
        self.assertEqual(len(loaded["assignments"]), 1)
        self.assertEqual(loaded["assignments"][0]["preferred_tuner_serial"], "00000001")

    def test_preferred_tuner_for_system_lookup(self):
        allocate(
            ["00000001", "14306619", "56919602"],
            [_sys("TACN", "769.83125"), _sys("Vanderbilt", "856.9375")],
            persist=True,
        )
        self.assertEqual(preferred_tuner_for_system("TACN"), "00000001")
        self.assertEqual(preferred_tuner_for_system("Vanderbilt"), "14306619")
        self.assertEqual(preferred_tuner_for_system("tacn"), "00000001")  # case-insensitive
        self.assertEqual(preferred_tuner_for_system("Unknown"), "")

    def test_traffic_pool_serials(self):
        allocate(
            ["00000001", "14306619", "56919602"],
            [_sys("TACN", "769.83125"), _sys("Vanderbilt", "856.9375")],
            persist=True,
        )
        self.assertEqual(traffic_pool_serials(), ["56919602"])

    def test_load_returns_none_when_missing(self):
        self.assertIsNone(load_assignments())

    def test_cache_invalidation(self):
        allocate(["00000001"], [_sys("A", "800.0")], persist=True)
        first = load_assignments()
        self.assertEqual(first["strategy"], "single_system")

        # Write different data directly.
        with open(self._path, "w") as fh:
            json.dump({"strategy": "manual_override", "assignments": [], "traffic_pool": []}, fh)

        # Cached -- still returns old data.
        cached = load_assignments()
        # mtime may or may not differ depending on filesystem resolution;
        # just verify invalidate_cache forces re-read.
        invalidate_cache()
        refreshed = load_assignments()
        self.assertEqual(refreshed["strategy"], "manual_override")


if __name__ == "__main__":
    unittest.main()
