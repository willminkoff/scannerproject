"""RNNoise (ffmpeg arnndn) encoder option — config plumb + encoder branch.

Option A (2026-06-06): when denoise is on, IcecastSink encodes via ffmpeg
arnndn instead of lame. airband enables it; ground stays on lame. Same
stdin/stdout contract, so libshout/flowgraph are untouched.
"""
import json

import pytest

from chirp.daemon import load_config
from chirp.dsp.icecast_sink import IcecastSink, IcecastSinkConfig


def _cfg_file(tmp_path, body):
    p = tmp_path / "band.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


# --- config plumb-through ---------------------------------------------------

def test_denoise_config_plumbs(tmp_path, monkeypatch):
    monkeypatch.delenv("CHIRP_DENOISE", raising=False)
    monkeypatch.delenv("CHIRP_DENOISE_MODEL", raising=False)
    cfg = load_config(_cfg_file(tmp_path, {
        "band": "airband", "denoise": True,
        "denoise_model": "chirp/models/sh.rnnn",
    }))
    assert cfg.denoise is True
    assert cfg.denoise_model == "chirp/models/sh.rnnn"


def test_denoise_defaults_off(tmp_path, monkeypatch):
    monkeypatch.delenv("CHIRP_DENOISE", raising=False)
    cfg = load_config(_cfg_file(tmp_path, {"band": "ground"}))
    assert cfg.denoise is False
    assert cfg.denoise_model == ""


# --- encoder selection branch ----------------------------------------------

class _Stub:
    stdin = None
    stdout = None


def _sink(denoise, model=""):
    cfg = IcecastSinkConfig(host="h", port=8000, mount="/m.mp3", password="p",
                            denoise=denoise, denoise_model=model)
    # encoder + publisher injected, autostart off -> __init__ does NOT spawn or
    # start threads; we drive _spawn_encoder() manually to observe the branch.
    return IcecastSink(cfg, encoder=_Stub(), publisher=_Stub(),
                       autostart_publisher=False)


def test_encoder_branch_lame_when_denoise_off(monkeypatch):
    s = _sink(denoise=False)
    called = []
    monkeypatch.setattr(s, "_spawn_lame", lambda: called.append("lame"))
    monkeypatch.setattr(s, "_spawn_ffmpeg_arnndn", lambda: called.append("ffmpeg"))
    s._spawn_encoder()
    assert called == ["lame"]


def test_encoder_branch_ffmpeg_when_denoise_on(monkeypatch):
    s = _sink(denoise=True, model="chirp/models/sh.rnnn")
    called = []
    monkeypatch.setattr(s, "_spawn_lame", lambda: called.append("lame"))
    monkeypatch.setattr(s, "_spawn_ffmpeg_arnndn", lambda: called.append("ffmpeg"))
    s._spawn_encoder()
    assert called == ["ffmpeg"]


def test_ffmpeg_cmd_shape():
    cmd = IcecastSink._ffmpeg_arnndn_cmd("/x/sh.rnnn", 16000, 32)
    s = " ".join(cmd)
    assert "arnndn=m=/x/sh.rnnn,aresample=16000" in s
    assert "-ar 16000" in s and "-b:a 32k" in s and "libmp3lame" in s
