from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SB3_PATH = ROOT / "ui" / "sb3.html"


class Sb3DigitalPlayerGuardrailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SB3_PATH.read_text(encoding="utf-8")

    def test_digital_player_has_health_and_strand_detection_helpers(self):
        self.assertIn("function digitalStreamHealthyForPlayer()", self.text)
        self.assertIn("function digitalPlayerNeedsReload(audioEl)", self.text)
        self.assertIn("playWhenReady(audioEl, 'sync-digital-reset');", self.text)

    def test_digital_stream_recovery_uses_bounded_retry_plan(self):
        self.assertIn("function embeddedRecoveryPlan(audioEl)", self.text)
        self.assertIn("{ delayMs: 1200, hardReset: false }", self.text)
        self.assertIn("{ delayMs: 3500, hardReset: true }", self.text)
        self.assertIn("{ delayMs: 7000, hardReset: true }", self.text)
        self.assertIn("if (tracker.recoveryTimer) return;", self.text)

    def test_digital_play_event_primes_dead_player(self):
        self.assertIn("if (isDigitalAudioElement(audioEl) && digitalPlayerNeedsReload(audioEl)) {", self.text)
        self.assertIn("startEmbeddedRecovery(audioEl, reloadFn, 'play-prime', {", self.text)


if __name__ == "__main__":
    unittest.main()
