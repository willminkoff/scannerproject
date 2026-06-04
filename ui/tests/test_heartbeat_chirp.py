"""Heartbeat tests for Phase 4c chirp awareness.

Flag off: chirp rows are ABSENT from the heartbeat payload (current
heartbeat schema unchanged).

Flag on: three rows appear (chirp-airband, chirp-ground, chirp-airband
icecast / chirp-ground icecast).  Daemon-down rolls the badge to
WEDGED.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _reload_handlers():
    """Reload ui.chirp_client (so it picks up env) and ui.handlers."""
    import ui.chirp_client as cc
    importlib.reload(cc)
    cc.reset_singletons()
    import ui.handlers as h
    importlib.reload(h)
    # Clear the heartbeat memo cache so each test sees a fresh probe.
    with h._HEARTBEAT_LOCK:
        h._HEARTBEAT_CACHE.clear()
    return h, cc


# --- flag-OFF: heartbeat schema unchanged -----------------------------------


def test_heartbeat_no_chirp_rows_when_flag_off(monkeypatch, tmp_path):
    monkeypatch.delenv("SB5_USE_GR_DEMOD", raising=False)
    h, _cc = _reload_handlers()

    # Stub out everything in _compute_heartbeat_payload that would touch
    # the real filesystem so we exercise the chirp-row branch in isolation.
    with patch.object(h, "rtl_airband_sample_flow_state",
                      return_value={"sample_flow_ok": True, "stats_age_sec": 1.0}), \
         patch.object(h, "_unit_active_cached", return_value=True), \
         patch.object(h, "_unit_exists_cached", return_value=True), \
         patch.object(h, "_unit_enabled_state_cached", return_value="enabled"), \
         patch.object(h, "fetch_local_icecast_status", return_value=""), \
         patch.object(h, "mount_publishing", return_value=True), \
         patch.object(h, "_heartbeat_fetch_admin_stats", return_value={}), \
         patch.object(h, "_heartbeat_check_mount_bytes",
                      return_value={"value": "ok", "status": "ok"}), \
         patch.object(h, "_waterfall_dongle_evidence_rows", return_value=[]), \
         patch.object(h, "_vfo_dongle_evidence_row",
                      return_value={"label": "vfo", "value": "ok", "status": "ok"}), \
         patch.object(h, "_broker_aware_dongle_rows", return_value=[]):
        payload = h._compute_heartbeat_payload()

    labels = [r["label"] for r in payload["evidence"]]
    assert not any(l.startswith("chirp-") for l in labels), \
        f"chirp rows must be absent when flag off, got {labels}"


# --- flag-ON: chirp rows present, OK -----------------------------------------


def test_heartbeat_chirp_rows_present_when_flag_on(monkeypatch, tmp_path):
    monkeypatch.setenv("SB5_USE_GR_DEMOD", "true")
    h, cc = _reload_handlers()

    fake_air = MagicMock()
    fake_air.get_status.return_value = {
        "channels": [{"id": "A"}, {"id": "B"}],
        "pool_free": 30,
        "icecast_state": "connected",
        "icecast_bytes_sent": 12345678,
        "icecast_drop_count": 0,
    }
    fake_gnd = MagicMock()
    fake_gnd.get_status.return_value = {
        "channels": [{"id": "G1"}],
        "pool_free": 31,
        "icecast_state": "connected",
        "icecast_bytes_sent": 999,
        "icecast_drop_count": 0,
    }

    with patch.object(h, "_chirp_airband_client", return_value=fake_air), \
         patch.object(h, "_chirp_ground_client", return_value=fake_gnd), \
         patch.object(h, "rtl_airband_sample_flow_state",
                      return_value={"sample_flow_ok": True, "stats_age_sec": 1.0}), \
         patch.object(h, "_unit_active_cached", return_value=True), \
         patch.object(h, "_unit_exists_cached", return_value=True), \
         patch.object(h, "_unit_enabled_state_cached", return_value="enabled"), \
         patch.object(h, "fetch_local_icecast_status", return_value=""), \
         patch.object(h, "mount_publishing", return_value=True), \
         patch.object(h, "_heartbeat_fetch_admin_stats", return_value={}), \
         patch.object(h, "_heartbeat_check_mount_bytes",
                      return_value={"value": "ok", "status": "ok"}), \
         patch.object(h, "_waterfall_dongle_evidence_rows", return_value=[]), \
         patch.object(h, "_vfo_dongle_evidence_row",
                      return_value={"label": "vfo", "value": "ok", "status": "ok"}), \
         patch.object(h, "_broker_aware_dongle_rows", return_value=[]):
        payload = h._compute_heartbeat_payload()

    labels = [r["label"] for r in payload["evidence"]]
    by_label = {r["label"]: r for r in payload["evidence"]}
    assert "chirp-airband" in labels
    assert "chirp-ground" in labels
    assert "chirp-airband icecast" in labels
    assert "chirp-ground icecast" in labels
    assert by_label["chirp-airband"]["status"] == "ok"
    assert "active" in by_label["chirp-airband"]["value"]
    assert "2 chan" in by_label["chirp-airband"]["value"]
    assert by_label["chirp-ground"]["status"] == "ok"
    # Icecast row reflects connected state
    assert "connected" in by_label["chirp-airband icecast"]["value"]


# --- flag-ON: chirp daemon down -> bad row + wedged rollup ------------------


def test_heartbeat_chirp_daemon_down_rolls_to_wedged(monkeypatch, tmp_path):
    monkeypatch.setenv("SB5_USE_GR_DEMOD", "true")
    h, cc = _reload_handlers()

    fake_air = MagicMock()
    fake_air.get_status.side_effect = cc.ChirpDaemonDown("ECONNREFUSED")
    fake_gnd = MagicMock()
    fake_gnd.get_status.return_value = {
        "channels": [], "pool_free": 32,
        "icecast_state": "connected",
        "icecast_bytes_sent": 0, "icecast_drop_count": 0,
    }

    with patch.object(h, "_chirp_airband_client", return_value=fake_air), \
         patch.object(h, "_chirp_ground_client", return_value=fake_gnd), \
         patch.object(h, "rtl_airband_sample_flow_state",
                      return_value={"sample_flow_ok": True, "stats_age_sec": 1.0}), \
         patch.object(h, "_unit_active_cached", return_value=True), \
         patch.object(h, "_unit_exists_cached", return_value=True), \
         patch.object(h, "_unit_enabled_state_cached", return_value="enabled"), \
         patch.object(h, "fetch_local_icecast_status", return_value=""), \
         patch.object(h, "mount_publishing", return_value=True), \
         patch.object(h, "_heartbeat_fetch_admin_stats", return_value={}), \
         patch.object(h, "_heartbeat_check_mount_bytes",
                      return_value={"value": "ok", "status": "ok"}), \
         patch.object(h, "_waterfall_dongle_evidence_rows", return_value=[]), \
         patch.object(h, "_vfo_dongle_evidence_row",
                      return_value={"label": "vfo", "value": "ok", "status": "ok"}), \
         patch.object(h, "_broker_aware_dongle_rows", return_value=[]):
        payload = h._compute_heartbeat_payload()

    by_label = {r["label"]: r for r in payload["evidence"]}
    assert by_label["chirp-airband"]["status"] == "bad"
    assert "daemon down" in by_label["chirp-airband"]["value"]
    assert by_label["chirp-ground"]["status"] == "ok"
    # Down chirp daemon rolls badge to wedged.
    assert payload["state"] == "wedged"


# --- icecast state mapping --------------------------------------------------


@pytest.mark.parametrize("ic_state,expected_status", [
    ("connected", "ok"),
    ("not_configured", "ok"),
    ("disconnected", "warn"),
    ("failed", "bad"),
])
def test_heartbeat_icecast_state_status_mapping(monkeypatch, tmp_path,
                                                 ic_state, expected_status):
    monkeypatch.setenv("SB5_USE_GR_DEMOD", "true")
    h, cc = _reload_handlers()

    fake_air = MagicMock()
    fake_air.get_status.return_value = {
        "channels": [], "pool_free": 32,
        "icecast_state": ic_state,
        "icecast_bytes_sent": 0, "icecast_drop_count": 0,
    }
    fake_gnd = MagicMock()
    fake_gnd.get_status.return_value = {
        "channels": [], "pool_free": 32,
        "icecast_state": "connected",
        "icecast_bytes_sent": 0, "icecast_drop_count": 0,
    }

    with patch.object(h, "_chirp_airband_client", return_value=fake_air), \
         patch.object(h, "_chirp_ground_client", return_value=fake_gnd), \
         patch.object(h, "rtl_airband_sample_flow_state",
                      return_value={"sample_flow_ok": True, "stats_age_sec": 1.0}), \
         patch.object(h, "_unit_active_cached", return_value=True), \
         patch.object(h, "_unit_exists_cached", return_value=True), \
         patch.object(h, "_unit_enabled_state_cached", return_value="enabled"), \
         patch.object(h, "fetch_local_icecast_status", return_value=""), \
         patch.object(h, "mount_publishing", return_value=True), \
         patch.object(h, "_heartbeat_fetch_admin_stats", return_value={}), \
         patch.object(h, "_heartbeat_check_mount_bytes",
                      return_value={"value": "ok", "status": "ok"}), \
         patch.object(h, "_waterfall_dongle_evidence_rows", return_value=[]), \
         patch.object(h, "_vfo_dongle_evidence_row",
                      return_value={"label": "vfo", "value": "ok", "status": "ok"}), \
         patch.object(h, "_broker_aware_dongle_rows", return_value=[]):
        payload = h._compute_heartbeat_payload()

    by_label = {r["label"]: r for r in payload["evidence"]}
    assert by_label["chirp-airband icecast"]["status"] == expected_status
