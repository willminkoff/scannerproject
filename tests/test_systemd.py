from __future__ import annotations

import os
import subprocess
import unittest
from unittest import mock

from ui import service_backend
from ui import systemd


class _SystemdBackendTestCase(unittest.TestCase):
    """Base: pin the cached ServiceBackend to systemd for the duration.

    SB7.2 moved the systemctl subprocess bodies verbatim into
    ui/service_backend.py::SystemdBackend; the ui/systemd.py primitives
    are now shims over the process-wide cached backend.  These tests
    patch ``SystemdBackend.run`` (the single systemctl funnel — the old
    ``systemd._run_systemctl`` seam still exists but only direct callers
    like set_bt_heal_auto_recovery route through it).
    """

    def setUp(self) -> None:
        self._env = mock.patch.dict(os.environ, {"SCANNER_SERVICE_BACKEND": "systemd"})
        self._env.start()
        self.addCleanup(self._env.stop)
        service_backend._reset_backend_for_tests()
        self.addCleanup(service_backend._reset_backend_for_tests)


class UnitActiveEnterEpochTests(_SystemdBackendTestCase):
    def test_returns_epoch_without_sudo_when_unprivileged_show_succeeds(self):
        result = subprocess.CompletedProcess(
            ["systemctl", "show"],
            0,
            stdout="1234567\n",
            stderr="",
        )
        with mock.patch.object(
            service_backend.SystemdBackend, "run", return_value=result
        ) as run_systemctl:
            epoch = systemd.unit_active_enter_epoch("scanner-digital-op25.service")
        self.assertEqual(1.234567, epoch)
        run_systemctl.assert_called_once()

    def test_skips_sudo_fallback_when_failure_is_not_permission_related(self):
        result = subprocess.CompletedProcess(
            ["systemctl", "show"],
            1,
            stdout="",
            stderr="Unit scanner-digital-op25.service could not be found.",
        )
        with mock.patch.object(
            service_backend.SystemdBackend, "run", return_value=result
        ) as run_systemctl:
            epoch = systemd.unit_active_enter_epoch("scanner-digital-op25.service")
        self.assertIsNone(epoch)
        run_systemctl.assert_called_once()

    def test_uses_sudo_fallback_when_permission_error_is_reported(self):
        results = [
            subprocess.CompletedProcess(
                ["systemctl", "show"],
                1,
                stdout="",
                stderr="Interactive authentication required.",
            ),
            subprocess.CompletedProcess(
                ["sudo", "systemctl", "show"],
                0,
                stdout="7654321\n",
                stderr="",
            ),
        ]
        with mock.patch.object(
            service_backend.SystemdBackend, "run", side_effect=results
        ) as run_systemctl:
            epoch = systemd.unit_active_enter_epoch("scanner-digital-op25.service")
        self.assertEqual(7.654321, epoch)
        self.assertEqual(2, run_systemctl.call_count)


class RestartDigitalRecoveryTests(_SystemdBackendTestCase):
    """Pin the restart_digital() systemctl cascade order.

    Historically these ran the REAL ``_sdrplay_daemon_alive`` pgrep and
    the REAL ``_wait_for_op25_health`` HTTP probe, so they only passed on
    a box with op25 actually serving 127.0.0.1:8080 (the Micro) and
    wedged for the full 45 s probe window anywhere else.  SB7.2 made them
    hermetic: daemon-alive and the post-start probe are patched so the
    assertions pin the cascade itself, machine-independently.
    """

    def _ok(self, args):
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def test_restarts_digital_with_sdrplay_and_audio_recovery(self):
        calls = []

        def fake_run(args, use_sudo=False):
            calls.append((tuple(args), use_sudo))
            return self._ok(args)

        existing_units = {"scanner-digital-op25-audio", "sdrplay"}
        with mock.patch.dict(
            systemd.UNITS,
            {
                "digital": "scanner-digital-op25",
                "digital_audio": "scanner-digital-op25-audio",
            },
        ), mock.patch.object(
            systemd,
            "unit_exists",
            side_effect=lambda unit: unit in existing_units,
        ), mock.patch.object(
            service_backend.SystemdBackend,
            "run",
            side_effect=fake_run,
        ), mock.patch.object(
            systemd,
            "_sdrplay_daemon_alive",
            return_value=True,
        ), mock.patch.object(
            systemd,
            "_sdrplay_daemon_healthy",
            return_value=(False, "test: forced unhealthy"),
        ), mock.patch.object(
            systemd,
            "_wait_for_op25_health",
            return_value=(True, "test: probe ok"),
        ), mock.patch.object(systemd.time, "sleep") as sleep:
            ok, err = systemd.restart_digital()

        self.assertTrue(ok)
        self.assertEqual("", err)
        self.assertEqual(
            [
                (("stop", "scanner-digital-op25-audio"), True),
                (("stop", "scanner-digital-op25"), True),
                (("kill", "-s", "SIGKILL", "scanner-digital-op25"), True),
                (("kill", "-s", "SIGKILL", "scanner-digital-op25-audio"), True),
                (
                    (
                        "reset-failed",
                        "scanner-digital-op25",
                        "scanner-digital-op25-audio",
                    ),
                    True,
                ),
                (("restart", "sdrplay"), True),
                (("start", "scanner-digital-op25"), True),
                (("restart", "scanner-digital-op25-audio"), True),
            ],
            calls,
        )
        sleep.assert_has_calls([mock.call(3.0), mock.call(16.0)])

    def test_skips_sdrplay_restart_when_daemon_healthy(self):
        """Option D: skip the sdrplay daemon restart when the probe says healthy.

        Every observed restart of sdrplay segfaults the daemon (12/12 on
        2026-05-13), so the heavy-handed restart is a net loss when the
        daemon's grpc state is intact. Asserts the cascade omits both the
        sdrplay restart call AND its 3s settle sleep.
        """
        calls = []

        def fake_run(args, use_sudo=False):
            calls.append((tuple(args), use_sudo))
            return self._ok(args)

        existing_units = {"scanner-digital-op25-audio", "sdrplay"}
        with mock.patch.dict(
            systemd.UNITS,
            {
                "digital": "scanner-digital-op25",
                "digital_audio": "scanner-digital-op25-audio",
            },
        ), mock.patch.object(
            systemd,
            "unit_exists",
            side_effect=lambda unit: unit in existing_units,
        ), mock.patch.object(
            service_backend.SystemdBackend,
            "run",
            side_effect=fake_run,
        ), mock.patch.object(
            systemd,
            "_sdrplay_daemon_alive",
            return_value=True,
        ), mock.patch.object(
            systemd,
            "_sdrplay_daemon_healthy",
            return_value=(True, "test: forced healthy"),
        ), mock.patch.object(
            systemd,
            "_wait_for_op25_health",
            return_value=(True, "test: probe ok"),
        ), mock.patch.object(systemd.time, "sleep") as sleep:
            ok, err = systemd.restart_digital()

        self.assertTrue(ok)
        self.assertEqual("", err)
        self.assertNotIn((("restart", "sdrplay"), True), calls)
        # Only the post-start op25 settle remains; the 3s sdrplay settle is gone.
        sleep.assert_called_once_with(16.0)

    def test_restarts_digital_without_optional_units(self):
        calls = []

        def fake_run(args, use_sudo=False):
            calls.append((tuple(args), use_sudo))
            return self._ok(args)

        with mock.patch.dict(
            systemd.UNITS,
            {"digital": "scanner-digital-op25", "digital_audio": "missing-audio"},
        ), mock.patch.object(
            systemd,
            "unit_exists",
            return_value=False,
        ), mock.patch.object(
            service_backend.SystemdBackend,
            "run",
            side_effect=fake_run,
        ), mock.patch.object(
            systemd,
            "_wait_for_op25_health",
            return_value=(True, "test: probe ok"),
        ), mock.patch.object(systemd.time, "sleep") as sleep:
            ok, err = systemd.restart_digital()

        self.assertTrue(ok)
        self.assertEqual("", err)
        self.assertEqual(
            [
                (("stop", "scanner-digital-op25"), True),
                (("kill", "-s", "SIGKILL", "scanner-digital-op25"), True),
                (("reset-failed", "scanner-digital-op25"), True),
                (("start", "scanner-digital-op25"), True),
            ],
            calls,
        )
        sleep.assert_called_once_with(16.0)

    def test_reports_digital_start_failure(self):
        def fake_run(args, use_sudo=False):
            if args == ["start", "scanner-digital-op25"]:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="start failed")
            return self._ok(args)

        with mock.patch.dict(
            systemd.UNITS,
            {"digital": "scanner-digital-op25", "digital_audio": "missing-audio"},
        ), mock.patch.object(
            systemd,
            "unit_exists",
            return_value=False,
        ), mock.patch.object(
            service_backend.SystemdBackend,
            "run",
            side_effect=fake_run,
        ), mock.patch.object(systemd.time, "sleep") as sleep:
            ok, err = systemd.restart_digital()

        self.assertFalse(ok)
        # A gentle-attempt failure escalates through the wedge-recovery
        # loop (OP25_WEDGE_RECOVERY_MAX_ATTEMPTS, default 2) before giving
        # up — the caller sees the exhausted wrapper around the last
        # underlying error.  (The pre-wedge-recovery expectation of a bare
        # "digital start: ..." error predates the escalation loop.)
        self.assertEqual(
            "wedge recovery exhausted after 2 escalations: digital start: start failed",
            err,
        )
        sleep.assert_not_called()


class SdrplayDaemonHealthProbeTests(_SystemdBackendTestCase):
    def test_unhealthy_when_daemon_not_running(self):
        pgrep_miss = subprocess.CompletedProcess(["pgrep"], 1, stdout="", stderr="")
        with mock.patch.object(systemd.subprocess, "run", return_value=pgrep_miss):
            healthy, reason = systemd._sdrplay_daemon_healthy()
        self.assertFalse(healthy)
        self.assertIn("not running", reason)

    def test_unhealthy_when_recent_segfault(self):
        pgrep_hit = subprocess.CompletedProcess(["pgrep"], 0, stdout="12345\n", stderr="")
        journal_hit = subprocess.CompletedProcess(
            ["journalctl"],
            0,
            stdout="May 13 09:32:26 Micro kernel: sdrplay_apiServ[459348]: segfault at 7c58604dd2d4 ip ...\n",
            stderr="",
        )

        def fake_run(args, **kwargs):
            if args and args[0] == "pgrep":
                return pgrep_hit
            return journal_hit

        with mock.patch.object(systemd.subprocess, "run", side_effect=fake_run):
            healthy, reason = systemd._sdrplay_daemon_healthy()
        self.assertFalse(healthy)
        self.assertIn("segfault", reason)

    def test_healthy_when_daemon_running_and_no_segfault(self):
        pgrep_hit = subprocess.CompletedProcess(["pgrep"], 0, stdout="12345\n", stderr="")
        journal_clean = subprocess.CompletedProcess(["journalctl"], 0, stdout="", stderr="")

        def fake_run(args, **kwargs):
            if args and args[0] == "pgrep":
                return pgrep_hit
            return journal_clean

        with mock.patch.object(systemd.subprocess, "run", side_effect=fake_run):
            healthy, reason = systemd._sdrplay_daemon_healthy()
        self.assertTrue(healthy)
        self.assertIn("no segfault", reason)


if __name__ == "__main__":
    unittest.main()
