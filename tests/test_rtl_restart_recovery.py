"""Tests for the rtl-airband sequenced-recovery restart path.

Background (2026-05-26)
-----------------------
The pre-refactor ``restart_rtl()`` was a bare ``systemctl restart`` with
no awareness of sdrplay daemon state and no post-start verification of
sample flow.  When the sdrplay daemon's internal state was corrupted by
a previous client wedge (the recurring failure mode), the restart would
reconnect to the same broken daemon and the new rtl_airband instance
would inherit the wedge — systemd reported "active" while no samples
flowed.

The refactor mirrors ``restart_digital()``'s proven pattern: gentle
attempt → escalate with forced sdrplay bounce on probe failure →
record telemetry → cap at ``RTL_WEDGE_RECOVERY_MAX_ATTEMPTS``.  These
tests pin the pieces that don't require spawning real subprocesses:
the stats-file polling probe and the state-recording bookkeeping.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest import mock

from ui import systemd as systemd_mod


def _touch(path: str, mtime_offset_sec: float = 0.0) -> None:
    target = time.time() + mtime_offset_sec
    os.utime(path, (target, target))


class WaitForRtlAirbandHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        fh = tempfile.NamedTemporaryFile(
            mode="w", suffix="_rtl_stats.txt", delete=False
        )
        fh.write("# rtl-airband stats\n")
        fh.close()
        self.stats_path = fh.name
        # Pin the module-level constants the probe reads from config.
        self._patches = [
            mock.patch.object(systemd_mod, "RTL_AIRBAND_STATS_PATH", self.stats_path),
            mock.patch.object(systemd_mod, "RTL_AIRBAND_STATS_STALE_SEC", 5.0),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        try:
            os.unlink(self.stats_path)
        except FileNotFoundError:
            pass

    def test_fresh_stats_probe_returns_ok_immediately(self) -> None:
        _touch(self.stats_path, mtime_offset_sec=-0.5)
        start = time.monotonic()
        ok, detail = systemd_mod._wait_for_rtl_airband_health(
            timeout_sec=10.0, poll_interval_sec=0.5
        )
        elapsed = time.monotonic() - start
        self.assertTrue(ok)
        self.assertIn("stats fresh", detail)
        # No polling needed — first check succeeds.
        self.assertLess(elapsed, 1.0)

    def test_stale_stats_probe_times_out_with_reason(self) -> None:
        _touch(self.stats_path, mtime_offset_sec=-60.0)
        start = time.monotonic()
        ok, detail = systemd_mod._wait_for_rtl_airband_health(
            timeout_sec=1.5, poll_interval_sec=0.5
        )
        elapsed = time.monotonic() - start
        self.assertFalse(ok)
        self.assertIn("timeout", detail)
        self.assertIn("stale", detail)
        # Should respect timeout — not return early.
        self.assertGreaterEqual(elapsed, 1.0)

    def test_probe_recovers_when_stats_freshen_mid_poll(self) -> None:
        # Start with stale stats; bump the mtime mid-poll to simulate
        # rtl_airband completing init and starting to write.
        _touch(self.stats_path, mtime_offset_sec=-60.0)
        real_mtime = [None]

        def freshen_after_first_call(*args, **kwargs):
            if real_mtime[0] is None:
                real_mtime[0] = "deferred"
                return os.stat(self.stats_path).st_mtime
            _touch(self.stats_path, mtime_offset_sec=-0.1)
            return os.stat(self.stats_path).st_mtime

        with mock.patch("os.path.getmtime", side_effect=freshen_after_first_call):
            ok, detail = systemd_mod._wait_for_rtl_airband_health(
                timeout_sec=5.0, poll_interval_sec=0.3
            )
        self.assertTrue(ok)
        self.assertIn("fresh", detail)

    def test_missing_stats_file_times_out(self) -> None:
        os.unlink(self.stats_path)
        ok, detail = systemd_mod._wait_for_rtl_airband_health(
            timeout_sec=1.0, poll_interval_sec=0.3
        )
        self.assertFalse(ok)
        self.assertIn("timeout", detail)


class RtlRestartStateTrackingTests(unittest.TestCase):
    def setUp(self) -> None:
        # Reset the state so other tests don't bleed in.
        with systemd_mod._RTL_RESTART_STATE_LOCK:
            for k in list(systemd_mod._RTL_RESTART_STATE.keys()):
                if isinstance(systemd_mod._RTL_RESTART_STATE[k], (int, float)):
                    systemd_mod._RTL_RESTART_STATE[k] = 0 if isinstance(
                        systemd_mod._RTL_RESTART_STATE[k], int
                    ) else 0.0
                else:
                    systemd_mod._RTL_RESTART_STATE[k] = ""

    def test_record_attempt_increments_counter_and_stores_reason(self) -> None:
        systemd_mod._record_rtl_restart_attempt("profile_switch")
        state = systemd_mod.rtl_restart_state()
        self.assertEqual(state["attempts_total"], 1)
        self.assertEqual(state["last_attempt_reason"], "profile_switch")
        self.assertGreater(state["last_attempt_ts"], 0.0)

    def test_record_attempt_normalizes_blank_reason(self) -> None:
        systemd_mod._record_rtl_restart_attempt("")
        state = systemd_mod.rtl_restart_state()
        self.assertEqual(state["last_attempt_reason"], "unspecified")

    def test_record_health_probe_round_trip(self) -> None:
        systemd_mod._record_rtl_health_probe("ok", "stats fresh (age 2.1s)")
        state = systemd_mod.rtl_restart_state()
        self.assertEqual(state["last_health_probe_result"], "ok")
        self.assertEqual(
            state["last_health_probe_detail"], "stats fresh (age 2.1s)"
        )
        self.assertGreater(state["last_health_probe_ts"], 0.0)

    def test_record_wedge_recovery_increments_total(self) -> None:
        systemd_mod._record_rtl_wedge_recovery()
        systemd_mod._record_rtl_wedge_recovery()
        state = systemd_mod.rtl_restart_state()
        self.assertEqual(state["wedge_recovery_total"], 2)
        self.assertGreater(state["last_wedge_recovery_ts"], 0.0)

    def test_state_snapshot_is_a_copy(self) -> None:
        # Mutating the returned dict must not affect the module state —
        # otherwise external consumers could corrupt our telemetry.
        systemd_mod._record_rtl_restart_attempt("first")
        snap = systemd_mod.rtl_restart_state()
        snap["attempts_total"] = 9999
        snap["last_attempt_reason"] = "tampered"
        fresh = systemd_mod.rtl_restart_state()
        self.assertEqual(fresh["attempts_total"], 1)
        self.assertEqual(fresh["last_attempt_reason"], "first")


class RestartRtlOrchestrationTests(unittest.TestCase):
    """Heavy mocks around restart_rtl() to verify the escalation flow.

    We stub out the subprocess-touching helpers and assert the call
    sequence — which prevents regressions like "restart_rtl forgot to
    bounce sdrplay during escalation" or "restart_rtl ran the probe
    before the rtl unit was even started".
    """

    def setUp(self) -> None:
        # Skip if an earlier test in the suite globally mocked
        # ui.systemd and didn't clean up — our patches would be no-ops
        # against the Mock and restart_rtl() would try to spawn real
        # subprocesses.  These tests pass cleanly in isolation; see
        # test_profile_validation_gate.py for the same workaround.
        from unittest.mock import Mock
        if isinstance(systemd_mod, Mock) or not callable(
            getattr(systemd_mod, "restart_rtl", None)
        ):
            self.skipTest(
                "ui.systemd is contaminated by an earlier test's global "
                "mock; run this module in isolation to verify."
            )

        # Capture an ordered log of mocked-helper invocations so the
        # tests can assert the exact sequence the orchestrator runs.
        self.calls: list = []
        self.patches = [
            mock.patch.object(
                systemd_mod, "_unit_configured",
                side_effect=lambda u: self.calls.append(("_unit_configured", u)) or True,
            ),
            mock.patch.object(
                systemd_mod, "_sdrplay_daemon_alive",
                side_effect=lambda: self.calls.append(("_sdrplay_daemon_alive",)) or True,
            ),
            mock.patch.object(
                systemd_mod, "_sdrplay_daemon_healthy",
                side_effect=lambda: self.calls.append(("_sdrplay_daemon_healthy",))
                or (True, "ok"),
            ),
            mock.patch.object(
                systemd_mod, "_stop_unit",
                side_effect=lambda u, use_sudo=False: self.calls.append(("_stop_unit", u)) or (True, ""),
            ),
            mock.patch.object(
                systemd_mod, "_kill_unit",
                side_effect=lambda u: self.calls.append(("_kill_unit", u)),
            ),
            mock.patch.object(
                systemd_mod, "_reset_failed_units",
                side_effect=lambda units: self.calls.append(("_reset_failed_units", tuple(units))),
            ),
            mock.patch.object(
                systemd_mod, "_restart_unit",
                side_effect=lambda u, use_sudo=False: self.calls.append(("_restart_unit", u)) or (True, ""),
            ),
            mock.patch.object(
                systemd_mod, "_start_unit",
                side_effect=lambda u, use_sudo=False: self.calls.append(("_start_unit", u)) or (True, ""),
            ),
            mock.patch.object(time, "sleep", side_effect=lambda s: None),
            mock.patch.dict(os.environ, {
                "RTL_RESTART_SDRPLAY_SETTLE_SEC": "0",
                "RTL_RESTART_SETTLE_SEC": "0",
                "RTL_POST_START_PROBE_POLL_SEC": "0.01",
                "RTL_POST_START_PROBE_TIMEOUT_SEC": "0.1",
                "RTL_WEDGE_RECOVERY_MAX_ATTEMPTS": "2",
            }),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()

    def _names(self) -> list:
        return [c[0] for c in self.calls]

    def test_gentle_attempt_with_passing_probe_does_not_escalate(self) -> None:
        # Sample-flow probe immediately returns ok — no sdrplay bounce,
        # no escalation, single attempt.
        with mock.patch.object(
            systemd_mod, "_wait_for_rtl_airband_health",
            return_value=(True, "stats fresh (age 0.5s)"),
        ):
            ok, err = systemd_mod.restart_rtl(reason="unit-test-gentle")
        self.assertTrue(ok)
        self.assertEqual(err, "")
        # Gentle path: stop → kill → reset → (no sdrplay bounce because
        # _sdrplay_daemon_alive said yes) → start → probe.  No
        # _restart_unit call to sdrplay.
        self.assertNotIn("_restart_unit", self._names())
        self.assertIn("_start_unit", self._names())

    def test_gentle_attempt_does_not_touch_op25(self) -> None:
        # On gentle (no escalation), OP25 must keep running — interrupting
        # digital audio for an attempt that may not even need a daemon
        # bounce is unjustified.
        with mock.patch.object(
            systemd_mod, "_wait_for_rtl_airband_health",
            return_value=(True, "stats fresh (age 0.5s)"),
        ), mock.patch.object(
            systemd_mod, "unit_active", return_value=True,
        ):
            systemd_mod.restart_rtl(reason="gentle-op25-untouched")
        digital_stops = [
            c for c in self.calls
            if c[0] == "_stop_unit" and "op25" in str(c[1] or "").lower()
        ]
        self.assertEqual(
            0, len(digital_stops),
            f"OP25 was stopped on a gentle attempt: {digital_stops}",
        )

    def test_probe_failure_triggers_sdrplay_bounce_on_escalation(self) -> None:
        # Probe fails twice (gentle + escalation-1), succeeds on
        # escalation-2.  Verify each escalation does a forced sdrplay
        # restart (visible as _restart_unit on the sdrplay unit name).
        probe_results = iter([
            (False, "timeout after 0.1s; stats stale"),  # gentle
            (False, "timeout after 0.1s; stats stale"),  # escalation-1
            (True, "stats fresh (age 0.3s)"),            # escalation-2
        ])
        with mock.patch.object(
            systemd_mod, "_wait_for_rtl_airband_health",
            side_effect=lambda **kw: next(probe_results),
        ), mock.patch.object(
            systemd_mod, "unit_active", return_value=True,
        ):
            ok, err = systemd_mod.restart_rtl(reason="unit-test-escalate")
        self.assertTrue(ok)
        # Escalation-1 and escalation-2 each forcibly restart sdrplay.
        sdrplay_restarts = [c for c in self.calls if c[0] == "_restart_unit"]
        self.assertEqual(
            len(sdrplay_restarts), 2,
            f"expected exactly 2 sdrplay bounces (one per escalation), saw "
            f"{len(sdrplay_restarts)} in {[c for c in self.calls if c[0] in ('_restart_unit', '_start_unit')]}"
        )

    def test_escalation_stops_op25_then_restarts_it_after_recovery(self) -> None:
        # B.5 regression guard.  Pre-B.5, escalation would bounce sdrplay
        # while OP25 was still connected; the daemon couldn't fully clear
        # its multi-tuner state and rtl-airband would re-wedge ~30s after
        # a probe-ok.  Fix is to stop OP25 before the daemon bounce and
        # restart it after rtl-airband's sample-flow probe passes.
        probe_results = iter([
            (False, "timeout; gentle failed"),   # gentle
            (True, "stats fresh (age 0.4s)"),    # escalation-1 passes
        ])
        with mock.patch.object(
            systemd_mod, "_wait_for_rtl_airband_health",
            side_effect=lambda **kw: next(probe_results),
        ), mock.patch.object(
            systemd_mod, "unit_active", return_value=True,
        ):
            ok, err = systemd_mod.restart_rtl(reason="b5-coordination")
        self.assertTrue(ok)
        # On escalation-1 we expect: stop op25 (digital unit) → bounce
        # sdrplay → start rtl-airband → probe ok → start op25.
        # Find indices for the relevant calls in order.
        def find_idx(predicate):
            for i, c in enumerate(self.calls):
                if predicate(c):
                    return i
            return -1
        # The digital unit defaults to "scanner-digital-op25" via UNITS,
        # but for the test we don't pin the exact name — we match any
        # _stop_unit / _start_unit on a non-rtl-airband unit during the
        # escalation phase.
        stops = [(i, c) for i, c in enumerate(self.calls) if c[0] == "_stop_unit"]
        starts = [(i, c) for i, c in enumerate(self.calls) if c[0] == "_start_unit"]
        sdrplay_restarts = [
            (i, c) for i, c in enumerate(self.calls)
            if c[0] == "_restart_unit"
        ]
        # Must have stopped OP25 (any non-rtl unit) before the sdrplay
        # daemon bounce.
        non_rtl_stops = [
            (i, c) for i, c in stops
            if "airband" not in str(c[1] or "").lower()
        ]
        self.assertTrue(
            non_rtl_stops,
            "escalation must stop OP25 before bouncing sdrplay daemon",
        )
        self.assertTrue(sdrplay_restarts, "escalation must restart sdrplay")
        first_op25_stop_idx = non_rtl_stops[0][0]
        first_sdrplay_idx = sdrplay_restarts[0][0]
        self.assertLess(
            first_op25_stop_idx, first_sdrplay_idx,
            "OP25 stop must happen BEFORE sdrplay daemon bounce",
        )
        # And after rtl-airband recovers, OP25 must be restarted.
        non_rtl_starts = [
            (i, c) for i, c in starts
            if "airband" not in str(c[1] or "").lower()
        ]
        self.assertTrue(
            non_rtl_starts,
            "escalation must restart OP25 after rtl-airband recovers",
        )

    def test_op25_not_stopped_if_already_inactive(self) -> None:
        # If OP25 is already down before restart_rtl runs (operator
        # disabled it, or it crashed), the escalation shouldn't try to
        # stop it — that would just generate a noisy systemctl warning
        # and the OP25 restart at the end would be wrong-step.
        probe_results = iter([
            (False, "timeout; gentle"),
            (True, "stats fresh (age 0.4s)"),
        ])
        with mock.patch.object(
            systemd_mod, "_wait_for_rtl_airband_health",
            side_effect=lambda **kw: next(probe_results),
        ), mock.patch.object(
            systemd_mod, "unit_active", return_value=False,
        ):
            ok, err = systemd_mod.restart_rtl(reason="op25-already-down")
        self.assertTrue(ok)
        # No _start_unit calls to a digital/op25-named unit should have
        # happened — restart_rtl shouldn't side-effect start OP25 if
        # we didn't stop it.
        non_rtl_starts = [
            c for c in self.calls
            if c[0] == "_start_unit"
            and "airband" not in str(c[1] or "").lower()
        ]
        self.assertEqual(
            [], non_rtl_starts,
            f"OP25 was started even though it was already down: {non_rtl_starts}",
        )

    def test_escalation_exhaustion_returns_failure(self) -> None:
        # Probe fails forever — gentle + 2 escalations all fail.
        with mock.patch.object(
            systemd_mod, "_wait_for_rtl_airband_health",
            return_value=(False, "timeout after 0.1s; stats stale"),
        ), mock.patch.object(
            systemd_mod, "unit_active", return_value=True,
        ):
            ok, err = systemd_mod.restart_rtl(reason="unit-test-exhaust")
        self.assertFalse(ok)
        self.assertIn("wedge recovery exhausted", err)
        self.assertIn("escalations", err)

    def test_probe_can_be_disabled_via_env(self) -> None:
        with mock.patch.dict(os.environ, {"RTL_POST_START_PROBE_ENABLED": "0"}):
            with mock.patch.object(
                systemd_mod, "_wait_for_rtl_airband_health",
            ) as mocked_probe:
                ok, err = systemd_mod.restart_rtl(reason="probe-disabled")
        self.assertTrue(ok)
        self.assertEqual(err, "")
        mocked_probe.assert_not_called()
        # Health-probe record should reflect skip.
        state = systemd_mod.rtl_restart_state()
        self.assertEqual(state["last_health_probe_result"], "skipped")


if __name__ == "__main__":
    unittest.main()
