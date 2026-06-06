"""channel_bw_hz config plumb-through (2026-06-06).

Guards that the per-channel demod bandwidth is read from config (airband
tightened to 8 kHz) and defaults to 12.5 kHz when unset, so other bands /
existing behavior don't regress. The Channel flowgraph itself needs GNU
Radio, so we test the config layer (load_config) which is what feeds the
Channel(...) call site.
"""
import json

import pytest

from chirp.daemon import load_config


def _write_cfg(tmp_path, body):
    p = tmp_path / "band.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


def test_channel_bw_hz_read_from_config(tmp_path, monkeypatch):
    monkeypatch.delenv("CHIRP_CHANNEL_BW_HZ", raising=False)
    p = _write_cfg(tmp_path, {"band": "airband", "channel_bw_hz": 8000})
    cfg = load_config(p)
    assert cfg.channel_bw_hz == 8000.0


def test_channel_bw_hz_defaults_to_12500_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("CHIRP_CHANNEL_BW_HZ", raising=False)
    p = _write_cfg(tmp_path, {"band": "ground"})  # no channel_bw_hz key
    cfg = load_config(p)
    assert cfg.channel_bw_hz == 12500.0


def test_channel_bw_hz_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIRP_CHANNEL_BW_HZ", "6000")
    p = _write_cfg(tmp_path, {"band": "airband", "channel_bw_hz": 8000})
    cfg = load_config(p)
    assert cfg.channel_bw_hz == 6000.0
