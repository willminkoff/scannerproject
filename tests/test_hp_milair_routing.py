"""Band split in _hp_extract_pool — the function that decides which radio a
favorite reaches, and the one place a frequency can vanish without a trace.

UHF milair (225-400) used to match neither arm and was dropped silently:
_hp_auto_sync is best-effort, so a save with nothing but milair in it
reported success while the radio got an empty channel list. These tests
pin every edge of the three bands so that can't come back.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from sb3.ui.routes import _hp_extract_pool  # noqa: E402


def _state(*freqs_mhz):
    return {
        "custom_favorites": [
            {"kind": "conventional", "frequency": f, "alpha_tag": "ch-%s" % f}
            for f in freqs_mhz
        ]
    }


def _split(*freqs_mhz):
    airs, grounds, digital = _hp_extract_pool(_state(*freqs_mhz))
    return (
        [a["freq_mhz"] for a in airs],
        [g["freq_mhz"] for g in grounds],
        digital,
    )


class BandSplitTests(unittest.TestCase):
    def test_civilian_airband_goes_to_airband(self):
        airs, grounds, _ = _split(108.0, 121.5, 137.0)
        self.assertEqual(airs, [108.0, 121.5, 137.0])
        self.assertEqual(grounds, [])

    def test_uhf_milair_goes_to_airband_not_ground(self):
        """The regression this suite exists for."""
        airs, grounds, _ = _split(225.0, 243.0, 285.4, 327.125, 399.975)
        self.assertEqual(airs, [225.0, 243.0, 285.4, 327.125, 399.975])
        self.assertEqual(grounds, [], "milair is AM; ground is pushed as nfm")

    def test_vhf_and_uhf_lmr_go_to_ground(self):
        airs, grounds, _ = _split(137.5, 175.0, 400.0, 462.675, 470.0)
        self.assertEqual(airs, [])
        self.assertEqual(grounds, [137.5, 175.0, 400.0, 462.675, 470.0])

    def test_400_is_ground_not_milair(self):
        """225-400 is half-open at the top so 400.000 stays with UHF LMR,
        which owns 400-470. The top milair channel is 399.975."""
        airs, grounds, _ = _split(399.975, 400.0)
        self.assertEqual(airs, [399.975])
        self.assertEqual(grounds, [400.0])

    def test_gaps_are_still_dropped(self):
        """175-225 and >470 have no radio. Dropping is the existing
        behaviour; this records it so the silence is at least deliberate."""
        airs, grounds, _ = _split(200.0, 220.0, 500.0, 850.0)
        self.assertEqual(airs, [])
        self.assertEqual(grounds, [])

    def test_mixed_profile_splits_both_ways(self):
        airs, grounds, _ = _split(133.125, 285.4, 462.675)
        self.assertEqual(airs, [133.125, 285.4])
        self.assertEqual(grounds, [462.675])

    def test_unparseable_frequency_is_skipped_not_fatal(self):
        airs, grounds, _ = _split("not-a-number", 285.4)
        self.assertEqual(airs, [285.4])
        self.assertEqual(grounds, [])


if __name__ == "__main__":
    unittest.main()
