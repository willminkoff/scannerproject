from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SB3_PATH = ROOT / "ui" / "sb3.html"


class Sb3HpFiltersGuardrailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SB3_PATH.read_text(encoding="utf-8")

    def test_embedded_sidecar_no_longer_renders_optional_location_checkbox(self):
        hp_filters_start = self.text.index('<div class="hp-filters-grid">')
        hp_filters_end = self.text.index('<div class="hp-filters-actions-main">', hp_filters_start)
        hp_filters_text = self.text[hp_filters_start:hp_filters_end]
        self.assertNotIn('id="hp-filter-use-location"', hp_filters_text)
        self.assertNotIn('Use Location Filter', hp_filters_text)
        self.assertIn('id="hp-filter-strict-location"', hp_filters_text)

    def test_embedded_sidecar_save_path_always_keeps_location_enabled(self):
        self.assertIn('use_location: true,', self.text)
        self.assertNotIn('use_location: Boolean(els.hpFilterUseLocation && els.hpFilterUseLocation.checked)', self.text)

    def test_embedded_sidecar_service_type_sort_keeps_custom_last(self):
        self.assertIn("const aIsCustom = a.name.startsWith('Custom ');", self.text)
        self.assertIn("const bIsCustom = b.name.startsWith('Custom ');", self.text)
        self.assertIn('if (aIsCustom !== bIsCustom) return aIsCustom ? 1 : -1;', self.text)


if __name__ == "__main__":
    unittest.main()
