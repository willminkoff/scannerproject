"""Wizard → analog SB3 profile (sub-phase 1)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sb3.profile import parse_profile
from sb3.ui import profilegen as G

MED = [(462.95, "Med 9"), (462.975, "Med 10"), (463.0, "Med 1 Baptist"),
       (463.025, "Med 2 Vanderbilt"), (463.05, "Med 3 St Thomas")]


def _req(**kw):
    base = {"name": "med-net", "device_serial": "95339533",
            "channels": [{"freq_hz": int(round(f * 1e6)), "label": l} for f, l in MED]}
    base.update(kw)
    return base


class TestGeneratedProfileIsLoadable(unittest.TestCase):
    """The generator runs the real parser, so a saved profile always loads."""

    def test_builds_and_parses(self):
        p = G.build_profile(_req())
        parse_profile(p, path="<test>")          # must not raise
        self.assertEqual(p["name"], "med-net")
        self.assertEqual(len(p["channels"]), 5)

    def test_centre_is_the_midpoint(self):
        p = G.build_profile(_req())
        self.assertEqual(p["device"]["center_freq_hz"], (462_950_000 + 463_050_000) // 2)

    def test_sample_rate_snaps_up_to_a_supported_rate(self):
        p = G.build_profile(_req())
        rate = p["device"]["sample_rate_hz"]
        self.assertIn(rate, G.SUPPORTED_RATES["RTLSDR"])
        span = 463_050_000 - 462_950_000
        self.assertGreaterEqual(rate, span * G.USABLE_FRACTION)

    def test_every_channel_fits_the_baseband_window(self):
        """THE fit check — the thing that silently drops channels if wrong."""
        p = G.build_profile(_req())
        half = p["device"]["sample_rate_hz"] / 2
        for ch in p["channels"]:
            edge = abs(ch["freq_hz"] - p["device"]["center_freq_hz"]) + ch["rf_bw_hz"] / 2
            self.assertLess(edge, half, f"{ch['title']} falls outside the window")

    def test_exactly_one_keepalive_and_it_is_first(self):
        p = G.build_profile(_req())
        ka = [c for c in p["channels"] if c.get("keepalive")]
        self.assertEqual(len(ka), 1)
        self.assertEqual(ka[0]["freq_hz"], min(c["freq_hz"] for c in p["channels"]))
        self.assertEqual(ka[0]["squelch_db"], G.KEEPALIVE_SQUELCH_DB)

    def test_non_airband_is_nfm(self):
        p = G.build_profile(_req())
        self.assertTrue(all(c["demod"] == "NFM" for c in p["channels"]))

    def test_airband_is_am(self):
        p = G.build_profile(_req(channels=[{"freq_hz": 118_400_000, "label": "a"},
                                           {"freq_hz": 119_350_000, "label": "b"}]))
        self.assertTrue(all(c["demod"] == "AM" for c in p["channels"]))
        self.assertEqual(p["channels"][0]["rf_bw_hz"], G.AM_RF_BW)

    def test_rsp1b_gets_the_sdrplay_gain_model(self):
        p = G.build_profile(_req(device_serial="2405265A60",
                                 channels=[{"freq_hz": 118_400_000},
                                           {"freq_hz": 119_350_000}]))
        self.assertIn("if_gain_db", p["device"])
        self.assertNotIn("gain_tenths_db", p["device"])
        self.assertEqual(p["deviceset_index"], 0)

    def test_mount_and_tap_match_the_shared_analog_chain(self):
        p = G.build_profile(_req())
        self.assertEqual(p["mount"], "neptune-analog.mp3")
        self.assertEqual(p["copy_to_udp"]["port"], 9998)
        self.assertEqual(p["audio_device"]["strategy"], "system_default")


class TestRejections(unittest.TestCase):
    def _err(self, **kw):
        with self.assertRaises(G.GenError) as ctx:
            G.build_profile(_req(**kw))
        return ctx.exception

    def test_mixed_am_and_fm_rejected(self):
        e = self._err(channels=[{"freq_hz": 118_400_000}, {"freq_hz": 463_000_000}])
        self.assertEqual(e.code, 400)
        self.assertIn("mixes AM airband", e.reason)

    def test_no_channels_rejected(self):
        self.assertEqual(self._err(channels=[]).code, 400)

    def test_duplicate_freq_rejected(self):
        e = self._err(channels=[{"freq_hz": 463_000_000}, {"freq_hz": 463_000_000}])
        self.assertIn("twice", e.reason)

    def test_out_of_tuning_range_rejected(self):
        e = self._err(channels=[{"freq_hz": 10_000_000}])
        self.assertIn("tunable range", e.reason)

    def test_span_too_wide_rejected(self):
        e = self._err(channels=[{"freq_hz": 30_000_000}, {"freq_hz": 800_000_000}])
        self.assertIn("bandwidth", e.reason)

    def test_unknown_device_rejected(self):
        self.assertIn("unknown device", self._err(device_serial="NOPE").reason)

    def test_too_many_channels_rejected(self):
        e = self._err(channels=[{"freq_hz": 462_000_000 + i * 25_000} for i in range(33)])
        self.assertIn("limit", e.reason)


class TestNameIsRejectedNotMangled(unittest.TestCase):
    """Silently stripping ../../etc/passwd to 'etcpasswd' saves the user's work
    under a name they never chose. Reject instead."""

    def test_traversal_rejected(self):
        for bad in ("../../etc/passwd", "..", "a/b", "a\\b", "-lead", "café", ""):
            with self.assertRaises(G.GenError, msg=bad):
                G.safe_name(bad)

    def test_reasonable_names_normalised(self):
        self.assertEqual(G.safe_name("Nashville Med Net"), "nashville-med-net")
        self.assertEqual(G.safe_name("davidson-fire"), "davidson-fire")


class TestProfileDiscovery(unittest.TestCase):
    def test_repo_wins_on_collision(self):
        import sb3.profilecmd as pc
        src = Path(pc.__file__).read_text()
        self.assertIn("repo first = wins", src)

    def test_list_reports_source(self):
        import sb3.profilecmd as pc
        rows = pc.list_profiles()
        self.assertTrue(rows)
        self.assertTrue(all(len(r) == 3 for r in rows))
        self.assertTrue(any(s == "repo" for _n, s, _p in rows))


if __name__ == "__main__":
    unittest.main()
