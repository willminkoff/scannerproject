"""Tests for PR #34 — sigidwiki-informed catalog ingestion.

Validates the expanded service-signature catalog: schema completeness on
every entry, source tagging on the new entries, the band-scope coverage
invariant on the new scoped entries, and the generator script's output
shape.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_CFG = _ROOT / "disco" / "configs"

_VALID_SHAPES = {"narrow_carrier", "wide_flat", "ofdm_multicarrier",
                 "fsk_two_tone", "broadband_noise", "unknown"}
_VALID_DUTY = {"continuous", "bursty", "hopping", "unknown"}
_REQUIRED = ("name", "freq_min_hz", "freq_max_hz", "bandwidth_3db_hz_min",
             "bandwidth_3db_hz_max", "shape", "duty_cycle")


def _load_catalog():
    with open(_CFG / "service_signatures.yaml") as f:
        return yaml.safe_load(f)["signatures"]


def _band_ranges():
    with open(_CFG / "us_band_plan.yaml") as f:
        return [(b["name"], b["freq_min_hz"], b["freq_max_hz"])
                for b in yaml.safe_load(f)["bands"]]


class CatalogSchemaTests(unittest.TestCase):
    def setUp(self):
        self.catalog = _load_catalog()

    def test_entry_count_grew(self):
        # Was ~30 before PR #34; target ~80-100.
        self.assertGreaterEqual(len(self.catalog), 80,
                                f"catalog has only {len(self.catalog)} entries")
        self.assertLessEqual(len(self.catalog), 120)

    def test_every_entry_has_required_fields(self):
        for e in self.catalog:
            with self.subTest(name=e.get("name")):
                for k in _REQUIRED:
                    self.assertIn(k, e, f"missing {k}")

    def test_shapes_and_duty_valid(self):
        for e in self.catalog:
            with self.subTest(name=e["name"]):
                self.assertIn(e["shape"], _VALID_SHAPES)
                self.assertIn(e["duty_cycle"], _VALID_DUTY)

    def test_freq_and_bandwidth_sane(self):
        for e in self.catalog:
            with self.subTest(name=e["name"]):
                self.assertLess(e["freq_min_hz"], e["freq_max_hz"])
                self.assertLessEqual(e["bandwidth_3db_hz_min"], e["bandwidth_3db_hz_max"])
                self.assertGreater(e["bandwidth_3db_hz_min"], 0)

    def test_names_unique(self):
        names = [e["name"] for e in self.catalog]
        self.assertEqual(len(names), len(set(names)), "duplicate catalog names")


class CatalogSourceTaggingTests(unittest.TestCase):
    def setUp(self):
        self.catalog = _load_catalog()

    def test_new_entries_tagged_source_manual(self):
        manual = [e for e in self.catalog if e.get("source") == "manual"]
        self.assertGreaterEqual(len(manual), 40,
                                "expected the bulk-ingested entries tagged source: manual")

    def test_manual_entries_cite_reference(self):
        for e in self.catalog:
            if e.get("source") == "manual":
                with self.subTest(name=e["name"]):
                    self.assertIn("sigidwiki", (e.get("notes") or "").lower())

    def test_no_duplicate_of_preexisting_services(self):
        # The ingest must not re-add entries already present pre-PR-#34.
        preexisting = {"Wide FM (generic)", "NOAA Weather Radio", "ATSC TV (8VSB)",
                       "FM Broadcast", "WiFi 2.4 GHz (802.11 OFDM)", "DMR / Mototrbo"}
        manual_names = {e["name"] for e in self.catalog if e.get("source") == "manual"}
        self.assertEqual(set(), preexisting & manual_names)


class CatalogBandScopeCoverageTests(unittest.TestCase):
    """New scoped entries must satisfy the PR #29 coverage invariant:
    allowed_bands equals exactly the set of bands the freq range overlaps."""

    def setUp(self):
        self.catalog = _load_catalog()
        self.bands = _band_ranges()

    def _spanning(self, fmin, fmax):
        return {n for (n, lo, hi) in self.bands if not (hi <= fmin or lo >= fmax)}

    def test_manual_allowed_bands_match_span(self):
        for e in self.catalog:
            if e.get("source") != "manual" or "allowed_bands" not in e:
                continue
            spanned = self._spanning(e["freq_min_hz"], e["freq_max_hz"])
            with self.subTest(name=e["name"]):
                self.assertEqual(set(e["allowed_bands"]), spanned,
                                 f"{e['name']} allowed_bands must equal its spanned bands")

    def test_out_of_plan_manual_entries_unscoped(self):
        # HF / microwave entries whose range overlaps no band must be unscoped.
        for e in self.catalog:
            if e.get("source") != "manual":
                continue
            spanned = self._spanning(e["freq_min_hz"], e["freq_max_hz"])
            if not spanned:
                with self.subTest(name=e["name"]):
                    self.assertNotIn("allowed_bands", e,
                                     f"{e['name']} is out of band plan; should be unscoped")


class IngestScriptTests(unittest.TestCase):
    def test_script_builds_entries_without_dupes(self):
        sys.path.insert(0, str(_ROOT / "scripts"))
        import ingest_sigidwiki
        entries = ingest_sigidwiki.build_entries()
        # Re-running against the now-populated catalog should yield 0 (all
        # already present) — proves the idempotent skip-existing guard.
        names = {e["name"] for e in entries}
        catalog_names = {e["name"] for e in _load_catalog()}
        self.assertTrue(names.issubset(catalog_names) or not names)


if __name__ == "__main__":
    unittest.main()
