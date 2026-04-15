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
        self.assertIn("const activated = state.userActivatedEmbedAudio[kind];", self.text)
        self.assertIn("const shouldPlay = activated && (wantsPlay || !wasPaused);", self.text)
        self.assertIn("const next = normalizeAudioUrl(base);", self.text)
        self.assertIn("} else if (shouldPlay && analogPlayerNeedsReload(audioEl)) {", self.text)
        self.assertIn("playWhenReady(audioEl, 'sync-live-reset');", self.text)

    def test_analog_play_event_uses_embedded_recovery_and_metadata_nudges_live_edge(self):
        self.assertIn("startEmbeddedRecovery(audioEl, reloadFn, 'play-prime', {", self.text)
        self.assertIn("forcePlay: true,", self.text)
        self.assertIn("hardReset: true,", self.text)
        self.assertIn("immediate: true,", self.text)
        self.assertIn("audioEl.addEventListener('loadedmetadata', () => {", self.text)
        self.assertIn("audioEl.addEventListener('durationchange', () => {", self.text)
        self.assertIn("nudgeLiveAudioToSeekableEnd(audioEl);", self.text)

    def test_explicit_analog_restart_actions_force_immediate_hard_reset_recovery(self):
        self.assertIn("function analogRestartRecoveryOptions(reason) {", self.text)
        self.assertIn("tag === 'auto-squelch'", self.text)
        self.assertIn("tag.startsWith('apply-')", self.text)
        self.assertIn("tag.startsWith('apply-batch-')", self.text)
        self.assertIn("tag.startsWith('profile-')", self.text)
        self.assertIn("tag.startsWith('tune-')", self.text)
        self.assertIn("const restartAwareOptions = normalized === 'analog'", self.text)
        self.assertIn("immediate: Boolean(restartAwareOptions.immediate),", self.text)
        self.assertIn("hardReset: Boolean(restartAwareOptions.hardReset),", self.text)

    def test_request_audio_play_logs_with_resolved_target_label(self):
        self.assertIn("const label = audioTargetLabel(audioEl);", self.text)
        self.assertIn("logActivity(`${label} player ${msg}`", self.text)


if __name__ == "__main__":
    unittest.main()
