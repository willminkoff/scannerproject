"""lame held 4.1 s of audio in libc's stdio buffer, and starved icecast's burst.

Measured on neptune 2026-11-08 with the exact deployed argv:

    lame as deployed     first byte at 4.45 s, stdout chunks of 16384 B
    stdbuf -o0 lame      first byte at 0.10 s, stdout chunks of 144 B

144 B is one MPEG-2 Layer III frame (576 samples at 16 kHz = 36 ms). The live
publisher showed the same thing from the other end: icecast_bytes_sent advanced
in increments of exactly 16384, spaced 4.08/4.15/4.03/4.09/4.05 s apart.

The second-order effect is worse than the latency. icecast replays burst-size
to each new listener, so a source silent for 4 s at a stretch leaves that
buffer empty between clumps: three successive connects to the ground mount
returned 0 B, 19600 B, 0 B. After burst-size was lowered 65535 -> 8192 that
became a startup failure rather than merely a delay.

chirp/dsp/icecast_sink.py imports gnuradio, which is radioconda-only and not
importable on every dev box, so these tests read the source rather than the
module where they must.
"""
from __future__ import annotations

import ast
import io
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

_SINK = Path(__file__).resolve().parents[1] / "chirp" / "dsp" / "icecast_sink.py"
_SRC = io.open(_SINK, encoding="utf-8").read()

_BITRATE_KBPS = 32
_BYTES_PER_SEC = _BITRATE_KBPS * 1000 / 8.0        # 4000
_MP3_FRAME_BYTES = 144                              # MPEG-2 L3, 32 kbps, 16 kHz
_PIPE_STDIO_BUF = 16384                             # macOS pipe st_blksize


def _func_src(name):
    tree = ast.parse(_SRC)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return ast.get_source_segment(_SRC, n) or ""
    return None


class UnbufferPrefixTests(unittest.TestCase):
    def test_helper_exists(self):
        self.assertIsNotNone(_func_src("_unbuffer_prefix"),
                             "_unbuffer_prefix() missing from icecast_sink.py")

    def test_helper_uses_stdbuf_o0(self):
        src = _func_src("_unbuffer_prefix")
        self.assertIn('shutil.which("stdbuf")', src)
        self.assertIn('"-o0"', src,
                      "-o0 is unbuffered; -oL is line buffering and is "
                      "meaningless for binary MP3")

    def test_helper_degrades_to_empty_list(self):
        """Absent stdbuf must not break the encoder."""
        src = _func_src("_unbuffer_prefix")
        self.assertRegex(src, r"if sb else \[\]",
                         "must fall back to no prefix when stdbuf is absent")

    def test_helper_is_importable_without_gnuradio(self):
        """Exec just the helper so the behaviour, not only the text, is tested."""
        ns = {"shutil": shutil}
        exec(compile(_func_src("_unbuffer_prefix"), "<helper>", "exec"), ns)
        got = ns["_unbuffer_prefix"]()
        self.assertIsInstance(got, list)
        if shutil.which("stdbuf"):
            self.assertEqual(len(got), 2)
            self.assertEqual(got[1], "-o0")
            self.assertTrue(got[0].endswith("stdbuf"))
        else:
            self.assertEqual(got, [])


class SpawnLameTests(unittest.TestCase):
    def setUp(self):
        self.src = _func_src("_spawn_lame")
        self.assertIsNotNone(self.src, "_spawn_lame() missing")

    def test_lame_command_is_prefixed(self):
        self.assertIn("_unbuffer_prefix()", self.src,
                      "_spawn_lame must prefix the command, or lame reverts to "
                      "16 KiB stdio batching")

    def test_prefix_comes_first(self):
        """stdbuf must wrap lame, not appear among lame's own arguments."""
        self.assertRegex(self.src, r"cmd\s*=\s*_unbuffer_prefix\(\)\s*\+\s*\[")

    def test_lame_still_gets_its_own_args(self):
        for tok in ('"-r"', '"--cbr"', '"--silent"', '"--bitwidth"', '"--signed"'):
            with self.subTest(tok=tok):
                self.assertIn(tok, self.src)

    def test_ffmpeg_paths_already_had_flush_control(self):
        """The asymmetry this fixes: only the lame path lacked it."""
        self.assertIn('"-flush_packets", "1"', _SRC)


class ArithmeticTests(unittest.TestCase):
    """Pin the numbers the fix is justified by, in units, not magic constants."""

    def test_stdio_buffer_is_about_four_seconds(self):
        secs = _PIPE_STDIO_BUF / _BYTES_PER_SEC
        self.assertAlmostEqual(secs, 4.096, places=3)

    def test_one_frame_is_36ms(self):
        secs = _MP3_FRAME_BYTES / _BYTES_PER_SEC
        self.assertAlmostEqual(secs, 0.036, places=3)

    def test_a_clump_is_more_than_one_burst(self):
        """Why burst-size 8192 starved: one source flush exceeds the whole burst.

        If burst-size is ever raised above a flush this stops being true, but
        the right fix is a streaming source, not a bigger burst.
        """
        burst = 8192
        self.assertGreater(
            _PIPE_STDIO_BUF, burst,
            "a 16 KiB clump is larger than the %d B burst, so the burst buffer "
            "sits empty between clumps" % burst)


@unittest.skipUnless(shutil.which("stdbuf") and shutil.which("lame"),
                     "needs both stdbuf and lame on PATH")
class LiveEncoderTests(unittest.TestCase):
    """Actually run lame both ways and compare. Skipped where either is absent."""

    ARGS = ["-r", "-s", "16.0", "--bitwidth", "16", "--signed",
            "-m", "m", "-q", "5", "--cbr", "-b", "32", "--silent", "-", "-"]

    def _first_chunk(self, cmd, budget_s=6.0):
        import threading
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, bufsize=0)
        t0 = time.time()

        def feed():
            try:
                for _ in range(int(budget_s * 10) + 20):
                    p.stdin.write(b"\x00" * 3200)   # 0.1 s at 16 kHz mono s16
                    time.sleep(0.1)
            except Exception:
                pass
        threading.Thread(target=feed, daemon=True).start()
        try:
            data = p.stdout.read(65536)
            dt = time.time() - t0
            return dt, len(data)
        finally:
            try:
                p.kill(); p.wait(timeout=5)
            except Exception:
                pass

    def test_unbuffered_first_byte_is_much_sooner(self):
        lame = shutil.which("lame")
        sb = shutil.which("stdbuf")
        t_buf, n_buf = self._first_chunk([lame] + self.ARGS)
        t_unb, n_unb = self._first_chunk([sb, "-o0", lame] + self.ARGS)
        self.assertLess(
            t_unb, t_buf,
            "stdbuf -o0 must deliver sooner (buffered %.2fs/%dB vs "
            "unbuffered %.2fs/%dB)" % (t_buf, n_buf, t_unb, n_unb))
        self.assertLess(
            t_unb, 1.5,
            "unbuffered first byte took %.2fs; expected well under a second" % t_unb)

    def test_unbuffered_chunks_are_frame_sized(self):
        sb, lame = shutil.which("stdbuf"), shutil.which("lame")
        _, n = self._first_chunk([sb, "-o0", lame] + self.ARGS)
        self.assertLess(
            n, _PIPE_STDIO_BUF,
            "unbuffered read returned %d B; a full %d B stdio clump means "
            "stdbuf did not take effect" % (n, _PIPE_STDIO_BUF))


if __name__ == "__main__":
    unittest.main()
