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

    def test_over_subscription_audit_log_includes_primary_site_distance(self):
        """When dropping the tail, log a single INFO line that names the dropped
        systems with primary-site + distance so operators can grep for surprises."""
        import logging
        systems = [
            {
                "name": "TACN",
                "control_channels_mhz": ["769.83125"],
                "sites": [{"site_name": "District 3", "distance_miles": 3.2}],
            },
            {
                "name": "MTRTRS",
                "control_channels_mhz": ["856.9375"],
                "sites": [{"site_name": "Simulcast", "distance_miles": 6.0}],
            },
            {
                "name": "FarAway",
                "control_channels_mhz": ["851.5"],
                "sites": [{"site_name": "Edge Tower", "distance_miles": 28.7}],
            },
        ]
        with self.assertLogs("ui.dongle_allocator", level="INFO") as cm:
            allocate(
                ["00000001", "14306619"],
                systems,
                persist=False,
            )
        joined = "\n".join(cm.output)
        self.assertIn("Dongle over-subscription: 3 systems > 2 dongles", joined)
        self.assertIn("TACN@District 3(3.2mi)", joined)
        self.assertIn("FarAway@Edge Tower(28.7mi)", joined)
        # Dropped list must contain the farthest; kept list must contain the closest.
        # Split on "Kept" / "Dropped" markers for ordering check.
        self.assertIn("Kept 1 (closest): TACN@District 3(3.2mi)", joined)
        self.assertIn("Dropped 2:", joined)

    def test_over_subscription_audit_log_handles_missing_site_metadata(self):
        """Systems without site data still produce a usable log line."""
        import logging
        with self.assertLogs("ui.dongle_allocator", level="INFO") as cm:
            allocate(
                ["00000001", "14306619"],
                [
                    _sys("A", "851.0"),
                    _sys("B", "852.0"),
                    _sys("C", "853.0"),
                ],
                persist=False,
            )
        joined = "\n".join(cm.output)
        self.assertIn("Dongle over-subscription:", joined)
        self.assertIn("(?mi)", joined)

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


class TestPriorityPool(unittest.TestCase):
    """Priority tier (e.g. RSPduo) gets control assignment ahead of regulars."""

    def test_priority_serials_ignored_when_none(self):
        """priority_serials=None preserves legacy behavior."""
        result = allocate(
            ["00000001", "14306619"],
            [_sys("A", "851.0")],
            persist=False,
        )
        # First sorted regular gets control; identical to before.
        self.assertEqual(result["assignments"][0]["preferred_tuner_serial"], "00000001")
        self.assertEqual(result["traffic_pool"], ["14306619"])
        self.assertEqual(result["priority_serials"], [])

    def test_priority_assigned_first_for_control(self):
        """RSPduo (priority) gets control even when its lex-sort key beats the RTLs'."""
        # Lexicographically "RSPduo Tuner 1..." > "70613472" > "80000003",
        # so without priority handling the RTLs would win. Priority must override.
        result = allocate(
            ["70613472", "80000003"],
            [_sys("MTRTRS", "851.0")],
            priority_serials=["RSPduo Tuner 1 SER#180903EF32"],
            persist=False,
        )
        self.assertEqual(result["strategy"], "single_system")
        self.assertEqual(
            result["assignments"][0]["preferred_tuner_serial"],
            "RSPduo Tuner 1 SER#180903EF32",
        )
        # The RTLs land in the traffic pool.
        self.assertEqual(sorted(result["traffic_pool"]), ["70613472", "80000003"])
        self.assertEqual(
            result["priority_serials"], ["RSPduo Tuner 1 SER#180903EF32"]
        )

    def test_priority_pair_assigned_to_two_systems(self):
        """Two RSPduo tuners take control on two systems, RTLs go to traffic."""
        result = allocate(
            ["70613472", "80000003"],
            [_sys("MTRTRS", "851.0"), _sys("davidson-services", "856.0")],
            priority_serials=[
                "RSPduo Tuner 1 SER#180903EF32",
                "RSPduo Tuner 2 SER#180903EF32",
            ],
            persist=False,
        )
        self.assertEqual(result["strategy"], "dedicated_control")
        # Both RSPduo tuners on control, in lex order (Tuner 1 then Tuner 2).
        self.assertEqual(
            result["assignments"][0]["preferred_tuner_serial"],
            "RSPduo Tuner 1 SER#180903EF32",
        )
        self.assertEqual(
            result["assignments"][1]["preferred_tuner_serial"],
            "RSPduo Tuner 2 SER#180903EF32",
        )
        self.assertEqual(sorted(result["traffic_pool"]), ["70613472", "80000003"])

    def test_priority_dedup_against_regular(self):
        """An identifier listed in both priority and regular is treated as priority."""
        result = allocate(
            ["RSPduo Tuner 1 SER#180903EF32", "70613472"],
            [_sys("MTRTRS", "851.0")],
            priority_serials=["RSPduo Tuner 1 SER#180903EF32"],
            persist=False,
        )
        # Pool size is 2 (deduped), with RSPduo as priority.
        self.assertEqual(len(result["digital_serials"]), 2)
        self.assertEqual(
            result["digital_serials"][0],
            "RSPduo Tuner 1 SER#180903EF32",
        )
        self.assertEqual(
            result["assignments"][0]["preferred_tuner_serial"],
            "RSPduo Tuner 1 SER#180903EF32",
        )
        self.assertEqual(result["traffic_pool"], ["70613472"])

    def test_three_systems_two_priority_one_regular(self):
        """3 systems, 2 RSPduo + 1 RTL -> all_control with RSPduo first, RTL last."""
        result = allocate(
            ["70613472"],
            [
                _sys("MTRTRS", "851.0"),
                _sys("davidson-services", "856.0"),
                _sys("Vanderbilt", "769.0"),
            ],
            priority_serials=[
                "RSPduo Tuner 1 SER#180903EF32",
                "RSPduo Tuner 2 SER#180903EF32",
            ],
            persist=False,
        )
        self.assertEqual(result["strategy"], "all_control")
        self.assertEqual(
            result["assignments"][0]["preferred_tuner_serial"],
            "RSPduo Tuner 1 SER#180903EF32",
        )
        self.assertEqual(
            result["assignments"][1]["preferred_tuner_serial"],
            "RSPduo Tuner 2 SER#180903EF32",
        )
        # Lone RTL takes the third system's control.
        self.assertEqual(
            result["assignments"][2]["preferred_tuner_serial"], "70613472"
        )
        self.assertEqual(result["traffic_pool"], [])

    def test_priority_only_pool(self):
        """No regulars, just RSPduo tuners."""
        result = allocate(
            [],
            [_sys("MTRTRS", "851.0")],
            priority_serials=[
                "RSPduo Tuner 1 SER#180903EF32",
                "RSPduo Tuner 2 SER#180903EF32",
            ],
            persist=False,
        )
        self.assertEqual(result["strategy"], "single_system")
        self.assertEqual(
            result["assignments"][0]["preferred_tuner_serial"],
            "RSPduo Tuner 1 SER#180903EF32",
        )
        self.assertEqual(
            result["traffic_pool"], ["RSPduo Tuner 2 SER#180903EF32"]
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
