"""Tests for scan-hold (LO latch-on-hit) and the priority gate (2026-06-06).

Scan-hold suspends the LoScheduler's cluster rotation while a TX is live on
the active cluster, releasing after a hang window (or a max-time cap).  The
priority gate passes exactly one open channel's audio at a time.

Both are tested as pure units: scheduler with injected callbacks + a fake
clock (no GR, no daemon); priority gate standalone.
"""

from __future__ import annotations

import pytest

from chirp.dsp.cluster_planner import PlanChannel
from chirp.dsp.lo_scheduler import LoScheduler
from chirp.dsp.priority_gate import PriorityGate


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


class Sink:
    """Records callbacks; ``open`` set controls is_open() for scan-hold."""

    def __init__(self, channels):
        self.channels = list(channels)
        self.events: list[tuple[str, dict]] = []
        self.retunes: list[float] = []
        self.open: set[str] = set()

    def get_channels(self):
        return list(self.channels)

    def retune(self, hz):
        self.retunes.append(hz)

    def park(self, ids):
        pass

    def unpark(self, ids):
        pass

    def emit(self, name, **kw):
        self.events.append((name, kw))

    def is_open(self, cid):
        return cid in self.open

    # helpers
    def names(self, name):
        return [kw for (n, kw) in self.events if n == name]

    def hops(self):
        return self.names("cluster_hop")


# Two clusters: 'a' @121 MHz and 'b' @130 MHz are >2 MHz apart → 2 clusters.
TWO_CLUSTER = [PlanChannel("a", 121.0e6), PlanChannel("b", 130.0e6)]


def _mk(sink, clk, *, enabled=True, hang=2.0, max_s=30.0, dwell=10.0):
    return LoScheduler(
        get_channels=sink.get_channels,
        retune_to=sink.retune,
        park_channels=sink.park,
        unpark_channels=sink.unpark,
        emit_event=sink.emit,
        iq_bw_hz=2e6,
        dwell_s=dwell,
        max_clusters=3,
        clock=clk,
        is_open=sink.is_open,
        scan_hold_enabled=enabled,
        scan_hold_hang_sec=hang,
        scan_hold_max_sec=max_s,
    )


def _prime(sink, clk):
    """First step: recompute + apply cluster 0.  Returns the live channel id."""
    sched = _mk(sink, clk)
    sched.step()
    live = sched.snapshot()["live_channel_ids"]
    assert len(live) >= 1
    return sched, live[0]


class TestScanHold:
    def test_engages_on_open_and_suppresses_hop(self):
        sink = Sink(TWO_CLUSTER)
        clk = FakeClock()
        sched, live = _prime(sink, clk)
        hops_before = len(sink.hops())
        sink.open = {live}
        clk.advance(20.0)  # well past dwell
        sched.step()
        assert sched.snapshot()["scan_hold_state"] == "holding"
        assert len(sink.hops()) == hops_before, "must NOT hop while held"
        eng = sink.names("scan_hold_engaged")
        assert len(eng) == 1 and eng[0]["channel_id"] == live

    def test_holds_through_short_blip_under_hang(self):
        sink = Sink(TWO_CLUSTER)
        clk = FakeClock()
        sched, live = _prime(sink, clk)
        sink.open = {live}
        clk.advance(5.0)
        sched.step()  # engage
        sink.open = set()  # close
        clk.advance(1.0)  # < hang (2s)
        sched.step()
        assert sched.snapshot()["scan_hold_state"] == "holding"
        assert sink.names("scan_hold_released") == []

    def test_releases_after_hang_then_advances(self):
        sink = Sink(TWO_CLUSTER)
        clk = FakeClock()
        sched, live = _prime(sink, clk)
        hops_before = len(sink.hops())
        sink.open = {live}
        clk.advance(5.0)
        sched.step()  # engage
        sink.open = set()
        clk.advance(2.5)  # > hang
        sched.step()  # release + advance
        rel = sink.names("scan_hold_released")
        assert len(rel) == 1 and rel[0]["released_reason"] == "hang"
        assert len(sink.hops()) == hops_before + 1, "must advance on release"
        assert sched.snapshot()["scan_hold_state"] == "scanning"

    def test_reopen_extends_and_bumps_counter(self):
        sink = Sink(TWO_CLUSTER)
        clk = FakeClock()
        sched, live = _prime(sink, clk)
        sink.open = {live}
        clk.advance(1.0)
        sched.step()  # engage (extended=0)
        sink.open = set()
        clk.advance(1.0)  # within hang
        sched.step()  # still holding
        sink.open = {live}  # re-open
        clk.advance(1.0)
        sched.step()  # closed->open transition extends
        assert sched.snapshot()["hold_extended_n_times"] == 1
        # close it out and confirm released carries the count
        sink.open = set()
        clk.advance(3.0)
        sched.step()
        rel = sink.names("scan_hold_released")[-1]
        assert rel["extended_n_times"] == 1

    def test_new_channel_in_cluster_extends(self):
        # cluster A has two channels within 2 MHz; B is far.
        sink = Sink([
            PlanChannel("a1", 121.0e6),
            PlanChannel("a2", 121.4e6),
            PlanChannel("b", 130.0e6),
        ])
        clk = FakeClock()
        sched = _mk(sink, clk)
        sched.step()
        live = set(sched.snapshot()["live_channel_ids"])
        assert {"a1", "a2"} <= live
        sink.open = {"a1"}
        clk.advance(1.0)
        sched.step()  # engage on a1
        sink.open = {"a1", "a2"}  # a2 also opens
        clk.advance(1.0)
        sched.step()
        assert sched.snapshot()["hold_extended_n_times"] == 1

    def test_max_cap_force_release_and_advance(self):
        sink = Sink(TWO_CLUSTER)
        clk = FakeClock()
        sched, live = _prime(sink, clk)
        hops_before = len(sink.hops())
        sink.open = {live}
        clk.advance(1.0)
        sched.step()  # engage
        clk.advance(31.0)  # exceed max_sec while still open
        sched.step()
        rel = sink.names("scan_hold_released")
        assert len(rel) == 1 and rel[0]["released_reason"] == "max_cap"
        assert len(sink.hops()) == hops_before + 1

    def test_disabled_rotates_normally(self):
        sink = Sink(TWO_CLUSTER)
        clk = FakeClock()
        sched = _mk(sink, clk, enabled=False)
        sched.step()
        hops_before = len(sink.hops())
        sink.open = {sched.snapshot()["live_channel_ids"][0]}  # open ignored
        clk.advance(11.0)
        sched.step()
        assert len(sink.hops()) == hops_before + 1, "disabled must rotate on dwell"
        assert sink.names("scan_hold_engaged") == []

    def test_open_on_other_cluster_does_not_hold(self):
        sink = Sink(TWO_CLUSTER)
        clk = FakeClock()
        sched, live = _prime(sink, clk)
        # Open the channel NOT in the active cluster (parked in reality).
        other = "b" if live == "a" else "a"
        hops_before = len(sink.hops())
        sink.open = {other}
        clk.advance(11.0)
        sched.step()
        assert len(sink.hops()) == hops_before + 1, "off-cluster open must not hold"
        assert sink.names("scan_hold_engaged") == []


class TestPriorityGate:
    def test_first_open_selected(self):
        g = PriorityGate()
        assert g.update(["A"], 1.0) == "A"

    def test_latch_holds_until_close(self):
        g = PriorityGate()
        g.update(["A"], 1.0)
        # B opens while A live → A keeps the path (latch).
        assert g.update(["A", "B"], 2.0) == "A"
        assert g.update(["A", "B"], 3.0) == "A"

    def test_handoff_on_close_most_recent(self):
        g = PriorityGate()
        g.update(["A"], 1.0)
        g.update(["A", "B"], 2.0)  # B opens (more recent)
        g.update(["A", "B", "C"], 3.0)  # C opens (most recent), A still holds
        # A closes → most-recently-opened among {B, C} = C takes over.
        assert g.update(["B", "C"], 4.0) == "C"

    def test_all_closed_clears(self):
        g = PriorityGate()
        g.update(["A"], 1.0)
        assert g.update([], 2.0) is None

    def test_priority_locked_emitted_on_claim(self):
        ev: list[tuple[str, dict]] = []
        g = PriorityGate(emit_event=lambda n, **k: ev.append((n, k)))
        g.update(["A"], 1.0)
        g.update(["A"], 2.0)  # no re-emit while held
        g.update([], 3.0)
        g.update(["B"], 4.0)
        locks = [k["channel_id"] for n, k in ev if n == "priority_locked"]
        assert locks == ["A", "B"]

    def test_reset_clears_state(self):
        g = PriorityGate()
        g.update(["A"], 1.0)
        g.reset()
        assert g.selected is None
