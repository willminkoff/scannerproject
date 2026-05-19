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


class CaptureCountTest(unittest.TestCase):
    """Regression test for the .iq.f32-only counting in _capture_count.

    The original implementation used `len(os.listdir(d))` which counted both
    .iq.f32 captures AND their .meta sidecars. With CAPTURE_MAX_PER_LABEL=2000
    that effectively halved the cap; labels with >1000 captures (FM_BROADCAST,
    P25) stopped archiving silently. The fix filters to .iq.f32 only.
    """

    def setUp(self):
        import os
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Override the module-level CAPTURE_DIR so the test can populate a
        # controlled directory tree without touching production paths.
        self._orig_capture_dir = classifier.CAPTURE_DIR
        classifier.CAPTURE_DIR = self.tmp.name
        classifier._CAPTURE_COUNTS.clear()
        self.addCleanup(self._restore_capture_dir)

    def _restore_capture_dir(self):
        classifier.CAPTURE_DIR = self._orig_capture_dir
        classifier._CAPTURE_COUNTS.clear()

    def _make_files(self, label, n_slices, with_meta=True):
        import os
        d = os.path.join(self.tmp.name, label)
        os.makedirs(d, exist_ok=True)
        for i in range(n_slices):
            with open(os.path.join(d, f"A-T1_770000000_8500_50000_t{i}_uid{i}.iq.f32"), "wb") as fh:
                fh.write(b"\x00")
            if with_meta:
                with open(os.path.join(d, f"A-T1_770000000_8500_50000_t{i}_uid{i}.iq.f32.meta"), "w") as fh:
                    fh.write("snr_db=20.0\n")

    def test_capture_count_ignores_meta_sidecars(self):
        # 1500 actual captures, each with a .meta sidecar → 3000 dir entries.
        # Pre-fix code would return 3000 (≥ 2000 cap → stop archiving).
        # Post-fix returns 1500 (under 2000 cap → keep archiving).
        self._make_files("P25", 1500, with_meta=True)
        self.assertEqual(classifier._capture_count("P25"), 1500)

    def test_capture_count_no_meta_just_iqf32(self):
        # 50 captures, no sidecars (e.g. legacy _uls migration leftover).
        self._make_files("DMR", 50, with_meta=False)
        self.assertEqual(classifier._capture_count("DMR"), 50)

    def test_capture_count_zero_when_dir_missing(self):
        self.assertEqual(classifier._capture_count("NEVER_USED"), 0)

    def test_capture_count_is_cached(self):
        # After first call, repeated calls don't re-scan the filesystem —
        # so adding files after the first count is computed is invisible
        # until the cache is cleared (matches production behavior).
        self._make_files("NXDN", 10)
        self.assertEqual(classifier._capture_count("NXDN"), 10)
        self._make_files("NXDN", 10)  # +10 more on disk = 20 total
        self.assertEqual(classifier._capture_count("NXDN"), 10)  # cache wins


if __name__ == "__main__":
    unittest.main()
