"""GR sync block: per-channel audio gate using voice-activity detection.

Phase 2 of the SB5 squelch redesign.  Replaces the power-only RF squelch
with an audio-domain gate that mutes blocks of audio whose VAD score
falls below a configurable threshold.

Behaviour:
  - Scores every 50 ms block of audio with chirp.audio_gate.AudioVAD.
  - If the composite score is at or above ``threshold``, audio passes
    through and a 200 ms hold-open is armed.
  - Below threshold, the block is replaced with silence.  Hold-open
    counts down across subsequent low-scoring blocks so we don't chop
    voice mid-syllable.

The gate runs at 16 kHz audio rate (default chirp.config audio_rate).
"""
from __future__ import annotations

import threading
import numpy as np
from gnuradio import gr

from chirp.audio_gate import AudioVAD


class AudioVADGate(gr.sync_block):
    """Per-channel audio gate driven by AudioVAD composite score."""

    BLOCK_SAMPLES = 800  # 50 ms at 16 kHz
    HOLD_OPEN_BLOCKS = 4  # 200 ms hold-open after a passing block

    def __init__(
        self,
        audio_rate: float = 16000.0,
        threshold: float = 50.0,
        bypass: bool = False,
    ) -> None:
        gr.sync_block.__init__(
            self,
            name="audio_vad_gate",
            in_sig=[np.float32],
            out_sig=[np.float32],
        )
        # Ensure work() always receives a multiple of BLOCK_SAMPLES so we
        # never have to buffer across calls.  At 16 kHz audio rate the
        # downstream encoder consumes hundreds of blocks per second so
        # this never starves throughput.
        self.set_output_multiple(self.BLOCK_SAMPLES)

        self._audio_rate = float(audio_rate)
        self._vad = AudioVAD(
            audio_rate=audio_rate, block_samples=self.BLOCK_SAMPLES,
        )
        self._lock = threading.Lock()
        self._threshold = float(threshold)
        self._bypass = bool(bypass)
        self._hold_remaining = 0
        self._last_score = 0.0
        self._last_decision_open = False

    # ---- thread-safe setters ------------------------------------------------
    def set_threshold(self, threshold: float) -> None:
        with self._lock:
            self._threshold = float(max(0.0, min(100.0, threshold)))

    def set_bypass(self, bypass: bool) -> None:
        with self._lock:
            self._bypass = bool(bypass)
            # If we just bypassed, drop the hold counter so re-arming starts fresh.
            if self._bypass:
                self._hold_remaining = 0

    # ---- read-only state for status -----------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "threshold": float(self._threshold),
                "bypass": bool(self._bypass),
                "last_score": float(self._last_score),
                "last_open": bool(self._last_decision_open),
                "hold_remaining_blocks": int(self._hold_remaining),
            }

    # ---- GR work -------------------------------------------------------------
    def work(self, input_items, output_items):
        x = input_items[0]
        out = output_items[0]
        n = len(x)
        if n == 0:
            return 0

        with self._lock:
            bypass = self._bypass
            threshold = self._threshold

        if bypass:
            # Straight passthrough — gate is operator-disabled.
            out[:n] = x[:n]
            return n

        # n is guaranteed to be a multiple of BLOCK_SAMPLES thanks to
        # set_output_multiple.  Process block-by-block, score, then either
        # copy or silence.
        for i in range(0, n, self.BLOCK_SAMPLES):
            block = x[i : i + self.BLOCK_SAMPLES]
            score = self._vad.score(np.asarray(block, dtype=np.float32)).composite
            with self._lock:
                if score >= threshold:
                    gate_open = True
                    self._hold_remaining = self.HOLD_OPEN_BLOCKS
                elif self._hold_remaining > 0:
                    gate_open = True
                    self._hold_remaining -= 1
                else:
                    gate_open = False
                self._last_score = float(score)
                self._last_decision_open = bool(gate_open)
            if gate_open:
                out[i : i + self.BLOCK_SAMPLES] = block
            else:
                out[i : i + self.BLOCK_SAMPLES] = 0.0
        return n
