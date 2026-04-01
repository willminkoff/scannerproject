from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SIDECAR_PATH = ROOT / "ui" / "static" / "hp3-react-app.mjs"


class Hp3SidecarLocationGuardrailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SIDECAR_PATH.read_text(encoding="utf-8")

    def test_sidecar_no_longer_renders_optional_location_checkbox(self):
        self.assertNotIn("Use location for scanning", self.text)
        self.assertNotIn('type:"checkbox",checked:L', self.text)

    def test_sidecar_save_path_forces_location_enabled(self):
        self.assertIn('let C={zip:i,use_location:!0};', self.text)
        self.assertIn('i&&(C.resolve_zip=!0)', self.text)
        self.assertNotIn('let C={zip:i,use_location:L};', self.text)

    def test_sidecar_auto_locate_still_persists_enabled_location(self):
        self.assertIn('await t({zip:C,use_location:!0,lat:g.lat,lon:g.lon})', self.text)


if __name__ == "__main__":
    unittest.main()
