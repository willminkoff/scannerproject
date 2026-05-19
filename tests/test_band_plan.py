"""Unit tests for disco.src.band_plan — Layer 1 of the Phase 4 classifier.

Two flavors of test:
  - Pure unit tests with synthetic in-memory band plans (boundary behavior).
  - Integration tests that load the real configs/us_band_plan.yaml and exercise
    the canonical bug case (NXDN-at-116.98-MHz) end-to-end.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from disco.src.band_plan import (
    Band,
    band_for,
    is_mode_allowed,
    load_band_plan,
    tag_for,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
US_BAND_PLAN = REPO_ROOT / "disco" / "configs" / "us_band_plan.yaml"


def _synthetic_plan() -> list:
    """Three-band plan with overlapping coverage and an empty-allowed-modes
    band (the radio-astronomy / reject-all case)."""
    return [
        Band(
            name="TEST_AVIATION_NAV",
            freq_min_hz=108_000_000,
            freq_max_hz=117_975_000,
            allowed_modes=frozenset({"AM_VOICE"}),
            notes="aviation nav — AM only",
        ),
        Band(
            name="TEST_AVIATION_VOICE",
            freq_min_hz=118_000_000,
            freq_max_hz=136_975_000,
            allowed_modes=frozenset({"AM_VOICE"}),
        ),
        Band(
            name="TEST_RADIO_ASTRONOMY",
            freq_min_hz=608_000_000,
            freq_max_hz=614_000_000,
            allowed_modes=frozenset(),
        ),
    ]


class BandForTests(unittest.TestCase):
    def test_freq_inside_band(self):
        plan = _synthetic_plan()
        band = band_for(112_000_000, plan)
        self.assertIsNotNone(band)
        self.assertEqual(band.name, "TEST_AVIATION_NAV")

    def test_freq_outside_any_band(self):
        plan = _synthetic_plan()
        self.assertIsNone(band_for(50_000_000, plan))

    def test_exact_lower_boundary(self):
        plan = _synthetic_plan()
        band = band_for(108_000_000, plan)
        self.assertEqual(band.name, "TEST_AVIATION_NAV")

    def test_exact_upper_boundary(self):
        plan = _synthetic_plan()
        band = band_for(117_975_000, plan)
        self.assertEqual(band.name, "TEST_AVIATION_NAV")

    def test_one_hz_above_upper_boundary(self):
        plan = _synthetic_plan()
        # 117_975_001 falls in the gap between AVIATION_NAV upper (117_975_000)
        # and AVIATION_VOICE lower (118_000_000). The synthetic plan exposes
        # the same intentional gap as the real us_band_plan.yaml.
        self.assertIsNone(band_for(117_975_001, plan))

    def test_first_match_wins(self):
        plan = [
            Band(
                name="WIDE",
                freq_min_hz=100_000_000,
                freq_max_hz=200_000_000,
                allowed_modes=frozenset({"FM_NARROW"}),
            ),
            Band(
                name="NARROW",
                freq_min_hz=120_000_000,
                freq_max_hz=130_000_000,
                allowed_modes=frozenset({"AM_VOICE"}),
            ),
        ]
        # WIDE wins because it's first — caller is responsible for ordering.
        band = band_for(125_000_000, plan)
        self.assertEqual(band.name, "WIDE")


class IsModeAllowedTests(unittest.TestCase):
    def test_allowed_in_band(self):
        plan = _synthetic_plan()
        self.assertTrue(is_mode_allowed("AM_VOICE", 112_000_000, plan))

    def test_rejected_in_band(self):
        plan = _synthetic_plan()
        self.assertFalse(is_mode_allowed("NXDN", 112_000_000, plan))

    def test_permissive_outside_any_band(self):
        plan = _synthetic_plan()
        # 50 MHz is not in any synthetic band → permissive default → True.
        self.assertTrue(is_mode_allowed("NXDN", 50_000_000, plan))

    def test_empty_allowed_modes_rejects_all(self):
        plan = _synthetic_plan()
        # RADIO_ASTRONOMY band has allowed_modes=[] — any class rejected.
        self.assertFalse(is_mode_allowed("FM_NARROW", 611_000_000, plan))
        self.assertFalse(is_mode_allowed("NOISE", 611_000_000, plan))


class TagForTests(unittest.TestCase):
    def test_in_band_allowed(self):
        plan = _synthetic_plan()
        tag = tag_for("AM_VOICE", 119_500_000, plan)
        self.assertEqual(tag, "TEST_AVIATION_VOICE — AM_VOICE")

    def test_in_band_rejected(self):
        plan = _synthetic_plan()
        tag = tag_for("NXDN", 116_980_100, plan)
        self.assertEqual(
            tag,
            "TEST_AVIATION_NAV — unidentified",
        )

    def test_outside_band_returns_class_unmodified(self):
        plan = _synthetic_plan()
        tag = tag_for("FM_NARROW", 50_000_000, plan)
        self.assertEqual(tag, "FM_NARROW")

    def test_empty_allowed_modes_rejects(self):
        plan = _synthetic_plan()
        tag = tag_for("FM_NARROW", 611_000_000, plan)
        self.assertEqual(
            tag,
            "TEST_RADIO_ASTRONOMY — unidentified",
        )


class LoadBandPlanTests(unittest.TestCase):
    def test_round_trip_minimal(self):
        yaml_content = (
            "bands:\n"
            "  - name: MINI\n"
            "    freq_min_hz: 100\n"
            "    freq_max_hz: 200\n"
            "    allowed_modes: [FM_NARROW, AM_VOICE]\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(yaml_content)
            path = f.name
        try:
            plan = load_band_plan(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0].name, "MINI")
        self.assertEqual(plan[0].freq_min_hz, 100)
        self.assertEqual(plan[0].allowed_modes, frozenset({"FM_NARROW", "AM_VOICE"}))
        self.assertEqual(plan[0].notes, "")  # missing notes defaults to empty

    def test_empty_allowed_modes_loads_as_empty_frozenset(self):
        yaml_content = (
            "bands:\n"
            "  - name: PROTECTED\n"
            "    freq_min_hz: 1000\n"
            "    freq_max_hz: 2000\n"
            "    allowed_modes: []\n"
            "    notes: reject all\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(yaml_content)
            path = f.name
        try:
            plan = load_band_plan(path)
        finally:
            os.unlink(path)
        self.assertEqual(plan[0].allowed_modes, frozenset())
        self.assertEqual(plan[0].notes, "reject all")

    def test_band_is_frozen(self):
        plan = _synthetic_plan()
        with self.assertRaises(Exception):
            plan[0].name = "MUTATED"  # type: ignore[misc]


class RealUsBandPlanTests(unittest.TestCase):
    """Integration tests against the actual configs/us_band_plan.yaml.

    Skipped if the file doesn't exist — keeps the unit tests above runnable
    in isolation (e.g., when running tests on a partial checkout)."""

    @classmethod
    def setUpClass(cls):
        if not US_BAND_PLAN.exists():
            raise unittest.SkipTest(f"{US_BAND_PLAN} not present")
        cls.plan = load_band_plan(str(US_BAND_PLAN))

    def test_plan_is_nonempty(self):
        self.assertGreater(len(self.plan), 30)

    def test_canonical_nxdn_at_116_98(self):
        """The bug Phase 4 is designed to catch: ML predicts NXDN at 116.9801
        MHz, which is in AVIATION_NAV (108-117.975 MHz) — AM-only by FCC
        allocation. Expect a reject tag preserving 'NXDN' in the model-said
        clause for retrain-set curation."""
        tag = tag_for("NXDN", 116_980_100, self.plan)
        self.assertEqual(
            tag,
            "AVIATION_NAV — unidentified",
        )

    def test_legitimate_airband_am(self):
        """AM_VOICE at 119.5 MHz is a normal aviation comm — should accept."""
        tag = tag_for("AM_VOICE", 119_500_000, self.plan)
        self.assertEqual(tag, "AVIATION_VOICE — AM_VOICE")

    def test_mtrtrs_p25_at_851_55(self):
        """MTRTRS control channel at 851.55 MHz — P25 in PS_800_NARROW band.
        Should accept."""
        tag = tag_for("P25", 851_550_000, self.plan)
        self.assertEqual(tag, "PS_800_NARROW — P25")

    def test_tacn_p25_at_769_456(self):
        """TACN control channel — P25 in PS_700_NARROW band."""
        tag = tag_for("P25", 769_456_250, self.plan)
        self.assertEqual(tag, "PS_700_NARROW — P25")

    def test_noaa_wx_fm_narrow(self):
        """NOAA weather radio 162.475 MHz — FM_NARROW expected."""
        tag = tag_for("FM_NARROW", 162_475_000, self.plan)
        self.assertEqual(tag, "NOAA_WX — FM_NARROW")

    def test_radiosonde_metaids(self):
        """Radiosonde at 403 MHz — GMSK is the typical modulation; should
        accept under METAIDS."""
        tag = tag_for("GMSK", 403_000_000, self.plan)
        self.assertEqual(tag, "METAIDS — GMSK")

    def test_radio_astronomy_rejects_everything(self):
        """608-614 MHz is protected radio astronomy — any signal is
        anomalous. RADIO_ASTRONOMY band has empty allowed_modes. Every
        ml_class produces the same reject tag (C6: ml_class no longer
        embedded in tag string; preserved in detections.modulation_class)."""
        for cls in ("FM_NARROW", "QAM", "P25", "NOISE"):
            tag = tag_for(cls, 611_000_000, self.plan)
            self.assertEqual(tag, "RADIO_ASTRONOMY — unidentified")

    def test_below_30mhz_is_permissive(self):
        """Out of the band-plan's covered range — should return class
        unmodified (permissive default)."""
        tag = tag_for("FM_NARROW", 7_200_000, self.plan)
        self.assertEqual(tag, "FM_NARROW")

    def test_no_overlapping_bands_in_real_plan(self):
        """The real us_band_plan.yaml is partitioned (no overlaps). Verify
        no frequency lands in more than one band."""
        for i, b in enumerate(self.plan):
            for j, other in enumerate(self.plan):
                if i == j:
                    continue
                self.assertFalse(
                    b.freq_min_hz < other.freq_max_hz
                    and other.freq_min_hz < b.freq_max_hz,
                    f"overlap: {b.name} ({b.freq_min_hz}-{b.freq_max_hz}) "
                    f"vs {other.name} ({other.freq_min_hz}-{other.freq_max_hz})",
                )

    def test_all_allowed_modes_in_v3_class_list(self):
        """Every allowed_modes value must be one of the 15 v3 classes."""
        v3 = {
            "NOISE", "FM_BROADCAST", "FM_NARROW", "AM_VOICE", "DMR", "GMSK",
            "QPSK", "OQPSK", "QAM", "OOK", "CELLULAR", "LTE", "P25", "NXDN",
            "POCSAG",
        }
        for b in self.plan:
            bad = set(b.allowed_modes) - v3
            self.assertFalse(
                bad,
                f"band {b.name} has non-v3 modes: {bad}",
            )


if __name__ == "__main__":
    unittest.main()
