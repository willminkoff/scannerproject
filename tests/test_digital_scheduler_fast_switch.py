import unittest
import threading
from unittest import mock

from ui import digital


def _make_manager() -> digital.DigitalManager:
    mgr = digital.DigitalManager.__new__(digital.DigitalManager)
    mgr._adapter = mock.Mock()
    mgr._adapter.isActive.return_value = True
    mgr._adapter.getProfile.return_value = "p1"
    mgr._super_profile_mode = False
    mgr._scheduler_mode = "single_system"
    mgr._scheduler_systems = []
    mgr._scheduler_order = []
    mgr._scheduler_profile = ""
    mgr._scheduler_active_system = ""
    mgr._scheduler_last_switch_time_ms = 0
    mgr._scheduler_switch_reason = "manual"
    mgr._scheduler_in_call_hold = False
    mgr._scheduler_last_applied_system = ""
    mgr._scheduler_last_apply_time_ms = 0
    mgr._scheduler_last_apply_attempt_ms = 0
    mgr._scheduler_last_apply_error = ""
    mgr._scheduler_last_apply_error_system = ""
    mgr._scheduler_last_apply_method = ""
    mgr._scheduler_last_apply_duration_ms = 0
    mgr._scheduler_lock_loss_ms = 2500
    mgr._scheduler_dwell_ms = 900
    mgr._scheduler_hang_ms = 4000
    mgr._scheduler_pause_on_hit = True
    mgr._scheduler_perf_profile = "legacy"
    mgr._scheduler_env_overrides = {
        "base_tick_sec": False,
        "fast_switch_enabled": False,
        "fast_tick_sec": False,
        "fast_lock_timeout_ms": False,
        "preflight_cache_ms": False,
        "lock_miss_ticks": False,
    }
    mgr._scheduler_base_tick_sec = digital._DIGITAL_SCHEDULER_TICK_SEC
    mgr._scheduler_fast_switch_enabled = False
    mgr._scheduler_fast_tick_sec = 0.25
    mgr._scheduler_fast_lock_timeout_ms = 1200
    mgr._scheduler_preflight_cache_ttl_ms = 750
    mgr._scheduler_lock_miss_required_ticks = 3
    mgr._scheduler_lock_miss_ticks = 0
    mgr._scheduler_lock_miss_system = ""
    mgr._scheduler_last_tick_interval_ms = 1000
    mgr._scheduler_cached_preflight = {}
    mgr._scheduler_cached_preflight_at_ms = 0
    mgr._scheduler_last_preflight_cache_age_ms = 0
    mgr._scheduler_snapshot = {}
    mgr._scheduler_snapshot_at_ms = 0
    mgr._status_snapshot_enabled = False
    mgr._preflight_snapshot = {}
    mgr._preflight_snapshot_at_ms = 0
    mgr._scheduler_system_health = {}
    mgr._scheduler_pool_system_channels = {}
    mgr._scheduler_pool_system_channels_lower = {}
    mgr._scheduler_pool_system_talkgroups = {}
    mgr._scheduler_pool_system_labels = {}
    mgr._scheduler_pool_department_labels = {}
    mgr._scheduler_pool_site_to_system = {}
    mgr._scheduler_pool_talkgroup_labels = {}
    mgr._scheduler_pool_talkgroup_groups = {}
    mgr._scheduler_lock = threading.Lock()
    mgr._scheduler_health_entry = digital.DigitalManager._scheduler_health_entry.__get__(
        mgr, digital.DigitalManager
    )
    return mgr


class SchedulerFastSwitchTests(unittest.TestCase):
    def test_apply_scheduler_target_flag_off_uses_playlist_path(self):
        mgr = _make_manager()
        mgr._scheduler_fast_switch_enabled = False
        mgr._scheduler_mode = "timeslice_multi_system"
        mgr._scheduler_systems = ["alpha", "bravo"]

        with mock.patch.object(
            mgr, "_apply_scheduler_fast_retune", return_value=(True, "", True)
        ) as fast_apply, mock.patch.object(
            mgr, "_apply_scheduler_system", return_value=(True, "", True)
        ) as playlist_apply:
            ok, err, changed = mgr._apply_scheduler_target("p1", "alpha", force=True)

        self.assertTrue(ok)
        self.assertEqual("", err)
        self.assertTrue(changed)
        fast_apply.assert_not_called()
        playlist_apply.assert_called_once_with("p1", "alpha", force=True)

    def test_apply_scheduler_target_flag_on_uses_fast_path(self):
        mgr = _make_manager()
        mgr._scheduler_fast_switch_enabled = True
        mgr._scheduler_mode = "timeslice_multi_system"
        mgr._scheduler_systems = ["alpha", "bravo"]

        with mock.patch.object(
            mgr, "_apply_scheduler_fast_retune", return_value=(True, "", True)
        ) as fast_apply, mock.patch.object(
            mgr, "_apply_scheduler_system", return_value=(True, "", True)
        ) as playlist_apply:
            ok, err, changed = mgr._apply_scheduler_target("p1", "alpha", force=True)

        self.assertTrue(ok)
        self.assertEqual("", err)
        self.assertTrue(changed)
        fast_apply.assert_called_once_with("p1", "alpha", force=True)
        playlist_apply.assert_not_called()

    def test_fast_retune_falls_back_to_playlist_apply(self):
        mgr = _make_manager()
        mgr._adapter = mock.Mock()
        mgr._adapter.retune_control_frequency.return_value = (False, "retune failed")
        mgr._resolve_scheduler_system_control_channels = mock.Mock(return_value=[769_831_250])
        mgr._apply_scheduler_system = mock.Mock(return_value=(True, "", True))

        ok, err, changed = mgr._apply_scheduler_fast_retune("p1", "alpha", force=True)

        self.assertTrue(ok)
        self.assertEqual("", err)
        self.assertTrue(changed)
        mgr._adapter.retune_control_frequency.assert_called_once()
        mgr._apply_scheduler_system.assert_called_once_with("p1", "alpha", force=True)
        self.assertEqual("fast_retune_fallback_playlist", mgr._scheduler_last_apply_method)

    def test_adaptive_tick_interval_only_fast_in_multisystem_without_hold(self):
        mgr = _make_manager()
        mgr._scheduler_fast_switch_enabled = True
        mgr._scheduler_mode = "timeslice_multi_system"
        mgr._scheduler_systems = ["alpha", "bravo"]
        mgr._scheduler_in_call_hold = False

        fast_interval = mgr._scheduler_tick_interval_sec_locked()
        self.assertAlmostEqual(mgr._scheduler_fast_tick_sec, fast_interval)

        mgr._scheduler_in_call_hold = True
        held_interval = mgr._scheduler_tick_interval_sec_locked()
        self.assertAlmostEqual(mgr._scheduler_base_tick_sec, held_interval)

        mgr._scheduler_in_call_hold = False
        mgr._scheduler_mode = "single_system"
        single_interval = mgr._scheduler_tick_interval_sec_locked()
        self.assertAlmostEqual(mgr._scheduler_base_tick_sec, single_interval)

    def test_lock_timeout_hysteresis_requires_consecutive_misses(self):
        mgr = _make_manager()
        mgr._scheduler_fast_switch_enabled = True
        mgr._scheduler_mode = "timeslice_multi_system"
        mgr._scheduler_systems = ["alpha", "bravo"]
        mgr._scheduler_profile = "p1"
        mgr._scheduler_active_system = "alpha"
        mgr._scheduler_pause_on_hit = False
        mgr._scheduler_last_applied_system = "alpha"
        mgr._scheduler_last_switch_time_ms = 0
        mgr.getProfile = mock.Mock(return_value="p1")
        mgr._discover_scheduler_systems = mock.Mock(return_value=["alpha", "bravo"])
        mgr._apply_scheduler_target_timed = mock.Mock(
            side_effect=lambda _pid, sysname, force=True: (
                setattr(mgr, "_scheduler_last_applied_system", sysname) or True,
                "",
                True,
            )
        )

        with mock.patch.object(digital.time, "time", return_value=5.0), mock.patch.object(
            digital, "get_current_scan_mode", return_value="profile"
        ):
            preflight = {"control_decode_available": True, "control_channel_locked": False, "tuner_busy": False}
            payload1 = mgr._scheduler_payload({}, preflight)
            payload2 = mgr._scheduler_payload({}, preflight)
            payload3 = mgr._scheduler_payload({}, preflight)

        self.assertEqual("alpha", payload1["digital_scheduler_active_system"])
        self.assertEqual("alpha", payload2["digital_scheduler_active_system"])
        self.assertEqual("bravo", payload3["digital_scheduler_active_system"])
        self.assertEqual("lock_timeout", payload3["digital_scheduler_switch_reason"])
        self.assertGreaterEqual(payload3["digital_scheduler_lock_miss_ticks"], 3)

    def test_fast_lock_timeout_scales_for_large_control_channel_sets(self):
        mgr = _make_manager()
        mgr._scheduler_fast_switch_enabled = True
        mgr._scheduler_mode = "timeslice_multi_system"
        mgr._scheduler_systems = ["alpha", "bravo"]
        mgr._scheduler_fast_lock_timeout_ms = 1000
        mgr._resolve_scheduler_system_control_channels = mock.Mock(
            return_value=[769_831_250, 770_081_250, 770_331_250, 770_581_250]
        )

        with mock.patch.object(digital, "_DIGITAL_SOURCE_ROTATION_DELAY_MS", 250):
            timeout = mgr._scheduler_lock_timeout_ms_locked(
                "timeslice_multi_system",
                ["alpha", "bravo"],
                profile_id="p1",
                active_system="alpha",
            )

        self.assertEqual(1700, timeout)

    def test_dwell_does_not_preempt_lock_acquisition(self):
        mgr = _make_manager()
        mgr._scheduler_fast_switch_enabled = False
        mgr._scheduler_mode = "timeslice_multi_system"
        mgr._scheduler_systems = ["alpha", "bravo"]
        mgr._scheduler_profile = "p1"
        mgr._scheduler_active_system = "alpha"
        mgr._scheduler_pause_on_hit = False
        mgr._scheduler_last_applied_system = "alpha"
        mgr._scheduler_dwell_ms = 900
        mgr._scheduler_lock_loss_ms = 2500
        mgr._scheduler_last_switch_time_ms = 0
        mgr.getProfile = mock.Mock(return_value="p1")
        mgr._discover_scheduler_systems = mock.Mock(return_value=["alpha", "bravo"])
        mgr._apply_scheduler_target_timed = mock.Mock(return_value=(True, "", False))

        with mock.patch.object(digital.time, "time", return_value=1.2), mock.patch.object(
            digital, "get_current_scan_mode", return_value="profile"
        ):
            payload = mgr._scheduler_payload(
                {},
                {"control_decode_available": True, "control_channel_locked": False, "tuner_busy": False},
            )

        self.assertEqual("alpha", payload["digital_scheduler_active_system"])
        self.assertGreaterEqual(payload["digital_scheduler_lock_timeout_ms"], 2000)

    def test_scheduler_preflight_cache_respects_ttl(self):
        mgr = _make_manager()
        mgr._fresh_preflight = mock.Mock(side_effect=[{"marker": 1}, {"marker": 2}])

        with mock.patch.object(digital.time, "time", side_effect=[1000.0, 1000.2, 1001.0]):
            first = mgr._scheduler_preflight()
            second = mgr._scheduler_preflight()
            third = mgr._scheduler_preflight()

        self.assertEqual({"marker": 1}, first)
        self.assertEqual({"marker": 1}, second)
        self.assertEqual({"marker": 2}, third)
        self.assertEqual(2, mgr._fresh_preflight.call_count)

    def test_scheduler_preflight_uses_snapshot_sampler_when_enabled(self):
        mgr = _make_manager()
        mgr._status_snapshot_enabled = True
        mgr._preflight_snapshot = {"marker": 9}
        mgr._preflight_snapshot_at_ms = 1000

        with mock.patch.object(digital.time, "time", return_value=1.5):
            payload = mgr._scheduler_preflight()

        self.assertEqual({"marker": 9}, payload)
        self.assertEqual(500, mgr._scheduler_last_preflight_cache_age_ms)

    def test_get_scheduler_read_path_does_not_overwrite_cached_snapshot(self):
        mgr = _make_manager()
        mgr._scheduler_snapshot = {"digital_scheduler_mode": "single_system"}
        mgr._scheduler_snapshot_at_ms = 1000
        mgr._preflight_snapshot_at_ms = 900
        mgr.getLastEvent = mock.Mock(return_value={})
        mgr._status_preflight_snapshot = mock.Mock(return_value={})

        with mock.patch.object(digital.time, "time", return_value=2.0):
            payload = mgr.getScheduler()

        self.assertEqual({"digital_scheduler_mode": "single_system"}, mgr._scheduler_snapshot)
        self.assertEqual(1000, mgr._scheduler_snapshot_at_ms)
        self.assertTrue(payload.get("ok"))
        self.assertEqual("single_system", payload.get("digital_scheduler_mode"))

    def test_get_scheduler_fallback_payload_is_not_persisted(self):
        mgr = _make_manager()
        mgr.getLastEvent = mock.Mock(return_value={})
        mgr._status_preflight_snapshot = mock.Mock(return_value={})
        mgr._scheduler_snapshot_payload_locked = mock.Mock(
            return_value={"digital_scheduler_mode": "single_system"}
        )

        with mock.patch.object(digital.time, "time", return_value=2.0):
            payload = mgr.getScheduler()

        self.assertEqual({}, mgr._scheduler_snapshot)
        self.assertEqual(0, mgr._scheduler_snapshot_at_ms)
        self.assertTrue(payload.get("ok"))
        self.assertEqual("single_system", payload.get("digital_scheduler_mode"))
        mgr._scheduler_snapshot_payload_locked.assert_called_once()

    def test_scheduler_snapshot_includes_fast_switch_telemetry_fields(self):
        mgr = _make_manager()
        mgr._scheduler_fast_switch_enabled = True
        mgr._scheduler_mode = "timeslice_multi_system"
        mgr._scheduler_systems = ["alpha", "bravo"]
        mgr._scheduler_active_system = "alpha"
        mgr._scheduler_last_switch_time_ms = 1700000000000
        mgr._scheduler_last_applied_system = "alpha"
        mgr._scheduler_last_apply_time_ms = 1700000000100
        mgr._scheduler_last_apply_method = "fast_retune"
        mgr._scheduler_last_apply_duration_ms = 27
        mgr._scheduler_last_preflight_cache_age_ms = 143
        mgr._scheduler_last_tick_interval_ms = 250
        mgr._scheduler_lock_miss_ticks = 2

        snapshot = mgr._scheduler_status_snapshot_locked({}, {"tuner_busy": False})

        self.assertIn("digital_scheduler_fast_switch_enabled", snapshot)
        self.assertIn("digital_scheduler_tick_interval_ms", snapshot)
        self.assertIn("digital_scheduler_apply_method", snapshot)
        self.assertIn("digital_scheduler_last_apply_duration_ms", snapshot)
        self.assertIn("digital_scheduler_preflight_cache_age_ms", snapshot)
        self.assertIn("digital_scheduler_lock_miss_ticks", snapshot)
        self.assertIn("digital_scheduler_adaptive_lock_timeout_ms", snapshot)
        self.assertIn("digital_scheduler_active_control_channel_count", snapshot)
        self.assertIn("digital_scheduler_perf_profile", snapshot)
        self.assertIn("digital_scheduler_effective", snapshot)
        self.assertIsInstance(snapshot["digital_scheduler_fast_switch_enabled"], bool)
        self.assertIsInstance(snapshot["digital_scheduler_tick_interval_ms"], int)
        self.assertIsInstance(snapshot["digital_scheduler_apply_method"], str)
        self.assertIsInstance(snapshot["digital_scheduler_last_apply_duration_ms"], int)
        self.assertIsInstance(snapshot["digital_scheduler_preflight_cache_age_ms"], int)
        self.assertIsInstance(snapshot["digital_scheduler_lock_miss_ticks"], int)
        self.assertIsInstance(snapshot["digital_scheduler_adaptive_lock_timeout_ms"], int)
        self.assertIsInstance(snapshot["digital_scheduler_active_control_channel_count"], int)
        self.assertIsInstance(snapshot["digital_scheduler_perf_profile"], str)
        self.assertIsInstance(snapshot["digital_scheduler_effective"], dict)

    def test_set_scheduler_accepts_performance_profile(self):
        mgr = _make_manager()
        mgr._scheduler_lock = mock.MagicMock()
        mgr._scheduler_lock.__enter__.return_value = mgr._scheduler_lock
        mgr._scheduler_lock.__exit__.return_value = False
        mgr._write_scheduler_state = mock.Mock()
        mgr.getScheduler = mock.Mock(return_value={"ok": True})

        ok, err, snapshot = mgr.setScheduler({"performance_profile": "pc_moderate"})

        self.assertTrue(ok)
        self.assertEqual("", err)
        self.assertEqual({"ok": True}, snapshot)
        self.assertEqual("pc_moderate", mgr._scheduler_perf_profile)
        self.assertTrue(mgr._scheduler_fast_switch_enabled)
        self.assertEqual(0.25, mgr._scheduler_fast_tick_sec)
        self.assertEqual(1000, mgr._scheduler_fast_lock_timeout_ms)
        self.assertEqual(300, mgr._scheduler_preflight_cache_ttl_ms)
        self.assertEqual(2, mgr._scheduler_lock_miss_required_ticks)
        self.assertEqual(0.75, mgr._scheduler_base_tick_sec)

    def test_set_scheduler_rejects_invalid_performance_profile(self):
        mgr = _make_manager()
        mgr._scheduler_lock = mock.MagicMock()
        mgr._scheduler_lock.__enter__.return_value = mgr._scheduler_lock
        mgr._scheduler_lock.__exit__.return_value = False
        mgr._write_scheduler_state = mock.Mock()

        ok, err, snapshot = mgr.setScheduler({"performance_profile": "nope"})

        self.assertFalse(ok)
        self.assertEqual("invalid performance_profile", err)
        self.assertEqual({}, snapshot)
        mgr._write_scheduler_state.assert_not_called()

    def test_perf_profile_respects_explicit_env_overrides(self):
        mgr = _make_manager()
        mgr._scheduler_env_overrides = {
            "base_tick_sec": True,
            "fast_switch_enabled": True,
            "fast_tick_sec": True,
            "fast_lock_timeout_ms": True,
            "preflight_cache_ms": True,
            "lock_miss_ticks": True,
        }
        mgr._scheduler_base_tick_sec = 0.99
        mgr._scheduler_fast_switch_enabled = False
        mgr._scheduler_fast_tick_sec = 0.42
        mgr._scheduler_fast_lock_timeout_ms = 4321
        mgr._scheduler_preflight_cache_ttl_ms = 654
        mgr._scheduler_lock_miss_required_ticks = 9

        mgr._apply_scheduler_perf_profile_locked("pc_moderate")

        self.assertEqual(0.99, mgr._scheduler_base_tick_sec)
        self.assertFalse(mgr._scheduler_fast_switch_enabled)
        self.assertEqual(0.42, mgr._scheduler_fast_tick_sec)
        self.assertEqual(4321, mgr._scheduler_fast_lock_timeout_ms)
        self.assertEqual(654, mgr._scheduler_preflight_cache_ttl_ms)
        self.assertEqual(9, mgr._scheduler_lock_miss_required_ticks)


if __name__ == "__main__":
    unittest.main()
