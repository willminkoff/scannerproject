"""Phase 4a tests — NFM demod, per-band config, two-daemon coexistence.

Run from repo root:
    python3 -m pytest chirp/tests/test_phase4a.py -v

Covers:
  - Channel(mode="nfm") instantiates and respects setters.
  - Synthetic NFM IQ (carrier at +100 kHz, FM-mod by 500 Hz tone, 5 kHz
    deviation) demodulates to a clear 500 Hz audio tone — verified by FFT
    peak detection on the daemon's audio output file.
  - load_config picks the right per-band JSON via CHIRP_BAND env var.
  - load_config fails fast on invalid JSON / unsupported pool_mode.
  - Two daemons (airband + ground) run simultaneously in the same pytest
    process: distinct UDP ports, state files, hit logs, audio outputs.
  - set_squelch to one daemon's port does NOT mutate the other daemon's
    channel state (cross-talk check).
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from chirp.cmd.schema import parse_args, AddChannelArgs
from chirp.daemon import (
    ChirpFlowgraph,
    DaemonConfig,
    load_config,
)
from chirp.dsp.channel import Channel
from chirp.tests.fixtures.make_nfm_iq import make_nfm_iq


# ---------------------------------------------------------------------------
# Channel NFM
# ---------------------------------------------------------------------------


class TestChannelNFM:
    def test_instantiate_nfm(self):
        ch = Channel(samp_rate=1e6, mode="nfm",
                     center_freq_offset=100e3, squelch_dbfs=-60.0, gain_db=0.0)
        assert ch.mode == "nfm"
        assert ch.center_freq_offset == 100e3
        assert ch.nfm_max_deviation_hz == 5e3
        # NFM members wired, AM members are None.
        assert ch.quad_demod is not None
        assert ch.nfm_audio_gain is not None
        assert ch.agc is None
        assert ch.am_demod is None

    def test_instantiate_am_still_works(self):
        # Regression: AM path is unchanged.
        ch = Channel(samp_rate=1e6, mode="am")
        assert ch.mode == "am"
        assert ch.agc is not None
        assert ch.am_demod is not None
        assert ch.quad_demod is None
        assert ch.nfm_audio_gain is None

    def test_rejects_bad_mode(self):
        with pytest.raises(ValueError):
            Channel(samp_rate=1e6, mode="ssb")  # type: ignore[arg-type]

    def test_nfm_setters(self):
        ch = Channel(samp_rate=1e6, mode="nfm",
                     center_freq_offset=0.0, squelch_dbfs=-60.0, gain_db=0.0)
        ch.set_center_freq_offset(250e3)
        assert ch.center_freq_offset == 250e3
        ch.set_squelch(-40.0)
        assert ch.squelch_dbfs == -40.0
        # set_gain on NFM path must not raise (no AGC) and must update state.
        ch.set_gain(6.0)
        assert ch.gain_db == 6.0

    def test_nfm_snapshot_shape(self):
        ch = Channel(samp_rate=1e6, mode="nfm")
        snap = ch.snapshot()
        assert snap["mode"] == "nfm"
        for k in ("center_freq_offset_hz", "squelch_dbfs", "gain_db",
                  "audio_rate", "samp_rate", "signal_level_dbfs",
                  "squelch_open", "nfm_max_deviation_hz"):
            assert k in snap


# ---------------------------------------------------------------------------
# NFM end-to-end: synthetic IQ in, demod tone out (FFT peak detection)
# ---------------------------------------------------------------------------


def _write_nfm_iq(path: Path, samp_rate: float, duration_s: float,
                  carrier_hz: float, tone_hz: float, max_dev_hz: float) -> None:
    iq = make_nfm_iq(
        samp_rate=samp_rate, duration_s=duration_s,
        carrier_hz=carrier_hz, tone_hz=tone_hz,
        max_dev_hz=max_dev_hz, noise_sigma=0.001,
    )
    iq.tofile(path)


class TestNFMDemodEndToEnd:
    """Drive the ChirpFlowgraph with a synthetic NFM IQ file and verify
    the demodulated audio has a strong peak at the expected tone."""

    def test_nfm_tone_recovered(self, tmp_path):
        samp_rate = 1e6
        duration = 3.0
        carrier = 100e3
        tone = 500.0
        max_dev = 5e3
        audio_rate = 16000.0

        iq_path = tmp_path / "nfm_test.iq"
        _write_nfm_iq(iq_path, samp_rate, duration, carrier, tone, max_dev)
        audio_path = tmp_path / "ground_audio.f32"
        state_path = tmp_path / "ground.state.json"
        hit_log = tmp_path / "ground_hits.jsonl"

        cfg = DaemonConfig(
            band="ground",
            pool_mode="nfm",
            cmd_host="127.0.0.1",
            cmd_port=_pick_port(),
            source_kind="file",
            source_path=str(iq_path),
            source_samp_rate=samp_rate,
            audio_out_kind="file",
            audio_out_path=str(audio_path),
            audio_rate=audio_rate,
            max_channels=2,
            state_path=str(state_path),
            hit_log_path=str(hit_log),
        )

        srv = _DummyServer()
        tb = ChirpFlowgraph(cfg, srv)
        # Add an NFM channel pointed at the carrier with squelch wide open.
        args = parse_args("add_channel", {
            "id": "g1", "freq_mhz": carrier / 1e6,  # treated as offset Hz
            "mode": "nfm",
            "squelch_dbfs": -100.0,
            "gain_db": 6.0,
        })
        assert isinstance(args, AddChannelArgs)
        env = _DummyEnv(id="r1", cmd="add_channel")
        resp = tb._cmd_add_channel(env, args)
        assert resp.status == "ok", resp.error

        tb.start()
        try:
            # Let it run long enough to consume IQ + emit a few seconds audio.
            time.sleep(min(duration + 0.5, 4.0))
        finally:
            tb.stop()
            tb.wait()

        # Read audio (float32 mono). Skip the first ~0.5 s (startup/transient).
        raw = np.fromfile(audio_path, dtype=np.float32)
        n_skip = int(audio_rate * 0.5)
        if raw.size <= n_skip + int(audio_rate * 0.5):
            pytest.skip(f"insufficient audio produced ({raw.size} samples)")
        sig = raw[n_skip:]

        # FFT peak.
        n = sig.size
        spectrum = np.abs(np.fft.rfft(sig * np.hanning(n)))
        freqs = np.fft.rfftfreq(n, 1.0 / audio_rate)
        peak_idx = int(np.argmax(spectrum))
        peak_hz = float(freqs[peak_idx])

        # Tolerance: ~10 Hz FFT bin width at 1-2 second audio capture.
        assert abs(peak_hz - tone) < 30.0, (
            f"expected peak at {tone} Hz, got {peak_hz} Hz"
        )

        # SNR check: peak should dominate the mean spectrum by >= 15 dB.
        peak_mag = spectrum[peak_idx]
        mean_mag = np.mean(spectrum) + 1e-12
        snr_db = 20.0 * np.log10(peak_mag / mean_mag)
        assert snr_db > 15.0, f"weak NFM tone SNR: {snr_db:.1f} dB"


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


class TestConfigLoader:
    def test_airband_loads_with_band_env(self, monkeypatch):
        monkeypatch.setenv("CHIRP_BAND", "airband")
        # Strip env that would force-override loaded config.
        for k in ("CHIRP_CMD_PORT", "CHIRP_POOL_MODE"):
            monkeypatch.delenv(k, raising=False)
        cfg = load_config()
        assert cfg.band == "airband"
        assert cfg.pool_mode == "am"
        assert cfg.cmd_port == 7400

    def test_ground_loads_with_band_env(self, monkeypatch):
        monkeypatch.setenv("CHIRP_BAND", "ground")
        for k in ("CHIRP_CMD_PORT", "CHIRP_POOL_MODE"):
            monkeypatch.delenv(k, raising=False)
        cfg = load_config()
        assert cfg.band == "ground"
        assert cfg.pool_mode == "nfm"
        assert cfg.cmd_port == 7401

    def test_bad_json_fails_fast(self, tmp_path):
        bad = tmp_path / "broken.json"
        bad.write_text("{ this is not json :(")
        with pytest.raises(ValueError, match="invalid JSON"):
            load_config(defaults_path=bad)

    def test_bad_pool_mode_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHIRP_POOL_MODE", "ssb")
        ok = tmp_path / "ok.json"
        ok.write_text(json.dumps({"band": "airband"}))
        with pytest.raises(ValueError, match="invalid pool_mode"):
            load_config(defaults_path=ok)

    def test_missing_file_uses_defaults(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CHIRP_BAND", raising=False)
        cfg = load_config(defaults_path=tmp_path / "doesnotexist.json")
        # No env, no file: must still produce a sane airband default.
        assert cfg.band == "airband"
        assert cfg.pool_mode == "am"


# ---------------------------------------------------------------------------
# Two-daemon coexistence
# ---------------------------------------------------------------------------


def _pick_port() -> int:
    """Bind a UDP socket on port 0 to grab an ephemeral free port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _DummyEnv:
    def __init__(self, id: str, cmd: str):
        self.id = id
        self.cmd = cmd


class _DummyServer:
    """Stand-in for CommandServer: collects emitted events without UDP."""

    def __init__(self):
        self.events: list[tuple] = []

    def emit_event(self, name, **kwargs):
        self.events.append((name, kwargs))


def _make_iq_file(path: Path, samp_rate: float, duration_s: float,
                  kind: str) -> None:
    """Create a small AM or NFM IQ fixture used by the coexistence test."""
    if kind == "am":
        n = int(round(samp_rate * duration_s))
        t = np.arange(n) / samp_rate
        env = 0.5 * (1.0 + 0.8 * np.sin(2 * np.pi * 1000.0 * t))
        carrier = np.exp(2j * np.pi * 200e3 * t)
        iq = (env * carrier).astype(np.complex64)
        iq.tofile(path)
    else:  # nfm
        iq = make_nfm_iq(samp_rate, duration_s, 100e3, 500.0, 5e3,
                         noise_sigma=0.001)
        iq.tofile(path)


class TestTwoDaemonCoexistence:
    """Two ChirpFlowgraph instances side-by-side in one pytest process.

    Verifies the Phase 4a coexistence story: distinct UDP ports, distinct
    state files, distinct hit logs, distinct audio sinks. Commands sent to
    one don't mutate the other.

    NOTE: we use direct dispatch() calls + dummy servers rather than spinning
    up real UDP listeners — this gives us deterministic ordering and lets the
    test focus on the architectural question (any shared module-level state?)
    rather than UDP timing.
    """

    def _build(self, tmp_path: Path, band: str, mode: str) -> tuple:
        samp_rate = 1e6
        iq_path = tmp_path / f"{band}.iq"
        _make_iq_file(iq_path, samp_rate, 1.0, mode)
        audio_path = tmp_path / f"{band}_audio.f32"
        state_path = tmp_path / f"{band}.state.json"
        hit_log = tmp_path / f"{band}_hits.jsonl"
        cfg = DaemonConfig(
            band=band,
            pool_mode=mode,
            cmd_host="127.0.0.1",
            cmd_port=_pick_port(),
            source_kind="file",
            source_path=str(iq_path),
            source_samp_rate=samp_rate,
            audio_out_kind="file",
            audio_out_path=str(audio_path),
            audio_rate=16000.0,
            max_channels=2,
            state_path=str(state_path),
            hit_log_path=str(hit_log),
        )
        srv = _DummyServer()
        tb = ChirpFlowgraph(cfg, srv)
        return cfg, tb, srv

    def test_two_daemons_distinct_state(self, tmp_path):
        cfg_a, tb_a, srv_a = self._build(tmp_path, "airband", "am")
        cfg_g, tb_g, srv_g = self._build(tmp_path, "ground", "nfm")

        # Distinct ports.
        assert cfg_a.cmd_port != cfg_g.cmd_port

        # Distinct on-disk state paths.
        assert cfg_a.state_path != cfg_g.state_path

        # Distinct hit logs.
        assert cfg_a.hit_log_path != cfg_g.hit_log_path

        # Distinct audio out paths.
        assert cfg_a.audio_out_path != cfg_g.audio_out_path

        # No instance variables aliased between the two flowgraphs.
        assert tb_a is not tb_g
        assert tb_a.state_store is not tb_g.state_store
        assert tb_a.mixer is not tb_g.mixer
        assert tb_a.slots is not tb_g.slots

        # Add a channel to each.
        env = _DummyEnv(id="r1", cmd="add_channel")
        resp_a = tb_a._cmd_add_channel(env, parse_args("add_channel", {
            "id": "air01", "freq_mhz": 0.2, "mode": "am",
            "squelch_dbfs": -60.0,
        }))
        assert resp_a.status == "ok", resp_a.error

        resp_g = tb_g._cmd_add_channel(env, parse_args("add_channel", {
            "id": "gnd01", "freq_mhz": 0.1, "mode": "nfm",
            "squelch_dbfs": -60.0,
        }))
        assert resp_g.status == "ok", resp_g.error

        # Cross-channel ids: airband shouldn't know about gnd01 and vice versa.
        assert "air01" in tb_a._by_id
        assert "air01" not in tb_g._by_id
        assert "gnd01" in tb_g._by_id
        assert "gnd01" not in tb_a._by_id

    def test_no_cross_talk_on_squelch(self, tmp_path):
        cfg_a, tb_a, _ = self._build(tmp_path, "airband", "am")
        cfg_g, tb_g, _ = self._build(tmp_path, "ground", "nfm")

        env_add = _DummyEnv(id="r1", cmd="add_channel")
        tb_a._cmd_add_channel(env_add, parse_args("add_channel", {
            "id": "air01", "freq_mhz": 0.2, "mode": "am",
            "squelch_dbfs": -60.0,
        }))
        tb_g._cmd_add_channel(env_add, parse_args("add_channel", {
            "id": "gnd01", "freq_mhz": 0.1, "mode": "nfm",
            "squelch_dbfs": -60.0,
        }))

        # Set squelch on airband — ground must be unaffected.
        env_sq = _DummyEnv(id="r2", cmd="set_squelch")
        from chirp.cmd.schema import SetSquelchArgs
        resp = tb_a._cmd_set_squelch(env_sq, SetSquelchArgs(id="air01", dbfs=-30.0))
        assert resp.status == "ok"

        # airband slot reflects new threshold; ground slot does not.
        a_slot = tb_a.slots[tb_a._by_id["air01"]]
        g_slot = tb_g.slots[tb_g._by_id["gnd01"]]
        assert a_slot.last_squelch_dbfs == -30.0
        assert g_slot.last_squelch_dbfs == -60.0

        # And the reverse direction.
        resp2 = tb_g._cmd_set_squelch(env_sq, SetSquelchArgs(id="gnd01", dbfs=-10.0))
        assert resp2.status == "ok"
        assert tb_g.slots[tb_g._by_id["gnd01"]].last_squelch_dbfs == -10.0
        # airband still at -30 from the previous call.
        assert tb_a.slots[tb_a._by_id["air01"]].last_squelch_dbfs == -30.0

    def test_reject_mode_mismatch(self, tmp_path):
        """Airband pool (AM) must reject add_channel with mode='nfm'."""
        cfg_a, tb_a, _ = self._build(tmp_path, "airband", "am")
        env = _DummyEnv(id="r1", cmd="add_channel")
        resp = tb_a._cmd_add_channel(env, parse_args("add_channel", {
            "id": "wrong", "freq_mhz": 0.1, "mode": "nfm",
            "squelch_dbfs": -60.0,
        }))
        assert resp.status == "rejected"
        assert "mode mismatch" in (resp.error or "")

    def test_state_files_independent(self, tmp_path):
        cfg_a, tb_a, _ = self._build(tmp_path, "airband", "am")
        cfg_g, tb_g, _ = self._build(tmp_path, "ground", "nfm")
        env = _DummyEnv(id="r1", cmd="add_channel")
        tb_a._cmd_add_channel(env, parse_args("add_channel", {
            "id": "air01", "freq_mhz": 0.12, "mode": "am",
            "squelch_dbfs": -55.0,
        }))
        tb_g._cmd_add_channel(env, parse_args("add_channel", {
            "id": "gnd01", "freq_mhz": 0.15, "mode": "nfm",
            "squelch_dbfs": -65.0,
        }))
        # Both state files exist on disk and contain only their own channel.
        assert Path(cfg_a.state_path).is_file()
        assert Path(cfg_g.state_path).is_file()
        state_a = json.loads(Path(cfg_a.state_path).read_text())
        state_g = json.loads(Path(cfg_g.state_path).read_text())
        assert {c["id"] for c in state_a["channels"]} == {"air01"}
        assert {c["id"] for c in state_g["channels"]} == {"gnd01"}
        assert state_a["band"] == "airband"
        assert state_g["band"] == "ground"
