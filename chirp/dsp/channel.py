"""chirp.dsp.channel — Single-channel AM / NFM demod hier_block.

Phase 1 introduced this block for AM (airband). Phase 4a extends it with an
NFM mode for the ground band (mil-air narrowband FM). Mode is selected at
construction time and is **immutable per channel** — locked decision from
SDR_DEMOD_DESIGN_2026-06-03.md (changing mode would require retearing the
flowgraph wiring).

AM chain (mirrors ham2mon TunerDemodAM, see Phase 1 docstring):
    xlating(decim 5) -> fir(decim 5) -> fir(decim ~samp_rate/1e6)
        -> pwr_squelch -> agc(ref=_AGC_REF_0DB, fixed)
        -> complex_to_mag (envelope)
        -> audio_lpf(decim 5) -> arb_resampler
        -> audio_trim(gain_db) -> audio_out

NFM chain (Phase 4a, audio_trim added Phase 4d):
    xlating(decim 5) -> fir(decim 5) -> fir(decim ~samp_rate/1e6)
        -> pwr_squelch
        -> quadrature_demod(gain = samp_rate_pre / (2*pi*max_dev))
        -> nfm_audio_gain(=1.0, static)
        -> audio_lpf(decim 5) -> arb_resampler
        -> audio_trim(gain_db) -> audio_out

Phase 4d (2026-06-04):  ``set_gain(db)`` now drives a dedicated
post-demod ``audio_trim`` block instead of perturbing the AM AGC
reference or the NFM post-discriminator multiplier.  Both demod-side
blocks are kept at their amplitude-calibrated defaults (AGC target
0.1, NFM multiplier 1.0) so the demod output stays near unity for any
operator ``gain_db`` choice.  The ``audio_trim`` block is clamped to
±20 dB.  Background: the Phase-4c migration copied the operator's RF
front-end gain (32.8 dB) into per-channel ``gain_db``, which the
pre-Phase-4d AGC-scaling path interpreted as 4.37x AGC target → audio
sink clipped on every squelch-open burst.

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

from chirp.dsp.audio_vad_block import AudioVADGate
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

    # AGC reference for the AM path.  This is a CONSTANT target output
    # magnitude for the agc3_cc block (ham2mon's TunerDemodAM default).
    # It is intentionally NOT modulated by ``gain_db`` — see the design
    # note on Phase-4d / 2026-06-04: ``gain_db`` is a post-demod audio
    # trim, not an SDR-side or AGC-reference knob.  Multiplying the AGC
    # reference by ``10^(gain_db/20)`` with a stage gain of 32.8 dB (the
    # operator's RF front-end setting, accidentally copied into per-
    # channel ``gain_db`` by the Phase-4c migration) drove the AGC to
    # target 4.37x full scale → the audio sink clipped every
    # squelch-open burst on /ANALOG.mp3.
    _AGC_REF_0DB = 0.1

    # ``set_gain`` clamps the per-channel trim to this dB range.  20 dB
    # of headroom on either side covers operator level trims between
    # channels without letting a stale config peg the audio sink.
    _GAIN_DB_MIN = -20.0
    _GAIN_DB_MAX = 20.0

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
        agc_max_gain: float = 1000.0,
        agc_attack: float = 0.1,
        agc_decay: float = 1e-4,
        am_agc_enabled: bool = True,
        am_fixed_gain: float = 10.0,
        audio_bandpass_low_hz: float = 300.0,
        audio_bandpass_high_hz: float = 3500.0,
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
        self._gain_db = self._clamp_gain_db(float(gain_db))
        self._nfm_max_deviation_hz = float(nfm_max_deviation_hz)
        # AM AGC ceiling + attack rate (configurable). Caps the old 96 dB /
        # fast-attack AGC that amplified the noise floor on weak signals.
        # AM-only (NFM has no AGC).
        self._agc_max_gain = float(agc_max_gain)
        self._agc_attack = float(agc_attack)
        # AGC decay rate: lower = slower gain ramp-up when signal drops,
        # approximating a hang/hold so inter-syllable gaps stay quiet. AM-only.
        self._agc_decay = float(agc_decay)
        # AM AGC enable. A FAST adaptive AGC on AM is destructive: it holds the
        # IF amplitude constant, which erases the amplitude MODULATION that
        # carries the voice -> the envelope detector outputs mush (heard as
        # pure noise). Proven 2026-06-14 by A/B of chirp's Channel vs a textbook
        # demod on captured BNA IQ (judged by ear; chirp=noise, no-AGC=clean).
        # When disabled, a fixed-gain multiply replaces the AGC so the
        # modulation survives; downstream audio_trim/mixer handle leveling.
        self._am_agc_enabled = bool(am_agc_enabled)
        self._am_fixed_gain = float(am_fixed_gain)
        # AM voice band-pass edges (Hz): low ~300 strips envelope-detector DC +
        # rumble; high trims hiss above the voice band. AM-only (config-tunable).
        self._audio_bandpass_low_hz = float(audio_bandpass_low_hz)
        self._audio_bandpass_high_hz = float(audio_bandpass_high_hz)

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
            if self._am_agc_enabled:
                # agc3_cc(attack, decay, reference, gain_init, max_gain_floor).
                # WARNING: a fast attack here flattens the AM modulation (the
                # voice) into noise — see _am_agc_enabled note above. Only use
                # with a very slow attack/decay if enabled at all.
                self.agc = analog.agc3_cc(self._agc_attack, self._agc_decay, self._AGC_REF_0DB, 10, 1)
                self.agc.set_max_gain(int(self._agc_max_gain))
            else:
                # Fixed gain — preserves the AM envelope (= the voice).
                self.agc = blocks.multiply_const_cc(self._am_fixed_gain)
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

        # --- Audio filter + decim ------------------------------------------
        # AM: voice BAND-PASS (audio_bandpass_low_hz .. audio_bandpass_high_hz).
        # The ~300 Hz HPF removes the envelope detector's DC + sub-300 Hz
        # rumble; the high edge trims hiss above the voice band. NFM: plain
        # LOW-PASS (no DC after the discriminator). Both decimate by decims[0].
        if mode == "am":
            taps_audio = grfilter.firdes.band_pass(
                1.0, pre_demod_rate,
                self._audio_bandpass_low_hz, self._audio_bandpass_high_hz,
                200.0, window.WIN_HAMMING,
            )
            self.audio_bpf = grfilter.fir_filter_fff(decims[0], taps_audio)
            self.audio_lpf = None
            self._audio_filt = self.audio_bpf
        else:  # nfm
            taps_audio = grfilter.firdes.low_pass(
                1.0, pre_demod_rate,
                self._audio_bw_hz, 500.0, window.WIN_HAMMING,
            )
            self.audio_lpf = grfilter.fir_filter_fff(decims[0], taps_audio)
            self.audio_bpf = None
            self._audio_filt = self.audio_lpf

        # Arb resampler -> audio_rate.
        post_audio_lpf_rate = pre_demod_rate / decims[0]
        resamp_ratio = self._audio_rate / post_audio_lpf_rate
        self.audio_resamp = pfb.arb_resampler_fff(
            resamp_ratio, taps=None, flt_size=32
        )

        # --- Audio trim (Phase 4d) -----------------------------------------
        # Post-mixer-stage audio level trim driven by ``set_gain(db)``.  Sits
        # at the very end of the channel chain so it does NOT affect the AGC
        # reference (AM) or the quadrature_demod output (NFM).  Default
        # initial multiplier = 1.0 (0 dB).  Updated via ``_apply_gain``.
        self.audio_trim = blocks.multiply_const_ff(1.0)

        # --- VAD gate (SB5 2026-06-09 squelch redesign) ---------------------
        # Voice-activity detector that mutes audio blocks that don't look
        # like voice (high envelope variance, low spectral flatness, voice-
        # band ZCR). Replaces power-only RF squelch for the audible output;
        # the pwr_squelch above still drives hit detection + scan_hold +
        # priority gate so the per-channel state machine is untouched.
        self.vad_gate = AudioVADGate(
            audio_rate=self._audio_rate,
            threshold=50.0,
            bypass=False,
        )

        # --- Priority gate (scan single-active-channel) ---------------------
        # A 0/1 multiplier the priority-gate controller flips to mute
        # non-selected channels even when their squelch is open, so only ONE
        # channel's audio reaches the mixer at a time.  Default 1.0 (pass) —
        # the gate is a no-op unless the controller mutes it, so when
        # priority_gate_enabled is False this block never changes from unity.
        self._priority_muted: bool = False
        self.priority_gate = blocks.multiply_const_ff(1.0)

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
            self.connect(self.am_demod, self._audio_filt)
        else:  # nfm
            self.connect(self.pwr_squelch, self.quad_demod)
            self.connect(self.quad_demod, self.nfm_audio_gain)
            self.connect(self.nfm_audio_gain, self._audio_filt)

        self.connect(self._audio_filt, self.audio_resamp)
        # Phase 4d: route the resampled audio through the trim multiplier
        # so set_gain(db) is a clean post-demod level adjustment rather
        # than something that perturbs the AM AGC or the NFM
        # discriminator output.
        self.connect(self.audio_resamp, self.audio_trim)
        self.connect(self.audio_trim, self.vad_gate)
        self.connect(self.vad_gate, self.priority_gate)
        self.connect(self.priority_gate, self)

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

    # -- VAD gate setters (SB5 2026-06-09) ----------------------------------

    def set_vad_threshold(self, threshold: float) -> None:
        """Set the per-channel VAD score threshold (0-100).

        Higher = stricter (only strong voice passes); lower = more
        permissive (hiss may bleed through).  Default 50.
        """
        self.vad_gate.set_threshold(float(threshold))

    def set_vad_bypass(self, bypass: bool) -> None:
        """Bypass the VAD gate (passthrough audio).

        Used by the 'Test' / wide-open UI button to verify the audio path
        is intact independently of voice-detection.
        """
        self.vad_gate.set_bypass(bool(bypass))

    def vad_snapshot(self) -> dict:
        """Return the gate's current state for /api/chirp status."""
        return self.vad_gate.snapshot()

    @classmethod
    def _clamp_gain_db(cls, db: float) -> float:
        """Clamp a gain-in-dB request to [``_GAIN_DB_MIN``, ``_GAIN_DB_MAX``].

        Phase 4d: ``gain_db`` is a post-demod audio trim, not an RF or AGC
        knob.  Values outside ±20 dB tend to either drive the audio sink
        into clipping (positive) or mute the channel below MP3-encoder
        noise floor (negative), so the clamp protects the audio output
        from accidental stale configs (e.g. the 32.8 dB RF gain the
        Phase-4c migration script used to drop in here).
        """
        return max(cls._GAIN_DB_MIN, min(cls._GAIN_DB_MAX, float(db)))

    def set_gain(self, db: float) -> None:
        """Set the per-channel post-demod audio trim in dB.

        Phase 4d: this is a small audio level trim applied AFTER the
        demod chain (post-AGC for AM, post-quad-demod for NFM, post
        audio LPF + resampler).  It does NOT modulate the AGC reference
        or the quadrature_demod gain — those are amplitude-calibrated to
        keep the demod output near unity, independent of the operator's
        per-channel trim choice.  Clamped to [_GAIN_DB_MIN, _GAIN_DB_MAX].
        """
        self._gain_db = self._clamp_gain_db(db)
        self._apply_gain(self._gain_db)

    def _apply_gain(self, db: float) -> None:
        """Push the current ``gain_db`` to the audio trim multiplier.

        Pre-Phase-4d this scaled the AM AGC reference and the NFM
        post-discriminator multiply_const.  Both side-effects are gone:
        the AGC always targets ``_AGC_REF_0DB`` (0.1), and the NFM
        post-demod block stays at 1.0.  Only ``audio_trim`` is touched.
        """
        linear = 10.0 ** (self._clamp_gain_db(db) / 20.0)
        self.audio_trim.set_k(linear)

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

    # ----- Priority gate (scan single-active-channel) ----------------------

    def set_priority_muted(self, muted: bool) -> None:
        """Mute (0) or pass (1) this channel's audio at the priority gate.

        Independent of squelch/parking: the priority-gate controller mutes
        every channel except the one currently holding the audio path, so a
        cluster with several open channels still yields one voice, not a mix.
        Idempotent.
        """
        muted = bool(muted)
        if muted == self._priority_muted:
            return
        self._priority_muted = muted
        self.priority_gate.set_k(0.0 if muted else 1.0)

    @property
    def is_priority_muted(self) -> bool:
        return self._priority_muted

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
            "priority_muted": self._priority_muted,
        }


__all__ = ["Channel", "ChannelMode"]
