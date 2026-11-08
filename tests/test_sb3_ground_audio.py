"""The Ground player was the only stream with no self-healing.

``bindAudioAutoRecover`` watches an <audio> element for a stall and reloads
the stream. It was wired for Analog and Digital only, so a Ground stall was
permanent until the operator reloaded the page -- which is the shape of the
2026-11-08 report that ground playback "is more than 30 seconds late": once it
fell behind there was nothing to pull it back.

Binding it was not a one-liner, because the audio layer assumed exactly TWO
players. ``isDigitalAudioElement(el) ? 'digital' : 'analog'`` filed Ground
under 'analog', so the two bands shared one ``userActivatedEmbedAudio`` flag
and one another's pause state, and the recovery timings keyed off
``isAnalogAudioElement`` gave Ground the digital (HLS) grace periods even
though it is a 32 kbps icecast MP3 exactly like Analog.

grep-level on purpose: there is no JS test harness in this repo, and each of
these regressions is visible as a single wrong token. The companion check
that the file still parses is `node --check` over the inline <script> blocks.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

_HTML = Path(__file__).resolve().parents[1] / "ui" / "sb3.html"


class GroundPlayerBindingTests(unittest.TestCase):
    def setUp(self):
        self.src = _HTML.read_text(encoding="utf-8")

    def test_all_three_players_are_bound(self):
        bound = set(re.findall(r"bindAudioAutoRecover\(els\.(audioPlayer\w+)", self.src))
        self.assertEqual(
            bound, {"audioPlayerAnalog", "audioPlayerGround", "audioPlayerDigital"},
            "every player must self-heal; found %s" % sorted(bound))

    def test_ground_has_a_reload_function(self):
        self.assertRegex(self.src, r"function reloadGroundStream\(")

    def test_ground_reload_targets_the_ground_element_and_state_key(self):
        m = re.search(r"function reloadGroundStream\([^)]*\)\s*\{(.*?)\n    \}", self.src, re.S)
        self.assertIsNotNone(m, "reloadGroundStream() missing")
        body = m.group(1)
        self.assertIn("els.audioPlayerGround", body)
        self.assertIn("groundStreamUrl()", body)
        self.assertIn("'groundStreamBaseUrl'", body,
                      "must use ground's own state key, not analog's")


class ThreeWayKindTests(unittest.TestCase):
    def setUp(self):
        self.src = _HTML.read_text(encoding="utf-8")

    def test_no_two_way_audio_kind_ternary_remains(self):
        """This expression is what filed Ground under 'analog'."""
        hits = re.findall(r"isDigitalAudioElement\(audioEl\)\s*\?\s*'digital'\s*:\s*'analog'",
                          self.src)
        self.assertEqual(hits, [], "%d two-way kind ternary/ies left" % len(hits))

    def test_kind_resolver_knows_all_three(self):
        m = re.search(r"function audioKindFor\(audioEl\)\s*\{(.*?)\n    \}", self.src, re.S)
        self.assertIsNotNone(m, "audioKindFor() missing")
        body = m.group(1)
        for kind in ("'digital'", "'ground'", "'analog'"):
            self.assertIn(kind, body)

    def test_activation_map_has_a_ground_slot(self):
        m = re.search(r"userActivatedEmbedAudio:\s*\{([^}]*)\}", self.src)
        self.assertIsNotNone(m)
        for kind in ("analog", "ground", "digital"):
            self.assertIn(kind, m.group(1),
                          "without its own slot, Ground shares analog's flag")

    def test_label_distinguishes_ground(self):
        m = re.search(r"function audioTargetLabel\(audioEl\)\s*\{(.*?)\n    \}", self.src, re.S)
        self.assertIsNotNone(m)
        self.assertIn("'Ground'", m.group(1))

    def test_binder_does_not_rederive_the_label(self):
        """It had its own copy of the two-way label expression."""
        m = re.search(r"function bindAudioAutoRecover\([^)]*\)\s*\{(.*?)\n    \}", self.src, re.S)
        self.assertIsNotNone(m, "bindAudioAutoRecover() missing")
        self.assertIn("audioTargetLabel(audioEl)", m.group(1))


class GroundHealthTests(unittest.TestCase):
    def setUp(self):
        self.src = _HTML.read_text(encoding="utf-8")

    def test_ground_has_its_own_health_check(self):
        """The pause-vs-drop decision needs to know if ground's mount is up;
        without it, a dropped ground stream reads as a deliberate pause."""
        self.assertRegex(self.src, r"function groundStreamHealthyForPlayer\(")

    def test_health_resolver_covers_all_three(self):
        m = re.search(r"function audioStreamHealthyForPlayer\(audioEl\)\s*\{(.*?)\n    \}",
                      self.src, re.S)
        self.assertIsNotNone(m, "audioStreamHealthyForPlayer() missing")
        body = m.group(1)
        for fn in ("digitalStreamHealthyForPlayer",
                   "groundStreamHealthyForPlayer",
                   "analogStreamHealthyForPlayer"):
            self.assertIn(fn, body)

    def test_pause_path_uses_the_resolver(self):
        self.assertNotRegex(
            self.src,
            r"isAnalog\s*\?\s*analogStreamHealthyForPlayer\(\)\s*:\s*digitalStreamHealthyForPlayer\(\)",
            "the two-way health ternary cannot classify Ground")

    def test_mount_name_is_defined_once(self):
        """Three copies of the literal is how a rename breaks the health check
        while leaving the player apparently working."""
        self.assertRegex(self.src, r"const GROUND_STREAM_MOUNT = 'neptune-ground\.mp3'")
        literals = self.src.count("'neptune-ground.mp3'")
        self.assertLessEqual(
            literals, 2,
            "expected the mount literal only in its const (plus at most one "
            "fallback); found %d" % literals)

    def test_ground_gets_the_low_bitrate_recovery_timings(self):
        """Ground is a 32 kbps icecast MP3 like Analog, not HLS like Digital.
        Keying these off isAnalogAudioElement handed it Digital's grace."""
        for fn in ("audioRecoverCooldownMs", "audioWaitingGraceMs",
                   "audioStartupRecoverGraceMs"):
            m = re.search(r"function %s\(audioEl\)\s*\{(.*?)\n    \}" % fn, self.src, re.S)
            with self.subTest(fn=fn):
                self.assertIsNotNone(m, "%s() missing" % fn)
                self.assertIn("isDigitalAudioElement(audioEl)", m.group(1),
                              "%s must branch on digital, so analog AND ground "
                              "both get the MP3 timings" % fn)


if __name__ == "__main__":
    unittest.main()
