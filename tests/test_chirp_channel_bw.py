"""chirp's channel_bw_hz is a ONE-SIDED cutoff, and ground inherited the wrong one.

The GMRS bench test on 2026-11-08 produced ~10 hits for a single key-up on
462.5625 and static on playback. Cause: ``ground.json`` never set
``channel_bw_hz``, so it inherited ``daemon.py``'s 12500.0 default. That value
is handed straight to ``firdes.low_pass()`` as the *cutoff* (chirp/dsp/channel.py),
so the demod passband is +-12.5 kHz -- a 25 kHz-wide filter sitting on the
12.5 kHz GMRS grid. The neighbouring channel's centre lands on the passband
edge, so neighbours open on every transmission and the priority gate can latch
an off-tune one.

These tests assert the *mechanism*, not just the number: they rebuild the same
filter and measure what it does to a neighbour 12.5 kHz away.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parents[1]
_CFG = _ROOT / "chirp" / "config"

# chirp/dsp/channel.py runs stage-2 at samp_rate / decims[0]**2 with decims[0]=5.
_GROUND_SAMP_RATE = 2_000_000.0
_STAGE2_RATE = _GROUND_SAMP_RATE / 5 ** 2          # 80 kHz
_TRANSITION_HZ = 1e3                               # channel.py passes 1e3
_GMRS_SPACING_HZ = 12_500.0                        # 462.5500, .5625, .5750, ...


def _firdes_low_pass(gain, fs, cutoff, transition, hamming_att_db=53.0):
    """Mirror gnuradio.filter.firdes.low_pass(..., WIN_HAMMING).

    GNU Radio is not importable on every dev box, so this reproduces the
    upstream algorithm: ntaps from compute_ntaps(), windowed sinc, DC-normalised.
    test_replica_is_minus_6db_at_cutoff pins it to the defining property of the
    design, so a drifting replica fails loudly instead of lying quietly.
    """
    ntaps = int(hamming_att_db * fs / (22.0 * transition))
    if ntaps % 2 == 0:
        ntaps += 1
    m = (ntaps - 1) // 2
    n = np.arange(-m, m + 1, dtype=float)
    fw_t0 = 2 * np.pi * cutoff / fs
    with np.errstate(invalid="ignore", divide="ignore"):
        sinc = np.where(n == 0, fw_t0 / np.pi, np.sin(n * fw_t0) / (n * np.pi))
    win = 0.54 - 0.46 * np.cos(2 * np.pi * np.arange(ntaps) / (ntaps - 1))
    taps = sinc * win
    return taps * (gain / taps.sum())


def _response_db(taps, freq_hz, fs):
    """|H(f)| in dB relative to DC."""
    n = np.arange(len(taps))
    h = np.abs(np.sum(taps * np.exp(-2j * np.pi * freq_hz * n / fs)))
    return 20 * np.log10(h / np.abs(taps.sum()))


def _cfg(name):
    return json.loads((_CFG / ("%s.json" % name)).read_text(encoding="utf-8"))


class ReplicaSanityTests(unittest.TestCase):
    """Guard the stand-in filter before trusting anything it measures."""

    def test_replica_is_minus_6db_at_cutoff(self):
        taps = _firdes_low_pass(1.0, _STAGE2_RATE, 8_000.0, _TRANSITION_HZ)
        self.assertAlmostEqual(
            _response_db(taps, 8_000.0, _STAGE2_RATE), -6.0, delta=1.5,
            msg="a low-pass sits ~6 dB down at its cutoff; replica has drifted")

    def test_replica_passes_dc(self):
        taps = _firdes_low_pass(1.0, _STAGE2_RATE, 8_000.0, _TRANSITION_HZ)
        self.assertAlmostEqual(_response_db(taps, 0.0, _STAGE2_RATE), 0.0, delta=0.01)


class AdjacentChannelRejectionTests(unittest.TestCase):
    def test_the_old_default_barely_rejected_the_neighbour(self):
        """Why one key-up became a run of hits. Not a regression guard -- a record."""
        taps = _firdes_low_pass(1.0, _STAGE2_RATE, 12_500.0, _TRANSITION_HZ)
        att = _response_db(taps, _GMRS_SPACING_HZ, _STAGE2_RATE)
        self.assertGreater(
            att, -12.0,
            "12500.0 was expected to leave the 12.5 kHz neighbour near the "
            "passband edge; measured %.1f dB" % att)

    def test_ground_rejects_the_neighbour_hard(self):
        bw = _cfg("ground")["channel_bw_hz"]
        taps = _firdes_low_pass(1.0, _STAGE2_RATE, bw, _TRANSITION_HZ)
        att = _response_db(taps, _GMRS_SPACING_HZ, _STAGE2_RATE)
        self.assertLess(
            att, -30.0,
            "ground channel_bw_hz=%.0f leaves the 12.5 kHz neighbour only "
            "%.1f dB down -- a single key-up will open its neighbours" % (bw, att))

    def test_ground_still_passes_wideband_nfm_voice(self):
        """Rejection must not be bought by clipping the signal.

        Carson for wideband GMRS: 2*(5 kHz deviation + 3 kHz audio) = 16 kHz,
        so +-8 kHz must survive. Check the band that carries the voice.
        """
        bw = _cfg("ground")["channel_bw_hz"]
        taps = _firdes_low_pass(1.0, _STAGE2_RATE, bw, _TRANSITION_HZ)
        for f in (0.0, 2_500.0, 5_000.0, 6_500.0):
            att = _response_db(taps, f, _STAGE2_RATE)
            self.assertGreater(
                att, -3.0,
                "%.0f Hz is %.1f dB down -- cutoff %.0f is clipping NFM voice"
                % (f, att, bw))


class PinnedPerBandTests(unittest.TestCase):
    def test_every_band_config_pins_channel_bw(self):
        """Inheriting the default is the bug. Both bands must say it out loud."""
        for band in ("airband", "ground"):
            with self.subTest(band=band):
                self.assertIn(
                    "channel_bw_hz", _cfg(band),
                    "%s.json must pin channel_bw_hz rather than inherit "
                    "daemon.py's 12500.0 default" % band)

    def test_ground_is_narrow_enough_for_a_12_5_khz_grid(self):
        bw = _cfg("ground")["channel_bw_hz"]
        self.assertLess(
            bw + _TRANSITION_HZ, _GMRS_SPACING_HZ,
            "cutoff + transition (%.0f) must fall below the %.0f Hz channel "
            "spacing so the neighbour's centre is in the stopband"
            % (bw + _TRANSITION_HZ, _GMRS_SPACING_HZ))

    def test_ground_squelch_hears_the_gmrs15_identifier(self):
        """-56.0 was too tight, and the margin was tiny but decisive.

        A real repeater identifier on 462.5500 bursts for 9.14 s every 30 min
        at median -56.94 dBFS; 84 of 103 logged events sit below -56.0, so that
        threshold missed ~82% of them. This pins the value as a detection
        requirement rather than a magic number, so a future tightening has to
        argue with the measurement.
        """
        sq = _cfg("ground")["global_squelch_dbfs"]
        self.assertLessEqual(
            sq, -58.0,
            "squelch %.1f dBFS is too tight to hear the GMRS 15 identifier "
            "(median -56.94, min -62.88)" % sq)
        self.assertGreaterEqual(
            sq, -70.0,
            "squelch %.1f dBFS is low enough to open on noise; -73.0 was tried "
            "on 2026-11-03 and abandoned" % sq)

    def test_daemon_default_is_documented_as_one_sided(self):
        """The old comment called 12500 the "full 25 kHz AM channel", which
        reads as a width and makes the default look safe on any grid."""
        src = (_ROOT / "chirp" / "daemon.py").read_text(encoding="utf-8")
        head = src.split("channel_bw_hz: float = 12500.0")[0][-1400:]
        self.assertIn("HALF the demod bandwidth", head,
                      "daemon.py must say the value is a one-sided cutoff, "
                      "i.e. half the filter width")
        self.assertIn("firdes.low_pass", head,
                      "daemon.py must name where the value actually lands")
        self.assertIn("12.5 kHz grid", head,
                      "daemon.py must warn that the default is wrong on a "
                      "12.5 kHz grid")


if __name__ == "__main__":
    unittest.main()
