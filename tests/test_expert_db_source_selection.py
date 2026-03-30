from pathlib import Path
import unittest

from ui import scan_mode_controller


REPO_ROOT = Path(__file__).resolve().parents[1]
SB3_HTML_PATH = REPO_ROOT / "ui" / "sb3.html"
HP3_REACT_APP_PATH = REPO_ROOT / "ui" / "static" / "hp3-react-app.mjs"


class ExpertDbSourceSelectionTests(unittest.TestCase):
    def test_scan_mode_controller_reports_expert_only_error(self):
        controller = scan_mode_controller.ScanModeController(db_path="/tmp/hpdb-test.db")
        with self.assertRaisesRegex(ValueError, "mode must be expert"):
            controller.set_mode("invalid-mode")

    def test_sb3_favorites_source_selection_rolls_back_on_failed_save(self):
        text = SB3_HTML_PATH.read_text(encoding="utf-8")
        self.assertIn("function captureHpFavoritesBuilderSelectionState()", text)
        self.assertIn("function restoreHpFavoritesBuilderSelectionState(snapshot)", text)
        self.assertIn("const persistedMode = String(hpState && hpState.mode || '')", text)
        self.assertGreaterEqual(text.count("restoreHpFavoritesBuilderSelectionState(previousState);"), 4)

    def test_hp3_react_app_no_longer_offers_hp_vs_sb3_mode_toggle(self):
        text = HP3_REACT_APP_PATH.read_text(encoding="utf-8")
        self.assertIn('message:"SB3 mode is always active. Use Favorites or Full Database to choose the DB source."', text)
        self.assertIn('title:"Scan Source"', text)
        self.assertIn('mode:"expert"', text)
        self.assertNotIn("HP3 Mode", text)
        self.assertNotIn("SB3 Mode", text)


if __name__ == "__main__":
    unittest.main()
