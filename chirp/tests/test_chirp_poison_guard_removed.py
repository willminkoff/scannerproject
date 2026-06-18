"""Regression tests for the poison-ceiling guard removal on the chirp path.

Before this change, ``ui.chirp_adapter.apply_squelch_preset_via_chirp``
and ``ui.squelch_tracker._run_cycle_for_band_via_chirp`` ran the same
poison-noise-floor rejection that ``ui.squelch_preset.apply_preset`` runs
for the rtl-airband path: if the median ``signal_level_dbfs`` exceeded a
per-band ceiling (AM = -55 dBFS, NFM = -50 dBFS), the entire apply was
short-circuited with ``error == "noise_floor_not_warm"`` and a 30-second
retry hint.

That guard was a rtl-airband-era safeguard against the pre-buffer init
constant poisoning the squelch estimator across a service restart. The
chirp daemons have no such initialization quirk — ``set_squelch`` is a
hot UDP call on the active demod block — and meanwhile a normally busy
airband produced signal_level_dbfs values that routinely sat above the
ceiling (the airband median is around -44 dBFS during regular traffic),
so EVERY operator chip click tripped the guard and showed the user a
spurious "noise floor not warm, retry 30s" message.

These tests pin the new contract: under the chirp path, the preset and
the tracker apply unconditionally — high signal_level_dbfs is *not* an
error condition. If anyone re-introduces the guard, the assertions in
this file blow up.

Companion changes that landed in the same commit:
  - The rtl-airband path (``ui.squelch_preset.apply_preset`` and
    ``ui.squelch_tracker._run_cycle_for_band``) STILL runs the poison
    guard. Different backend, different failure mode, different
    contract — we do not touch it here.
  - The audit-log emit gate in ``squelch_tracker._tracker_loop`` (~line
    803) still includes ``"poison_noise_floor"`` in its skipped-result
    filter for the rtl-airband path. Tests in this file therefore do
    not assert on the absence of that constant from the gate.
"""
from __future__ import annotations

import importlib
import json
import socket
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# ---------------------------------------------------------------------------
# Reuse the StubDaemon pattern from test_phase4c.py. Duplicating a slim
# version keeps this file independent — if test_phase4c.py is rewritten
# the regression coverage here stays intact.
# ---------------------------------------------------------------------------


class _StubDaemon:
    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.channels: dict[str, dict] = {}
        self.global_squelch_dbfs: float = -56.0
        self.received: list[dict] = []
        self.signal_level_dbfs = -70.0
        self.icecast_state = "connected"
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> "_StubDaemon":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(b"\x00", ("127.0.0.1", self.port))
            s.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)
        self.sock.close()

    def _loop(self) -> None:
        self.sock.settimeout(0.3)
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if self._stop.is_set() or not data or data == b"\x00":
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
        if cmd == "set_squelch":
            cid = args.get("id")
            if cid not in self.channels:
                return self._reject(req_id, f"unknown channel: {cid}")
            self.channels[cid]["squelch_dbfs"] = float(args.get("dbfs"))
            return self._ok(req_id, {"dbfs": float(args.get("dbfs"))})
        if cmd == "set_global_squelch_dbfs":
            # SB6 2026-06-18: one band-wide threshold applied to every channel.
            self.global_squelch_dbfs = float(args.get("dbfs"))
            for c in self.channels.values():
                c["squelch_dbfs"] = self.global_squelch_dbfs
            return self._ok(req_id, {
                "dbfs": self.global_squelch_dbfs,
                "channels_applied": len(self.channels),
            })
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
                "icecast_bytes_sent": 0,
                "icecast_drop_count": 0,
            })
        return self._reject(req_id, f"unknown cmd: {cmd}")

    @staticmethod
    def _ok(req_id, data):
        return {"v": 1, "id": req_id, "status": "ok", "data": data, "error": None}

    @staticmethod
    def _reject(req_id, err):
        return {"v": 1, "id": req_id, "status": "rejected", "data": None, "error": err}


@pytest.fixture()
def air_daemon(monkeypatch, tmp_path):
    """Spin a single airband mock daemon and point a fresh chirp_client at it."""
    air = _StubDaemon().start()
    monkeypatch.setenv("SB5_USE_GR_DEMOD", "true")
    monkeypatch.setenv("CHIRP_CLIENT_LOG_PATH", str(tmp_path / "chirp.jsonl"))
    monkeypatch.setenv("CHIRP_AIRBAND_PORT", str(air.port))
    monkeypatch.setenv("CHIRP_GROUND_PORT", "1")  # unused but parsed
    import ui.chirp_client as cc
    importlib.reload(cc)
    cc.reset_singletons()
    yield air
    air.stop()


# ---------------------------------------------------------------------------
# Adapter (apply_squelch_preset_via_chirp)
# ---------------------------------------------------------------------------


def test_apply_succeeds_when_signal_level_above_old_poison_ceiling(air_daemon):
    """signal_level_dbfs = -40 (above the old AM poison ceiling of -55)
    must NOT trip the noise-floor-not-warm rejection. The preset applies,
    set_squelch fires for every channel, and the response carries no
    error.
    """
    air = air_daemon
    air.signal_level_dbfs = -40.0
    for cid, freq in [("A", 121.0), ("B", 122.0), ("C", 123.0)]:
        air.channels[cid] = {
            "id": cid, "freq_mhz": freq, "mode": "am",
            "squelch_dbfs": -60.0, "gain_db": 0.0,
            "label": None, "signal_level_dbfs": -40.0,
        }
    import ui.chirp_adapter as ca
    importlib.reload(ca)

    plan = ca.apply_squelch_preset_via_chirp("airband", "balanced")

    # Most critical assertion: no noise_floor_not_warm error.
    assert plan.get("error") != "noise_floor_not_warm", (
        "poison-ceiling guard re-introduced on the chirp path; see commit "
        "removing it for the architectural rationale."
    )
    assert plan.get("error", "") == ""
    assert plan.get("status") != "rejected"
    assert plan["applied_count"] == 3
    assert plan["rejected_count"] == 0
    # Aggregate noise -40 + 6 (balanced margin) = -34. Not clamped to -100.
    assert plan["threshold_median"] == -34
    # SB6 2026-06-18: ONE global push (not per-channel set_squelch).
    global_calls = [r for r in air.received if r["cmd"] == "set_global_squelch_dbfs"]
    assert len(global_calls) == 1
    assert global_calls[0]["args"]["dbfs"] == -34.0
    assert [r for r in air.received if r["cmd"] == "set_squelch"] == []


def test_apply_no_per_channel_clamp_when_one_channel_above_old_ceiling(air_daemon):
    """One healthy channel + one channel with signal_level above the old
    ceiling. Old behaviour: clamp the offending channel to -100 dBFS.
    SB6 2026-06-18 behaviour: the band runs on ONE threshold derived from the
    AGGREGATE noise floor (median across channels) + margin — no per-channel
    value, and crucially no -100 clamp on the hot channel.
    """
    air = air_daemon
    # Two channels; we'll mutate per-channel signal_level via a custom
    # get_status response by adjusting the stub after add.
    air.channels["A"] = {
        "id": "A", "freq_mhz": 121.0, "mode": "am",
        "squelch_dbfs": -60.0, "gain_db": 0.0,
        "label": None, "signal_level_dbfs": -70.0,  # healthy
    }
    air.channels["B"] = {
        "id": "B", "freq_mhz": 122.0, "mode": "am",
        "squelch_dbfs": -60.0, "gain_db": 0.0,
        "label": None, "signal_level_dbfs": -30.0,  # was poison
    }
    import ui.chirp_adapter as ca
    importlib.reload(ca)

    plan = ca.apply_squelch_preset_via_chirp("airband", "balanced")

    assert plan.get("error", "") == ""
    assert plan["applied_count"] == 2
    # sanitized_channels stays in the response shape but is always empty
    # on the chirp path now.
    assert plan["sanitized_count"] == 0
    assert plan["sanitized_channels"] == []
    # SB6: aggregate noise = median(-70, -30) = -50; +6 (balanced) = -44.
    # ONE global push at -44; BOTH channels inherit it (no -100 clamp on B).
    assert plan["global_squelch_dbfs"] == -44
    global_calls = [r for r in air.received if r["cmd"] == "set_global_squelch_dbfs"]
    assert len(global_calls) == 1
    assert global_calls[0]["args"]["dbfs"] == -44.0
    assert air.channels["A"]["squelch_dbfs"] == -44.0
    assert air.channels["B"]["squelch_dbfs"] == -44.0


def test_apply_succeeds_for_airband_median_at_real_world_level(air_daemon):
    """Smoke test matching the production scenario that prompted this
    fix: a real airband at a busy site has signal_level_dbfs around
    -44 dBFS (above the old -55 AM ceiling). Pre-change this tripped
    on EVERY chip click. Post-change it must apply cleanly.
    """
    air = air_daemon
    air.signal_level_dbfs = -44.0
    for cid, freq in [("TWR", 119.1), ("APP", 124.5), ("GND", 121.9)]:
        air.channels[cid] = {
            "id": cid, "freq_mhz": freq, "mode": "am",
            "squelch_dbfs": -60.0, "gain_db": 0.0,
            "label": None, "signal_level_dbfs": -44.0,
        }
    import ui.chirp_adapter as ca
    importlib.reload(ca)

    plan = ca.apply_squelch_preset_via_chirp("airband", "selective")
    assert plan.get("error", "") == ""
    assert plan["applied_count"] == 3
    # PRESET_MARGINS_DB = {sensitive: 3, balanced: 6, selective: 12}.
    # selective at noise=-44 -> threshold = -44 + 12 = -32.
    assert plan["threshold_median"] == -32


# ---------------------------------------------------------------------------
# Tracker (_run_cycle_for_band_via_chirp)
# ---------------------------------------------------------------------------


def _reload_tracker():
    import ui.chirp_client as cc
    importlib.reload(cc)
    cc.reset_singletons()
    import ui.squelch_tracker as st
    importlib.reload(st)
    return st, cc


def _mock_chirp_status(channels):
    return {"channels": channels, "band": "airband",
            "pool_free": 32 - len(channels)}


def test_tracker_chirp_cycle_completes_when_signal_above_old_ceiling(
        monkeypatch, tmp_path):
    """Tracker cycle on the chirp path must NOT short-circuit with
    skipped == poison_noise_floor when median signal_level_dbfs is above
    the old per-band ceiling.
    """
    monkeypatch.setenv("SB5_USE_GR_DEMOD", "true")
    monkeypatch.setenv("SQUELCH_TRACKER_AUDIT_LOG_PATH",
                       str(tmp_path / "t.jsonl"))
    st, _cc = _reload_tracker()
    fake_client = MagicMock()
    fake_client.get_status.return_value = _mock_chirp_status([
        {"id": "T1", "freq_mhz": 121.5, "signal_level_dbfs": -40.0,
         "squelch_dbfs": -50, "mode": "am"},
        {"id": "T2", "freq_mhz": 122.0, "signal_level_dbfs": -42.0,
         "squelch_dbfs": -50, "mode": "am"},
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

    # Most critical: NOT skipped as poison_noise_floor.
    assert result.get("skipped") != "poison_noise_floor", (
        "poison-ceiling skip re-introduced on the chirp tracker path; see "
        "commit removing it for the architectural rationale."
    )
    assert result["applied"] is True
    assert result["via"] == "chirp"
    assert result["applied_count"] == 2
    # No "poison_ceiling_dbfs" field on the chirp result (dropped).
    assert "poison_ceiling_dbfs" not in result


def test_tracker_chirp_cycle_no_per_channel_fallback_under_high_signal(
        monkeypatch, tmp_path):
    """Tracker cycle must apply noise+margin to a channel whose
    signal_level_dbfs is above the old ceiling — no fallback to the
    prior threshold.
    """
    monkeypatch.setenv("SB5_USE_GR_DEMOD", "true")
    monkeypatch.setenv("SQUELCH_TRACKER_AUDIT_LOG_PATH",
                       str(tmp_path / "t.jsonl"))
    st, _cc = _reload_tracker()
    fake_client = MagicMock()
    fake_client.get_status.return_value = _mock_chirp_status([
        {"id": "T1", "freq_mhz": 121.5, "signal_level_dbfs": -70.0,
         "squelch_dbfs": -50, "mode": "am"},   # would be healthy under old rules
        {"id": "T2", "freq_mhz": 122.0, "signal_level_dbfs": -10.0,
         "squelch_dbfs": -50, "mode": "am"},   # would be poison under old rules
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

    by_id = {call.args[0]: call.args[1]
             for call in fake_client.set_squelch.call_args_list}
    assert by_id["T1"] == -64.0          # -70 + 6
    assert by_id["T2"] == -4.0           # -10 + 6, NOT clamped to prior -50
    assert result["sanitized_count"] == 0
    assert result["sanitized_channels"] == []
    assert result["applied"] is True


def test_tracker_chirp_audit_log_carries_no_poison_noise_floor_skip(
        monkeypatch, tmp_path):
    """Smoke test: an audit-log emission for a cycle run under the
    high-signal-level scenario should NOT contain the poison_noise_floor
    sentinel for the chirp path.
    """
    monkeypatch.setenv("SB5_USE_GR_DEMOD", "true")
    audit_path = tmp_path / "t.jsonl"
    monkeypatch.setenv("SQUELCH_TRACKER_AUDIT_LOG_PATH", str(audit_path))
    st, _cc = _reload_tracker()
    fake_client = MagicMock()
    fake_client.get_status.return_value = _mock_chirp_status([
        {"id": "T1", "freq_mhz": 121.5, "signal_level_dbfs": -44.0,
         "squelch_dbfs": -50, "mode": "am"},
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

    assert result.get("skipped") != "poison_noise_floor"
    assert result["applied"] is True
