"""Phase 5 — digital-mode CAPTURE_RULES tests for classifier._match_capture_rule.

Covers:
  - P25 inside 700 MHz PS band (769-776) matches when BW + SNR are in range
  - P25 inside 800 MHz PS band (851-869) matches likewise
  - NXDN inside 452-454 MHz with tight 4-7 kHz BW matches
  - DMR inside 452-458 MHz with 8-14 kHz BW matches
  - Below the SNR floor (15 dB) returns None even when freq/BW would otherwise match
  - Outside the freq range returns None
  - The analog rules (no snr_min) still match without snr_db being passed
"""
from __future__ import annotations

import unittest

from disco.src import classifier


def m(freq_mhz, bw_khz):
    """Build a minimal meta dict for _match_capture_rule."""
    return {"freq_hz": freq_mhz * 1e6, "bandwidth_hz": bw_khz * 1e3}


class CaptureRulesTest(unittest.TestCase):
    # --- Digital rules ------------------------------------------------------
    def test_p25_700_mhz_inband_matches(self):
        self.assertEqual(classifier._match_capture_rule(m(770.5, 8.5), snr_db=20.0), "P25")

    def test_p25_800_mhz_inband_matches(self):
        self.assertEqual(classifier._match_capture_rule(m(855.0, 9.0), snr_db=20.0), "P25")

    def test_nxdn_inband_matches(self):
        self.assertEqual(classifier._match_capture_rule(m(453.0, 5.5), snr_db=18.0), "NXDN")

    def test_dmr_inband_matches(self):
        self.assertEqual(classifier._match_capture_rule(m(456.0, 12.0), snr_db=17.0), "DMR")

    # --- SNR floor ----------------------------------------------------------
    def test_p25_below_snr_floor_returns_none(self):
        # Same freq + BW as the matching test but SNR < 15.0
        self.assertIsNone(classifier._match_capture_rule(m(770.5, 8.5), snr_db=12.0))

    def test_nxdn_below_snr_floor_returns_none(self):
        self.assertIsNone(classifier._match_capture_rule(m(453.0, 5.5), snr_db=10.0))

    def test_dmr_missing_snr_returns_none(self):
        # Digital rules require snr_db to be provided AND >= snr_min.
        self.assertIsNone(classifier._match_capture_rule(m(456.0, 12.0), snr_db=None))

    # --- Out-of-range -------------------------------------------------------
    def test_p25_outside_freq_range_returns_none(self):
        # 850 MHz is just below the 851 MHz floor of the 800 PS rule and above
        # the 776 MHz ceiling of the 700 PS rule — falls through.
        self.assertIsNone(classifier._match_capture_rule(m(850.0, 8.5), snr_db=20.0))

    def test_nxdn_above_bw_falls_through_to_dmr(self):
        # 8 kHz BW at 453 MHz is too wide for NXDN's 4-7 kHz window but matches
        # DMR's 8-14 kHz window in 452-458 MHz. First-match-wins ordering puts
        # DMR after NXDN, so the second rule catches it.
        self.assertEqual(classifier._match_capture_rule(m(453.0, 8.5), snr_db=20.0), "DMR")

    def test_dmr_below_bw_floor_returns_none(self):
        # 7 kHz at 456 MHz: too wide for NXDN's range (only 452-454), too narrow
        # for DMR's 8 kHz floor, and the FM_NARROW UHF rule starts at 462 MHz.
        # No rule matches.
        self.assertIsNone(classifier._match_capture_rule(m(456.0, 7.0), snr_db=20.0))

    # --- Analog rules unchanged --------------------------------------------
    def test_fm_broadcast_still_matches_without_snr(self):
        # FM_BROADCAST has no snr_min — passing snr_db=None must still match.
        self.assertEqual(classifier._match_capture_rule(m(96.5, 200.0), snr_db=None), "FM_BROADCAST")

    def test_am_voice_airband_still_matches_without_snr(self):
        self.assertEqual(classifier._match_capture_rule(m(122.0, 8.0), snr_db=None), "AM_VOICE")

    def test_fm_narrow_amateur_2m_still_matches_without_snr(self):
        self.assertEqual(classifier._match_capture_rule(m(146.52, 12.5), snr_db=None), "FM_NARROW")

    # --- Ordering: digital wins over analog on freq overlap ----------------
    def test_p25_wins_over_legacy_analog_at_800_ps(self):
        # No analog rule covers 851-869 today (the prior code had no rules
        # in this range at all). Sanity check that the new digital rule fires.
        self.assertEqual(classifier._match_capture_rule(m(853.5, 9.0), snr_db=20.0), "P25")


if __name__ == "__main__":
    unittest.main()
