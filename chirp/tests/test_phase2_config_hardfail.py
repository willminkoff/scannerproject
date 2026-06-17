"""SB6 Phase 2 — JSON-load hard-fail + dataclass/load_config default alignment.

Regression coverage for the 2026-06-14 "voice-as-noise" root cause: a broken
``airband.json`` used to warn-and-continue, falling through to defaults whose
``am_agc_enabled`` literal (``True`` in load_config) disagreed with the dataclass
(``False``). The silent fast-AGC normalized AM voice into noise for a full day.

These tests pin two invariants permanently:
  1. A config file that EXISTS but is broken aborts load_config (no silent
     fallback) with a clear error naming the path.
  2. With no JSON keys and no env overrides, every simple scalar knob resolves
     to its DATACLASS default — a single source of truth. This kills the whole
     "duplicated literal drifts out of sync" bug class, not just am_agc_enabled.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from chirp.daemon import DaemonConfig, load_config

# Simple scalar knobs whose default must come solely from the dataclass.
# Derived fields (band, cmd_port, pool_mode, source/audio/icecast/sdr, event_sink,
# hit_log_path, config_source_path) are intentionally excluded — they compute
# their value rather than defaulting to a literal.
_SIMPLE_DEFAULT_FIELDS = [
    "source_samp_rate", "audio_rate", "channel_bw_hz", "agc_max_gain",
    "agc_attack", "agc_decay", "am_agc_enabled", "am_fixed_gain", "vad_enabled",
    "audio_bandpass_low_hz", "audio_bandpass_high_hz", "denoise", "denoise_model",
    "denoise_gain_db", "audio_eq", "max_channels", "log_level", "state_path",
    "icecast_bitrate_kbps", "icecast_fallback_file", "lo_dwell_sec",
    "lo_max_clusters", "scan_hold_enabled", "scan_hold_hang_sec",
    "scan_hold_max_sec", "priority_gate_enabled", "audio_trace_enabled",
]


@pytest.fixture(autouse=True)
def _clear_chirp_env(monkeypatch):
    """Strip every CHIRP_* override so JSON+default resolution is what's tested."""
    import os
    for k in list(os.environ):
        if k.startswith("CHIRP_"):
            monkeypatch.delenv(k, raising=False)


def _write(tmp_path, body: str) -> Path:
    p = tmp_path / "band.json"
    p.write_text(body, encoding="utf-8")
    return p


# --- 1. hard-fail on a broken config (the actual fix) ---------------------

def test_trailing_comma_hard_fails(tmp_path):
    p = _write(tmp_path, '{"band": "airband", "am_agc_enabled": false,}')
    with pytest.raises(ValueError) as ei:
        load_config(p)
    assert str(p) in str(ei.value)
    assert "JSON" in str(ei.value)


def test_garbage_hard_fails(tmp_path):
    p = _write(tmp_path, "this is not json at all")
    with pytest.raises(ValueError) as ei:
        load_config(p)
    assert str(p) in str(ei.value)


def test_non_object_toplevel_hard_fails(tmp_path):
    p = _write(tmp_path, "[1, 2, 3]")
    with pytest.raises(ValueError) as ei:
        load_config(p)
    assert "JSON object" in str(ei.value)


def test_unreadable_file_hard_fails(tmp_path):
    import os
    if os.geteuid() == 0:
        pytest.skip("root bypasses file permissions")
    p = _write(tmp_path, '{"band": "airband"}')
    p.chmod(0o000)
    try:
        with pytest.raises(ValueError) as ei:
            load_config(p)
        assert str(p) in str(ei.value)
    finally:
        p.chmod(0o644)  # let tmp cleanup remove it


# --- 2. dataclass is the single source of truth for simple defaults -------

def test_empty_config_uses_dataclass_defaults(tmp_path):
    """The keystone: empty JSON + no env == DaemonConfig() for every simple
    field. Any future field whose load_config literal drifts from its dataclass
    default fails HERE, before it can ship a silent-fallback bug."""
    cfg = load_config(_write(tmp_path, "{}"))
    dc = DaemonConfig()
    drift = {f: (getattr(cfg, f), getattr(dc, f))
             for f in _SIMPLE_DEFAULT_FIELDS
             if getattr(cfg, f) != getattr(dc, f)}
    assert not drift, f"load_config defaults drifted from dataclass: {drift}"


def test_am_agc_default_is_false(tmp_path):
    """The specific 2026-06-14 regression: missing am_agc_enabled must resolve
    to False (AGC OFF), never the old stray True that erased AM voice."""
    cfg = load_config(_write(tmp_path, "{}"))
    assert cfg.am_agc_enabled is False


def test_explicit_null_field_falls_back_to_default(tmp_path):
    cfg = load_config(_write(tmp_path, '{"am_agc_enabled": null}'))
    assert cfg.am_agc_enabled is False


# --- 3. env > json > default precedence preserved -------------------------

def test_env_overrides_json(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIRP_AM_AGC_ENABLED", "1")
    cfg = load_config(_write(tmp_path, '{"am_agc_enabled": false}'))
    assert cfg.am_agc_enabled is True


def test_json_overrides_default(tmp_path):
    cfg = load_config(_write(tmp_path, '{"channel_bw_hz": 6000}'))
    assert cfg.channel_bw_hz == 6000.0


# --- 4. missing file: defaults by default, hard-fail when required ---------

def test_missing_file_uses_defaults(tmp_path):
    cfg = load_config(tmp_path / "does-not-exist.json")
    assert cfg.am_agc_enabled is False
    assert cfg.config_source_path is None


def test_missing_file_hard_fails_when_required(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIRP_CONFIG_REQUIRED", "1")
    with pytest.raises(ValueError) as ei:
        load_config(tmp_path / "does-not-exist.json")
    assert "required" in str(ei.value).lower()


# --- 5. config_source_path surfaced for the metric ------------------------

def test_config_source_path_set_on_load(tmp_path):
    p = _write(tmp_path, "{}")
    cfg = load_config(p)
    assert cfg.config_source_path == str(p)


# --- 6. the real production config still loads with the locked-in AM knobs -

def test_production_airband_config_loads_clean():
    cfg_path = Path(__file__).resolve().parents[1] / "config" / "airband.json"
    if not cfg_path.is_file():
        pytest.skip("airband.json not present in this checkout")
    cfg = load_config(cfg_path)
    assert cfg.am_agc_enabled is False, "airband must run with AM AGC OFF"
    assert cfg.vad_enabled is False, "airband must run with VAD OFF"
    assert cfg.channel_bw_hz == 6000.0
    assert cfg.audio_bandpass_high_hz == 3500.0
