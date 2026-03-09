import unittest
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
    mgr._scheduler_system_health = {}
    mgr._scheduler_pool_system_channels = {}
    mgr._scheduler_pool_system_channels_lower = {}
    mgr._scheduler_pool_system_talkgroups = {}
    mgr._scheduler_pool_system_labels = {}
    mgr._scheduler_pool_department_labels = {}
    mgr._scheduler_pool_site_to_system = {}
    mgr._scheduler_pool_talkgroup_labels = {}
    mgr._scheduler_pool_talkgroup_groups = {}
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
        self.assertAlmostEqual(digital._DIGITAL_SCHEDULER_TICK_SEC, held_interval)

        mgr._scheduler_in_call_hold = False
        mgr._scheduler_mode = "single_system"
        single_interval = mgr._scheduler_tick_interval_sec_locked()
        self.assertAlmostEqual(digital._DIGITAL_SCHEDULER_TICK_SEC, single_interval)

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
        self.assertIsInstance(snapshot["digital_scheduler_fast_switch_enabled"], bool)
        self.assertIsInstance(snapshot["digital_scheduler_tick_interval_ms"], int)
        self.assertIsInstance(snapshot["digital_scheduler_apply_method"], str)
        self.assertIsInstance(snapshot["digital_scheduler_last_apply_duration_ms"], int)
        self.assertIsInstance(snapshot["digital_scheduler_preflight_cache_age_ms"], int)
        self.assertIsInstance(snapshot["digital_scheduler_lock_miss_ticks"], int)


if __name__ == "__main__":
    unittest.main()
