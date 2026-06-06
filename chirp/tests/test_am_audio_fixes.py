"""AM audio-quality fixes (2026-06-06): AGC ceiling config + voice band-pass.

- AGC max_gain / attack plumb through config (airband caps the old 96 dB / fast
  AGC that amplified noise on weak signals); defaults apply when unset.
- AM channels get a voice BAND-PASS (300 Hz HPF strips envelope-detector DC +
  rumble); NFM channels keep a plain low-pass.
"""
import json

import pytest

from chirp.daemon import load_config
from chirp.dsp.channel import Channel


def _cfg(tmp_path, body):
    p = tmp_path / "band.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


# --- AGC config plumb-through (lightweight: no GNU Radio) -------------------

def test_agc_config_plumbs_through(tmp_path, monkeypatch):
    monkeypatch.delenv("CHIRP_AGC_MAX_GAIN", raising=False)
    monkeypatch.delenv("CHIRP_AGC_ATTACK", raising=False)
    p = _cfg(tmp_path, {"band": "airband", "agc_max_gain": 1000, "agc_attack": 0.1})
    cfg = load_config(p)
    assert cfg.agc_max_gain == 1000.0
    assert cfg.agc_attack == 0.1


def test_agc_defaults_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("CHIRP_AGC_MAX_GAIN", raising=False)
    monkeypatch.delenv("CHIRP_AGC_ATTACK", raising=False)
    p = _cfg(tmp_path, {"band": "airband"})
    cfg = load_config(p)
    assert cfg.agc_max_gain == 1000.0
    assert cfg.agc_attack == 0.1


def test_agc_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIRP_AGC_MAX_GAIN", "500")
    p = _cfg(tmp_path, {"band": "airband", "agc_max_gain": 1000})
    cfg = load_config(p)
    assert cfg.agc_max_gain == 500.0


# --- voice band-pass: AM only (needs GNU Radio -> runs on host) -------------

def test_am_channel_has_voice_bandpass():
    ch = Channel(samp_rate=1e6, mode="am", agc_max_gain=1000.0, agc_attack=0.1)
    assert ch.audio_bpf is not None, "AM must use a band-pass"
    assert ch.audio_lpf is None, "AM must NOT use the plain low-pass"
    # AGC ceiling honored.
    assert int(ch._agc_max_gain) == 1000


def test_nfm_channel_keeps_lowpass():
    ch = Channel(samp_rate=1e6, mode="nfm")
    assert ch.audio_lpf is not None, "NFM must use a low-pass"
    assert ch.audio_bpf is None, "NFM must NOT get the AM voice band-pass"
    assert ch.agc is None, "NFM has no AGC"
