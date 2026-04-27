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


if __name__ == "__main__":
    unittest.main()
