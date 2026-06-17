"""SB6 2026-06-17 — `set_sdr_gain` cmd-bus command (UI airband gain-slider fix).

Root cause: the airband gain slider wrote the rtl-airband controls file and a
managed-controls override, but neither reaches the running chirp daemon — so
the live SDR front-end gain never changed and the slider snapped back to the
daemon's value on the next status read. This command wires the slider all the
way to the live osmosdr source.

These tests pin the contract end to end WITHOUT a real SDR or a running daemon
(every external touchpoint is faked):

  1. Schema   — SetSdrGainArgs accepts the 0-60 UI range, rejects out-of-band.
  2. Wiring   — set_sdr_gain is registered in COMMAND_ARGS, _KNOWN_CMDS, and
                exposed as ChirpClient.set_sdr_gain with the right envelope.
  3. Source   — SdrIQSource.set_gain returns the driver read-back (clamped)
                value and mirrors it into _cfg.gain_db.
  4. Handler  — _cmd_set_sdr_gain rejects non-sdr sources and, for an sdr
                source, applies the gain, returns the read-back actual, updates
                _cfg.sdr_gain_db, and emits the sdr_gain_changed event.

Requires the daemon's Python env (pydantic + gnuradio importable); it does NOT
require an SDR device or a live daemon process.
"""
from __future__ import annotations

import types

import pytest


# ---------------------------------------------------------------------------
# 1. Schema validation — 0-60 UI range
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("db", [0.0, 30.0, 52.0, 60.0])
def test_set_sdr_gain_args_accepts_ui_range(db):
    from chirp.cmd.schema import SetSdrGainArgs
    assert SetSdrGainArgs(db=db).db == db


@pytest.mark.parametrize("bad", [-1.0, -0.01, 60.01, 100.0])
def test_set_sdr_gain_args_rejects_out_of_range(bad):
    from pydantic import ValidationError
    from chirp.cmd.schema import SetSdrGainArgs
    with pytest.raises(ValidationError):
        SetSdrGainArgs(db=bad)


# ---------------------------------------------------------------------------
# 2. Wiring — COMMAND_ARGS / parse_args / _KNOWN_CMDS / ChirpClient
# ---------------------------------------------------------------------------

def test_set_sdr_gain_registered_in_command_args():
    from chirp.cmd.schema import COMMAND_ARGS, SetSdrGainArgs, parse_args
    assert COMMAND_ARGS.get("set_sdr_gain") is SetSdrGainArgs
    parsed = parse_args("set_sdr_gain", {"db": 45.0})
    assert isinstance(parsed, SetSdrGainArgs)
    assert parsed.db == 45.0


def test_set_sdr_gain_in_known_cmds():
    from chirp.daemon import _KNOWN_CMDS
    assert "set_sdr_gain" in _KNOWN_CMDS


def test_chirp_client_set_sdr_gain_envelope():
    from ui.chirp_client import ChirpClient
    sent = {}

    def fake_send(self, cmd, args, **kw):
        sent["cmd"] = cmd
        sent["args"] = args
        return {"db": args["db"]}

    client = object.__new__(ChirpClient)
    client._send = types.MethodType(fake_send, client)
    out = client.set_sdr_gain(45.0)
    assert sent["cmd"] == "set_sdr_gain"
    assert sent["args"] == {"db": 45.0}
    assert out == {"db": 45.0}


# ---------------------------------------------------------------------------
# 3. Source — read-back is the truth (driver clamps the request)
# ---------------------------------------------------------------------------

class _FakeOsmosdr:
    """Minimal stand-in for the gr-osmosdr source. Records set_gain calls and
    reports a CLAMPED gain on read-back, mimicking SoapySDRPlay3 pinning a
    request to the device's real internal range."""

    def __init__(self, clamp_max):
        self._clamp_max = clamp_max
        self._gain = 0.0
        self.set_calls = []

    def set_gain(self, db, chan):
        self.set_calls.append((db, chan))
        self._gain = min(db, self._clamp_max)

    def get_gain(self, chan):
        return self._gain


def _bare_source(fake_src):
    from chirp.dsp.source_sdr import SdrIQSource, SdrSourceConfig
    src = object.__new__(SdrIQSource)  # skip __init__ -> no hardware open
    src._cfg = SdrSourceConfig(
        device_args="driver=sdrplay",
        sample_rate=2_000_000.0,
        center_freq_hz=119_350_000.0,
    )
    src._src = fake_src
    return src


def test_sdr_source_set_gain_returns_readback_and_updates_cfg():
    src = _bare_source(_FakeOsmosdr(clamp_max=49.0))
    # Request 60; driver clamps to 49 -> read-back is the truth.
    actual = src.set_gain(60.0)
    assert actual == 49.0
    assert src._cfg.gain_db == 49.0
    assert src._src.set_calls == [(60.0, 0)]


def test_sdr_source_set_gain_falls_back_when_readback_raises():
    class _NoReadback:
        def set_gain(self, db, chan):
            pass

        def get_gain(self, chan):
            raise RuntimeError("driver has no get_gain")

    src = _bare_source(_NoReadback())
    actual = src.set_gain(33.0)
    assert actual == 33.0          # falls back to the requested value
    assert src._cfg.gain_db == 33.0


# ---------------------------------------------------------------------------
# 4. Handler — reject non-sdr; apply + read-back + cfg update + event for sdr
# ---------------------------------------------------------------------------

class _FakeLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeSource:
    def __init__(self, applied):
        self.applied = applied
        self.calls = []

    def set_gain(self, db):
        self.calls.append(db)
        return self.applied


class _FakeCfg:
    def __init__(self, source_kind):
        self.source_kind = source_kind
        self.sdr_gain_db = 30.0


class _FakeServer:
    def __init__(self):
        self.events = []

    def emit_event(self, name, **kw):
        self.events.append((name, kw))


def _fake_daemon(source_kind, applied=None):
    from chirp.daemon import ChirpFlowgraph
    d = object.__new__(ChirpFlowgraph)  # skip __init__ -> no flowgraph
    d._lock = _FakeLock()
    d._cfg = _FakeCfg(source_kind)
    d.source = _FakeSource(applied)
    d._server = _FakeServer()
    return d


def test_cmd_set_sdr_gain_rejects_non_sdr_source():
    from chirp.daemon import ChirpFlowgraph
    from chirp.cmd.schema import parse_args
    env = types.SimpleNamespace(id="t1")
    args = parse_args("set_sdr_gain", {"db": 45.0})
    d = _fake_daemon("file")
    resp = ChirpFlowgraph._cmd_set_sdr_gain(d, env, args)
    assert resp.status == "rejected"
    assert "sdr" in (resp.error or "")
    assert d.source.calls == []        # source never touched
    assert d._cfg.sdr_gain_db == 30.0  # cfg unchanged


def test_cmd_set_sdr_gain_applies_and_returns_readback():
    from chirp.daemon import ChirpFlowgraph
    from chirp.cmd.schema import parse_args
    env = types.SimpleNamespace(id="t2")
    args = parse_args("set_sdr_gain", {"db": 60.0})
    d = _fake_daemon("sdr", applied=49.0)  # driver clamps 60 -> 49
    resp = ChirpFlowgraph._cmd_set_sdr_gain(d, env, args)
    assert resp.status == "ok"
    assert resp.data == {"db": 49.0}
    assert d._cfg.sdr_gain_db == 49.0
    assert d.source.calls == [60.0]
    assert ("sdr_gain_changed", {"db": 49.0}) in d._server.events
