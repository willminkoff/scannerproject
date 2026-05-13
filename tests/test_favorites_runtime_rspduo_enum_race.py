"""Tests for the RSPduo USB-enum race retry in ui.favorites_runtime.

Covers the post-boot window where sdrplay-api has finished firmware load
on the USB device tree (so VID:PID is visible) but the kernel hasn't yet
populated the ``serial`` sysfs attribute.  A single-pass probe would
silently return a partial list; the production path now retries once
after a short sleep.
"""

from __future__ import annotations

import logging
import unittest
from unittest import mock

from ui import favorites_runtime


class RspduoUsbEnumRaceTests(unittest.TestCase):
    """_rspduo_usb_serials() count-vs-serials validation + one-shot retry."""

    def test_normal_case_returns_serials_immediately(self):
        """count == len(serials) on first pass → no retry, no sleep."""
        sample = (2, ["AAAA1111", "BBBB2222"])
        with mock.patch.object(
            favorites_runtime, "_rspduo_usb_sample", return_value=sample
        ) as sample_mock, mock.patch.object(
            favorites_runtime.time, "sleep"
        ) as sleep_mock:
            result = favorites_runtime._rspduo_usb_serials()
        self.assertEqual(result, ["AAAA1111", "BBBB2222"])
        self.assertEqual(sample_mock.call_count, 1)
        sleep_mock.assert_not_called()

    def test_zero_devices_returns_empty_no_retry(self):
        """No VID:PID matches → return [] immediately, no retry."""
        with mock.patch.object(
            favorites_runtime, "_rspduo_usb_sample", return_value=(0, [])
        ) as sample_mock, mock.patch.object(
            favorites_runtime.time, "sleep"
        ) as sleep_mock:
            result = favorites_runtime._rspduo_usb_serials()
        self.assertEqual(result, [])
        self.assertEqual(sample_mock.call_count, 1)
        sleep_mock.assert_not_called()

    def test_race_first_pass_partial_retry_succeeds(self):
        """Pass 1: 2 devices, 1 serial.  Pass 2: 2 devices, 2 serials.
        Retry should succeed and return the now-complete list.  INFO logs.
        """
        sample_results = iter([
            (2, ["AAAA1111"]),
            (2, ["AAAA1111", "BBBB2222"]),
        ])
        with mock.patch.object(
            favorites_runtime, "_rspduo_usb_sample",
            side_effect=lambda: next(sample_results),
        ) as sample_mock, mock.patch.object(
            favorites_runtime.time, "sleep"
        ) as sleep_mock, self.assertLogs(
            favorites_runtime.logger, level=logging.INFO
        ) as log_ctx:
            result = favorites_runtime._rspduo_usb_serials()

        self.assertEqual(result, ["AAAA1111", "BBBB2222"])
        self.assertEqual(sample_mock.call_count, 2)
        sleep_mock.assert_called_once()
        # Sleep argument should be the configured constant (default 2.0s).
        sleep_arg = sleep_mock.call_args[0][0]
        self.assertAlmostEqual(sleep_arg, 2.0, places=2)
        # Confirm both INFO log lines fired (incomplete + recovered).
        joined = "\n".join(log_ctx.output)
        self.assertIn("USB enum incomplete", joined)
        self.assertIn("retry recovered", joined)

    def test_persistent_race_both_passes_partial_returns_empty(self):
        """Pass 1 and pass 2 both incomplete → return [] with WARNING."""
        sample_results = iter([
            (2, ["AAAA1111"]),
            (2, ["AAAA1111"]),  # still missing the second serial after retry
        ])
        with mock.patch.object(
            favorites_runtime, "_rspduo_usb_sample",
            side_effect=lambda: next(sample_results),
        ) as sample_mock, mock.patch.object(
            favorites_runtime.time, "sleep"
        ) as sleep_mock, self.assertLogs(
            favorites_runtime.logger, level=logging.WARNING
        ) as log_ctx:
            result = favorites_runtime._rspduo_usb_serials()

        self.assertEqual(result, [])
        self.assertEqual(sample_mock.call_count, 2)
        sleep_mock.assert_called_once()
        joined = "\n".join(log_ctx.output)
        self.assertIn("still incomplete after retry", joined)
        self.assertIn("SoapySDR fallback", joined)

    def test_env_var_overrides_retry_sleep(self):
        """RSPDUO_USB_ENUM_RETRY_SLEEP_SEC env var overrides the default 2.0s."""
        sample_results = iter([
            (2, ["AAAA1111"]),
            (2, ["AAAA1111", "BBBB2222"]),
        ])
        with mock.patch.object(
            favorites_runtime, "_rspduo_usb_sample",
            side_effect=lambda: next(sample_results),
        ), mock.patch.object(
            favorites_runtime.time, "sleep"
        ) as sleep_mock, mock.patch.dict(
            favorites_runtime.os.environ,
            {"RSPDUO_USB_ENUM_RETRY_SLEEP_SEC": "0.5"},
        ):
            result = favorites_runtime._rspduo_usb_serials()
        self.assertEqual(result, ["AAAA1111", "BBBB2222"])
        sleep_mock.assert_called_once()
        self.assertAlmostEqual(sleep_mock.call_args[0][0], 0.5, places=3)

    def test_invalid_env_var_falls_back_to_default(self):
        """Garbage env-var value → default 2.0s sleep, retry still happens."""
        sample_results = iter([
            (2, ["AAAA1111"]),
            (2, ["AAAA1111", "BBBB2222"]),
        ])
        with mock.patch.object(
            favorites_runtime, "_rspduo_usb_sample",
            side_effect=lambda: next(sample_results),
        ), mock.patch.object(
            favorites_runtime.time, "sleep"
        ) as sleep_mock, mock.patch.dict(
            favorites_runtime.os.environ,
            {"RSPDUO_USB_ENUM_RETRY_SLEEP_SEC": "not-a-number"},
        ):
            result = favorites_runtime._rspduo_usb_serials()
        self.assertEqual(result, ["AAAA1111", "BBBB2222"])
        sleep_mock.assert_called_once()
        self.assertAlmostEqual(sleep_mock.call_args[0][0], 2.0, places=2)


class RspduoUsbSampleTests(unittest.TestCase):
    """_rspduo_usb_sample() — single pass; counts VID:PID matches separately
    from collected serials so the caller can detect the firmware-load race."""

    def test_no_sysfs_dir_returns_zero_empty(self):
        with mock.patch.object(favorites_runtime.os.path, "isdir", return_value=False):
            self.assertEqual(favorites_runtime._rspduo_usb_sample(), (0, []))

    def test_listdir_failure_returns_zero_empty(self):
        with mock.patch.object(favorites_runtime.os.path, "isdir", return_value=True), \
             mock.patch.object(favorites_runtime.os, "listdir", side_effect=PermissionError):
            self.assertEqual(favorites_runtime._rspduo_usb_sample(), (0, []))

    def test_counts_match_then_collects_serial(self):
        sysfs = {
            "/sys/bus/usb/devices/1-1/idVendor": "1df7\n",
            "/sys/bus/usb/devices/1-1/idProduct": "3020\n",
            "/sys/bus/usb/devices/1-1/serial": "AAAA1111\n",
        }
        with mock.patch.object(favorites_runtime.os.path, "isdir", return_value=True), \
             mock.patch.object(favorites_runtime.os, "listdir", return_value=["1-1"]), \
             mock.patch.object(favorites_runtime, "_sysfs_text",
                               side_effect=lambda p: sysfs.get(p, "").strip()):
            count, serials = favorites_runtime._rspduo_usb_sample()
        self.assertEqual(count, 1)
        self.assertEqual(serials, ["AAAA1111"])

    def test_match_with_empty_serial_counts_but_not_collected(self):
        """The race signature: VID:PID present, serial empty (firmware loading)."""
        sysfs = {
            "/sys/bus/usb/devices/1-1/idVendor": "1df7",
            "/sys/bus/usb/devices/1-1/idProduct": "3020",
            "/sys/bus/usb/devices/1-1/serial": "",
            "/sys/bus/usb/devices/1-2/idVendor": "1df7",
            "/sys/bus/usb/devices/1-2/idProduct": "3020",
            "/sys/bus/usb/devices/1-2/serial": "BBBB2222",
        }
        with mock.patch.object(favorites_runtime.os.path, "isdir", return_value=True), \
             mock.patch.object(favorites_runtime.os, "listdir",
                               return_value=["1-1", "1-2"]), \
             mock.patch.object(favorites_runtime, "_sysfs_text",
                               side_effect=lambda p: sysfs.get(p, "")):
            count, serials = favorites_runtime._rspduo_usb_sample()
        self.assertEqual(count, 2)
        self.assertEqual(serials, ["BBBB2222"])

    def test_non_rspduo_devices_filtered(self):
        sysfs = {
            "/sys/bus/usb/devices/2-1/idVendor": "0bda",  # Realtek (RTL-SDR)
            "/sys/bus/usb/devices/2-1/idProduct": "2838",
            "/sys/bus/usb/devices/2-1/serial": "80000003",
        }
        with mock.patch.object(favorites_runtime.os.path, "isdir", return_value=True), \
             mock.patch.object(favorites_runtime.os, "listdir", return_value=["2-1"]), \
             mock.patch.object(favorites_runtime, "_sysfs_text",
                               side_effect=lambda p: sysfs.get(p, "")):
            self.assertEqual(favorites_runtime._rspduo_usb_sample(), (0, []))


if __name__ == "__main__":
    unittest.main()
