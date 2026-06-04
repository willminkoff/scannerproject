"""Phase 2 unit + integration tests.

Covers:
  - chirp.dsp.mixer: AudioMixer hier_block correctness (1, 4, 32 inputs;
    master_gain in dB), NullAudioSource sanity.
  - chirp.cmd.schema Phase 2 additions: AddChannelArgs batch shape (both
    legacy single AND `{"channels": [...]}` form), SetMasterGainArgs and
    ResetArgs bounds, duplicate-id rejection.
  - chirp.hit_detector: state machine on synthetic Channel mocks.
  - chirp.daemon Phase 2 dispatch: batch add_channel end-to-end on the
    real ChirpFlowgraph, set_master_gain effect on audio file amplitude,
    reset wipes pool + zeroes master gain + clears state file.
  - State restore: a daemon booted with a populated state file restores
    those channels into live slots.
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
from pydantic import ValidationError

from chirp.cmd.schema import (
    AddChannelArgs,
    COMMAND_ARGS,
    ResetArgs,
    SetMasterGainArgs,
    parse_args,
)
from chirp.cmd.server import CommandServer, ServerConfig
from chirp.daemon import ChirpFlowgraph, DaemonConfig
from chirp.dsp.mixer import AudioMixer, NullAudioSource
from chirp.hit_detector import HitDetector
from chirp.state import ChannelState, ChirpState, StateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _udp_roundtrip(port: int, payload: bytes, timeout: float = 2.0) -> bytes:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(timeout)
        s.sendto(payload, ("127.0.0.1", port))
        data, _ = s.recvfrom(16384)
        return data
    finally:
        s.close()


def _write_am_iq(path, samp_rate, duration_s, carrier_hz, tone_hz=1000.0,
                 mod_index=0.8, noise_sigma=0.002):
    n = int(round(samp_rate * duration_s))
    t = np.arange(n, dtype=np.float64) / samp_rate
    envelope = 0.5 * (1.0 + mod_index * np.sin(2 * np.pi * tone_hz * t))
    carrier = np.exp(2j * np.pi * carrier_hz * t)
    iq = (envelope * carrier).astype(np.complex64)
    rng = np.random.default_rng(7)
    noise = (rng.normal(0, noise_sigma, n) + 1j * rng.normal(0, noise_sigma, n)).astype(np.complex64)
    (iq + noise).tofile(path)


# ---------------------------------------------------------------------------
# Mixer
# ---------------------------------------------------------------------------


class TestAudioMixer:
    def test_construct_various_sizes(self):
        for n in (1, 4, 32):
            m = AudioMixer(n_inputs=n, master_gain_db=0.0)
            assert m.n_inputs == n

    def test_rejects_zero_inputs(self):
        with pytest.raises(ValueError):
            AudioMixer(n_inputs=0)

    def test_master_gain_db_to_linear(self):
        m = AudioMixer(n_inputs=4, master_gain_db=0.0)
        # 0 dB -> 1.0
        assert abs(m.master.k() - 1.0) < 1e-9
        m.set_master_gain(6.02059991)
        # 6.02 dB -> ~2.0
        assert abs(m.master.k() - 2.0) < 1e-3
        m.set_master_gain(-20.0)
        assert abs(m.master.k() - 0.1) < 1e-3

    def test_master_gain_clamps_extremes(self):
        m = AudioMixer(n_inputs=1, master_gain_db=0.0)
        m.set_master_gain(1e6)  # should clamp at +60 dB
        assert m.master.k() <= 10.0 ** (60.0 / 20.0) + 1e-3

    def test_null_source_runs(self):
        # Smoke test: just confirm we can instantiate. Wiring is GR's problem.
        n = NullAudioSource()
        assert n is not None


# ---------------------------------------------------------------------------
# Phase 2 schema
# ---------------------------------------------------------------------------


class TestPhase2Schema:
    def test_legacy_single_form_still_works(self):
        a = parse_args("add_channel", {
            "id": "ch01", "freq_mhz": 121.025, "mode": "am",
            "squelch_dbfs": -60.0, "gain_db": 0.0, "label": "TWR",
        })
        assert isinstance(a, AddChannelArgs)
        assert len(a.channels) == 1
        assert a.channels[0].id == "ch01"

    def test_batch_form(self):
        a = parse_args("add_channel", {
            "channels": [
                {"id": "ch01", "freq_mhz": 121.025, "mode": "am", "squelch_dbfs": -60.0},
                {"id": "ch02", "freq_mhz": 121.900, "mode": "am", "squelch_dbfs": -60.0,
                 "gain_db": -3.0, "label": "GND"},
            ],
        })
        assert len(a.channels) == 2
        assert {c.id for c in a.channels} == {"ch01", "ch02"}

    def test_batch_rejects_duplicate_ids_in_batch(self):
        with pytest.raises(ValidationError):
            parse_args("add_channel", {
                "channels": [
                    {"id": "ch01", "freq_mhz": 121.0, "mode": "am", "squelch_dbfs": -60.0},
                    {"id": "ch01", "freq_mhz": 122.0, "mode": "am", "squelch_dbfs": -60.0},
                ],
            })

    def test_batch_form_does_not_allow_mixing_with_legacy_keys(self):
        with pytest.raises(ValidationError):
            parse_args("add_channel", {
                "channels": [{"id": "x", "freq_mhz": 121.0, "mode": "am", "squelch_dbfs": -60.0}],
                "id": "y",
            })

    def test_batch_empty_rejected(self):
        with pytest.raises(ValidationError):
            parse_args("add_channel", {"channels": []})

    def test_set_master_gain_bounds(self):
        parse_args("set_master_gain", {"db": 3.0})
        parse_args("set_master_gain", {"db": -20.0})
        parse_args("set_master_gain", {"db": 40.0})
        with pytest.raises(ValidationError):
            parse_args("set_master_gain", {"db": 50.0})
        with pytest.raises(ValidationError):
            parse_args("set_master_gain", {"db": -30.0})

    def test_reset_takes_no_args(self):
        parse_args("reset", {})
        with pytest.raises(ValidationError):
            parse_args("reset", {"surprise": True})

    def test_phase2_commands_in_dispatch_table(self):
        for cmd in ("set_master_gain", "reset", "add_channel"):
            assert cmd in COMMAND_ARGS


# ---------------------------------------------------------------------------
# Hit detector (mocked Channels)
# ---------------------------------------------------------------------------


class _FakeChannel:
    def __init__(self, open: bool = False, level: float = -90.0):
        self._open = open
        self._lvl = level

    def get_squelch_open(self) -> bool:
        return self._open

    def get_signal_level_dbfs(self) -> float:
        return self._lvl


class _FakeSlot:
    def __init__(self, index, user_id, channel, freq_mhz=121.0, claimed_at=None):
        self.index = index
        self.user_id = user_id
        self.channel = channel
        self.last_freq_mhz = freq_mhz
        self.claimed_at = claimed_at if claimed_at is not None else time.time()


class _FakeServer:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit_event(self, evt, **kwargs):
        self.events.append((evt, kwargs))


class TestHitDetector:
    def test_emits_hit_start_on_open_transition(self, tmp_path):
        slots = []
        ch = _FakeChannel(open=False, level=-90.0)
        slots.append(_FakeSlot(0, "ch01", ch))
        srv = _FakeServer()
        hd = HitDetector(slots=slots, server=srv,
                         hit_log_path=str(tmp_path / "hits.jsonl"),
                         poll_s=0.05, warmup_s=0.0)
        hd._tick()  # establish initial closed
        ch._open = True
        ch._lvl = -40.0
        hd._tick()  # detect transition
        evts = [e for e in srv.events if e[0] == "hit_start"]
        assert len(evts) == 1
        assert evts[0][1]["ch"] == "ch01"

    def test_emits_hit_end_with_peak_and_duration(self, tmp_path):
        slots = []
        ch = _FakeChannel(open=False, level=-90.0)
        slots.append(_FakeSlot(0, "ch02", ch, freq_mhz=121.5,
                               claimed_at=time.time() - 5.0))
        srv = _FakeServer()
        log_path = tmp_path / "hits.jsonl"
        hd = HitDetector(slots=slots, server=srv,
                         hit_log_path=str(log_path),
                         poll_s=0.01, warmup_s=0.0)
        hd._tick()  # closed
        ch._open = True
        ch._lvl = -50.0
        hd._tick()  # start
        time.sleep(0.01)  # ensure non-zero duration
        ch._lvl = -30.0  # peak rises
        hd._tick()  # peak update
        ch._lvl = -40.0  # then dips
        hd._tick()
        time.sleep(0.01)
        ch._open = False
        hd._tick()  # end
        ends = [e for e in srv.events if e[0] == "hit_end"]
        assert len(ends) == 1
        assert ends[0][1]["peak_dbfs"] == pytest.approx(-30.0, abs=0.05)
        assert ends[0][1]["duration_s"] > 0.0
        # JSONL line was appended.
        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["ch"] == "ch02"
        assert rec["freq_mhz"] == 121.5

    def test_warmup_flag_on_recently_claimed_channel(self, tmp_path):
        slots = []
        ch = _FakeChannel(open=False, level=-90.0)
        # Claimed JUST NOW — within warmup window.
        slots.append(_FakeSlot(0, "ch03", ch, claimed_at=time.time()))
        srv = _FakeServer()
        hd = HitDetector(slots=slots, server=srv,
                         hit_log_path=str(tmp_path / "hits.jsonl"),
                         poll_s=0.01, warmup_s=10.0)
        hd._tick()
        ch._open = True
        hd._tick()
        starts = [e for e in srv.events if e[0] == "hit_start"]
        assert starts and starts[0][1]["warmup"] is True

    def test_log_disabled_when_path_unwritable(self):
        srv = _FakeServer()
        # /dev/null/foo is unwritable as a directory.
        hd = HitDetector(slots=[], server=srv,
                         hit_log_path="/dev/null/cannot-create/x.jsonl",
                         poll_s=0.1, warmup_s=0.0)
        assert hd.log_disabled is True

    def test_idle_slot_cleared_after_removal(self, tmp_path):
        slots = []
        ch = _FakeChannel(open=True, level=-50.0)
        slot = _FakeSlot(0, "ch04", ch, claimed_at=time.time() - 5.0)
        slots.append(slot)
        srv = _FakeServer()
        hd = HitDetector(slots=slots, server=srv,
                         hit_log_path=str(tmp_path / "hits.jsonl"),
                         poll_s=0.01, warmup_s=0.0)
        hd._tick()  # open, no transition (first tick treats as transition)
        # In our impl first tick sees prev=False -> is_open True = hit_start
        assert any(e[0] == "hit_start" for e in srv.events)
        # Now release the slot (simulate remove_channel).
        slot.user_id = None
        hd._tick()
        # No new event from the now-empty slot.
        assert hd._in_flight.get(0) is None


# ---------------------------------------------------------------------------
# Phase 2 daemon integration: 4-slot mixer + state restore + master gain
# ---------------------------------------------------------------------------


@pytest.fixture
def small_daemon(tmp_path):
    """Build a real 4-slot daemon over a synthetic IQ file. Carriers at:
    +200 kHz (ch1) and +400 kHz (ch2). We use 4 slots so we can also exercise
    pool_free arithmetic.
    """
    samp_rate = 1e6
    iq_path = tmp_path / "twocar.iq"
    n = int(samp_rate * 3.0)
    t = np.arange(n, dtype=np.float64) / samp_rate
    env1 = 0.5 * (1.0 + 0.8 * np.sin(2 * np.pi * 1000 * t))
    env2 = 0.5 * (1.0 + 0.8 * np.sin(2 * np.pi * 1500 * t))
    iq = (env1 * np.exp(2j * np.pi * 200e3 * t) +
          env2 * np.exp(2j * np.pi * 400e3 * t)).astype(np.complex64)
    rng = np.random.default_rng(11)
    noise = (rng.normal(0, 0.002, n) + 1j * rng.normal(0, 0.002, n)).astype(np.complex64)
    (iq + noise).tofile(iq_path)

    audio_path = tmp_path / "mix.f32"
    state_path = tmp_path / "x.state.json"
    hit_log = tmp_path / "hits.jsonl"

    cfg = DaemonConfig(
        band="airband",
        cmd_port=18600 + (os.getpid() % 500),
        source_kind="file",
        source_path=str(iq_path),
        source_samp_rate=samp_rate,
        audio_out_kind="file",
        audio_out_path=str(audio_path),
        audio_rate=16000.0,
        max_channels=4,
        state_path=str(state_path),
        hit_log_path=str(hit_log),
    )
    server = CommandServer(
        ServerConfig(host=cfg.cmd_host, port=cfg.cmd_port),
        dispatch=lambda env, args: tb.dispatch(env, args),
    )
    tb = ChirpFlowgraph(cfg, server, state_store=StateStore(state_path))
    tb.start()
    tb.start_health()
    server.start()
    try:
        yield cfg, tb, server, audio_path, state_path, hit_log
    finally:
        tb.stop_health()
        tb.shutdown_drain()
        tb.stop()
        tb.wait()
        server.stop()


class TestPhase2Daemon:
    def test_batch_add_two_channels(self, small_daemon):
        cfg, _tb, _server, _audio, state_path, _ = small_daemon
        body = json.dumps({"v": 1, "id": "rb", "cmd": "add_channel", "args": {
            "channels": [
                {"id": "ch01", "freq_mhz": 0.2, "mode": "am", "squelch_dbfs": -85.0, "label": "C1"},
                {"id": "ch02", "freq_mhz": 0.4, "mode": "am", "squelch_dbfs": -85.0, "label": "C2"},
            ],
        }}).encode()
        resp = json.loads(_udp_roundtrip(cfg.cmd_port, body))
        assert resp["status"] == "ok", resp
        assert resp["data"]["count"] == 2

        st_body = json.dumps({"v": 1, "id": "st", "cmd": "get_status", "args": {}}).encode()
        st = json.loads(_udp_roundtrip(cfg.cmd_port, st_body))
        assert st["status"] == "ok"
        assert st["data"]["max_channels"] == 4
        assert st["data"]["pool_free"] == 2
        assert len(st["data"]["channels"]) == 2

        # State file was persisted.
        assert state_path.is_file()
        on_disk = json.loads(state_path.read_text())
        assert len(on_disk["channels"]) == 2
        assert {c["id"] for c in on_disk["channels"]} == {"ch01", "ch02"}

    def test_batch_pool_exhaustion_rolls_back(self, small_daemon):
        """Batch larger than free pool returns rejected and DOES NOT
        partially add anything."""
        cfg, _tb, _server, _audio, _, _ = small_daemon
        body = json.dumps({"v": 1, "id": "rb", "cmd": "add_channel", "args": {
            "channels": [
                {"id": f"x{i:02d}", "freq_mhz": 0.1 + 0.02 * i, "mode": "am",
                 "squelch_dbfs": -85.0}
                for i in range(5)  # 4-slot pool, 5 requested
            ],
        }}).encode()
        resp = json.loads(_udp_roundtrip(cfg.cmd_port, body))
        assert resp["status"] == "rejected"
        st = json.loads(_udp_roundtrip(cfg.cmd_port, json.dumps({
            "v": 1, "id": "st", "cmd": "get_status", "args": {},
        }).encode()))
        assert st["data"]["pool_free"] == 4  # untouched

    def test_set_master_gain_changes_audio_amplitude(self, small_daemon):
        cfg, _tb, _server, audio_path, _, _ = small_daemon
        # Add one channel with squelch wide open.
        _udp_roundtrip(cfg.cmd_port, json.dumps({"v": 1, "id": "a1",
            "cmd": "add_channel", "args": {"id": "ch01", "freq_mhz": 0.2,
            "mode": "am", "squelch_dbfs": -90.0}}).encode())
        time.sleep(1.0)  # AGC settle
        mark0 = audio_path.stat().st_size
        time.sleep(0.5)
        mark1 = audio_path.stat().st_size
        # Crank master gain to -20 dB.
        resp = json.loads(_udp_roundtrip(cfg.cmd_port, json.dumps({"v": 1,
            "id": "mg", "cmd": "set_master_gain", "args": {"db": -20.0},
        }).encode()))
        assert resp["status"] == "ok"
        time.sleep(0.6)
        mark2 = audio_path.stat().st_size
        time.sleep(0.5)
        mark3 = audio_path.stat().st_size
        # Read each window. The stream kept flowing (size grows), but RMS
        # should drop ~10x with -20 dB master gain.
        assert mark1 > mark0 and mark3 > mark2
        with audio_path.open("rb") as f:
            f.seek(mark0)
            loud = np.frombuffer(f.read(mark1 - mark0), dtype=np.float32)
            f.seek(mark2)
            quiet = np.frombuffer(f.read(mark3 - mark2), dtype=np.float32)
        loud_rms = float(np.sqrt(np.mean(loud.astype(np.float64) ** 2)))
        quiet_rms = float(np.sqrt(np.mean(quiet.astype(np.float64) ** 2)))
        assert loud_rms > 0
        # -20 dB = 10x amplitude drop; require at least 5x to allow for
        # AGC overshoot / boundary noise.
        assert quiet_rms * 5.0 < loud_rms, f"loud={loud_rms} quiet={quiet_rms}"

    def test_reset_clears_pool_and_state(self, small_daemon):
        cfg, _tb, _server, _audio, state_path, _ = small_daemon
        _udp_roundtrip(cfg.cmd_port, json.dumps({"v": 1, "id": "a1",
            "cmd": "add_channel", "args": {"id": "ch01", "freq_mhz": 0.2,
            "mode": "am", "squelch_dbfs": -85.0}}).encode())
        _udp_roundtrip(cfg.cmd_port, json.dumps({"v": 1, "id": "mg",
            "cmd": "set_master_gain", "args": {"db": 6.0}}).encode())
        resp = json.loads(_udp_roundtrip(cfg.cmd_port, json.dumps({"v": 1,
            "id": "rs", "cmd": "reset", "args": {}}).encode()))
        assert resp["status"] == "ok"
        st = json.loads(_udp_roundtrip(cfg.cmd_port, json.dumps({"v": 1,
            "id": "st", "cmd": "get_status", "args": {}}).encode()))
        assert st["data"]["pool_free"] == 4
        assert st["data"]["channels"] == []
        assert st["data"]["master_gain_db"] == 0.0
        # State file rewritten to empty.
        on_disk = json.loads(state_path.read_text())
        assert on_disk["channels"] == []
        assert on_disk["master_gain_db"] == 0.0


class TestStateRestore:
    def test_daemon_restores_persisted_channels(self, tmp_path):
        # Pre-seed a state file with two channels.
        state_path = tmp_path / "boot.state.json"
        StateStore(state_path).save(ChirpState(
            band="airband",
            master_gain_db=3.0,
            channels=[
                ChannelState(id="alpha", freq_mhz=0.2, mode="am",
                             squelch_dbfs=-80.0, gain_db=0.0, label="A"),
                ChannelState(id="bravo", freq_mhz=0.3, mode="am",
                             squelch_dbfs=-80.0, gain_db=-2.0, label="B"),
            ],
        ))
        # Synthetic IQ file.
        samp_rate = 1e6
        iq_path = tmp_path / "x.iq"
        _write_am_iq(iq_path, samp_rate, 2.0, 200e3)
        audio = tmp_path / "out.f32"
        hits = tmp_path / "h.jsonl"

        cfg = DaemonConfig(
            band="airband",
            cmd_port=18900 + (os.getpid() % 400),
            source_kind="file",
            source_path=str(iq_path),
            source_samp_rate=samp_rate,
            audio_out_kind="file",
            audio_out_path=str(audio),
            audio_rate=16000.0,
            max_channels=4,
            state_path=str(state_path),
            hit_log_path=str(hits),
        )
        server = CommandServer(
            ServerConfig(host=cfg.cmd_host, port=cfg.cmd_port),
            dispatch=lambda env, args: tb.dispatch(env, args),
        )
        tb = ChirpFlowgraph(cfg, server, state_store=StateStore(state_path))
        tb.start()
        tb.start_health()
        server.start()
        try:
            restored = tb.restore_from_state()
            assert restored == 2
            st = json.loads(_udp_roundtrip(cfg.cmd_port, json.dumps({"v": 1,
                "id": "st", "cmd": "get_status", "args": {}}).encode()))
            assert st["data"]["pool_free"] == 2
            assert {c["id"] for c in st["data"]["channels"]} == {"alpha", "bravo"}
            assert st["data"]["master_gain_db"] == 3.0
        finally:
            tb.stop_health()
            tb.shutdown_drain()
            tb.stop()
            tb.wait()
            server.stop()
