"""Tests for PR #30 — rtl_433 specialist identifier integration.

Covers the do-no-harm contract: lookup_rtl433() returns None (never raises)
for binary-missing / slice-missing / timeout / malformed-output / disabled,
returns a structured match on a clean decode, and the trust hierarchy slots
the rtl_433 layer between the curated DBs and the spectral signature.

subprocess is mocked throughout so these tests don't depend on the rtl_433
binary being installed.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_DISCO_SRC = str(Path(__file__).resolve().parents[1] / "disco" / "src")
if _DISCO_SRC not in sys.path:
    sys.path.insert(0, _DISCO_SRC)

import rtl433  # noqa: E402
import identification  # noqa: E402


class _FakeProc:
    def __init__(self, stdout: bytes):
        self.stdout = stdout
        self.returncode = 0


_ACURITE_JSON = (
    b'{"time":"2026-05-21 20:00:00","model":"Acurite-Tower","id":1234,'
    b'"channel":"A","temperature_C":21.5,"humidity":47}\n'
)


class Rtl433HelperTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        rtl433.STATS_PATH = os.path.join(self._tmp, "stats.json")
        rtl433.STDERR_LOG_PATH = os.path.join(self._tmp, "stderr.log")
        # Reset counters between tests.
        rtl433._STATS.update({"invocations": 0, "matches": 0, "errors": 0,
                              "last_match_ts": 0.0, "last_match_service": ""})

    def test_is_enabled_default_true(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISCO_RTL433_ENABLED", None)
            self.assertTrue(rtl433.is_enabled())

    def test_is_enabled_false_when_zero(self):
        for val in ("0", "false", "no", "off"):
            with mock.patch.dict(os.environ, {"DISCO_RTL433_ENABLED": val}):
                self.assertFalse(rtl433.is_enabled(), f"{val!r} should disable")

    def test_is_available_reflects_which(self):
        with mock.patch("rtl433.shutil.which", return_value="/usr/bin/rtl_433"):
            self.assertTrue(rtl433.is_available())
        with mock.patch("rtl433.shutil.which", return_value=None):
            self.assertFalse(rtl433.is_available())

    def test_is_ism_band(self):
        for f in (315.0e6, 433.92e6, 868.3e6, 915.0e6, 902.5e6):
            self.assertTrue(rtl433.is_ism_band(f), f"{f} should be ISM")
        for f in (162.55e6, 460.0e6, 770.0e6, 98.5e6, 1090.0e6):
            self.assertFalse(rtl433.is_ism_band(f), f"{f} should NOT be ISM")
        self.assertFalse(rtl433.is_ism_band(None))

    def test_rate_from_filename(self):
        name = "B-T2_902345043_7324_50000_1779414067.453_2fa67680.iq.f32"
        self.assertEqual(50000, rtl433._rate_from_filename(name))
        self.assertIsNone(rtl433._rate_from_filename("garbage"))


class LookupRtl433Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        rtl433.STATS_PATH = os.path.join(self._tmp, "stats.json")
        rtl433.STDERR_LOG_PATH = os.path.join(self._tmp, "stderr.log")
        rtl433._STATS.update({"invocations": 0, "matches": 0, "errors": 0,
                              "last_match_ts": 0.0, "last_match_service": ""})
        # A real (empty-ish) slice file so the is_file() check passes.
        self.slice = os.path.join(self._tmp, "B-T2_902345043_7324_50000_1.0_ab.iq.f32")
        with open(self.slice, "wb") as f:
            f.write(b"\x00" * 16384)

    def _enable(self):
        # is_available True + enabled True for the happy paths.
        return mock.patch("rtl433.shutil.which", return_value="/usr/bin/rtl_433")

    def test_happy_path_returns_device(self):
        with self._enable(), \
             mock.patch("rtl433.subprocess.run", return_value=_FakeProc(_ACURITE_JSON)) as m:
            result = rtl433.lookup_rtl433(self.slice, 915.0e6)
        self.assertIsNotNone(result)
        self.assertIn("Acurite-Tower", result["device_name"])
        self.assertEqual("1234", result["device_id"])
        self.assertEqual(0.9, result["confidence"])
        self.assertEqual(21.5, result["metadata"]["temperature_C"])
        # Command used cf32: prefix + sample rate from filename (50000).
        cmd = m.call_args.args[0]
        self.assertIn("cf32:" + self.slice, cmd)
        self.assertIn("50000", cmd)
        # Counters bumped + persisted.
        self.assertEqual(1, rtl433._STATS["matches"])
        stats = rtl433.read_stats()
        self.assertEqual(1, stats["rtl433_matches_total"])
        self.assertIn("Acurite-Tower", stats["rtl433_last_match_service"])

    def test_binary_missing_returns_none(self):
        with mock.patch("rtl433.shutil.which", return_value=None), \
             mock.patch("rtl433.subprocess.run") as m:
            result = rtl433.lookup_rtl433(self.slice, 915.0e6)
        self.assertIsNone(result)
        m.assert_not_called()  # never even spawns the subprocess

    def test_disabled_flag_skips_invocation(self):
        with mock.patch.dict(os.environ, {"DISCO_RTL433_ENABLED": "0"}), \
             self._enable(), \
             mock.patch("rtl433.subprocess.run") as m:
            result = rtl433.lookup_rtl433(self.slice, 915.0e6)
        self.assertIsNone(result)
        m.assert_not_called()

    def test_slice_missing_returns_none(self):
        with self._enable(), mock.patch("rtl433.subprocess.run") as m:
            result = rtl433.lookup_rtl433(
                os.path.join(self._tmp, "does_not_exist.iq.f32"), 915.0e6)
        self.assertIsNone(result)
        m.assert_not_called()
        self.assertEqual(1, rtl433._STATS["errors"])

    def test_timeout_returns_none(self):
        with self._enable(), \
             mock.patch("rtl433.subprocess.run",
                        side_effect=subprocess.TimeoutExpired(cmd="rtl_433", timeout=5.0)):
            result = rtl433.lookup_rtl433(self.slice, 915.0e6)
        self.assertIsNone(result)
        self.assertEqual(1, rtl433._STATS["errors"])

    def test_malformed_output_returns_none(self):
        with self._enable(), \
             mock.patch("rtl433.subprocess.run",
                        return_value=_FakeProc(b"not json at all\n<garbage>\n")):
            result = rtl433.lookup_rtl433(self.slice, 915.0e6)
        self.assertIsNone(result)  # no-match, not an error
        self.assertEqual(0, rtl433._STATS["matches"])

    def test_empty_output_returns_none(self):
        with self._enable(), \
             mock.patch("rtl433.subprocess.run", return_value=_FakeProc(b"")):
            result = rtl433.lookup_rtl433(self.slice, 915.0e6)
        self.assertIsNone(result)

    def test_subprocess_exception_swallowed(self):
        with self._enable(), \
             mock.patch("rtl433.subprocess.run", side_effect=OSError("boom")):
            result = rtl433.lookup_rtl433(self.slice, 915.0e6)
        self.assertIsNone(result)
        self.assertEqual(1, rtl433._STATS["errors"])


class Rtl433TrustHierarchyTests(unittest.TestCase):
    """The rtl_433 layer in build_identification()."""

    def _rtl433_match(self):
        return {"device_name": "Acurite-Tower (id 1234) chA",
                "device_id": "1234", "metadata": {"model": "Acurite-Tower"},
                "confidence": 0.9}

    def test_rtl433_match_yields_high_tier(self):
        r = identification.build_identification(
            modulation_class="unclassified", modulation_confidence=0.0,
            snr_db=20.0, band_name=None,
            rtl433_match=self._rtl433_match(),
        )
        self.assertEqual(identification.CONFIDENCE_HIGH, r.confidence)
        self.assertEqual(identification.SOURCE_RTL433, r.source)
        self.assertIn("Acurite-Tower", r.service)
        self.assertIn("rtl433_match", r.evidence)

    def test_rtl433_outranks_signature(self):
        sig = {"name": "ISM 915 MHz (generic)", "confidence": 0.7,
               "catalog_entry": {}, "features": {}}
        r = identification.build_identification(
            modulation_class="unclassified", modulation_confidence=0.0,
            snr_db=20.0, band_name=None,
            rtl433_match=self._rtl433_match(),
            signature_match=sig,
        )
        self.assertEqual(identification.SOURCE_RTL433, r.source)

    def test_hpdb_overrides_rtl433(self):
        r = identification.build_identification(
            modulation_class="FM_NARROW", modulation_confidence=0.8,
            snr_db=22.0,
            hpdb_match={"source_table": "conventional", "alpha_tag": "Police"},
            rtl433_match=self._rtl433_match(),
        )
        self.assertEqual(identification.SOURCE_HPDB, r.source)

    def test_cdbs_overrides_rtl433(self):
        r = identification.build_identification(
            modulation_class="FM_BROADCAST", modulation_confidence=0.85,
            snr_db=30.0,
            cdbs_match={"callsign": "WPLN-FM", "entity_name": "Nashville PR"},
            rtl433_match=self._rtl433_match(),
        )
        self.assertEqual(identification.SOURCE_CDBS, r.source)


class ClassifierGateLogicTests(unittest.TestCase):
    """The classifier only invokes rtl_433 for ISM bands. Verifies the
    predicate the classifier loop uses (HPDB+CDBS miss already covered by the
    signature gate; here we lock down the ISM-band half)."""

    def test_ism_freqs_pass_gate(self):
        for f in (315.0e6, 433.92e6, 915.0e6):
            self.assertTrue(rtl433.is_ism_band(f))

    def test_non_ism_freqs_skip_gate(self):
        # NOAA WX, UHF LMR, public-safety 700, FM broadcast — rtl_433 must
        # NOT be invoked for these.
        for f in (162.55e6, 460.0e6, 771.0e6, 98.5e6):
            self.assertFalse(rtl433.is_ism_band(f))


class ClassicIsmRangeTests(unittest.TestCase):
    """PR #31 — classic-ISM sub-range + amateur-window predicates."""

    def test_classic_ism_centers(self):
        for f in (315.0e6, 433.92e6, 868.0e6, 903.0e6, 910.0e6, 925.0e6):
            self.assertTrue(rtl433.is_in_classic_ism(f), f"{f} should be classic-ISM")

    def test_amateur_window_excluded_from_classic(self):
        # 915-920 MHz is deliberately NOT classic-ISM.
        for f in (915.5e6, 917.0e6, 919.9e6):
            self.assertFalse(rtl433.is_in_classic_ism(f), f"{f} must not be classic-ISM")
            self.assertTrue(rtl433.is_amateur_33cm(f), f"{f} should be amateur window")

    def test_amateur_predicate_bounds(self):
        self.assertTrue(rtl433.is_amateur_33cm(915.0e6))
        self.assertFalse(rtl433.is_amateur_33cm(920.0e6))   # upper edge → classic
        self.assertFalse(rtl433.is_amateur_33cm(914.9e6))   # just below → classic
        self.assertTrue(rtl433.is_in_classic_ism(914.9e6))
        self.assertTrue(rtl433.is_in_classic_ism(920.0e6))

    def test_non_ism_not_classic(self):
        for f in (162.55e6, 460.0e6, 98.5e6):
            self.assertFalse(rtl433.is_in_classic_ism(f))
            self.assertFalse(rtl433.is_amateur_33cm(f))


class Rtl433PriorityTrustHierarchyTests(unittest.TestCase):
    """PR #31 — rtl433_priority=True makes rtl_433 win ahead of HPDB/CDBS/ULS."""

    def _match(self):
        return {"device_name": "Acurite-606TX (id 4815)", "device_id": "4815",
                "metadata": {"model": "Acurite-606TX"}, "confidence": 0.9}

    def test_priority_beats_uls_at_classic_ism(self):
        # 903 MHz classic-ISM: rtl_433 device decode wins over an amateur ULS.
        r = identification.build_identification(
            modulation_class="unclassified", modulation_confidence=0.0,
            snr_db=20.0, band_name="ISM_915",
            rtl433_match=self._match(), rtl433_priority=True,
            uls_match={"callsign": "AA0JE", "source": "amateur"},
        )
        self.assertEqual(identification.SOURCE_RTL433, r.source)
        self.assertEqual(identification.CONFIDENCE_HIGH, r.confidence)
        self.assertIn("Acurite-606TX", r.service)
        self.assertTrue(r.evidence.get("rtl433_priority"))

    def test_priority_beats_hpdb(self):
        r = identification.build_identification(
            modulation_class="FM_NARROW", modulation_confidence=0.8, snr_db=22.0,
            rtl433_match=self._match(), rtl433_priority=True,
            hpdb_match={"source_table": "conventional", "alpha_tag": "X"},
        )
        self.assertEqual(identification.SOURCE_RTL433, r.source)

    def test_priority_no_match_falls_through_to_uls(self):
        # Classic-ISM but rtl_433 returned nothing → chain proceeds, ULS wins.
        r = identification.build_identification(
            modulation_class="unclassified", modulation_confidence=0.0,
            snr_db=20.0, band_name="ISM_915",
            rtl433_match=None, rtl433_priority=True,
            uls_match={"callsign": "AA0JE", "source": "amateur"},
        )
        self.assertEqual(identification.SOURCE_ULS, r.source)

    def test_amateur_window_uls_wins_no_priority(self):
        # 915.5 MHz: classifier would NOT set priority (and would not invoke
        # rtl_433 at all). Simulate that — no rtl433_match, priority False —
        # and confirm ULS wins.
        r = identification.build_identification(
            modulation_class="unclassified", modulation_confidence=0.0,
            snr_db=20.0, band_name="ISM_915",
            rtl433_match=None, rtl433_priority=False,
            uls_match={"callsign": "AA0JE", "source": "amateur"},
        )
        self.assertEqual(identification.SOURCE_ULS, r.source)

    def test_non_priority_match_still_below_hpdb_cdbs(self):
        # PR #30 fallback path unchanged: non-priority rtl_433 defers to CDBS.
        r = identification.build_identification(
            modulation_class="FM_BROADCAST", modulation_confidence=0.85,
            snr_db=30.0,
            cdbs_match={"callsign": "WPLN-FM", "entity_name": "NPR"},
            rtl433_match=self._match(), rtl433_priority=False,
        )
        self.assertEqual(identification.SOURCE_CDBS, r.source)


if __name__ == "__main__":
    unittest.main()
