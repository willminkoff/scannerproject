from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SB3_PATH = ROOT / "ui" / "sb3.html"


class Sb3RecentHitsLayoutGuardrailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SB3_PATH.read_text(encoding="utf-8")

    def test_recent_hits_and_system_activity_widgets_have_stable_ids(self):
        self.assertIn('id="system-activity-widget"', self.text)
        self.assertIn('id="recent-hits-widget"', self.text)

    def test_recent_hits_card_uses_grid_height_contract(self):
        self.assertIn(".widget-hits {", self.text)
        self.assertIn("display: grid;", self.text)
        self.assertIn("grid-template-rows: auto minmax(0, 1fr);", self.text)

    def test_recent_hits_height_sync_helpers_exist(self):
        self.assertIn("function syncRecentHitsHeight()", self.text)
        self.assertIn("function setupRecentHitsHeightSync()", self.text)
        self.assertIn("new ResizeObserver(() => syncRecentHitsHeight())", self.text)
        self.assertIn("window.addEventListener('resize', syncRecentHitsHeight);", self.text)

    def test_recent_hits_height_sync_is_initialized(self):
        self.assertIn("setupRecentHitsHeightSync();", self.text)
        self.assertIn("recentWidget.style.height = `${targetHeight}px`;", self.text)
        self.assertIn("recentWidget.style.maxHeight = `${targetHeight}px`;", self.text)


if __name__ == "__main__":
    unittest.main()
