"""chirp.dsp.source_sdr — Live IQ source via SoapySDR/osmocom_source.

Phase 4b. Wraps `osmocom_source` so the chirp daemon can pull real IQ from
an SDR (SDRplay RSPduo today; design works for any SoapySDR-supported
device).

Design notes
------------
* osmocom_source ("osmosdr.source") drives SoapySDR via a `soapy=,driver=...`
  args string. For SDRplay RSPduo we pass `serial`, `mode` (MA/SL/ST/DT) and
  `tuner` selectors — these match the strings rtl-airband uses in its own
  device_string.
* Output is complex64 baseband at `samp_rate` sps, identical wire shape to
  `FileIQSource`, so the downstream channel pool wiring is unchanged.
* No `throttle` block in front: real RF runs at real time already.
* Gains: SDRplay exposes per-stage gains (IFGR/RFGR). We accept a single
  "overall_gain_db" knob (set via `set_gain(db)` on the osmocom source, which
  the SoapySDR driver maps internally) plus optional `gain_mode` for AGC.
  Per-stage gain overrides can be threaded later if needed.
* Hot retune: `set_center_freq(hz)` updates the LO. The channel pool's
  per-slot offsets stay valid as long as the new center keeps them inside
  the source's passband.

Why no Throttle?
----------------
osmocom_source produces samples in real time (driven by the SDR's clock).
A `throttle` block on top would only kick in if upstream over-produced;
since the SDR sets the rate, throttle is redundant and adds latency.

A note on the SoapySDR --find / open hang seen in Phase 4b first pass
--------------------------------------------------------------------
While both RSPduo tuner slots are held by rtl-airband (one device, MA + SL),
the sdrplay_apiService serializes new client connections and a third client
hangs on open. This adapter does not work around that — it assumes the
target device has at least one free slot when chirp opens it. For Phase 4b
retry we use a DIFFERENT physical RSPduo (the digital one, briefly freed by
stopping op25) so the open succeeds.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from gnuradio import gr

log = logging.getLogger("chirp.source_sdr")

from chirp.dsp.source_validator import (
    SourceContractValidator,
)

try:  # pragma: no cover — osmocom is system-installed on Micro
    import osmosdr
except ImportError:  # pragma: no cover
    osmosdr = None  # type: ignore[assignment]


def _validate_enabled() -> bool:
    """``CHIRP_SOURCE_VALIDATE`` is on by default off so deploys without
    the env var have zero validator overhead.  Phase 1 ships off; we
    flip it on once the envelope is calibrated against healthy traffic.
    """
    return str(os.getenv("CHIRP_SOURCE_VALIDATE", "0")).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


@dataclass
class SdrSourceConfig:
    """Knobs for SdrIQSource. Field-validated at SdrIQSource construction.

    Args:
        device_args: SoapySDR args string passed to osmocom_source.
            For SDRplay RSPduo: 'soapy=,driver=sdrplay,serial=...,mode=...,tuner=...'.
        sample_rate: baseband sample rate (sps). Must be >= 1e6 and a multiple
            of 1e6 to match the channel pool's decimation chain.
        center_freq_hz: SDR LO frequency (Hz). Channels at +/- (sample_rate/2)
            of this are observable.
        gain_db: overall gain in dB; passed to osmocom_source.set_gain(). For
            SDRplay this maps internally to IFGR/RFGR via the SoapySDR driver.
        gain_mode_auto: if True, enable hardware AGC. Most SDRplay setups use
            manual gain (False) for predictable squelch behaviour. Default False.
        bandwidth_hz: optional analog bandwidth (Hz). 0 means "driver default"
            (driver picks closest filter to sample_rate).
        ppm: optional frequency correction in PPM. 0 means no correction.
        antenna: optional antenna selector. Valid names are driver-specific
            and MUST match the driver's own list exactly — for SoapySDRPlay3
            on the RSPduo they are 'Tuner 1 50 ohm', 'Tuner 1 Hi-Z', and
            'Tuner 2 50 ohm' ('Antenna A'-style names are NOT valid and were
            previously accepted silently, leaving the driver default port
            selected — the 2026-06-12 no-RF incident). Selection is verified
            by read-back after set; a mismatch raises at construction.
            None means "driver default".
    """

    device_args: str
    sample_rate: float
    center_freq_hz: float
    gain_db: float = 30.0
    gain_mode_auto: bool = False
    bandwidth_hz: float = 0.0
    ppm: float = 0.0
    antenna: Optional[str] = None
    # Per-stage gains for fine control. Keys are SDRplay gain element names
    # ("IFGR", "RFGR"). Values are dB (or driver-defined units — SDRplay uses
    # gain *reduction* counts for these elements, where higher = quieter).
    # When provided, set after the overall set_gain() and overall AGC mode.
    element_gains: dict[str, float] = field(default_factory=dict)


class SdrIQSource(gr.hier_block2):
    """Live SDR IQ source.

    Public surface matches FileIQSource so the chirp daemon can swap them
    behind a `source_kind` flag:
        - output port: 1 x complex64
        - introspection: .samp_rate property, .center_freq_hz property
        - hot-set: .set_center_freq(hz), .set_gain(db)
    """

    def __init__(self, cfg: SdrSourceConfig) -> None:
        super().__init__(
            "SdrIQSource",
            gr.io_signature(0, 0, 0),
            gr.io_signature(1, 1, gr.sizeof_gr_complex),
        )
        if osmosdr is None:
            raise RuntimeError(
                "osmosdr (gr-osmosdr) is not importable; cannot use sdr source"
            )

        if cfg.sample_rate < 1e6:
            raise ValueError("SdrIQSource: sample_rate must be >= 1 Msps")
        if abs(round(cfg.sample_rate / 1e6) - cfg.sample_rate / 1e6) > 1e-9:
            raise ValueError(
                "SdrIQSource: sample_rate must be a multiple of 1 Msps "
                "(channel-pool decimation constraint)"
            )

        self._cfg = cfg
        self._src = osmosdr.source(args=cfg.device_args)

        # Order matters: rate first, then bandwidth, then freq + gain.
        self._src.set_sample_rate(float(cfg.sample_rate))
        if cfg.bandwidth_hz > 0:
            self._src.set_bandwidth(float(cfg.bandwidth_hz), 0)
        self._src.set_center_freq(float(cfg.center_freq_hz), 0)
        if cfg.ppm:
            self._src.set_freq_corr(float(cfg.ppm), 0)
        # AGC mode first; then overall gain takes effect when AGC is off.
        self._src.set_gain_mode(bool(cfg.gain_mode_auto), 0)
        self._src.set_gain(float(cfg.gain_db), 0)
        if cfg.antenna:
            # Verified antenna selection (2026-06-12 no-RF incident).  An
            # invalid name (e.g. 'Antenna A' on SoapySDRPlay3) used to fail
            # silently inside the driver, leaving the default port selected
            # while the config CLAIMED another — the daemon then ran "alive
            # but useless" on a port with no coax.  Contract now: set, read
            # back, and refuse to boot on a mismatch so systemd surfaces a
            # deterministic failure instead of a silent wrong-port runtime.
            requested = str(cfg.antenna).strip()
            try:
                self._src.set_antenna(requested, 0)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"SdrIQSource: set_antenna({requested!r}) failed: {exc}; "
                    f"available: {self._list_antennas_safe()}"
                ) from exc
            applied = ""
            try:
                applied = str(self._src.get_antenna(0) or "").strip()
            except Exception:  # noqa: BLE001
                # Driver without antenna read-back: nothing to verify
                # against; log the gap rather than guessing.
                log.warning(
                    "SdrIQSource: driver does not support get_antenna(); "
                    "cannot verify %r was applied", requested,
                )
            if applied and applied != requested:
                raise RuntimeError(
                    f"SdrIQSource: antenna read-back mismatch: requested "
                    f"{requested!r} but driver reports {applied!r}; "
                    f"available: {self._list_antennas_safe()}. Fix the "
                    f"config/CHIRP_SDR_ANTENNA to one of the available "
                    f"names exactly."
                )
            if applied:
                log.info("SdrIQSource: antenna %r verified active", applied)
        # Per-element overrides (IFGR/RFGR for SDRplay) applied last so they
        # win over the unified set_gain() call.
        for elem, val in cfg.element_gains.items():
            try:
                self._src.set_gain(float(val), elem, 0)
            except Exception:  # noqa: BLE001
                pass

        # No throttle: SDR drives the clock.
        self.connect(self._src, self)

        # Optional source-contract validator (Phase 1).  When enabled,
        # wires a parallel branch source -> head(N) -> vector_sink_c
        # that captures the first ~200 ms of samples after start().  The
        # daemon reads + evaluates the capture, then either continues
        # normally or aborts with a structured diagnostic if the sample
        # stream is outside the envelope (constant wedge, all-ones
        # saturation, DC-only, starved arrival).  Zero overhead when
        # disabled — the branch isn't even constructed.
        self._validator: Optional[SourceContractValidator] = None
        if _validate_enabled():
            self._validator = SourceContractValidator(
                sample_rate=cfg.sample_rate,
            )
            # IMPORTANT: this is a PARALLEL branch off the source.  It
            # never sits between the source and the production output
            # port (see chirp/dsp/source_validator.py for why — the
            # P0-1 lesson from 2026-06-12).  After N samples, head emits
            # EOS to its vector_sink and stops consuming; the production
            # branch is unaffected.
            self._validator.connect_to(self, self._src)

    def _list_antennas_safe(self) -> list[str]:
        """Available antenna names, or [] when the driver can't say."""
        try:
            return [str(a) for a in (self._src.get_antennas(0) or [])]
        except Exception:  # noqa: BLE001
            return []

    # -- introspection ------------------------------------------------------

    @property
    def samp_rate(self) -> float:
        return float(self._cfg.sample_rate)

    @property
    def center_freq_hz(self) -> float:
        try:
            return float(self._src.get_center_freq(0))
        except Exception:  # noqa: BLE001
            return float(self._cfg.center_freq_hz)

    @property
    def device_args(self) -> str:
        return self._cfg.device_args

    # -- hot setters --------------------------------------------------------

    def set_center_freq(self, hz: float) -> None:
        self._src.set_center_freq(float(hz), 0)

    def set_gain(self, db: float) -> float:
        """Hot-set the overall SDR front-end gain (dB) and return the value
        the driver actually applied.

        SoapySDRPlay3 / gr-osmosdr clamp the request to the device's real
        internal range, so the read-back via ``get_gain(0)`` is the truth —
        the UI gain slider relies on it to display reality, not the request.
        Also updates ``_cfg.gain_db`` so the config mirrors live state.
        """
        self._src.set_gain(float(db), 0)
        try:
            actual = float(self._src.get_gain(0))
        except Exception:  # noqa: BLE001 -- read-back is best-effort
            actual = float(db)
        self._cfg.gain_db = actual
        return actual

    # -- Phase 1: source contract validator ---------------------------------

    @property
    def validator(self) -> Optional[SourceContractValidator]:
        """The wired validator instance, or None when CHIRP_SOURCE_VALIDATE
        is off.  Caller (daemon) inspects this AFTER the flowgraph has
        ``start()``ed to read the captured window and compare to envelope.
        """
        return self._validator


__all__ = ["SdrIQSource", "SdrSourceConfig"]
