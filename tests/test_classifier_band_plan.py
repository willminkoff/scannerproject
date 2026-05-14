"""Phase 4 — C3 integration tests for the new derive_protocol_tag().

Verifies the dispatcher in disco/src/classifier.py wires the band_plan helper
correctly: in-band classes pass through with a band tag, out-of-band classes
get the reject form, missing-plan falls back to the legacy heuristic.

Does NOT exercise the full classifier_loop (which needs ONNX model + sqlite +
slice files — out of scope for unit tests). C3's behavior change is isolated
to the derive_protocol_tag dispatch layer.
"""
from __future__ import annotations

import logging
import unittest
from pathlib import Path

from disco.src.band_plan import Band, load_band_plan
from disco.src import classifier


REPO_ROOT = Path(__file__).resolve().parent.parent
US_BAND_PLAN = REPO_ROOT / "disco" / "configs" / "us_band_plan.yaml"


def _synthetic_plan():
    return [
        Band(
            name="TEST_AVIATION_NAV",
            freq_min_hz=108_000_000,
            freq_max_hz=117_975_000,
            allowed_modes=frozenset({"AM_VOICE"}),
        ),
        Band(
            name="TEST_PS_800",
            freq_min_hz=851_000_000,
            freq_max_hz=869_000_000,
            allowed_modes=frozenset({"P25", "FM_NARROW"}),
        ),
    ]


class DeriveProtocolTagTests(unittest.TestCase):
    def test_in_band_allowed(self):
        plan = _synthetic_plan()
        tag = classifier.derive_protocol_tag("P25", 851_550_000, 12_500, plan)
        self.assertEqual(tag, "TEST_PS_800 — P25")

    def test_in_band_rejected(self):
        """Canonical NXDN-at-116.98 case routed through classifier dispatcher."""
        plan = _synthetic_plan()
        tag = classifier.derive_protocol_tag("NXDN", 116_980_100, 8_000, plan)
        self.assertEqual(
            tag,
            "TEST_AVIATION_NAV — unidentified (model said: NXDN)",
        )

    def test_outside_any_band_returns_unmodified(self):
        plan = _synthetic_plan()
        tag = classifier.derive_protocol_tag("FM_NARROW", 50_000_000, 25_000, plan)
        self.assertEqual(tag, "FM_NARROW")

    def test_plan_none_falls_back_to_legacy(self):
        """When plan=None, dispatcher must call _legacy_derive_protocol_tag.
        We probe a freq+class combo the legacy function recognizes."""
        # legacy returns "Airband (AM)" for is_am=True at 118-137 MHz, bw_khz < 25
        tag = classifier.derive_protocol_tag("AM", 119_500_000, 8_000, None)
        self.assertEqual(tag, "Airband (AM)")

    def test_legacy_function_still_callable(self):
        """_legacy_derive_protocol_tag must remain importable for one release
        as a fallback per Will's spec."""
        self.assertTrue(callable(classifier._legacy_derive_protocol_tag))
        result = classifier._legacy_derive_protocol_tag("FM", 162_500_000, 20_000)
        self.assertEqual(result, "NOAA WX")


class DeriveProtocolTagLoggingTests(unittest.TestCase):
    """Verify the band-plan rejection log line emits with the expected format."""

    def test_rejection_emits_log(self):
        plan = _synthetic_plan()
        with self.assertLogs(classifier.LOG, level="INFO") as captured:
            classifier.derive_protocol_tag("NXDN", 116_980_100, 8_000, plan)
        rejection_lines = [m for m in captured.output if "band-plan rejected" in m]
        self.assertEqual(len(rejection_lines), 1)
        line = rejection_lines[0]
        self.assertIn("freq=116.9801 MHz", line)
        self.assertIn("ml=NXDN", line)
        self.assertIn("band=TEST_AVIATION_NAV", line)
        self.assertIn("unidentified", line)

    def test_in_band_does_not_emit_rejection_log(self):
        plan = _synthetic_plan()
        with self.assertLogs(classifier.LOG, level="INFO") as captured:
            classifier.derive_protocol_tag("P25", 851_550_000, 12_500, plan)
            # need at least one log to satisfy assertLogs — emit a sentinel
            classifier.LOG.info("sentinel")
        rejection_lines = [m for m in captured.output if "band-plan rejected" in m]
        self.assertEqual(rejection_lines, [])


class RealUsBandPlanIntegrationTests(unittest.TestCase):
    """Wire the C3 dispatcher against the actual configs/us_band_plan.yaml."""

    @classmethod
    def setUpClass(cls):
        if not US_BAND_PLAN.exists():
            raise unittest.SkipTest(f"{US_BAND_PLAN} not present")
        cls.plan = load_band_plan(str(US_BAND_PLAN))

    def test_canonical_nxdn_at_116_98(self):
        tag = classifier.derive_protocol_tag("NXDN", 116_980_100, 8_000, self.plan)
        self.assertEqual(
            tag,
            "AVIATION_NAV — unidentified (model said: NXDN)",
        )

    def test_legitimate_p25_at_mtrtrs(self):
        """MTRTRS control channel at 851.55 MHz (P25 in PS_800_NARROW band)."""
        tag = classifier.derive_protocol_tag("P25", 851_550_000, 12_500, self.plan)
        self.assertEqual(tag, "PS_800_NARROW — P25")

    def test_legitimate_p25_at_tacn(self):
        """TACN control channel at 769.456 MHz (P25 in PS_700_NARROW band)."""
        tag = classifier.derive_protocol_tag("P25", 769_456_250, 12_500, self.plan)
        self.assertEqual(tag, "PS_700_NARROW — P25")

    def test_lte_mislabeled_in_aviation_nav(self):
        """Another anomaly class for the same airband: model predicts LTE in
        AVIATION_NAV. Verify the same reject path applies."""
        tag = classifier.derive_protocol_tag("LTE", 110_500_000, 10_000_000, self.plan)
        self.assertEqual(
            tag,
            "AVIATION_NAV — unidentified (model said: LTE)",
        )


if __name__ == "__main__":
    unittest.main()
