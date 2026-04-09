from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SB3_PATH = ROOT / "ui" / "sb3.html"


class Sb3VlcPlayGuardrailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SB3_PATH.read_text(encoding="utf-8")

    def test_vlc_toggle_fetches_live_status_before_deciding_action(self):
        self.assertIn("async function fetchVlcTargetsStatus(target)", self.text)
        self.assertIn("const liveTargets = await fetchVlcTargetsStatus(resolvedTarget);", self.text)
        self.assertIn("const targetRunning = liveTargets ? !!liveTargets[resolvedTarget] : !!targetState.running;", self.text)
        self.assertIn("const action = targetRunning ? 'stop' : 'start';", self.text)

    def test_vlc_toggle_schedules_post_action_status_reconciliation(self):
        self.assertIn("function scheduleVlcStatusRefresh(delayMs)", self.text)
        self.assertIn("scheduleVlcStatusRefresh(350);", self.text)
        self.assertIn("scheduleVlcStatusRefresh(1500);", self.text)


if __name__ == "__main__":
    unittest.main()
