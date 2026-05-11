from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from ui import systemd


class UnitActiveEnterEpochTests(unittest.TestCase):
    def test_returns_epoch_without_sudo_when_unprivileged_show_succeeds(self):
        result = subprocess.CompletedProcess(
            ["systemctl", "show"],
            0,
            stdout="1234567\n",
            stderr="",
        )
        with mock.patch.object(systemd, "_run_systemctl", return_value=result) as run_systemctl:
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
        with mock.patch.object(systemd, "_run_systemctl", return_value=result) as run_systemctl:
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
        with mock.patch.object(systemd, "_run_systemctl", side_effect=results) as run_systemctl:
            epoch = systemd.unit_active_enter_epoch("scanner-digital-op25.service")
        self.assertEqual(7.654321, epoch)
        self.assertEqual(2, run_systemctl.call_count)


class RestartDigitalRecoveryTests(unittest.TestCase):
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
            systemd,
            "_run_systemctl",
            side_effect=fake_run,
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
            systemd,
            "_run_systemctl",
            side_effect=fake_run,
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
            systemd,
            "_run_systemctl",
            side_effect=fake_run,
        ), mock.patch.object(systemd.time, "sleep") as sleep:
            ok, err = systemd.restart_digital()

        self.assertFalse(ok)
        self.assertEqual("digital start: start failed", err)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
