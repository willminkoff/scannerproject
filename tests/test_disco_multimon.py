"""Tests for PR #32 — multimon-ng paging decoder integration.

subprocess is mocked throughout so these tests don't depend on the
multimon-ng binary being installed.
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

import multimon  # noqa: E402
import identification  # noqa: E402


class _FakeProc:
    def __init__(self, stdout: bytes):
        self.stdout = stdout
        self.returncode = 0


_POCSAG_OUT = (
    b"POCSAG1200: Address: 1234567  Function: 0  Alpha:   PATIENT IN ROOM 412\n"
)
_FLEX_OUT = b"FLEX: 2026-05-21 1600/2/K [0009876543] ALN test page body\n"


class MultimonHelperTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        multimon.STATS_PATH = os.path.join(self._tmp, "stats.json")
        multimon.STDERR_LOG_PATH = os.path.join(self._tmp, "stderr.log")
        multimon._STATS.update({"invocations": 0, "matches": 0, "errors": 0,
                                "last_match_ts": 0.0, "last_match_capcode": ""})

    def test_is_enabled_default_true(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISCO_MULTIMON_ENABLED", None)
            self.assertTrue(multimon.is_enabled())

    def test_is_enabled_false_when_disabled(self):
        for v in ("0", "false", "no", "off"):
            with mock.patch.dict(os.environ, {"DISCO_MULTIMON_ENABLED": v}):
                self.assertFalse(multimon.is_enabled())

    def test_is_available_reflects_which(self):
        with mock.patch("multimon.shutil.which", return_value="/usr/bin/multimon-ng"):
            self.assertTrue(multimon.is_available())
        with mock.patch("multimon.shutil.which", return_value=None):
            self.assertFalse(multimon.is_available())

    def test_is_paging_band(self):
        for f in (152.84e6, 158.7e6, 451.0e6, 929.6e6):
            self.assertTrue(multimon.is_paging_band(f), f"{f} should be paging")
        for f in (162.55e6, 433.92e6, 915.0e6, 1090e6):
            self.assertFalse(multimon.is_paging_band(f), f"{f} should NOT be paging")
        self.assertFalse(multimon.is_paging_band(None))


class LookupMultimonTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        multimon.STATS_PATH = os.path.join(self._tmp, "stats.json")
        multimon.STDERR_LOG_PATH = os.path.join(self._tmp, "stderr.log")
        multimon._STATS.update({"invocations": 0, "matches": 0, "errors": 0,
                                "last_match_ts": 0.0, "last_match_capcode": ""})
        self.slice = os.path.join(self._tmp, "B-T1_152840000_8000_50000_1.0_ab.iq.f32")
        with open(self.slice, "wb") as f:
            f.write(b"\x00" * 16384)

    def _enable(self):
        return mock.patch("multimon.shutil.which", return_value="/usr/bin/multimon-ng")

    def test_happy_path_pocsag(self):
        with self._enable(), \
             mock.patch("multimon.subprocess.run", return_value=_FakeProc(_POCSAG_OUT)) as m:
            r = multimon.lookup_multimon(self.slice, 152.84e6)
        self.assertIsNotNone(r)
        self.assertEqual("1234567", r["capcode"])
        self.assertIn("PATIENT IN ROOM 412", r["message"])
        self.assertIn("POCSAG1200 capcode 1234567", r["device_name"])
        self.assertEqual(0.85, r["confidence"])
        # command includes the decoders + the slice path
        cmd = m.call_args.args[0]
        self.assertIn("POCSAG1200", cmd)
        self.assertIn("FLEX_NEXT", cmd)
        self.assertIn(self.slice, cmd)
        self.assertEqual(1, multimon._STATS["matches"])
        self.assertEqual("1234567", multimon.read_stats()["multimon_last_match_capcode"])

    def test_happy_path_flex(self):
        with self._enable(), \
             mock.patch("multimon.subprocess.run", return_value=_FakeProc(_FLEX_OUT)):
            r = multimon.lookup_multimon(self.slice, 929.6e6)
        self.assertIsNotNone(r)
        self.assertEqual("0009876543", r["capcode"])
        self.assertEqual("FLEX", r["protocol"])

    def test_binary_missing_returns_none(self):
        with mock.patch("multimon.shutil.which", return_value=None), \
             mock.patch("multimon.subprocess.run") as m:
            r = multimon.lookup_multimon(self.slice, 152.84e6)
        self.assertIsNone(r)
        m.assert_not_called()

    def test_disabled_flag_skips_invocation(self):
        with mock.patch.dict(os.environ, {"DISCO_MULTIMON_ENABLED": "0"}), \
             self._enable(), mock.patch("multimon.subprocess.run") as m:
            r = multimon.lookup_multimon(self.slice, 152.84e6)
        self.assertIsNone(r)
        m.assert_not_called()

    def test_slice_missing_returns_none(self):
        with self._enable(), mock.patch("multimon.subprocess.run") as m:
            r = multimon.lookup_multimon(os.path.join(self._tmp, "nope.iq.f32"), 152.84e6)
        self.assertIsNone(r)
        m.assert_not_called()
        self.assertEqual(1, multimon._STATS["errors"])

    def test_timeout_returns_none(self):
        with self._enable(), \
             mock.patch("multimon.subprocess.run",
                        side_effect=subprocess.TimeoutExpired(cmd="multimon-ng", timeout=5.0)):
            r = multimon.lookup_multimon(self.slice, 152.84e6)
        self.assertIsNone(r)
        self.assertEqual(1, multimon._STATS["errors"])

    def test_malformed_output_returns_none(self):
        with self._enable(), \
             mock.patch("multimon.subprocess.run", return_value=_FakeProc(b"garbage\nnoise\n")):
            r = multimon.lookup_multimon(self.slice, 152.84e6)
        self.assertIsNone(r)
        self.assertEqual(0, multimon._STATS["matches"])

    def test_exception_swallowed(self):
        with self._enable(), \
             mock.patch("multimon.subprocess.run", side_effect=OSError("boom")):
            r = multimon.lookup_multimon(self.slice, 152.84e6)
        self.assertIsNone(r)
        self.assertEqual(1, multimon._STATS["errors"])


class MultimonTrustHierarchyTests(unittest.TestCase):
    def _match(self):
        return {"device_name": "POCSAG1200 capcode 1234567: PATIENT",
                "protocol": "POCSAG1200", "capcode": "1234567",
                "message": "PATIENT", "confidence": 0.85}

    def test_multimon_match_yields_high_tier(self):
        r = identification.build_identification(
            modulation_class="unclassified", modulation_confidence=0.0,
            snr_db=20.0, band_name="PAGING_929", multimon_match=self._match())
        self.assertEqual(identification.CONFIDENCE_HIGH, r.confidence)
        self.assertEqual(identification.SOURCE_MULTIMON, r.source)
        self.assertIn("capcode 1234567", r.service)
        self.assertIn("multimon_match", r.evidence)

    def test_multimon_beats_uls_and_signature(self):
        sig = {"name": "POCSAG / FLEX paging", "confidence": 0.7,
               "catalog_entry": {}, "features": {}}
        r = identification.build_identification(
            modulation_class="unclassified", modulation_confidence=0.0,
            snr_db=20.0, band_name="PAGING_929",
            multimon_match=self._match(),
            uls_match={"callsign": "WPXX", "entity_name": "Pager Co"},
            signature_match=sig)
        self.assertEqual(identification.SOURCE_MULTIMON, r.source)

    def test_classic_ism_rtl433_priority_beats_multimon(self):
        # If both somehow set, the rtl_433 classic-ISM priority step is first.
        r = identification.build_identification(
            modulation_class="unclassified", modulation_confidence=0.0,
            snr_db=20.0,
            rtl433_match={"device_name": "Acurite"}, rtl433_priority=True,
            multimon_match=self._match())
        self.assertEqual(identification.SOURCE_RTL433, r.source)

    def test_no_multimon_match_falls_through(self):
        r = identification.build_identification(
            modulation_class="unclassified", modulation_confidence=0.0,
            snr_db=20.0, band_name="PAGING_929",
            multimon_match=None,
            uls_match={"callsign": "WPXX", "entity_name": "Pager Co"})
        self.assertEqual(identification.SOURCE_ULS, r.source)


class MultimonGateLogicTests(unittest.TestCase):
    def test_paging_freqs_pass_gate(self):
        for f in (152.84e6, 158.7e6, 452.0e6, 930.0e6):
            self.assertTrue(multimon.is_paging_band(f))

    def test_non_paging_freqs_skip(self):
        for f in (162.55e6, 433.92e6, 915.0e6, 98.5e6):
            self.assertFalse(multimon.is_paging_band(f))


if __name__ == "__main__":
    unittest.main()
