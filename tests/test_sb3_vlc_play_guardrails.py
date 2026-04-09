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
        self.assertIn("const targetRunning = liveTarget ? !!liveTarget.running : !!targetState.running;", self.text)
        self.assertIn("const action = targetRunning ? 'stop' : 'start';", self.text)

    def test_vlc_commands_wait_for_backend_settle_state(self):
        self.assertIn("async function waitForVlcSettledState(target, expectedRunning, options = {})", self.text)
        self.assertIn("const pendingState = action === 'stop' ? 'stopping' : 'starting';", self.text)
        self.assertIn("const settled = await waitForVlcSettledState(resolvedTarget, expectedRunning, options);", self.text)
        self.assertIn("error: 'playback status timeout'", self.text)

    def test_vlc_restart_paths_reuse_shared_command_helper(self):
        self.assertIn("async function runVlcCommand(target, action, options = {})", self.text)
        self.assertIn("await runVlcCommand('analog', 'restart', { timeoutMs: 5000, pollMs: 250 });", self.text)
        self.assertIn("await runVlcCommand('digital', 'restart', { timeoutMs: 5000, pollMs: 250 });", self.text)

    def test_vlc_error_targets_continue_background_polling(self):
        self.assertIn("|| state.vlc.analog.state === 'error'", self.text)
        self.assertIn("|| state.vlc.digital.state === 'error'", self.text)


if __name__ == "__main__":
    unittest.main()
