"""Tests for the iPhone-driven travel mode location push endpoint and toggle.

The push endpoint mutates HPState.zip/lat/lon only — it must never touch
use_location, strict_location, range_miles, favorites, or service tags. That
isolation is what keeps travel mode from regressing the failure mode the
sidecar "Use location" toggle had (commit 48c68ca): users flipping location
settings out from under their saved configuration.

The toggle endpoint is the user-facing on/off switch. Push is gated on
HPState.travel_mode_enabled; rejected pushes return 409 with reason
travel_mode_disabled. The toggle itself is a pure gate — it never mutates
zip/lat/lon (or anything besides the flag). Manual sidecar ZIP entry
remains the way to set baseline.

Note: the push endpoint is intentionally unauthenticated. It is safe only
because the UI listens on a tailnet-only interface (no Funnel, no public
proxy). If that ever changes, the auth layer at commit 61864b5 needs to
come back.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ui import handlers
from ui.hp_state import HPState


class _FakePostRequest:
    def __init__(self, path: str, body: str):
        self.path = path
        payload = body.encode("utf-8")
        self.headers = {
            "Content-Length": str(len(payload)),
            "Content-Type": "application/json",
        }
        self.rfile = io.BytesIO(payload)
        self.sent = []

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="ignore")
        self.sent.append((code, body, ctype))
        return code, body, ctype


class _BaseTravelModeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        self.state_path = Path(self._tmp.name) / "hp_state.json"
        self.log_path = Path(self._tmp.name) / "travel.jsonl"
        state_path = str(self.state_path)

        # Seed an HP state with non-default user-controlled fields so we can
        # prove the push / toggle leave them alone. Travel mode is ON by
        # default in this base class so subclasses inherit a working push
        # path; the gating-specific tests flip it off in their own setUp.
        seed = HPState.default()
        seed.zip = "37221"
        seed.lat = 36.12
        seed.lon = -86.67
        seed.use_location = True
        seed.strict_location = True
        seed.range_miles = 22.5
        seed.enabled_service_tags = [2, 7, 23]
        seed.travel_mode_enabled = True
        seed.save(state_path)

        # Route HPState.load() at the temp file regardless of CWD.
        original_load = HPState.load.__func__
        self._load_patcher = mock.patch.object(
            HPState,
            "load",
            classmethod(lambda cls, path=state_path, db_path=None: original_load(
                cls, path
            )),
        )
        self._load_patcher.start()
        self.addCleanup(self._load_patcher.stop)

        def _fake_save_with_sync(state):
            state.save(state_path)
            return {
                "ok": True,
                "state": state.to_dict(),
                "favorites_runtime_sync": {"ok": True, "changed": False, "errors": []},
            }

        self._sync_patcher = mock.patch.object(
            handlers, "_save_hp_state_with_sync", side_effect=_fake_save_with_sync
        )
        self._sync_patcher.start()
        self.addCleanup(self._sync_patcher.stop)

        self._log_patcher = mock.patch.object(
            handlers, "HP_LOCATION_PUSH_LOG_PATH", str(self.log_path)
        )
        self._log_patcher.start()
        self.addCleanup(self._log_patcher.stop)

    def _post(self, path, body):
        req = _FakePostRequest(path, body)
        return handlers.Handler.do_POST(req)

    def _push(self, body):
        return self._post("/api/hp/location/push", body)

    def _toggle(self, body):
        return self._post("/api/hp/travel_mode/toggle", body)


class TravelModePushTests(_BaseTravelModeTest):
    def test_malformed_zip_returns_400(self):
        for bad in ("", "abcde", "123", "1234567", "12345-1234"):
            with self.subTest(bad=bad):
                code, body, _ = self._push(json.dumps({"zip": bad}))
                self.assertEqual(400, code)
                self.assertIn("zip", body)

    def test_missing_zip_returns_400(self):
        code, body, _ = self._push(json.dumps({"lat": 40.0, "lon": -74.0}))
        self.assertEqual(400, code)
        self.assertIn("missing zip", body)

    def test_out_of_range_lat_lon_returns_400(self):
        cases = [
            {"zip": "10001", "lat": 91.0, "lon": -74.0},
            {"zip": "10001", "lat": -91.0, "lon": -74.0},
            {"zip": "10001", "lat": 40.0, "lon": 181.0},
            {"zip": "10001", "lat": 40.0, "lon": -181.0},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                code, body, _ = self._push(json.dumps(payload))
                self.assertEqual(400, code)
                self.assertIn("invalid", body)

    def test_lat_without_lon_returns_400(self):
        code, body, _ = self._push(json.dumps({"zip": "10001", "lat": 40.0}))
        self.assertEqual(400, code)
        self.assertIn("lat and lon", body)

    def test_successful_push_mutates_zip_and_persists(self):
        code, body, _ = self._push(
            json.dumps({"zip": "10001", "lat": 40.7128, "lon": -74.0060, "source": "ios_shortcut"})
        )
        self.assertEqual(200, code)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertEqual("10001", payload["zip"])
        self.assertAlmostEqual(40.7128, payload["lat"])
        self.assertAlmostEqual(-74.0060, payload["lon"])
        self.assertIn("updated_at", payload)

        reloaded = HPState.load(str(self.state_path))
        self.assertEqual("10001", reloaded.zip)
        self.assertAlmostEqual(40.7128, reloaded.lat)
        self.assertAlmostEqual(-74.0060, reloaded.lon)

    def test_push_does_not_modify_user_controlled_fields(self):
        before = HPState.load(str(self.state_path))
        code, _, _ = self._push(json.dumps({"zip": "10001", "lat": 40.7, "lon": -74.0}))
        self.assertEqual(200, code)

        after = HPState.load(str(self.state_path))
        self.assertEqual(before.use_location, after.use_location)
        self.assertEqual(before.strict_location, after.strict_location)
        self.assertAlmostEqual(before.range_miles, after.range_miles)
        self.assertEqual(before.enabled_service_tags, after.enabled_service_tags)
        self.assertEqual(before.mode, after.mode)
        self.assertEqual(before.favorites, after.favorites)
        # Travel mode flag itself is not flipped by a push.
        self.assertEqual(before.travel_mode_enabled, after.travel_mode_enabled)

    def test_zip_only_push_leaves_lat_lon_unchanged(self):
        before = HPState.load(str(self.state_path))
        code, _, _ = self._push(json.dumps({"zip": "10001"}))
        self.assertEqual(200, code)

        after = HPState.load(str(self.state_path))
        self.assertEqual("10001", after.zip)
        self.assertAlmostEqual(before.lat, after.lat)
        self.assertAlmostEqual(before.lon, after.lon)

    def test_push_writes_log_line(self):
        code, _, _ = self._push(
            json.dumps({"zip": "10001", "lat": 40.7, "lon": -74.0, "source": "ios_shortcut"})
        )
        self.assertEqual(200, code)

        self.assertTrue(self.log_path.is_file())
        lines = self.log_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(1, len(lines))
        record = json.loads(lines[0])
        self.assertEqual("10001", record["zip"])
        self.assertEqual("ios_shortcut", record["source"])
        self.assertAlmostEqual(40.7, record["lat"])
        self.assertAlmostEqual(-74.0, record["lon"])
        self.assertIn("ts", record)
        self.assertTrue(record["accepted"])

    def test_push_creates_missing_log_parent_directory(self):
        nested = Path(self._tmp.name) / "missing" / "nested" / "travel.jsonl"
        with mock.patch.object(handlers, "HP_LOCATION_PUSH_LOG_PATH", str(nested)):
            code, _, _ = self._push(
                json.dumps({"zip": "10001", "lat": 40.7, "lon": -74.0, "source": "manual_test"})
            )
        self.assertEqual(200, code)
        self.assertTrue(nested.is_file())
        record = json.loads(nested.read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual("10001", record["zip"])


class TravelModeGatingTests(_BaseTravelModeTest):
    """Pushes must be rejected when travel_mode_enabled is False."""

    def setUp(self):
        super().setUp()
        # Flip travel_mode off after the base seed.
        state = HPState.load()
        state.travel_mode_enabled = False
        state.save(str(self.state_path))

    def test_push_rejected_when_travel_mode_off(self):
        before = HPState.load()
        code, body, _ = self._push(
            json.dumps({"zip": "10001", "lat": 40.7, "lon": -74.0, "source": "ios_shortcut"})
        )
        self.assertEqual(409, code)
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertEqual("travel_mode_disabled", payload["reason"])

        # State unchanged.
        after = HPState.load()
        self.assertEqual(before.zip, after.zip)
        self.assertAlmostEqual(before.lat, after.lat)
        self.assertAlmostEqual(before.lon, after.lon)

    def test_rejected_push_logged_with_accepted_false(self):
        code, _, _ = self._push(
            json.dumps({"zip": "10001", "lat": 40.7, "lon": -74.0, "source": "ios_shortcut"})
        )
        self.assertEqual(409, code)
        self.assertTrue(self.log_path.is_file())
        record = json.loads(self.log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertFalse(record["accepted"])
        self.assertEqual("travel_mode_disabled", record["reason"])
        self.assertEqual("10001", record["zip"])
        self.assertEqual("ios_shortcut", record["source"])


class TravelModeToggleTests(_BaseTravelModeTest):
    def _assert_isolation_invariant(self, before, after):
        """Every field besides travel_mode_enabled must be untouched."""
        self.assertEqual(before.zip, after.zip)
        self.assertAlmostEqual(before.lat, after.lat)
        self.assertAlmostEqual(before.lon, after.lon)
        self.assertEqual(before.use_location, after.use_location)
        self.assertEqual(before.strict_location, after.strict_location)
        self.assertAlmostEqual(before.range_miles, after.range_miles)
        self.assertEqual(before.enabled_service_tags, after.enabled_service_tags)
        self.assertEqual(before.favorites, after.favorites)
        self.assertEqual(before.mode, after.mode)

    def test_toggle_off_to_on_flips_flag_only(self):
        state = HPState.load()
        state.travel_mode_enabled = False
        state.save(str(self.state_path))
        before = HPState.load()

        code, body, _ = self._toggle(json.dumps({"enabled": True}))
        self.assertEqual(200, code)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["travel_mode_enabled"])
        self.assertNotIn("snapped_to_home", payload)

        after = HPState.load()
        self.assertTrue(after.travel_mode_enabled)
        self._assert_isolation_invariant(before, after)

    def test_toggle_on_to_off_does_not_mutate_zip_lat_lon(self):
        # Seed travel-pushed state — non-home ZIP from the iPhone.
        state = HPState.load()
        state.zip = "10001"
        state.lat = 40.7128
        state.lon = -74.0060
        state.travel_mode_enabled = True
        state.save(str(self.state_path))
        before = HPState.load()

        code, body, _ = self._toggle(json.dumps({"enabled": False}))
        self.assertEqual(200, code)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["travel_mode_enabled"])
        self.assertNotIn("snapped_to_home", payload)
        # Response reflects the same zip/lat/lon that was already in state.
        self.assertEqual("10001", payload["zip"])
        self.assertAlmostEqual(40.7128, payload["lat"])
        self.assertAlmostEqual(-74.0060, payload["lon"])

        after = HPState.load()
        self.assertFalse(after.travel_mode_enabled)
        # ZIP/lat/lon untouched: the OFF toggle is a pure gate, not a reset.
        # Manual sidecar entry (or scripts/reset-home-zip.sh in an emergency)
        # is how baseline gets set.
        self._assert_isolation_invariant(before, after)

    def test_toggle_off_to_off_is_idempotent(self):
        state = HPState.load()
        state.travel_mode_enabled = False
        state.zip = "10001"
        state.lat = 40.0
        state.lon = -74.0
        state.save(str(self.state_path))
        before = HPState.load()

        code, body, _ = self._toggle(json.dumps({"enabled": False}))
        self.assertEqual(200, code)

        after = HPState.load()
        self.assertFalse(after.travel_mode_enabled)
        self._assert_isolation_invariant(before, after)

    def test_toggle_missing_enabled_returns_400(self):
        code, body, _ = self._toggle(json.dumps({}))
        self.assertEqual(400, code)
        self.assertIn("missing enabled", body)

    def test_toggle_non_boolean_enabled_returns_400(self):
        for bad in ("true", 1, 0, "yes", None):
            with self.subTest(bad=bad):
                code, body, _ = self._toggle(json.dumps({"enabled": bad}))
                self.assertEqual(400, code)
                self.assertIn("boolean", body)


class TravelModeStateExposureTests(_BaseTravelModeTest):
    """GET /api/hp/state must surface travel_mode_enabled and last-push."""

    class _FakeGetRequest:
        def __init__(self, path):
            self.path = path
            self.sent = []

        def _parse_optional_bool_query(self, qs, key):
            return handlers.Handler._parse_optional_bool_query(qs, key)

        def _send(self, code, body, ctype="text/html; charset=utf-8"):
            if isinstance(body, bytes):
                body = body.decode("utf-8", errors="ignore")
            self.sent.append((code, body, ctype))
            return code, body, ctype

    def _get_state(self):
        req = self._FakeGetRequest("/api/hp/state")
        return handlers.Handler.do_GET(req)

    def test_state_returns_travel_mode_enabled(self):
        # The seed in the base class set travel_mode_enabled=True.
        code, body, _ = self._get_state()
        self.assertEqual(200, code)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertIn("travel_mode_enabled", payload["state"])
        self.assertTrue(payload["state"]["travel_mode_enabled"])

    def test_state_returns_last_push_when_log_present(self):
        # Land a push so a receipt exists.
        push_code, _, _ = self._push(
            json.dumps({"zip": "10001", "lat": 40.7, "lon": -74.0, "source": "ios_shortcut"})
        )
        self.assertEqual(200, push_code)

        code, body, _ = self._get_state()
        self.assertEqual(200, code)
        payload = json.loads(body)
        last = payload.get("travel_mode_last_push")
        self.assertIsNotNone(last)
        self.assertEqual("10001", last["zip"])
        self.assertEqual("ios_shortcut", last["source"])
        self.assertTrue(last.get("accepted"))

    def test_state_returns_null_last_push_when_no_log(self):
        # No pushes happened in this test; HP_LOCATION_PUSH_LOG_PATH points
        # at a non-existent file.
        code, body, _ = self._get_state()
        self.assertEqual(200, code)
        payload = json.loads(body)
        self.assertIsNone(payload.get("travel_mode_last_push"))


class TravelModePushUnitTests(unittest.TestCase):
    def test_apply_travel_push_strips_zip(self):
        state = HPState.default()
        handlers._apply_travel_push(state, {"zip": " 10001 "})
        self.assertEqual("10001", state.zip)

    def test_default_state_has_travel_mode_disabled(self):
        state = HPState.default()
        self.assertFalse(state.travel_mode_enabled)

    def test_to_dict_round_trip_preserves_travel_mode_enabled(self):
        state = HPState.default()
        state.travel_mode_enabled = True
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "hp_state.json")
            state.save(path)
            reloaded = HPState.load(path)
        self.assertTrue(reloaded.travel_mode_enabled)


if __name__ == "__main__":
    unittest.main()
