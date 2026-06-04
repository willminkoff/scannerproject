"""End-to-end Phase 4c integration tests.

Spins up a real UDP mock daemon (no monkey-patching of the socket)
and exercises the FULL chirp_adapter helpers + heartbeat probe path
against it.  This is the closest we can get to the real production
path without starting actual chirp daemons.
"""
from __future__ import annotations

import importlib
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# --- in-process UDP mock daemon ---------------------------------------------


class StubDaemon:
    """Mock chirp daemon that maintains a per-channel squelch_dbfs map
    and answers add_channel / remove_channel / set_squelch / get_status
    / reset.

    Records every received envelope so tests can assert on the
    interaction sequence.
    """

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.channels: dict[str, dict] = {}
        self.received: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self.signal_level_dbfs = -70.0  # default noise floor
        self.icecast_state = "connected"

    def start(self):
        self._thread.start()
        # Spin until socket is listening (immediate after bind).
        return self

    def stop(self):
        self._stop.set()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(b"\x00", ("127.0.0.1", self.port))
            s.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)
        self.sock.close()

    def _loop(self):
        self.sock.settimeout(0.3)
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if self._stop.is_set():
                return
            if not data or data == b"\x00":
                continue
            try:
                env = json.loads(data.decode("utf-8"))
            except Exception:
                continue
            self.received.append(env)
            reply = self._dispatch(env)
            self.sock.sendto(json.dumps(reply).encode("utf-8"), addr)

    def _dispatch(self, env: dict) -> dict:
        req_id = env.get("id")
        cmd = env.get("cmd")
        args = env.get("args") or {}

        if cmd == "add_channel":
            chans = args.get("channels") or [args]
            for c in chans:
                self.channels[c["id"]] = {
                    "id": c["id"],
                    "freq_mhz": c.get("freq_mhz"),
                    "mode": c.get("mode", "am"),
                    "squelch_dbfs": c.get("squelch_dbfs", -60.0),
                    "gain_db": c.get("gain_db", 0.0),
                    "label": c.get("label"),
                    "signal_level_dbfs": self.signal_level_dbfs,
                }
            return self._ok(req_id, {"count": len(chans)})

        if cmd == "remove_channel":
            cid = args.get("id")
            if cid not in self.channels:
                return self._reject(req_id, f"unknown channel: {cid}")
            del self.channels[cid]
            return self._ok(req_id, {"id": cid})

        if cmd == "set_squelch":
            cid = args.get("id")
            if cid not in self.channels:
                return self._reject(req_id, f"unknown channel: {cid}")
            self.channels[cid]["squelch_dbfs"] = float(args.get("dbfs"))
            return self._ok(req_id, {"dbfs": float(args.get("dbfs"))})

        if cmd == "reset":
            self.channels.clear()
            return self._ok(req_id, {"pool_free": 32, "removed": []})

        if cmd == "get_status":
            return self._ok(req_id, {
                "version": 1,
                "band": "airband",
                "channels": [
                    {**c, "slot": i, "squelch_open": False}
                    for i, c in enumerate(self.channels.values())
                ],
                "pool_free": 32 - len(self.channels),
                "icecast_state": self.icecast_state,
                "icecast_bytes_sent": 12345,
                "icecast_drop_count": 0,
            })

        return self._reject(req_id, f"unknown cmd: {cmd}")

    @staticmethod
    def _ok(req_id, data):
        return {"v": 1, "id": req_id, "status": "ok", "data": data, "error": None}

    @staticmethod
    def _reject(req_id, err):
        return {"v": 1, "id": req_id, "status": "rejected", "data": None, "error": err}


# --- fixtures ---------------------------------------------------------------


@pytest.fixture()
def two_daemons(monkeypatch, tmp_path):
    """Two mock daemons + a freshly reloaded chirp_client pointed at them."""
    air = StubDaemon().start()
    gnd = StubDaemon().start()
    monkeypatch.setenv("SB5_USE_GR_DEMOD", "true")
    monkeypatch.setenv("CHIRP_CLIENT_LOG_PATH", str(tmp_path / "chirp.jsonl"))
    monkeypatch.setenv("CHIRP_AIRBAND_PORT", str(air.port))
    monkeypatch.setenv("CHIRP_GROUND_PORT", str(gnd.port))
    import ui.chirp_client as cc
    importlib.reload(cc)
    cc.reset_singletons()
    yield air, gnd, cc
    air.stop()
    gnd.stop()


# --- test: reset_radios_via_chirp end-to-end --------------------------------


def test_reset_radios_via_chirp_hits_both_daemons(two_daemons):
    air, gnd, cc = two_daemons
    air.channels["X"] = {"id": "X", "freq_mhz": 121.5, "mode": "am",
                          "squelch_dbfs": -60, "gain_db": 0,
                          "label": None, "signal_level_dbfs": -70.0}
    gnd.channels["Y"] = {"id": "Y", "freq_mhz": 138.05, "mode": "nfm",
                          "squelch_dbfs": -50, "gain_db": 0,
                          "label": None, "signal_level_dbfs": -68.0}
    import ui.chirp_adapter as ca
    importlib.reload(ca)
    ok, msg, err = ca.reset_radios_via_chirp()
    assert ok is True
    assert err == ""
    assert "triggered" in msg
    # Both daemons reset
    assert air.channels == {}
    assert gnd.channels == {}
    # Both received reset
    assert any(r["cmd"] == "reset" for r in air.received)
    assert any(r["cmd"] == "reset" for r in gnd.received)


def test_reset_radios_via_chirp_partial_failure(two_daemons):
    air, gnd, cc = two_daemons
    # Stop ground daemon -> timeouts when adapter probes it.
    gnd.stop()
    # Tighten the timeout so the test is fast.
    cc.get_ground_client().timeout = 0.3
    import ui.chirp_adapter as ca
    importlib.reload(ca)
    ok, msg, err = ca.reset_radios_via_chirp()
    assert ok is False
    assert "partial fail" in err
    assert "ground" in err


# --- test: apply_squelch_preset end-to-end ----------------------------------


def test_apply_squelch_preset_pushes_per_channel(two_daemons):
    air, gnd, cc = two_daemons
    # Pre-populate airband with three channels.
    for cid, freq in [("A", 121.0), ("B", 122.0), ("C", 123.0)]:
        air.channels[cid] = {
            "id": cid, "freq_mhz": freq, "mode": "am",
            "squelch_dbfs": -60.0, "gain_db": 0.0,
            "label": None, "signal_level_dbfs": -70.0,
        }
    import ui.chirp_adapter as ca
    importlib.reload(ca)
    plan = ca.apply_squelch_preset_via_chirp("airband", "balanced")
    # 3 channels, threshold = noise + 6 dB = -64
    assert plan["applied_count"] == 3
    assert plan["rejected_count"] == 0
    assert plan["threshold_median"] == -64
    assert plan["preset"] == "balanced"
    # All three got set_squelch
    assert air.channels["A"]["squelch_dbfs"] == -64.0
    assert air.channels["B"]["squelch_dbfs"] == -64.0
    assert air.channels["C"]["squelch_dbfs"] == -64.0


def test_apply_squelch_preset_rejects_poison_noise_floor(two_daemons):
    air, gnd, cc = two_daemons
    # Set the stub's signal_level_dbfs above the AM poison ceiling (-55).
    air.signal_level_dbfs = -10.0
    for cid, freq in [("A", 121.0), ("B", 122.0)]:
        air.channels[cid] = {
            "id": cid, "freq_mhz": freq, "mode": "am",
            "squelch_dbfs": -60.0, "gain_db": 0.0,
            "label": None, "signal_level_dbfs": -10.0,
        }
    import ui.chirp_adapter as ca
    importlib.reload(ca)
    plan = ca.apply_squelch_preset_via_chirp("airband", "balanced")
    assert plan["error"] == "noise_floor_not_warm"
    assert plan["status"] == "rejected"
    assert plan["retry_after_sec"] == 30
    # NO set_squelch was sent
    set_squelch_calls = [r for r in air.received if r["cmd"] == "set_squelch"]
    assert set_squelch_calls == []


def test_apply_squelch_preset_daemon_down(two_daemons):
    air, gnd, cc = two_daemons
    air.stop()
    cc.get_airband_client().timeout = 0.3
    import ui.chirp_adapter as ca
    importlib.reload(ca)
    plan = ca.apply_squelch_preset_via_chirp("airband", "balanced")
    assert plan["error"].startswith("chirp_daemon_down")
    assert plan["changed"] is False


# --- test: heartbeat end-to-end (real ChirpClient + mock daemon) ------------


def test_heartbeat_probes_mock_daemons(two_daemons, monkeypatch):
    air, gnd, cc = two_daemons
    air.channels["A"] = {"id": "A", "freq_mhz": 121.0, "mode": "am",
                          "squelch_dbfs": -60, "gain_db": 0,
                          "label": None, "signal_level_dbfs": -70.0}
    import ui.handlers as h
    importlib.reload(h)
    with h._HEARTBEAT_LOCK:
        h._HEARTBEAT_CACHE.clear()
    # Stub the legacy probes (we only care about the chirp rows).
    from unittest.mock import patch
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

    by_label = {r["label"]: r for r in payload["evidence"]}
    assert by_label["chirp-airband"]["status"] == "ok"
    assert by_label["chirp-ground"]["status"] == "ok"
    assert "1 chan" in by_label["chirp-airband"]["value"]
    # icecast state is from the mock daemon's get_status response
    assert "connected" in by_label["chirp-airband icecast"]["value"]


def test_heartbeat_marks_wedged_when_chirp_daemon_dies(two_daemons, monkeypatch):
    air, gnd, cc = two_daemons
    air.stop()
    # Tighten timeout for fast test.
    cc.get_airband_client().timeout = 0.3
    import ui.handlers as h
    importlib.reload(h)
    with h._HEARTBEAT_LOCK:
        h._HEARTBEAT_CACHE.clear()
    from unittest.mock import patch
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

    assert payload["state"] == "wedged"
    by_label = {r["label"]: r for r in payload["evidence"]}
    assert by_label["chirp-airband"]["status"] == "bad"


# --- test: flag OFF path totally untouched ----------------------------------


def test_flag_off_no_chirp_traffic(monkeypatch, tmp_path):
    """With flag off, the chirp client must not send any datagrams to
    the mock daemons even when a heartbeat probe runs."""
    air = StubDaemon().start()
    gnd = StubDaemon().start()
    monkeypatch.delenv("SB5_USE_GR_DEMOD", raising=False)
    monkeypatch.setenv("CHIRP_AIRBAND_PORT", str(air.port))
    monkeypatch.setenv("CHIRP_GROUND_PORT", str(gnd.port))
    monkeypatch.setenv("CHIRP_CLIENT_LOG_PATH", str(tmp_path / "chirp.jsonl"))
    try:
        import ui.chirp_client as cc
        importlib.reload(cc)
        cc.reset_singletons()
        import ui.handlers as h
        importlib.reload(h)
        with h._HEARTBEAT_LOCK:
            h._HEARTBEAT_CACHE.clear()
        from unittest.mock import patch
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
            _payload = h._compute_heartbeat_payload()
        # Give the daemon a moment in case anything was in flight.
        time.sleep(0.05)
        # No envelopes hit the mock daemons.
        assert air.received == []
        assert gnd.received == []
    finally:
        air.stop()
        gnd.stop()
