"""Phase 1 unit + integration tests.

Run from repo root:
    python3 -m pytest chirp/tests/ -v

Covers:
  - schema: envelope parsing, arg validation, response shape, validator edges
  - server: end-to-end UDP roundtrip with a mock dispatch
  - channel: hier_block instantiation + hot-setter state
  - daemon dispatch (without spawning the real process): synthesized IQ file
    + ChirpFlowgraph + UDP commands → audio file grows when squelch is open,
    grows much less (silence) when slammed shut.
"""

from __future__ import annotations

import json
import os
import socket
import struct
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
    Envelope,
    PROTOCOL_VERSION,
    Response,
    SetSquelchArgs,
    parse_args,
    parse_envelope,
)
from chirp.cmd.server import CommandServer, ServerConfig
from chirp.dsp.channel import Channel
from chirp.dsp.source_file import FileIQSource
from chirp.daemon import ChirpFlowgraph, DaemonConfig


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestSchema:
    def test_envelope_round_trip(self):
        raw = b'{"v":1,"id":"x","cmd":"get_status","args":{}}'
        env = parse_envelope(raw)
        assert env.v == 1 and env.id == "x" and env.cmd == "get_status"

    def test_envelope_wrong_version(self):
        with pytest.raises(ValidationError):
            parse_envelope(b'{"v":2,"id":"x","cmd":"get_status","args":{}}')

    def test_envelope_missing_id(self):
        with pytest.raises(ValidationError):
            parse_envelope(b'{"v":1,"cmd":"get_status","args":{}}')

    def test_envelope_extra_field_rejected(self):
        # extra="forbid" on the envelope
        with pytest.raises(ValidationError):
            parse_envelope(b'{"v":1,"id":"x","cmd":"get_status","args":{},"junk":1}')

    def test_add_channel_args_ok(self):
        # Phase 2: AddChannelArgs is now a batch wrapper. The legacy
        # single-channel wire form is still accepted and normalises to
        # a 1-element `channels` list.
        a = parse_args("add_channel", {
            "id": "ch01", "freq_mhz": 121.025, "mode": "am",
            "squelch_dbfs": -68.0, "gain_db": 0.0, "label": "TWR",
        })
        assert isinstance(a, AddChannelArgs)
        assert len(a.channels) == 1
        assert a.channels[0].freq_mhz == 121.025

    def test_add_channel_accepts_nfm(self):
        # Phase 4a: NFM is now a valid mode (ground band uses it).
        a = parse_args("add_channel", {
            "id": "ch01", "freq_mhz": 162.4, "mode": "nfm",
            "squelch_dbfs": -60.0,
        })
        assert len(a.channels) == 1
        assert a.channels[0].mode == "nfm"

    def test_add_channel_rejects_unknown_mode(self):
        with pytest.raises(ValidationError):
            parse_args("add_channel", {
                "id": "ch01", "freq_mhz": 162.4, "mode": "ssb",
                "squelch_dbfs": -60.0,
            })

    @pytest.mark.parametrize("bad", [
        {"id": "", "freq_mhz": 121.0, "mode": "am", "squelch_dbfs": -60.0},
        {"id": "ch01", "freq_mhz": 0.0, "mode": "am", "squelch_dbfs": -60.0},
        {"id": "ch01", "freq_mhz": -1.0, "mode": "am", "squelch_dbfs": -60.0},
        {"id": "ch01", "freq_mhz": 121.0, "mode": "am", "squelch_dbfs": -200.0},
        {"id": "ch01", "freq_mhz": 121.0, "mode": "am", "squelch_dbfs": 5.0},
        {"id": "ch01", "freq_mhz": 121.0, "mode": "am", "squelch_dbfs": -60.0, "gain_db": 99.0},
        {"id": "ch01", "freq_mhz": 121.0, "mode": "am", "squelch_dbfs": -60.0, "gain_db": -50.0},
    ])
    def test_add_channel_bad_inputs(self, bad):
        with pytest.raises(ValidationError):
            parse_args("add_channel", bad)

    def test_set_squelch_bounds(self):
        parse_args("set_squelch", {"id": "ch01", "dbfs": -60.0})
        with pytest.raises(ValidationError):
            parse_args("set_squelch", {"id": "ch01", "dbfs": -130.0})

    def test_command_set_includes_phase1_commands(self):
        # Phase 1 commands must still be there; Phase 2 ADDS to the set.
        phase1 = {
            "add_channel", "remove_channel", "set_squelch",
            "set_freq", "set_gain", "get_status",
        }
        assert phase1.issubset(set(COMMAND_ARGS.keys()))

    def test_response_factories(self):
        assert Response.make_ok("x", {"a": 1}).status == "ok"
        assert Response.make_rejected("x", "nope").status == "rejected"
        e = Response.make_error("x", "boom")
        assert e.status == "error" and e.error == "boom"
        # Round-trip through JSON.
        s = e.model_dump_json()
        parsed = json.loads(s)
        assert parsed["v"] == PROTOCOL_VERSION
        assert parsed["status"] == "error"


# ---------------------------------------------------------------------------
# UDP command server
# ---------------------------------------------------------------------------


def _udp_roundtrip(port: int, payload: bytes, timeout: float = 2.0) -> bytes:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(timeout)
        s.sendto(payload, ("127.0.0.1", port))
        data, _ = s.recvfrom(8192)
        return data
    finally:
        s.close()


@pytest.fixture
def mock_server():
    """Spin up a CommandServer with a stub dispatch that records calls."""
    calls: list[tuple[str, dict]] = []
    state: dict[str, dict] = {}

    def dispatch(env, args):
        calls.append((env.cmd, args.model_dump()))
        if env.cmd == "add_channel":
            # Phase 2: args is a batch (channels: list[ChannelArgs]).
            for ch in args.channels:
                state[ch.id] = ch.model_dump()
            return Response.make_ok(env.id, {"slot": 0})
        if env.cmd == "remove_channel":
            state.pop(args.id, None)
            return Response.make_ok(env.id)
        if env.cmd == "get_status":
            return Response.make_ok(env.id, {"channels": list(state)})
        return Response.make_rejected(env.id, "nyi")

    srv = CommandServer(ServerConfig(port=0), dispatch)
    # ServerConfig with port=0 doesn't auto-pick; specify a known port.
    srv.cfg.port = 17500 + (os.getpid() % 1000)
    srv.start()
    try:
        yield srv, calls, state
    finally:
        srv.stop()


class TestServer:
    def test_happy_path_add(self, mock_server):
        srv, calls, _state = mock_server
        port = srv.cfg.port
        body = json.dumps({"v": 1, "id": "r1", "cmd": "add_channel", "args": {
            "id": "ch01", "freq_mhz": 121.025, "mode": "am",
            "squelch_dbfs": -68.0, "gain_db": 0.0, "label": "TWR",
        }}).encode()
        resp = json.loads(_udp_roundtrip(port, body))
        assert resp["status"] == "ok"
        assert resp["id"] == "r1"
        assert resp["data"]["slot"] == 0
        assert calls and calls[0][0] == "add_channel"

    def test_unknown_cmd(self, mock_server):
        srv, _calls, _state = mock_server
        body = json.dumps({"v": 1, "id": "r2", "cmd": "bogus", "args": {}}).encode()
        resp = json.loads(_udp_roundtrip(srv.cfg.port, body))
        assert resp["status"] == "rejected"
        assert "bogus" in resp["error"]

    def test_malformed_json(self, mock_server):
        srv, _calls, _state = mock_server
        resp = json.loads(_udp_roundtrip(srv.cfg.port, b"not a json packet"))
        assert resp["status"] == "error"
        assert resp["id"] == "unknown"

    def test_arg_validation_failure(self, mock_server):
        srv, _calls, _state = mock_server
        body = json.dumps({"v": 1, "id": "r3", "cmd": "add_channel", "args": {
            "id": "ch01", "freq_mhz": 121.0, "mode": "am", "squelch_dbfs": -500,
        }}).encode()
        resp = json.loads(_udp_roundtrip(srv.cfg.port, body))
        assert resp["status"] == "rejected"
        assert "squelch_dbfs" in resp["error"]


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------


class TestChannel:
    def test_instantiate_and_setters(self):
        ch = Channel(samp_rate=1e6, audio_rate=16000.0,
                     center_freq_offset=100e3, squelch_dbfs=-60.0, gain_db=0.0)
        assert ch.center_freq_offset == 100e3
        ch.set_center_freq_offset(250e3)
        assert ch.center_freq_offset == 250e3
        ch.set_squelch(-40.0)
        assert ch.squelch_dbfs == -40.0
        ch.set_gain(6.0)
        assert ch.gain_db == 6.0

    def test_rejects_low_samp_rate(self):
        with pytest.raises(ValueError):
            Channel(samp_rate=500e3)

    def test_snapshot_shape(self):
        ch = Channel(samp_rate=1e6)
        snap = ch.snapshot()
        for k in ("center_freq_offset_hz", "squelch_dbfs", "gain_db",
                  "audio_rate", "samp_rate", "signal_level_dbfs", "squelch_open"):
            assert k in snap


# ---------------------------------------------------------------------------
# Daemon dispatch w/ synthesized IQ → audio file growth
# ---------------------------------------------------------------------------


def _write_am_iq(path: Path, samp_rate: float, duration_s: float,
                 carrier_hz: float, tone_hz: float, mod_index: float = 0.8) -> None:
    """Write a synthetic AM-modulated complex baseband IQ file (fc32).

    Carrier at `carrier_hz` (relative to baseband center 0 Hz).
    Modulated by an audio tone at `tone_hz` (envelope detection will recover
    that tone). Includes a small noise floor so the squelch threshold is
    meaningful.
    """
    n = int(round(samp_rate * duration_s))
    t = np.arange(n, dtype=np.float64) / samp_rate
    envelope = 0.5 * (1.0 + mod_index * np.sin(2 * np.pi * tone_hz * t))
    carrier = np.exp(2j * np.pi * carrier_hz * t)
    iq = (envelope * carrier).astype(np.complex64)
    # Add a touch of complex AWGN.
    noise = (np.random.normal(0, 0.002, n) + 1j * np.random.normal(0, 0.002, n)).astype(np.complex64)
    (iq + noise).tofile(path)


@pytest.fixture
def integration_env(tmp_path):
    """Synthesize a small IQ file + build a ChirpFlowgraph + start UDP server."""
    samp_rate = 1e6
    duration = 4.0  # 4 seconds, ~32 MB on disk — fine for a unit test
    carrier_hz = 200e3
    tone_hz = 1000.0
    iq_path = tmp_path / "am_test.iq"
    audio_path = tmp_path / "out.f32"
    _write_am_iq(iq_path, samp_rate, duration, carrier_hz, tone_hz)

    cfg = DaemonConfig(
        band="airband",
        cmd_port=17600 + (os.getpid() % 500),
        source_kind="file",
        source_path=str(iq_path),
        source_samp_rate=samp_rate,
        audio_out_kind="file",
        audio_out_path=str(audio_path),
        audio_rate=16000.0,
        max_channels=1,
    )

    server = CommandServer(
        ServerConfig(host=cfg.cmd_host, port=cfg.cmd_port),
        dispatch=lambda env, args: tb.dispatch(env, args),
    )
    tb = ChirpFlowgraph(cfg, server)
    tb.start()
    tb.start_health()
    server.start()
    try:
        yield cfg, tb, server, audio_path
    finally:
        tb.stop_health()
        tb.stop()
        tb.wait()
        server.stop()


class TestDaemonDispatch:
    def test_squelch_gates_audio_amplitude(self, integration_env):
        """The per-channel squelch is non-blocking (design intent: keeps
        parallel channels in stream-sync for the future adder), so a closed
        squelch zeros the samples rather than halting the stream. The audio
        file therefore keeps growing in byte size either way — what changes
        is the amplitude of the float32 samples written. Open: tone visible;
        closed: ~all zeros.
        """
        cfg, _tb, _server, audio_path = integration_env
        sample_size = 4  # float32

        # Add a channel at +200 kHz with squelch wide open.
        body = json.dumps({"v": 1, "id": "r-add", "cmd": "add_channel", "args": {
            "id": "ch01", "freq_mhz": 0.2, "mode": "am",
            "squelch_dbfs": -90.0, "gain_db": 0.0,
        }}).encode()
        resp = json.loads(_udp_roundtrip(cfg.cmd_port, body))
        assert resp["status"] == "ok", resp

        # Let the AGC settle, then sample the OPEN-squelch window.
        time.sleep(1.0)
        open_mark_bytes = audio_path.stat().st_size
        time.sleep(0.5)
        post_open_bytes = audio_path.stat().st_size

        # Slam squelch shut.
        body2 = json.dumps({"v": 1, "id": "r-sq", "cmd": "set_squelch", "args": {
            "id": "ch01", "dbfs": 0.0,
        }}).encode()
        resp2 = json.loads(_udp_roundtrip(cfg.cmd_port, body2))
        assert resp2["status"] == "ok"

        # Drain transition + sample the CLOSED-squelch window.
        time.sleep(0.6)
        closed_mark_bytes = audio_path.stat().st_size
        time.sleep(0.6)
        post_closed_bytes = audio_path.stat().st_size

        # Sanity: stream kept flowing through both windows.
        assert post_open_bytes > open_mark_bytes, (
            f"audio stream stalled during open window "
            f"({open_mark_bytes} -> {post_open_bytes})"
        )
        assert post_closed_bytes > closed_mark_bytes, (
            f"audio stream stalled during closed window "
            f"({closed_mark_bytes} -> {post_closed_bytes})"
        )

        # Read the OPEN-squelch slice and the CLOSED-squelch slice, compare RMS.
        with audio_path.open("rb") as f:
            f.seek(open_mark_bytes)
            open_buf = f.read(post_open_bytes - open_mark_bytes)
            f.seek(closed_mark_bytes)
            closed_buf = f.read(post_closed_bytes - closed_mark_bytes)
        open_samples = np.frombuffer(open_buf, dtype=np.float32)
        closed_samples = np.frombuffer(closed_buf, dtype=np.float32)
        assert open_samples.size > 1000, f"too few open-window samples ({open_samples.size})"
        assert closed_samples.size > 1000, f"too few closed-window samples ({closed_samples.size})"

        open_rms = float(np.sqrt(np.mean(open_samples.astype(np.float64) ** 2)))
        closed_rms = float(np.sqrt(np.mean(closed_samples.astype(np.float64) ** 2)))

        # AGC pushes the tone toward ~AGC reference (0.1 ish). Closed squelch
        # zeros the stream. We require the closed RMS to be at least 100x
        # quieter than the open RMS — a 40 dB drop floor.
        assert open_rms > 1e-3, f"open-squelch audio is too quiet: rms={open_rms}"
        assert closed_rms < open_rms / 100.0, (
            f"closed-squelch audio not silenced: open_rms={open_rms}, "
            f"closed_rms={closed_rms}"
        )

    def test_get_status_shape(self, integration_env):
        cfg, _tb, _server, _audio_path = integration_env
        # Add then ask status.
        add = json.dumps({"v": 1, "id": "ra", "cmd": "add_channel", "args": {
            "id": "ch77", "freq_mhz": 0.2, "mode": "am",
            "squelch_dbfs": -80.0, "gain_db": 3.0, "label": "test",
        }}).encode()
        _udp_roundtrip(cfg.cmd_port, add)

        st = json.dumps({"v": 1, "id": "rs", "cmd": "get_status", "args": {}}).encode()
        resp = json.loads(_udp_roundtrip(cfg.cmd_port, st))
        assert resp["status"] == "ok"
        data = resp["data"]
        assert data["version"] == 1
        assert data["band"] == "airband"
        assert data["max_channels"] == 1
        assert data["pool_free"] == 0
        assert len(data["channels"]) == 1
        ch = data["channels"][0]
        assert ch["id"] == "ch77"
        assert ch["label"] == "test"
        assert ch["freq_mhz"] == 0.2
        assert ch["gain_db"] == 3.0

    def test_remove_then_readd(self, integration_env):
        cfg, _tb, _server, _audio_path = integration_env
        add = json.dumps({"v": 1, "id": "ra", "cmd": "add_channel", "args": {
            "id": "ch01", "freq_mhz": 0.2, "mode": "am", "squelch_dbfs": -80.0,
        }}).encode()
        _udp_roundtrip(cfg.cmd_port, add)
        rm = json.dumps({"v": 1, "id": "rr", "cmd": "remove_channel", "args": {
            "id": "ch01",
        }}).encode()
        resp = json.loads(_udp_roundtrip(cfg.cmd_port, rm))
        assert resp["status"] == "ok"
        # Re-adding the same id must succeed (slot was released).
        resp2 = json.loads(_udp_roundtrip(cfg.cmd_port, add))
        assert resp2["status"] == "ok"

    def test_pool_exhaustion(self, integration_env):
        cfg, _tb, _server, _audio_path = integration_env
        a1 = json.dumps({"v": 1, "id": "ra", "cmd": "add_channel", "args": {
            "id": "ch01", "freq_mhz": 0.2, "mode": "am", "squelch_dbfs": -80.0,
        }}).encode()
        _udp_roundtrip(cfg.cmd_port, a1)
        a2 = json.dumps({"v": 1, "id": "rb", "cmd": "add_channel", "args": {
            "id": "ch02", "freq_mhz": 0.3, "mode": "am", "squelch_dbfs": -80.0,
        }}).encode()
        resp = json.loads(_udp_roundtrip(cfg.cmd_port, a2))
        assert resp["status"] == "rejected"
        assert "pool" in resp["error"].lower()
