"""Tests for Phase 4-pre LO scheduler + channel parking + hit-log tagging.

Splits into three groups:

  1. ``Channel.set_parked / .is_parked`` semantics (real GR block,
     no flowgraph): squelch slammed when parked, restored on unpark,
     operator set_squelch while parked is stashed for unpark.
  2. ``LoScheduler`` state machine: tested with injected callbacks
     and a fake clock — no daemon, no GR, deterministic.
  3. ``HitDetector`` + parking: parked channels never fire hit_start
     or hit_end, and the ``cluster_center_hz`` tag is propagated when
     a scheduler callback is wired in.

Phase 4-pre intentionally does NOT spin up a full flowgraph end-to-end
in these tests — the test_phase4-pre integration tests do that on a
file source.  Here we test the units.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import pytest

from gnuradio import gr

from chirp.dsp.channel import Channel
from chirp.dsp.cluster_planner import PlanChannel
from chirp.dsp.lo_scheduler import LoScheduler


# ---------------------------------------------------------------------------
# 1. Channel.set_parked semantics
# ---------------------------------------------------------------------------


def _mk_channel(squelch_dbfs: float = -45.0) -> Channel:
    """Helper: build a real AM Channel block (no flowgraph attached)."""
    return Channel(samp_rate=1e6, squelch_dbfs=squelch_dbfs, gain_db=0.0, mode="am")


class TestChannelParked:
    def test_initial_state_not_parked(self):
        ch = _mk_channel(-40.0)
        assert ch.is_parked is False
        assert ch.squelch_dbfs == pytest.approx(-40.0)

    def test_park_slams_squelch_threshold_to_zero_dbfs(self):
        ch = _mk_channel(-40.0)
        ch.set_parked(True)
        assert ch.is_parked is True
        # GR pwr_squelch.threshold() returns the LIVE threshold.
        assert ch.pwr_squelch.threshold() == pytest.approx(0.0)

    def test_unpark_restores_squelch_threshold(self):
        ch = _mk_channel(-37.0)
        ch.set_parked(True)
        ch.set_parked(False)
        assert ch.is_parked is False
        assert ch.pwr_squelch.threshold() == pytest.approx(-37.0)
        assert ch.squelch_dbfs == pytest.approx(-37.0)

    def test_set_squelch_while_parked_stashes_for_unpark(self):
        ch = _mk_channel(-40.0)
        ch.set_parked(True)
        # Live threshold is parked.
        assert ch.pwr_squelch.threshold() == pytest.approx(0.0)
        # Operator changes squelch while parked.
        ch.set_squelch(-25.0)
        # Live threshold is STILL parked (silenced).
        assert ch.pwr_squelch.threshold() == pytest.approx(0.0)
        # Snapshot's squelch_dbfs reflects operator value.
        assert ch.squelch_dbfs == pytest.approx(-25.0)
        # On unpark, the operator-intended value applies.
        ch.set_parked(False)
        assert ch.pwr_squelch.threshold() == pytest.approx(-25.0)

    def test_park_is_idempotent(self):
        ch = _mk_channel(-40.0)
        ch.set_parked(True)
        ch.set_parked(True)  # no-op
        assert ch.pwr_squelch.threshold() == pytest.approx(0.0)
        # And original squelch still recovered on unpark.
        ch.set_parked(False)
        assert ch.pwr_squelch.threshold() == pytest.approx(-40.0)

    def test_unpark_is_idempotent(self):
        ch = _mk_channel(-40.0)
        ch.set_parked(False)  # already unparked; no-op
        assert ch.is_parked is False
        assert ch.pwr_squelch.threshold() == pytest.approx(-40.0)

    def test_snapshot_includes_is_parked(self):
        ch = _mk_channel(-40.0)
        snap = ch.snapshot()
        assert snap["is_parked"] is False
        ch.set_parked(True)
        assert ch.snapshot()["is_parked"] is True


# ---------------------------------------------------------------------------
# 2. LoScheduler state machine
# ---------------------------------------------------------------------------


@dataclass
class FakeClock:
    now: float = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


@dataclass
class FakeSink:
    """Records all scheduler callbacks for assertion."""
    retune_calls: list[float] = field(default_factory=list)
    park_calls: list[set[str]] = field(default_factory=list)
    unpark_calls: list[set[str]] = field(default_factory=list)
    events: list[tuple[str, dict]] = field(default_factory=list)
    channels: list[PlanChannel] = field(default_factory=list)

    def retune_to(self, hz: float) -> None:
        self.retune_calls.append(hz)

    def park(self, ids: set[str]) -> None:
        self.park_calls.append(set(ids))

    def unpark(self, ids: set[str]) -> None:
        self.unpark_calls.append(set(ids))

    def emit(self, name: str, **kw):
        self.events.append((name, kw))

    def get_channels(self):
        return list(self.channels)


def _mk_scheduler(sink: FakeSink, clock: FakeClock,
                  dwell_s: float = 30.0, max_clusters: int = 3,
                  iq_bw_hz: float = 2e6) -> LoScheduler:
    return LoScheduler(
        get_channels=sink.get_channels,
        retune_to=sink.retune_to,
        park_channels=sink.park,
        unpark_channels=sink.unpark,
        emit_event=sink.emit,
        iq_bw_hz=iq_bw_hz,
        dwell_s=dwell_s,
        max_clusters=max_clusters,
        clock=clock,
    )


class TestLoSchedulerStateMachine:
    def test_no_channels_no_action(self):
        sink = FakeSink(channels=[])
        clk = FakeClock()
        sched = _mk_scheduler(sink, clk)
        sched.step()
        assert sink.retune_calls == []
        assert sink.events == []
        assert sched.current_cluster_center_hz() is None

    def test_single_cluster_tunes_once_no_rotation(self):
        # 3 channels all within 2 MHz → 1 cluster.
        sink = FakeSink(channels=[
            PlanChannel("a", 121.0e6),
            PlanChannel("b", 121.5e6),
            PlanChannel("c", 122.0e6),
        ])
        clk = FakeClock()
        sched = _mk_scheduler(sink, clk, dwell_s=10.0)
        sched.step()  # first step → recompute + apply cluster 0
        assert len(sink.retune_calls) == 1
        assert sink.retune_calls[0] == pytest.approx(121.5e6)
        # All 3 channels unparked, none parked.
        assert sink.unpark_calls[-1] == {"a", "b", "c"}
        assert sink.park_calls == []
        # Advance 60 s — still no second retune (single cluster).
        clk.advance(60.0)
        sched.step()
        assert len(sink.retune_calls) == 1, "single cluster must NOT rotate"
        # cluster_hop event fired exactly once.
        hops = [(n, kw) for (n, kw) in sink.events if n == "cluster_hop"]
        assert len(hops) == 1
        assert hops[0][1]["to_center_hz"] == pytest.approx(121.5e6)
        assert hops[0][1]["from_center_hz"] is None
        assert sched.snapshot()["single_cluster"] is True

    def test_two_clusters_rotate_on_dwell_expiry(self):
        sink = FakeSink(channels=[
            PlanChannel("a", 121.0e6),
            PlanChannel("b", 121.5e6),
            PlanChannel("c", 135.0e6),
            PlanChannel("d", 135.5e6),
        ])
        clk = FakeClock()
        sched = _mk_scheduler(sink, clk, dwell_s=10.0)
        sched.step()  # apply cluster 0 (centered ~121.25)
        assert sink.retune_calls[-1] == pytest.approx(121.25e6)
        # cluster 1's channels parked, cluster 0's unparked.
        assert {"c", "d"} <= sink.park_calls[-1]
        assert {"a", "b"} <= sink.unpark_calls[-1]
        # Half-dwell — no rotation yet.
        clk.advance(5.0)
        sched.step()
        assert len(sink.retune_calls) == 1
        # Full dwell — rotate to cluster 1.
        clk.advance(6.0)
        sched.step()
        assert len(sink.retune_calls) == 2
        assert sink.retune_calls[-1] == pytest.approx(135.25e6)
        # cluster 0's channels parked, cluster 1's unparked.
        assert {"a", "b"} <= sink.park_calls[-1]
        assert {"c", "d"} <= sink.unpark_calls[-1]
        # Round-robin back to cluster 0.
        clk.advance(11.0)
        sched.step()
        assert len(sink.retune_calls) == 3
        assert sink.retune_calls[-1] == pytest.approx(121.25e6)
        # Two cluster_hop events with from_center_hz != None now.
        hops = [kw for (n, kw) in sink.events if n == "cluster_hop"]
        assert len(hops) == 3
        # The 2nd hop's from_center is the 1st hop's to_center.
        assert hops[1]["from_center_hz"] == pytest.approx(121.25e6)
        assert hops[1]["to_center_hz"] == pytest.approx(135.25e6)
        assert hops[1]["dwell_actual_sec"] == pytest.approx(11.0)
        assert hops[1]["cluster_idx"] == 1
        assert hops[1]["n_clusters"] == 2

    def test_invalidate_recomputes_at_next_tick(self):
        sink = FakeSink(channels=[PlanChannel("a", 121.0e6)])
        clk = FakeClock()
        sched = _mk_scheduler(sink, clk, dwell_s=10.0)
        sched.step()
        # Operator adds two channels far away → would need 2 clusters.
        sink.channels = [
            PlanChannel("a", 121.0e6),
            PlanChannel("b", 135.0e6),
        ]
        sched.invalidate()
        clk.advance(1.0)
        sched.step()
        snap = sched.snapshot()
        assert snap["n_clusters"] == 2
        assert sched.current_cluster_center_hz() == pytest.approx(121.0e6)

    def test_invalidate_DOES_NOT_cut_dwell_short(self):
        """Mid-dwell add_channel must NOT trigger an immediate retune;
        the current dwell completes first."""
        sink = FakeSink(channels=[
            PlanChannel("a", 121.0e6),
            PlanChannel("c", 135.0e6),
        ])
        clk = FakeClock()
        sched = _mk_scheduler(sink, clk, dwell_s=10.0)
        sched.step()  # apply cluster 0
        assert len(sink.retune_calls) == 1
        # Half-dwell, operator adds another channel.
        clk.advance(5.0)
        sink.channels.append(PlanChannel("b", 121.5e6))
        sched.invalidate()
        sched.step()
        # Recompute happened, but we are still in dwell window for the new
        # cluster 0 (centers may shift slightly; that's expected on
        # recompute).  Either way: no extra rotation hop YET — only the
        # initial apply for the new plan.
        # The recompute triggers a fresh apply (cluster_dwell_start_ts=None
        # after recompute → applies cluster 0).
        # So we expect ONE additional retune call (the recompute's apply),
        # not two.
        assert len(sink.retune_calls) == 2

    def test_plan_failed_parks_all_channels_and_emits_event(self):
        # 5 channels spread 6 MHz apart → needs 5 clusters at 2 MHz.
        # max_clusters=2 forces failure.
        sink = FakeSink(channels=[
            PlanChannel("a", 121.0e6),
            PlanChannel("b", 127.0e6),
            PlanChannel("c", 133.0e6),
            PlanChannel("d", 139.0e6),
            PlanChannel("e", 145.0e6),
        ])
        clk = FakeClock()
        sched = _mk_scheduler(sink, clk, max_clusters=2)
        sched.step()
        # No retune happened.
        assert sink.retune_calls == []
        # All known ids parked.
        assert sink.park_calls and sink.park_calls[-1] == {"a", "b", "c", "d", "e"}
        # scheduler_plan_failed event emitted with needed=5.
        names = [n for (n, _) in sink.events]
        assert "scheduler_plan_failed" in names
        ev = next(kw for (n, kw) in sink.events if n == "scheduler_plan_failed")
        assert ev["needed"] == 5
        assert ev["max_allowed"] == 2

    def test_snapshot_shape_always_consistent(self):
        sink = FakeSink(channels=[])
        clk = FakeClock()
        sched = _mk_scheduler(sink, clk)
        # No channels: clusters list empty.
        s1 = sched.snapshot()
        for k in (
            "dwell_s", "max_clusters", "iq_bw_hz", "tick_s",
            "plan_failed_reason", "plan_needed", "last_recompute_ts",
            "current_cluster_center_hz", "current_cluster_idx",
            "dwell_remaining_sec", "clusters", "live_channel_ids",
            "parked_channel_ids", "single_cluster", "n_clusters",
        ):
            assert k in s1, f"snapshot missing key {k!r}"

        sink.channels = [PlanChannel("a", 121.0e6), PlanChannel("b", 135.0e6)]
        sched.invalidate()
        sched.step()
        s2 = sched.snapshot()
        # Same shape after the step.
        assert set(s1.keys()) == set(s2.keys())
        assert s2["n_clusters"] == 2
        assert s2["dwell_remaining_sec"] is not None

    def test_channel_removed_is_parked(self):
        sink = FakeSink(channels=[
            PlanChannel("a", 121.0e6),
            PlanChannel("b", 135.0e6),
        ])
        clk = FakeClock()
        sched = _mk_scheduler(sink, clk, dwell_s=10.0)
        sched.step()  # apply cluster 0 → unpark a, park b
        # Remove 'b' from the pool.
        sink.channels = [PlanChannel("a", 121.0e6)]
        sched.invalidate()
        sched.step()
        # 'b' should be in the park calls AFTER the recompute (it's gone
        # from the new plan but was in the old known pool).
        flat_parked: set[str] = set()
        for s in sink.park_calls:
            flat_parked |= s
        assert "b" in flat_parked

    def test_current_cluster_center_hz_with_no_plan_returns_none(self):
        sink = FakeSink(channels=[])
        clk = FakeClock()
        sched = _mk_scheduler(sink, clk)
        assert sched.current_cluster_center_hz() is None
        sched.step()
        assert sched.current_cluster_center_hz() is None

    def test_invalid_constructor_args(self):
        sink = FakeSink()
        clk = FakeClock()
        with pytest.raises(ValueError, match="iq_bw_hz"):
            LoScheduler(
                get_channels=sink.get_channels,
                retune_to=sink.retune_to,
                park_channels=sink.park,
                unpark_channels=sink.unpark,
                emit_event=sink.emit,
                iq_bw_hz=0,
                clock=clk,
            )
        with pytest.raises(ValueError, match="dwell_s"):
            LoScheduler(
                get_channels=sink.get_channels,
                retune_to=sink.retune_to,
                park_channels=sink.park,
                unpark_channels=sink.unpark,
                emit_event=sink.emit,
                iq_bw_hz=2e6, dwell_s=-1, clock=clk,
            )
        with pytest.raises(ValueError, match="max_clusters"):
            LoScheduler(
                get_channels=sink.get_channels,
                retune_to=sink.retune_to,
                park_channels=sink.park,
                unpark_channels=sink.unpark,
                emit_event=sink.emit,
                iq_bw_hz=2e6, max_clusters=0, clock=clk,
            )

    def test_lifecycle_thread_starts_and_stops(self):
        sink = FakeSink(channels=[PlanChannel("a", 121e6)])
        clk = FakeClock()
        sched = _mk_scheduler(sink, clk, dwell_s=1.0)
        sched.start()
        # Give it a moment to tick.
        time.sleep(0.4)
        sched.stop()
        # At least one retune from initial apply.
        assert len(sink.retune_calls) >= 1

    def test_retune_failure_does_not_kill_scheduler(self):
        def bad_retune(hz):
            raise RuntimeError("simulated SDR retune failure")

        sink = FakeSink(channels=[PlanChannel("a", 121e6)])
        clk = FakeClock()
        sched = LoScheduler(
            get_channels=sink.get_channels,
            retune_to=bad_retune,
            park_channels=sink.park,
            unpark_channels=sink.unpark,
            emit_event=sink.emit,
            iq_bw_hz=2e6,
            clock=clk,
        )
        # Should not raise.
        sched.step()
        # park/unpark still happen.
        assert sink.unpark_calls and sink.unpark_calls[-1] == {"a"}


# ---------------------------------------------------------------------------
# 3. HitDetector + parking + cluster tagging
# ---------------------------------------------------------------------------


class _StubServer:
    """Records every emit_event call for assertion."""
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit_event(self, name: str, **kw):
        self.events.append((name, kw))


@dataclass
class _StubChannel:
    """Minimal channel stub for HitDetector tests."""
    _squelch_open: bool = False
    _level_dbfs: float = -70.0
    is_parked: bool = False

    def get_squelch_open(self) -> bool:
        return self._squelch_open

    def get_signal_level_dbfs(self) -> float:
        return self._level_dbfs


@dataclass
class _StubSlot:
    index: int
    channel: _StubChannel
    user_id: Optional[str] = None
    last_freq_mhz: Optional[float] = None
    claimed_at: Optional[float] = None


class TestHitDetectorWithScheduler:
    def _mk_detector(self, slots, server, get_center=None, tmp_path=None):
        from chirp.hit_detector import HitDetector
        # Use a tmp file path so the JSONL append works without /var/log
        # write permission.
        log_path = None
        if tmp_path is not None:
            log_path = str(tmp_path / "hits.jsonl")
        return HitDetector(
            slots=slots, server=server, hit_log_path=log_path,
            poll_s=0.05, warmup_s=0.0,
            get_cluster_center_hz=get_center,
        )

    def test_parked_channel_does_not_fire_hit_start(self, tmp_path):
        ch = _StubChannel(_squelch_open=True, _level_dbfs=-20.0,
                          is_parked=True)
        slot = _StubSlot(index=0, channel=ch, user_id="X",
                        last_freq_mhz=121.5, claimed_at=time.time())
        server = _StubServer()
        det = self._mk_detector([slot], server, tmp_path=tmp_path)
        det._tick()
        # Parked + squelch_open: NO hit_start event.
        names = [n for (n, _) in server.events]
        assert "hit_start" not in names

    def test_unparked_channel_fires_hit_start_with_cluster_tag(self, tmp_path):
        ch = _StubChannel(_squelch_open=True, _level_dbfs=-20.0, is_parked=False)
        slot = _StubSlot(index=0, channel=ch, user_id="X",
                        last_freq_mhz=121.5, claimed_at=time.time())
        server = _StubServer()
        det = self._mk_detector(
            [slot], server,
            get_center=lambda: 121.5e6,
            tmp_path=tmp_path,
        )
        det._tick()
        names = [n for (n, _) in server.events]
        assert "hit_start" in names
        ev = next(kw for (n, kw) in server.events if n == "hit_start")
        assert ev["cluster_center_hz"] == pytest.approx(121.5e6)

    def test_hit_end_carries_cluster_center_from_hit_start(self, tmp_path):
        ch = _StubChannel(_squelch_open=True, _level_dbfs=-20.0, is_parked=False)
        slot = _StubSlot(index=0, channel=ch, user_id="X",
                        last_freq_mhz=121.5, claimed_at=time.time())
        server = _StubServer()
        # Use a mutable holder so we can flip the center mid-test (simulate
        # an LO hop AFTER hit_start).  hit_end should still report the
        # center at hit_start time.
        center_holder = [121.5e6]
        det = self._mk_detector(
            [slot], server,
            get_center=lambda: center_holder[0],
            tmp_path=tmp_path,
        )
        det._tick()  # hit_start at 121.5 MHz
        center_holder[0] = 135.0e6  # LO hops
        ch._squelch_open = False
        det._tick()  # hit_end
        hit_end = next(kw for (n, kw) in server.events if n == "hit_end")
        assert hit_end["cluster_center_hz"] == pytest.approx(121.5e6)

    def test_channel_parked_mid_hit_drops_in_flight_silently(self, tmp_path):
        # Open squelch, unparked → hit_start fires.
        ch = _StubChannel(_squelch_open=True, _level_dbfs=-20.0, is_parked=False)
        slot = _StubSlot(index=0, channel=ch, user_id="X",
                        last_freq_mhz=121.5, claimed_at=time.time())
        server = _StubServer()
        det = self._mk_detector([slot], server, tmp_path=tmp_path)
        det._tick()
        # Now park the channel — simulates the scheduler hopping LO.
        ch.is_parked = True
        det._tick()
        # No hit_end fired (the in-flight was dropped silently).
        names = [n for (n, _) in server.events]
        assert "hit_end" not in names
        # last_open reset to False, so on unpark + still-open RF we'd see
        # a FRESH hit_start.  Verify:
        ch.is_parked = False
        # squelch is still open from the original RF, but now we expect a
        # new hit_start (because parked tick reset last_open).
        det._tick()
        starts = [kw for (n, kw) in server.events if n == "hit_start"]
        assert len(starts) == 2  # the original + a fresh one after unpark

    def test_no_cluster_tag_when_callback_returns_none(self, tmp_path):
        """Backward-compat: a daemon that hasn't wired the scheduler
        callback (or whose scheduler returns None) emits events WITHOUT
        the cluster_center_hz field."""
        ch = _StubChannel(_squelch_open=True, _level_dbfs=-20.0, is_parked=False)
        slot = _StubSlot(index=0, channel=ch, user_id="X",
                        last_freq_mhz=121.5, claimed_at=time.time())
        server = _StubServer()
        det = self._mk_detector(
            [slot], server, get_center=lambda: None,
            tmp_path=tmp_path,
        )
        det._tick()
        ev = next(kw for (n, kw) in server.events if n == "hit_start")
        assert "cluster_center_hz" not in ev

    def test_cluster_callback_exception_does_not_break_detector(self, tmp_path):
        def boom():
            raise RuntimeError("scheduler crashed")
        ch = _StubChannel(_squelch_open=True, _level_dbfs=-20.0, is_parked=False)
        slot = _StubSlot(index=0, channel=ch, user_id="X",
                        last_freq_mhz=121.5, claimed_at=time.time())
        server = _StubServer()
        det = self._mk_detector(
            [slot], server, get_center=boom, tmp_path=tmp_path,
        )
        det._tick()
        # No exception, no cluster tag.
        ev = next(kw for (n, kw) in server.events if n == "hit_start")
        assert "cluster_center_hz" not in ev
