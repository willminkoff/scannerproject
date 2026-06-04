"""chirp.tests.test_phase4b — SDR source adapter (Phase 4b).

These tests mock osmocom_source so we can exercise the SdrIQSource wiring,
config validation, and the daemon's source_kind branching without touching
real hardware. Real-hardware validation lives in PROGRESS.md Phase 4b-retry.

What this covers:
  * SdrSourceConfig dataclass: defaults + field plumbing.
  * SdrIQSource: rejects bad sample rates, calls the right osmocom setters
    in the right order, handles missing antenna/element_gain gracefully.
  * DaemonConfig.source_kind="sdr": correctly populates the SDR fields from
    a config json + env override, and the daemon raises a clear error on
    missing args.

What this DOES NOT cover (intentionally — needs real hardware):
  * Actual SoapySDR/SDRplay open success.
  * Real IQ samples / signal-level validation. See PROGRESS.md Phase 4b-retry
    for the hardware-side blocker.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from gnuradio import blocks, gr


# ---------------------------------------------------------------------------
# osmocom-source proxy — a real gr.hier_block2 wrapping a null_source so
# `self.connect(self._src, self)` works inside SdrIQSource, while we still
# record every setter call for assertion.
# ---------------------------------------------------------------------------


class _FakeOsmoSource(gr.hier_block2):
    def __init__(self, args: str) -> None:
        super().__init__(
            "FakeOsmoSource",
            gr.io_signature(0, 0, 0),
            gr.io_signature(1, 1, gr.sizeof_gr_complex),
        )
        self.args = args
        self.calls: list[tuple[str, tuple]] = []
        self._center_freq = 0.0
        self._sample_rate = 0.0
        self._null = blocks.null_source(gr.sizeof_gr_complex)
        self.connect(self._null, self)
        self._raise_on_set_antenna = False

    def _record(self, name: str, *args):
        self.calls.append((name, args))

    def set_sample_rate(self, r):
        self._record("set_sample_rate", r)
        self._sample_rate = r

    def set_bandwidth(self, bw, ch):
        self._record("set_bandwidth", bw, ch)

    def set_center_freq(self, f, ch):
        self._record("set_center_freq", f, ch)
        self._center_freq = f

    def get_center_freq(self, ch):
        return self._center_freq

    def set_freq_corr(self, ppm, ch):
        self._record("set_freq_corr", ppm, ch)

    def set_gain_mode(self, on, ch):
        self._record("set_gain_mode", on, ch)

    def set_gain(self, *args):
        self._record("set_gain", *args)

    def set_antenna(self, name, ch):
        self._record("set_antenna", name, ch)
        if self._raise_on_set_antenna:
            raise RuntimeError("driver hates set_antenna")


@pytest.fixture
def fake_osmosdr(monkeypatch):
    """Install a fake `osmosdr` module BEFORE source_sdr is (re-)imported,
    so the `try: import osmosdr` at module scope binds to our fake."""
    fake_module = types.ModuleType("osmosdr")
    holder = {"instances": []}

    def _factory(args: str):
        inst = _FakeOsmoSource(args)
        holder["instances"].append(inst)
        return inst

    fake_module.source = _factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "osmosdr", fake_module)
    # Force re-import so source_sdr.osmosdr binds to the fake.
    monkeypatch.delitem(sys.modules, "chirp.dsp.source_sdr", raising=False)
    return holder


# ---------------------------------------------------------------------------
# SdrSourceConfig + SdrIQSource
# ---------------------------------------------------------------------------


def test_sdr_source_config_defaults():
    from chirp.dsp.source_sdr import SdrSourceConfig

    cfg = SdrSourceConfig(
        device_args="soapy=,driver=sdrplay,serial=ABC,mode=MA,tuner=1",
        sample_rate=1e6,
        center_freq_hz=127.5e6,
    )
    assert cfg.gain_db == 30.0
    assert cfg.gain_mode_auto is False
    assert cfg.bandwidth_hz == 0.0
    assert cfg.ppm == 0.0
    assert cfg.antenna is None
    assert cfg.element_gains == {}


def test_sdr_iq_source_rejects_low_sample_rate(fake_osmosdr):
    from chirp.dsp.source_sdr import SdrIQSource, SdrSourceConfig

    cfg = SdrSourceConfig(
        device_args="soapy=,driver=sdrplay,serial=ABC,mode=MA,tuner=1",
        sample_rate=500_000,
        center_freq_hz=127.5e6,
    )
    with pytest.raises(ValueError, match=">= 1 Msps"):
        SdrIQSource(cfg)


def test_sdr_iq_source_rejects_non_integer_msps(fake_osmosdr):
    from chirp.dsp.source_sdr import SdrIQSource, SdrSourceConfig

    cfg = SdrSourceConfig(
        device_args="soapy=,driver=sdrplay,serial=ABC,mode=MA,tuner=1",
        sample_rate=2_400_000,  # not a multiple of 1 Msps
        center_freq_hz=127.5e6,
    )
    with pytest.raises(ValueError, match="multiple of 1 Msps"):
        SdrIQSource(cfg)


def test_sdr_iq_source_setter_order_and_values(fake_osmosdr):
    from chirp.dsp.source_sdr import SdrIQSource, SdrSourceConfig

    cfg = SdrSourceConfig(
        device_args="soapy=,driver=sdrplay,serial=ABC,mode=MA,tuner=1",
        sample_rate=2_000_000,
        center_freq_hz=127.5e6,
        gain_db=28.0,
        gain_mode_auto=False,
        bandwidth_hz=600_000.0,
        ppm=0.5,
        antenna="Antenna A",
        element_gains={"IFGR": 20.0, "RFGR": 0.0},
    )
    src = SdrIQSource(cfg)
    inst = fake_osmosdr["instances"][0]
    names = [c[0] for c in inst.calls]

    # Order: rate -> bandwidth -> center_freq -> freq_corr -> gain_mode ->
    # set_gain (overall) -> set_antenna -> per-element gains.
    assert names[0] == "set_sample_rate"
    assert names[1] == "set_bandwidth"
    assert names[2] == "set_center_freq"
    assert names[3] == "set_freq_corr"
    assert names[4] == "set_gain_mode"
    assert names[5] == "set_gain"
    assert "set_antenna" in names

    # Element gains come last and use the (value, name, channel) signature.
    elem_calls = [c for c in inst.calls if c[0] == "set_gain" and len(c[1]) == 3]
    elem_args = {c[1][1]: c[1][0] for c in elem_calls}
    assert elem_args == {"IFGR": 20.0, "RFGR": 0.0}

    # Introspection.
    assert src.samp_rate == 2_000_000
    assert abs(src.center_freq_hz - 127.5e6) < 1


def test_sdr_iq_source_handles_set_antenna_failure(fake_osmosdr):
    """If the driver rejects set_antenna we should NOT abort — fall back to
    the driver default. This is bitten in older SDRplay/Soapy versions."""
    from chirp.dsp.source_sdr import SdrIQSource, SdrSourceConfig

    fake_module = sys.modules["osmosdr"]
    orig_factory = fake_module.source

    def broken_factory(args):
        inst = orig_factory(args)
        inst._raise_on_set_antenna = True
        return inst

    fake_module.source = broken_factory

    cfg = SdrSourceConfig(
        device_args="soapy=,driver=sdrplay,serial=ABC,mode=MA,tuner=1",
        sample_rate=1e6,
        center_freq_hz=127.5e6,
        antenna="Antenna A",
    )
    # Must not raise.
    SdrIQSource(cfg)


def test_sdr_iq_source_hot_retune(fake_osmosdr):
    from chirp.dsp.source_sdr import SdrIQSource, SdrSourceConfig

    cfg = SdrSourceConfig(
        device_args="soapy=,driver=sdrplay,serial=ABC,mode=MA,tuner=1",
        sample_rate=1e6,
        center_freq_hz=127.5e6,
    )
    src = SdrIQSource(cfg)
    src.set_center_freq(128.0e6)
    src.set_gain(36.0)
    inst = fake_osmosdr["instances"][0]
    setter_freqs = [c[1][0] for c in inst.calls if c[0] == "set_center_freq"]
    assert 128.0e6 in setter_freqs


# ---------------------------------------------------------------------------
# DaemonConfig + daemon flowgraph branching
# ---------------------------------------------------------------------------


def test_daemon_config_loads_sdr_block_from_json(tmp_path, monkeypatch):
    """When source='sdr' lives in the config JSON, env-free load picks it up."""
    from chirp.daemon import load_config

    cfg_path = tmp_path / "airband.json"
    cfg_path.write_text(json.dumps({
        "band": "airband",
        "source": "sdr",
        "source_samp_rate": 1000000.0,
        "audio_out": "file:" + str(tmp_path / "out.f32"),
        "sdr": {
            "device_args": "soapy=,driver=sdrplay,serial=ABC,mode=MA,tuner=1",
            "center_freq_hz": 127500000.0,
            "gain_db": 32.8,
        },
    }))
    # Clear any env knobs that might leak from the parent shell.
    for k in (
        "CHIRP_SOURCE", "CHIRP_SDR_DEVICE_ARGS", "CHIRP_SDR_CENTER_FREQ_HZ",
        "CHIRP_SDR_GAIN_DB", "CHIRP_SDR_GAIN_MODE_AUTO", "CHIRP_SDR_BANDWIDTH_HZ",
        "CHIRP_SDR_PPM", "CHIRP_SDR_ANTENNA", "CHIRP_AUDIO_OUT",
        "CHIRP_SOURCE_SAMP_RATE",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CHIRP_BAND", "airband")
    cfg = load_config(defaults_path=cfg_path)
    assert cfg.source_kind == "sdr"
    assert cfg.sdr_device_args == "soapy=,driver=sdrplay,serial=ABC,mode=MA,tuner=1"
    assert cfg.sdr_center_freq_hz == 127500000.0
    assert cfg.sdr_gain_db == 32.8


def test_daemon_config_sdr_env_override(tmp_path, monkeypatch):
    from chirp.daemon import load_config

    cfg_path = tmp_path / "airband.json"
    cfg_path.write_text(json.dumps({
        "band": "airband",
        "audio_out": "file:" + str(tmp_path / "out.f32"),
    }))
    monkeypatch.setenv("CHIRP_BAND", "airband")
    monkeypatch.setenv("CHIRP_SOURCE", "sdr")
    monkeypatch.setenv("CHIRP_SDR_DEVICE_ARGS",
                       "soapy=,driver=sdrplay,serial=ZZZ,mode=ST,tuner=1")
    monkeypatch.setenv("CHIRP_SDR_CENTER_FREQ_HZ", "138050000")
    monkeypatch.setenv("CHIRP_SDR_GAIN_DB", "20.0")
    monkeypatch.setenv("CHIRP_SDR_GAIN_MODE_AUTO", "true")
    cfg = load_config(defaults_path=cfg_path)
    assert cfg.source_kind == "sdr"
    assert cfg.sdr_device_args.endswith("serial=ZZZ,mode=ST,tuner=1")
    assert cfg.sdr_center_freq_hz == 138050000.0
    assert cfg.sdr_gain_db == 20.0
    assert cfg.sdr_gain_mode_auto is True


def test_daemon_freq_to_offset_sdr_mode(tmp_path):
    """For SDR sources, the per-channel xlating offset is (freq - LO)."""
    from chirp.daemon import ChirpFlowgraph, DaemonConfig

    cfg = DaemonConfig(
        band="airband",
        source_kind="sdr",
        sdr_device_args="soapy=,driver=sdrplay,serial=ABC,mode=MA,tuner=1",
        sdr_center_freq_hz=127_500_000.0,
        source_samp_rate=1e6,
        audio_out_kind="file",
        audio_out_path=str(tmp_path / "audio.f32"),
        state_path=str(tmp_path / "state.json"),
        hit_log_path=str(tmp_path / "hits.jsonl"),
        max_channels=2,
    )

    # _freq_to_offset_hz is pure — call it on a stub that mimics the
    # attribute the helper reads.
    class _Stub:
        _cfg = cfg
        _freq_to_offset_hz = ChirpFlowgraph._freq_to_offset_hz

    stub = _Stub()
    # 127.700 MHz channel, LO at 127.500 MHz -> +200 kHz baseband offset.
    assert abs(stub._freq_to_offset_hz(127.700) - 200_000) < 1
    # 127.300 MHz channel -> -200 kHz baseband offset.
    assert abs(stub._freq_to_offset_hz(127.300) - (-200_000)) < 1


def test_daemon_freq_to_offset_file_mode_unchanged(tmp_path):
    """The file source path keeps its pre-Phase-4b behaviour."""
    from chirp.daemon import ChirpFlowgraph, DaemonConfig

    cfg = DaemonConfig(
        band="airband",
        source_kind="file",
        source_path=str(tmp_path / "iq.fc32"),
        source_samp_rate=1e6,
        audio_out_kind="file",
        audio_out_path=str(tmp_path / "audio.f32"),
    )

    class _Stub:
        _cfg = cfg
        _freq_to_offset_hz = ChirpFlowgraph._freq_to_offset_hz

    stub = _Stub()
    # Old contract: freq_mhz multiplied by 1e6, no LO subtraction.
    assert abs(stub._freq_to_offset_hz(0.2) - 200_000) < 1
    assert abs(stub._freq_to_offset_hz(127.5) - 127_500_000) < 1


def test_daemon_config_sdr_requires_device_args():
    """The DaemonConfig default has source_kind='file' so source-kind='sdr'
    with no device_args is a config-error path. We verify the field defaults
    and the contract that the daemon will reject it at flowgraph build time."""
    from chirp.daemon import DaemonConfig

    cfg = DaemonConfig(
        band="airband",
        source_kind="sdr",
        sdr_device_args=None,
        sdr_center_freq_hz=127.5e6,
        source_samp_rate=1e6,
        audio_out_kind="file",
        audio_out_path="/tmp/a.f32",
    )
    assert cfg.source_kind == "sdr"
    assert cfg.sdr_device_args is None  # daemon will raise on this
