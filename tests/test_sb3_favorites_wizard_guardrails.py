from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SB3_PATH = ROOT / "ui" / "sb3.html"


class Sb3FavoritesWizardGuardrailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SB3_PATH.read_text(encoding="utf-8")

    def test_loading_systems_does_not_auto_select_first_system(self):
        self.assertIn(
            "if (!builder.systems.some((row) => String(row.id || '') === String(builder.selectedSystemId || ''))) {\n            builder.selectedSystemId = '';\n          }",
            self.text,
        )
        self.assertNotIn(
            "builder.selectedSystemId = firstId;",
            self.text,
        )

    def test_loading_systems_does_not_auto_load_channels(self):
        self.assertNotIn("let autoLoadChannels = false;", self.text)
        self.assertNotIn("autoLoadChannels = Boolean(String(builder.selectedSystemId || '').trim());", self.text)
        self.assertNotIn("if (autoLoadChannels) {\n        await loadHpFavoriteChannels({ quiet: true });\n      }", self.text)


if __name__ == "__main__":
    unittest.main()
