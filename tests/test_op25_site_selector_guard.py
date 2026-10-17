"""The selector's "is op25 ready to be judged?" guard.

The selector restarts an op25 that is running but stuck. Two failure modes
this guard exists to prevent:

- acting on an op25 an operator stopped on purpose: preflight() reads a
  stopped decoder as stale and would eventually restart it;
- judging a decoder that just (re)started: cold status reads as stale and
  would trigger a pointless site switch.

The unit used BindsTo=scanner-digital-op25 to get the first property by
accident, which also killed the selector on every op25 restart it made
itself -- it died on its first site switch and stayed dead for two days.
"""
from __future__ import annotations

import importlib.util
import sys
import time
import unittest
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "bin" / "op25-site-selector.py"
_spec = importlib.util.spec_from_file_location("op25_site_selector", _PATH)
sel = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sel
_spec.loader.exec_module(sel)

UNIT = "scanner-digital-op25"


def _active(value):
    return lambda unit: value


def _since(seconds_ago):
    return lambda unit: None if seconds_ago is None else time.time() - seconds_ago


def _raises(*_a, **_k):
    raise RuntimeError("systemctl unavailable")


class Op25ReadyTests(unittest.TestCase):
    def test_stopped_op25_is_never_evaluated(self):
        ready, why = sel._op25_ready(_active(False), _since(3600), UNIT)
        self.assertFalse(ready)
        self.assertIn("not active", why)

    def test_running_and_warm_is_evaluated(self):
        ready, why = sel._op25_ready(_active(True), _since(sel._WARMUP_SEC + 5), UNIT)
        self.assertTrue(ready)
        self.assertEqual(why, "")

    def test_just_restarted_waits_for_warmup(self):
        ready, why = sel._op25_ready(_active(True), _since(10), UNIT)
        self.assertFalse(ready)
        self.assertIn("warming up", why)

    def test_unknown_start_time_does_not_block(self):
        """None is the backend's 'don't know' -- evaluate rather than stall."""
        ready, _ = sel._op25_ready(_active(True), _since(None), UNIT)
        self.assertTrue(ready)

    def test_cannot_query_systemd_falls_back_to_evaluating(self):
        """If the check itself fails, behave as before it existed."""
        ready, _ = sel._op25_ready(_raises, _since(3600), UNIT)
        self.assertTrue(ready)

    def test_start_time_query_failure_does_not_block(self):
        ready, _ = sel._op25_ready(_active(True), _raises, UNIT)
        self.assertTrue(ready)


class UnitFileTests(unittest.TestCase):
    UNIT_PATH = Path(__file__).resolve().parents[1] / "systemd" / "scanner-digital-op25-selector.service"

    def _directives(self):
        out = {}
        for line in self.UNIT_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out.setdefault(k.strip(), []).append(v.strip())
        return out

    def test_not_bound_to_op25(self):
        """BindsTo makes the selector's own op25 restarts kill it."""
        d = self._directives()
        self.assertNotIn("BindsTo", d)
        self.assertNotIn("PartOf", d)

    def test_still_ordered_after_and_wants_op25(self):
        d = self._directives()
        self.assertTrue(any("scanner-digital-op25.service" in v for v in d.get("After", [])))
        self.assertIn("scanner-digital-op25.service", d.get("Wants", []))

    def test_restarts_whenever_it_stops_unasked(self):
        self.assertEqual(self._directives().get("Restart"), ["always"])


if __name__ == "__main__":
    unittest.main()
