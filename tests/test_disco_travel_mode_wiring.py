"""Tests for Disco's Travel Mode wiring — interpret + classifier consume
current_location instead of the old hardcoded Nashville constants.

These are the integration-shaped tests:
- interpret.py's _build_geographic_context() reflects the current location
- the cache_key_obj built in interpret_loop includes location_bucket and prompt_v=c7
- classifier.py passes lat_dd/lon_dd through to lookup_uls + lookup_cdbs

The interpret prompt string is exercised; classifier's wiring is verified via
a focused mock of lookup_uls + lookup_cdbs (the existing
test_classifier_band_plan suite covers the full classifier loop).
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

# disco/src on path so we can import the modules directly.
_DISCO_SRC = str(Path(__file__).resolve().parents[1] / "disco" / "src")
if _DISCO_SRC not in sys.path:
    sys.path.insert(0, _DISCO_SRC)

import current_location  # noqa: E402
from disco.src import interpret  # noqa: E402


class _LocMock:
    """Stand-in for a current_location.Location namedtuple."""

    def __init__(self, zip_, lat, lon, label):
        self.zip = zip_
        self.lat = lat
        self.lon = lon
        self.label = label


class InterpretGeographicContextTests(unittest.TestCase):
    def test_uses_current_location_when_available(self):
        fake = _LocMock("19146", 39.9526, -75.1652, "Philadelphia, PA")
        with mock.patch.object(interpret, "_LOCATION_AVAILABLE", True), \
             mock.patch.object(interpret, "get_current_location", return_value=fake):
            ctx = interpret._build_geographic_context()
        self.assertIn("Philadelphia, PA", ctx)
        self.assertIn("ZIP 19146", ctx)
        self.assertIn("39.9526", ctx)
        self.assertIn("-75.1652", ctx)
        self.assertIn("meteorologist", ctx)

    def test_falls_back_to_nashville_when_module_unavailable(self):
        with mock.patch.object(interpret, "_LOCATION_AVAILABLE", False), \
             mock.patch.object(interpret, "get_current_location", None):
            ctx = interpret._build_geographic_context()
        self.assertIn("Nashville, TN", ctx)
        self.assertIn("meteorologist", ctx)

    def test_nashville_context_renders_natively_for_home_zip(self):
        fake = _LocMock("37221", 36.0662, -86.9639, "Nashville, TN")
        with mock.patch.object(interpret, "_LOCATION_AVAILABLE", True), \
             mock.patch.object(interpret, "get_current_location", return_value=fake):
            ctx = interpret._build_geographic_context()
        self.assertIn("Nashville, TN", ctx)
        self.assertIn("ZIP 37221", ctx)


class InterpretCacheKeyTests(unittest.TestCase):
    """The interpret loop builds a cache key with location_bucket + prompt_v=c8 (HPDB era)."""

    def test_cache_key_includes_location_bucket_and_prompt_v_c8(self):
        src = Path(_DISCO_SRC).joinpath("interpret.py").read_text(encoding="utf-8")
        self.assertIn('"location_bucket": location_bucket', src)
        self.assertIn('"prompt_v": "c8"', src)
        # Old prompt_v values must be gone (one-way invalidation across c5→c7→c8).
        self.assertNotIn('"prompt_v": "c5"', src)
        self.assertNotIn('"prompt_v": "c6"', src)
        self.assertNotIn('"prompt_v": "c7"', src)


class InterpretLicenseeHeaderTests(unittest.TestCase):
    """Three different licensee_block headers depending on uls_source value."""

    def _build_bundle_with_source(self, source: str) -> dict:
        return {
            "freq_mhz": 769.4563,
            "modulation_class": "P25",
            "ml_class": "P25",
            "confidence": 0.91,
            "bandwidth_khz": 8.5,
            "snr_db": 22.0,
            "tuner": "B-T1",
            "protocol_tag": "P25",
            "uls_callsign": "TACN — West Nashville",
            "uls_entity_name": "Tennessee Advanced Communications Network (TACN)",
            "uls_emission_designator": None,
            "uls_station_class": "P25X2_TDMA",
            "uls_distance_km": 5.0,
            "uls_source": source,
            "band_name": "PS_700_NB",
            "band_allowed_modes": ["P25", "DMR"],
            "band_rejected": False,
        }

    def _capture_prompt(self, bundle: dict) -> str:
        """Run call_claude with a mocked HTTP layer; capture the prompt sent."""
        captured = {}

        class _FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return b'{"content":[{"text":"x"}]}'

        def fake_urlopen(req, timeout=None):
            data = req.data
            text = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else (data or "")
            payload = json.loads(text)
            captured["prompt"] = payload["messages"][0]["content"]
            return _FakeResp()

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            interpret.call_claude("fake-key", bundle, "claude-haiku-4-5-20251001", timeout=5.0)
        return captured.get("prompt", "")

    def test_hpdb_conventional_header(self):
        prompt = self._capture_prompt(self._build_bundle_with_source("hpdb-conventional"))
        self.assertIn("Curated label match (HomePatrol — conventional channel)", prompt)
        self.assertNotIn("FCC license match", prompt)
        self.assertNotIn("Broadcast station match", prompt)

    def test_hpdb_trunk_control_header(self):
        prompt = self._capture_prompt(self._build_bundle_with_source("hpdb-trunk_control"))
        self.assertIn("Curated label match (HomePatrol — trunked control channel", prompt)
        self.assertIn("not a per-call talkgroup", prompt)

    def test_uls_header(self):
        prompt = self._capture_prompt(self._build_bundle_with_source("ULS-LM"))
        self.assertIn("FCC license match (ULS)", prompt)
        self.assertNotIn("HomePatrol", prompt)

    def test_cdbs_header(self):
        prompt = self._capture_prompt(self._build_bundle_with_source("cdbs"))
        self.assertIn("Broadcast station match (CDBS)", prompt)
        self.assertNotIn("HomePatrol", prompt)
        self.assertNotIn("FCC license", prompt)


class ClassifierHpdbWiringTests(unittest.TestCase):
    """Source-level assertions: classifier imports + calls HPDB before ULS."""

    def setUp(self):
        self.src = Path(_DISCO_SRC).joinpath("classifier.py").read_text(encoding="utf-8")

    def test_classifier_imports_lookup_hpdb(self):
        self.assertIn("from hpdb import lookup_hpdb", self.src)
        self.assertIn("_HPDB_AVAILABLE", self.src)

    def test_hpdb_block_appears_before_uls_block(self):
        hpdb_idx = self.src.find("if _HPDB_AVAILABLE and lookup_hpdb is not None:")
        uls_idx = self.src.find("if uls_call is None and _ULS_AVAILABLE and lookup_uls is not None:")
        self.assertGreater(hpdb_idx, 0)
        self.assertGreater(uls_idx, 0)
        self.assertLess(hpdb_idx, uls_idx,
                        "HPDB block must appear before ULS so curated labels win")

    def test_hpdb_call_passes_lat_lon_from_current_location(self):
        self.assertIn("lookup_hpdb(\n", self.src)
        # 3 lookup callers now (HPDB + ULS + CDBS) each in a location-aware branch.
        self.assertGreaterEqual(
            self.src.count("lat_dd=_loc.lat"), 3,
            "expected lat_dd=_loc.lat passed to HPDB + ULS + CDBS lookups",
        )

    def test_uls_src_set_to_hpdb_prefix_when_match(self):
        self.assertIn('uls_src = f"hpdb-', self.src)


class ClassifierWiringTests(unittest.TestCase):
    """Verify the classifier source passes lat/lon to lookup_uls + lookup_cdbs.

    Source-level assertions because the full classifier_loop has heavy deps
    (numpy, sqlite, the trained model, etc.) and an end-to-end is overkill
    for confirming a 3-line wiring change. The contract is: "when a current
    location is available, it's passed to the FCC lookups."
    """

    def setUp(self):
        self.src = Path(_DISCO_SRC).joinpath("classifier.py").read_text(encoding="utf-8")

    def test_classifier_imports_get_current_location(self):
        self.assertIn("from current_location import get_current_location", self.src)
        self.assertIn("_LOCATION_AVAILABLE", self.src)

    def test_lookup_uls_call_passes_lat_lon_when_location_available(self):
        # The new call shape: lookup_uls(freq_hz, lat_dd=..., lon_dd=..., limit=1)
        self.assertIn("lat_dd=_loc.lat", self.src)
        self.assertIn("lon_dd=_loc.lon", self.src)

    def test_lookup_uls_legacy_fallback_path_preserved(self):
        # When _LOCATION_AVAILABLE is False, the original positional call
        # shape must remain so Disco still works if current_location fails
        # to import.
        self.assertIn("matches = lookup_uls(meta[\"freq_hz\"], limit=1)", self.src)
        self.assertIn("cdbs_matches = lookup_cdbs(meta[\"freq_hz\"], limit=1)", self.src)

    def test_cdbs_call_pattern_matches_uls(self):
        # Both lookups should follow the same location-aware pattern.
        self.assertGreaterEqual(
            self.src.count("lat_dd=_loc.lat"), 2,
            "expected lat_dd=_loc.lat passed to both ULS and CDBS lookups",
        )


if __name__ == "__main__":
    unittest.main()
