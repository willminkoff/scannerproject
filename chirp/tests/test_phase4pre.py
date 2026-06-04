"""End-to-end Phase 4-pre integration tests.

Spins up a real :class:`ChirpFlowgraph` over a synthetic file IQ source and
verifies the full LO-scheduler-into-daemon wiring:

  * Scheduler boots, plans clusters, applies cluster 0 on the first tick.
  * Multi-cluster setup: scheduler rotates on dwell, emits ``cluster_hop``
    events, parked-channel ids flip on each hop.
  * Hit log integrity: parked channels NEVER fire hit_start, hits include
    ``cluster_center_hz`` tag from hit_start time.
  * ``get_status`` includes ``lo_scheduler`` block AND ``is_parked`` per
    channel.
  * Single-channel pool: scheduler does not rotate (the
    "no regression at cutover" guard).
  * Operator add_channel mid-dwell triggers a plan recompute.
  * Plan-failed mode: max_clusters=1 + multi-cluster channel list emits
    ``scheduler_plan_failed`` and parks every channel.

These are slow-ish tests — they need real flowgraph start / dwell windows.
They run by default; mark with ``slow`` if your local dev loop wants to
skip them via ``pytest -m 'not slow'``.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np
import pytest

from chirp.cmd.server import CommandServer, ServerConfig
from chirp.daemon import ChirpFlowgraph, DaemonConfig
from chirp.state import StateStore


# ---------------------------------------------------------------------------
# UDP helpers
# ---------------------------------------------------------------------------


def _udp_send(port: int, body: bytes, timeout: float = 1.0) -> bytes:
    """Send a single UDP request to the daemon and read the response."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(body, ("127.0.0.1", port))
        data, _ = s.recvfrom(65536)
        return data
    finally:
        s.close()


class _EventListener:
    """UDP listener for the daemon's event_sink stream.

    The daemon emits every event_emit() to the configured event_sink
    address.  We bind ephemeral, hand the address to DaemonConfig, and
    spool events into ``self.events`` for assertion.
    """

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.events: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        # Poke ourselves to unblock recv.
        try:
            poke = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            poke.sendto(b"\x00", ("127.0.0.1", self.port))
            poke.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)
        self.sock.close()

    def _loop(self):
        self.sock.settimeout(0.3)
        while not self._stop.is_set():
            try:
                data, _ = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if data == b"\x00":
                continue
            try:
                self.events.append(json.loads(data.decode("utf-8")))
            except Exception:
                pass

    def of_kind(self, name: str) -> list[dict]:
        return [e for e in self.events
                if e.get("evt") == name or e.get("name") == name
                or e.get("event") == name]


def _events_named(events: list[dict], name: str) -> list[dict]:
    """Best-effort filter for events of a given name across event-schema
    variants.  The chirp emitter uses 'evt' as the event-name field."""
    out = []
    for e in events:
        ename = e.get("evt") or e.get("event") or e.get("name")
        if ename == name:
            out.append(e)
    return out


# ---------------------------------------------------------------------------
# fixture: 4-slot daemon, file source, configurable scheduler
# ---------------------------------------------------------------------------


@pytest.fixture
def daemon_factory(tmp_path):
    """Build a daemon with configurable scheduler dwell / cap.

    Returns a callable; the caller picks lo_dwell_sec / lo_max_clusters /
    max_channels so we can construct different scenarios per-test.

    Carries an _EventListener so tests can assert on cluster_hop /
    scheduler_plan_failed events without subscribe/unsubscribe ceremony.
    """
    # Synthetic IQ — single carrier at +200 kHz.  Channels can be placed
    # anywhere in the planner's coordinate space; for the LO-scheduler
    # tests we don't actually need RF detection, just the state machine.
    samp_rate = 1e6
    iq_path = tmp_path / "scheduler.iq"
    n = int(samp_rate * 5.0)
    t = np.arange(n, dtype=np.float64) / samp_rate
    env = 0.5 * (1.0 + 0.8 * np.sin(2 * np.pi * 1000 * t))
    iq = (env * np.exp(2j * np.pi * 200e3 * t)).astype(np.complex64)
    rng = np.random.default_rng(7)
    noise = (rng.normal(0, 0.002, n) + 1j * rng.normal(0, 0.002, n)).astype(np.complex64)
    (iq + noise).tofile(iq_path)

    teardown: list = []

    def _make(*, lo_dwell_sec: float = 0.5,
              lo_max_clusters: int = 3,
              max_channels: int = 8):
        listener = _EventListener().start()
        cfg = DaemonConfig(
            band="airband",
            cmd_port=18900 + (os.getpid() % 300),
            source_kind="file",
            source_path=str(iq_path),
            source_samp_rate=samp_rate,
            audio_out_kind="file",
            audio_out_path=str(tmp_path / "mix.f32"),
            audio_rate=16000.0,
            max_channels=max_channels,
            state_path=str(tmp_path / "x.state.json"),
            hit_log_path=str(tmp_path / "hits.jsonl"),
            event_sink=("127.0.0.1", listener.port),
            lo_dwell_sec=lo_dwell_sec,
            lo_max_clusters=lo_max_clusters,
        )
        server = CommandServer(
            ServerConfig(host=cfg.cmd_host, port=cfg.cmd_port,
                         event_sink=cfg.event_sink),
            dispatch=lambda env, args: tb.dispatch(env, args),
        )
        tb = ChirpFlowgraph(cfg, server, state_store=StateStore(cfg.state_path))
        tb.start()
        tb.start_health()
        server.start()
        teardown.append((tb, server, listener))
        return cfg, tb, server, listener

    yield _make

    for tb, server, listener in teardown:
        try:
            tb.stop_health()
            tb.shutdown_drain()
            tb.stop()
            tb.wait()
            server.stop()
            listener.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def _add_channels(port: int, channels: list[dict]) -> dict:
    body = json.dumps({
        "v": 1, "id": "a", "cmd": "add_channel",
        "args": {"channels": channels},
    }).encode()
    return json.loads(_udp_send(port, body).decode())


def _get_status(port: int) -> dict:
    body = json.dumps({"v": 1, "id": "s", "cmd": "get_status", "args": {}}).encode()
    return json.loads(_udp_send(port, body).decode())


class TestPhase4PreDaemon:

    def test_get_status_includes_lo_scheduler_block_when_empty(self, daemon_factory):
        cfg, _tb, _srv, _ = daemon_factory(lo_dwell_sec=0.5)
        resp = _get_status(cfg.cmd_port)
        assert resp["status"] == "ok"
        sched = resp["data"]["lo_scheduler"]
        assert sched["n_clusters"] == 0
        assert sched["current_cluster_center_hz"] is None
        assert sched["dwell_s"] == pytest.approx(0.5)
        assert sched["max_clusters"] == 3

    def test_single_channel_no_rotation(self, daemon_factory):
        """Single-channel pool MUST NOT rotate.  This is the
        regression guard against the small-channel-list path."""
        cfg, _tb, _srv, listener = daemon_factory(lo_dwell_sec=0.3)
        _add_channels(cfg.cmd_port, [
            {"id": "solo", "freq_mhz": 121.5, "mode": "am", "squelch_dbfs": -85.0},
        ])
        time.sleep(1.0)  # would allow ~3 hops at 0.3s dwell
        resp = _get_status(cfg.cmd_port)
        sched = resp["data"]["lo_scheduler"]
        assert sched["n_clusters"] == 1
        assert sched["single_cluster"] is True
        assert sched["current_cluster_center_hz"] == pytest.approx(121.5e6)
        # Exactly one cluster_hop event (the initial apply).
        hops = _events_named(listener.events, "cluster_hop")
        assert len(hops) == 1, f"expected 1 hop, got {len(hops)}: {hops}"

    def test_multi_cluster_rotates_and_parks(self, daemon_factory):
        """Two clusters, 4 channels.  Scheduler rotates between them on
        the dwell.  is_parked flips per hop in get_status."""
        cfg, _tb, _srv, listener = daemon_factory(lo_dwell_sec=0.4)
        # Cluster A: 121.0, 121.5 (center 121.25).
        # Cluster B: 135.0, 135.5 (center 135.25).
        resp = _add_channels(cfg.cmd_port, [
            {"id": "a", "freq_mhz": 121.0, "mode": "am", "squelch_dbfs": -85.0},
            {"id": "b", "freq_mhz": 121.5, "mode": "am", "squelch_dbfs": -85.0},
            {"id": "c", "freq_mhz": 135.0, "mode": "am", "squelch_dbfs": -85.0},
            {"id": "d", "freq_mhz": 135.5, "mode": "am", "squelch_dbfs": -85.0},
        ])
        assert resp["status"] == "ok"
        # Give the scheduler thread a tick (≤0.25s) to apply cluster 0.
        time.sleep(0.3)
        st = _get_status(cfg.cmd_port)["data"]
        sched = st["lo_scheduler"]
        assert sched["n_clusters"] == 2
        # Which cluster is current — depends on the planner's ordering
        # (sorted by center_hz, so cluster 0 is the LOW one).
        assert sched["current_cluster_idx"] == 0
        assert sched["current_cluster_center_hz"] == pytest.approx(121.25e6)
        assert set(sched["live_channel_ids"]) == {"a", "b"}
        assert set(sched["parked_channel_ids"]) == {"c", "d"}
        # is_parked on per-channel data reflects this.
        by_id = {c["id"]: c for c in st["channels"]}
        assert by_id["a"]["is_parked"] is False
        assert by_id["b"]["is_parked"] is False
        assert by_id["c"]["is_parked"] is True
        assert by_id["d"]["is_parked"] is True

        # Wait for one full dwell to rotate to cluster 1.
        time.sleep(0.6)
        st = _get_status(cfg.cmd_port)["data"]
        sched = st["lo_scheduler"]
        assert sched["current_cluster_idx"] == 1
        assert sched["current_cluster_center_hz"] == pytest.approx(135.25e6)
        assert set(sched["live_channel_ids"]) == {"c", "d"}
        assert set(sched["parked_channel_ids"]) == {"a", "b"}
        by_id = {c["id"]: c for c in st["channels"]}
        assert by_id["a"]["is_parked"] is True
        assert by_id["d"]["is_parked"] is False

        # At least 2 cluster_hop events observed (initial + at least 1
        # rotation).  The exact count depends on the 0.5s wait above
        # vs the 0.4s dwell, but we should see ≥ 2.
        hops = _events_named(listener.events, "cluster_hop")
        assert len(hops) >= 2, f"got {len(hops)} hops"
        # The 2nd hop has from_center_hz != None.
        non_initial = [h for h in hops if h.get("from_center_hz") is not None]
        assert non_initial, "no actual cluster_hop with from_center"
        assert non_initial[0]["from_center_hz"] in (
            pytest.approx(121.25e6), pytest.approx(135.25e6),
        )

    def test_add_channel_mid_dwell_invalidates_plan(self, daemon_factory):
        cfg, _tb, _srv, _ = daemon_factory(lo_dwell_sec=2.0)
        _add_channels(cfg.cmd_port, [
            {"id": "a", "freq_mhz": 121.0, "mode": "am", "squelch_dbfs": -85.0},
        ])
        time.sleep(0.3)
        sched1 = _get_status(cfg.cmd_port)["data"]["lo_scheduler"]
        assert sched1["n_clusters"] == 1
        # Add a far-away channel mid-dwell.
        _add_channels(cfg.cmd_port, [
            {"id": "b", "freq_mhz": 200.0, "mode": "am", "squelch_dbfs": -85.0},
        ])
        time.sleep(0.4)  # scheduler tick (~0.25 s) + a bit
        sched2 = _get_status(cfg.cmd_port)["data"]["lo_scheduler"]
        assert sched2["n_clusters"] == 2

    def test_plan_failed_parks_every_channel(self, daemon_factory):
        cfg, _tb, _srv, listener = daemon_factory(
            lo_dwell_sec=0.4, lo_max_clusters=1,
        )
        # 3 channels 5 MHz apart → needs 3 clusters at 1 MHz IQ window.
        # max_clusters=1 forces failure.
        _add_channels(cfg.cmd_port, [
            {"id": "a", "freq_mhz": 121.0, "mode": "am", "squelch_dbfs": -85.0},
            {"id": "b", "freq_mhz": 126.0, "mode": "am", "squelch_dbfs": -85.0},
            {"id": "c", "freq_mhz": 131.0, "mode": "am", "squelch_dbfs": -85.0},
        ])
        time.sleep(0.4)
        st = _get_status(cfg.cmd_port)["data"]
        sched = st["lo_scheduler"]
        assert sched["n_clusters"] == 0
        assert sched["plan_failed_reason"] is not None
        assert sched["plan_needed"] >= 3
        # All channels parked.
        by_id = {c["id"]: c for c in st["channels"]}
        assert all(v["is_parked"] for v in by_id.values()), by_id
        # scheduler_plan_failed event emitted.
        failed = _events_named(listener.events, "scheduler_plan_failed")
        assert failed, listener.events
        assert failed[0]["needed"] >= 3
        assert failed[0]["max_allowed"] == 1

    def test_remove_channel_parks_it(self, daemon_factory):
        cfg, _tb, _srv, _ = daemon_factory(lo_dwell_sec=0.4)
        _add_channels(cfg.cmd_port, [
            {"id": "a", "freq_mhz": 121.0, "mode": "am", "squelch_dbfs": -85.0},
            {"id": "b", "freq_mhz": 135.0, "mode": "am", "squelch_dbfs": -85.0},
        ])
        time.sleep(0.3)
        # Remove 'b'.
        body = json.dumps({
            "v": 1, "id": "rm", "cmd": "remove_channel",
            "args": {"id": "b"},
        }).encode()
        resp = json.loads(_udp_send(cfg.cmd_port, body).decode())
        assert resp["status"] == "ok"
        time.sleep(0.4)
        st = _get_status(cfg.cmd_port)["data"]
        sched = st["lo_scheduler"]
        assert sched["n_clusters"] == 1
        assert "b" not in [c["id"] for c in st["channels"]]
        # The remaining channel is live.
        by_id = {c["id"]: c for c in st["channels"]}
        assert by_id["a"]["is_parked"] is False

    def test_protocol_status_includes_dwell_remaining(self, daemon_factory):
        cfg, _tb, _srv, _ = daemon_factory(lo_dwell_sec=1.0)
        _add_channels(cfg.cmd_port, [
            {"id": "a", "freq_mhz": 121.0, "mode": "am", "squelch_dbfs": -85.0},
            {"id": "c", "freq_mhz": 135.0, "mode": "am", "squelch_dbfs": -85.0},
        ])
        time.sleep(0.3)
        sched = _get_status(cfg.cmd_port)["data"]["lo_scheduler"]
        # dwell_remaining_sec is in (0, 1.0]
        assert sched["dwell_remaining_sec"] is not None
        assert 0.0 < sched["dwell_remaining_sec"] <= 1.0
        # clusters payload has the per-cluster shape we ship to the
        # dashboard.
        c0 = sched["clusters"][0]
        for k in ("center_hz", "center_mhz", "channel_ids", "priority",
                  "min_freq_hz", "max_freq_hz", "span_hz", "n_channels"):
            assert k in c0

    def test_no_hits_fired_on_parked_channels(self, daemon_factory, tmp_path):
        """Phase 4-pre integrity guard.

        Two clusters, slow dwell.  Each channel sits at the center of
        its own cluster.  The invariant we test is the SCHEDULER
        CORRELATION:

          * Every hit_start carries ``cluster_center_hz``.
          * For every hit, the channel's freq lies WITHIN the active
            cluster's IQ window.  Equivalently: a channel never fires
            a hit while a different cluster is active (because the
            channel is parked then).
          * No hit_start fires BEFORE the first cluster_hop event
            (race-window guard — channels start parked at claim time).
        """
        cfg, _tb, _srv, listener = daemon_factory(lo_dwell_sec=0.4)
        _add_channels(cfg.cmd_port, [
            {"id": "a", "freq_mhz": 0.2, "mode": "am", "squelch_dbfs": -85.0},
            {"id": "c", "freq_mhz": 5.0, "mode": "am", "squelch_dbfs": -85.0},
        ])
        # Wait through several hops at iq_bw=1 MHz.
        time.sleep(2.0)

        starts = _events_named(listener.events, "hit_start")
        hops = _events_named(listener.events, "cluster_hop")
        assert starts, "no hit_starts observed — test is not exercising the path"
        assert hops, "no cluster_hops observed — scheduler not running"

        # Race guard: the FIRST cluster_hop happens before any
        # hit_start (channels start parked, scheduler unparks the
        # live cluster, then hits flow).
        first_hop_ts = min(h["ts"] for h in hops)
        first_hit_ts = min(s["ts"] for s in starts)
        assert first_hit_ts >= first_hop_ts, (
            f"hit fired BEFORE first cluster_hop: hit={first_hit_ts} "
            f"hop={first_hop_ts}"
        )

        # Every hit_start carries cluster_center_hz (scheduler wired).
        assert all("cluster_center_hz" in e for e in starts), starts

        # Correlation invariant: channel freq is within ±iq_bw/2 of the
        # tagged cluster center.  iq_bw_hz=1 MHz for this fixture.
        iq_half = 0.5e6
        for ev in starts:
            ch_freq_hz = ev["freq_mhz"] * 1e6
            ctr = ev["cluster_center_hz"]
            assert abs(ch_freq_hz - ctr) <= iq_half + 1.0, (
                f"hit on ch={ev['ch']} freq={ev['freq_mhz']} MHz tagged "
                f"with cluster_center={ctr/1e6} MHz — outside IQ window"
            )

        # Stronger: the channel's hits are ONLY tagged with its own
        # cluster's center (proves parking actually silenced it during
        # the other cluster's dwell).
        a_centers = {ev["cluster_center_hz"] for ev in starts if ev["ch"] == "a"}
        c_centers = {ev["cluster_center_hz"] for ev in starts if ev["ch"] == "c"}
        assert a_centers <= {200000.0}, (
            f"'a' fired hits tagged with cluster centers it doesn't "
            f"belong to: {a_centers}"
        )
        assert c_centers <= {5000000.0}, (
            f"'c' fired hits tagged with cluster centers it doesn't "
            f"belong to: {c_centers}"
        )
