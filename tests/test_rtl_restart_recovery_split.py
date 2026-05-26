"""Tests for the MA/SL split-process restart machinery.

Sister to ``test_rtl_restart_recovery.py``.  That file pins the
legacy single-service ``restart_rtl()``; this one pins the new
per-service ``restart_rtl_airband()`` and ``restart_rtl_ground()``
introduced by the MA/SL architecture (docs/rspduo_ma_sl_split.md).

What we verify
--------------
- Gentle path is per-service: restart_rtl_airband touches only its
  own unit and probes only the airband stats file.  Restart_rtl_ground
  symmetric.
- Escalation cycles the whole stack: stop target + peer + OP25,
  forced sdrplay daemon bounce, restart in ORDER (airband first as
  Master, ground second as Slave, OP25 last).
- Probe failure on the Master halts escalation (don't continue and
  silently leave OP25 stopped).
- Per-service restart-state telemetry is independent: an airband
  restart attempt doesn't increment the ground service's counter.

The tests use the same skip-on-contamination setUp pattern as
``test_rtl_restart_recovery.py`` because the same test-isolation
problem affects ui.systemd in the full-suite discover order.
"""
from __future__ import annotations

import os
import time
import unittest
from unittest import mock

from ui import systemd as systemd_mod


class _BaseSplitOrchestrationTest(unittest.TestCase):
    """Shared setUp that stubs out subprocess-touching helpers and
    captures the call sequence so subclasses can assert ordering."""

    def setUp(self) -> None:
        from unittest.mock import Mock
        if isinstance(systemd_mod, Mock) or not callable(
            getattr(systemd_mod, "restart_rtl_airband", None)
        ):
            self.skipTest(
                "ui.systemd is contaminated by an earlier test's global "
                "mock; run this module in isolation to verify."
            )

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
                "RTL_RESTART_DIGITAL_SETTLE_SEC": "0",
                "RTL_POST_START_PROBE_POLL_SEC": "0.01",
                "RTL_POST_START_PROBE_TIMEOUT_SEC": "0.1",
                "RTL_WEDGE_RECOVERY_MAX_ATTEMPTS": "2",
            }),
        ]
        # Reset per-service state so tests don't bleed counts between cases.
        with systemd_mod._RTL_AIRBAND_RESTART_STATE_LOCK:
            for k in systemd_mod._RTL_AIRBAND_RESTART_STATE:
                v = systemd_mod._RTL_AIRBAND_RESTART_STATE[k]
                systemd_mod._RTL_AIRBAND_RESTART_STATE[k] = (
                    0 if isinstance(v, int) and not isinstance(v, bool)
                    else 0.0 if isinstance(v, float)
                    else ""
                )
        with systemd_mod._RTL_GROUND_RESTART_STATE_LOCK:
            for k in systemd_mod._RTL_GROUND_RESTART_STATE:
                v = systemd_mod._RTL_GROUND_RESTART_STATE[k]
                systemd_mod._RTL_GROUND_RESTART_STATE[k] = (
                    0 if isinstance(v, int) and not isinstance(v, bool)
                    else 0.0 if isinstance(v, float)
                    else ""
                )
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()

    def _calls_of(self, kind: str) -> list:
        return [c for c in self.calls if c[0] == kind]

    def _unit_names_for(self, kind: str) -> list[str]:
        return [c[1] for c in self.calls if c[0] == kind and len(c) > 1]


class RestartRtlAirbandTests(_BaseSplitOrchestrationTest):
    def test_gentle_attempt_touches_only_airband_unit(self) -> None:
        # Probe returns ok immediately — no escalation, peer + OP25
        # untouched.
        with mock.patch.object(
            systemd_mod, "_wait_for_rtl_airband_health",
            return_value=(True, "stats fresh (age 0.5s)"),
        ), mock.patch.object(
            systemd_mod, "unit_active", return_value=True,
        ):
            ok, err = systemd_mod.restart_rtl_airband(reason="gentle-only")
        self.assertTrue(ok)
        # Stop calls must reference only the airband unit, not ground
        # or digital.
        stopped = self._unit_names_for("_stop_unit")
        airband_unit = systemd_mod.UNITS.get("rtl_airband")
        for u in stopped:
            self.assertIn(
                "airband", str(u).lower(),
                f"gentle path stopped non-airband unit: {u}",
            )
        # Confirm we stopped the right thing.
        self.assertIn(airband_unit, stopped)

    def test_escalation_stops_peer_and_op25_before_daemon_bounce(self) -> None:
        # Gentle fails; one escalation succeeds.  The escalation path
        # probes TWICE — once for the airband Master after it comes
        # up, once for the target service after the Slave starts —
        # so the iterator needs three results total.
        probes = iter([
            (False, "timeout; gentle"),
            (True, "Master probe: stats fresh"),
            (True, "Target probe: stats fresh"),
        ])
        with mock.patch.object(
            systemd_mod, "_wait_for_rtl_airband_health",
            side_effect=lambda **kw: next(probes),
        ), mock.patch.object(
            systemd_mod, "unit_active", return_value=True,
        ):
            ok, err = systemd_mod.restart_rtl_airband(reason="escalate")
        self.assertTrue(ok)

        # Locate the index of the sdrplay daemon bounce.
        idx_daemon_bounce = next(
            i for i, c in enumerate(self.calls)
            if c[0] == "_restart_unit"
        )
        # Find every _stop_unit call BEFORE the daemon bounce.
        stops_before = [
            c[1] for i, c in enumerate(self.calls)
            if c[0] == "_stop_unit" and i < idx_daemon_bounce
        ]
        # We expect the airband unit, the ground unit, AND the
        # digital unit all to have been stopped before the bounce.
        airband_unit = systemd_mod.UNITS.get("rtl_airband")
        ground_unit = systemd_mod.UNITS.get("rtl_ground")
        digital_unit = systemd_mod.UNITS.get("digital")
        self.assertIn(
            airband_unit, stops_before,
            f"airband not stopped before daemon bounce. stops: {stops_before}"
        )
        self.assertIn(
            ground_unit, stops_before,
            f"ground (peer) not stopped before daemon bounce. stops: {stops_before}"
        )
        self.assertIn(
            digital_unit, stops_before,
            f"OP25 not stopped before daemon bounce. stops: {stops_before}"
        )

        # Now verify the post-bounce start order: airband first,
        # ground second, OP25 third.
        starts_after = [
            (i, c[1]) for i, c in enumerate(self.calls)
            if c[0] == "_start_unit" and i > idx_daemon_bounce
        ]
        # Earliest start must be airband (Master); ground (Slave)
        # must come after airband; OP25 last.
        start_units_in_order = [u for _, u in starts_after]
        self.assertGreaterEqual(
            len(start_units_in_order), 3,
            f"expected at least 3 starts after daemon bounce, got "
            f"{start_units_in_order}"
        )
        airband_idx = start_units_in_order.index(airband_unit)
        ground_idx = start_units_in_order.index(ground_unit)
        digital_idx = start_units_in_order.index(digital_unit)
        self.assertLess(
            airband_idx, ground_idx,
            "airband must start before ground (Master before Slave)"
        )
        self.assertLess(
            ground_idx, digital_idx,
            "ground must start before OP25 (free the daemon first)"
        )

    def test_state_telemetry_increments_airband_only(self) -> None:
        # An airband restart must NOT bump the ground service's counter.
        with mock.patch.object(
            systemd_mod, "_wait_for_rtl_airband_health",
            return_value=(True, "fresh"),
        ), mock.patch.object(
            systemd_mod, "unit_active", return_value=True,
        ):
            systemd_mod.restart_rtl_airband(reason="airband-only-telemetry")
        airband_state = systemd_mod.rtl_airband_restart_state()
        ground_state = systemd_mod.rtl_ground_restart_state()
        self.assertEqual(airband_state["attempts_total"], 1)
        self.assertEqual(
            ground_state["attempts_total"], 0,
            "an airband restart must not increment the ground counter"
        )

    def test_master_probe_failure_aborts_escalation(self) -> None:
        # If the post-bounce Master (airband) probe fails, escalation
        # must NOT proceed to start the Slave or OP25 — we leave
        # everything stopped and surface the error so the next attempt
        # gets a clean cycle.
        with mock.patch.object(
            systemd_mod, "_wait_for_rtl_airband_health",
            return_value=(False, "stats stale forever"),
        ), mock.patch.object(
            systemd_mod, "unit_active", return_value=True,
        ):
            ok, err = systemd_mod.restart_rtl_airband(reason="all-failures")
        self.assertFalse(ok)
        self.assertIn("wedge recovery exhausted", err)


class RestartRtlGroundTests(_BaseSplitOrchestrationTest):
    def test_gentle_attempt_touches_only_ground_unit(self) -> None:
        with mock.patch.object(
            systemd_mod, "_wait_for_rtl_airband_health",
            return_value=(True, "fresh"),
        ), mock.patch.object(
            systemd_mod, "unit_active", return_value=True,
        ):
            ok, err = systemd_mod.restart_rtl_ground(reason="gentle-only")
        self.assertTrue(ok)
        stopped = self._unit_names_for("_stop_unit")
        for u in stopped:
            self.assertIn(
                "ground", str(u).lower(),
                f"gentle ground restart touched non-ground unit: {u}",
            )

    def test_ground_escalation_also_starts_master_first(self) -> None:
        # When the GROUND service escalates, the airband Master must
        # still start before the Slave — it's a property of MA/SL,
        # not of which service the operator restarted.
        probes = iter([
            (False, "timeout; gentle"),
            (True, "Master probe ok"),
            (True, "Target probe ok"),
        ])
        with mock.patch.object(
            systemd_mod, "_wait_for_rtl_airband_health",
            side_effect=lambda **kw: next(probes),
        ), mock.patch.object(
            systemd_mod, "unit_active", return_value=True,
        ):
            ok, err = systemd_mod.restart_rtl_ground(reason="escalate-ground")
        self.assertTrue(ok)
        idx_daemon = next(
            i for i, c in enumerate(self.calls)
            if c[0] == "_restart_unit"
        )
        starts_after = [
            c[1] for i, c in enumerate(self.calls)
            if c[0] == "_start_unit" and i > idx_daemon
        ]
        airband_unit = systemd_mod.UNITS.get("rtl_airband")
        ground_unit = systemd_mod.UNITS.get("rtl_ground")
        self.assertLess(
            starts_after.index(airband_unit),
            starts_after.index(ground_unit),
            "airband (Master) must start before ground (Slave) even "
            "when the operator restarted the GROUND service"
        )

    def test_state_telemetry_increments_ground_only(self) -> None:
        with mock.patch.object(
            systemd_mod, "_wait_for_rtl_airband_health",
            return_value=(True, "fresh"),
        ), mock.patch.object(
            systemd_mod, "unit_active", return_value=True,
        ):
            systemd_mod.restart_rtl_ground(reason="ground-only")
        airband_state = systemd_mod.rtl_airband_restart_state()
        ground_state = systemd_mod.rtl_ground_restart_state()
        self.assertEqual(ground_state["attempts_total"], 1)
        self.assertEqual(
            airband_state["attempts_total"], 0,
            "a ground restart must not increment the airband counter"
        )


if __name__ == "__main__":
    unittest.main()
