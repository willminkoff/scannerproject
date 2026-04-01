from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SB3_PATH = ROOT / "ui" / "sb3.html"


class Sb3GmrsQuickScanGuardrailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SB3_PATH.read_text(encoding="utf-8")

    def test_gmrs_quick_scan_uses_auto_provisioned_bandscan_path(self):
        self.assertIn("key: 'gmrs_frs_murs'", self.text)
        self.assertIn("profileLabel: 'GMRS/FRS/MURS'", self.text)
        self.assertIn("modulation: 'nfm'", self.text)
        self.assertIn("bandwidth: 12500", self.text)
        self.assertIn("buildFrequencies: () => GMRS_FRS_MURS_BANDSCAN_FREQS_MHZ.slice()", self.text)
        self.assertIn("autoProvision: true", self.text)

    def test_gmrs_quick_scan_includes_murs_channels(self):
        self.assertIn("const MURS_CHANNEL_FREQS_MHZ = [", self.text)
        for freq in ("151.8200", "151.8800", "151.9400", "154.5700", "154.6000"):
            self.assertIn(freq, self.text)

    def test_gmrs_quick_scan_combines_known_gmrs_and_murs_frequencies(self):
        self.assertIn("const GMRS_FRS_MURS_BANDSCAN_FREQS_MHZ = Array.from(new Set([", self.text)
        self.assertIn("...Array.from(GMRS_CHANNEL_LABELS_BY_FREQ.keys()).map((freq) => Number(freq))", self.text)
        self.assertIn("...MURS_CHANNEL_FREQS_MHZ", self.text)
        self.assertIn("])).filter((freq) => Number.isFinite(freq))", self.text)
        self.assertIn(".sort((a, b) => a - b);", self.text)

    def test_gmrs_button_still_routes_through_bandscan_preset_handler(self):
        self.assertIn("async function applyGroundQuickGmrsFrsMurs()", self.text)
        self.assertIn("await applyBandScanPresetByKey('gmrs_frs_murs');", self.text)


if __name__ == "__main__":
    unittest.main()
