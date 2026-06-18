"""SB6 2026-06-18 global-squelch redesign — daemon propagation tests.

The band runs on ONE squelch threshold (``DaemonConfig.global_squelch_dbfs``)
applied to every channel: at startup, on every channel add, and live via the
``set_global_squelch_dbfs`` cmd. Per-channel squelch is no longer
independently configurable — an incoming per-channel value on add_channel is
IGNORED and the channel inherits the global.

These tests need GNU Radio (they build a real ChirpFlowgraph), so they run on
the Micro alongside the other daemon tests. The gnuradio-free schema + state
coverage lives in test_global_squelch_schema_state.py.

Mirrors the Phase-2 regression style: set the global → assert every channel
reports the SAME value (the property the per-channel model couldn't give).
"""

from __future__ import annotations

import os

import numpy as np
import pytest

from chirp.cmd.schema import (
    AddChannelArgs,
    ChannelArgs,
    Envelope,
    GetStatusArgs,
    SetGlobalSquelchArgs,
)
from chirp.cmd.server import CommandServer, ServerConfig
from chirp.daemon import ChirpFlowgraph, DaemonConfig, load_config
from chirp.state import ChirpState, StateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_quiet_iq(path, samp_rate=1e6, duration_s=1.0, noise_sigma=0.001):
    n = int(samp_rate * duration_s)
    rng = np.random.default_rng(13)
    iq = (rng.normal(0, noise_sigma, n) + 1j * rng.normal(0, noise_sigma, n)).astype(np.complex64)
    iq.tofile(path)


def _build_daemon(tmp_path, *, global_squelch_dbfs=-56.0, n_slots=8):
    samp_rate = 1e6
    iq_path = tmp_path / "q.iq"
    _write_quiet_iq(iq_path, samp_rate, 1.0)
    cfg = DaemonConfig(
        band="airband",
        cmd_port=21400 + (os.getpid() % 400),
        source_kind="file",
        source_path=str(iq_path),
        source_samp_rate=samp_rate,
        audio_out_kind="file",
        audio_out_path=str(tmp_path / "a.f32"),
        audio_rate=16000.0,
        max_channels=n_slots,
        state_path=str(tmp_path / "s.json"),
        hit_log_path=str(tmp_path / "h.jsonl"),
        global_squelch_dbfs=global_squelch_dbfs,
    )
    server = CommandServer(
        ServerConfig(host=cfg.cmd_host, port=cfg.cmd_port),
        dispatch=lambda env, args: tb.dispatch(env, args),
    )
    tb = ChirpFlowgraph(cfg, server, state_store=StateStore(cfg.state_path))
    return cfg, tb


def _add(tb, *channels):
    """channels: (id, freq_mhz) tuples. Each is given a deliberately WRONG
    per-channel squelch (-12) to prove the daemon ignores it and substitutes
    the global."""
    chans = [
        ChannelArgs(id=cid, freq_mhz=freq, mode="am", squelch_dbfs=-12.0, gain_db=0.0)
        for cid, freq in channels
    ]
    env = Envelope(v=1, id="t", cmd="add_channel", args={})
    return tb.dispatch(env, AddChannelArgs(channels=chans))


def _status(tb):
    env = Envelope(v=1, id="t", cmd="get_status", args={})
    return tb.dispatch(env, GetStatusArgs()).data


def _set_global(tb, dbfs):
    env = Envelope(v=1, id="t", cmd="set_global_squelch_dbfs", args={})
    return tb.dispatch(env, SetGlobalSquelchArgs(dbfs=dbfs))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGlobalSquelchPropagation:
    def test_added_channels_inherit_global_not_per_channel(self, tmp_path):
        """add_channel carries squelch_dbfs=-12 per channel, but every channel
        must report the daemon's global (-56), proving the per-channel value is
        ignored."""
        cfg, tb = _build_daemon(tmp_path, global_squelch_dbfs=-56.0)
        tb.start(); tb.start_health()
        try:
            _add(tb, ("a", 119.35), ("b", 118.6), ("c", 121.9))
            data = _status(tb)
            assert data["global_squelch_dbfs"] == -56.0
            vals = {c["id"]: c["squelch_dbfs"] for c in data["channels"]}
            assert vals == {"a": -56.0, "b": -56.0, "c": -56.0}, vals
        finally:
            tb.stop_health(); tb.stop(); tb.wait()

    def test_set_global_squelch_applies_to_all_five_channels(self, tmp_path):
        """Phase-2-style regression: set the global once → all 5 channels
        report the same new value via get_status."""
        cfg, tb = _build_daemon(tmp_path, global_squelch_dbfs=-56.0)
        tb.start(); tb.start_health()
        try:
            _add(tb, ("a", 118.4), ("b", 118.6), ("c", 119.35),
                 ("d", 119.45), ("e", 127.175))
            resp = _set_global(tb, -64.0)
            assert resp.status == "ok"
            assert resp.data["dbfs"] == -64.0
            assert resp.data["channels_applied"] == 5
            data = _status(tb)
            assert data["global_squelch_dbfs"] == -64.0
            vals = [c["squelch_dbfs"] for c in data["channels"]]
            assert len(vals) == 5
            assert all(v == -64.0 for v in vals), vals
        finally:
            tb.stop_health(); tb.stop(); tb.wait()

    def test_channel_added_after_set_global_inherits_current_global(self, tmp_path):
        """A channel added AFTER the global was changed inherits the current
        global, not the startup default and not a per-channel value."""
        cfg, tb = _build_daemon(tmp_path, global_squelch_dbfs=-56.0)
        tb.start(); tb.start_health()
        try:
            _add(tb, ("a", 118.4))
            _set_global(tb, -70.0)
            _add(tb, ("b", 119.45))  # added after the change
            vals = {c["id"]: c["squelch_dbfs"] for c in _status(tb)["channels"]}
            assert vals == {"a": -70.0, "b": -70.0}, vals
        finally:
            tb.stop_health(); tb.stop(); tb.wait()

    def test_persisted_state_carries_global_and_restore_adopts_it(self, tmp_path):
        """_persist_state writes the global; a fresh daemon restoring that
        state adopts the persisted global over its config default."""
        cfg, tb = _build_daemon(tmp_path, global_squelch_dbfs=-56.0)
        tb.start(); tb.start_health()
        try:
            _add(tb, ("a", 118.4), ("b", 119.45))
            _set_global(tb, -61.0)
        finally:
            tb.stop_health(); tb.stop(); tb.wait()

        # On-disk state should now record the global + per-channel == global.
        st = StateStore(cfg.state_path).load()
        assert st.global_squelch_dbfs == -61.0
        assert all(c.squelch_dbfs == -61.0 for c in st.channels)

        # A new daemon with a DIFFERENT config default must adopt the
        # persisted -61, not its own -56.
        cfg2, tb2 = _build_daemon(tmp_path, global_squelch_dbfs=-56.0)
        tb2.start(); tb2.start_health()
        try:
            tb2.restore_from_state()
            data = _status(tb2)
            assert data["global_squelch_dbfs"] == -61.0
            assert all(c["squelch_dbfs"] == -61.0 for c in data["channels"])
        finally:
            tb2.stop_health(); tb2.stop(); tb2.wait()


class TestGlobalSquelchConfig:
    def test_env_var_overrides_default(self, tmp_path, monkeypatch):
        """CHIRP_GLOBAL_SQUELCH_DBFS is the rollback knob — env beats json/
        dataclass default."""
        monkeypatch.setenv("CHIRP_GLOBAL_SQUELCH_DBFS", "-48")
        cfg = load_config(tmp_path / "does_not_exist.json")
        assert cfg.global_squelch_dbfs == -48.0

    def test_default_is_minus_56(self):
        assert DaemonConfig().global_squelch_dbfs == -56.0
