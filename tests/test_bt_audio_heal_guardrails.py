from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = ROOT / "systemd" / "scanner-bt-audio-heal.service"
SCRIPT_PATH = ROOT / "scripts" / "bt-audio-heal.sh"


class BtAudioHealGuardrailTests(unittest.TestCase):
    def test_bt_heal_service_does_not_pull_ui_up(self):
        text = SERVICE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("Wants=network-online.target bluetooth.service airband-ui.service", text)
        self.assertNotIn("After=network-online.target bluetooth.service airband-ui.service", text)

    def test_bt_heal_script_skips_when_ui_inactive_by_default(self):
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn('BT_HEAL_REQUIRE_UI_ACTIVE="${BT_HEAL_REQUIRE_UI_ACTIVE:-1}"', text)
        self.assertIn('systemctl --quiet is-active airband-ui.service', text)


if __name__ == "__main__":
    unittest.main()
