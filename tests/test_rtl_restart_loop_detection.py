"""Regression tests for rtl-airband crash-loop visibility.

Bug context (2026-05-25)
------------------------
``/api/status`` lied about rtl-airband's health.  ``rtl_active`` is
derived from ``systemctl is-active``, which stays ``True`` through a
crash-loop: between each ``rtl_airband -F ... combined.conf`` exit and
the next ``Restart=on-failure`` start, the unit briefly toggles
inactive→active, and a polling reader almost always samples one of
those active windows.  An operator could see ``rtl_active: true``
while the underlying decoder was failing immediately every time it
started (e.g. ``LIBUSB_BUSY`` when ``acarsdec`` still held the dongle).

Fix: sample systemd's ``NRestarts`` on a sliding window.  When the
delta exceeds ``RTL_RESTART_LOOP_THRESHOLD`` over
``RTL_RESTART_LOOP_WINDOW_SEC``, surface ``rtl_restart_loop_detected``
in /api/status and force ``rtl_active`` / ``ground_active`` to False
so downstream readers (UI, alerting) can see the failure.
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

from ui import handlers


class RtlRestartLoopDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        # Fresh window per test.  The detector uses a module-level
        # ring buffer keyed by unit name; isolate by clearing.
        with handlers._CACHE_LOCK:
            handlers._UNIT_RESTART_SAMPLES.pop("rtl-airband.service", None)
        self.addCleanup(self._reset_ring)

    def _reset_ring(self) -> None:
        with handlers._CACHE_LOCK:
            handlers._UNIT_RESTART_SAMPLES.pop("rtl-airband.service", None)

    def test_stable_unit_does_not_flag_loop(self) -> None:
        # Same NRestarts across multiple polls → no loop.
        with mock.patch("ui.systemd.unit_restart_count", return_value=100):
            for _ in range(5):
                state = handlers._unit_restart_loop_state("rtl-airband.service")
                time.sleep(0.01)
        self.assertEqual(100, state["count"])
        self.assertEqual(0, state["window_restarts"])
        self.assertFalse(state["loop_detected"])

    def test_burst_of_restarts_within_window_flags_loop(self) -> None:
        # Simulate 5 restarts across multiple polls within the window.
        # Threshold default is 3; this should clear it.
        seq = iter([100, 101, 103, 105, 105])
        with mock.patch("ui.systemd.unit_restart_count", side_effect=lambda _u: next(seq)):
            state = None
            for _ in range(5):
                state = handlers._unit_restart_loop_state("rtl-airband.service")
        self.assertIsNotNone(state)
        self.assertEqual(105, state["count"])
        # Delta from oldest in-window sample (100) to newest (105) = 5
        self.assertEqual(5, state["window_restarts"])
        self.assertTrue(
            state["loop_detected"],
            f"5 restarts in window must flag loop; state={state!r}",
        )

    def test_restart_count_unavailable_yields_safe_payload(self) -> None:
        # systemd query failed → no count, no false-positive flag.
        with mock.patch("ui.systemd.unit_restart_count", return_value=None):
            state = handlers._unit_restart_loop_state("rtl-airband.service")
        self.assertIsNone(state["count"])
        self.assertFalse(state["loop_detected"])
        self.assertEqual(0, state["window_restarts"])

    def test_payload_exposes_window_and_threshold(self) -> None:
        with mock.patch("ui.systemd.unit_restart_count", return_value=42):
            state = handlers._unit_restart_loop_state("rtl-airband.service")
        self.assertEqual(handlers.RTL_RESTART_LOOP_WINDOW_SEC, state["window_sec"])
        self.assertEqual(handlers.RTL_RESTART_LOOP_THRESHOLD, state["threshold"])

    def test_threshold_just_below_does_not_flag(self) -> None:
        # Delta of (threshold - 1) restarts must not flag the loop.
        below = handlers.RTL_RESTART_LOOP_THRESHOLD - 1
        seq = iter([10, 10 + below])
        with mock.patch("ui.systemd.unit_restart_count", side_effect=lambda _u: next(seq)):
            handlers._unit_restart_loop_state("rtl-airband.service")
            state = handlers._unit_restart_loop_state("rtl-airband.service")
        self.assertEqual(below, state["window_restarts"])
        self.assertFalse(state["loop_detected"])


if __name__ == "__main__":
    unittest.main()
