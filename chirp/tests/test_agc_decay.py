"""AGC decay config plumb-through (2026-06-06, audio tuning step 1).

Slowing agc_decay approximates an AGC hang/hold: lower decay = slower gain
ramp-up when the signal drops, keeping inter-syllable gaps quiet. airband
drops to 1e-5 (10x slower than the 1e-4 default); other bands keep 1e-4.
"""
import json

import pytest

from chirp.daemon import load_config


def _cfg(tmp_path, body):
    p = tmp_path / "band.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


def test_agc_decay_read_from_config(tmp_path, monkeypatch):
    monkeypatch.delenv("CHIRP_AGC_DECAY", raising=False)
    cfg = load_config(_cfg(tmp_path, {"band": "airband", "agc_decay": 1e-5}))
    assert cfg.agc_decay == 1e-5


def test_agc_decay_defaults_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("CHIRP_AGC_DECAY", raising=False)
    cfg = load_config(_cfg(tmp_path, {"band": "ground"}))
    assert cfg.agc_decay == 1e-4


def test_agc_decay_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIRP_AGC_DECAY", "1e-6")
    cfg = load_config(_cfg(tmp_path, {"band": "airband", "agc_decay": 1e-5}))
    assert cfg.agc_decay == 1e-6
