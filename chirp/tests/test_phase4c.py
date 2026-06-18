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
        # SB6 2026-06-18 global-squelch redesign: the one band-wide threshold.
        self.global_squelch_dbfs: float = -56.0
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

        if cmd == "set_global_squelch_dbfs":
            # SB6 2026-06-18: apply the one band-wide threshold to every channel.
            self.global_squelch_dbfs = float(args.get("dbfs"))
            for c in self.channels.values():
                c["squelch_dbfs"] = self.global_squelch_dbfs
            return self._ok(req_id, {
                "dbfs": self.global_squelch_dbfs,
                "channels_applied": len(self.channels),
            })

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
                "global_squelch_dbfs": self.global_squelch_dbfs,
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
    """Phase 4c contract: reset_radios_via_chirp must hit BOTH daemons
    and empty their pools.  We pass unconditional_repopulate=False here
    to preserve the original "does reset wipe?" semantics — Phase 4d
    flipped the default to True (auto-repopulate from HPState) so the
    dashboard auto-apply countdown no longer silently nukes the pool.
    Repopulate behaviour gets its own coverage further down in this
    file (test_reset_radios_via_chirp_repopulates_both_bands_by_default
    and friends)."""
    air, gnd, cc = two_daemons
    air.channels["X"] = {"id": "X", "freq_mhz": 121.5, "mode": "am",
                          "squelch_dbfs": -60, "gain_db": 0,
                          "label": None, "signal_level_dbfs": -70.0}
    gnd.channels["Y"] = {"id": "Y", "freq_mhz": 138.05, "mode": "nfm",
                          "squelch_dbfs": -50, "gain_db": 0,
                          "label": None, "signal_level_dbfs": -68.0}
    import ui.chirp_adapter as ca
    importlib.reload(ca)
    ok, msg, err = ca.reset_radios_via_chirp(unconditional_repopulate=False)
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


def test_apply_squelch_preset_pushes_global(two_daemons):
    """SB6 2026-06-18 global-squelch redesign: the preset apply now computes ONE
    band-wide threshold from the AGGREGATE noise floor (median across channels)
    + the preset margin and pushes it with a single set_global_squelch_dbfs.
    Every channel ends up at that one value (no per-channel set_squelch)."""
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
    # 3 channels, aggregate noise -70, threshold = -70 + 6 dB = -64
    assert plan["applied_count"] == 3
    assert plan["rejected_count"] == 0
    assert plan["threshold_median"] == -64
    assert plan["global_squelch_dbfs"] == -64
    assert plan["preset"] == "balanced"
    # ONE global push, not three per-channel set_squelch calls.
    global_calls = [r for r in air.received if r["cmd"] == "set_global_squelch_dbfs"]
    assert len(global_calls) == 1
    assert global_calls[0]["args"]["dbfs"] == -64.0
    assert [r for r in air.received if r["cmd"] == "set_squelch"] == []
    # Every channel inherited the one global value.
    assert air.channels["A"]["squelch_dbfs"] == -64.0
    assert air.channels["B"]["squelch_dbfs"] == -64.0
    assert air.channels["C"]["squelch_dbfs"] == -64.0
    assert air.global_squelch_dbfs == -64.0


def test_apply_squelch_preset_applies_under_high_signal_level(two_daemons):
    """Updated spec: the chirp path no longer runs the poison-ceiling
    rejection that the rtl-airband path runs. signal_level_dbfs above
    the old AM ceiling (-55 dBFS) is NOT an error condition — the
    operator's chip click applies unconditionally. See
    chirp/tests/test_chirp_poison_guard_removed.py for the regression
    suite that anchors this contract.

    Pre-removal this test asserted the opposite (error ==
    noise_floor_not_warm, status == rejected, no set_squelch). The
    rejection broke EVERY operator chip click under chirp because the
    airband's busy-time signal_level_dbfs (~-44) routinely sat above
    the ceiling.
    """
    air, gnd, cc = two_daemons
    # signal_level above old AM poison ceiling — would have rejected
    # the entire apply pre-removal.
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
    assert plan.get("error", "") == ""
    assert plan.get("status") != "rejected"
    assert plan["applied_count"] == 2
    assert plan["rejected_count"] == 0
    # Aggregate noise -10, threshold = -10 + 6 (balanced) = -4. NOT clamped.
    assert plan["threshold_median"] == -4
    assert plan["global_squelch_dbfs"] == -4
    # ONE global push at -4 (SB6), applied to both channels.
    global_calls = [r for r in air.received if r["cmd"] == "set_global_squelch_dbfs"]
    assert len(global_calls) == 1
    assert global_calls[0]["args"]["dbfs"] == -4.0


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


# ---------------------------------------------------------------------------
# Phase 4d additions — reset_radios_via_chirp must repopulate the pool by
# default so a dashboard auto-apply chip-click no longer silently wipes the
# channel inventory and leaves the operator at "0 hits".
# ---------------------------------------------------------------------------

from types import SimpleNamespace


def _stub_hp_state(*, fav_id: str, enabled_air: bool, enabled_ground: bool,
                   airband_freqs=(121.0, 122.5, 130.5),
                   ground_freqs=(138.05, 138.3, 139.15)):
    """Build a minimal HPState-shaped stub with one favorite + custom
    channels.  Used by the Phase 4d repopulate tests so we don't need
    a real /home/ubuntu/scannerproject/data/hp_state.json on disk.
    """
    custom = []
    for f in airband_freqs:
        custom.append({
            "id": f"freq:test:air:{f:.4f}", "frequency": float(f),
            "alpha_tag": f"AIR {f}", "department_name": "TEST",
            "kind": "conventional",
        })
    for f in ground_freqs:
        custom.append({
            "id": f"freq:test:ground:{f:.4f}", "frequency": float(f),
            "alpha_tag": f"GND {f}", "department_name": "TEST",
            "kind": "conventional",
        })
    favorites = [{
        "id": fav_id, "label": fav_id.upper(),
        "enabled": True,
        "enabled_air": bool(enabled_air),
        "enabled_ground": bool(enabled_ground),
        "enabled_digital": False,
        "custom_favorites": [],
        "profile_id": "", "target": "favorites", "type": "list",
    }]
    return SimpleNamespace(favorites=favorites, custom_favorites=custom)


@pytest.fixture()
def stub_hp_state(monkeypatch):
    """Yield a callable that installs a stub HPState.load() returning
    the SimpleNamespace from ``_stub_hp_state``.  Also stubs out the
    post-add preset apply path so the test doesn't depend on
    /etc/scannerproject configs.
    """
    import ui.chirp_adapter as ca
    importlib.reload(ca)
    import ui.hp_state as hps

    installed: dict[str, object] = {}

    def install(**kwargs):
        state = _stub_hp_state(**kwargs)
        monkeypatch.setattr(hps.HPState, "load",
                            classmethod(lambda cls, *a, **kw: state))
        # Re-import chirp_adapter so it picks up the patched HPState
        # via its lazy import path.
        installed["state"] = state

        # Neutralise the post-add preset re-apply step: it tries to
        # read /home/ubuntu/scannerproject/profiles/* which doesn't
        # exist in the test sandbox.  Pretend "no preset configured"
        # so _populate_after_reset's Step B is a no-op.
        try:
            import ui.managed_analog_controls as mac
            monkeypatch.setattr(mac, "recommended_managed_controls",
                                lambda *a, **kw: {})
        except Exception:
            pass
        try:
            import ui.profile_config as pc
            monkeypatch.setattr(pc, "resolve_controls_path",
                                lambda *a, **kw: "/tmp/nonexistent.conf")
        except Exception:
            pass
        return state

    return install


def test_reset_radios_via_chirp_repopulates_both_bands_by_default(
    two_daemons, stub_hp_state,
):
    """Default reset_radios_via_chirp() should reset both daemons AND
    immediately push the enabled favorite's channel list back, so the
    pool stays populated end-to-end (no UI-visible "0 hits" gap)."""
    air, gnd, cc = two_daemons
    # Pre-populate the daemons to prove the reset clears them.
    air.channels["X"] = {"id": "X", "freq_mhz": 121.5, "mode": "am",
                          "squelch_dbfs": -60, "gain_db": 0,
                          "label": None, "signal_level_dbfs": -70.0}
    gnd.channels["Y"] = {"id": "Y", "freq_mhz": 138.05, "mode": "nfm",
                          "squelch_dbfs": -50, "gain_db": 0,
                          "label": None, "signal_level_dbfs": -68.0}

    stub_hp_state(fav_id="fav-test", enabled_air=True, enabled_ground=True)
    import ui.chirp_adapter as ca
    importlib.reload(ca)

    ok, msg, err = ca.reset_radios_via_chirp()

    assert ok is True, f"expected ok=True, got msg={msg!r} err={err!r}"
    assert err == ""
    # Per-band repop counts surface in the message body so a forensic
    # pass on /var/log can see "we repopulated 3+3" without parsing
    # chirp_client.jsonl.
    assert "repop=3" in msg, msg
    # Both daemons received reset.
    air_cmds = [r["cmd"] for r in air.received]
    gnd_cmds = [r["cmd"] for r in gnd.received]
    assert "reset" in air_cmds and "reset" in gnd_cmds
    # Both daemons received exactly one add_channel after the reset
    # (no double-reset — reset_radios_via_chirp goes through
    # _populate_after_reset, not the full activate_favorite_via_chirp).
    assert air_cmds.count("reset") == 1, f"airband resets: {air_cmds}"
    assert gnd_cmds.count("reset") == 1, f"ground resets: {gnd_cmds}"
    assert air_cmds.count("add_channel") == 1, f"airband adds: {air_cmds}"
    assert gnd_cmds.count("add_channel") == 1, f"ground adds: {gnd_cmds}"
    # Order: reset BEFORE add_channel.
    assert air_cmds.index("reset") < air_cmds.index("add_channel")
    assert gnd_cmds.index("reset") < gnd_cmds.index("add_channel")
    # Daemon state reflects the repopulate.
    assert len(air.channels) == 3
    assert len(gnd.channels) == 3
    # Channels are band-filtered (<137 for air, >=137 for ground).
    for c in air.channels.values():
        assert c["freq_mhz"] < 137.0
    for c in gnd.channels.values():
        assert c["freq_mhz"] >= 137.0


def test_reset_radios_via_chirp_opt_out_keeps_pool_empty(
    two_daemons, stub_hp_state,
):
    """unconditional_repopulate=False must skip the repopulate step
    entirely so the CLI / test harness / future callers can ask for a
    really-empty pool when they need to."""
    air, gnd, cc = two_daemons
    air.channels["X"] = {"id": "X", "freq_mhz": 121.5, "mode": "am",
                          "squelch_dbfs": -60, "gain_db": 0,
                          "label": None, "signal_level_dbfs": -70.0}
    gnd.channels["Y"] = {"id": "Y", "freq_mhz": 138.05, "mode": "nfm",
                          "squelch_dbfs": -50, "gain_db": 0,
                          "label": None, "signal_level_dbfs": -68.0}

    stub_hp_state(fav_id="fav-test", enabled_air=True, enabled_ground=True)
    import ui.chirp_adapter as ca
    importlib.reload(ca)

    ok, msg, err = ca.reset_radios_via_chirp(unconditional_repopulate=False)

    assert ok is True
    assert err == ""
    # Message must signal no repopulate happened.
    assert "no-repop" in msg, msg
    # No add_channel went out to either daemon.
    air_cmds = [r["cmd"] for r in air.received]
    gnd_cmds = [r["cmd"] for r in gnd.received]
    assert "reset" in air_cmds and "reset" in gnd_cmds
    assert "add_channel" not in air_cmds
    assert "add_channel" not in gnd_cmds
    # Pools really empty.
    assert air.channels == {}
    assert gnd.channels == {}


def test_reset_radios_via_chirp_no_enabled_favorite_skips_repopulate(
    two_daemons, stub_hp_state,
):
    """When the HP state has NO favorite enabled for a band, repop is
    skipped for that band — clean operational signal, not a failure."""
    air, gnd, cc = two_daemons
    # enabled_air=True, enabled_ground=False → only airband repopulates.
    stub_hp_state(fav_id="fav-air-only",
                  enabled_air=True, enabled_ground=False)
    import ui.chirp_adapter as ca
    importlib.reload(ca)

    ok, msg, err = ca.reset_radios_via_chirp()

    assert ok is True
    # Airband repopulated, ground skipped.
    assert "repop=3" in msg
    assert "no-repop(no_enabled_favorite)" in msg, msg
    air_cmds = [r["cmd"] for r in air.received]
    gnd_cmds = [r["cmd"] for r in gnd.received]
    assert "add_channel" in air_cmds
    assert "add_channel" not in gnd_cmds


def test_reset_radios_via_chirp_repopulate_failure_logged_not_fatal(
    two_daemons, stub_hp_state, monkeypatch, caplog,
):
    """If _populate_after_reset raises (e.g. corrupt config, downstream
    bug), the overall reset_radios_via_chirp call still reports ok=True
    because the reset itself succeeded.  The per-band repop error
    surfaces in the message so the operator sees it."""
    air, gnd, cc = two_daemons
    stub_hp_state(fav_id="fav-test", enabled_air=True, enabled_ground=True)
    import ui.chirp_adapter as ca
    importlib.reload(ca)

    boom_calls = {"n": 0}

    def _boom(band, fav_id):
        boom_calls["n"] += 1
        raise RuntimeError(f"synthetic boom for {band}")

    monkeypatch.setattr(ca, "_populate_after_reset", _boom)

    with caplog.at_level("ERROR", logger="ui.chirp_adapter"):
        ok, msg, err = ca.reset_radios_via_chirp()

    # Reset succeeded → overall ok.  Per-band repop errors live in msg.
    assert ok is True
    assert err == ""
    assert "repop-fail" in msg, msg
    # Helper was invoked for both bands (no early-exit after first
    # band's failure).
    assert boom_calls["n"] == 2


def test_activate_favorite_via_chirp_still_works_post_refactor(
    two_daemons, stub_hp_state,
):
    """Regression: the refactor that extracted _populate_after_reset out
    of activate_favorite_via_chirp must not have broken its existing
    end-to-end contract (reset → add_channel for one band)."""
    air, _gnd, cc = two_daemons
    stub_hp_state(fav_id="fav-test", enabled_air=True, enabled_ground=False)
    import ui.chirp_adapter as ca
    importlib.reload(ca)

    result = ca.activate_favorite_via_chirp("airband", "fav-test")

    assert result["ok"] is True
    assert result["target"] == "airband"
    assert result["fav_id"] == "fav-test"
    assert result["added_count"] == 3
    # Exactly one reset + one add_channel against the airband daemon.
    air_cmds = [r["cmd"] for r in air.received]
    assert air_cmds.count("reset") == 1
    assert air_cmds.count("add_channel") == 1
    assert air_cmds.index("reset") < air_cmds.index("add_channel")
    assert len(air.channels) == 3
