"""chirp.dsp.source_file — File-backed IQ source (complex64, looped, throttled).

For dev/test without contending with the real SDR. The Phase-1 smoke test uses
this to play back a synthesized AM-modulated IQ file at a known sample rate.

Wire format on disk: interleaved float32 I/Q pairs (GNU Radio's native fc32 /
`numpy.complex64`). Generate fixtures with `numpy.tofile` or via the
`make_test_iq.py` helper shipped under chirp/tests/fixtures/.
"""

from __future__ import annotations

from pathlib import Path

from gnuradio import blocks, gr


class FileIQSource(gr.hier_block2):
    """A looped, throttled complex IQ source.

    Output: complex64 stream at `samp_rate` samples/sec.

    Args:
        path: absolute path to an interleaved-float32 IQ file.
        samp_rate: stream sample rate; throttle pins playback to this rate so
            downstream sees data at real-time, not flat-out disk speed.
        repeat: loop the file when it ends (True for soak tests, False if you
            want the daemon to observe EOF and shut down).
    """

    def __init__(self, path: str | Path, samp_rate: float, repeat: bool = True) -> None:
        super().__init__(
            "FileIQSource",
            gr.io_signature(0, 0, 0),
            gr.io_signature(1, 1, gr.sizeof_gr_complex),
        )
        self._path = str(Path(path))
        self._samp_rate = float(samp_rate)
        self._src = blocks.file_source(gr.sizeof_gr_complex, self._path, repeat)
        self._throttle = blocks.throttle(gr.sizeof_gr_complex, self._samp_rate, True)
        self.connect(self._src, self._throttle, self)

    # -- introspection ------------------------------------------------------

    @property
    def path(self) -> str:
        return self._path

    @property
    def samp_rate(self) -> float:
        return self._samp_rate


__all__ = ["FileIQSource"]
