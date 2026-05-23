"""Tests for the PR #35 Owntracks Travel Mode adapter.

POST /api/hp/owntracks accepts the Owntracks iOS-app JSON payload, reverse-
geocodes lat/lon to the nearest US ZIP, and routes through the same
_apply_travel_push / _save_hp_state_with_sync / _log_travel_push helpers as
the existing /api/hp/location/push endpoint. Same tailnet-only security
posture, same travel_mode_enabled gate.

The base test fixture from test_travel_mode_push wires up a temp HPState,
patches HPState.load(), patches _save_hp_state_with_sync, and redirects
the push receipt log — all reused here so this suite only exercises the
new code paths.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from test_travel_mode_push import _BaseTravelModeTest  # noqa: E402
from ui import handlers  # noqa: E402
from ui import zip_lookup  # noqa: E402
from ui.hp_state import HPState  # noqa: E402


def _owntracks_location(**overrides) -> dict:
    base = {
        "_type": "location",
        "tid": "wp",
        "lat": 36.0662,
        "lon": -86.9639,    # Nashville → 37221
        "tst": 1716480000,
        "acc": 10,
        "alt": 200,
        "vel": 0,
        "cog": 0,
        "batt": 78,
    }
    base.update(overrides)
    return base


# ---- nearest_zip reverse-lookup ------------------------------------------


class NearestZipTests(unittest.TestCase):
    def setUp(self):
        zip_lookup.reset_reverse_index_cache()
        self.addCleanup(zip_lookup.reset_reverse_index_cache)

    def test_known_points_resolve(self):
        # Bundled US Census ZCTA index — these are real centroid neighbors.
        self.assertEqual("24354", zip_lookup.nearest_zip(36.81203, -81.57894))  # Marion VA
        self.assertEqual("37221", zip_lookup.nearest_zip(36.0662, -86.9639))    # Nashville TN

    def test_out_of_range_returns_empty(self):
        self.assertEqual("", zip_lookup.nearest_zip(200.0, -86.0))
        self.assertEqual("", zip_lookup.nearest_zip(36.0, -200.0))
        self.assertEqual("", zip_lookup.nearest_zip("x", None))

    def test_missing_index_returns_empty(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        os.unlink(tmp.name)  # ensure missing
        self.assertEqual("", zip_lookup.nearest_zip(36.0, -86.0, index_path=tmp.name))

    def test_malformed_index_returns_empty(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            f.write("{not json")
            bad = f.name
        try:
            self.assertEqual("", zip_lookup.nearest_zip(36.0, -86.0, index_path=bad))
        finally:
            os.unlink(bad)


# ---- /api/hp/owntracks endpoint ------------------------------------------


class _BaseOwntracksTest(_BaseTravelModeTest):
    """Reuse the travel-mode base fixture; reset Owntracks counters per test."""

    def setUp(self):
        super().setUp()
        # The counters are module-level; snapshot + restore.
        self._orig_stats = dict(handlers._OWNTRACKS_STATS)
        handlers._OWNTRACKS_STATS.update({
            "invocations_total": 0,
            "pushes_accepted_total": 0,
            "pushes_rejected_total": 0,
            "last_push_ts": 0.0,
            "last_lat": None,
            "last_lon": None,
            "last_battery_pct": None,
        })
        self.addCleanup(lambda: handlers._OWNTRACKS_STATS.update(self._orig_stats))

    def _owntracks(self, body):
        return self._post("/api/hp/owntracks", body)


class OwntracksLocationAcceptedTests(_BaseOwntracksTest):
    def test_location_push_mutates_hp_state(self):
        code, body, _ = self._owntracks(json.dumps(_owntracks_location()))
        self.assertEqual(200, code)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertEqual("37221", payload["zip"])
        self.assertAlmostEqual(36.0662, payload["lat"], places=4)
        # HPState file actually mutated.
        state = HPState.load()
        self.assertEqual("37221", state.zip)
        # Counters bumped.
        self.assertEqual(1, handlers._OWNTRACKS_STATS["invocations_total"])
        self.assertEqual(1, handlers._OWNTRACKS_STATS["pushes_accepted_total"])
        self.assertEqual(0, handlers._OWNTRACKS_STATS["pushes_rejected_total"])
        self.assertAlmostEqual(36.0662, handlers._OWNTRACKS_STATS["last_lat"], places=4)
        self.assertEqual(78, handlers._OWNTRACKS_STATS["last_battery_pct"])

    def test_receipt_log_records_owntracks_source(self):
        self._owntracks(json.dumps(_owntracks_location()))
        lines = self.log_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(1, len(lines))
        rec = json.loads(lines[0])
        self.assertTrue(rec["accepted"])
        self.assertEqual("owntracks", rec["source"])
        self.assertEqual("37221", rec["zip"])
        self.assertEqual("wp", rec.get("owntracks_tid"))
        self.assertEqual(78, rec.get("owntracks_battery_pct"))

    def test_marion_va_resolves_24354(self):
        code, body, _ = self._owntracks(json.dumps(_owntracks_location(
            lat=36.81203, lon=-81.57894)))
        self.assertEqual(200, code)
        self.assertEqual("24354", json.loads(body)["zip"])

    def test_no_battery_field_handled(self):
        # Owntracks payloads can omit batt; counters must not crash.
        payload = _owntracks_location()
        del payload["batt"]
        code, body, _ = self._owntracks(json.dumps(payload))
        self.assertEqual(200, code)
        self.assertIsNone(handlers._OWNTRACKS_STATS["last_battery_pct"])


class OwntracksNonLocationTypesTests(_BaseOwntracksTest):
    def test_lwt_ignored_with_200(self):
        code, body, _ = self._owntracks(json.dumps({"_type": "lwt", "tid": "wp"}))
        self.assertEqual(200, code)
        self.assertIn("ignored", body)
        self.assertEqual(1, handlers._OWNTRACKS_STATS["invocations_total"])
        self.assertEqual(0, handlers._OWNTRACKS_STATS["pushes_accepted_total"])

    def test_transition_ignored(self):
        code, _, _ = self._owntracks(json.dumps({
            "_type": "transition", "tid": "wp",
            "lat": 36.0, "lon": -86.0, "event": "enter", "desc": "home",
        }))
        self.assertEqual(200, code)
        self.assertEqual(0, handlers._OWNTRACKS_STATS["pushes_accepted_total"])
        # HPState NOT mutated.
        state = HPState.load()
        self.assertEqual("37221", state.zip)  # base-class default

    def test_waypoint_ignored(self):
        code, _, _ = self._owntracks(json.dumps({"_type": "waypoint", "tid": "wp"}))
        self.assertEqual(200, code)

    def test_no_type_field_ignored(self):
        # Defensive: malformed-with-no-_type is treated as "ignore", not 400.
        code, _, _ = self._owntracks(json.dumps({"lat": 36.0, "lon": -86.0}))
        self.assertEqual(200, code)
        self.assertEqual(0, handlers._OWNTRACKS_STATS["pushes_accepted_total"])


class OwntracksGatingTests(_BaseOwntracksTest):
    def setUp(self):
        super().setUp()
        # Flip travel mode off.
        state = HPState.load()
        state.travel_mode_enabled = False
        state.save(str(self.state_path))

    def test_disabled_returns_409(self):
        code, body, _ = self._owntracks(json.dumps(_owntracks_location()))
        self.assertEqual(409, code)
        self.assertEqual("travel_mode_disabled", json.loads(body)["reason"])
        self.assertEqual(1, handlers._OWNTRACKS_STATS["pushes_rejected_total"])
        self.assertEqual(0, handlers._OWNTRACKS_STATS["pushes_accepted_total"])

    def test_disabled_does_not_mutate_state(self):
        before = HPState.load().zip
        self._owntracks(json.dumps(_owntracks_location(lat=40.7128, lon=-74.0060)))
        self.assertEqual(before, HPState.load().zip)

    def test_disabled_logs_rejection_receipt(self):
        self._owntracks(json.dumps(_owntracks_location()))
        rec = json.loads(self.log_path.read_text().strip().splitlines()[-1])
        self.assertFalse(rec["accepted"])
        self.assertEqual("travel_mode_disabled", rec.get("reason"))
        self.assertEqual("owntracks", rec.get("source"))


class OwntracksMalformedPayloadTests(_BaseOwntracksTest):
    def test_location_missing_lat_lon_returns_400(self):
        code, body, _ = self._owntracks(json.dumps({"_type": "location", "tid": "wp"}))
        self.assertEqual(400, code)
        self.assertIn("missing lat/lon", body)
        self.assertEqual(1, handlers._OWNTRACKS_STATS["pushes_rejected_total"])

    def test_location_non_numeric_lat_returns_400(self):
        code, body, _ = self._owntracks(json.dumps(
            {"_type": "location", "lat": "north", "lon": -86.0}))
        self.assertEqual(400, code)
        self.assertIn("invalid lat/lon", body)

    def test_location_out_of_range_lat_returns_400(self):
        code, body, _ = self._owntracks(json.dumps(
            {"_type": "location", "lat": 95.0, "lon": -86.0}))
        self.assertEqual(400, code)
        self.assertIn("out of range", body)

    def test_location_outside_us_coverage_returns_400(self):
        # Simulate the reverse index returning "" (e.g. Owntracks pushed from
        # outside the US ZCTA dataset).
        with mock.patch("ui.handlers._nearest_zip", return_value=""):
            code, body, _ = self._owntracks(json.dumps(_owntracks_location()))
        self.assertEqual(400, code)
        self.assertIn("nearest US ZIP", body)


if __name__ == "__main__":
    unittest.main()
