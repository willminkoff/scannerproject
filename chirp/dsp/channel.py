"""chirp.dsp.channel — Single-channel AM demod hier_block (Phase 1).

Per-channel pipeline mirrors ham2mon `TunerDemodAM` but:

  - drops the integrated wav-file sink (daemon owns audio routing),
  - outputs a single float-32 audio stream at a fixed `audio_rate` (default 16 kHz),
  - exposes hot setters and probes wired to the design-doc command list,
  - the AM demod is `blocks.complex_to_mag` (envelope detector) — same as
    ham2mon's choice for N>2 parallel demods.

Input:  complex64 baseband at `samp_rate` sps (>= 1 Msps).
Output: float32 audio at `audio_rate` sps (default 16 kHz).

Hot setters:
    set_center_freq_offset(hz) — retune the per-channel xlating filter
    set_squelch(dbfs)          — pwr_squelch_cc threshold
    set_gain(db)               — post-demod gain (multiplies AGC reference)

Probes:
    get_signal_level_dbfs()    — running avg magnitude squared, in dBFS
    get_squelch_open()         — True if the squelch is currently passing audio
    is_squelch_open()          — alias used by daemon status snapshots

See SDR_DEMOD_DESIGN_2026-06-03.md Section 4. The block does not touch any
output sink — the daemon wires Channel → file/null/adder as appropriate.
"""

from __future__ import annotations

import math
from typing import Optional

from gnuradio import analog, blocks, gr
from gnuradio import filter as grfilter
from gnuradio.fft import window
from gnuradio.filter import pfb


class Channel(gr.hier_block2):
    """Single-channel AM demodulator hier_block.

    Args:
        samp_rate: baseband input sample rate, sps. Must be >= 1e6 and a
            multiple of 1e6 to match ham2mon's decimation chain assumptions.
        audio_rate: mono float audio output sample rate, Hz. Default 16000.
        channel_bw_hz: per-channel post-decimation LPF cutoff (Hz). Default
            12.5 kHz, matching the design doc 25 kHz spacing assumption.
        audio_bw_hz: audio LPF cutoff after demod (Hz). Default 3.5 kHz.
        center_freq_offset: initial xlating filter offset, Hz.
        squelch_dbfs: initial pwr_squelch threshold, dBFS.
        gain_db: initial post-demod gain (dB).
    """

    # AGC reference at 0 dB gain. Matches ham2mon's TunerDemodAM default.
    _AGC_REF_0DB = 0.1

    def __init__(
        self,
        samp_rate: float,
        audio_rate: float = 16000.0,
        channel_bw_hz: float = 12.5e3,
        audio_bw_hz: float = 3.5e3,
        center_freq_offset: float = 0.0,
        squelch_dbfs: float = -60.0,
        gain_db: float = 0.0,
    ) -> None:
        super().__init__(
            "ChirpChannelAM",
            gr.io_signature(1, 1, gr.sizeof_gr_complex),
            gr.io_signature(1, 1, gr.sizeof_float),
        )

        self._samp_rate = float(samp_rate)
        self._audio_rate = float(audio_rate)
        self._channel_bw_hz = float(channel_bw_hz)
        self._audio_bw_hz = float(audio_bw_hz)
        self._center_freq_offset = float(center_freq_offset)
        self._squelch_dbfs = float(squelch_dbfs)
        self._gain_db = float(gain_db)

        # --- Decimation plan (matches ham2mon TunerDemodAM) -----------------
        # Stage 0: freq_xlating decim 5  (samp_rate -> samp_rate/5)
        # Stage 1: fir_filter_ccc decim 5 (samp_rate/5 -> samp_rate/25)
        # Stage 2: fir_filter_ccc decim int(samp_rate/1e6) (-> ~40 ksps)
        # Stage 3: fir_filter_fff decim 5 (audio LPF, -> ~8 ksps)
        # Stage 4: pfb arb_resampler to audio_rate (e.g. -> 16 ksps).
        if samp_rate < 1e6:
            raise ValueError("Channel requires samp_rate >= 1 Msps")
        decim_stage2 = max(1, int(round(samp_rate / 1e6)))
        decims = (5, decim_stage2)

        # Low-pass taps for the (samp_rate/5) -> (samp_rate/25) stages.
        taps_stage_0 = grfilter.firdes.low_pass(
            1.0, 1.0, 0.090, 0.010, window.WIN_HAMMING
        )

        # Stage 0: freq-xlating + decim 5.
        self.freq_xlating = grfilter.freq_xlating_fir_filter_ccc(
            decims[0], taps_stage_0, self._center_freq_offset, self._samp_rate
        )

        # Stage 1: FIR decim 5.
        self.fir_stage1 = grfilter.fir_filter_ccc(decims[0], taps_stage_0)

        # Stage 2: channel-bandwidth LPF + decim by ~int(samp_rate/1e6).
        # Cutoff = channel_bw_hz (default 12.5 kHz), transition 1 kHz.
        taps_stage_2 = grfilter.firdes.low_pass(
            1.0,
            self._samp_rate / decims[0] ** 2,
            self._channel_bw_hz,
            1e3,
            window.WIN_HAMMING,
        )
        self.fir_stage2 = grfilter.fir_filter_ccc(decims[1], taps_stage_2)

        # Power squelch (non-blocking — we want to keep samples flowing so
        # parallel channels stay in lock-step in a future adder).
        self.pwr_squelch = analog.pwr_squelch_cc(
            self._squelch_dbfs, 1e-1, 0, False
        )

        # AGC. agc3_cc(attack, decay, reference, gain_init, max_gain_floor).
        self.agc = analog.agc3_cc(1.0, 1e-4, self._AGC_REF_0DB, 10, 1)
        self.agc.set_max_gain(65536)

        # AM envelope detector. complex_to_mag is the ham2mon-tested choice
        # for N parallel AM channels (analog.am_demod_cf doesn't scale to N>2).
        self.am_demod = blocks.complex_to_mag(1)

        # Audio LPF + decim 5 (down to ~8 ksps before the arb-resampler).
        taps_audio = grfilter.firdes.low_pass(
            1.0,
            self._samp_rate / (decims[1] * decims[0] ** 2),
            self._audio_bw_hz,
            500.0,
            window.WIN_HAMMING,
        )
        self.audio_lpf = grfilter.fir_filter_fff(decims[0], taps_audio)

        # Arb resampler → audio_rate.
        pre_resamp_rate = self._samp_rate / (decims[1] * decims[0] ** 3)
        resamp_ratio = self._audio_rate / pre_resamp_rate
        self.audio_resamp = pfb.arb_resampler_fff(
            resamp_ratio, taps=None, flt_size=32
        )

        # Signal-level probe (running avg of |x|^2 at the post-decim point —
        # i.e. just before the squelch — so the operator sees what the squelch
        # sees). alpha = 1e-1 matches the squelch's averaging.
        self.level_probe = analog.probe_avg_mag_sqrd_c(0.0, 1e-1)

        # --- Wiring ---------------------------------------------------------
        self.connect(self, self.freq_xlating)
        self.connect(self.freq_xlating, self.fir_stage1)
        self.connect(self.fir_stage1, self.fir_stage2)
        # tee fir_stage2 -> squelch (audio path) and -> level_probe
        self.connect(self.fir_stage2, self.pwr_squelch)
        self.connect(self.fir_stage2, self.level_probe)
        self.connect(self.pwr_squelch, self.agc)
        self.connect(self.agc, self.am_demod)
        self.connect(self.am_demod, self.audio_lpf)
        self.connect(self.audio_lpf, self.audio_resamp)
        self.connect(self.audio_resamp, self)

        # Apply gain (must come after agc is wired).
        self._apply_gain(self._gain_db)

    # ----- Hot setters -----------------------------------------------------

    def set_center_freq_offset(self, hz: float) -> None:
        self._center_freq_offset = float(hz)
        self.freq_xlating.set_center_freq(self._center_freq_offset)

    def set_squelch(self, dbfs: float) -> None:
        self._squelch_dbfs = float(dbfs)
        self.pwr_squelch.set_threshold(self._squelch_dbfs)

    def set_gain(self, db: float) -> None:
        self._gain_db = float(db)
        self._apply_gain(self._gain_db)

    def _apply_gain(self, db: float) -> None:
        ref = self._AGC_REF_0DB * (10.0 ** (db / 20.0))
        self.agc.set_reference(ref)

    # ----- Probes ----------------------------------------------------------

    def get_signal_level_dbfs(self) -> float:
        """Running average magnitude-squared expressed in dBFS.

        Fullscale = magnitude 1.0 → 0 dBFS. We add a small floor so log10 of
        absolute silence returns -120 dBFS instead of -inf.
        """
        m2 = float(self.level_probe.level())
        if m2 <= 0.0:
            return -120.0
        return 10.0 * math.log10(max(m2, 1e-12))

    def get_squelch_open(self) -> bool:
        return bool(self.pwr_squelch.unmuted())

    is_squelch_open = get_squelch_open  # convenience alias

    # ----- Introspection ---------------------------------------------------

    @property
    def samp_rate(self) -> float:
        return self._samp_rate

    @property
    def audio_rate(self) -> float:
        return self._audio_rate

    @property
    def center_freq_offset(self) -> float:
        return self._center_freq_offset

    @property
    def squelch_dbfs(self) -> float:
        return self._squelch_dbfs

    @property
    def gain_db(self) -> float:
        return self._gain_db

    def snapshot(self) -> dict:
        """Return a JSON-serializable snapshot of this channel's state."""
        return {
            "center_freq_offset_hz": self._center_freq_offset,
            "squelch_dbfs": self._squelch_dbfs,
            "gain_db": self._gain_db,
            "audio_rate": self._audio_rate,
            "samp_rate": self._samp_rate,
            "channel_bw_hz": self._channel_bw_hz,
            "audio_bw_hz": self._audio_bw_hz,
            "signal_level_dbfs": self.get_signal_level_dbfs(),
            "squelch_open": self.get_squelch_open(),
        }


__all__ = ["Channel"]
