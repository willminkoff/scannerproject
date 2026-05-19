"""Tests for the iPhone-driven travel mode location push endpoint.

The endpoint mutates HPState.zip/lat/lon only — it must never touch
use_location, strict_location, range_miles, favorites, or service tags. That
isolation is what keeps travel mode from regressing the failure mode the
sidecar "Use location" toggle had (commit 48c68ca): users flipping location
settings out from under their saved configuration.
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


_TEST_SECRET = "test-secret-abcd1234"


class _FakePostRequest:
    def __init__(self, path: str, body: str, *, secret_header: str | None = _TEST_SECRET):
        self.path = path
        payload = body.encode("utf-8")
        self.headers = {
            "Content-Length": str(len(payload)),
            "Content-Type": "application/json",
        }
        if secret_header is not None:
            self.headers["X-Travel-Secret"] = secret_header
        self.rfile = io.BytesIO(payload)
        self.sent = []

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="ignore")
        self.sent.append((code, body, ctype))
        return code, body, ctype


class TravelModePushTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        self.state_path = Path(self._tmp.name) / "hp_state.json"
        self.log_path = Path(self._tmp.name) / "travel.jsonl"
        state_path = str(self.state_path)

        # Seed an HP state with non-default user-controlled fields so we can
        # prove the push leaves them alone.
        seed = HPState.default()
        seed.zip = "37221"
        seed.lat = 36.12
        seed.lon = -86.67
        seed.use_location = True
        seed.strict_location = True
        seed.range_miles = 22.5
        seed.enabled_service_tags = [2, 7, 23]
        seed.save(state_path)

        # Route HPState.load() (no-arg, as the handler calls it) at the temp file.
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

        # Replace _save_hp_state_with_sync to save to the temp file and skip
        # the favorites runtime sync fan-out.
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

        self._secret_patcher = mock.patch.object(
            handlers, "HP_LOCATION_PUSH_SECRET", _TEST_SECRET
        )
        self._secret_patcher.start()
        self.addCleanup(self._secret_patcher.stop)

        self._log_patcher = mock.patch.object(
            handlers, "HP_LOCATION_PUSH_LOG_PATH", str(self.log_path)
        )
        self._log_patcher.start()
        self.addCleanup(self._log_patcher.stop)

    def _post(self, body, *, secret_header=_TEST_SECRET):
        req = _FakePostRequest("/api/hp/location/push", body, secret_header=secret_header)
        return handlers.Handler.do_POST(req)

    def test_missing_secret_header_returns_401(self):
        code, body, _ = self._post(json.dumps({"zip": "10001"}), secret_header=None)
        self.assertEqual(401, code)
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertEqual("unauthorized", payload["error"])

    def test_wrong_secret_returns_401(self):
        code, body, _ = self._post(
            json.dumps({"zip": "10001"}), secret_header="not-the-secret"
        )
        self.assertEqual(401, code)
        self.assertIn("unauthorized", body)

    def test_endpoint_disabled_when_secret_unset(self):
        with mock.patch.object(handlers, "HP_LOCATION_PUSH_SECRET", ""):
            code, body, _ = self._post(json.dumps({"zip": "10001"}))
        self.assertEqual(404, code)
        payload = json.loads(body)
        self.assertFalse(payload["ok"])

    def test_malformed_zip_returns_400(self):
        for bad in ("", "abcde", "123", "1234567", "12345-1234"):
            with self.subTest(bad=bad):
                code, body, _ = self._post(json.dumps({"zip": bad}))
                self.assertEqual(400, code)
                self.assertIn("zip", body)

    def test_missing_zip_returns_400(self):
        code, body, _ = self._post(json.dumps({"lat": 40.0, "lon": -74.0}))
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
                code, body, _ = self._post(json.dumps(payload))
                self.assertEqual(400, code)
                self.assertIn("invalid", body)

    def test_lat_without_lon_returns_400(self):
        code, body, _ = self._post(json.dumps({"zip": "10001", "lat": 40.0}))
        self.assertEqual(400, code)
        self.assertIn("lat and lon", body)

    def test_successful_push_mutates_zip_and_persists(self):
        code, body, _ = self._post(
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
        code, _, _ = self._post(json.dumps({"zip": "10001", "lat": 40.7, "lon": -74.0}))
        self.assertEqual(200, code)

        after = HPState.load(str(self.state_path))
        self.assertEqual(before.use_location, after.use_location)
        self.assertEqual(before.strict_location, after.strict_location)
        self.assertAlmostEqual(before.range_miles, after.range_miles)
        self.assertEqual(before.enabled_service_tags, after.enabled_service_tags)
        self.assertEqual(before.mode, after.mode)
        self.assertEqual(before.favorites, after.favorites)

    def test_zip_only_push_leaves_lat_lon_unchanged(self):
        before = HPState.load(str(self.state_path))
        code, _, _ = self._post(json.dumps({"zip": "10001"}))
        self.assertEqual(200, code)

        after = HPState.load(str(self.state_path))
        self.assertEqual("10001", after.zip)
        self.assertAlmostEqual(before.lat, after.lat)
        self.assertAlmostEqual(before.lon, after.lon)

    def test_push_writes_log_line_without_secret(self):
        code, _, _ = self._post(
            json.dumps({"zip": "10001", "lat": 40.7, "lon": -74.0, "source": "ios_shortcut"})
        )
        self.assertEqual(200, code)

        self.assertTrue(self.log_path.is_file())
        lines = self.log_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(1, len(lines))
        record = json.loads(lines[0])
        self.assertEqual("10001", record["zip"])
        self.assertEqual("ios_shortcut", record["source"])
        self.assertNotIn("secret", lines[0].lower())
        self.assertNotIn(_TEST_SECRET, lines[0])


class TravelModePushUnitTests(unittest.TestCase):
    def test_secret_ok_rejects_empty_configured_secret(self):
        with mock.patch.object(handlers, "HP_LOCATION_PUSH_SECRET", ""):
            self.assertFalse(handlers._travel_push_secret_ok("anything"))
            self.assertFalse(handlers._travel_push_secret_ok(""))
            self.assertFalse(handlers._travel_push_secret_ok(None))

    def test_secret_ok_constant_time_compare(self):
        with mock.patch.object(handlers, "HP_LOCATION_PUSH_SECRET", "abc123"):
            self.assertTrue(handlers._travel_push_secret_ok("abc123"))
            self.assertFalse(handlers._travel_push_secret_ok("abc124"))
            self.assertFalse(handlers._travel_push_secret_ok("abc12"))
            self.assertFalse(handlers._travel_push_secret_ok("abc1234"))

    def test_apply_travel_push_strips_zip(self):
        state = HPState.default()
        handlers._apply_travel_push(state, {"zip": " 10001 "})
        self.assertEqual("10001", state.zip)


if __name__ == "__main__":
    unittest.main()
