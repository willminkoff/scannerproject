"""chirp.dsp.channel — Single-channel AM / NFM demod hier_block.

Phase 1 introduced this block for AM (airband). Phase 4a extends it with an
NFM mode for the ground band (mil-air narrowband FM). Mode is selected at
construction time and is **immutable per channel** — locked decision from
SDR_DEMOD_DESIGN_2026-06-03.md (changing mode would require retearing the
flowgraph wiring).

AM chain (mirrors ham2mon TunerDemodAM, see Phase 1 docstring):
    xlating(decim 5) -> fir(decim 5) -> fir(decim ~samp_rate/1e6)
        -> pwr_squelch -> agc -> complex_to_mag (envelope)
        -> audio_lpf(decim 5) -> arb_resampler -> audio_out

NFM chain (Phase 4a):
    xlating(decim 5) -> fir(decim 5) -> fir(decim ~samp_rate/1e6)
        -> pwr_squelch
        -> quadrature_demod(gain = samp_rate_pre / (2*pi*max_dev))
        -> audio_lpf(decim 5) -> arb_resampler -> audio_out

For mil-air narrowband FM the canonical max deviation is ~5 kHz and the
audio LPF is at ~3.5 kHz. We deliberately do NOT apply 75 us de-emphasis --
narrowband FM voice channels (per rtl-airband's `ground` and mil-air NFM
practice) don't pre-emphasise.

Input:  complex64 baseband at `samp_rate` sps (>= 1 Msps).
Output: float32 audio at `audio_rate` sps (default 16 kHz).

Hot setters (work the same for AM and NFM):
    set_center_freq_offset(hz), set_squelch(dbfs), set_gain(db)

Probes:
    get_signal_level_dbfs(), get_squelch_open() / is_squelch_open()
"""

from __future__ import annotations

import math
from typing import Literal, Optional

from gnuradio import analog, blocks, gr
from gnuradio import filter as grfilter
from gnuradio.fft import window
from gnuradio.filter import pfb


# Modes the channel block supports. Phase 4a adds "nfm".
ChannelMode = Literal["am", "nfm"]


class Channel(gr.hier_block2):
    """Single-channel AM or NFM demodulator hier_block.

    Args:
        samp_rate: baseband input sample rate, sps. Must be >= 1e6 and a
            multiple of 1e6 to match the decimation chain assumptions.
        audio_rate: mono float audio output sample rate, Hz. Default 16000.
        channel_bw_hz: per-channel post-decimation LPF cutoff (Hz). Default
            12.5 kHz (matches design doc 25 kHz spacing assumption).
        audio_bw_hz: audio LPF cutoff after demod (Hz). Default 3.5 kHz.
        center_freq_offset: initial xlating filter offset, Hz.
        squelch_dbfs: initial pwr_squelch threshold, dBFS.
        gain_db: initial post-demod gain (dB).
        mode: "am" (default) or "nfm". Immutable after construction.
        nfm_max_deviation_hz: peak FM deviation for NFM gain calibration.
            Default 5 kHz (mil-air narrowband). Only consulted when mode=="nfm".
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
        mode: ChannelMode = "am",
        nfm_max_deviation_hz: float = 5e3,
    ) -> None:
        if mode not in ("am", "nfm"):
            raise ValueError(f"unsupported mode: {mode!r} (want 'am' or 'nfm')")

        super().__init__(
            f"ChirpChannel{mode.upper()}",
            gr.io_signature(1, 1, gr.sizeof_gr_complex),
            gr.io_signature(1, 1, gr.sizeof_float),
        )

        self._mode: ChannelMode = mode
        self._samp_rate = float(samp_rate)
        self._audio_rate = float(audio_rate)
        self._channel_bw_hz = float(channel_bw_hz)
        self._audio_bw_hz = float(audio_bw_hz)
        self._center_freq_offset = float(center_freq_offset)
        self._squelch_dbfs = float(squelch_dbfs)
        self._gain_db = float(gain_db)
        self._nfm_max_deviation_hz = float(nfm_max_deviation_hz)

        # Phase 4-pre: LO scheduler parks channels that are NOT in the
        # currently-tuned cluster.  A parked channel:
        #   * has its pwr_squelch slammed to PARKED_SQUELCH_DBFS (0.0 dBFS),
        #     which closes the gate hard so audio output ≈ 0 (mixer
        #     contribution is silent — no audio contamination);
        #   * remembers the operator's intended squelch threshold so
        #     ``set_parked(False)`` can restore it;
        #   * accepts ``set_squelch(x)`` from the operator while parked —
        #     the new value is stashed (will be applied on unpark) and is
        #     NOT written to the live pwr_squelch (which would un-silence
        #     a parked channel).
        # ``set_center_freq_offset`` and ``set_gain`` ARE applied even while
        # parked: they don't break silencing, and they let the operator
        # retune a channel while its cluster is dormant.
        self._is_parked: bool = False
        # Squelch value to restore on unpark. Starts as the constructor's
        # squelch_dbfs so the channel can be parked-then-unparked at any
        # time without losing the configured threshold.
        self._parked_saved_squelch_dbfs: float = self._squelch_dbfs

        if samp_rate < 1e6:
            raise ValueError("Channel requires samp_rate >= 1 Msps")
        decim_stage2 = max(1, int(round(samp_rate / 1e6)))
        decims = (5, decim_stage2)

        # Low-pass taps for the (samp_rate/5) -> (samp_rate/25) stages.
        taps_stage_0 = grfilter.firdes.low_pass(
            1.0, 1.0, 0.090, 0.010, window.WIN_HAMMING
        )

        # --- Front-end (identical for AM and NFM) ---------------------------
        self.freq_xlating = grfilter.freq_xlating_fir_filter_ccc(
            decims[0], taps_stage_0, self._center_freq_offset, self._samp_rate
        )
        self.fir_stage1 = grfilter.fir_filter_ccc(decims[0], taps_stage_0)

        taps_stage_2 = grfilter.firdes.low_pass(
            1.0,
            self._samp_rate / decims[0] ** 2,
            self._channel_bw_hz,
            1e3,
            window.WIN_HAMMING,
        )
        self.fir_stage2 = grfilter.fir_filter_ccc(decims[1], taps_stage_2)

        # Power squelch (non-blocking).
        self.pwr_squelch = analog.pwr_squelch_cc(
            self._squelch_dbfs, 1e-1, 0, False
        )

        # Pre-demod sample rate at the squelch output. Used for the audio LPF
        # taps and for the FM discriminator gain.
        pre_demod_rate = self._samp_rate / (decims[1] * decims[0] ** 2)

        # Signal-level probe (running avg |x|^2 at the post-decim point --
        # what the squelch sees). Wired regardless of mode.
        self.level_probe = analog.probe_avg_mag_sqrd_c(0.0, 1e-1)

        # --- Mode-specific demod --------------------------------------------
        if mode == "am":
            # AGC. agc3_cc(attack, decay, reference, gain_init, max_gain_floor).
            self.agc = analog.agc3_cc(1.0, 1e-4, self._AGC_REF_0DB, 10, 1)
            self.agc.set_max_gain(65536)
            # AM envelope detector (ham2mon's choice for N>2 parallel demods).
            self.am_demod = blocks.complex_to_mag(1)
            # NFM members exist as None so introspection code can branch
            # cleanly without hasattr() games.
            self.quad_demod = None
            self.nfm_audio_gain = None
        else:  # nfm
            # No AGC in the NFM path -- FM is amplitude-insensitive after the
            # discriminator, so the AGC would just chase noise floor.
            self.agc = None
            self.am_demod = None
            # quadrature_demod gain = pre_demod_rate / (2*pi*max_deviation).
            # Output is normalised to ~[-1, +1] for a max-deviation signal.
            quad_gain = pre_demod_rate / (2.0 * math.pi * self._nfm_max_deviation_hz)
            self.quad_demod = analog.quadrature_demod_cf(quad_gain)
            # Post-demod amplitude trim (replaces AGC's role in AM path).
            # set_gain() multiplies this scalar so set_gain() behaviour is
            # identical between modes from the operator's perspective.
            self.nfm_audio_gain = blocks.multiply_const_ff(1.0)

        # --- Audio LPF + decim (same shape for AM and NFM) ------------------
        taps_audio = grfilter.firdes.low_pass(
            1.0,
            pre_demod_rate,
            self._audio_bw_hz,
            500.0,
            window.WIN_HAMMING,
        )
        self.audio_lpf = grfilter.fir_filter_fff(decims[0], taps_audio)

        # Arb resampler -> audio_rate.
        post_audio_lpf_rate = pre_demod_rate / decims[0]
        resamp_ratio = self._audio_rate / post_audio_lpf_rate
        self.audio_resamp = pfb.arb_resampler_fff(
            resamp_ratio, taps=None, flt_size=32
        )

        # --- Wiring ---------------------------------------------------------
        self.connect(self, self.freq_xlating)
        self.connect(self.freq_xlating, self.fir_stage1)
        self.connect(self.fir_stage1, self.fir_stage2)
        # tee fir_stage2 -> squelch (audio path) and -> level_probe
        self.connect(self.fir_stage2, self.pwr_squelch)
        self.connect(self.fir_stage2, self.level_probe)

        if mode == "am":
            self.connect(self.pwr_squelch, self.agc)
            self.connect(self.agc, self.am_demod)
            self.connect(self.am_demod, self.audio_lpf)
        else:  # nfm
            self.connect(self.pwr_squelch, self.quad_demod)
            self.connect(self.quad_demod, self.nfm_audio_gain)
            self.connect(self.nfm_audio_gain, self.audio_lpf)

        self.connect(self.audio_lpf, self.audio_resamp)
        self.connect(self.audio_resamp, self)

        # Apply gain (after demod wiring is complete).
        self._apply_gain(self._gain_db)

    # ----- Hot setters -----------------------------------------------------

    def set_center_freq_offset(self, hz: float) -> None:
        self._center_freq_offset = float(hz)
        self.freq_xlating.set_center_freq(self._center_freq_offset)

    def set_squelch(self, dbfs: float) -> None:
        """Set squelch threshold (dBFS).

        Phase 4-pre: when the channel is parked, the operator's value is
        stashed in ``_parked_saved_squelch_dbfs`` so it's applied on
        :py:meth:`set_parked(False)`, but the live pwr_squelch stays
        slammed at PARKED_SQUELCH_DBFS so audio remains silent.
        """
        new_val = float(dbfs)
        self._squelch_dbfs = new_val
        if self._is_parked:
            # Stash for restore-on-unpark; do NOT touch the live gate.
            self._parked_saved_squelch_dbfs = new_val
            return
        self.pwr_squelch.set_threshold(new_val)

    # -- Phase 4-pre: park/unpark for LO scheduler --------------------------

    # Squelch threshold used to slam the gate shut while parked.  0 dBFS =
    # the gate is closed unless input exceeds full-scale (i.e. never).
    _PARKED_SQUELCH_DBFS = 0.0

    def set_parked(self, parked: bool) -> None:
        """Park or unpark this channel.

        While parked the pwr_squelch threshold is forced to
        :data:`_PARKED_SQUELCH_DBFS` (0 dBFS), so the gate stays closed
        and the channel contributes silence to the mixer.  The operator's
        previously-configured squelch threshold is preserved and restored
        on unpark.

        Idempotent: calling ``set_parked(True)`` twice does nothing.
        """
        parked = bool(parked)
        if parked == self._is_parked:
            return
        if parked:
            # Save current operator-intended threshold, slam gate shut.
            self._parked_saved_squelch_dbfs = self._squelch_dbfs
            self._is_parked = True
            self.pwr_squelch.set_threshold(self._PARKED_SQUELCH_DBFS)
        else:
            # Restore operator-intended threshold.
            self._is_parked = False
            self._squelch_dbfs = self._parked_saved_squelch_dbfs
            self.pwr_squelch.set_threshold(self._parked_saved_squelch_dbfs)

    def set_gain(self, db: float) -> None:
        self._gain_db = float(db)
        self._apply_gain(self._gain_db)

    def _apply_gain(self, db: float) -> None:
        # AM path applies gain by scaling the AGC reference (matches Phase 1).
        # NFM path applies it as a post-discriminator scalar multiplier, so
        # the operator semantics ("gain_db dB louder") are equivalent.
        linear = 10.0 ** (db / 20.0)
        if self._mode == "am":
            self.agc.set_reference(self._AGC_REF_0DB * linear)
        else:  # nfm
            # set_k is the GR name for multiply_const_ff's setter.
            self.nfm_audio_gain.set_k(linear)

    # ----- Probes ----------------------------------------------------------

    def get_signal_level_dbfs(self) -> float:
        """Running average magnitude-squared expressed in dBFS."""
        m2 = float(self.level_probe.level())
        if m2 <= 0.0:
            return -120.0
        return 10.0 * math.log10(max(m2, 1e-12))

    def get_squelch_open(self) -> bool:
        return bool(self.pwr_squelch.unmuted())

    is_squelch_open = get_squelch_open  # convenience alias

    # ----- Introspection ---------------------------------------------------

    @property
    def mode(self) -> ChannelMode:
        return self._mode

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

    @property
    def nfm_max_deviation_hz(self) -> float:
        return self._nfm_max_deviation_hz

    @property
    def is_parked(self) -> bool:
        """True if this channel is in the LO scheduler's parked state.

        Phase 4-pre.  Parked channels have their pwr_squelch slammed shut
        and contribute silence to the mixer.  The hit detector skips
        parked channels entirely — no hit_start / hit_end events fire
        while parked.
        """
        return self._is_parked

    def snapshot(self) -> dict:
        """Return a JSON-serializable snapshot of this channel's state."""
        return {
            "mode": self._mode,
            "center_freq_offset_hz": self._center_freq_offset,
            "squelch_dbfs": self._squelch_dbfs,
            "gain_db": self._gain_db,
            "audio_rate": self._audio_rate,
            "samp_rate": self._samp_rate,
            "channel_bw_hz": self._channel_bw_hz,
            "audio_bw_hz": self._audio_bw_hz,
            "nfm_max_deviation_hz": self._nfm_max_deviation_hz,
            "signal_level_dbfs": self.get_signal_level_dbfs(),
            "squelch_open": self.get_squelch_open(),
            "is_parked": self._is_parked,
        }


__all__ = ["Channel", "ChannelMode"]
