import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from ui import system_stats


def _write_device(root: str, name: str, *, serial: str, speed: int = 480) -> None:
    path = os.path.join(root, name)
    os.makedirs(path, exist_ok=True)
    for filename, value in (
        ("idVendor", "0bda"),
        ("idProduct", "2838"),
        ("serial", serial),
        ("speed", str(speed)),
    ):
        with open(os.path.join(path, filename), "w", encoding="utf-8") as handle:
            handle.write(f"{value}\n")


def _fresh_event_state() -> dict:
    return {
        "initialized": False,
        "present_serials": [],
        "details_by_serial": {},
        "events": [],
    }


class DongleEventHistoryTests(unittest.TestCase):
    def test_read_rtl_dongle_health_tracks_connect_and_loss_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            sysfs_root = os.path.join(tmp, "usb")
            log_path = os.path.join(tmp, "dongle-events.jsonl")
            os.makedirs(sysfs_root, exist_ok=True)

            _write_device(sysfs_root, "1-6.3", serial="00000002")
            _write_device(sysfs_root, "1-6.4", serial="56919602")

            env = {
                "AIRBAND_RTL_SERIAL": "00000002",
                "DIGITAL_RTL_SERIAL": "56919602",
                "DIGITAL_RTL_SERIAL_SECONDARY": "49571227",
            }

            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                system_stats, "_RTL_USB_SYSFS_ROOT", sysfs_root
            ), mock.patch.object(
                system_stats, "_RTL_DONGLE_EVENT_LOG_PATH", log_path
            ), mock.patch.object(
                system_stats, "_RTL_DONGLE_EVENT_HISTORY_LIMIT", 10
            ), mock.patch.object(
                system_stats, "_RTL_DONGLE_EVENT_STATE", new=_fresh_event_state()
            ), mock.patch.object(
                system_stats.time, "time", side_effect=[1000.0, 1001.0, 1002.0, 1003.0]
            ):
                baseline = system_stats.read_rtl_dongle_health()
                self.assertEqual([], baseline["events"])
                self.assertEqual(0, baseline["last_event_time_ms"])

                _write_device(sysfs_root, "1-6.5", serial="49571227")
                after_connect = system_stats.read_rtl_dongle_health()

                self.assertEqual("connected", after_connect["events"][0]["status"])
                self.assertEqual("49571227", after_connect["events"][0]["serial"])
                self.assertEqual(1001000, after_connect["last_event_time_ms"])

                shutil.rmtree(os.path.join(sysfs_root, "1-6.4"))
                after_loss = system_stats.read_rtl_dongle_health()

                self.assertEqual("lost", after_loss["events"][0]["status"])
                self.assertEqual("56919602", after_loss["events"][0]["serial"])
                self.assertEqual("connected", after_loss["events"][1]["status"])
                self.assertEqual("49571227", after_loss["events"][1]["serial"])
                self.assertEqual(1002000, after_loss["last_event_time_ms"])

                with open(log_path, "r", encoding="utf-8") as handle:
                    persisted = [json.loads(line) for line in handle if line.strip()]

                self.assertEqual(2, len(persisted))
                self.assertEqual("connected", persisted[0]["status"])
                self.assertEqual("49571227", persisted[0]["serial"])
                self.assertEqual("lost", persisted[1]["status"])
                self.assertEqual("56919602", persisted[1]["serial"])

            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                system_stats, "_RTL_USB_SYSFS_ROOT", sysfs_root
            ), mock.patch.object(
                system_stats, "_RTL_DONGLE_EVENT_LOG_PATH", log_path
            ), mock.patch.object(
                system_stats, "_RTL_DONGLE_EVENT_HISTORY_LIMIT", 10
            ), mock.patch.object(
                system_stats, "_RTL_DONGLE_EVENT_STATE", new=_fresh_event_state()
            ), mock.patch.object(
                system_stats.time, "time", return_value=1003.0
            ):
                reloaded = system_stats.read_rtl_dongle_health()

            self.assertEqual("lost", reloaded["events"][0]["status"])
            self.assertEqual("56919602", reloaded["events"][0]["serial"])
            self.assertEqual("connected", reloaded["events"][1]["status"])
            self.assertEqual("49571227", reloaded["events"][1]["serial"])
            self.assertEqual(1002000, reloaded["last_event_time_ms"])

    def test_extra_rtl_does_not_degrade_health_when_expected_roles_are_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            sysfs_root = os.path.join(tmp, "usb")
            os.makedirs(sysfs_root, exist_ok=True)

            _write_device(sysfs_root, "1-6.1.1", serial="14306619")
            _write_device(sysfs_root, "1-6.1.2", serial="56919602")
            _write_device(sysfs_root, "1-6.1.3", serial="70613472")
            _write_device(sysfs_root, "1-6.3", serial="00000002")
            _write_device(sysfs_root, "1-6.4", serial="49571227")

            env = {
                "AIRBAND_RTL_SERIAL": "00000002",
                "GROUND_RTL_SERIAL": "70613472",
                "DIGITAL_RTL_SERIAL": "56919602",
                "DIGITAL_RTL_SERIAL_SECONDARY": "49571227",
            }

            with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                system_stats, "_RTL_USB_SYSFS_ROOT", sysfs_root
            ), mock.patch.object(
                system_stats, "_RTL_DONGLE_EVENT_STATE", new=_fresh_event_state()
            ):
                payload = system_stats.read_rtl_dongle_health()

            self.assertEqual("ideal", payload["status"])
            self.assertTrue(payload["healthy"])
            self.assertEqual([], payload["missing_expected_serials"])
            self.assertEqual(["14306619"], payload["unexpected_serials"])


if __name__ == "__main__":
    unittest.main()
