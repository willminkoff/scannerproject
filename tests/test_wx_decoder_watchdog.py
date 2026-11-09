"""The watchdog must never leave the box with no decoder, and never fight a handoff.

com.scannerproject.radiosonde ships RunAtLoad=false AND KeepAlive=false, so
when auto_rx dies nothing restarts it -- and ACARS does not return either,
because the handoff booted it out of the launchd domain, which is exactly what
stops its own KeepAlive from recovering it. A crash at 3am meant no ACARS, no
radiosonde, and no sign anything had happened.

The watchdog's contract:
  * requested decoder running  -> do nothing (and clear any strikes)
  * one miss                   -> wait; a handoff takes 10-30 s and the
                                  interval is 60 s, so acting on one miss would
                                  interrupt a switch still in progress
  * two misses, radiosonde     -> retry it, up to MAX_SONDE_RESTARTS, so a
                                  transient failure does not silently end an
                                  overnight session
  * budget spent               -> restore ACARS, the KeepAlive=true baseline,
                                  and rewrite intent so it stops retrying
"""
from __future__ import annotations

import importlib.util
import time as _real_time
import types
import unittest
from pathlib import Path

_WD = Path(__file__).resolve().parents[1] / "macos" / "bin" / "wx-decoder-watchdog.py"


def _load():
    spec = importlib.util.spec_from_file_location("wx_watchdog", _WD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Harness:
    """Run main() with every side effect captured instead of performed."""

    def __init__(self, intent, states, state=None, start_fails=()):
        self.mod = _load()
        self.intent = intent
        self.states = dict(states)
        self.state = dict(state or {})
        self.started = []
        self.kicks = 0
        self.intent_writes = []
        self.state_writes = []
        self.logs = []
        self._fails = set(start_fails)
        m = self.mod
        m.read_intent = lambda: self.intent
        m.service_states = lambda: self.states
        m.read_state = lambda: self.state
        m.write_state = lambda d: self.state_writes.append(dict(d))
        m.write_intent = lambda t: self.intent_writes.append(t)
        m.log = lambda msg: self.logs.append(msg)
        m.kick_sdrplay = self._kick
        m.start_service = self._start
        # Rebind the NAME in the watchdog's globals -- do NOT do
        # `m.time.sleep = ...`, which mutates the shared `time` module and
        # silently breaks every other test in the process that sleeps.
        m.time = types.SimpleNamespace(
            sleep=lambda *_: None,
            time=_real_time.time,
            strftime=_real_time.strftime,
        )

    def _kick(self):
        self.kicks += 1

    def _start(self, label):
        self.started.append(label)
        return label not in self._fails

    def run(self):
        return self.mod.main()

    @property
    def last_state(self):
        return self.state_writes[-1] if self.state_writes else None


class ContractTests(unittest.TestCase):
    ACARS = "com.scannerproject.acarsdec"
    VDL2 = "com.scannerproject.dumpvdl2"
    SONDE = "com.scannerproject.radiosonde"

    def test_nothing_happens_when_the_request_is_satisfied(self):
        h = _Harness("acars", {self.ACARS: "running", self.VDL2: "running"})
        h.run()
        self.assertEqual(h.started, [])
        self.assertEqual(h.kicks, 0)
        self.assertEqual(h.intent_writes, [])

    def test_radiosonde_running_is_left_alone(self):
        h = _Harness("radiosonde", {self.SONDE: "running"})
        h.run()
        self.assertEqual(h.started, [])

    def test_loaded_but_not_running_counts_as_down(self):
        """The whole bug family: 'loaded' is not 'running'."""
        h = _Harness("radiosonde", {self.SONDE: "loaded"})
        h.run()
        self.assertEqual(h.last_state["strikes"], 1)

    def test_one_miss_does_not_act(self):
        """A handoff in flight must not be interrupted."""
        h = _Harness("radiosonde", {self.SONDE: "absent"})
        h.run()
        self.assertEqual(h.started, [], "acted on a single miss")
        self.assertEqual(h.kicks, 0)
        self.assertEqual(h.last_state["strikes"], 1)

    def test_second_miss_retries_radiosonde(self):
        h = _Harness("radiosonde", {self.SONDE: "absent"}, state={"strikes": 1})
        h.run()
        self.assertEqual(h.started, [self.SONDE])
        self.assertEqual(h.last_state["sonde_restarts"], 1)
        self.assertEqual(h.intent_writes, [], "must not abandon the session yet")

    def test_retry_budget_is_finite_then_falls_back_to_acars(self):
        h = _Harness("radiosonde", {self.SONDE: "absent"},
                     state={"strikes": 1,
                            "sonde_restarts": _load().MAX_SONDE_RESTARTS})
        h.run()
        self.assertEqual(h.started, [self.ACARS, self.VDL2])
        self.assertEqual(h.intent_writes, ["acars"],
                         "intent must flip so it stops retrying a dead decoder")

    def test_fallback_starts_master_before_slave(self):
        h = _Harness("radiosonde", {self.SONDE: "absent"},
                     state={"strikes": 1,
                            "sonde_restarts": _load().MAX_SONDE_RESTARTS})
        h.run()
        self.assertEqual(h.started, [self.ACARS, self.VDL2],
                         "acarsdec opens the RSPduo as master; dumpvdl2 is the "
                         "slave and must follow")

    def test_no_decoder_at_all_on_acars_intent_restores_acars(self):
        h = _Harness("acars", {}, state={"strikes": 1})
        h.run()
        self.assertEqual(h.started, [self.ACARS, self.VDL2])

    def test_fallback_kicks_sdrplay_first(self):
        """A decoder that died mid-stream can leave the API wedged."""
        h = _Harness("acars", {}, state={"strikes": 1})
        h.run()
        self.assertGreaterEqual(h.kicks, 1)

    def test_recovery_clears_strikes_and_budget(self):
        h = _Harness("radiosonde", {self.SONDE: "running"},
                     state={"strikes": 1, "sonde_restarts": 2})
        h.run()
        self.assertEqual(h.last_state, {"strikes": 0, "sonde_restarts": 0})

    def test_a_failed_retry_stays_armed_and_spends_budget(self):
        """A failed retry must NOT reset the strike count.

        Holding it at the threshold means the next 60 s cycle retries straight
        away instead of waiting two fresh misses, so three failures take ~3
        minutes rather than ~6 and ACARS comes back sooner. Resetting it would
        also mean a permanently dead decoder never spends its budget and the
        fallback never fires.
        """
        m = _load()
        h = _Harness("radiosonde", {self.SONDE: "absent"},
                     state={"strikes": 1}, start_fails=(self.SONDE,))
        h.run()
        self.assertGreaterEqual(h.last_state["strikes"], m.STRIKES_BEFORE_ACTION,
                                "must stay armed so the next cycle acts at once")
        self.assertEqual(h.last_state["sonde_restarts"], 1,
                         "the failed attempt must count against the budget")

    def test_three_failed_retries_reach_the_acars_fallback(self):
        """End to end: a decoder that will not start must not strand the box."""
        m = _load()
        state = {"strikes": 1}
        started_each_round = []
        for _ in range(m.MAX_SONDE_RESTARTS + 1):
            h = _Harness("radiosonde", {self.SONDE: "absent"},
                         state=state, start_fails=(self.SONDE,))
            h.run()
            started_each_round.append(list(h.started))
            state = h.last_state
            if h.intent_writes:
                break
        self.assertEqual(started_each_round[-1], [self.ACARS, self.VDL2],
                         "the last round must restore ACARS; rounds=%r"
                         % (started_each_round,))


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.m = _load()

    def test_acars_mode_covers_both_halves_of_the_rspduo_pair(self):
        self.assertEqual(self.m.SERVICES["acars"],
                         ["com.scannerproject.acarsdec",
                          "com.scannerproject.dumpvdl2"])

    def test_intent_defaults_to_the_resilient_baseline(self):
        """acarsdec/dumpvdl2 are KeepAlive=true; radiosonde is not."""
        self.m.INTENT_PATH = Path("/nonexistent/wx_decoder_intent")
        self.assertEqual(self.m.read_intent(), "acars")

    def test_unknown_intent_falls_back_to_acars(self):
        import tempfile, os
        fd, p = tempfile.mkstemp()
        try:
            os.write(fd, b"banana\n")
            os.close(fd)
            self.m.INTENT_PATH = Path(p)
            self.assertEqual(self.m.read_intent(), "acars")
        finally:
            os.unlink(p)

    def test_grace_is_more_than_one_interval(self):
        self.assertGreaterEqual(self.m.STRIKES_BEFORE_ACTION, 2,
                                "one strike would interrupt a live handoff")

    def test_retry_budget_is_bounded(self):
        self.assertGreater(self.m.MAX_SONDE_RESTARTS, 0)
        self.assertLessEqual(self.m.MAX_SONDE_RESTARTS, 10,
                             "an unbounded retry loop is a crash loop")


class StateParsingTests(unittest.TestCase):
    LIST_OUT = ("PID\tStatus\tLabel\n"
                "17893\t0\tcom.scannerproject.dumpvdl2\n"
                "17890\t0\tcom.scannerproject.acarsdec\n"
                "-\t0\tcom.scannerproject.radiosonde\n")

    def test_pid_column_decides_running_vs_loaded(self):
        m = _load()

        class R:
            stdout = StateParsingTests.LIST_OUT
            returncode = 0
        m.launchctl = lambda *a, **k: R()
        st = m.service_states()
        self.assertEqual(st["com.scannerproject.acarsdec"], "running")
        self.assertEqual(st["com.scannerproject.radiosonde"], "loaded")
        self.assertNotIn("Label", st)


if __name__ == "__main__":
    unittest.main()
