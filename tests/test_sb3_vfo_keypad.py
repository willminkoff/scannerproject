"""Static guardrails for the VFO keypad and the Philly removal.

The keypad's *behaviour* (entry state machine, validation, tune dispatch) is
proven by driving the extracted functions in node — see the report. These are
the structural guarantees that survive in CI without a JS runtime.
"""

from __future__ import annotations

import csv
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HTML = (REPO / "ui" / "sb3.html").read_text(encoding="utf-8")
CSV_PATH = REPO / "etc" / "mac" / "sdrangel" / "scan-nashville.csv"


class TestPhillyRemoved(unittest.TestCase):
    def test_no_philly_anywhere_in_the_ui(self):
        self.assertNotIn("philly", HTML.lower())

    def test_remote_pi_host_is_gone(self):
        self.assertNotIn("100.69.95.13", HTML)

    def test_tab_bar_has_the_three_surviving_tabs(self):
        for tab in ("tab-airband", "tab-vfo", "tab-digital"):
            self.assertIn(f'id="{tab}"', HTML)
        self.assertNotIn('id="tab-philly"', HTML)

    def test_wizard_endpoints_preserved(self):
        """Philly removal must not touch the wizard we just wired."""
        for ep in ("countries", "states", "counties", "systems", "channels"):
            self.assertIn(f"/api/scan/favorites-wizard/{ep}", HTML)

    def test_travel_mode_endpoints_preserved(self):
        """/api/hp/* is Travel Mode, NOT Philly — it must survive."""
        self.assertIn("/api/hp/state", HTML)


class TestKeypadStructure(unittest.TestCase):
    def test_all_twelve_grid_keys_present(self):
        keys = set(re.findall(r'data-kp="([^"]+)"', HTML))
        self.assertEqual(keys, set("0123456789") | {".", "back"})

    def test_clear_and_tune_actions(self):
        self.assertIn('id="vfo-kp-clear"', HTML)
        self.assertIn('id="vfo-kp-tune"', HTML)

    def test_no_text_input_for_frequency_entry(self):
        """Will's call: keypad only, no text-input alternative."""
        block = HTML[HTML.index('id="vfo-keypad"'):HTML.index('id="vfo-kp-hint"')]
        self.assertNotIn("<input", block)

    def test_posts_to_the_proven_tune_endpoint(self):
        self.assertIn("postAPI('/api/tune', { target: 'vfo', freq })", HTML)

    def test_bounds_mirror_the_server(self):
        self.assertIn("VFO_KP_MIN_MHZ = 24", HTML)
        self.assertIn("VFO_KP_MAX_MHZ = 1766", HTML)


class TestPresetsAreRealNotFabricated(unittest.TestCase):
    """Every preset must exist in the curated Nashville CSV."""

    def _presets(self):
        block = HTML[HTML.index("const VFO_PRESETS = ["):
                     HTML.index("// Mirrors sb3/ui/routes.py")]
        return re.findall(r"mhz:\s*([0-9.]+),\s*label:\s*'([^']+)'", block)

    def test_ten_presets(self):
        self.assertEqual(len(self._presets()), 10)

    def test_every_preset_is_in_scan_nashville_csv_and_enabled(self):
        rows = {int(r["Freq (Hz)"]): r for r in csv.DictReader(CSV_PATH.open())}
        for mhz, label in self._presets():
            hz = int(round(float(mhz) * 1e6))
            self.assertIn(hz, rows, f"{mhz} MHz ({label}) is not in scan-nashville.csv")
            self.assertEqual(rows[hz]["Enable"], "true",
                             f"{mhz} MHz is not an enabled row")

    def test_no_am_airband_presets(self):
        """VFO is NFM and /api/tune does not change demod, so AM airband
        frequencies would tune but never demodulate."""
        for mhz, _label in self._presets():
            self.assertFalse(108.0 <= float(mhz) <= 137.0,
                             f"{mhz} MHz is AM airband — cannot demod on an NFM VFO")


if __name__ == "__main__":
    unittest.main()
