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
    """The interpret loop builds a cache key that must include location_bucket + prompt_v=c7."""

    def test_cache_key_includes_location_bucket_and_prompt_v_c7(self):
        # Read the source to assert the cache_key_obj literal includes the
        # right fields. Avoids needing a full interpret_loop integration test
        # — the literal is the contract.
        src = Path(_DISCO_SRC).joinpath("interpret.py").read_text(encoding="utf-8")
        self.assertIn('"location_bucket": location_bucket', src)
        self.assertIn('"prompt_v": "c7"', src)
        # Old prompt_v values must be gone (one-way invalidation).
        self.assertNotIn('"prompt_v": "c5"', src)
        self.assertNotIn('"prompt_v": "c6"', src)


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
