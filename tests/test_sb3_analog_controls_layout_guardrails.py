from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SB3_PATH = ROOT / "ui" / "sb3.html"


class Sb3AnalogControlsLayoutGuardrailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SB3_PATH.read_text(encoding="utf-8")

    def test_apply_is_first_analog_control_button_and_primary(self):
        analog_start = self.text.index('<div id="controls-analog">')
        analog_end = self.text.index('<div id="controls-digital" class="hidden">', analog_start)
        analog_text = self.text[analog_start:analog_end]
        btn_row_start = analog_text.index('<div class="btn-row">')
        btn_row_end = analog_text.index('</div>', btn_row_start)
        btn_row_text = analog_text[btn_row_start:btn_row_end]
        self.assertIn('<button class="action action-primary" id="btn-apply">Apply</button>', btn_row_text)
        self.assertLess(
            btn_row_text.index('id="btn-apply"'),
            btn_row_text.index('id="btn-play-analog"'),
        )
        self.assertIn('#controls-analog .action-primary {', self.text)

    def test_filter_hz_is_hidden_only_in_analog_controls(self):
        analog_start = self.text.index('<div id="controls-analog">')
        analog_end = self.text.index('<div id="controls-digital" class="hidden">', analog_start)
        analog_text = self.text[analog_start:analog_end]
        self.assertIn('<div class="control-item hidden" id="analog-filter-control">', analog_text)
        self.assertIn('<span>Filter (Hz)</span>', analog_text)
        digital_text = self.text[analog_end:]
        self.assertNotIn('analog-filter-control', digital_text)
        self.assertNotIn('action-primary" id="btn-play-digital"', self.text)


if __name__ == "__main__":
    unittest.main()
