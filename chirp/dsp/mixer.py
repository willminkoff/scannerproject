"""chirp.dsp.mixer — N-into-1 audio mixer with master gain.

Phase 2. Combines N parallel float-32 audio streams from `Channel` blocks into
a single float-32 stream, then applies a master gain trim.

Design rationale (see SDR_DEMOD_DESIGN_2026-06-03.md §4.3):

  - Empty pool slots still need a stream wired (GR flowgraph topology is
    fixed at start()). We provide a built-in null_source per slot that the
    daemon connects when no Channel is bound. Parked Channels (squelch
    slammed shut) ALSO contribute ~zero, so either wiring strategy works.
  - `add_ff` is the natural N-into-1 mixer in GR. It takes vlen=1 floats.
  - A `multiply_const_ff` post-mixer realises master gain (dB → linear).

This block is a thin hier_block2 so the daemon can hold it as a single
object and call `set_master_gain(db)` at runtime.

Topology:

    in_0  ─┐
    in_1  ─┤
     ...   ├─► add_ff ─► multiply_const_ff ─► out
    in_N-1 ─┘
"""

from __future__ import annotations

import math
from typing import List

from gnuradio import blocks, gr


class AudioMixer(gr.hier_block2):
    """N-input float mixer with master gain.

    Args:
        n_inputs: number of input ports the mixer exposes.
        master_gain_db: initial master gain in dB. Clamped to [-20, 40].
    """

    def __init__(self, n_inputs: int, master_gain_db: float = 0.0) -> None:
        if n_inputs < 1:
            raise ValueError("n_inputs must be >= 1")
        super().__init__(
            "ChirpAudioMixer",
            gr.io_signature(n_inputs, n_inputs, gr.sizeof_float),
            gr.io_signature(1, 1, gr.sizeof_float),
        )
        self._n_inputs = int(n_inputs)
        self._master_gain_db = float(master_gain_db)

        # add_ff implicitly handles 1..N inputs via connect() with port indices.
        self.adder = blocks.add_ff()
        self.master = blocks.multiply_const_ff(self._db_to_linear(master_gain_db))

        for i in range(self._n_inputs):
            # External input port i of self → input port i of the adder.
            self.connect((self, i), (self.adder, i))
        self.connect(self.adder, self.master, self)

    # -- master gain --------------------------------------------------------

    @staticmethod
    def _db_to_linear(db: float) -> float:
        # Clamp wildly out-of-range values defensively (the schema also
        # enforces [-20, 40] but the mixer is allowed to be called from tests).
        db = max(-60.0, min(60.0, float(db)))
        return 10.0 ** (db / 20.0)

    def set_master_gain(self, db: float) -> None:
        self._master_gain_db = float(db)
        self.master.set_k(self._db_to_linear(self._master_gain_db))

    @property
    def master_gain_db(self) -> float:
        return self._master_gain_db

    @property
    def n_inputs(self) -> int:
        return self._n_inputs


class NullAudioSource(gr.hier_block2):
    """Float zero-stream source. Used when a pool slot is empty and we still
    need to wire SOMETHING to the mixer to keep the flowgraph well-formed.

    In the current daemon we pre-allocate Channels for every slot and just
    park them with squelch=0 dBFS (which zeros their audio output), so the
    mixer's inputs are always real Channels. NullAudioSource is retained as
    a small utility for tests that want a known-zero feed without standing
    up a full Channel.
    """

    def __init__(self) -> None:
        super().__init__(
            "ChirpNullAudio",
            gr.io_signature(0, 0, 0),
            gr.io_signature(1, 1, gr.sizeof_float),
        )
        self._src = blocks.null_source(gr.sizeof_float)
        # Throttle so the null stream doesn't run flat-out CPU.
        # 48 kHz is comfortably above any audio_rate we use; it merely paces.
        self._throttle = blocks.throttle(gr.sizeof_float, 48000.0, True)
        self.connect(self._src, self._throttle, self)


__all__ = ["AudioMixer", "NullAudioSource"]
