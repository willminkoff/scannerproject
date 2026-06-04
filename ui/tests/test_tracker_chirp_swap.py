"""Tracker-swap tests: with SB5_USE_GR_DEMOD=on, the squelch tracker
reads noise floor from the chirp client and applies via set_squelch,
NOT via rtl_airband.conf writes / unit restart.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _reload_tracker():
    import ui.chirp_client as cc
    importlib.reload(cc)
    cc.reset_singletons()
    import ui.squelch_tracker as st
    importlib.reload(st)
    return st, cc


# --- flag-off (legacy) path stays untouched ---------------------------------


def test_flag_off_uses_rtl_airband_path(monkeypatch, tmp_path):
    """With the flag OFF the tracker must touch the rtl-airband path."""
    monkeypatch.delenv("SB5_USE_GR_DEMOD", raising=False)
    monkeypatch.setenv("SQUELCH_TRACKER_AUDIT_LOG_PATH", str(tmp_path / "t.jsonl"))
    st, _cc = _reload_tracker()
    assert st._chirp_flag() is False

    # Patch the rtl-airband-specific reads/writes so we can assert they
    # were called (and the chirp path was NOT).
    with patch.object(st, "read_noise_floor_for_target", return_value={121.5: -70.0}), \
         patch.object(st, "read_profile_freqs", return_value=[121.5]), \
         patch.object(st, "read_current_thresholds", return_value=[]), \
         patch.object(st, "compute_threshold_list",
                      return_value=([-64], [-70.0])), \
         patch.object(st, "write_per_channel_squelch_list", return_value=True) as wpcsl, \
         patch.object(st, "_restart_band_unit", return_value=(True, "")) as restart, \
         patch.object(st, "resolve_controls_path", return_value="/tmp/x.conf"), \
         patch.object(st, "recommended_managed_controls", return_value={"squelch_preset": "balanced"}), \
         patch.object(st, "persist_managed_controls_override"), \
         patch.object(st, "record_tracker_apply"), \
         patch.object(st, "parse_controls", return_value=(32.8, 10.0, -60.0, "dbfs")):
        result = st._run_cycle_for_band("airband", force=True)

    assert result["applied"] is True
    assert "via" not in result  # rtl-airband path does not stamp via
    wpcsl.assert_called_once()
    restart.assert_called_once()


# --- flag-on (chirp) path swaps source + sink -------------------------------


def _mock_chirp_status(channels):
    return {"channels": channels, "band": "airband", "pool_free": 32 - len(channels)}


def test_flag_on_swaps_to_chirp_source_and_sink(monkeypatch, tmp_path):
    """With SB5_USE_GR_DEMOD=true the tracker reads from chirp.get_status
    and writes via chirp.set_squelch.  The rtl-airband config-write and
    unit-restart helpers MUST NOT be called."""
    monkeypatch.setenv("SB5_USE_GR_DEMOD", "true")
    monkeypatch.setenv("SQUELCH_TRACKER_AUDIT_LOG_PATH", str(tmp_path / "t.jsonl"))
    st, cc = _reload_tracker()
    assert st._chirp_flag() is True

    fake_client = MagicMock()
    fake_client.get_status.return_value = _mock_chirp_status([
        {"id": "T1", "freq_mhz": 121.5, "signal_level_dbfs": -70.0,
         "squelch_dbfs": -50, "mode": "am", "label": "Tower"},
        {"id": "T2", "freq_mhz": 122.0, "signal_level_dbfs": -68.0,
         "squelch_dbfs": -50, "mode": "am", "label": "App"},
    ])

    with patch.object(st, "_chirp_client_for", return_value=fake_client), \
         patch.object(st, "resolve_controls_path", return_value="/tmp/x.conf"), \
         patch.object(st, "recommended_managed_controls",
                      return_value={"squelch_preset": "balanced"}), \
         patch.object(st, "persist_managed_controls_override"), \
         patch.object(st, "record_tracker_apply"), \
         patch.object(st, "parse_controls",
                      return_value=(32.8, 10.0, -60.0, "dbfs")), \
         patch.object(st, "write_per_channel_squelch_list") as wpcsl, \
         patch.object(st, "_restart_band_unit") as restart:
        result = st._run_cycle_for_band("airband", force=True)

    # 1) Chirp source: get_status was called
    fake_client.get_status.assert_called_once()
    # 2) Chirp sink: set_squelch was called for each channel
    assert fake_client.set_squelch.call_count == 2
    cids = sorted(call.args[0] for call in fake_client.set_squelch.call_args_list)
    assert cids == ["T1", "T2"]
    # Thresholds = noise + margin (balanced=6 dB)
    dbfs_vals = sorted(call.args[1] for call in fake_client.set_squelch.call_args_list)
    assert dbfs_vals == [-64.0, -62.0]
    # 3) Legacy path NOT used
    wpcsl.assert_not_called()
    restart.assert_not_called()

    assert result["applied"] is True
    assert result["via"] == "chirp"
    assert result["applied_count"] == 2
    assert result["touched_count"] == 2


def test_flag_on_chirp_down_returns_skip(monkeypatch, tmp_path):
    monkeypatch.setenv("SB5_USE_GR_DEMOD", "true")
    monkeypatch.setenv("SQUELCH_TRACKER_AUDIT_LOG_PATH", str(tmp_path / "t.jsonl"))
    st, _cc = _reload_tracker()
    fake_client = MagicMock()
    fake_client.get_status.side_effect = RuntimeError("daemon down")

    with patch.object(st, "_chirp_client_for", return_value=fake_client), \
         patch.object(st, "resolve_controls_path", return_value="/tmp/x.conf"), \
         patch.object(st, "recommended_managed_controls",
                      return_value={"squelch_preset": "balanced"}):
        result = st._run_cycle_for_band("airband", force=True)

    assert result["skipped"] == "chirp_down"
    assert "daemon down" in result["error"]
    assert result["via"] == "chirp"


def test_flag_on_poison_noise_floor_rejected(monkeypatch, tmp_path):
    """Same poison-noise gate applies on the chirp path."""
    monkeypatch.setenv("SB5_USE_GR_DEMOD", "true")
    monkeypatch.setenv("SQUELCH_TRACKER_AUDIT_LOG_PATH", str(tmp_path / "t.jsonl"))
    # AM ceiling = -55 dBFS by default; -10 dBFS is well above (poison).
    st, _cc = _reload_tracker()
    fake_client = MagicMock()
    fake_client.get_status.return_value = _mock_chirp_status([
        {"id": "T1", "freq_mhz": 121.5, "signal_level_dbfs": -10.0,
         "squelch_dbfs": -50, "mode": "am"},
    ])

    with patch.object(st, "_chirp_client_for", return_value=fake_client), \
         patch.object(st, "resolve_controls_path", return_value="/tmp/x.conf"), \
         patch.object(st, "recommended_managed_controls",
                      return_value={"squelch_preset": "balanced"}):
        result = st._run_cycle_for_band("airband", force=True)

    assert result["skipped"] == "poison_noise_floor"
    fake_client.set_squelch.assert_not_called()


def test_flag_on_per_channel_poison_falls_back(monkeypatch, tmp_path):
    """Single-channel poison: that channel keeps its prior threshold."""
    monkeypatch.setenv("SB5_USE_GR_DEMOD", "true")
    monkeypatch.setenv("SQUELCH_TRACKER_AUDIT_LOG_PATH", str(tmp_path / "t.jsonl"))
    st, _cc = _reload_tracker()
    fake_client = MagicMock()
    fake_client.get_status.return_value = _mock_chirp_status([
        {"id": "T1", "freq_mhz": 121.5, "signal_level_dbfs": -70.0,
         "squelch_dbfs": -50, "mode": "am"},   # healthy
        {"id": "T2", "freq_mhz": 122.0, "signal_level_dbfs": -10.0,
         "squelch_dbfs": -50, "mode": "am"},   # poison
        {"id": "T3", "freq_mhz": 123.0, "signal_level_dbfs": -65.0,
         "squelch_dbfs": -50, "mode": "am"},   # healthy
    ])

    with patch.object(st, "_chirp_client_for", return_value=fake_client), \
         patch.object(st, "resolve_controls_path", return_value="/tmp/x.conf"), \
         patch.object(st, "recommended_managed_controls",
                      return_value={"squelch_preset": "balanced"}), \
         patch.object(st, "persist_managed_controls_override"), \
         patch.object(st, "record_tracker_apply"), \
         patch.object(st, "parse_controls",
                      return_value=(32.8, 10.0, -60.0, "dbfs")):
        result = st._run_cycle_for_band("airband", force=True)

    # Both healthy channels get noise+6; T2 keeps prior (-50).
    by_id = {call.args[0]: call.args[1]
             for call in fake_client.set_squelch.call_args_list}
    assert by_id["T1"] == -64.0
    assert by_id["T2"] == -50.0  # fallback to prior threshold
    assert by_id["T3"] == -59.0
    assert result["sanitized_count"] == 1
    assert result["sanitized_channels"][0]["id"] == "T2"


def test_flag_on_hysteresis_skips_below_threshold(monkeypatch, tmp_path):
    """If current==new (within 5 dB hysteresis) and not force, skip."""
    monkeypatch.setenv("SB5_USE_GR_DEMOD", "true")
    monkeypatch.setenv("SQUELCH_TRACKER_HYSTERESIS_DB", "5.0")
    monkeypatch.setenv("SQUELCH_TRACKER_AUDIT_LOG_PATH", str(tmp_path / "t.jsonl"))
    st, _cc = _reload_tracker()
    fake_client = MagicMock()
    # noise -70 -> threshold -64; cur -65 -> delta 1 -> below hysteresis
    fake_client.get_status.return_value = _mock_chirp_status([
        {"id": "T1", "freq_mhz": 121.5, "signal_level_dbfs": -70.0,
         "squelch_dbfs": -65, "mode": "am"},
    ])

    with patch.object(st, "_chirp_client_for", return_value=fake_client), \
         patch.object(st, "resolve_controls_path", return_value="/tmp/x.conf"), \
         patch.object(st, "recommended_managed_controls",
                      return_value={"squelch_preset": "balanced"}):
        # Note: NOT forcing here.
        result = st._run_cycle_for_band("airband", force=False)

    assert result["skipped"] == "below_hysteresis"
    fake_client.set_squelch.assert_not_called()


def test_flag_on_no_channels_returns_skip(monkeypatch, tmp_path):
    monkeypatch.setenv("SB5_USE_GR_DEMOD", "true")
    monkeypatch.setenv("SQUELCH_TRACKER_AUDIT_LOG_PATH", str(tmp_path / "t.jsonl"))
    st, _cc = _reload_tracker()
    fake_client = MagicMock()
    fake_client.get_status.return_value = _mock_chirp_status([])

    with patch.object(st, "_chirp_client_for", return_value=fake_client), \
         patch.object(st, "resolve_controls_path", return_value="/tmp/x.conf"), \
         patch.object(st, "recommended_managed_controls",
                      return_value={"squelch_preset": "balanced"}):
        result = st._run_cycle_for_band("airband", force=True)

    assert result["skipped"] == "no_chirp_channels"
    fake_client.set_squelch.assert_not_called()


def test_flag_off_chirp_client_not_imported_at_module_load(monkeypatch):
    """When the flag is off, the tracker module must still load without
    contacting the chirp daemon."""
    monkeypatch.delenv("SB5_USE_GR_DEMOD", raising=False)
    st, _cc = _reload_tracker()
    # _chirp_flag is the only chirp-flavored entry point on the cold path.
    assert st._chirp_flag() is False


def test_flag_probe_resilient_to_chirp_import_failure(monkeypatch):
    """If ui.chirp_client fails to import, _chirp_flag must return False
    rather than crashing the tracker thread."""
    monkeypatch.setenv("SB5_USE_GR_DEMOD", "true")
    st, _cc = _reload_tracker()
    # Sabotage the import path.
    import importlib as _il
    with patch("builtins.__import__", side_effect=ImportError("boom")):
        # _chirp_flag must swallow the ImportError and return False.
        assert st._chirp_flag() is False
