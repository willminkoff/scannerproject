from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SIDECAR_PATH = ROOT / "ui" / "static" / "hp3-react-app.mjs"


class Hp3ServiceTypeSortGuardrailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SIDECAR_PATH.read_text(encoding="utf-8")

    def test_sidecar_sorts_service_types_alphabetically_with_custom_last(self):
        self.assertIn('b.startsWith("Custom ")', self.text)
        self.assertIn('l.startsWith("Custom ")', self.text)
        self.assertIn('let W=b.localeCompare(l);', self.text)
        self.assertIn('return _?1:-1;', self.text)


if __name__ == "__main__":
    unittest.main()
