"""Tests for disco/src/current_location.py — Travel Mode location plumbing.

Covers the read+cache+fallback contract Disco's interpret/classifier depend on:
- happy path: HPState present and valid → returns ZIP/lat/lon from disk
- file missing / malformed JSON / mid-write truncation → returns Nashville fallback
- TTL cache: repeat calls within window don't re-read disk
- version stamp: increments only when the cached value actually changed
- bucket key: ZIP first-3 prefix, with home fallback
- label table: known prefixes map to city names, unknown → "ZIP NNNNN"

End-to-end via Disco's interpret + classifier wiring lives in the existing
test_interpret_band_plan / test_classifier_band_plan suites; this file only
covers the location module itself.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# Add disco/src to path so we can import the module directly (matches how
# classifier.py / interpret.py import it).
_DISCO_SRC = str(Path(__file__).resolve().parents[1] / "disco" / "src")
if _DISCO_SRC not in sys.path:
    sys.path.insert(0, _DISCO_SRC)

import current_location  # noqa: E402


def _seed_state(path, **overrides):
    payload = {
        "mode": "favorites",
        "use_location": True,
        "strict_location": False,
        "zip": "37221",
        "lat": 36.0662,
        "lon": -86.9639,
        "range_miles": 20.0,
        "travel_mode_enabled": False,
    }
    payload.update(overrides)
    Path(path).write_text(json.dumps(payload), encoding="utf-8")


class CurrentLocationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_path = os.path.join(self._tmp.name, "hp_state.json")

        # Force re-read on every test call so cache TTL doesn't bleed across
        # tests. TTL=0 means "always treat as stale".
        self._env_patcher = mock.patch.dict(os.environ, {
            "DISCO_HP_STATE_PATH": self.state_path,
            "DISCO_LOCATION_CACHE_TTL_SEC": "0",
            # These cases exercise the FILE source, so the HTTP source
            # is disabled. Without this they reach the real SB3 host:
            # the suite slows to a crawl on timeouts and silently tests
            # whatever that box reports instead of the seeded fixture.
            "DISCO_SB3_STATE_URL": "",
        })
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)
        current_location.reset_cache_for_tests()

    def test_happy_path_returns_hp_state_values(self):
        _seed_state(self.state_path, zip="10001", lat=40.7128, lon=-74.0060)
        loc = current_location.get_current_location()
        self.assertEqual("10001", loc.zip)
        self.assertAlmostEqual(40.7128, loc.lat)
        self.assertAlmostEqual(-74.0060, loc.lon)
        self.assertEqual("New York, NY", loc.label)

    def test_missing_file_returns_home_fallback(self):
        # No file written → read returns None → home fallback.
        loc = current_location.get_current_location()
        self.assertEqual(current_location.HOME_ZIP, loc.zip)
        self.assertAlmostEqual(current_location.HOME_LAT, loc.lat)
        self.assertAlmostEqual(current_location.HOME_LON, loc.lon)
        self.assertEqual(current_location.HOME_LABEL, loc.label)

    def test_malformed_json_returns_home_fallback(self):
        Path(self.state_path).write_text("{not valid json", encoding="utf-8")
        loc = current_location.get_current_location()
        self.assertEqual(current_location.HOME_ZIP, loc.zip)
        self.assertEqual(current_location.HOME_LABEL, loc.label)

    def test_truncated_mid_write_returns_home_fallback(self):
        # Simulate mid-write: open file with partial JSON. The atomic-rename
        # pattern SB3 uses for HPState.save() makes this rare but possible if
        # the writer is interrupted before rename.
        Path(self.state_path).write_text('{"zip": "1000', encoding="utf-8")
        loc = current_location.get_current_location()
        self.assertEqual(current_location.HOME_ZIP, loc.zip)

    def test_empty_file_returns_home_fallback(self):
        Path(self.state_path).write_text("", encoding="utf-8")
        loc = current_location.get_current_location()
        self.assertEqual(current_location.HOME_ZIP, loc.zip)

    def test_non_dict_root_returns_home_fallback(self):
        # JSON root is a list, not a dict.
        Path(self.state_path).write_text("[1, 2, 3]", encoding="utf-8")
        loc = current_location.get_current_location()
        self.assertEqual(current_location.HOME_ZIP, loc.zip)

    def test_malformed_zip_falls_back_to_home_zip_only(self):
        # ZIP is non-5-digit (string ABCDE) — that field falls back, but lat/lon
        # from the same payload still come through.
        _seed_state(self.state_path, zip="ABCDE", lat=40.0, lon=-74.0)
        loc = current_location.get_current_location()
        self.assertEqual(current_location.HOME_ZIP, loc.zip)
        self.assertAlmostEqual(40.0, loc.lat)
        self.assertAlmostEqual(-74.0, loc.lon)

    def test_out_of_range_lat_lon_clamped_to_home(self):
        _seed_state(self.state_path, zip="10001", lat=999.0, lon=-999.0)
        loc = current_location.get_current_location()
        self.assertEqual("10001", loc.zip)
        self.assertAlmostEqual(current_location.HOME_LAT, loc.lat)
        self.assertAlmostEqual(current_location.HOME_LON, loc.lon)

    def test_label_lookup_known_prefix(self):
        cases = [
            ("37221", "Nashville, TN"),
            ("19146", "Philadelphia, PA"),
            ("10001", "New York, NY"),
            ("30303", "Atlanta, GA"),
            ("60601", "Chicago, IL"),
            ("90001", "Los Angeles, CA"),
            ("94101", "San Francisco, CA"),
        ]
        for zip_code, expected in cases:
            with self.subTest(zip=zip_code):
                self.assertEqual(expected, current_location._label_for_zip(zip_code))

    def test_label_lookup_unknown_prefix_falls_back(self):
        # 555 isn't in the prefix table.
        self.assertEqual("ZIP 55555", current_location._label_for_zip("55555"))

    def test_label_lookup_blank_zip_returns_home_label(self):
        self.assertEqual(current_location.HOME_LABEL, current_location._label_for_zip(""))

    def test_bucket_uses_zip_first_three(self):
        _seed_state(self.state_path, zip="19146")
        # bucket implicitly calls get_current_location()
        self.assertEqual("191", current_location.get_location_bucket())

    def test_bucket_falls_back_to_home_when_state_missing(self):
        # No file → home fallback → bucket is "372" (Nashville).
        self.assertEqual("372", current_location.get_location_bucket())


class CurrentLocationCacheTests(unittest.TestCase):
    """Cache TTL + version-stamp behavior, with mocked time + read calls."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_path = os.path.join(self._tmp.name, "hp_state.json")
        self._env_patcher = mock.patch.dict(os.environ, {
            "DISCO_HP_STATE_PATH": self.state_path,
            "DISCO_LOCATION_CACHE_TTL_SEC": "60",
            # File-source cases: disable the HTTP source so these count disk
            # reads rather than silently short-circuiting on a live SB3 host.
            "DISCO_SB3_STATE_URL": "",
        })
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)
        current_location.reset_cache_for_tests()

    def test_repeat_calls_within_ttl_dont_re_read_disk(self):
        _seed_state(self.state_path, zip="10001", lat=40.7, lon=-74.0)
        original = current_location._read_hp_state
        calls = []

        def counter(path):
            calls.append(path)
            return original(path)

        with mock.patch.object(current_location, "_read_hp_state", side_effect=counter):
            current_location.get_current_location()
            current_location.get_current_location()
            current_location.get_current_location()
        self.assertEqual(1, len(calls))

    def test_force_refresh_re_reads_even_within_ttl(self):
        _seed_state(self.state_path, zip="10001", lat=40.7, lon=-74.0)
        current_location.get_current_location()  # populates cache
        original = current_location._read_hp_state
        calls = []

        def counter(path):
            calls.append(path)
            return original(path)

        with mock.patch.object(current_location, "_read_hp_state", side_effect=counter):
            current_location.get_current_location(force_refresh=True)
        self.assertEqual(1, len(calls))

    def test_version_increments_only_when_location_changes(self):
        _seed_state(self.state_path, zip="10001", lat=40.7, lon=-74.0)
        current_location.get_current_location(force_refresh=True)
        v1 = current_location.get_location_version()

        # Same state, force refresh → version should NOT increment.
        current_location.get_current_location(force_refresh=True)
        v2 = current_location.get_location_version()
        self.assertEqual(v1, v2)

        # Change ZIP, force refresh → version SHOULD increment.
        _seed_state(self.state_path, zip="19146", lat=39.95, lon=-75.16)
        current_location.get_current_location(force_refresh=True)
        v3 = current_location.get_location_version()
        self.assertEqual(v1 + 1, v3)


class TravelModeEndToEndTests(unittest.TestCase):
    """End-to-end: change HPState ZIP, confirm next call sees new location."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_path = os.path.join(self._tmp.name, "hp_state.json")
        self._env_patcher = mock.patch.dict(os.environ, {
            "DISCO_HP_STATE_PATH": self.state_path,
            "DISCO_LOCATION_CACHE_TTL_SEC": "0",
            # These cases exercise the FILE source, so the HTTP source
            # is disabled. Without this they reach the real SB3 host:
            # the suite slows to a crawl on timeouts and silently tests
            # whatever that box reports instead of the seeded fixture.
            "DISCO_SB3_STATE_URL": "",
        })
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)
        current_location.reset_cache_for_tests()

    def test_zip_change_propagates_to_next_call(self):
        _seed_state(self.state_path, zip="37221", lat=36.0662, lon=-86.9639)
        first = current_location.get_current_location()
        self.assertEqual("37221", first.zip)
        self.assertEqual("Nashville, TN", first.label)
        self.assertEqual("372", current_location.get_location_bucket())

        # iPhone Shortcut pushed Philadelphia.
        _seed_state(self.state_path, zip="19146", lat=39.9526, lon=-75.1652)
        second = current_location.get_current_location()
        self.assertEqual("19146", second.zip)
        self.assertEqual("Philadelphia, PA", second.label)
        self.assertEqual("191", current_location.get_location_bucket())


if __name__ == "__main__":
    unittest.main()


class SB3HttpSourceTests(unittest.TestCase):
    """Location now comes from SB3 over HTTP, not a shared filesystem.

    Disco and SB3 used to run on the same box, so reading SB3's hp_state.json
    directly was the cheap option. They no longer do: SB3 is on Neptune and
    Disco on scannerbox-1, where that path does not exist. Every lookup fell
    through to the Nashville home tuple, silently, and Disco spent a New
    Jersey deployment classifying signals against Tennessee licensees.
    """

    SB3_PAYLOAD = {"ok": True, "state": {"zip": "08243", "lat": 39.154, "lon": -74.7005}}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_path = os.path.join(self._tmp.name, "hp_state.json")
        self._env = mock.patch.dict(os.environ, {
            "DISCO_HP_STATE_PATH": self.state_path,
            "DISCO_LOCATION_CACHE_TTL_SEC": "0",
            "DISCO_SB3_STATE_URL": "http://sb3.test/api/scan/state",
        })
        self._env.start()
        self.addCleanup(self._env.stop)
        current_location.reset_cache_for_tests()

    def _http(self, payload=None, raises=False):
        """Stub urlopen; payload=None with raises=False yields a bad body."""
        class _Resp:
            def __init__(self, body): self._b = body
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def _open(url, timeout=0):
            if raises:
                raise OSError("unreachable")
            body = json.dumps(payload).encode() if payload is not None else b"not json"
            return _Resp(body)
        return mock.patch.object(current_location.urllib.request, "urlopen", _open)

    def test_http_is_the_source_of_truth(self):
        with self._http(self.SB3_PAYLOAD):
            loc = current_location.get_current_location()
        self.assertEqual("08243", loc.zip)
        self.assertAlmostEqual(39.154, loc.lat)

    def test_http_wins_over_a_stale_local_file(self):
        """A leftover file must not override where SB3 says we are."""
        _seed_state(self.state_path, zip="37221", lat=36.0662, lon=-86.9639)
        with self._http(self.SB3_PAYLOAD):
            loc = current_location.get_current_location()
        self.assertEqual("08243", loc.zip)

    def test_falls_back_to_file_when_sb3_is_unreachable(self):
        _seed_state(self.state_path, zip="10001", lat=40.7128, lon=-74.0060)
        with self._http(raises=True):
            loc = current_location.get_current_location()
        self.assertEqual("10001", loc.zip)

    def test_falls_back_to_file_on_garbage_response(self):
        _seed_state(self.state_path, zip="10001", lat=40.7128, lon=-74.0060)
        with self._http(payload=None):
            loc = current_location.get_current_location()
        self.assertEqual("10001", loc.zip)

    def test_home_fallback_is_logged_not_silent(self):
        """The silence is what let this run unnoticed for a whole trip."""
        with self._http(raises=True):
            with self.assertLogs(current_location.logger, level="WARNING") as cm:
                loc = current_location.get_current_location()
        self.assertEqual(current_location.HOME_ZIP, loc.zip)
        self.assertTrue(any("falling back to home" in m for m in cm.output))

    def test_empty_url_disables_http(self):
        _seed_state(self.state_path, zip="10001", lat=40.7128, lon=-74.0060)
        with mock.patch.dict(os.environ, {"DISCO_SB3_STATE_URL": ""}):
            loc = current_location.get_current_location()
        self.assertEqual("10001", loc.zip)

    def test_south_jersey_zips_get_real_labels(self):
        with self._http(self.SB3_PAYLOAD):
            loc = current_location.get_current_location()
        self.assertEqual("Cape May County, NJ", loc.label)
