"""Decision logic for macos/bin/sdrplay-healer.py.

The healer replaced a watchdog whose blind restarts every 30 s churned the
SDRplay API into a wedge. So the properties that matter most are the ones
that keep it from churning: it only acts on a real stall, gives a restarted
daemon time to come up, backs off after a recovery that didn't work, caps
itself per hour, and never touches ground when an operator stopped it.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "macos" / "bin" / "sdrplay-healer.py"
_spec = importlib.util.spec_from_file_location("sdrplay_healer", _PATH)
healer = importlib.util.module_from_spec(_spec)
# dataclasses resolve annotations through sys.modules[cls.__module__].
sys.modules[_spec.name] = healer
_spec.loader.exec_module(healer)

State, Observation, decide = healer.State, healer.Observation, healer.decide
STALL = healer.STALL_SEC
GRACE = healer.START_GRACE_SEC


class Sim:
    """Feed observations through decide() the way 30 s launchd ticks would."""

    def __init__(self):
        self.state = State()
        self.now = 1_000_000.0
        self.actions = []

    def tick(self, bytes_, loaded=True, dt=30.0):
        self.now += dt
        action, self.state, reason = decide(
            self.state, Observation(agent_loaded=loaded, bytes=bytes_), self.now)
        self.actions.append(action)
        return action, reason

    def run_healthy(self, n=3, start=1000.0):
        b = start
        for _ in range(n):
            b += 4096
            self.tick(b)
        return b


class SteadyStateTests(unittest.TestCase):
    def test_first_sample_is_only_a_baseline(self):
        sim = Sim()
        self.assertEqual(sim.tick(5000.0)[0], "wait")

    def test_advancing_counter_is_healthy(self):
        sim = Sim()
        sim.tick(5000.0)
        self.assertEqual(sim.tick(9096.0)[0], "healthy")

    def test_flat_counter_waits_until_stall_limit(self):
        sim = Sim()
        b = sim.run_healthy()
        self.assertEqual(sim.tick(b, dt=STALL - 1)[0], "wait")

    def test_flat_counter_past_limit_recovers(self):
        sim = Sim()
        b = sim.run_healthy()
        action, reason = sim.tick(b, dt=STALL + 1)
        self.assertEqual(action, "recover")
        self.assertIn("not advancing", reason)

    def test_unreachable_metrics_past_limit_recovers(self):
        """The 'hung in SDR open' case: daemon loaded, metrics never answer."""
        sim = Sim()
        sim.run_healthy()
        action, reason = sim.tick(None, dt=STALL + 1)
        self.assertEqual(action, "recover")
        self.assertIn("unreachable", reason)

    def test_counter_reset_is_a_restart_not_a_stall(self):
        sim = Sim()
        sim.run_healthy(start=500_000.0)
        action, reason = sim.tick(4096.0)
        self.assertEqual(action, "wait")
        self.assertIn("reset", reason)
        self.assertEqual(sim.tick(8192.0)[0], "healthy")


class OperatorTests(unittest.TestCase):
    def test_unloaded_agent_is_left_alone(self):
        sim = Sim()
        sim.run_healthy()
        for _ in range(20):  # 10 minutes stopped on purpose
            self.assertEqual(sim.tick(None, loaded=False)[0], "idle")

    def test_reload_after_manual_stop_gets_fresh_grace(self):
        sim = Sim()
        sim.run_healthy()
        sim.tick(None, loaded=False, dt=3600)
        # Reloaded; metrics take a moment to appear. Must not recover on the
        # stale pre-stop clock.
        self.assertEqual(sim.tick(None)[0], "wait")
        self.assertEqual(sim.tick(None)[0], "wait")


class RecoveryTests(unittest.TestCase):
    def _stall_to_recover(self, sim):
        b = sim.run_healthy()
        self.assertEqual(sim.tick(b, dt=STALL + 1)[0], "recover")

    def test_successful_recovery(self):
        sim = Sim()
        self._stall_to_recover(sim)
        self.assertEqual(sim.tick(None)[0], "wait")   # daemon still starting
        self.assertEqual(sim.tick(2048.0)[0], "wait")  # first sample: baseline
        self.assertEqual(sim.tick(6144.0)[0], "recovered")
        self.assertEqual(sim.state.failed_streak, 0)
        self.assertFalse(sim.state.awaiting_verify)

    def test_start_grace_after_recovery(self):
        """A restarted daemon gets START_GRACE, not STALL, to show progress."""
        sim = Sim()
        self._stall_to_recover(sim)
        t = 0.0
        while t + 30 < GRACE:
            t += 30
            self.assertEqual(sim.tick(None)[0], "wait", f"at +{t}s")

    def test_failed_recovery_backs_off(self):
        sim = Sim()
        self._stall_to_recover(sim)
        action, reason = sim.tick(None, dt=GRACE + 1)
        self.assertEqual(action, "hold")
        self.assertIn("backing off", reason)
        self.assertEqual(sim.state.failed_streak, 1)
        # Still inside the 120 s backoff for one failure.
        self.assertEqual(sim.tick(None, dt=30)[0], "hold")
        # Past it: try again.
        self.assertEqual(sim.tick(None, dt=healer.BACKOFF_SEC[1])[0], "recover")

    def test_backoff_grows_with_consecutive_failures(self):
        sim = Sim()
        self._stall_to_recover(sim)
        delays_seen = []
        for _ in range(3):
            sim.tick(None, dt=GRACE + 1)          # recovery judged failed
            streak = sim.state.failed_streak
            delay = healer.BACKOFF_SEC[min(streak, len(healer.BACKOFF_SEC) - 1)]
            delays_seen.append(delay)
            self.assertEqual(sim.tick(None, dt=delay + 1)[0], "recover")
        self.assertEqual(delays_seen, sorted(delays_seen))
        self.assertGreater(delays_seen[-1], delays_seen[0])

    def test_success_resets_the_failure_streak(self):
        sim = Sim()
        self._stall_to_recover(sim)
        sim.tick(None, dt=GRACE + 1)
        self.assertEqual(sim.state.failed_streak, 1)
        self.assertEqual(sim.tick(None, dt=healer.BACKOFF_SEC[1] + 1)[0], "recover")
        sim.tick(1000.0)
        self.assertEqual(sim.tick(5000.0)[0], "recovered")
        self.assertEqual(sim.state.failed_streak, 0)


class RateLimitTests(unittest.TestCase):
    def test_hourly_cap_then_hold(self):
        """Each recovery succeeds briefly then the radio dies again (the
        USB-A pattern, compressed). The cap must stop a fifth attempt."""
        sim = Sim()
        recovers = 0
        b = 1000.0
        for _ in range(8):
            b = sim.run_healthy(n=2, start=b)
            action, reason = sim.tick(b, dt=STALL + 1)
            if action == "recover":
                recovers += 1
                b = 0.0
            else:
                self.assertEqual(action, "hold")
                self.assertIn("this hour", reason)
                break
        self.assertEqual(recovers, healer.MAX_RECOVERIES_PER_HOUR)

    def test_cap_releases_after_an_hour(self):
        sim = Sim()
        sim.state.recoveries = [sim.now - 10] * healer.MAX_RECOVERIES_PER_HOUR
        b = sim.run_healthy()
        self.assertEqual(sim.tick(b, dt=STALL + 1)[0], "hold")
        self.assertEqual(sim.tick(b, dt=3600)[0], "recover")


class ParsingTests(unittest.TestCase):
    METRICS = (
        "# HELP chirp_audio_bytes_published_total bytes\n"
        "# TYPE chirp_audio_bytes_published_total counter\n"
        'chirp_audio_branch_silent{daemon="ground"} 0\n'
        'chirp_audio_bytes_published_total{mount="/neptune-ground.mp3"} 573440\n'
        'chirp_flowgraph_alive_seconds_total{daemon="ground"} 147.1\n'
    )

    def test_parse_bytes(self):
        self.assertEqual(healer.parse_bytes(self.METRICS), 573440.0)

    def test_parse_bytes_ignores_other_mounts(self):
        other = 'chirp_audio_bytes_published_total{mount="/neptune-analog.mp3"} 99\n'
        self.assertIsNone(healer.parse_bytes(other))

    def test_parse_bytes_missing(self):
        self.assertIsNone(healer.parse_bytes(""))
        self.assertIsNone(healer.parse_bytes(None))

    def test_parse_bytes_scientific(self):
        line = 'chirp_audio_bytes_published_total{mount="/neptune-ground.mp3"} 1.2e+07\n'
        self.assertEqual(healer.parse_bytes(line), 12_000_000.0)

    def test_parse_serial(self):
        env = (
            "# Set CHIRP_SDR_DEVICE_ARGS to a real serial after plug-in:\n"
            "#   soapy=,driver=sdrplay,serial=<XXXXXXXX>\n"
            "CHIRP_SDR_DEVICE_ARGS=soapy=,driver=sdrplay,serial=240526DE60\n"
            "SOAPY_SDR_PLUGIN_PATH=/opt/homebrew/lib/SoapySDR/modules0.8\n"
        )
        self.assertEqual(healer.parse_serial(env), "240526DE60")

    def test_parse_serial_absent(self):
        self.assertIsNone(healer.parse_serial("CHIRP_SDR_DEVICE_ARGS=rtl=83241970\n"))

    def test_state_ignores_unknown_keys(self):
        s = State.from_dict({"failed_streak": 2, "added_in_a_later_version": True})
        self.assertEqual(s.failed_streak, 2)


if __name__ == "__main__":
    unittest.main()
