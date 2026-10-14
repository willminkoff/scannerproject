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

from sb3.ui.routes import (  # noqa: E402
    _cluster_count,
    _hp_extract_pool,
    _hp_mode_hint,
)


def _state(*freqs_mhz, mode=None):
    fav = {"kind": "conventional", "alpha_tag": "ch"}
    if mode is not None:
        fav["mode"] = mode
    return {
        "custom_favorites": [
            dict(fav, frequency=f, alpha_tag="ch-%s" % f) for f in freqs_mhz
        ]
    }


def _split(*freqs_mhz, mode=None):
    airs, grounds, digital = _hp_extract_pool(_state(*freqs_mhz, mode=mode))
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


class ModeHintTests(unittest.TestCase):
    """chirp's demod mode belongs to the daemon, not the channel -- a channel
    whose mode != pool_mode is rejected outright. So a favorite's declared mode
    has to choose the DESTINATION, and these pin that mapping."""

    def test_normalises_known_aliases(self):
        for raw, want in [("AM", "am"), ("am", "am"), ("FM", "nfm"),
                          ("nfm", "nfm"), ("FMN", "nfm"), ("Narrow", "nfm"),
                          (" am ", "am")]:
            self.assertEqual(_hp_mode_hint({"mode": raw}), want, raw)

    def test_digital_and_blank_modes_do_not_pick_a_demod(self):
        """P25/DMR are not chirp demods -- fall through to the band split."""
        for raw in ["P25", "DMR", "NXDN", "", None, "   ", "p25e"]:
            self.assertIsNone(_hp_mode_hint({"mode": raw}), repr(raw))
        self.assertIsNone(_hp_mode_hint({}))
        self.assertIsNone(_hp_mode_hint(None))

    def test_vhf_milair_tagged_am_goes_to_airband(self):
        """The 177 FW / 119 FS entries: AM traffic that lands on ground by
        band and comes out as garbled NFM."""
        airs, grounds, _ = _split(138.05, 138.875, 139.15, 140.1, 140.7,
                                  mode="AM")
        self.assertEqual(airs, [138.05, 138.875, 139.15, 140.1, 140.7])
        self.assertEqual(grounds, [])

    def test_same_frequencies_untagged_still_go_to_ground(self):
        """Untagged behaviour is unchanged -- 138-144 also carries FM federal
        traffic, so the band alone must not assume AM."""
        airs, grounds, _ = _split(138.05, 138.875, 139.15, 140.1, 140.7)
        self.assertEqual(airs, [])
        self.assertEqual(grounds, [138.05, 138.875, 139.15, 140.1, 140.7])

    def test_nfm_hint_overrides_an_airband_frequency(self):
        airs, grounds, _ = _split(133.125, mode="nfm")
        self.assertEqual(airs, [])
        self.assertEqual(grounds, [133.125])

    def test_unknown_mode_falls_back_to_band(self):
        airs, grounds, _ = _split(133.125, 462.675, mode="P25")
        self.assertEqual(airs, [133.125])
        self.assertEqual(grounds, [462.675])


class ClusterCountTests(unittest.TestCase):
    """_cluster_count is a local copy of chirp's greedy packer, used to trim a
    push to the LO budget before chirp can reject the batch outright. If it
    disagrees with the real planner the guard is worthless, so these pin the
    algorithm -- and test_matches_real_planner checks the two for real."""

    IQ = 2.0e6

    def _count(self, *freqs_mhz):
        return _cluster_count([{"freq_mhz": f} for f in freqs_mhz], self.IQ)

    def test_empty(self):
        self.assertEqual(_cluster_count([], self.IQ), 0)

    def test_civilian_airband_packs_tightly(self):
        """12 channels inside 118-137: this is why airband never hit the cap."""
        self.assertLessEqual(
            self._count(118.65, 121.025, 121.7, 122.525, 125.45, 126.475,
                        127.7, 128.3, 133.125, 133.15, 133.5, 134.325),
            6,
        )

    def test_milair_costs_about_one_cluster_each(self):
        self.assertEqual(self._count(255.0, 285.4, 363.8, 382.2, 388.2), 5)

    def test_span_exactly_iq_bw_shares_a_cluster(self):
        """Boundary is <=, matching plan_clusters."""
        self.assertEqual(self._count(300.0, 302.0), 1)
        self.assertEqual(self._count(300.0, 302.000001), 2)

    def test_greedy_anchors_on_group_start_not_previous_channel(self):
        """A chain 1.5 MHz apart must not collapse into one cluster: the span
        is measured from the group's first channel, not its last."""
        self.assertEqual(self._count(300.0, 301.5, 303.0, 304.5), 2)

    def test_sic_milair_profile_fits_the_default_budget(self):
        """The shipped profile is built to land at exactly 6."""
        self.assertEqual(
            self._count(255.0, 255.8, 260.9, 261.0, 281.45, 283.1,
                        285.4, 286.2, 298.6, 298.875, 327.125),
            6,
        )

    def test_mixed_civilian_plus_milair_blows_the_budget(self):
        """The regression that took airband to zero: 12 civilian + 5 milair."""
        self.assertGreater(
            self._count(118.65, 126.475, 363.8, 388.2, 255.0, 285.4, 382.2,
                        121.7, 122.525, 133.5, 134.325, 128.3),
            6,
        )

    def test_matches_real_planner(self):
        """Equivalence against chirp's planner over randomized inputs."""
        import random
        try:
            from chirp.dsp.cluster_planner import PlanChannel, plan_clusters
        except ImportError:  # pragma: no cover
            self.skipTest("chirp.dsp.cluster_planner unavailable")
        random.seed(7)
        for _ in range(500):
            freqs = sorted({round(random.uniform(108.0, 400.0), 4)
                            for _ in range(random.randint(1, 18))})
            mine = _cluster_count([{"freq_mhz": f} for f in freqs], self.IQ)
            real = len(plan_clusters(
                [PlanChannel(id="c%d" % i, freq_hz=f * 1e6)
                 for i, f in enumerate(freqs)],
                iq_bw_hz=self.IQ, max_clusters=10**6))
            self.assertEqual(mine, real, freqs)


if __name__ == "__main__":
    unittest.main()
