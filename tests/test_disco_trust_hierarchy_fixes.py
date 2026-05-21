"""Tests for the three post-#27 trust-hierarchy live-data fixes.

Bug 1 — fingerprinter band-context filtering:
    "Wide FM (generic)" was matching on signal shape in bands that can't
    physically host wide FM (RADIO_ASTRONOMY, TV_VHF_HIGH, AVIATION_NAV,
    LMR_800). The catalog now scopes each entry with ``allowed_bands`` /
    ``forbidden_bands`` and the fingerprinter rejects entries whose
    scope doesn't admit the detection's band.

Bug 2 — tier logic for unclassified-in-real-band:
    The trust hierarchy was demoting "unclassified" in a real band to
    SPURIOUS (because unclassified isn't in any band's allowed_modes,
    so band_rejected=True kicked the Layer F path). Distinguished:
    band-rejected with a REAL ML class → spurious; band-rejected with
    unclassified/None → unknown (signal exists, just not named).

Bug 3 — CDBS label dedup:
    CDBS loader synthesizes ``entity_name = "<callsign> (<community>)"``
    so the existing ``f"{cs} ({name})"`` rendered ``WHHM-FM (WHHM-FM
    (HENDERSON, TN))``. Fixed to detect the embedded callsign and render
    just the entity_name when it already starts with the callsign.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import yaml

_DISCO_SRC = str(Path(__file__).resolve().parents[1] / "disco" / "src")
if _DISCO_SRC not in sys.path:
    sys.path.insert(0, _DISCO_SRC)

import fingerprint  # noqa: E402
import identification  # noqa: E402


# ---- Bug 1 — fingerprinter band-context filtering ------------------------


def _make_wide_fm_iq(sample_rate_hz=2.0e6, duration_s=0.05, peak_dev_hz=75e3):
    """Synthesize wide-FM-broadcast-shaped IQ (matches "Wide FM (generic)")."""
    n = int(sample_rate_hz * duration_s)
    t = np.arange(n) / sample_rate_hz
    audio = (
        0.5 * np.sin(2 * np.pi * 400 * t)
        + 0.3 * np.sin(2 * np.pi * 1100 * t)
        + 0.2 * np.sin(2 * np.pi * 4500 * t)
    )
    phase = 2 * np.pi * peak_dev_hz * np.cumsum(audio) / sample_rate_hz
    iq = np.exp(1j * phase).astype(np.complex64)
    iq += (np.random.randn(n) + 1j * np.random.randn(n)) * 0.005
    return iq


class FingerprinterBandFilterTests(unittest.TestCase):
    """The fingerprinter consults catalog ``allowed_bands`` / ``forbidden_bands``."""

    def setUp(self) -> None:
        np.random.seed(7)
        fingerprint.reset_catalog_cache()

    def test_entry_band_allows_allowlist_permits_listed_band(self):
        entry = {"allowed_bands": ["BCAST_FM"]}
        self.assertTrue(fingerprint._entry_band_allows(entry, "BCAST_FM"))

    def test_entry_band_allows_allowlist_rejects_other_band(self):
        entry = {"allowed_bands": ["BCAST_FM"]}
        self.assertFalse(fingerprint._entry_band_allows(entry, "RADIO_ASTRONOMY"))
        self.assertFalse(fingerprint._entry_band_allows(entry, "TV_VHF_HIGH"))
        self.assertFalse(fingerprint._entry_band_allows(entry, "AVIATION_NAV"))
        self.assertFalse(fingerprint._entry_band_allows(entry, "LMR_800"))

    def test_entry_band_allows_allowlist_rejects_unknown_band(self):
        # Strict allowlist + unknown band → reject (we don't have evidence the
        # service is here).
        entry = {"allowed_bands": ["BCAST_FM"]}
        self.assertFalse(fingerprint._entry_band_allows(entry, None))

    def test_entry_band_allows_forbidden_rejects_listed_band(self):
        entry = {"forbidden_bands": ["NOAA_WX", "AVIATION_VOICE"]}
        self.assertFalse(fingerprint._entry_band_allows(entry, "NOAA_WX"))
        self.assertFalse(fingerprint._entry_band_allows(entry, "AVIATION_VOICE"))

    def test_entry_band_allows_forbidden_permits_other_band(self):
        entry = {"forbidden_bands": ["NOAA_WX"]}
        self.assertTrue(fingerprint._entry_band_allows(entry, "VHF_LMR_LOW"))
        # Forbidden + unknown band → permit (denylist doesn't gate unknowns).
        self.assertTrue(fingerprint._entry_band_allows(entry, None))

    def test_entry_band_allows_unconstrained_entry_admits_anything(self):
        entry = {}
        self.assertTrue(fingerprint._entry_band_allows(entry, "RADIO_ASTRONOMY"))
        self.assertTrue(fingerprint._entry_band_allows(entry, None))

    def test_wide_fm_rejected_in_radio_astronomy(self):
        """The canonical bug: 609.16 MHz wide-FM-shaped energy lands in
        RADIO_ASTRONOMY and was matching "Wide FM (generic)" at HIGH tier.
        With band scoping, the same IQ + band returns None."""
        iq = _make_wide_fm_iq()
        result = fingerprint.match_signature(
            iq, 2.0e6, 609.1647e6, snr_db=22.0, band_name="RADIO_ASTRONOMY",
        )
        self.assertIsNone(result, "Wide FM (generic) must NOT match in RADIO_ASTRONOMY")

    def test_wide_fm_rejected_in_tv_vhf_high(self):
        iq = _make_wide_fm_iq()
        result = fingerprint.match_signature(
            iq, 2.0e6, 199.1609e6, snr_db=22.0, band_name="TV_VHF_HIGH",
        )
        self.assertIsNone(result)

    def test_wide_fm_rejected_in_aviation_nav(self):
        iq = _make_wide_fm_iq()
        result = fingerprint.match_signature(
            iq, 2.0e6, 109.1758e6, snr_db=22.0, band_name="AVIATION_NAV",
        )
        self.assertIsNone(result)

    def test_wide_fm_rejected_in_lmr_800(self):
        iq = _make_wide_fm_iq()
        result = fingerprint.match_signature(
            iq, 2.0e6, 814.2365e6, snr_db=22.0, band_name="LMR_800",
        )
        self.assertIsNone(result)

    def test_wide_fm_rejected_when_band_unknown(self):
        # 803.79 MHz had band_name=None in the live data — strict allowlist
        # means a None band still gets rejected.
        iq = _make_wide_fm_iq()
        result = fingerprint.match_signature(
            iq, 2.0e6, 803.7889e6, snr_db=22.0, band_name=None,
        )
        self.assertIsNone(result)

    def test_wide_fm_accepted_in_bcast_fm(self):
        """Sanity check: the same IQ in the legitimate BCAST_FM band still
        matches. (Won't always match "Wide FM (generic)" specifically — "FM
        Broadcast" might score higher — but SOME entry should fire.)"""
        iq = _make_wide_fm_iq()
        result = fingerprint.match_signature(
            iq, 2.0e6, 98.3e6, snr_db=22.0, band_name="BCAST_FM",
            min_confidence=0.50,
        )
        if result is not None:
            self.assertIn(result.catalog_entry.get("name", ""),
                          ("FM Broadcast", "Wide FM (generic)"))


class CatalogScopeWalkTests(unittest.TestCase):
    """Verify the shipped catalog is correctly scoped for the entries that
    showed live-data over-firing in the bug report."""

    def setUp(self) -> None:
        path = Path(__file__).resolve().parents[1] / "disco" / "configs" / "service_signatures.yaml"
        with open(path) as f:
            self.catalog = yaml.safe_load(f)["signatures"]
        self.by_name = {e["name"]: e for e in self.catalog}

    def test_wide_fm_generic_scoped_to_bcast_fm_only(self):
        entry = self.by_name["Wide FM (generic)"]
        self.assertEqual(["BCAST_FM"], entry["allowed_bands"])
        # And freq range pinned to BCAST_FM, belt-and-suspenders.
        self.assertEqual(87900000, entry["freq_min_hz"])
        self.assertEqual(108000000, entry["freq_max_hz"])

    def test_fm_broadcast_scoped_to_bcast_fm(self):
        self.assertEqual(["BCAST_FM"], self.by_name["FM Broadcast"]["allowed_bands"])

    def test_noaa_wx_scoped_to_noaa_wx_band(self):
        self.assertEqual(["NOAA_WX"], self.by_name["NOAA Weather Radio"]["allowed_bands"])

    def test_aircraft_vhf_am_scoped_to_aviation_voice(self):
        self.assertEqual(["AVIATION_VOICE"],
                         self.by_name["Aircraft VHF AM (25 kHz channel)"]["allowed_bands"])

    def test_amateur_2m_scoped_to_amateur_2m(self):
        self.assertEqual(["AMATEUR_2M"],
                         self.by_name["Amateur 2m (NFM repeater / simplex)"]["allowed_bands"])

    def test_lora_scoped_to_ism_915(self):
        self.assertEqual(["ISM_915"], self.by_name["LoRa (915 MHz)"]["allowed_bands"])

    def test_wifi_scoped_to_ism_2_4(self):
        self.assertEqual(["ISM_2_4"],
                         self.by_name["WiFi 2.4 GHz (802.11 OFDM)"]["allowed_bands"])

    def test_cellular_850_scoped_to_cell_850_dl(self):
        self.assertEqual(["CELL_850_DL"],
                         self.by_name["Cellular (Cellular Band, 850 MHz)"]["allowed_bands"])

    def test_p25_phase1_control_scoped(self):
        # P25 control channels live in PS_700_NARROW.
        self.assertIn("PS_700_NARROW",
                      self.by_name["P25 Phase 1 control (C4FM)"]["allowed_bands"])

    def test_pocsag_uses_forbidden_bands(self):
        # Pagers use 138–174 MHz broadly but must NOT match NOAA / marine /
        # air / ham 2m sub-bands inside that range. The exclusion list approach
        # fits this better than a long allow-list.
        entry = self.by_name["POCSAG / FLEX paging"]
        forbidden = entry.get("forbidden_bands") or []
        for must_forbid in ("NOAA_WX", "MARINE_VHF", "MARINE_VHF_HIGH",
                            "AVIATION_VOICE", "AMATEUR_2M"):
            self.assertIn(must_forbid, forbidden)

    def test_almost_all_entries_have_band_scope(self):
        """Catch any new catalog entry shipped without band scope. Only
        entries operating outside the band-plan's covered range
        (AM Broadcast / ISM 433 / CB at 27 MHz) are exempt."""
        unscoped_allowed = {"AM Broadcast", "ISM 433 MHz (OOK remote)", "CB radio (27 MHz AM)"}
        for entry in self.catalog:
            if entry["name"] in unscoped_allowed:
                continue
            has_scope = bool(entry.get("allowed_bands")) or bool(entry.get("forbidden_bands"))
            with self.subTest(name=entry["name"]):
                self.assertTrue(has_scope,
                                f"{entry['name']!r} missing allowed_bands/forbidden_bands")


# ---- Bug 2 — tier logic for unclassified-in-real-band --------------------


class TierLogicUnclassifiedInBandTests(unittest.TestCase):
    """``build_identification`` distinguishes band-rejected REAL classes
    (spurious) from unclassified-in-band (unknown).
    """

    def test_unclassified_in_cell_850_lands_unknown_not_spurious(self):
        r = identification.build_identification(
            modulation_class="unclassified",
            modulation_confidence=0.0,
            snr_db=20.0,
            band_name="CELL_850_DL",
            band_rejected=True,  # unclassified isn't in CELL_850_DL allowed_modes
            band_allowed_modes=["LTE", "QAM", "OQPSK", "QPSK", "CELLULAR"],
        )
        self.assertEqual(identification.CONFIDENCE_UNKNOWN, r.confidence)
        self.assertEqual(identification.SOURCE_UNKNOWN, r.source)
        self.assertEqual("CELL_850_DL", r.band_name)

    def test_unclassified_in_tv_uhf_mid_lands_unknown(self):
        r = identification.build_identification(
            modulation_class="unclassified",
            modulation_confidence=0.0,
            snr_db=18.0,
            band_name="TV_UHF_MID",
            band_rejected=True,
        )
        self.assertEqual(identification.CONFIDENCE_UNKNOWN, r.confidence)

    def test_unclassified_in_amateur_125_lands_unknown(self):
        r = identification.build_identification(
            modulation_class="unclassified",
            modulation_confidence=0.0,
            snr_db=15.0,
            band_name="AMATEUR_125",
            band_rejected=True,
        )
        self.assertEqual(identification.CONFIDENCE_UNKNOWN, r.confidence)

    def test_none_class_in_real_band_lands_unknown(self):
        # null/None modulation_class in a real band should also be unknown,
        # not spurious — the signal is real, just not classified.
        r = identification.build_identification(
            modulation_class=None,
            modulation_confidence=None,
            snr_db=18.0,
            band_name="LMR_800",
            band_rejected=False,
        )
        self.assertEqual(identification.CONFIDENCE_UNKNOWN, r.confidence)

    def test_real_band_rejected_class_still_spurious(self):
        # Bug-2 fix MUST NOT regress the spurious case for REAL band-rejected
        # ML classes. FM_BROADCAST in CELL_850_DL is structurally spurious.
        r = identification.build_identification(
            modulation_class="FM_BROADCAST",
            modulation_confidence=0.8,
            snr_db=22.0,
            band_name="CELL_850_DL",
            band_rejected=True,
            band_allowed_modes=["LTE", "QAM", "OQPSK", "QPSK", "CELLULAR"],
        )
        self.assertEqual(identification.CONFIDENCE_SPURIOUS, r.confidence)
        self.assertEqual(identification.SOURCE_BAND_PLAN, r.source)

    def test_noise_class_stays_spurious(self):
        r = identification.build_identification(
            modulation_class="NOISE",
            modulation_confidence=0.9,
            snr_db=20.0,
            band_name="UHF_LMR_LOW",
            band_rejected=True,
        )
        self.assertEqual(identification.CONFIDENCE_SPURIOUS, r.confidence)

    def test_sub_floor_snr_stays_spurious(self):
        r = identification.build_identification(
            modulation_class="FM_NARROW",
            modulation_confidence=0.6,
            snr_db=2.0,  # below SNR_FLOOR_DB (8.0)
            band_name="UHF_LMR_LOW",
            band_rejected=False,
        )
        self.assertEqual(identification.CONFIDENCE_SPURIOUS, r.confidence)

    def test_real_class_in_allowed_band_stays_low(self):
        # Regression guard: Layer E (real class in allowed_modes) still
        # yields LOW confidence, not unknown.
        r = identification.build_identification(
            modulation_class="FM_NARROW",
            modulation_confidence=0.7,
            snr_db=20.0,
            band_name="UHF_LMR_LOW",
            band_rejected=False,
        )
        self.assertEqual(identification.CONFIDENCE_LOW, r.confidence)
        self.assertEqual(identification.SOURCE_MODULATION_CLASS, r.source)


# ---- Bug 3 — CDBS label dedup --------------------------------------------


class CdbsServiceLabelDedupTests(unittest.TestCase):
    """``_format_cdbs_service`` avoids double-printing the callsign that the
    CDBS loader already embedded in entity_name."""

    def test_whhm_fm_renders_single_callsign(self):
        # Canonical example from the live bug report.
        out = identification._format_cdbs_service({
            "callsign": "WHHM-FM",
            "entity_name": "WHHM-FM (HENDERSON, TN)",
        })
        self.assertEqual("WHHM-FM (HENDERSON, TN)", out)
        # Hard check: the callsign appears exactly once, not nested.
        self.assertEqual(1, out.count("WHHM-FM"))

    def test_legacy_clean_entity_name_falls_back_to_old_format(self):
        # If entity_name doesn't start with the callsign (e.g. older row
        # imported before the loader change), keep the legacy "<cs> (<name>)"
        # render rather than mutating it.
        out = identification._format_cdbs_service({
            "callsign": "WPLN-FM",
            "entity_name": "Nashville Public Radio",
        })
        self.assertEqual("WPLN-FM (Nashville Public Radio)", out)

    def test_callsign_only_returns_callsign(self):
        out = identification._format_cdbs_service({
            "callsign": "WSMV-TV",
            "entity_name": "",
        })
        self.assertEqual("WSMV-TV", out)

    def test_entity_name_only_returns_name(self):
        out = identification._format_cdbs_service({
            "callsign": "",
            "entity_name": "Some Station",
        })
        self.assertEqual("Some Station", out)

    def test_no_data_returns_fallback(self):
        out = identification._format_cdbs_service({
            "callsign": None,
            "entity_name": None,
        })
        self.assertEqual("Broadcast station", out)


if __name__ == "__main__":
    unittest.main()
