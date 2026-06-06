"""AM voice band-pass edge config plumb-through (2026-06-06, tuning step A).

airband tightens the AM voice band-pass to 300-2500 Hz; defaults stay at the
prior hardcoded 300-3500 Hz so other bands are unchanged. Channel-level test
asserts AM uses the configured edges (band-pass present); NFM is unaffected.
"""
import json

import pytest

from chirp.daemon import load_config
from chirp.dsp.channel import Channel


def _cfg(tmp_path, body):
    p = tmp_path / "band.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


def test_bandpass_edges_read_from_config(tmp_path, monkeypatch):
    monkeypatch.delenv("CHIRP_AUDIO_BANDPASS_LOW_HZ", raising=False)
    monkeypatch.delenv("CHIRP_AUDIO_BANDPASS_HIGH_HZ", raising=False)
    cfg = load_config(_cfg(tmp_path, {
        "band": "airband",
        "audio_bandpass_low_hz": 300,
        "audio_bandpass_high_hz": 2500,
    }))
    assert cfg.audio_bandpass_low_hz == 300.0
    assert cfg.audio_bandpass_high_hz == 2500.0


def test_bandpass_defaults_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("CHIRP_AUDIO_BANDPASS_LOW_HZ", raising=False)
    monkeypatch.delenv("CHIRP_AUDIO_BANDPASS_HIGH_HZ", raising=False)
    cfg = load_config(_cfg(tmp_path, {"band": "ground"}))
    assert cfg.audio_bandpass_low_hz == 300.0
    assert cfg.audio_bandpass_high_hz == 3500.0


def test_am_channel_accepts_bandpass_edges():
    ch = Channel(samp_rate=1e6, mode="am",
                 audio_bandpass_low_hz=300.0, audio_bandpass_high_hz=2500.0)
    assert ch.audio_bpf is not None and ch.audio_lpf is None
    assert ch._audio_bandpass_low_hz == 300.0
    assert ch._audio_bandpass_high_hz == 2500.0


def test_nfm_unaffected_by_bandpass_edges():
    ch = Channel(samp_rate=1e6, mode="nfm",
                 audio_bandpass_low_hz=300.0, audio_bandpass_high_hz=2500.0)
    assert ch.audio_lpf is not None and ch.audio_bpf is None
