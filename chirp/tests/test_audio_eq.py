"""Presence-boost EQ encoder path (2026-06-06).

audio_eq routes the encoder through ffmpeg+libmp3lame (lame can't EQ). Encoder
selection covers all 4 combos of {denoise on/off} x {audio_eq set/empty}:
  denoise on            -> arnndn (+volume +eq)
  denoise off, eq set   -> ffmpeg eq-only
  denoise off, eq empty -> plain lame (default; ground)
"""
import json

import pytest

from chirp.daemon import load_config
from chirp.dsp.icecast_sink import IcecastSink, IcecastSinkConfig

EQ = "equalizer=f=2000:t=q:w=2:g=3"


def _cfg_file(tmp_path, body):
    p = tmp_path / "band.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


# --- config plumb-through ---------------------------------------------------

def test_audio_eq_config_plumbs(tmp_path, monkeypatch):
    monkeypatch.delenv("CHIRP_AUDIO_EQ", raising=False)
    cfg = load_config(_cfg_file(tmp_path, {"band": "airband", "audio_eq": EQ}))
    assert cfg.audio_eq == EQ
    cfg2 = load_config(_cfg_file(tmp_path, {"band": "ground"}))
    assert cfg2.audio_eq == ""


# --- ffmpeg command shapes --------------------------------------------------

def test_eq_only_cmd_shape():
    cmd = IcecastSink._ffmpeg_eq_cmd(16000, 32, EQ)
    s = " ".join(cmd)
    assert f"{EQ},aresample=16000" in s
    assert "arnndn" not in s
    assert "libmp3lame" in s and "-b:a 32k" in s


def test_arnndn_cmd_includes_eq_when_set():
    cmd = IcecastSink._ffmpeg_arnndn_cmd("/x/sh.rnnn", 16000, 32, gain_db=25.0, audio_eq=EQ)
    s = " ".join(cmd)
    assert f"arnndn=m=/x/sh.rnnn,volume=25.0dB,{EQ},aresample=16000" in s


# --- encoder selection branch (all 4 combos) --------------------------------

class _Stub:
    stdin = None
    stdout = None


def _sink(denoise=False, model="", eq=""):
    cfg = IcecastSinkConfig(host="h", port=8000, mount="/m.mp3", password="p",
                            denoise=denoise, denoise_model=model, audio_eq=eq)
    return IcecastSink(cfg, encoder=_Stub(), publisher=_Stub(), autostart_publisher=False)


def _which(s, monkeypatch):
    called = []
    monkeypatch.setattr(s, "_spawn_lame", lambda: called.append("lame"))
    monkeypatch.setattr(s, "_spawn_ffmpeg_eq", lambda: called.append("eq"))
    monkeypatch.setattr(s, "_spawn_ffmpeg_arnndn", lambda: called.append("arnndn"))
    s._spawn_encoder()
    return called


def test_branch_lame_when_off_and_no_eq(monkeypatch):
    assert _which(_sink(), monkeypatch) == ["lame"]


def test_branch_eq_when_off_and_eq_set(monkeypatch):
    assert _which(_sink(eq=EQ), monkeypatch) == ["eq"]


def test_branch_arnndn_when_denoise_on(monkeypatch):
    assert _which(_sink(denoise=True, model="chirp/models/sh.rnnn"), monkeypatch) == ["arnndn"]


def test_branch_arnndn_takes_precedence_over_eq(monkeypatch):
    assert _which(_sink(denoise=True, model="chirp/models/sh.rnnn", eq=EQ), monkeypatch) == ["arnndn"]
