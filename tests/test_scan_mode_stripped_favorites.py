"""SB6 2026-06-17 — get_last_stripped_custom_favorites must never crash /api/status.

Regression for the gain-slider snap-back root cause. The per-band favorites
merge in get_scan_pool stored each stripped-tag bucket as ``list(v)`` — which,
because ``v`` is the ``{'tag_label', 'entries'}`` dict, collapsed to the dict's
KEYS (``['tag_label', 'entries']``). The reader then called
``payload.get('tag_label')`` on a list -> AttributeError, which crashed the
ENTIRE /api/status handler. With /api/status returning no response, the sb5 UI
fell back to its hardcoded default gain (BAND_DEFAULTS.air.gain = 32.8 -> 33),
which is exactly the "slider snaps back" symptom.

The writer is fixed to preserve the bucket shape; this pins the reader's
defensive guard so a malformed payload can never take down /api/status again.
"""
from __future__ import annotations

import tempfile
import unittest

from ui import scan_mode_controller


class StrippedFavoritesShapeTest(unittest.TestCase):
    def _controller(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        return scan_mode_controller.ScanModeController(db_path=tmp.name)

    def test_reader_normalizes_dict_payload_and_drops_internal_keys(self):
        c = self._controller()
        c._last_stripped_custom_favorites = {
            7: {"tag_label": "Aircraft", "entries": ["A", "B"], "seen": {"x"}},
        }
        out = c.get_last_stripped_custom_favorites()
        self.assertEqual(out, {7: {"tag_label": "Aircraft", "entries": ["A", "B"]}})

    def test_reader_tolerates_list_payload_without_crashing(self):
        # The OLD corrupted shape that crashed /api/status: a bare list.
        c = self._controller()
        c._last_stripped_custom_favorites = {
            7: ["tag_label", "entries"],          # what list(dict) produced
            9: ["KBNA Ground", "KBNA Clearance"],  # or a bare entries list
        }
        out = c.get_last_stripped_custom_favorites()  # must NOT raise
        self.assertEqual(out[7], {"tag_label": "", "entries": ["tag_label", "entries"]})
        self.assertEqual(out[9], {"tag_label": "", "entries": ["KBNA Ground", "KBNA Clearance"]})

    def test_reader_empty(self):
        c = self._controller()
        c._last_stripped_custom_favorites = {}
        self.assertEqual(c.get_last_stripped_custom_favorites(), {})


if __name__ == "__main__":
    unittest.main()
