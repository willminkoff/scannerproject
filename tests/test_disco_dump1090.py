"""Tests for PR #33 — dump1090 ADS-B decoder integration.

subprocess is mocked throughout so these tests don't depend on the dump1090
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

import dump1090  # noqa: E402
import identification  # noqa: E402


class _FakeProc:
    def __init__(self, stdout: bytes):
        self.stdout = stdout
        self.returncode = 0


_ADSB_OUT = (
    b"*8d4840d6202cc371c32ce0576098;\n"
    b"CRC: 000000\n"
    b"DF 17: ADS-B message.\n"
    b"  ICAO Address:  4840d6 (Mode S / ADS-B)\n"
    b"  Ident: KLM123\n"
    b"  Altitude: 35000 feet\n"
)


class Dump1090HelperTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        dump1090.STATS_PATH = os.path.join(self._tmp, "stats.json")
        dump1090.STDERR_LOG_PATH = os.path.join(self._tmp, "stderr.log")
        dump1090._STATS.update({"invocations": 0, "matches": 0, "errors": 0,
                                "last_match_ts": 0.0, "last_match_icao": ""})

    def test_is_enabled_default_true(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISCO_DUMP1090_ENABLED", None)
            self.assertTrue(dump1090.is_enabled())

    def test_is_enabled_false_when_disabled(self):
        for v in ("0", "false", "no", "off"):
            with mock.patch.dict(os.environ, {"DISCO_DUMP1090_ENABLED": v}):
                self.assertFalse(dump1090.is_enabled())

    def test_is_available_reflects_which(self):
        with mock.patch("dump1090.shutil.which", return_value="/usr/bin/dump1090"):
            self.assertTrue(dump1090.is_available())
        with mock.patch("dump1090.shutil.which", return_value=None):
            self.assertFalse(dump1090.is_available())

    def test_is_adsb_band(self):
        for f in (1090.0e6, 1089.5e6, 1090.9e6):
            self.assertTrue(dump1090.is_adsb_band(f), f"{f} should be ADS-B")
        for f in (1088.0e6, 1092.0e6, 915.0e6, 162.55e6):
            self.assertFalse(dump1090.is_adsb_band(f), f"{f} should NOT be ADS-B")
        self.assertFalse(dump1090.is_adsb_band(None))


class LookupDump1090Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        dump1090.STATS_PATH = os.path.join(self._tmp, "stats.json")
        dump1090.STDERR_LOG_PATH = os.path.join(self._tmp, "stderr.log")
        dump1090._STATS.update({"invocations": 0, "matches": 0, "errors": 0,
                                "last_match_ts": 0.0, "last_match_icao": ""})
        self.slice = os.path.join(self._tmp, "A-T1_1090000000_2000000_50000_1.0_ab.iq.f32")
        with open(self.slice, "wb") as f:
            f.write(b"\x00" * 16384)

    def _enable(self):
        return mock.patch("dump1090.shutil.which", return_value="/usr/bin/dump1090")

    def test_happy_path(self):
        with self._enable(), \
             mock.patch("dump1090.subprocess.run", return_value=_FakeProc(_ADSB_OUT)) as m:
            r = dump1090.lookup_dump1090(self.slice, 1090.0e6)
        self.assertIsNotNone(r)
        self.assertEqual("4840d6", r["icao"])
        self.assertEqual("KLM123", r["flight"])
        self.assertEqual("35000", r["altitude"])
        self.assertIn("ICAO 4840d6", r["device_name"])
        self.assertEqual(0.95, r["confidence"])
        cmd = m.call_args.args[0]
        self.assertIn("--ifile", cmd)
        self.assertIn(self.slice, cmd)
        self.assertEqual("4840d6", dump1090.read_stats()["dump1090_last_match_icao"])

    def test_icao_only_still_matches(self):
        out = b"  ICAO Address:  abcdef\n"
        with self._enable(), mock.patch("dump1090.subprocess.run", return_value=_FakeProc(out)):
            r = dump1090.lookup_dump1090(self.slice, 1090.0e6)
        self.assertIsNotNone(r)
        self.assertEqual("abcdef", r["icao"])
        self.assertIsNone(r["flight"])

    def test_binary_missing_returns_none(self):
        with mock.patch("dump1090.shutil.which", return_value=None), \
             mock.patch("dump1090.subprocess.run") as m:
            r = dump1090.lookup_dump1090(self.slice, 1090.0e6)
        self.assertIsNone(r)
        m.assert_not_called()

    def test_disabled_flag_skips_invocation(self):
        with mock.patch.dict(os.environ, {"DISCO_DUMP1090_ENABLED": "0"}), \
             self._enable(), mock.patch("dump1090.subprocess.run") as m:
            r = dump1090.lookup_dump1090(self.slice, 1090.0e6)
        self.assertIsNone(r)
        m.assert_not_called()

    def test_slice_missing_returns_none(self):
        with self._enable(), mock.patch("dump1090.subprocess.run") as m:
            r = dump1090.lookup_dump1090(os.path.join(self._tmp, "nope.iq.f32"), 1090.0e6)
        self.assertIsNone(r)
        m.assert_not_called()
        self.assertEqual(1, dump1090._STATS["errors"])

    def test_timeout_returns_none(self):
        with self._enable(), \
             mock.patch("dump1090.subprocess.run",
                        side_effect=subprocess.TimeoutExpired(cmd="dump1090", timeout=5.0)):
            r = dump1090.lookup_dump1090(self.slice, 1090.0e6)
        self.assertIsNone(r)
        self.assertEqual(1, dump1090._STATS["errors"])

    def test_no_aircraft_returns_none(self):
        with self._enable(), \
             mock.patch("dump1090.subprocess.run", return_value=_FakeProc(b"noise\nCRC: ffff\n")):
            r = dump1090.lookup_dump1090(self.slice, 1090.0e6)
        self.assertIsNone(r)
        self.assertEqual(0, dump1090._STATS["matches"])

    def test_exception_swallowed(self):
        with self._enable(), \
             mock.patch("dump1090.subprocess.run", side_effect=OSError("boom")):
            r = dump1090.lookup_dump1090(self.slice, 1090.0e6)
        self.assertIsNone(r)
        self.assertEqual(1, dump1090._STATS["errors"])


class Dump1090TrustHierarchyTests(unittest.TestCase):
    def _match(self):
        return {"device_name": "Aircraft ICAO 4840d6 (KLM123) @ 35000 ft",
                "icao": "4840d6", "flight": "KLM123", "altitude": "35000",
                "confidence": 0.95}

    def test_dump1090_match_yields_high_tier(self):
        r = identification.build_identification(
            modulation_class="unclassified", modulation_confidence=0.0,
            snr_db=20.0, band_name="DME_TACAN", dump1090_match=self._match())
        self.assertEqual(identification.CONFIDENCE_HIGH, r.confidence)
        self.assertEqual(identification.SOURCE_DUMP1090, r.source)
        self.assertIn("ICAO 4840d6", r.service)
        self.assertIn("dump1090_match", r.evidence)

    def test_dump1090_beats_uls(self):
        r = identification.build_identification(
            modulation_class="unclassified", modulation_confidence=0.0,
            snr_db=20.0, band_name="DME_TACAN",
            dump1090_match=self._match(),
            uls_match={"callsign": "WXYZ", "entity_name": "Aero"})
        self.assertEqual(identification.SOURCE_DUMP1090, r.source)

    def test_rtl433_priority_and_multimon_precede_dump1090(self):
        # Step order: rtl433 priority (0) > multimon (0b) > dump1090 (0c).
        r1 = identification.build_identification(
            modulation_class="unclassified", modulation_confidence=0.0, snr_db=20.0,
            rtl433_match={"device_name": "X"}, rtl433_priority=True,
            dump1090_match=self._match())
        self.assertEqual(identification.SOURCE_RTL433, r1.source)
        r2 = identification.build_identification(
            modulation_class="unclassified", modulation_confidence=0.0, snr_db=20.0,
            multimon_match={"device_name": "POCSAG capcode 1"},
            dump1090_match=self._match())
        self.assertEqual(identification.SOURCE_MULTIMON, r2.source)

    def test_no_dump1090_match_falls_through(self):
        r = identification.build_identification(
            modulation_class="unclassified", modulation_confidence=0.0,
            snr_db=20.0, band_name="DME_TACAN", dump1090_match=None,
            uls_match={"callsign": "WXYZ", "entity_name": "Aero"})
        self.assertEqual(identification.SOURCE_ULS, r.source)


class Dump1090GateLogicTests(unittest.TestCase):
    def test_adsb_freq_passes_gate(self):
        for f in (1090.0e6, 1089.2e6, 1090.8e6):
            self.assertTrue(dump1090.is_adsb_band(f))

    def test_non_adsb_skips(self):
        for f in (915.0e6, 162.55e6, 1090.0e6 + 2e6):
            self.assertFalse(dump1090.is_adsb_band(f))


if __name__ == "__main__":
    unittest.main()
