"""SB6 (2026-06-18) — audio-flow watchdog probe regression suite.

Covers the "telemetry-lies" wedge that bit twice on 2026-06-18: a chirp daemon
reporting ``audio_path_health: live`` + climbing ``bytes_sent`` while the mount
published EXACT -180 dBFS digital silence.  The fix taps PCM amplitude at the
mount-publish layer and withholds the systemd ``WATCHDOG=1`` ping when the path
claims to be live yet no real audio has flowed for a grace window.

Two layers of test, mirroring the Phase 2 hard-fail pattern:

  1. Pure-logic tests of ``AudioFlowProbe`` (no gnuradio import — runs anywhere)
     driving a fake monotonic clock.  This is the regression core: it replays
     both wedge episodes and the legitimate-quiet cases that must NOT regress.
  2. Source-level wiring assertions on daemon.py / icecast_sink.py, in the
     style of test_sdrplay_wedge_fix.py (which also can't import the gnuradio
     modules), proving the probe is actually wired into the work() tap, the
     main loop gates on it, and the env rollback exists + defaults OFF.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chirp.audio_probe import (
    AudioFlowProbe,
    DEFAULT_SILENCE_EPS,
    DEFAULT_SILENCE_GRACE_S,
    DEFAULT_BLOCK_STALL_S,
)


# ---------------------------------------------------------------------------
# Fake clock — deterministic monotonic source
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += float(dt)


GRACE = 10.0
GRACE_INT = int(GRACE)
NONSILENT = 0.5  # a real audio peak, well above eps
SILENT = 0.0     # the -180 dBFS wedge


def _probe(clock: FakeClock, enabled: bool = True, block_stall_s: float = None) -> AudioFlowProbe:
    # Tests that only exercise the silence_grace_s (health=="live") mechanism
    # get a huge block_stall_s by default so the two independent ceilings
    # never coincidentally interact — the block-stall behavior is covered
    # explicitly by TestFlowgraphStall below.
    return AudioFlowProbe(
        enabled=enabled, silence_grace_s=GRACE,
        silence_eps=DEFAULT_SILENCE_EPS,
        block_stall_s=1e9 if block_stall_s is None else block_stall_s,
        monotonic=clock,
    )


# ---------------------------------------------------------------------------
# 1. Probe logic
# ---------------------------------------------------------------------------


class TestProbeDisabled:
    """Default state: probe off → behave exactly like the old unconditional
    ping. evaluate() is always (True, ...) and observe_peak is a no-op."""

    def test_disabled_always_pings_even_when_live_and_silent(self):
        clock = FakeClock()
        p = _probe(clock, enabled=False)
        clock.advance(10 * GRACE)
        ok, reason = p.evaluate(clock(), "live")
        assert ok is True
        assert reason == "probe_disabled"

    def test_disabled_observe_is_noop(self):
        clock = FakeClock()
        p = _probe(clock, enabled=False)
        p.observe_peak(NONSILENT)
        snap = p.snapshot()
        assert snap["blocks_observed"] == 0


class TestSilenceExpected:
    """health != "live" means nothing is keyed / all parked / all muted →
    silence is EXPECTED and the watchdog must keep pinging no matter how long
    it stays quiet — AS LONG AS THE FLOWGRAPH IS STILL ALIVE (blocks keep
    arriving, silent or not; see TestFlowgraphStall for the case where blocks
    stop arriving entirely, which is a different, always-a-wedge condition).
    This is the "don't regress legitimate quiet" guarantee."""

    @pytest.mark.parametrize("health", ["no_open", "no_live", "all_muted", "unknown"])
    def test_non_live_health_always_pings(self, health):
        clock = FakeClock()
        p = _probe(clock)
        # Never any flow, sit silent for ages.
        clock.advance(100 * GRACE)
        ok, reason = p.evaluate(clock(), health)
        assert ok is True
        assert "flow_ok" in reason


class TestLiveFlowing:
    def test_live_with_recent_flow_pings(self):
        clock = FakeClock()
        p = _probe(clock)
        p.observe_peak(NONSILENT)          # audio is flowing
        clock.advance(2.0)                  # < grace
        ok, reason = p.evaluate(clock(), "live")
        assert ok is True
        assert "flow_ok" in reason

    def test_eps_boundary(self):
        clock = FakeClock()
        p = _probe(clock)
        p.observe_peak(DEFAULT_SILENCE_EPS * 0.5)   # below eps = silent
        assert p.snapshot()["nonsilent_blocks"] == 0
        p.observe_peak(DEFAULT_SILENCE_EPS * 2.0)    # above eps = flow
        assert p.snapshot()["nonsilent_blocks"] == 1


class TestWedgeDetection:
    """THE regression. Both 2026-06-18 episodes: path says live, audio is
    exact-zero, watchdog must be withheld so systemd restarts."""

    def test_live_silent_beyond_grace_withholds_watchdog(self):
        clock = FakeClock()
        p = _probe(clock)
        # Audio was flowing, then the branch wedges (only silent blocks now).
        p.observe_peak(NONSILENT)
        # Establish the live spell.
        ok, _ = p.evaluate(clock(), "live")
        assert ok is True
        # Branch feeds only silence; time marches past the grace window.
        for _ in range(int(GRACE // 1) + 2):
            clock.advance(1.0)
            p.observe_peak(SILENT)
        ok, reason = p.evaluate(clock(), "live")
        assert ok is False, "wedge must withhold WATCHDOG so systemd restarts"
        assert "audio_branch_silent" in reason

    def test_cold_start_never_flowed_is_a_wedge(self):
        """Afternoon-ground case: branch silently wedged since the migration —
        it NEVER produced audio, yet the channel is live. Must be flagged."""
        clock = FakeClock()
        p = _probe(clock)
        # First eval starts the live timer (live_for=0) → still pings.
        ok, _ = p.evaluate(clock(), "live")
        assert ok is True
        # Only silence ever; advance past grace and re-eval.
        for _ in range(int(GRACE) + 2):
            clock.advance(1.0)
            p.observe_peak(SILENT)
        ok, reason = p.evaluate(clock(), "live")
        assert ok is False
        assert "audio_branch_silent" in reason

    def test_recovery_resumes_pings(self):
        clock = FakeClock()
        p = _probe(clock)
        p.evaluate(clock(), "live")
        clock.advance(2 * GRACE)
        assert p.evaluate(clock(), "live")[0] is False     # wedged
        # Audio comes back.
        p.observe_peak(NONSILENT)
        ok, reason = p.evaluate(clock(), "live")
        assert ok is True
        assert "flow_ok" in reason


class TestFlowgraphStall:
    """2026-07-06 regression: the whole flowgraph stops ticking (GR scheduler
    blocks on a dead source) and hit_detector's own health computation freezes
    right along with it, at whatever value it last held. A frozen "no_open"
    is indistinguishable from a fresh one under the health-only rule above —
    this is the second, independent ceiling that catches it: zero blocks
    observed AT ALL (not just zero non-silent ones) for block_stall_s, checked
    BEFORE health is even consulted."""

    STALL = 5.0  # a small, explicit value distinct from GRACE for clarity

    def _probe(self, clock: FakeClock) -> AudioFlowProbe:
        return AudioFlowProbe(
            enabled=True, silence_grace_s=GRACE, silence_eps=DEFAULT_SILENCE_EPS,
            block_stall_s=self.STALL, monotonic=clock,
        )

    def test_default_constant_is_shorter_than_silence_grace_would_allow(self):
        # Sanity check on the shipped default: it must be short enough to
        # catch a dead flowgraph promptly, not just "eventually".
        assert DEFAULT_BLOCK_STALL_S < 60.0

    @pytest.mark.parametrize("health", ["no_open", "no_live", "all_muted", "live"])
    def test_zero_blocks_ever_is_flagged_regardless_of_health(self, health):
        """THE regression: a flowgraph that never ticks even once must be
        caught after block_stall_s, no matter what health claims — this is
        exactly the shape of the 2026-07-06 incident (over an HOUR of frozen
        blocks_observed went unflagged under the old health-only logic)."""
        clock = FakeClock()
        p = self._probe(clock)
        clock.advance(self.STALL + 1.0)
        ok, reason = p.evaluate(clock(), health)
        assert ok is False, "a flowgraph with zero blocks ever must be flagged as stalled"
        assert "flowgraph_stalled" in reason

    def test_silent_blocks_keep_flowing_with_no_open_health_never_stalls(self):
        """The legitimate case this must NOT regress: health=no_open (nothing
        currently keyed) but the mixer keeps feeding the sink silent blocks
        (the Phase-3 park-CPU topology contract) — the flowgraph is alive,
        just quiet. Must never be flagged, no matter how long this persists."""
        clock = FakeClock()
        p = self._probe(clock)
        for _ in range(50):
            clock.advance(1.0)
            p.observe_peak(SILENT)
            ok, reason = p.evaluate(clock(), "no_open")
            assert ok is True, f"silent-but-ticking flowgraph must not stall-wedge (t={clock()})"

    def test_stall_detected_before_health_branch_even_on_live(self):
        """A stalled flowgraph with health frozen at "live" (not just
        no_open) must ALSO be caught — the stall check runs before the
        health-based silence_grace_s logic, so it fires even faster than the
        pre-existing audio_branch_silent path would for a genuinely shorter
        block_stall_s."""
        clock = FakeClock()
        p = self._probe(clock)
        p.observe_peak(NONSILENT)
        p.evaluate(clock(), "live")
        clock.advance(self.STALL + 1.0)  # no more observe_peak calls at all
        ok, reason = p.evaluate(clock(), "live")
        assert ok is False
        assert "flowgraph_stalled" in reason

    def test_recovery_after_stall_resumes_pinging(self):
        clock = FakeClock()
        p = self._probe(clock)
        clock.advance(self.STALL + 1.0)
        assert p.evaluate(clock(), "no_open")[0] is False  # stalled
        # The flowgraph starts ticking again.
        p.observe_peak(SILENT)
        ok, reason = p.evaluate(clock(), "no_open")
        assert ok is True
        assert "flow_ok" in reason

    def test_snapshot_exposes_seconds_since_any_block(self):
        clock = FakeClock()
        p = self._probe(clock)
        p.observe_peak(SILENT)
        clock.advance(2.0)
        snap = p.snapshot()
        assert snap["seconds_since_any_block"] == 2.0
        assert snap["block_stall_s"] == self.STALL


class TestLiveForGuard:
    """The subtle correctness case: a long quiet period (squelch closed) leaves
    last_flow stale; when a NEW transmission opens (health flips to live) the
    gap-since-flow is already huge, but the path has only just gone live — it
    must NOT be flagged on the first evaluation, or every transmission after a
    quiet stretch would trip a false restart."""

    def test_long_quiet_then_new_tx_does_not_false_wedge(self):
        clock = FakeClock()
        p = _probe(clock)
        # A real TX, then squelch closes for 5 minutes (no_open → silent).
        p.observe_peak(NONSILENT)
        clock.advance(300.0)
        # New TX opens. gap-since-flow is 300 s (>> grace) but we just went live.
        ok, reason = p.evaluate(clock(), "live")
        assert ok is True, "first eval after going live must not wedge on stale gap"
        # Healthy branch now flows continuously (observe_peak every block).
        for _ in range(GRACE_INT + 5):
            clock.advance(1.0)
            p.observe_peak(NONSILENT)
        assert p.evaluate(clock(), "live")[0] is True

    def test_quiet_resets_live_timer(self):
        clock = FakeClock()
        p = _probe(clock)
        # Live + silent, accumulating toward a wedge.
        p.evaluate(clock(), "live")
        clock.advance(GRACE - 1)
        p.observe_peak(SILENT)
        assert p.evaluate(clock(), "live")[0] is True   # not yet wedged
        # Squelch closes (health no_open) — resets the live timer.
        p.evaluate(clock(), "no_open")
        clock.advance(GRACE - 1)
        # Re-opens: live_for restarts, so still under grace → ping.
        ok, _ = p.evaluate(clock(), "live")
        assert ok is True


class TestSnapshot:
    def test_snapshot_fields(self):
        clock = FakeClock()
        p = _probe(clock)
        p.observe_peak(NONSILENT)
        snap = p.snapshot()
        assert snap["enabled"] is True
        assert snap["silence_grace_s"] == GRACE
        assert snap["blocks_observed"] == 1
        assert snap["nonsilent_blocks"] == 1
        assert snap["seconds_since_flow"] == 0.0


# ---------------------------------------------------------------------------
# 2. Source-level wiring (no gnuradio import needed)
# ---------------------------------------------------------------------------


_REPO = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


class TestIcecastSinkTap:
    SINK = "chirp/dsp/icecast_sink.py"

    def test_sink_accepts_flow_probe(self):
        src = _read(self.SINK)
        assert "flow_probe" in src, "IcecastSink must accept a flow_probe"

    def test_work_observes_peak_before_encode(self):
        src = _read(self.SINK)
        # The probe must be fed inside work(), and BEFORE the int16 encode line
        # (so it sees the float PCM, where the wedge is distinguishable).
        work_pos = src.find("def work(")
        assert work_pos != -1
        observe_pos = src.find("observe_peak", work_pos)
        encode_pos = src.find("astype(np.int16)", work_pos)
        assert observe_pos != -1, "work() must call flow_probe.observe_peak"
        assert observe_pos < encode_pos, "peak must be measured on float PCM"


class TestDaemonWiring:
    DAEMON = "chirp/daemon.py"

    def test_config_field_defaults_off(self):
        src = _read(self.DAEMON)
        assert "audio_probe_enabled: bool = False" in src, \
            "probe must default OFF (env-var rollback safety)"

    def test_env_var_wired(self):
        src = _read(self.DAEMON)
        assert "CHIRP_AUDIO_PROBE_ENABLED" in src, \
            "load_config must read CHIRP_AUDIO_PROBE_ENABLED"
        assert "CHIRP_AUDIO_PROBE_SILENCE_GRACE_S" in src

    def test_probe_constructed_and_wired_to_sink(self):
        src = _read(self.DAEMON)
        assert "AudioFlowProbe(" in src
        assert "flow_probe=self.flow_probe" in src, \
            "probe must be handed to the IcecastSink"

    def test_main_loop_gates_watchdog(self):
        src = _read(self.DAEMON)
        loop_pos = src.find("while not stop_evt.is_set")
        finally_pos = src.find("finally:", loop_pos)
        loop_body = src[loop_pos:finally_pos]
        # The ping is still present (gated, not removed) ...
        assert "WATCHDOG=1" in loop_body
        # ... but now goes through the gate.
        assert "audio_watchdog_status" in loop_body, \
            "main loop must gate WATCHDOG=1 on audio_watchdog_status()"
        assert "audio_branch_silent" in loop_body, \
            "main loop must surface the wedge as an audio_branch_silent signal"

    def test_gate_fails_safe_on_health_read_error(self):
        src = _read(self.DAEMON)
        # audio_watchdog_status must return ping=True if it can't read health.
        fn_pos = src.find("def audio_watchdog_status(")
        assert fn_pos != -1
        snippet = src[fn_pos:fn_pos + 2000]
        assert "health_read_failed" in snippet, \
            "watchdog gate must fail SAFE (keep pinging) on introspection error"
