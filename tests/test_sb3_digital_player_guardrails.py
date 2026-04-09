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
        self.assertIn("requestAudioPlay(audioEl, 'sync-digital-reset');", self.text)

    def test_digital_stream_recovery_uses_multiple_retry_attempts(self):
        self.assertIn("const plan = normalized === 'digital'", self.text)
        self.assertIn("{ delayMs: 1200, hardReset: false }", self.text)
        self.assertIn("{ delayMs: 3200, hardReset: true }", self.text)
        self.assertIn("{ delayMs: 6500, hardReset: true }", self.text)

    def test_digital_play_event_primes_dead_player(self):
        self.assertIn("if (isDigitalAudioElement(audioEl) && digitalPlayerNeedsReload(audioEl)) {", self.text)
        self.assertIn("reloadDigitalStream('play-prime', true, { hardReset: true });", self.text)


if __name__ == "__main__":
    unittest.main()
