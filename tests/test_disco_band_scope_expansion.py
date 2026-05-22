"""Tests for PR #29 — fingerprinter band-scope expansion.

PR #28 added per-entry ``allowed_bands`` to fix "Wide FM (generic)" firing
in bands that can't host wide FM. But several entries were scoped too
narrowly, causing the signature layer to under-fire. The worst case: LTE
Band 13 (746–756 MHz) was scoped to LTE_UPPER_700/LTE_D_700 (775–799 MHz),
which its frequency range never reaches — so it could *never* match.

This suite locks down:
  - the corrected per-entry band scopes (data assertions),
  - a coverage invariant: every band a signature's freq range overlaps is
    either in allowed_bands or in a documented intentional-exclusion set
    (this is the guard that prevents re-introducing the regression),
  - the Bug-1 guard from PR #28 still holds (Wide FM only in BCAST_FM),
  - end-to-end: a previously-unreachable scope now admits its band.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import yaml

_DISCO_SRC = str(Path(__file__).resolve().parents[1] / "disco" / "src")
if _DISCO_SRC not in sys.path:
    sys.path.insert(0, _DISCO_SRC)

import fingerprint  # noqa: E402

_CONFIG_DIR = Path(__file__).resolve().parents[1] / "disco" / "configs"


def _load_catalog():
    with open(_CONFIG_DIR / "service_signatures.yaml") as f:
        return yaml.safe_load(f)["signatures"]


def _load_band_ranges():
    with open(_CONFIG_DIR / "us_band_plan.yaml") as f:
        bands = yaml.safe_load(f)["bands"]
    return [(b["name"], b["freq_min_hz"], b["freq_max_hz"]) for b in bands]


# Bands a signature's freq range physically overlaps but where the service
# is NOT permitted (curated exclusions). The coverage-invariant test allows
# these to be absent from allowed_bands; everything else must be present.
INTENTIONAL_EXCLUSIONS = {
    "Wide FM (generic)": {"TV_VHF_LOW"},
    "FM Broadcast": {"TV_VHF_LOW"},
    "NOAA Weather Radio": {"GOV_NOAA_PRE", "GOV_VHF_HIGH"},
    "P25 Phase 1 control (C4FM)": {"LTE_UPPER_700"},
    "Public Safety (700 MHz narrowband)": {"PS_700_BROADBAND", "LTE_UPPER_700"},
    "ATSC TV (8VSB)": {
        "AMTS", "AMATEUR_125", "MIL_UHF_AVIATION", "AVIATION_GLIDESLOPE",
        "MIL_UHF", "NAV_SAT_UPLINK", "METSAT_400", "METAIDS", "EPIRB_406",
        "GOV_UHF_LOW", "AMATEUR_70CM", "UHF_LMR_LOW", "UHF_LMR_454",
        "UHF_LMR_HIGH", "RADIO_ASTRONOMY",
    },
    "Wireless microphone (UHF NFM)": {"RADIO_ASTRONOMY"},
    "NXDN (6.25 kHz)": {
        "MARINE_VHF", "MARINE_VHF_HIGH", "GOV_NOAA_PRE", "NOAA_WX",
        "TV_VHF_HIGH", "AMTS", "MIL_UHF_AVIATION", "AVIATION_GLIDESLOPE",
        "MIL_UHF", "NAV_SAT_UPLINK", "METSAT_400", "METAIDS", "EPIRB_406",
    },
    "DMR / Mototrbo": {
        "MARINE_VHF", "MARINE_VHF_HIGH", "GOV_NOAA_PRE", "NOAA_WX",
        "TV_VHF_HIGH", "AMTS", "MIL_UHF_AVIATION", "AVIATION_GLIDESLOPE",
        "MIL_UHF", "NAV_SAT_UPLINK", "METSAT_400", "METAIDS", "EPIRB_406",
    },
}


class BandScopeCoverageInvariantTests(unittest.TestCase):
    """The regression guard: an allowlisted entry must list every band its
    freq range overlaps, except the documented exclusions. A miss here means
    detections in the unlisted band are silently rejected (the PR #28 bug).
    """

    def setUp(self):
        self.catalog = _load_catalog()
        self.bands = _load_band_ranges()

    def _spanning(self, fmin, fmax):
        return {n for (n, lo, hi) in self.bands if not (hi <= fmin or lo >= fmax)}

    def test_every_allowlisted_band_is_reachable(self):
        # No entry should list a band its freq range never touches (dead refs).
        for e in self.catalog:
            allowed = e.get("allowed_bands")
            if not allowed:
                continue
            spanned = self._spanning(e["freq_min_hz"], e["freq_max_hz"])
            dead = set(allowed) - spanned
            with self.subTest(name=e["name"]):
                self.assertFalse(dead, f"{e['name']} lists unreachable bands: {sorted(dead)}")

    def test_no_unscoped_gap_in_freq_range(self):
        for e in self.catalog:
            allowed = e.get("allowed_bands")
            if not allowed:
                continue
            spanned = self._spanning(e["freq_min_hz"], e["freq_max_hz"])
            gap = spanned - set(allowed) - INTENTIONAL_EXCLUSIONS.get(e["name"], set())
            with self.subTest(name=e["name"]):
                self.assertFalse(
                    gap,
                    f"{e['name']} freq range overlaps {sorted(gap)} which is "
                    f"neither allowed nor an intentional exclusion — detections "
                    f"there would be silently rejected",
                )


class CorrectedScopeDataTests(unittest.TestCase):
    """Per-entry assertions for the scopes this PR corrected."""

    def setUp(self):
        self.by_name = {e["name"]: e for e in _load_catalog()}

    def test_lte_band_13_now_reachable(self):
        # The headline regression: 746–756 MHz resolves to LTE_LOWER_700, not
        # the LTE_UPPER_700/LTE_D_700 it was scoped to.
        e = self.by_name["LTE Band 13 (Verizon 700 MHz)"]
        self.assertEqual(["LTE_LOWER_700"], e["allowed_bands"])

    def test_lte_band_12_17_scoped_to_lower_700(self):
        e = self.by_name["LTE Band 12/17 (downlink)"]
        self.assertIn("LTE_LOWER_700", e["allowed_bands"])

    def test_marine_vhf_includes_interleaved_lmr_bands(self):
        e = self.by_name["Marine VHF"]
        for b in ("MARINE_VHF", "VHF_LMR_MID", "MARINE_VHF_HIGH", "VHF_LMR_LAST"):
            self.assertIn(b, e["allowed_bands"])

    def test_wireless_mic_includes_tv_uhf_high(self):
        e = self.by_name["Wireless microphone (UHF NFM)"]
        self.assertIn("TV_UHF_HIGH", e["allowed_bands"])
        self.assertNotIn("RADIO_ASTRONOMY", e["allowed_bands"])

    def test_railroad_includes_marine_vhf_high(self):
        e = self.by_name["Railroad (VHF NFM)"]
        self.assertIn("MARINE_VHF_HIGH", e["allowed_bands"])

    def test_nxdn_expanded_to_full_lmr_set(self):
        e = self.by_name["NXDN (6.25 kHz)"]
        for b in ("GOV_VHF_148", "VHF_LMR_LOW", "GOV_VHF_HIGH", "AMATEUR_125",
                  "GOV_UHF_LOW", "UHF_LMR_HIGH"):
            self.assertIn(b, e["allowed_bands"])

    def test_dmr_now_spans_vhf_and_uhf_lmr(self):
        e = self.by_name["DMR / Mototrbo"]
        self.assertEqual(144000000, e["freq_min_hz"])
        for b in ("VHF_LMR_LOW", "AMATEUR_2M", "UHF_LMR_HIGH"):
            self.assertIn(b, e["allowed_bands"])

    def test_ism_433_scoped_to_amateur_70cm(self):
        e = self.by_name["ISM 433 MHz (OOK remote)"]
        self.assertEqual(["AMATEUR_70CM"], e["allowed_bands"])

    def test_wide_fm_still_bcast_fm_only(self):
        # Bug-1 regression guard from PR #28 — must NOT be widened.
        e = self.by_name["Wide FM (generic)"]
        self.assertEqual(["BCAST_FM"], e["allowed_bands"])


class EntryBandAllowsCorrectedScopeTests(unittest.TestCase):
    """``_entry_band_allows`` evaluated against the corrected catalog."""

    def setUp(self):
        self.by_name = {e["name"]: e for e in _load_catalog()}

    def test_lte_band_13_admits_lower_700(self):
        e = self.by_name["LTE Band 13 (Verizon 700 MHz)"]
        self.assertTrue(fingerprint._entry_band_allows(e, "LTE_LOWER_700"))
        # And still rejects the band it was wrongly scoped to before.
        self.assertFalse(fingerprint._entry_band_allows(e, "LTE_UPPER_700"))

    def test_marine_admits_vhf_lmr_mid(self):
        e = self.by_name["Marine VHF"]
        self.assertTrue(fingerprint._entry_band_allows(e, "VHF_LMR_MID"))
        self.assertTrue(fingerprint._entry_band_allows(e, "MARINE_VHF_HIGH"))

    def test_wireless_mic_admits_tv_uhf_high(self):
        e = self.by_name["Wireless microphone (UHF NFM)"]
        self.assertTrue(fingerprint._entry_band_allows(e, "TV_UHF_HIGH"))
        self.assertFalse(fingerprint._entry_band_allows(e, "RADIO_ASTRONOMY"))

    def test_nxdn_admits_gov_and_uhf_lmr(self):
        e = self.by_name["NXDN (6.25 kHz)"]
        self.assertTrue(fingerprint._entry_band_allows(e, "GOV_VHF_HIGH"))
        self.assertTrue(fingerprint._entry_band_allows(e, "UHF_LMR_454"))
        # NXDN does NOT operate in marine / TV — still rejected.
        self.assertFalse(fingerprint._entry_band_allows(e, "MARINE_VHF"))
        self.assertFalse(fingerprint._entry_band_allows(e, "TV_VHF_HIGH"))

    def test_wide_fm_still_rejected_outside_bcast_fm(self):
        e = self.by_name["Wide FM (generic)"]
        self.assertTrue(fingerprint._entry_band_allows(e, "BCAST_FM"))
        for bad in ("RADIO_ASTRONOMY", "TV_VHF_HIGH", "AVIATION_NAV",
                    "LMR_800", "TV_UHF_MID", None):
            self.assertFalse(fingerprint._entry_band_allows(e, bad))


def _wide_fm_iq(sample_rate_hz=2.0e6, duration_s=0.05, peak_dev_hz=75e3):
    n = int(sample_rate_hz * duration_s)
    t = np.arange(n) / sample_rate_hz
    audio = (0.5 * np.sin(2 * np.pi * 400 * t)
             + 0.3 * np.sin(2 * np.pi * 1100 * t)
             + 0.2 * np.sin(2 * np.pi * 4500 * t))
    phase = 2 * np.pi * peak_dev_hz * np.cumsum(audio) / sample_rate_hz
    iq = np.exp(1j * phase).astype(np.complex64)
    iq += (np.random.randn(n) + 1j * np.random.randn(n)) * 0.005
    return iq


class EndToEndMatchTests(unittest.TestCase):
    """match_signature against the shipped catalog with band context."""

    def setUp(self):
        np.random.seed(11)
        fingerprint.reset_catalog_cache()

    def test_wide_fm_matches_in_bcast_fm(self):
        # Positive: legitimate FM broadcast still resolves.
        iq = _wide_fm_iq()
        result = fingerprint.match_signature(
            iq, 2.0e6, 98.5e6, snr_db=25.0, band_name="BCAST_FM",
            min_confidence=0.50,
        )
        self.assertIsNotNone(result)
        self.assertIn(result.catalog_entry.get("name"),
                      ("FM Broadcast", "Wide FM (generic)"))

    def test_wide_fm_still_rejected_in_radio_astronomy(self):
        # Bug-1 guard end-to-end: even with a perfect wide-FM shape, the
        # band veto blocks it outside BCAST_FM. (609 MHz is out of the
        # entry's freq range now too, but the veto is the belt.)
        iq = _wide_fm_iq()
        result = fingerprint.match_signature(
            iq, 2.0e6, 98.5e6, snr_db=25.0, band_name="RADIO_ASTRONOMY",
            min_confidence=0.50,
        )
        # In RADIO_ASTRONOMY no catalog entry admits a wide-FM shape → None.
        if result is not None:
            self.assertNotEqual("Wide FM (generic)", result.catalog_entry.get("name"))


if __name__ == "__main__":
    unittest.main()
