"""Tests for the trust-hierarchy IdentificationResult + build_identification().

The contract this file enforces:
  - First non-empty layer in the fall-through wins.
  - HPDB → high. CDBS → high. ULS amateur → medium. ULS land-mobile → medium.
  - Band-rejected ML class → spurious. NOISE class → spurious. Sub-floor SNR
    → spurious. (No service name set in any spurious case.)
  - ML class in band's allowed_modes → low (supporting info only).
  - Empty inputs → unknown.
  - should_invoke_claude only true for high/medium with curated DB source.
  - is_displayed_by_default false only for spurious tier.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_DISCO_SRC = str(Path(__file__).resolve().parents[1] / "disco" / "src")
if _DISCO_SRC not in sys.path:
    sys.path.insert(0, _DISCO_SRC)

import identification  # noqa: E402
from identification import (
    IdentificationResult,
    build_identification,
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_LOW,
    CONFIDENCE_UNKNOWN,
    CONFIDENCE_SPURIOUS,
    SOURCE_HPDB,
    SOURCE_CDBS,
    SOURCE_ULS,
    SOURCE_SIGNATURE,
    SOURCE_BAND_PLAN,
    SOURCE_MODULATION_CLASS,
    SOURCE_UNKNOWN,
)


def _evidence_base():
    return dict(
        modulation_class="P25",
        modulation_confidence=0.91,
        snr_db=22.0,
        band_rejected=False,
        band_allowed_modes=["P25", "DMR"],
    )


class IdentificationResultDataclassTests(unittest.TestCase):
    def test_invalid_confidence_raises(self):
        with self.assertRaises(ValueError):
            IdentificationResult(
                service=None, confidence="bogus", source=SOURCE_UNKNOWN
            )

    def test_invalid_source_raises(self):
        with self.assertRaises(ValueError):
            IdentificationResult(
                service=None, confidence=CONFIDENCE_HIGH, source="bogus"
            )

    def test_to_dict_includes_all_fields(self):
        r = IdentificationResult(
            service="WSMV-TV",
            confidence=CONFIDENCE_HIGH,
            source=SOURCE_CDBS,
            band_name="TV_VHF_LOW",
            evidence={"k": "v"},
        )
        d = r.to_dict()
        self.assertEqual("WSMV-TV", d["service"])
        self.assertEqual("high", d["confidence"])
        self.assertEqual("cdbs", d["source"])
        self.assertEqual("TV_VHF_LOW", d["band_name"])
        self.assertEqual({"k": "v"}, d["evidence"])

    def test_claude_gate_only_high_medium_curated(self):
        # Yes — high + curated DB
        for src in (SOURCE_HPDB, SOURCE_CDBS, SOURCE_SIGNATURE):
            r = IdentificationResult(service="x", confidence=CONFIDENCE_HIGH, source=src)
            self.assertTrue(r.should_invoke_claude, f"{src} should pass gate")
        # Yes — medium + curated DB
        for src in (SOURCE_HPDB, SOURCE_CDBS, SOURCE_SIGNATURE):
            r = IdentificationResult(service="x", confidence=CONFIDENCE_MEDIUM, source=src)
            self.assertTrue(r.should_invoke_claude)
        # No — medium ULS (licensee, not service)
        r = IdentificationResult(service="x", confidence=CONFIDENCE_MEDIUM, source=SOURCE_ULS)
        self.assertFalse(r.should_invoke_claude)
        # No — low / unknown / spurious never
        for conf in (CONFIDENCE_LOW, CONFIDENCE_UNKNOWN, CONFIDENCE_SPURIOUS):
            r = IdentificationResult(service=None, confidence=conf, source=SOURCE_BAND_PLAN)
            self.assertFalse(r.should_invoke_claude)

    def test_default_display_hides_spurious_only(self):
        for conf in (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW, CONFIDENCE_UNKNOWN):
            r = IdentificationResult(service=None, confidence=conf, source=SOURCE_UNKNOWN)
            self.assertTrue(r.is_displayed_by_default)
        r = IdentificationResult(
            service=None, confidence=CONFIDENCE_SPURIOUS, source=SOURCE_BAND_PLAN
        )
        self.assertFalse(r.is_displayed_by_default)


class BuildIdentificationFallThroughTests(unittest.TestCase):
    """Walk the layer fall-through end-to-end."""

    # ---- Layer A — HPDB --------------------------------------------------

    def test_hpdb_trunk_wins_high(self):
        r = build_identification(
            **_evidence_base(),
            band_name="PS_700_NB",
            hpdb_match={
                "source_table": "trunk_control",
                "alpha_tag": "TACN — West Nashville",
                "system_name": "TACN",
                "distance_km": 5.0,
            },
        )
        self.assertEqual(CONFIDENCE_HIGH, r.confidence)
        self.assertEqual(SOURCE_HPDB, r.source)
        self.assertEqual("TACN — West Nashville", r.service)
        self.assertEqual("PS_700_NB", r.band_name)
        self.assertIn("hpdb_match", r.evidence)

    def test_hpdb_conventional_uses_group_plus_alpha(self):
        r = build_identification(
            **_evidence_base(),
            hpdb_match={
                "source_table": "conventional",
                "alpha_tag": "Tower",
                "group_name": "Nashville International — Aircraft",
            },
        )
        self.assertEqual(CONFIDENCE_HIGH, r.confidence)
        self.assertIn("Tower", r.service)
        self.assertIn("Nashville International", r.service)

    def test_hpdb_wins_even_when_uls_also_present(self):
        # HPDB layer should win over ULS when both are non-empty.
        r = build_identification(
            **_evidence_base(),
            hpdb_match={"source_table": "conventional", "alpha_tag": "Police Dispatch"},
            uls_match={"callsign": "WPXX123", "entity_name": "CITY OF NOWHERE"},
        )
        self.assertEqual(SOURCE_HPDB, r.source)
        self.assertEqual("Police Dispatch", r.service)

    # ---- Layer B — CDBS --------------------------------------------------

    def test_cdbs_wins_high_when_no_hpdb(self):
        r = build_identification(
            **{**_evidence_base(), "modulation_class": "FM_BROADCAST"},
            band_name="FM_BROADCAST",
            cdbs_match={"callsign": "WPLN-FM", "entity_name": "Nashville Public Radio"},
        )
        self.assertEqual(CONFIDENCE_HIGH, r.confidence)
        self.assertEqual(SOURCE_CDBS, r.source)
        self.assertIn("WPLN-FM", r.service)

    # ---- Layer C / D — ULS amateur vs land-mobile ------------------------

    def test_uls_amateur_marked_medium_with_amateur_label(self):
        r = build_identification(
            **{**_evidence_base(), "modulation_class": "FM_NARROW"},
            band_name="AMATEUR_70CM",
            uls_match={
                "callsign": "N4ABC",
                "entity_name": "John Doe",
                "source": "amateur (70cm)",
            },
        )
        self.assertEqual(CONFIDENCE_MEDIUM, r.confidence)
        self.assertEqual(SOURCE_ULS, r.source)
        self.assertTrue(r.service.startswith("Amateur"))

    def test_uls_land_mobile_marked_medium_with_licensee(self):
        r = build_identification(
            **{**_evidence_base(), "modulation_class": "FM_NARROW"},
            band_name="BUSINESS_460",
            uls_match={
                "callsign": "WPXX123",
                "entity_name": "CITY OF FRANKLIN",
                "source": "ULS-LM",
            },
        )
        self.assertEqual(CONFIDENCE_MEDIUM, r.confidence)
        self.assertEqual(SOURCE_ULS, r.source)
        self.assertIn("CITY OF FRANKLIN", r.service)

    # ---- Layer F — band-rejected ML class --------------------------------

    def test_band_rejected_yields_spurious_without_service(self):
        r = build_identification(
            **{
                **_evidence_base(),
                "modulation_class": "AM_VOICE",
                "band_rejected": True,
                "band_allowed_modes": ["QAM", "FM_NARROW"],
            },
            band_name="TV_VHF_HIGH",
        )
        self.assertEqual(CONFIDENCE_SPURIOUS, r.confidence)
        self.assertEqual(SOURCE_BAND_PLAN, r.source)
        self.assertIsNone(r.service)
        self.assertEqual("TV_VHF_HIGH", r.band_name)

    # ---- Layer G — NOISE / sub-floor SNR ---------------------------------

    def test_noise_class_yields_spurious(self):
        r = build_identification(
            **{**_evidence_base(), "modulation_class": "NOISE", "modulation_confidence": 0.4}
        )
        self.assertEqual(CONFIDENCE_SPURIOUS, r.confidence)
        self.assertEqual(SOURCE_MODULATION_CLASS, r.source)
        self.assertIsNone(r.service)

    def test_sub_floor_snr_yields_spurious(self):
        r = build_identification(
            **{**_evidence_base(), "snr_db": 4.0}
        )
        self.assertEqual(CONFIDENCE_SPURIOUS, r.confidence)
        self.assertEqual(SOURCE_MODULATION_CLASS, r.source)

    # ---- Layer E — ML class allowed, low --------------------------------

    def test_ml_class_only_yields_low(self):
        r = build_identification(**_evidence_base(), band_name="GMRS")
        self.assertEqual(CONFIDENCE_LOW, r.confidence)
        self.assertEqual(SOURCE_MODULATION_CLASS, r.source)
        self.assertIsNone(r.service)

    # ---- Empty input → unknown -------------------------------------------

    def test_no_inputs_yields_unknown(self):
        r = build_identification(
            modulation_class=None,
            modulation_confidence=None,
            snr_db=None,
        )
        self.assertEqual(CONFIDENCE_UNKNOWN, r.confidence)
        self.assertEqual(SOURCE_UNKNOWN, r.source)
        self.assertIsNone(r.service)

    def test_unclassified_ml_yields_unknown(self):
        r = build_identification(
            modulation_class="unclassified",
            modulation_confidence=0.3,
            snr_db=15.0,
        )
        self.assertEqual(CONFIDENCE_UNKNOWN, r.confidence)


class EvidenceFidelityTests(unittest.TestCase):
    """The evidence dict always carries the inputs Claude / details panel need."""

    def test_evidence_includes_modulation_class_snr_band_rejected(self):
        r = build_identification(**_evidence_base())
        self.assertEqual("P25", r.evidence["modulation_class"])
        self.assertAlmostEqual(22.0, r.evidence["snr_db"])
        self.assertFalse(r.evidence["band_rejected"])
        self.assertEqual(["P25", "DMR"], r.evidence["band_allowed_modes"])

    def test_hpdb_match_preserved_in_evidence(self):
        m = {"source_table": "conventional", "alpha_tag": "x", "distance_km": 3.0}
        r = build_identification(**_evidence_base(), hpdb_match=m)
        # Stored as a copy so caller mutations don't affect the result.
        self.assertEqual(m, r.evidence["hpdb_match"])
        self.assertIsNot(m, r.evidence["hpdb_match"])


if __name__ == "__main__":
    unittest.main()
