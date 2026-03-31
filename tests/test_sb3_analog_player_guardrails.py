from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SB3_PATH = ROOT / "ui" / "sb3.html"


class Sb3AnalogPlayerGuardrailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SB3_PATH.read_text(encoding="utf-8")

    def test_analog_player_has_strand_detection_helpers(self):
        self.assertIn("function analogStreamHealthyForPlayer()", self.text)
        self.assertIn("function analogPlayerNeedsReload(audioEl)", self.text)
        self.assertIn("networkState === 3", self.text)

    def test_sync_audio_source_reloads_stranded_analog_stream(self):
        self.assertIn("const shouldPlay = wantsPlay || !wasPaused;", self.text)
        self.assertIn("const next = normalizeAudioUrl(base);", self.text)
        self.assertIn("} else if (shouldPlay && analogPlayerNeedsReload(audioEl)) {", self.text)
        self.assertIn("requestAudioPlay(audioEl, 'sync-live-reset');", self.text)

    def test_analog_play_event_primes_live_stream_and_metadata_nudges_live_edge(self):
        self.assertIn("reloadAnalogStream('play-prime', true, { hardReset: true });", self.text)
        self.assertIn("audioEl.addEventListener('loadedmetadata', () => {", self.text)
        self.assertIn("audioEl.addEventListener('durationchange', () => {", self.text)
        self.assertIn("nudgeLiveAudioToSeekableEnd(audioEl);", self.text)


if __name__ == "__main__":
    unittest.main()
