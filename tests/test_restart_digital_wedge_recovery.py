"""Tests for the restart_digital health-probe + wedge-recovery harness.

These cover the durable fix for the recurring op25 wedge pattern (sdrplay
tuner-claim hang triggered by profile switches + accumulated restart churn).
Without this harness, restart_digital() returned success the moment
`systemctl start` came back ok, even if multi_rx.py wedged before binding
its HTTP terminal — leaving the system in "alive but not decoding" state.

What the harness adds:
- _op25_http_status_ports() discovers ports from instances.json with fallback
- _probe_op25_http_port() does a single GET, returns (ok, detail)
- _wait_for_op25_health() polls until all ports HTTP-200 or timeout
- restart_digital() now polls post-start, escalates on probe failure by
  force-restarting sdrplay daemon, caps at OP25_WEDGE_RECOVERY_MAX_ATTEMPTS
- digital_restart_state() surfaces attempts/probe/wedge counters for /api/status
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from ui import systemd as systemd_mod


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Quiet200Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args, **kwargs):
        pass


class _StubHTTPServer:
    """Tiny background HTTP server bound to a known port; returns 200 for any GET."""

    def __init__(self):
        self.port = _free_port()
        self.server = HTTPServer(("127.0.0.1", self.port), _Quiet200Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class Op25HttpStatusPortsDiscoveryTests(unittest.TestCase):
    def test_falls_back_to_op25_status_port_env_when_instances_missing(self):
        with mock.patch.dict(os.environ, {
            "OP25_INSTANCES_PATH": "/nonexistent/path/instances.json",
            "OP25_STATUS_PORT": "8083",
        }):
            ports = systemd_mod._op25_http_status_ports()
        self.assertEqual([8083], ports)

    def test_falls_back_to_8080_when_no_env_and_no_file(self):
        env = {k: v for k, v in os.environ.items() if k != "OP25_STATUS_PORT"}
        env["OP25_INSTANCES_PATH"] = "/nonexistent/path/instances.json"
        with mock.patch.dict(os.environ, env, clear=True):
            ports = systemd_mod._op25_http_status_ports()
        self.assertEqual([8080], ports)

    def test_reads_instances_json_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "instances.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump([
                    {"http_status_port": 8080},
                    {"http_status_port": 8081},
                    {"http_status_port": 8082},
                ], fh)
            with mock.patch.dict(os.environ, {"OP25_INSTANCES_PATH": path}):
                ports = systemd_mod._op25_http_status_ports()
        self.assertEqual([8080, 8081, 8082], ports)

    def test_instances_json_dedups_and_skips_invalid_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "instances.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump([
                    {"http_status_port": 8080},
                    {"http_status_port": "nope"},       # invalid type
                    {"http_status_port": 0},            # invalid value
                    {"http_status_port": 8080},         # duplicate
                    "not-a-dict",                       # invalid entry
                    {"http_status_port": 8082},
                ], fh)
            with mock.patch.dict(os.environ, {"OP25_INSTANCES_PATH": path}):
                ports = systemd_mod._op25_http_status_ports()
        self.assertEqual([8080, 8082], ports)


class ProbeOp25HttpPortTests(unittest.TestCase):
    def test_returns_true_on_live_200(self):
        server = _StubHTTPServer()
        try:
            ok, detail = systemd_mod._probe_op25_http_port(server.port, timeout_sec=2.0)
        finally:
            server.stop()
        self.assertTrue(ok)
        self.assertIn(f"port {server.port}", detail)
        self.assertIn("200", detail)

    def test_returns_false_on_connection_refused(self):
        # Pick a port nothing's listening on.
        port = _free_port()
        ok, detail = systemd_mod._probe_op25_http_port(port, timeout_sec=1.0)
        self.assertFalse(ok)
        self.assertIn(f"port {port}", detail)

    def test_returns_false_on_timeout(self):
        # Use a routable-but-not-listening IP combo via short timeout.
        # 127.0.0.1 on an unused port → connection refused (fast). For a
        # true timeout we'd need a black-hole IP — that's flaky in CI. We
        # exercise the timeout-as-failure path via a Mock instead.
        with mock.patch.object(systemd_mod, "urlopen", side_effect=TimeoutError("read timeout")):
            ok, detail = systemd_mod._probe_op25_http_port(9999, timeout_sec=0.5)
        self.assertFalse(ok)
        self.assertIn("TimeoutError", detail)


class WaitForOp25HealthTests(unittest.TestCase):
    def test_all_ports_up_returns_ok(self):
        s1 = _StubHTTPServer()
        s2 = _StubHTTPServer()
        try:
            with mock.patch.object(
                systemd_mod, "_op25_http_status_ports",
                return_value=[s1.port, s2.port],
            ):
                ok, detail = systemd_mod._wait_for_op25_health(
                    timeout_sec=3.0, poll_interval_sec=0.2
                )
        finally:
            s1.stop()
            s2.stop()
        self.assertTrue(ok, detail)
        self.assertIn("200", detail)

    def test_no_ports_returns_failure(self):
        with mock.patch.object(systemd_mod, "_op25_http_status_ports", return_value=[]):
            ok, detail = systemd_mod._wait_for_op25_health(timeout_sec=1.0)
        self.assertFalse(ok)
        self.assertIn("no op25 status ports", detail)

    def test_times_out_when_ports_refuse(self):
        port = _free_port()  # nothing listening
        with mock.patch.object(systemd_mod, "_op25_http_status_ports", return_value=[port]):
            start = time.monotonic()
            ok, detail = systemd_mod._wait_for_op25_health(
                timeout_sec=0.6, poll_interval_sec=0.2, per_port_timeout_sec=0.1
            )
            elapsed = time.monotonic() - start
        self.assertFalse(ok)
        self.assertIn("timeout", detail)
        # Sanity: we waited approximately the timeout, not way longer.
        self.assertLess(elapsed, 2.5)

    def test_one_port_down_one_up_fails(self):
        live = _StubHTTPServer()
        dead = _free_port()
        try:
            with mock.patch.object(
                systemd_mod, "_op25_http_status_ports",
                return_value=[live.port, dead],
            ):
                ok, detail = systemd_mod._wait_for_op25_health(
                    timeout_sec=0.6, poll_interval_sec=0.2, per_port_timeout_sec=0.1
                )
        finally:
            live.stop()
        self.assertFalse(ok)
        # detail should reference both ports somewhere across the iterations
        self.assertIn(f"port {dead}", detail)


class RestartDigitalOrchestrationTests(unittest.TestCase):
    """End-to-end test of restart_digital() with systemctl/sleep/probe mocked.

    Strategy: patch the systemctl wrappers, time.sleep, and _wait_for_op25_health.
    Verify the orchestration: gentle attempt, then escalation on probe failure.
    """

    def setUp(self):
        # Reset module state before each test for clean assertions.
        with systemd_mod._DIGITAL_RESTART_STATE_LOCK:
            for k in systemd_mod._DIGITAL_RESTART_STATE:
                if isinstance(systemd_mod._DIGITAL_RESTART_STATE[k], (int, float)):
                    systemd_mod._DIGITAL_RESTART_STATE[k] = (
                        0 if isinstance(systemd_mod._DIGITAL_RESTART_STATE[k], int) else 0.0
                    )
                else:
                    systemd_mod._DIGITAL_RESTART_STATE[k] = ""

        self.calls: list[tuple] = []

        def _stop(unit, use_sudo=True):
            self.calls.append(("stop", unit))
            return True, ""

        def _start(unit, use_sudo=True):
            self.calls.append(("start", unit))
            return True, ""

        def _restart(unit, use_sudo=True):
            self.calls.append(("restart", unit))
            return True, ""

        def _kill(unit):
            self.calls.append(("kill", unit))

        def _reset_failed(units):
            self.calls.append(("reset-failed", tuple(units)))

        def _unit_configured(unit):
            return bool(unit)

        self.patches = [
            mock.patch.object(systemd_mod, "_stop_unit", side_effect=_stop),
            mock.patch.object(systemd_mod, "_start_unit", side_effect=_start),
            mock.patch.object(systemd_mod, "_restart_unit", side_effect=_restart),
            mock.patch.object(systemd_mod, "_kill_unit", side_effect=_kill),
            mock.patch.object(systemd_mod, "_reset_failed_units", side_effect=_reset_failed),
            mock.patch.object(systemd_mod, "_unit_configured", side_effect=_unit_configured),
            mock.patch.object(systemd_mod.time, "sleep", side_effect=lambda s: None),
        ]
        for p in self.patches:
            p.start()
        for p in self.patches:
            self.addCleanup(p.stop)

        env = {
            "OP25_POST_START_PROBE_ENABLED": "1",
            "OP25_POST_START_PROBE_TIMEOUT_SEC": "1.0",
            "OP25_WEDGE_RECOVERY_MAX_ATTEMPTS": "2",
            "DIGITAL_RESTART_SDRPLAY_SETTLE_SEC": "0",
            "DIGITAL_RESTART_OP25_SETTLE_SEC": "0",
        }
        self._env_patcher = mock.patch.dict(os.environ, env)
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

        self._units_patcher = mock.patch.dict(
            systemd_mod.UNITS,
            {"digital": "scanner-digital-op25", "digital_audio": "scanner-digital-op25-audio"},
        )
        self._units_patcher.start()
        self.addCleanup(self._units_patcher.stop)

    def test_gentle_path_succeeds_when_probe_passes(self):
        with mock.patch.object(
            systemd_mod, "_wait_for_op25_health",
            return_value=(True, "port 8080: HTTP 200"),
        ), mock.patch.object(systemd_mod, "_sdrplay_daemon_alive", return_value=True), \
             mock.patch.object(
                 systemd_mod, "_sdrplay_daemon_healthy",
                 return_value=(True, "daemon running"),
             ):
            ok, err = systemd_mod.restart_digital(reason="profile_switch")
        self.assertTrue(ok, err)
        self.assertEqual("", err)
        # No sdrplay restart on gentle attempt when daemon is alive + healthy.
        sdrplay_restarts = [c for c in self.calls if c == ("restart", "sdrplay")]
        self.assertEqual([], sdrplay_restarts)

        state = systemd_mod.digital_restart_state()
        self.assertEqual(1, state["attempts_total"])
        self.assertEqual("profile_switch", state["last_attempt_reason"])
        self.assertEqual("ok", state["last_health_probe_result"])
        self.assertEqual(0, state["wedge_recovery_total"])

    def test_wedge_recovery_kicks_in_when_first_probe_fails(self):
        # First probe call → wedged; second call (after escalation) → ok.
        probe_results = [
            (False, "timeout after 1.0s; port 8080: connection refused"),
            (True, "port 8080: HTTP 200"),
        ]
        probe_mock = mock.Mock(side_effect=probe_results)
        with mock.patch.object(systemd_mod, "_wait_for_op25_health", probe_mock), \
             mock.patch.object(systemd_mod, "_sdrplay_daemon_alive", return_value=True), \
             mock.patch.object(
                 systemd_mod, "_sdrplay_daemon_healthy",
                 return_value=(True, "daemon running"),
             ):
            ok, err = systemd_mod.restart_digital(reason="profile_switch")
        self.assertTrue(ok, err)
        # Two probe calls: gentle + 1 escalation
        self.assertEqual(2, probe_mock.call_count)
        # Escalation forced a sdrplay restart
        sdrplay_restarts = [c for c in self.calls if c == ("restart", "sdrplay")]
        self.assertEqual(1, len(sdrplay_restarts))

        state = systemd_mod.digital_restart_state()
        self.assertEqual(1, state["attempts_total"])
        self.assertEqual(1, state["wedge_recovery_total"])
        self.assertGreater(state["last_wedge_recovery_ts"], 0.0)

    def test_wedge_recovery_caps_at_max_escalations(self):
        # Every probe attempt fails — should cap at max_escalations escalations
        # then return error.
        probe_mock = mock.Mock(return_value=(False, "timeout; refused"))
        with mock.patch.object(systemd_mod, "_wait_for_op25_health", probe_mock), \
             mock.patch.object(systemd_mod, "_sdrplay_daemon_alive", return_value=True), \
             mock.patch.object(
                 systemd_mod, "_sdrplay_daemon_healthy",
                 return_value=(True, "daemon running"),
             ):
            ok, err = systemd_mod.restart_digital(reason="profile_switch")
        self.assertFalse(ok)
        self.assertIn("wedge recovery exhausted", err)
        # 1 gentle + 2 escalations = 3 total probe attempts
        self.assertEqual(3, probe_mock.call_count)

        state = systemd_mod.digital_restart_state()
        self.assertEqual(2, state["wedge_recovery_total"])

    def test_probe_disabled_skips_health_check_and_returns_ok(self):
        with mock.patch.dict(os.environ, {"OP25_POST_START_PROBE_ENABLED": "0"}):
            probe_mock = mock.Mock(return_value=(False, "would fail"))
            with mock.patch.object(systemd_mod, "_wait_for_op25_health", probe_mock), \
                 mock.patch.object(systemd_mod, "_sdrplay_daemon_alive", return_value=True), \
                 mock.patch.object(
                     systemd_mod, "_sdrplay_daemon_healthy",
                     return_value=(True, "daemon running"),
                 ):
                ok, err = systemd_mod.restart_digital(reason="manual_override")
        self.assertTrue(ok, err)
        probe_mock.assert_not_called()
        state = systemd_mod.digital_restart_state()
        self.assertEqual("skipped", state["last_health_probe_result"])

    def test_dead_sdrplay_triggers_restart_on_gentle_attempt(self):
        with mock.patch.object(
            systemd_mod, "_wait_for_op25_health",
            return_value=(True, "port 8080: HTTP 200"),
        ), mock.patch.object(systemd_mod, "_sdrplay_daemon_alive", return_value=False):
            ok, err = systemd_mod.restart_digital(reason="favorites_sync")
        self.assertTrue(ok, err)
        # gentle attempt should bounce sdrplay because daemon was not alive
        sdrplay_restarts = [c for c in self.calls if c == ("restart", "sdrplay")]
        self.assertEqual(1, len(sdrplay_restarts))

    def test_attempt_reason_recorded_in_state(self):
        with mock.patch.object(
            systemd_mod, "_wait_for_op25_health",
            return_value=(True, "ok"),
        ), mock.patch.object(systemd_mod, "_sdrplay_daemon_alive", return_value=True), \
             mock.patch.object(
                 systemd_mod, "_sdrplay_daemon_healthy",
                 return_value=(True, "daemon running"),
             ):
            for reason in ("profile_switch", "vdl2_reclaim_dongle", "manager_restart"):
                systemd_mod.restart_digital(reason=reason)

        state = systemd_mod.digital_restart_state()
        self.assertEqual(3, state["attempts_total"])
        self.assertEqual("manager_restart", state["last_attempt_reason"])  # most recent


class DigitalRestartStateAccessorTests(unittest.TestCase):
    def test_returns_a_copy_not_a_reference(self):
        snap_a = systemd_mod.digital_restart_state()
        snap_b = systemd_mod.digital_restart_state()
        self.assertIsNot(snap_a, snap_b)
        # Mutating snap_a must not affect snap_b or the underlying state.
        snap_a["attempts_total"] = 999999
        self.assertNotEqual(snap_b.get("attempts_total"), 999999)


if __name__ == "__main__":
    unittest.main()
