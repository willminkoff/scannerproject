"""Band-scan state read back from the radio, not from a profile variable.

The band-scan buttons derived "am I active?" from state[target].currentProfile,
but __BANDSCAN_DIRECT_POST_SHIM__ pushes channels straight to chirp and never
touches the profile system, so that variable was always stale. Pressing a
preset therefore changed nothing visible anywhere in the UI -- and the one
call that would have refreshed the buttons, updateBandScanButtonStates(), was
never defined, so its `typeof === 'function'` guard swallowed it silently.

chirp_scan_state() recovers the truth from what the daemon is actually
holding: bandscan_apply names every channel it creates `bs-<preset>-<n>`.
"""
from __future__ import annotations

import unittest

from sb3.ui.routes import chirp_scan_state


def snap(channels, **lo):
    base = {"n_clusters": 3, "dwell_s": 0.6, "current_cluster_idx": 1,
            "current_cluster_center_hz": 156800000.0, "scan_hold_state": "scanning"}
    base.update(lo)
    return {"channels": channels, "lo_scheduler": base}


def ch(cid, mhz=156.8):
    return {"id": cid, "freq_mhz": mhz}


class PresetDetectionTests(unittest.TestCase):
    def test_detects_preset_from_channel_ids(self):
        st = chirp_scan_state(snap([ch("bs-marine-0"), ch("bs-marine-1"), ch("bs-marine-2")]))
        self.assertEqual(st["preset"], "marine")
        self.assertEqual(st["preset_channels"], 3)
        self.assertEqual(st["channels"], 3)

    def test_no_preset_when_channels_are_from_a_favorite(self):
        """A favourites push makes n4-* ids -- that is not a band scan."""
        st = chirp_scan_state(snap([ch("n4-0-tower"), ch("n4-1-approach")]))
        self.assertNotIn("preset", st)
        self.assertEqual(st["channels"], 2)

    def test_keepalive_is_not_counted(self):
        st = chirp_scan_state(snap([ch("keepalive"), ch("bs-gmrs-0")]))
        self.assertEqual(st["channels"], 1)
        self.assertEqual(st["preset"], "gmrs")

    def test_multi_word_preset_key(self):
        st = chirp_scan_state(snap([ch("bs-rail-0"), ch("bs-rail-1")]))
        self.assertEqual(st["preset"], "rail")

    def test_busiest_preset_wins_when_mixed(self):
        st = chirp_scan_state(snap([ch("bs-marine-0"), ch("bs-gmrs-0"), ch("bs-gmrs-1")]))
        self.assertEqual(st["preset"], "gmrs")
        self.assertEqual(st["presets"], {"marine": 1, "gmrs": 2})


class ScanningFlagTests(unittest.TestCase):
    def test_rotating_counts_as_scanning(self):
        self.assertTrue(chirp_scan_state(snap([ch("bs-marine-0")]))["scanning"])

    def test_single_cluster_is_not_scanning(self):
        """One window with nothing to rotate to is parked, not scanning."""
        st = chirp_scan_state(snap([ch("bs-marine-0")], n_clusters=1))
        self.assertFalse(st["scanning"])

    def test_zero_dwell_is_not_scanning(self):
        self.assertFalse(chirp_scan_state(snap([ch("bs-marine-0")], dwell_s=0))["scanning"])

    def test_revisit_is_clusters_times_dwell(self):
        st = chirp_scan_state(snap([ch("bs-marine-0")], n_clusters=6, dwell_s=0.6))
        self.assertEqual(st["revisit_s"], 3.6)

    def test_reports_live_cluster_so_the_indicator_moves(self):
        st = chirp_scan_state(snap([ch("bs-marine-0")], current_cluster_idx=2,
                                   current_cluster_center_hz=162400000.0))
        self.assertEqual(st["cluster_index"], 2)
        self.assertAlmostEqual(st["cluster_center_mhz"], 162.4)

    def test_hold_is_surfaced(self):
        st = chirp_scan_state(snap([ch("bs-marine-0")], scan_hold_state="holding",
                                   held_channel_id="bs-marine-0"))
        self.assertEqual(st["hold_state"], "holding")
        self.assertEqual(st["held_channel_id"], "bs-marine-0")


class DegradedInputTests(unittest.TestCase):
    def test_dead_daemon_returns_empty_not_not_scanning(self):
        """{} means 'no data'. Returning scanning=False would claim the radio
        is idle when we simply could not reach it."""
        for bad in ({}, None, "", []):
            self.assertEqual(chirp_scan_state(bad), {})

    def test_missing_lo_scheduler_does_not_raise(self):
        st = chirp_scan_state({"channels": [ch("bs-marine-0")]})
        self.assertEqual(st["preset"], "marine")
        self.assertFalse(st["scanning"])
        self.assertIsNone(st["revisit_s"])

    def test_channels_as_dict_is_accepted(self):
        st = chirp_scan_state({"channels": {"bs-wx-0": ch("bs-wx-0")},
                               "lo_scheduler": {"n_clusters": 2, "dwell_s": 0.6}})
        self.assertEqual(st["preset"], "wx")

    def test_junk_entries_are_skipped(self):
        st = chirp_scan_state(snap([None, 42, ch("bs-cb-0")]))
        self.assertEqual(st["channels"], 1)
        self.assertEqual(st["preset"], "cb")

    def test_non_numeric_lo_fields_do_not_raise(self):
        st = chirp_scan_state(snap([ch("bs-marine-0")], n_clusters="x", dwell_s=None))
        self.assertIsNone(st["n_clusters"])
        self.assertFalse(st["scanning"])


if __name__ == "__main__":
    unittest.main()
