"""Icecast buffering is listener latency, and the Mac bootstrap had it wrong.

The GMRS bench test on 2026-11-08 reported playback running "more than 30
seconds late". Roughly half of that is icecast: a listener's first audio is
the burst icecast replays on connect, so ``burst-size / bitrate`` is a fixed
delay on every mount. chirp publishes at 32 kbps = 4000 B/s.

``scripts/mac-bootstrap.sh`` -- which is what actually provisions Neptune --
set no ``<burst-size>`` at all, so the mount inherited icecast's built-in
65536 default: 16.4 s. It also set ``queue-size`` to 524288, letting a slow
client bank 131 s of backlog rather than being dropped and recovering.

Confusingly, ``icecast/icecast.xml.example`` (the Pi-era config) has always
carried sane values, and both READMEs describe the low-buffer config as
already true. Those claims were about the example, not about the file the Mac
bootstrap writes. These tests hold the generated config to the same standard.

The other half of the delay is the chirp -> lame -> libshout chain buffering
~64 kB in OS pipes. No icecast setting reaches that; see
chirp/dsp/icecast_sink.py.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_BOOTSTRAP = _ROOT / "scripts" / "mac-bootstrap.sh"
_EXAMPLE = _ROOT / "icecast" / "icecast.xml.example"

_BITRATE_KBPS = 32                      # chirp/config/*.json icecast_bitrate_kbps
_BYTES_PER_SEC = _BITRATE_KBPS * 1000 / 8.0
_MAX_BURST_SECONDS = 4.0                # a browser needs frames, not seconds
_MIN_BURST_BYTES = 4096                 # ...but not so few it cannot start


def _tag_int(src, tag):
    m = re.search(r"<%s>\s*(\d+)\s*</%s>" % (tag, tag), src)
    return int(m.group(1)) if m else None


class BootstrapIcecastTests(unittest.TestCase):
    def setUp(self):
        self.src = _BOOTSTRAP.read_text(encoding="utf-8")

    def test_burst_size_is_set_explicitly(self):
        """Omitting it is not neutral -- icecast defaults to 65536 (16.4 s)."""
        self.assertIsNotNone(
            _tag_int(self.src, "burst-size"),
            "mac-bootstrap.sh must set <burst-size>; leaving it out inherits "
            "icecast's 65536 default = %.1f s of latency at %d kbps"
            % (65536 / _BYTES_PER_SEC, _BITRATE_KBPS))

    def test_burst_is_under_a_few_seconds_of_audio(self):
        burst = _tag_int(self.src, "burst-size")
        secs = burst / _BYTES_PER_SEC
        self.assertLessEqual(
            secs, _MAX_BURST_SECONDS,
            "burst-size %d = %.1f s at %d kbps; every listener pays that as "
            "startup delay" % (burst, secs, _BITRATE_KBPS))

    def test_burst_still_primes_a_decoder(self):
        burst = _tag_int(self.src, "burst-size")
        self.assertGreaterEqual(
            burst, _MIN_BURST_BYTES,
            "burst-size %d may be too small for a browser to begin decoding; "
            "burst-on-connect exists so the first frames arrive at once" % burst)

    def test_queue_is_not_a_two_minute_backlog(self):
        q = _tag_int(self.src, "queue-size")
        self.assertIsNotNone(q, "mac-bootstrap.sh must set <queue-size>")
        secs = q / _BYTES_PER_SEC
        self.assertLessEqual(
            secs, 20.0,
            "queue-size %d = %.0f s of per-client backlog; a slow client "
            "should be dropped and reconnect, not bank lag" % (q, secs))

    def test_the_latency_tradeoff_is_written_down(self):
        """The next person to raise these numbers should see the cost."""
        self.assertIn("Buffering is LATENCY", self.src)

    def test_the_other_half_of_the_delay_is_named(self):
        """Tuning icecast alone cannot fix the >30 s report; say so here."""
        self.assertIn("icecast_sink.py", self.src,
                      "the generated config should point at the source-side "
                      "pipe buffering that no icecast setting reaches")


class ExampleConfigTests(unittest.TestCase):
    """The example was already sane; keep it that way so the two don't diverge."""

    def setUp(self):
        self.src = _EXAMPLE.read_text(encoding="utf-8")

    def test_example_burst_is_bounded(self):
        burst = _tag_int(self.src, "burst-size")
        self.assertIsNotNone(burst)
        self.assertLessEqual(burst / _BYTES_PER_SEC, _MAX_BURST_SECONDS)

    def test_example_queue_is_bounded(self):
        q = _tag_int(self.src, "queue-size")
        self.assertIsNotNone(q)
        self.assertLessEqual(q / _BYTES_PER_SEC, 20.0)


if __name__ == "__main__":
    unittest.main()
