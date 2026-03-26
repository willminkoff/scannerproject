import os
import tempfile
import time
import unittest
from unittest import mock

from ui import actions
from ui import digital
from ui import handlers
from ui import scanner


_PROFILE_TEMPLATE = """airband = {airband};

devices:
({{
  type = "rtlsdr";
  index = {index};
  mode = "scan";
  gain = 32.800;  # UI_CONTROLLED
  channels:
  (
    {{
      freqs = ({freqs});
      labels = ({labels});
      squelch_threshold = -52;  # UI_CONTROLLED
    }}
  );
}});
"""


def _write_profile(path: str, *, airband: bool, freqs: list[float], labels: list[str]) -> None:
    freqs_text = ", ".join(f"{float(freq):.4f}" for freq in freqs)
    labels_text = ", ".join(f'"{label}"' for label in labels)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            _PROFILE_TEMPLATE.format(
                airband="true" if airband else "false",
                index=0 if airband else 1,
                freqs=freqs_text,
                labels=labels_text,
            )
        )


def _write_stats(path: str, counters: dict[str, int], when_ts: float) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for freq, count in counters.items():
            handle.write(
                f'channel_activity_counter{{freq="{freq}",label="Workflow {freq}"}}\t{int(count)}\n'
            )
    ns = int(when_ts * 1_000_000_000)
    os.utime(path, ns=(ns, ns))


class ProfileSwitchWorkflowTests(unittest.TestCase):
    def test_action_set_profile_updates_symlink_and_restarts_when_profile_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            current_path = os.path.join(tmp, "current_air.conf")
            next_path = os.path.join(tmp, "next_air.conf")
            active_link = os.path.join(tmp, "active_air.conf")
            _write_profile(current_path, airband=True, freqs=[118.6], labels=["Tower"])
            _write_profile(next_path, airband=True, freqs=[119.35], labels=["West"])
            os.symlink(current_path, active_link)

            profiles_airband = [
                {"id": "current", "label": "Current", "path": current_path},
                {"id": "next", "label": "Next", "path": next_path},
            ]

            with mock.patch.object(
                actions,
                "split_profiles",
                return_value=([], profiles_airband, []),
            ), mock.patch.object(
                actions,
                "CONFIG_SYMLINK",
                active_link,
            ), mock.patch.object(
                actions,
                "read_active_config_path",
                side_effect=lambda: os.path.realpath(active_link),
            ), mock.patch.object(
                actions,
                "write_combined_config",
                return_value=False,
            ) as write_combined, mock.patch.object(
                actions,
                "restart_rtl",
                return_value=(True, ""),
            ) as restart_rtl, mock.patch.object(
                actions,
                "mark_analog_hit_cutoff",
            ) as mark_cutoff:
                result = actions.action_set_profile("next", "airband", restart_service=True)
                self.assertEqual(200, result["status"])
                self.assertTrue(result["payload"]["ok"])
                self.assertTrue(result["payload"]["changed"])
                self.assertTrue(result["payload"]["profile_switched"])
                self.assertFalse(result["payload"]["combined_changed"])
                self.assertTrue(result["payload"]["restart_ok"])
                self.assertEqual(os.path.realpath(next_path), os.path.realpath(active_link))
                write_combined.assert_called_once()
                restart_rtl.assert_called_once()
                mark_cutoff.assert_called_once_with("airband")


class ScannerWorkflowTests(unittest.TestCase):
    def setUp(self):
        scanner._reset_live_analog_hit_state()

    def tearDown(self):
        scanner._reset_live_analog_hit_state()

    def test_profile_switch_clears_stale_hit_and_tracks_new_active_frequency(self):
        with tempfile.TemporaryDirectory() as tmp:
            current_path = os.path.join(tmp, "current_air.conf")
            next_path = os.path.join(tmp, "next_air.conf")
            ground_path = os.path.join(tmp, "ground.conf")
            active_link = os.path.join(tmp, "active_air.conf")
            stats_path = os.path.join(tmp, "stats.txt")
            _write_profile(current_path, airband=True, freqs=[118.6], labels=["Tower"])
            _write_profile(next_path, airband=True, freqs=[119.35], labels=["West"])
            _write_profile(ground_path, airband=False, freqs=[162.55], labels=["WX"])
            os.symlink(current_path, active_link)

            profiles_airband = [
                {"id": "current", "label": "Current", "path": current_path},
                {"id": "next", "label": "Next", "path": next_path},
            ]
            base = time.time()

            with mock.patch.object(
                actions,
                "split_profiles",
                return_value=([], profiles_airband, []),
            ), mock.patch.object(
                actions,
                "CONFIG_SYMLINK",
                active_link,
            ), mock.patch.object(
                actions,
                "read_active_config_path",
                side_effect=lambda: os.path.realpath(active_link),
            ), mock.patch.object(
                actions,
                "write_combined_config",
                return_value=False,
            ), mock.patch.object(
                scanner,
                "read_active_config_path",
                side_effect=lambda: os.path.realpath(active_link),
            ), mock.patch.object(
                scanner,
                "GROUND_CONFIG_PATH",
                ground_path,
            ), mock.patch.object(
                scanner,
                "LAST_HIT_AIRBAND_PATH",
                os.path.join(tmp, "missing-air.txt"),
            ), mock.patch.object(
                scanner,
                "LAST_HIT_GROUND_PATH",
                os.path.join(tmp, "missing-ground.txt"),
            ), mock.patch.object(
                scanner,
                "ANALOG_AUTO_SQUELCH_STATS_PATH",
                stats_path,
            ), mock.patch.object(
                scanner,
                "read_hit_list_for_unit",
                return_value=[],
            ):
                _write_stats(stats_path, {"118.6000": 0, "119.3500": 0}, base - 2.0)
                scanner.refresh_analog_hit_state(now=base - 1.8)

                _write_stats(stats_path, {"118.6000": 120, "119.3500": 0}, base - 1.0)
                scanner.refresh_analog_hit_state(now=base - 0.8)
                before_switch = scanner.read_hit_list(limit=5)

                switch_result = actions.action_set_profile("next", "airband", restart_service=False)
                after_switch = scanner.read_hit_list(limit=5)

                _write_stats(stats_path, {"118.6000": 120, "119.3500": 35}, base + 0.2)
                scanner.refresh_analog_hit_state(now=base + 0.3)
                after_new_activity = scanner.read_hit_list(limit=5)
                self.assertEqual("118.6000", before_switch[0]["freq"])
                self.assertEqual(200, switch_result["status"])
                self.assertTrue(switch_result["payload"]["profile_switched"])
                self.assertEqual([], after_switch)
                self.assertEqual(os.path.realpath(next_path), os.path.realpath(active_link))
                self.assertEqual("119.3500", after_new_activity[0]["freq"])
                self.assertTrue(all(item["freq"] != "118.6000" for item in after_new_activity))


class _DummyThread:
    def __init__(self, *args, **kwargs):
        del args, kwargs

    def start(self):
        return None


class _FakeAdapter:
    def __init__(self):
        self.active = False
        self.profile = "initial"
        self.start_calls = 0
        self.stop_calls = 0
        self.restart_calls = 0
        self.set_profile_calls: list[tuple[str, bool]] = []

    def start(self):
        self.start_calls += 1
        self.active = True
        return True, ""

    def stop(self):
        self.stop_calls += 1
        self.active = False
        return True, ""

    def restart(self):
        self.restart_calls += 1
        self.active = True
        return True, ""

    def isActive(self):
        return self.active

    def listProfiles(self):
        return [{"id": "metro"}]

    def getProfile(self):
        return self.profile

    def setProfile(self, profileId: str, *, restart_service: bool = True):
        self.profile = str(profileId)
        self.set_profile_calls.append((str(profileId), bool(restart_service)))
        return True, ""

    def getLastEvent(self):
        return {
            "label": "Dispatch",
            "tgid": "10560",
            "mode": "P25",
            "timeMs": int(time.time() * 1000),
        }

    def getLastError(self):
        return ""

    def getLastWarning(self):
        return ""

    def getRecentEvents(self, limit: int = 20):
        del limit
        return [self.getLastEvent()]

    def preflight(self):
        return {
            "tuner_busy": False,
            "control_decode_available": True,
            "control_channel_locked": True,
            "control_activity_count": 3,
            "control_last_time_ms": int(time.time() * 1000),
            "control_sync_loss_count": 0,
            "control_lock_fail_count": 0,
            "control_lock_fail_last_time_ms": 0,
            "control_window_ms": 120000,
            "control_decode_files": 1,
            "playlist_source_ok": True,
        }

    def runtime_retune_available(self):
        return True

    def runtime_metrics(self):
        return {
            "profile_apply_last_duration_ms": 15,
            "profile_apply_last_changed": True,
            "retune_last_duration_ms": 7,
            "retune_last_method": "runtime",
            "retune_last_changed": True,
        }


class DigitalLifecycleWorkflowTests(unittest.TestCase):
    def test_digital_manager_lifecycle_uses_adapter_and_reports_status(self):
        adapter = _FakeAdapter()
        with mock.patch.object(
            digital.DigitalManager,
            "_build_adapter",
            return_value=adapter,
        ), mock.patch.object(
            digital.DigitalManager,
            "_load_scheduler_state",
            lambda self: None,
        ), mock.patch.object(
            digital.DigitalManager,
            "_refresh_super_profile_systems",
            lambda self, profile_id="": None,
        ), mock.patch.object(
            digital.DigitalManager,
            "_ensure_super_profile_seed",
            lambda self, profile_id="", force=False: None,
        ), mock.patch.object(
            digital.DigitalManager,
            "_scheduler_tick",
            lambda self: None,
        ), mock.patch.object(
            digital.threading,
            "Thread",
            _DummyThread,
        ), mock.patch.object(
            digital,
            "_digital_tuner_runtime_health",
            return_value={"ready": True, "missing_serials": [], "slow_serials": []},
        ):
            manager = digital.DigitalManager(backend="sdrtrunk")
            manager._scheduler_snapshot = {"digital_scheduler_mode": "single_system"}
            manager._scheduler_snapshot_at_ms = int(time.time() * 1000)

            start_ok, start_err = manager.start()
            set_ok, set_err = manager.setProfile("metro", restart_service=True)
            payload = manager.status_payload()
            restart_ok, restart_err = manager.restart()
            stop_ok, stop_err = manager.stop()

        self.assertTrue(start_ok)
        self.assertEqual("", start_err)
        self.assertTrue(set_ok)
        self.assertEqual("", set_err)
        self.assertTrue(restart_ok)
        self.assertEqual("", restart_err)
        self.assertTrue(stop_ok)
        self.assertEqual("", stop_err)
        self.assertEqual(1, adapter.start_calls)
        self.assertEqual(1, adapter.restart_calls)
        self.assertEqual(1, adapter.stop_calls)
        self.assertEqual([("metro", True)], adapter.set_profile_calls)
        self.assertTrue(payload["digital_active"])
        self.assertEqual("sdrtrunk", payload["digital_backend"])
        self.assertEqual("metro", payload["digital_profile"])
        self.assertEqual("Dispatch", payload["digital_last_label"])
        self.assertEqual("10560", payload["digital_last_tgid"])
        self.assertEqual("P25", payload["digital_last_mode"])
        self.assertEqual([], payload["digital_tuner_missing_serials"])


class AnalogControlsSnapshotTests(unittest.TestCase):
    def test_effective_analog_controls_snapshot_is_dbfs_only(self):
        with mock.patch.object(
            handlers,
            "resolve_controls_path",
            side_effect=["/tmp/air.conf", "/tmp/ground.conf"],
        ), mock.patch.object(
            handlers,
            "parse_controls",
            side_effect=[(32.8, 10.0, -52.0, "dbfs"), (28.0, 10.0, -70.0, "dbfs")],
        ):
            snapshot = handlers._read_effective_analog_controls()

        self.assertEqual("/tmp/air.conf", snapshot["controls_airband_path"])
        self.assertEqual("/tmp/ground.conf", snapshot["controls_ground_path"])
        self.assertEqual(-52.0, snapshot["airband_dbfs"])
        self.assertEqual(-70.0, snapshot["ground_dbfs"])
        self.assertEqual("dbfs", snapshot["airband_mode"])
        self.assertEqual("dbfs", snapshot["ground_mode"])
        self.assertNotIn("airband_snr", snapshot)
        self.assertNotIn("ground_snr", snapshot)


if __name__ == "__main__":
    unittest.main()
