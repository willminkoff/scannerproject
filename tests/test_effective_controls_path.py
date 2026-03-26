import json
import os
import sqlite3
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

import combined_config
from ui import actions
from ui import digital
from ui import favorites_runtime
from ui import handlers
from ui.hp_state import HPState
from ui import managed_analog_controls
from ui import profile_config
from ui import scan_mode_controller


def _write_profile(path, *, airband, ui_disabled=False, with_devices=True):
    lines = [
        f"airband = {'true' if airband else 'false'};",
        f"ui_disabled = {'true' if ui_disabled else 'false'};",
    ]
    if with_devices:
        lines.extend(
            [
                "devices:",
                "(",
                "  {",
                "    type = \"rtlsdr\";",
                "    index = 0;",
                "  }",
                ");",
            ]
        )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _write_runtime_profile(path, *, airband, freqs, labels, squelch_dbfs, gain=32.8):
    freqs_text = ", ".join(f"{float(freq):.4f}" for freq in freqs)
    labels_text = ", ".join(f'"{label}"' for label in labels)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            "\n".join(
                [
                    f"airband = {'true' if airband else 'false'};",
                    "",
                    "devices:",
                    "({",
                    '  type = "rtlsdr";',
                    f"  index = {0 if airband else 1};",
                    '  mode = "scan";',
                    f"  gain = {float(gain):.3f};  # UI_CONTROLLED",
                    "",
                    "  channels:",
                    "  (",
                    "    {",
                    f"      freqs = ({freqs_text});",
                    f"      labels = ({labels_text});",
                    f'      modulation = {"\"am\"" if airband else "\"nfm\""};',
                    f"      squelch_threshold = {int(round(float(squelch_dbfs)))};  # UI_CONTROLLED",
                    "    }",
                    "  );",
                    "});",
                    "",
                ]
            )
        )


class EffectiveControlsPathTests(unittest.TestCase):
    def test_resolve_controls_path_uses_effective_ground_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            selected_air = os.path.join(tmp, "selected_air.conf")
            selected_ground = os.path.join(tmp, "selected_ground.conf")
            fallback_air = os.path.join(tmp, "fallback_air.conf")
            fallback_ground = os.path.join(tmp, "fallback_ground.conf")

            # Simulate "none_*" selected profiles that are UI-disabled.
            _write_profile(selected_air, airband=True, ui_disabled=True, with_devices=False)
            _write_profile(selected_ground, airband=False, ui_disabled=True, with_devices=False)
            _write_profile(fallback_air, airband=True, ui_disabled=False, with_devices=True)
            _write_profile(fallback_ground, airband=False, ui_disabled=False, with_devices=True)

            with mock.patch.object(profile_config, "AIRBAND_FALLBACK_PROFILE_PATH", fallback_air), mock.patch.object(
                profile_config, "GROUND_FALLBACK_PROFILE_PATH", fallback_ground
            ), mock.patch.object(profile_config, "GROUND_CONFIG_PATH", selected_ground), mock.patch.object(
                profile_config, "read_active_config_path", return_value=selected_air
            ):
                resolved = profile_config.resolve_controls_path("ground")

            self.assertEqual(os.path.realpath(fallback_ground), resolved)

    def test_action_apply_controls_writes_to_resolved_path(self):
        with mock.patch.object(actions, "resolve_controls_path", return_value="/tmp/effective.conf") as resolve_path, mock.patch.object(
            actions, "write_controls", return_value=True
        ) as write_controls, mock.patch.object(
            actions, "persist_managed_controls_override", return_value=False
        ) as persist_override, mock.patch.object(
            actions, "write_combined_config", return_value=False
        ), mock.patch.object(
            actions, "restart_rtl", return_value=(True, "")
        ):
            result = actions.action_apply_controls("ground", 43.4, "dbfs", 10.0, -64.0)

        self.assertEqual(200, result["status"])
        self.assertTrue(result["payload"]["ok"])
        resolve_path.assert_called_once_with("ground")
        write_controls.assert_called_once_with("/tmp/effective.conf", 43.4, "dbfs", 10.0, -64.0)
        persist_override.assert_called_once_with(
            "ground",
            "/tmp/effective.conf",
            gain=43.4,
            squelch_mode="dbfs",
            squelch_snr=10.0,
            squelch_dbfs=-64.0,
        )

    def test_resolve_controls_path_prefers_managed_profile_when_active_is_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            selected_air = os.path.join(tmp, "selected_air.conf")
            selected_ground = os.path.join(tmp, "selected_ground.conf")
            fallback_air = os.path.join(tmp, "fallback_air.conf")
            managed_air = os.path.join(tmp, "managed_air.conf")

            _write_profile(selected_air, airband=True, ui_disabled=True, with_devices=False)
            _write_profile(selected_ground, airband=False, ui_disabled=False, with_devices=True)
            _write_profile(fallback_air, airband=True, ui_disabled=False, with_devices=True)
            _write_profile(managed_air, airband=True, ui_disabled=False, with_devices=True)

            with mock.patch.object(profile_config, "AIRBAND_FALLBACK_PROFILE_PATH", fallback_air), mock.patch.object(
                profile_config, "GROUND_CONFIG_PATH", selected_ground
            ), mock.patch.object(
                profile_config, "read_active_config_path", return_value=selected_air
            ), mock.patch.object(
                profile_config,
                "load_profiles_registry",
                return_value=[
                    {
                        "id": "hp3_favorites_airband",
                        "label": "HP3 Favorites Airband",
                        "path": managed_air,
                        "airband": True,
                    }
                ],
            ):
                resolved = profile_config.resolve_controls_path("airband")

            self.assertEqual(os.path.realpath(managed_air), resolved)

    def test_action_apply_batch_writes_to_resolved_path(self):
        with mock.patch.object(actions, "resolve_controls_path", return_value="/tmp/effective.conf") as resolve_path, mock.patch.object(
            actions, "write_controls", return_value=True
        ) as write_controls, mock.patch.object(
            actions, "persist_managed_controls_override", return_value=False
        ) as persist_override, mock.patch.object(
            actions, "write_filter", return_value=False
        ), mock.patch.object(
            actions, "write_combined_config", return_value=True
        ), mock.patch.object(
            actions, "restart_rtl", return_value=(True, "")
        ):
            result = actions.action_apply_batch("airband", 29.7, "dbfs", 10.0, -72.0, 3500)

        self.assertEqual(200, result["status"])
        self.assertTrue(result["payload"]["ok"])
        resolve_path.assert_called_once_with("airband")
        write_controls.assert_called_once_with("/tmp/effective.conf", 29.7, "dbfs", 10.0, -72.0)
        persist_override.assert_called_once_with(
            "airband",
            "/tmp/effective.conf",
            gain=29.7,
            squelch_mode="dbfs",
            squelch_snr=10.0,
            squelch_dbfs=-72.0,
        )

    def test_action_set_profile_restarts_when_symlink_changes_without_combined_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            current_path = os.path.join(tmp, "current_air.conf")
            next_path = os.path.join(tmp, "next_air.conf")
            _write_runtime_profile(
                current_path,
                airband=True,
                freqs=[118.6],
                labels=["Tower"],
                squelch_dbfs=-52,
            )
            _write_runtime_profile(
                next_path,
                airband=True,
                freqs=[118.6],
                labels=["Tower"],
                squelch_dbfs=-52,
            )
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
                "read_active_config_path",
                return_value=current_path,
            ), mock.patch.object(
                actions,
                "set_profile",
                return_value=(True, True),
            ) as set_profile_mock, mock.patch.object(
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
        self.assertFalse(result["payload"]["restart_skipped"])
        self.assertTrue(result["payload"]["restart_ok"])
        set_profile_mock.assert_called_once()
        write_combined.assert_called_once()
        restart_rtl.assert_called_once()
        mark_cutoff.assert_called_once_with("airband")

    def test_action_auto_squelch_applies_noise_based_dbfs(self):
        noise_samples = {
            "118.600": [-52.0, -51.0],
            "119.350": [-50.5],
            "146.460": [-55.4, -54.6],
        }
        with mock.patch.object(actions, "_collect_dbfs_noise_samples", return_value=noise_samples), mock.patch.object(
            actions,
            "_load_target_profile_freqs",
            side_effect=[
                ("/tmp/airband.conf", [118.6, 119.35]),
                ("/tmp/ground.conf", [146.46]),
            ],
        ), mock.patch.object(
            actions,
            "parse_controls",
            side_effect=[(29.7, 10.0, -46.0, "dbfs"), (28.0, 10.0, -51.0, "dbfs")],
        ), mock.patch.object(
            actions, "write_controls", side_effect=[True, True]
        ) as write_controls, mock.patch.object(
            actions, "persist_managed_controls_override", return_value=False
        ) as persist_override, mock.patch.object(
            actions, "write_combined_config", return_value=True
        ) as write_combined, mock.patch.object(
            actions, "restart_rtl", return_value=(True, "")
        ) as restart_rtl, mock.patch.object(
            actions, "ANALOG_AUTO_SQUELCH_SAMPLE_SEC", 2
        ), mock.patch.object(
            actions, "ANALOG_AUTO_SQUELCH_MARGIN_DB", 4.0
        ), mock.patch.object(
            actions, "ANALOG_AUTO_SQUELCH_MIN_DBFS", -95.0
        ), mock.patch.object(
            actions, "ANALOG_AUTO_SQUELCH_MAX_DBFS", -1.0
        ):
            result = actions.action_auto_squelch(["airband", "ground"])

        self.assertEqual(200, result["status"])
        payload = result["payload"]
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["changed"])
        self.assertEqual(-47.0, payload["targets"]["airband"]["applied_squelch_dbfs"])
        self.assertEqual(-51.0, payload["targets"]["ground"]["applied_squelch_dbfs"])
        self.assertEqual(
            [
                mock.call("/tmp/airband.conf", 29.7, "dbfs", 10.0, -47.0),
                mock.call("/tmp/ground.conf", 28.0, "dbfs", 10.0, -51.0),
            ],
            write_controls.call_args_list,
        )
        self.assertEqual(
            [
                mock.call(
                    "airband",
                    "/tmp/airband.conf",
                    gain=29.7,
                    squelch_mode="dbfs",
                    squelch_snr=10.0,
                    squelch_dbfs=-47.0,
                ),
                mock.call(
                    "ground",
                    "/tmp/ground.conf",
                    gain=28.0,
                    squelch_mode="dbfs",
                    squelch_snr=10.0,
                    squelch_dbfs=-51.0,
                ),
            ],
            persist_override.call_args_list,
        )
        write_combined.assert_called_once()
        restart_rtl.assert_called_once()

    def test_action_auto_squelch_reports_missing_noise_metrics(self):
        with mock.patch.object(actions, "_collect_dbfs_noise_samples", return_value={}):
            result = actions.action_auto_squelch(["airband"])

        self.assertEqual(503, result["status"])
        self.assertFalse(result["payload"]["ok"])
        self.assertIn("no noise metrics", result["payload"]["error"])

    def test_handlers_control_snapshot_reads_from_resolved_paths(self):
        with mock.patch.object(
            handlers,
            "resolve_controls_path",
            side_effect=["/tmp/effective-air.conf", "/tmp/effective-ground.conf"],
        ) as resolve_path, mock.patch.object(
            handlers,
            "parse_controls",
            side_effect=[(32.8, 10.0, -46.0, "dbfs"), (33.8, 10.0, -70.0, "dbfs")],
        ) as parse_controls:
            snapshot = handlers._read_effective_analog_controls()

        self.assertEqual("/tmp/effective-air.conf", snapshot["controls_airband_path"])
        self.assertEqual("/tmp/effective-ground.conf", snapshot["controls_ground_path"])
        self.assertEqual(32.8, snapshot["airband_gain"])
        self.assertEqual(-70.0, snapshot["ground_dbfs"])
        self.assertEqual(
            [mock.call("airband"), mock.call("ground")],
            resolve_path.call_args_list,
        )
        self.assertEqual(
            [mock.call("/tmp/effective-air.conf"), mock.call("/tmp/effective-ground.conf")],
            parse_controls.call_args_list,
        )


class ManagedAnalogControlsTests(unittest.TestCase):
    def test_favorites_runtime_applies_default_airband_squelch_without_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            air_path = os.path.join(tmp, "managed_air.conf")
            ground_path = os.path.join(tmp, "managed_ground.conf")
            state_path = os.path.join(tmp, "managed_controls.json")
            _write_runtime_profile(
                air_path,
                airband=True,
                freqs=[118.4],
                labels=["East"],
                squelch_dbfs=-60,
            )
            _write_runtime_profile(
                ground_path,
                airband=False,
                freqs=[162.55],
                labels=["WX"],
                squelch_dbfs=-70,
            )
            profiles = [
                {
                    "id": "hp3_favorites_airband",
                    "label": "HP3 Favorites Airband",
                    "path": air_path,
                    "airband": True,
                },
                {
                    "id": "hp3_favorites_ground",
                    "label": "HP3 Favorites Ground",
                    "path": ground_path,
                    "airband": False,
                },
            ]

            def _ensure(_profiles, *, profile_id, label, airband):
                del label, airband
                return favorites_runtime.find_profile(profiles, profile_id), False

            with mock.patch.object(
                favorites_runtime, "load_profiles_registry", return_value=profiles
            ), mock.patch.object(
                favorites_runtime, "_ensure_managed_profile", side_effect=_ensure
            ), mock.patch.object(
                favorites_runtime, "_switch_profile_if_needed", return_value=(False, "")
            ), mock.patch.object(
                favorites_runtime, "write_combined_config", return_value=False
            ), mock.patch.object(
                managed_analog_controls, "MANAGED_ANALOG_CONTROLS_PATH", state_path
            ), mock.patch.object(
                managed_analog_controls,
                "managed_controls_profile_path",
                side_effect=lambda target: air_path if target == "airband" else ground_path,
            ):
                result = favorites_runtime.sync_scan_pool_to_analog_runtime(
                    force=True,
                    mode="hp",
                    pool={
                        "trunked_sites": [],
                        "conventional": [
                            {"frequency": 118.6, "alpha_tag": "Tower"},
                        ],
                    },
                )

            self.assertTrue(result["ok"])
            self.assertEqual("default", result["profile_controls_source"]["airband"])
            self.assertEqual(-52.0, profile_config.parse_controls(air_path)[2])

    def test_manual_managed_override_persists_across_runtime_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            air_path = os.path.join(tmp, "managed_air.conf")
            ground_path = os.path.join(tmp, "managed_ground.conf")
            state_path = os.path.join(tmp, "managed_controls.json")
            _write_runtime_profile(
                air_path,
                airband=True,
                freqs=[118.4],
                labels=["East"],
                squelch_dbfs=-60,
            )
            _write_runtime_profile(
                ground_path,
                airband=False,
                freqs=[162.55],
                labels=["WX"],
                squelch_dbfs=-70,
            )
            profiles = [
                {
                    "id": "hp3_favorites_airband",
                    "label": "HP3 Favorites Airband",
                    "path": air_path,
                    "airband": True,
                },
                {
                    "id": "hp3_favorites_ground",
                    "label": "HP3 Favorites Ground",
                    "path": ground_path,
                    "airband": False,
                },
            ]

            with mock.patch.object(actions, "resolve_controls_path", return_value=air_path), mock.patch.object(
                actions, "write_combined_config", return_value=False
            ), mock.patch.object(
                actions, "restart_rtl", return_value=(True, "")
            ), mock.patch.object(
                managed_analog_controls, "MANAGED_ANALOG_CONTROLS_PATH", state_path
            ), mock.patch.object(
                managed_analog_controls,
                "managed_controls_profile_path",
                side_effect=lambda target: air_path if target == "airband" else ground_path,
            ):
                apply_result = actions.action_apply_controls("airband", 32.8, "dbfs", 10.0, -58.0)

            self.assertEqual(200, apply_result["status"])
            self.assertEqual(-58.0, profile_config.parse_controls(air_path)[2])

            def _ensure(_profiles, *, profile_id, label, airband):
                del label, airband
                return favorites_runtime.find_profile(profiles, profile_id), False

            with mock.patch.object(
                favorites_runtime, "load_profiles_registry", return_value=profiles
            ), mock.patch.object(
                favorites_runtime, "_ensure_managed_profile", side_effect=_ensure
            ), mock.patch.object(
                favorites_runtime, "_switch_profile_if_needed", return_value=(False, "")
            ), mock.patch.object(
                favorites_runtime, "write_combined_config", return_value=False
            ), mock.patch.object(
                managed_analog_controls, "MANAGED_ANALOG_CONTROLS_PATH", state_path
            ), mock.patch.object(
                managed_analog_controls,
                "managed_controls_profile_path",
                side_effect=lambda target: air_path if target == "airband" else ground_path,
            ):
                result = favorites_runtime.sync_scan_pool_to_analog_runtime(
                    force=True,
                    mode="hp",
                    pool={
                        "trunked_sites": [],
                        "conventional": [
                            {"frequency": 118.6, "alpha_tag": "Tower"},
                        ],
                    },
                )

            self.assertTrue(result["ok"])
            self.assertEqual("override", result["profile_controls_source"]["airband"])
            self.assertEqual(-58.0, profile_config.parse_controls(air_path)[2])


class SchedulerPayloadExtractionTests(unittest.TestCase):
    def test_extract_scheduler_payload_includes_perf_profile_keys(self):
        payload = handlers._extract_scheduler_payload(
            {
                "mode": "timeslice_multi_system",
                "system_dwell_ms": "400",
                "performance_profile": "pc_moderate",
                "digital_allocation_perf_profile": "legacy",
            }
        )

        self.assertEqual("timeslice_multi_system", payload.get("mode"))
        self.assertEqual("400", payload.get("system_dwell_ms"))
        self.assertEqual("pc_moderate", payload.get("performance_profile"))
        self.assertEqual("legacy", payload.get("digital_allocation_perf_profile"))

    def test_extract_scheduler_payload_ignores_unrelated_form_keys(self):
        payload = handlers._extract_scheduler_payload(
            {
                "foo": "bar",
                "mode": "single_system",
                "unrelated": "1",
            }
        )

        self.assertEqual({"mode": "single_system"}, payload)


class RecentRegressionTests(unittest.TestCase):
    def test_action_hold_stop_preserves_other_hold_entries(self):
        initial_state = {
            "airband": {
                "active": True,
                "conf_path": "/tmp/airband.conf",
                "original_text": "airband-original",
            },
            "ground": {
                "active": True,
                "conf_path": "/tmp/ground.conf",
                "original_text": "ground-original",
            },
        }
        with mock.patch.object(actions, "_load_hold_state", return_value=initial_state), mock.patch.object(
            actions, "_write_text", return_value=None
        ), mock.patch.object(
            actions, "_save_or_clear_hold_state"
        ) as save_state, mock.patch.object(
            actions, "write_combined_config", return_value=False
        ), mock.patch.object(
            actions, "restart_rtl", return_value=(True, "")
        ):
            result = actions.action_hold_stop("airband")

        self.assertEqual(200, result["status"])
        self.assertTrue(result["payload"]["ok"])
        self.assertTrue(result["payload"]["restored"])
        saved = save_state.call_args.args[0]
        self.assertNotIn("airband", saved)
        self.assertIn("ground", saved)

    def test_action_hold_stop_incomplete_state_still_preserves_other_entries(self):
        initial_state = {
            "airband": {
                "active": True,
                "conf_path": "/tmp/airband.conf",
                "original_text": None,
            },
            "ground": {
                "active": True,
                "conf_path": "/tmp/ground.conf",
                "original_text": "ground-original",
            },
        }
        with mock.patch.object(actions, "_load_hold_state", return_value=initial_state), mock.patch.object(
            actions, "_save_or_clear_hold_state"
        ) as save_state:
            result = actions.action_hold_stop("airband")

        self.assertEqual(400, result["status"])
        self.assertIn("incomplete", result["payload"]["error"])
        saved = save_state.call_args.args[0]
        self.assertNotIn("airband", saved)
        self.assertIn("ground", saved)

    def test_merge_favorites_preserving_custom_retains_existing_metadata(self):
        existing_rows = [
            {"id": "fav-1", "label": "List A", "custom_favorites": [{"id": "a1"}]},
            {"id": "fav-2", "label": "List B", "custom_favorites": [{"id": "b1"}]},
        ]
        incoming_rows = [
            {"id": "fav-1", "label": "List A", "enabled": True},
            {"id": "fav-2", "label": "List B", "enabled": False, "custom_favorites": [{"id": "override"}]},
            "passthrough",
        ]

        merged = handlers.merge_favorites_preserving_custom(existing_rows, incoming_rows)

        self.assertEqual([{"id": "a1"}], merged[0]["custom_favorites"])
        self.assertEqual([{"id": "override"}], merged[1]["custom_favorites"])
        self.assertEqual("passthrough", merged[2])

    def test_should_resolve_zip_requires_use_location(self):
        self.assertTrue(handlers._should_resolve_zip(True, True))
        self.assertFalse(handlers._should_resolve_zip(True, False))
        self.assertFalse(handlers._should_resolve_zip(False, True))
        self.assertFalse(handlers._should_resolve_zip(False, False))

    def test_hits_payload_filters_entries_older_than_30_minutes(self):
        now = time.time()
        recent_hit = {
            "time": "12:00:00",
            "freq": "118.6000",
            "duration": 2,
            "ts": now - 60,
            "source": "airband",
        }
        stale_hit = {
            "time": "11:00:00",
            "freq": "119.3500",
            "duration": 3,
            "ts": now - 4000,
            "source": "airband",
        }
        fake_digital = mock.Mock()
        fake_digital.getRecentEvents.return_value = []

        with mock.patch.object(handlers, "read_active_config_path", return_value="/tmp/active.conf"), mock.patch.object(
            handlers, "split_profiles", return_value=([], [], [])
        ), mock.patch.object(
            handlers, "guess_current_profile", return_value=""
        ), mock.patch.object(
            handlers, "_resolve_analog_label_map", return_value={}
        ), mock.patch.object(
            handlers, "read_hit_list_cached", return_value=[recent_hit, stale_hit]
        ), mock.patch.object(
            handlers, "get_digital_manager", return_value=fake_digital
        ):
            payload = handlers._build_hits_payload(limit=50)

        items = payload.get("items") or []
        self.assertEqual(1, len(items))
        self.assertEqual("118.6000", str(items[0].get("freq") or ""))
        self.assertIsInstance(items[0].get("ts"), (int, float))
        self.assertGreater(float(items[0].get("ts") or 0.0), 0.0)

    def test_hits_payload_skips_non_audible_digital_fallback_events(self):
        fake_digital = mock.Mock()
        fake_digital.getRecentEvents.return_value = [
            {
                "label": "Lee County Sheriffs Office - East Dispatch",
                "tgid": "20052",
                "timeMs": int(time.time() * 1000),
                "event_id": "call-raw-without-audio",
            }
        ]

        with mock.patch.object(handlers, "DIGITAL_HITS_REQUIRE_AUDIO_EVENT", True), mock.patch.object(
            handlers, "read_active_config_path", return_value="/tmp/active.conf"
        ), mock.patch.object(
            handlers, "split_profiles", return_value=([], [], [])
        ), mock.patch.object(
            handlers, "guess_current_profile", return_value=""
        ), mock.patch.object(
            handlers, "_resolve_analog_label_map", return_value={}
        ), mock.patch.object(
            handlers, "read_hit_list_cached", return_value=[]
        ), mock.patch.object(
            handlers, "_digital_stream_active_for_hits", return_value=True
        ), mock.patch.object(
            handlers, "_digital_stream_routed_tgids_for_hits", return_value={"20052"}
        ), mock.patch.object(
            handlers, "get_digital_manager", return_value=fake_digital
        ):
            payload = handlers._build_hits_payload(limit=50)

        self.assertEqual([], payload.get("items") or [])

    def test_hits_payload_keeps_audible_digital_events_with_duration(self):
        fake_digital = mock.Mock()
        fake_digital.getRecentEvents.return_value = [
            {
                "label": "Metro Dispatch",
                "tgid": "20052",
                "timeMs": int(time.time() * 1000),
                "event_id": "call-1",
                "durationMs": 2300,
                "mode": "P25P1",
            }
        ]

        with mock.patch.object(handlers, "DIGITAL_HITS_REQUIRE_AUDIO_EVENT", True), mock.patch.object(
            handlers, "read_active_config_path", return_value="/tmp/active.conf"
        ), mock.patch.object(
            handlers, "split_profiles", return_value=([], [], [])
        ), mock.patch.object(
            handlers, "guess_current_profile", return_value=""
        ), mock.patch.object(
            handlers, "_resolve_analog_label_map", return_value={}
        ), mock.patch.object(
            handlers, "read_hit_list_cached", return_value=[]
        ), mock.patch.object(
            handlers, "_digital_stream_active_for_hits", return_value=True
        ), mock.patch.object(
            handlers, "_digital_stream_routed_tgids_for_hits", return_value={"20052"}
        ), mock.patch.object(
            handlers, "get_digital_manager", return_value=fake_digital
        ):
            payload = handlers._build_hits_payload(limit=50)

        items = payload.get("items") or []
        self.assertEqual(1, len(items))
        self.assertEqual("digital", str(items[0].get("source") or ""))
        self.assertEqual(3, int(items[0].get("duration") or 0))
        self.assertEqual("P25P1", str(items[0].get("mode") or ""))

    def test_hits_payload_skips_digital_events_not_routed_to_stream(self):
        fake_digital = mock.Mock()
        fake_digital.getRecentEvents.return_value = [
            {
                "label": "TG 20300",
                "tgid": "20300",
                "timeMs": int(time.time() * 1000),
                "durationMs": 4000,
                "mode": "P25P1",
            }
        ]

        with mock.patch.object(handlers, "DIGITAL_HITS_REQUIRE_AUDIO_EVENT", True), mock.patch.object(
            handlers, "read_active_config_path", return_value="/tmp/active.conf"
        ), mock.patch.object(
            handlers, "split_profiles", return_value=([], [], [])
        ), mock.patch.object(
            handlers, "guess_current_profile", return_value=""
        ), mock.patch.object(
            handlers, "_resolve_analog_label_map", return_value={}
        ), mock.patch.object(
            handlers, "read_hit_list_cached", return_value=[]
        ), mock.patch.object(
            handlers, "_digital_stream_active_for_hits", return_value=True
        ), mock.patch.object(
            handlers, "_digital_stream_routed_tgids_for_hits", return_value={"20052"}
        ), mock.patch.object(
            handlers, "get_digital_manager", return_value=fake_digital
        ):
            payload = handlers._build_hits_payload(limit=50)

        self.assertEqual([], payload.get("items") or [])

    def test_hits_payload_skips_audible_events_without_tgid_when_route_filter_enabled(self):
        fake_digital = mock.Mock()
        fake_digital.getRecentEvents.return_value = [
            {
                "label": "Metro Dispatch",
                "timeMs": int(time.time() * 1000),
                "durationMs": 2100,
                "mode": "P25P1",
            }
        ]

        with mock.patch.object(handlers, "DIGITAL_HITS_REQUIRE_AUDIO_EVENT", True), mock.patch.object(
            handlers, "read_active_config_path", return_value="/tmp/active.conf"
        ), mock.patch.object(
            handlers, "split_profiles", return_value=([], [], [])
        ), mock.patch.object(
            handlers, "guess_current_profile", return_value=""
        ), mock.patch.object(
            handlers, "_resolve_analog_label_map", return_value={}
        ), mock.patch.object(
            handlers, "read_hit_list_cached", return_value=[]
        ), mock.patch.object(
            handlers, "_digital_stream_active_for_hits", return_value=True
        ), mock.patch.object(
            handlers, "_digital_stream_routed_tgids_for_hits", return_value={"20052"}
        ), mock.patch.object(
            handlers, "get_digital_manager", return_value=fake_digital
        ):
            payload = handlers._build_hits_payload(limit=50)

        self.assertEqual([], payload.get("items") or [])

    def test_hits_payload_routes_by_tgid_parsed_from_label(self):
        fake_digital = mock.Mock()
        fake_digital.getRecentEvents.return_value = [
            {
                "label": "Metro Dispatch TG 20052",
                "timeMs": int(time.time() * 1000),
                "durationMs": 2100,
                "mode": "P25P1",
            }
        ]

        with mock.patch.object(handlers, "DIGITAL_HITS_REQUIRE_AUDIO_EVENT", True), mock.patch.object(
            handlers, "read_active_config_path", return_value="/tmp/active.conf"
        ), mock.patch.object(
            handlers, "split_profiles", return_value=([], [], [])
        ), mock.patch.object(
            handlers, "guess_current_profile", return_value=""
        ), mock.patch.object(
            handlers, "_resolve_analog_label_map", return_value={}
        ), mock.patch.object(
            handlers, "read_hit_list_cached", return_value=[]
        ), mock.patch.object(
            handlers, "_digital_stream_active_for_hits", return_value=True
        ), mock.patch.object(
            handlers, "_digital_stream_routed_tgids_for_hits", return_value={"20052"}
        ), mock.patch.object(
            handlers, "get_digital_manager", return_value=fake_digital
        ):
            payload = handlers._build_hits_payload(limit=50)

        items = payload.get("items") or []
        self.assertEqual(1, len(items))
        self.assertEqual("20052", str(items[0].get("tgid") or ""))
        self.assertEqual("digital", str(items[0].get("source") or ""))

    def test_gmrs_frs_murs_profile_infers_ground_target_without_file(self):
        inferred = profile_config._infer_airband_flag("gmrs_frs_murs", "/tmp/does-not-exist.conf")
        self.assertIs(inferred, False)

    def test_load_profiles_registry_appends_missing_builtin_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = os.path.join(tmp, "profiles.json")
            with open(registry_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "profiles": [
                            {
                                "id": "gmrs",
                                "label": "GMRS",
                                "path": "/tmp/rtl_airband_gmrs.conf",
                                "airband": False,
                            }
                        ]
                    },
                    handle,
                )

            builtins = [
                ("gmrs", "GMRS", "/tmp/rtl_airband_gmrs.conf"),
                ("gmrs_frs_murs", "GMRS/FRS/MURS", "/tmp/rtl_airband_gmrs_frs_murs.conf"),
            ]

            with mock.patch.object(profile_config, "PROFILES_DIR", tmp), mock.patch.object(
                profile_config, "PROFILES_REGISTRY_PATH", registry_path
            ), mock.patch.object(profile_config, "PROFILES", builtins):
                loaded = profile_config.load_profiles_registry()

            by_id = {row["id"]: row for row in loaded}
            self.assertIn("gmrs", by_id)
            self.assertIn("gmrs_frs_murs", by_id)
            self.assertFalse(by_id["gmrs_frs_murs"]["airband"])

    def test_load_profiles_registry_restores_builtin_profile_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = os.path.join(tmp, "profiles.json")
            with open(registry_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "profiles": [
                            {
                                "id": "wx",
                                "label": "WX (162.550)",
                                "path": "/tmp/rtl_airband_hp3_favorites_ground.conf",
                                "airband": True,
                            },
                            {
                                "id": "hp3_favorites_ground",
                                "label": "HP3 Favorites Ground",
                                "path": "/tmp/rtl_airband_hp3_favorites_ground.conf",
                                "airband": False,
                            },
                        ]
                    },
                    handle,
                )

            builtins = [
                ("wx", "WX (162.550)", "/tmp/rtl_airband_wx.conf"),
                ("none_ground", "No Profile", "/tmp/rtl_airband_none_ground.conf"),
            ]

            with mock.patch.object(profile_config, "PROFILES_DIR", tmp), mock.patch.object(
                profile_config, "PROFILES_REGISTRY_PATH", registry_path
            ), mock.patch.object(profile_config, "PROFILES", builtins):
                loaded = profile_config.load_profiles_registry()

            by_id = {row["id"]: row for row in loaded}
            self.assertIn("wx", by_id)
            self.assertEqual("/tmp/rtl_airband_wx.conf", by_id["wx"]["path"])
            self.assertFalse(by_id["wx"]["airband"])
            self.assertEqual(
                "/tmp/rtl_airband_hp3_favorites_ground.conf",
                by_id["hp3_favorites_ground"]["path"],
            )

    def test_save_hp_state_with_sync_reports_sync_errors(self):
        state = HPState.default()
        with mock.patch.object(state, "save", return_value=None), mock.patch.object(
            handlers,
            "sync_scan_pool_to_runtime",
            side_effect=RuntimeError("sync exploded"),
        ):
            payload = handlers._save_hp_state_with_sync(state)

        self.assertTrue(payload["ok"])
        self.assertIn("favorites_runtime_sync", payload)
        sync = payload["favorites_runtime_sync"]
        self.assertFalse(sync["ok"])
        self.assertFalse(sync["changed"])
        self.assertIn("sync exploded", sync["errors"][0])
        self.assertTrue(sync["request_complete"])
        self.assertFalse(sync["pending"])

    def test_save_hp_state_with_sync_returns_pending_when_sync_exceeds_wait_budget(self):
        state = HPState.default()

        def _slow_sync(force=True):  # noqa: ARG001
            time.sleep(0.05)
            return {"ok": True, "changed": True, "errors": []}

        with mock.patch.object(state, "save", return_value=None), mock.patch.object(
            handlers, "HP_STATE_SYNC_WAIT_SEC", 0.001
        ), mock.patch.object(
            handlers,
            "sync_scan_pool_to_runtime",
            side_effect=_slow_sync,
        ):
            payload = handlers._save_hp_state_with_sync(state)
            sync = payload["favorites_runtime_sync"]
            self.assertTrue(payload["ok"])
            self.assertTrue(sync["pending"])
            self.assertFalse(sync["request_complete"])
            handlers._wait_for_favorites_runtime_sync(sync["request_id"], 0.2)

    def test_parse_service_tags_normalizes_json_csv_and_scalar(self):
        self.assertEqual([2, 15, 3], handlers.parse_service_tags("[2, \"15\", 3, \"x\", 2]"))
        self.assertEqual([4, 15, 30], handlers.parse_service_tags("4,15,30,4"))
        self.assertEqual([7], handlers.parse_service_tags("7"))

    def test_scan_pool_filters_conventional_rows_using_hp_avoids(self):
        controller = scan_mode_controller.ScanModeController(db_path="/tmp/hpdb-test.db")
        controller.add_hp_avoid_system("conv:blocked")
        state = HPState.default()
        state.mode = "full_database"
        state.use_location = True
        state.lat = 36.12
        state.lon = -86.54
        state.enabled_service_tags = [2]
        base_pool = {
            "trunked_sites": [],
            "conventional": [
                {
                    "system_key": "conv:blocked",
                    "system_name": "Blocked",
                    "frequency": 155.5,
                    "alpha_tag": "Blocked Ch",
                    "service_tag": 2,
                },
                {
                    "system_key": "conv:allowed",
                    "system_name": "Allowed",
                    "frequency": 156.6,
                    "alpha_tag": "Allowed Ch",
                    "service_tag": 2,
                },
            ],
        }

        with mock.patch("ui.hp_state.HPState.load", return_value=state), mock.patch.object(
            controller, "_resolve_effective_service_tags", return_value=[2]
        ), mock.patch.object(
            controller._hp_builder, "build_full_database_pool", return_value=base_pool
        ):
            filtered = controller.get_scan_pool()

        conventional = filtered.get("conventional") or []
        self.assertEqual(1, len(conventional))
        self.assertEqual("conv:allowed", conventional[0].get("system_key"))

    def test_scan_pool_filters_conventional_rows_using_convfreq_avoids(self):
        controller = scan_mode_controller.ScanModeController(db_path="/tmp/hpdb-test.db")
        controller.add_hp_avoid_system("convfreq:155.500000")
        state = HPState.default()
        state.mode = "full_database"
        state.use_location = True
        state.lat = 36.12
        state.lon = -86.54
        state.enabled_service_tags = [2]
        base_pool = {
            "trunked_sites": [],
            "conventional": [
                {
                    "system_key": "",
                    "system_name": "",
                    "frequency": 155.5,
                    "alpha_tag": "Blocked Ch",
                    "service_tag": 2,
                },
                {
                    "system_key": "",
                    "system_name": "",
                    "frequency": 156.6,
                    "alpha_tag": "Allowed Ch",
                    "service_tag": 2,
                },
            ],
        }

        with mock.patch("ui.hp_state.HPState.load", return_value=state), mock.patch.object(
            controller, "_resolve_effective_service_tags", return_value=[2]
        ), mock.patch.object(
            controller._hp_builder, "build_full_database_pool", return_value=base_pool
        ):
            filtered = controller.get_scan_pool()

        conventional = filtered.get("conventional") or []
        self.assertEqual(1, len(conventional))
        self.assertAlmostEqual(156.6, float(conventional[0].get("frequency") or 0.0))

    def test_scan_pool_filters_trunked_rows_using_agency_avoids(self):
        controller = scan_mode_controller.ScanModeController(db_path="/tmp/hpdb-test.db")
        controller.add_hp_avoid_system("agency:42:police dispatch")
        state = HPState.default()
        state.mode = "full_database"
        state.use_location = True
        state.lat = 36.12
        state.lon = -86.54
        state.enabled_service_tags = [2]
        base_pool = {
            "trunked_sites": [
                {
                    "system_id": 42,
                    "site_id": 1,
                    "system_name": "Metro System",
                    "site_name": "Site 1",
                    "department_name": "Metro",
                    "control_channels": [853.4],
                    "talkgroups": [1001, 2002],
                    "talkgroup_labels": {"1001": "North Disp", "2002": "Fire Tac"},
                    "talkgroup_groups": {"1001": "Police Dispatch", "2002": "Fire Ops"},
                }
            ],
            "conventional": [],
        }

        with mock.patch("ui.hp_state.HPState.load", return_value=state), mock.patch.object(
            controller, "_resolve_effective_service_tags", return_value=[2]
        ), mock.patch.object(
            controller._hp_builder, "build_full_database_pool", return_value=base_pool
        ):
            filtered = controller.get_scan_pool()

        trunked = filtered.get("trunked_sites") or []
        self.assertEqual(1, len(trunked))
        self.assertEqual([2002], trunked[0].get("talkgroups"))

    def test_scan_pool_full_database_resolves_zip_when_lat_lon_missing(self):
        controller = scan_mode_controller.ScanModeController(db_path="/tmp/hpdb-test.db")
        state = HPState.default()
        state.mode = "full_database"
        state.use_location = True
        state.zip = "37221"
        state.lat = 0.0
        state.lon = 0.0
        state.range_miles = 15.0
        state.enabled_service_tags = [2]
        base_pool = {"trunked_sites": [], "conventional": []}

        with mock.patch("ui.hp_state.HPState.load", return_value=state), mock.patch.object(
            controller, "_resolve_effective_service_tags", return_value=[2]
        ), mock.patch(
            "ui.scan_mode_controller.resolve_postal_to_lat_lon",
            return_value=(36.1234, -86.5678),
        ) as resolve_zip, mock.patch.object(
            controller._hp_builder,
            "build_full_database_pool",
            return_value=base_pool,
        ) as build_pool:
            filtered = controller.get_scan_pool()

        self.assertIs(filtered, base_pool)
        resolve_zip.assert_called_once_with("37221", "US")
        self.assertEqual(1, build_pool.call_count)
        kwargs = build_pool.call_args.kwargs
        self.assertAlmostEqual(36.1234, float(kwargs.get("lat")))
        self.assertAlmostEqual(-86.5678, float(kwargs.get("lon")))
        self.assertEqual(15.0, float(kwargs.get("range_miles")))
        self.assertEqual([2], kwargs.get("service_tags"))
        self.assertFalse(bool(kwargs.get("strict_location")))

    def test_scan_pool_full_database_forwards_strict_location_flag(self):
        controller = scan_mode_controller.ScanModeController(db_path="/tmp/hpdb-test.db")
        state = HPState.default()
        state.mode = "full_database"
        state.use_location = True
        state.strict_location = True
        state.lat = 36.12
        state.lon = -86.54
        state.range_miles = 12.0
        state.enabled_service_tags = [2]
        base_pool = {"trunked_sites": [], "conventional": []}

        with mock.patch("ui.hp_state.HPState.load", return_value=state), mock.patch.object(
            controller, "_resolve_effective_service_tags", return_value=[2]
        ), mock.patch.object(
            controller._hp_builder,
            "build_full_database_pool",
            return_value=base_pool,
        ) as build_pool:
            filtered = controller.get_scan_pool()

        self.assertIs(filtered, base_pool)
        self.assertEqual(1, build_pool.call_count)
        kwargs = build_pool.call_args.kwargs
        self.assertTrue(bool(kwargs.get("strict_location")))

    def test_scan_pool_full_database_prefers_nearest_site_per_system(self):
        controller = scan_mode_controller.ScanModeController(db_path="/tmp/hpdb-test.db")
        state = HPState.default()
        state.mode = "full_database"
        state.use_location = True
        state.lat = 36.12
        state.lon = -86.54
        state.range_miles = 15.0
        state.enabled_service_tags = [2]
        base_pool = {
            "trunked_sites": [
                {
                    "system_id": 100,
                    "site_id": 10,
                    "distance_miles": 9.5,
                    "control_channels": [851.1],
                    "talkgroups": [1001],
                },
                {
                    "system_id": 100,
                    "site_id": 11,
                    "distance_miles": 2.2,
                    "control_channels": [852.1],
                    "talkgroups": [1001],
                },
                {
                    "system_id": 200,
                    "site_id": 20,
                    "distance_miles": 4.0,
                    "control_channels": [853.1],
                    "talkgroups": [2001],
                },
            ],
            "conventional": [],
        }

        with mock.patch("ui.hp_state.HPState.load", return_value=state), mock.patch.object(
            controller, "_resolve_effective_service_tags", return_value=[2]
        ), mock.patch.object(
            controller._hp_builder,
            "build_full_database_pool",
            return_value=base_pool,
        ):
            filtered = controller.get_scan_pool()

        trunked = filtered.get("trunked_sites") or []
        self.assertEqual(2, len(trunked))
        site_ids = sorted(int(row.get("site_id") or 0) for row in trunked)
        self.assertEqual([11, 20], site_ids)

    def test_scan_pool_favorites_location_trims_controls_to_nearest_sites(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "hp.db")
            with sqlite3.connect(db_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE trunk_sites (
                        site_id INTEGER PRIMARY KEY,
                        trunk_id INTEGER,
                        source_file TEXT,
                        latitude REAL,
                        longitude REAL,
                        radius REAL
                    );
                    CREATE TABLE trunk_freqs (
                        site_id INTEGER,
                        freq_hz INTEGER
                    );
                    """
                )
                conn.execute(
                    "INSERT INTO trunk_sites(site_id, trunk_id, source_file, latitude, longitude, radius) VALUES (?,?,?,?,?,?)",
                    (1001, 42, "TN.hpd", 36.1205, -86.5405, 2.0),
                )
                conn.execute(
                    "INSERT INTO trunk_sites(site_id, trunk_id, source_file, latitude, longitude, radius) VALUES (?,?,?,?,?,?)",
                    (1002, 42, "TN.hpd", 37.5000, -87.9000, 2.0),
                )
                conn.execute("INSERT INTO trunk_freqs(site_id, freq_hz) VALUES (?,?)", (1001, 851100000))
                conn.execute("INSERT INTO trunk_freqs(site_id, freq_hz) VALUES (?,?)", (1001, 851300000))
                conn.execute("INSERT INTO trunk_freqs(site_id, freq_hz) VALUES (?,?)", (1002, 853100000))
                conn.commit()

            controller = scan_mode_controller.ScanModeController(db_path=db_path)
            state = HPState.default()
            state.mode = "favorites"
            state.use_location = True
            state.lat = 36.12
            state.lon = -86.54
            state.range_miles = 35.0
            state.nationwide_systems = True
            state.enabled_service_tags = [2]
            entries = [
                {
                    "kind": "trunked",
                    "system_id": 42,
                    "system_name": "Metro System",
                    "department_name": "Police",
                    "alpha_tag": "Dispatch",
                    "talkgroup": 1001,
                    "service_tag": 2,
                    "control_channels": [851.1, 851.3, 853.1],
                }
            ]

            with mock.patch("ui.hp_state.HPState.load", return_value=state), mock.patch.object(
                controller, "_resolve_effective_service_tags", return_value=[2]
            ), mock.patch.object(
                controller, "_resolve_active_favorites_entries", return_value=entries
            ), mock.patch.object(
                controller, "_filter_favorites_entries", return_value=entries
            ):
                pool = controller.get_scan_pool()

            trunked = pool.get("trunked_sites") or []
            self.assertEqual(1, len(trunked))
            controls = list(trunked[0].get("control_channels") or [])
            self.assertEqual([851.1, 851.3], controls)
            self.assertNotIn(853.1, controls)

    def test_scan_pool_favorites_location_keeps_backup_site_when_limit_is_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "hp.db")
            with sqlite3.connect(db_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE trunk_sites (
                        site_id INTEGER PRIMARY KEY,
                        trunk_id INTEGER,
                        source_file TEXT,
                        latitude REAL,
                        longitude REAL,
                        radius REAL
                    );
                    CREATE TABLE trunk_freqs (
                        site_id INTEGER,
                        freq_hz INTEGER
                    );
                    """
                )
                conn.execute(
                    "INSERT INTO trunk_sites(site_id, trunk_id, source_file, latitude, longitude, radius) VALUES (?,?,?,?,?,?)",
                    (2001, 84, "TN.hpd", 36.1205, -86.5405, 2.0),
                )
                conn.execute(
                    "INSERT INTO trunk_sites(site_id, trunk_id, source_file, latitude, longitude, radius) VALUES (?,?,?,?,?,?)",
                    (2002, 84, "TN.hpd", 37.5000, -87.9000, 2.0),
                )
                conn.execute("INSERT INTO trunk_freqs(site_id, freq_hz) VALUES (?,?)", (2001, 851100000))
                conn.execute("INSERT INTO trunk_freqs(site_id, freq_hz) VALUES (?,?)", (2002, 853100000))
                conn.commit()

            controller = scan_mode_controller.ScanModeController(db_path=db_path)
            state = HPState.default()
            state.mode = "favorites"
            state.use_location = True
            state.lat = 36.12
            state.lon = -86.54
            state.range_miles = 35.0
            state.nationwide_systems = True
            state.enabled_service_tags = [2]
            entries = [
                {
                    "kind": "trunked",
                    "system_id": 84,
                    "system_name": "Backup Test System",
                    "department_name": "Police",
                    "alpha_tag": "Dispatch",
                    "talkgroup": 2001,
                    "service_tag": 2,
                    "control_channels": [851.1, 853.1],
                }
            ]

            with mock.patch.dict(os.environ, {"HP_TRUNK_SITES_PER_SYSTEM": "2"}, clear=False), mock.patch(
                "ui.hp_state.HPState.load", return_value=state
            ), mock.patch.object(
                controller, "_resolve_effective_service_tags", return_value=[2]
            ), mock.patch.object(
                controller, "_resolve_active_favorites_entries", return_value=entries
            ), mock.patch.object(
                controller, "_filter_favorites_entries", return_value=entries
            ):
                pool = controller.get_scan_pool()

            trunked = pool.get("trunked_sites") or []
            self.assertEqual(1, len(trunked))
            controls = list(trunked[0].get("control_channels") or [])
            self.assertEqual([851.1, 853.1], controls)

    def test_build_custom_favorites_pool_merges_trunked_departments_per_system(self):
        controller = scan_mode_controller.ScanModeController(db_path="/tmp/hpdb-test.db")
        entries = [
            {
                "kind": "trunked",
                "system_id": 7078,
                "system_name": "Middle Tennessee Regional Trunked Radio System",
                "department_name": "Vanderbilt University",
                "alpha_tag": "Police Dispatch",
                "talkgroup": 3207,
                "service_tag": 2,
                "control_channels": [769.11875, 771.10625],
            },
            {
                "kind": "trunked",
                "system_id": 7078,
                "system_name": "Middle Tennessee Regional Trunked Radio System",
                "department_name": "Davidson County - Goodlettsville",
                "alpha_tag": "Police - Dispatch",
                "talkgroup": 3301,
                "service_tag": 2,
                "control_channels": [769.11875, 771.10625],
            },
        ]

        pool = controller._build_custom_favorites_pool(entries)
        trunked = pool.get("trunked_sites") or []
        self.assertEqual(1, len(trunked))
        row = trunked[0]
        self.assertEqual([3207, 3301], row.get("talkgroups"))
        groups = row.get("talkgroup_groups") or {}
        self.assertEqual("Vanderbilt University", groups.get("3207"))
        self.assertEqual("Davidson County - Goodlettsville", groups.get("3301"))

    def test_resolve_analog_label_map_falls_back_to_profile_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_path = os.path.join(tmp, "rtl_airband_none_airband.conf")
            catalog_path = os.path.join(tmp, "rtl_airband_tower.conf")
            with open(active_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "devices:\n(\n  {\n    freqs = (118.6000);\n  }\n);\n"
                )
            with open(catalog_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "devices:\n(\n  {\n    freqs = (118.6000, 124.8750);\n"
                    "    labels = (\"Tower\", \"Approach\");\n  }\n);\n"
                )
            profile_rows = [
                {"id": "none_airband", "path": active_path},
                {"id": "tower", "path": catalog_path},
            ]
            mapping = handlers._resolve_analog_label_map(
                active_path,
                "none_airband",
                profile_rows,
            )
            self.assertEqual("Tower", mapping.get("118.6000"))
            self.assertEqual("Approach", mapping.get("124.8750"))

    def test_resolve_digital_stream_mount_prefers_configured_mount_when_present(self):
        status_text = json.dumps(
            {
                "icestats": {
                    "source": [
                        {
                            "listenurl": "http://127.0.0.1:8000/DIGITAL.mp3",
                            "audio_info": "bitrate=32;samplerate=16000",
                            "server_type": "audio/mpeg",
                        },
                        {
                            "listenurl": "http://127.0.0.1:8000/keepalive-digital.mp3",
                            "audio_info": "bitrate=32;samplerate=16000",
                            "server_type": "audio/mpeg",
                        },
                    ]
                }
            }
        )
        with mock.patch.object(handlers, "DIGITAL_STREAM_MOUNT", "DIGITAL.mp3"):
            mount = handlers._resolve_digital_stream_mount(status_text)
        self.assertEqual("DIGITAL.mp3", mount)

    def test_resolve_digital_stream_mount_falls_back_when_configured_mount_missing(self):
        status_text = json.dumps(
            {
                "icestats": {
                    "source": [
                        {
                            "listenurl": "http://127.0.0.1:8000/ANALOG.mp3",
                            "audio_info": "bitrate=32;samplerate=16000",
                            "server_type": "audio/mpeg",
                        },
                        {
                            "listenurl": "http://127.0.0.1:8000/keepalive-digital.mp3",
                            "audio_info": "bitrate=32;samplerate=16000",
                            "server_type": "audio/mpeg",
                        },
                    ]
                }
            }
        )
        with mock.patch.object(handlers, "DIGITAL_STREAM_MOUNT", "DIGITAL.mp3"):
            mount = handlers._resolve_digital_stream_mount(status_text)
        self.assertEqual("keepalive-digital.mp3", mount)

    def test_digital_stream_active_checks_resolved_mount_title(self):
        status_text = json.dumps(
            {
                "icestats": {
                    "source": [
                        {
                            "listenurl": "http://127.0.0.1:8000/keepalive-digital.mp3",
                            "audio_info": "bitrate=32;samplerate=16000",
                            "server_type": "audio/mpeg",
                        }
                    ]
                }
            }
        )
        with mock.patch.object(handlers, "DIGITAL_STREAM_MOUNT", "DIGITAL.mp3"), mock.patch.object(
            handlers, "fetch_local_icecast_status", return_value=status_text
        ), mock.patch.object(
            handlers,
            "extract_icecast_title_for_mount",
            side_effect=lambda _status, mount: "Unit 1 Dispatch" if mount == "/keepalive-digital.mp3" else "",
        ) as extract_title, mock.patch.object(
            handlers, "_digital_has_recent_event", return_value=False
        ):
            active = handlers._digital_stream_active_for_hits()
        self.assertTrue(active)
        extract_title.assert_called_once_with(status_text, "/keepalive-digital.mp3")

    def test_digital_alias_stream_binding_normalizes_malformed_tdigital(self):
        root = ET.fromstring(
            """
            <playlist>
              <alias list="HP3_FAVORITES_DIGITAL" name="Countywide Dispatch" group="Nashville Police Zone 1">
                <id type="talkgroup" value="625" protocol="APCO25" />
                <id type="tDIGITAL" />
              </alias>
            </playlist>
            """
        )

        with mock.patch.object(digital, "DIGITAL_ATTACH_BROADCAST_CHANNEL", True), mock.patch.object(
            digital, "DIGITAL_SDRTRUNK_STREAM_NAME", "DIGITAL"
        ):
            added = digital._ensure_alias_broadcast_channel(root, "HP3_FAVORITES_DIGITAL")

        self.assertEqual(0, added)
        alias = root.find("alias")
        self.assertIsNotNone(alias)
        ids = list(alias.findall("id")) if alias is not None else []
        types = [str(node.get("type") or "") for node in ids]
        self.assertNotIn("tDIGITAL", types)
        broadcast = [node for node in ids if str(node.get("type") or "") == "broadcastChannel"]
        self.assertEqual(1, len(broadcast))
        self.assertEqual("DIGITAL", str(broadcast[0].get("channel") or ""))

    def test_digital_alias_stream_binding_normalizes_stale_other_alias_list(self):
        root = ET.fromstring(
            """
            <playlist>
              <alias list="HP3_FAVORITES_DIGITAL" name="Countywide Dispatch" group="Nashville Police Zone 1">
                <id type="talkgroup" value="625" protocol="APCO25" />
                <id type="broadcastChannel" channel="DIGITAL" />
              </alias>
              <alias list="LEGACY_PROFILE" name="Old Alias" group="Legacy">
                <id type="talkgroup" value="700" protocol="APCO25" />
                <id type="tDIGITAL" />
              </alias>
            </playlist>
            """
        )

        with mock.patch.object(digital, "DIGITAL_ATTACH_BROADCAST_CHANNEL", True), mock.patch.object(
            digital, "DIGITAL_SDRTRUNK_STREAM_NAME", "DIGITAL"
        ):
            added = digital._ensure_alias_broadcast_channel(root, "HP3_FAVORITES_DIGITAL")

        self.assertEqual(0, added)
        for alias in root.findall("alias"):
            ids = list(alias.findall("id"))
            types = [str(node.get("type") or "") for node in ids]
            self.assertNotIn("tDIGITAL", types)
        legacy = root.findall("alias")[1]
        legacy_broadcast = [
            node for node in legacy.findall("id") if str(node.get("type") or "") == "broadcastChannel"
        ]
        self.assertEqual(1, len(legacy_broadcast))
        self.assertEqual("DIGITAL", str(legacy_broadcast[0].get("channel") or ""))


class HPAvoidPersistenceTests(unittest.TestCase):
    def test_hp_avoids_load_from_disk_on_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            avoids_path = os.path.join(tmp, "hp_avoids.json")
            with open(avoids_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "avoids": [
                            "conv:Blocked",
                            " agency:42:Police Dispatch ",
                            "",
                            None,
                        ]
                    },
                    handle,
                )
            controller = scan_mode_controller.ScanModeController(
                db_path="/tmp/hpdb-test.db",
                avoids_path=avoids_path,
            )
            self.assertEqual(
                ["agency:42:police dispatch", "conv:blocked"],
                controller.get_hp_avoids(),
            )

    def test_hp_avoids_add_remove_clear_persist_to_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            avoids_path = os.path.join(tmp, "hp_avoids.json")
            controller = scan_mode_controller.ScanModeController(
                db_path="/tmp/hpdb-test.db",
                avoids_path=avoids_path,
            )

            self.assertTrue(controller.add_hp_avoid_system("conv:Blocked"))
            self.assertTrue(controller.add_hp_avoid_system("agency:42:Police Dispatch"))

            with open(avoids_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual(
                ["agency:42:police dispatch", "conv:blocked"],
                payload.get("avoids"),
            )

            reloaded = scan_mode_controller.ScanModeController(
                db_path="/tmp/hpdb-test.db",
                avoids_path=avoids_path,
            )
            self.assertEqual(
                ["agency:42:police dispatch", "conv:blocked"],
                reloaded.get_hp_avoids(),
            )

            self.assertTrue(controller.remove_hp_avoid_system("conv:blocked"))
            with open(avoids_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual(["agency:42:police dispatch"], payload.get("avoids"))

            controller.clear_hp_avoids()
            with open(avoids_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual([], payload.get("avoids"))


class TempConfigWriteTests(unittest.TestCase):
    class _FakeTempFile:
        def __init__(self, name):
            self.name = name
            self.contents = ""

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def write(self, text):
            self.contents += text
            return len(text)

    def test_write_temp_config_falls_back_when_first_dir_unwritable(self):
        calls = []

        def fake_named_temp_file(*args, **kwargs):
            directory = kwargs.get("dir")
            calls.append(directory)
            if directory == "/unwritable":
                raise PermissionError("no write access")
            return self._FakeTempFile(f"{directory}/ok.conf")

        with mock.patch.object(actions, "_temp_config_dir_candidates", return_value=iter(["/unwritable", "/writable"])), mock.patch.object(
            actions.os, "makedirs", return_value=None
        ), mock.patch.object(
            actions.tempfile, "NamedTemporaryFile", side_effect=fake_named_temp_file
        ):
            path = actions._write_temp_config("airband", "tune", "freqs=(125.1750);")

        self.assertEqual("/writable/ok.conf", path)
        self.assertEqual(["/unwritable", "/writable"], calls)


class StatePersistenceFallbackTests(unittest.TestCase):
    class _FakeWriter:
        def __init__(self):
            self.buffer = ""

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def write(self, text):
            self.buffer += text
            return len(text)

    def test_save_json_with_fallback_uses_second_candidate(self):
        opened = []
        replaced = []

        def fake_open(path, mode="r", encoding=None):
            opened.append(path)
            if path.startswith("/run/"):
                raise PermissionError("primary path unwritable")
            return self._FakeWriter()

        def fake_replace(src, dst):
            replaced.append((src, dst))

        with mock.patch.object(
            actions,
            "_state_path_candidates",
            return_value=iter(["/run/airband_ui_tune_backup.json", "/tmp/airband_ui_tune_backup.json"]),
        ), mock.patch.object(actions.os, "makedirs", return_value=None), mock.patch.object(
            actions, "open", side_effect=fake_open, create=True
        ), mock.patch.object(
            actions.os, "replace", side_effect=fake_replace
        ), mock.patch.object(
            actions.os.path, "exists", return_value=False
        ):
            saved_path = actions._save_json_with_fallback("/run/airband_ui_tune_backup.json", {"ok": True})

        self.assertEqual("/tmp/airband_ui_tune_backup.json", saved_path)
        self.assertTrue(any(path.startswith("/run/") for path in opened))
        self.assertTrue(any(path.startswith("/tmp/") for path in opened))
        self.assertEqual(1, len(replaced))
        self.assertEqual("/tmp/airband_ui_tune_backup.json", replaced[0][1])


class CombinedConfigBitrateTests(unittest.TestCase):
    _PROFILE_TEMPLATE = (
        "airband = __AIRBAND__;\n"
        "devices:\n"
        "(\n"
        "  {\n"
        "    type = \"rtlsdr\";\n"
        "    index = 0;\n"
        "    outputs:\n"
        "    (\n"
        "      {\n"
        "        type = \"icecast\";\n"
        "        server = \"127.0.0.1\";\n"
        "        port = 8000;\n"
        "        mountpoint = \"ANALOG.mp3\";\n"
        "        bitrate = 32;\n"
        "      }\n"
        "    );\n"
        "  }\n"
        ");\n"
    )

    def _write_profiles(self, root_dir):
        air = os.path.join(root_dir, "air.conf")
        ground = os.path.join(root_dir, "ground.conf")
        with open(air, "w", encoding="utf-8") as f:
            f.write(self._PROFILE_TEMPLATE.replace("__AIRBAND__", "true"))
        with open(ground, "w", encoding="utf-8") as f:
            f.write(self._PROFILE_TEMPLATE.replace("__AIRBAND__", "false"))
        return air, ground

    def test_build_combined_config_overrides_analog_bitrate(self):
        with tempfile.TemporaryDirectory() as tmp:
            air, ground = self._write_profiles(tmp)
            rendered = combined_config.build_combined_config(
                air,
                ground,
                "combined",
                analog_bitrate_kbps=64,
            )
        self.assertIn("bitrate = 64;", rendered)
        self.assertNotIn("bitrate = 32;", rendered)

    def test_build_combined_config_clamps_bitrate(self):
        with tempfile.TemporaryDirectory() as tmp:
            air, ground = self._write_profiles(tmp)
            rendered = combined_config.build_combined_config(
                air,
                ground,
                "combined",
                analog_bitrate_kbps=9999,
            )
        self.assertIn("bitrate = 320;", rendered)


class DigitalListenConsistencyTests(unittest.TestCase):
    def test_write_digital_listen_prunes_stale_tgids(self):
        with tempfile.TemporaryDirectory() as tmp:
            talkgroups_csv = os.path.join(tmp, "talkgroups.csv")
            with open(talkgroups_csv, "w", encoding="utf-8") as f:
                f.write("DEC,HEX,Mode,Alpha Tag,Description,Tag\n")
                f.write("3207,c87,D,Police Dispatch,Police Dispatch,\n")
                f.write("3209,c89,D,Police Tactical 1,Police Tactical 1,\n")

            listen_path = os.path.join(tmp, "talkgroups_listen.json")
            with open(listen_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "default_listen": False,
                        "items": {
                            "3207": True,
                            "3209": True,
                            "10350": True,
                        },
                        "talkgroups": {
                            "3207": {"listen": True},
                            "3209": {"listen": True},
                            "10350": {"listen": True},
                        },
                    },
                    f,
                    indent=2,
                )

            with mock.patch.object(digital, "_get_profile_dir", return_value=(tmp, "")):
                ok, err = digital.write_digital_listen(
                    "hp3_favorites_digital",
                    [{"dec": "3207", "listen": True}, {"dec": "3209", "listen": False}],
                )

            self.assertTrue(ok, msg=err)
            with open(listen_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            self.assertEqual({"3207", "3209"}, set((payload.get("items") or {}).keys()))
        self.assertNotIn("10350", (payload.get("items") or {}))
        self.assertFalse(bool((payload.get("items") or {}).get("3209")))

    def test_map_event_label_mutes_unknown_tgid_when_profile_map_exists(self):
        adapter = digital.SdrtrunkAdapter()
        adapter._listen_default = False
        adapter._tg_group_map = {}
        event = {"tgid": "10350", "label": "TG 10350", "raw": "Group Call TO (10350)"}
        with mock.patch.object(digital, "get_current_scan_mode", return_value="profile"), mock.patch.object(
            adapter, "_load_talkgroup_map", return_value={"3207": "Police Dispatch"}
        ), mock.patch.object(
            adapter, "_load_listen_map", return_value={"10350": True}
        ):
            mapped = adapter._map_event_label(event)
        self.assertTrue(bool(mapped.get("muted")))
        self.assertEqual("10350", str(mapped.get("tgid") or ""))


class DigitalRetuneCacheTests(unittest.TestCase):
    @staticmethod
    def _write_playlist(path: str, frequency_hz: int) -> None:
        text = (
            "<playlist>"
            "<channel>"
            f"<source_configuration type=\"sourceConfigTuner\" source_type=\"TUNER\" frequency=\"{int(frequency_hz)}\"/>"
            "</channel>"
            "</playlist>"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    @staticmethod
    def _write_multi_playlist(path: str, frequencies_hz: list[int]) -> None:
        entries = "".join(f"<frequency>{int(hz)}</frequency>" for hz in frequencies_hz)
        text = (
            "<playlist>"
            "<channel>"
            "<source_configuration type=\"sourceConfigTunerMultipleFrequency\" "
            "source_type=\"TUNER_MULTIPLE_FREQUENCIES\">"
            f"{entries}"
            "</source_configuration>"
            "</channel>"
            "</playlist>"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_retune_cache_short_circuits_when_state_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            playlist_path = os.path.join(tmp, "playlist.xml")
            self._write_playlist(playlist_path, 162_550_000)
            adapter = digital.SdrtrunkAdapter()
            playlist_real = os.path.realpath(playlist_path)
            mtime_ns = adapter._playlist_mtime_ns(playlist_real)
            adapter._playlist_cache_update(
                path=playlist_real,
                mtime_ns=mtime_ns,
                last_retune_hz=162_550_000,
            )
            with mock.patch.object(digital, "DIGITAL_PLAYLIST_PATH", playlist_path), mock.patch.object(
                adapter, "_runtime_retune_control_frequency", return_value=(False, False, "")
            ), mock.patch.object(
                digital.ET, "parse", side_effect=AssertionError("playlist parse should not run")
            ):
                ok, err = adapter.retune_control_frequency(162.55)

            self.assertTrue(ok)
            self.assertEqual("", err)
            self.assertFalse(bool(adapter.runtime_metrics().get("retune_last_changed")))

    def test_retune_playlist_path_uses_atomic_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            playlist_path = os.path.join(tmp, "playlist.xml")
            self._write_playlist(playlist_path, 162_400_000)
            adapter = digital.SdrtrunkAdapter()
            with mock.patch.object(digital, "DIGITAL_PLAYLIST_PATH", playlist_path), mock.patch.object(
                adapter, "_runtime_retune_control_frequency", return_value=(False, False, "")
            ), mock.patch.object(
                digital, "_write_playlist_tree_atomic", return_value=(True, "")
            ) as write_atomic:
                ok, err = adapter.retune_control_frequency(162.55)

            self.assertTrue(ok)
            self.assertEqual("", err)
            write_atomic.assert_called_once()

    def test_retune_multi_source_requires_runtime_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            playlist_path = os.path.join(tmp, "playlist.xml")
            self._write_multi_playlist(playlist_path, [851_312_500, 852_312_500, 853_312_500])
            adapter = digital.SdrtrunkAdapter()
            with mock.patch.object(digital, "DIGITAL_PLAYLIST_PATH", playlist_path), mock.patch.object(
                adapter, "_runtime_retune_control_frequency", return_value=(False, False, "")
            ), mock.patch.object(
                adapter, "runtime_retune_available", return_value=False
            ), mock.patch.object(
                digital, "_write_playlist_tree_atomic", return_value=(True, "")
            ) as write_atomic:
                ok, err = adapter.retune_control_frequency(852.3125)

            self.assertFalse(ok)
            self.assertIn("requires runtime backend", err)
            write_atomic.assert_not_called()

    def test_profile_apply_writes_when_alias_seed_changes_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(tmp, "p1")
            os.makedirs(profile_dir, exist_ok=True)
            with open(os.path.join(profile_dir, "control_channels.txt"), "w", encoding="utf-8") as f:
                f.write("162.4000\n")
            with open(os.path.join(profile_dir, "talkgroups.csv"), "w", encoding="utf-8") as f:
                f.write("DEC,HEX,Mode,Alpha Tag,Description,Tag\n")
                f.write("1001,3E9,D,Alpha One,Alpha One,Law\n")
                f.write("1002,3EA,D,Alpha Two,Alpha Two,Law\n")

            playlist_path = os.path.join(tmp, "playlist.xml")
            with open(playlist_path, "w", encoding="utf-8") as f:
                f.write(
                    "<playlist>"
                    "<alias list=\"P1\" name=\"Alpha One\" group=\"Law\">"
                    "<id type=\"talkgroup\" value=\"1001\" protocol=\"APCO25\"/>"
                    "<id type=\"broadcastChannel\" channel=\"DIGITAL\"/>"
                    "</alias>"
                    "<channel system=\"P25\" enabled=\"true\" order=\"1\" name=\"p1\">"
                    "<alias_list_name>P1</alias_list_name>"
                    "<source_configuration type=\"sourceConfigTuner\" source_type=\"TUNER\" frequency=\"162400000\"/>"
                    "</channel>"
                    "<stream type=\"icecastHTTPConfiguration\" sample_rate=\"16000\" user_name=\"source\" bitrate=\"32\" channels=\"1\" mount_point=\"/DIGITAL.mp3\" public=\"false\" inline=\"true\" host=\"127.0.0.1\" delay=\"0\" port=\"8000\" enabled=\"true\" password=\"x\" maximum_recording_age=\"600000\" name=\"DIGITAL\"><format>MP3</format></stream>"
                    "</playlist>"
                )

            adapter = digital.SdrtrunkAdapter()
            with mock.patch.object(digital, "DIGITAL_PLAYLIST_PATH", playlist_path), mock.patch.object(
                digital, "_sync_stream_configuration", return_value=False
            ), mock.patch.object(
                digital, "_write_playlist_tree_atomic", return_value=(True, "")
            ) as write_atomic:
                ok, err = adapter._apply_profile_runtime(profile_dir, "p1")

            self.assertTrue(ok, msg=err)
            self.assertEqual("", err)
            write_atomic.assert_called_once()


class AliasSeedRowsTests(unittest.TestCase):
    def test_read_profile_alias_seed_rows_merges_grouped_and_plain_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            grouped = os.path.join(tmp, "talkgroups_with_group.csv")
            plain = os.path.join(tmp, "talkgroups.csv")

            with open(grouped, "w", encoding="utf-8") as f:
                f.write("DEC,HEX,Mode,Alpha Tag,Description,Group,Tag\n")
                f.write("1001,3e9,D,Alpha One,Alpha One,Dispatch,Law\n")

            with open(plain, "w", encoding="utf-8") as f:
                f.write("DEC,HEX,Mode,Alpha Tag,Description,Tag\n")
                f.write("1001,3e9,D,Alpha One Plain,Alpha One Plain,Law\n")
                f.write("2002,7d2,ALL,Bravo Two,Bravo Two,Law\n")

            rows = digital._read_profile_alias_seed_rows(tmp)
            by_dec = {dec: (name, group) for dec, name, group in rows}

            self.assertIn("1001", by_dec)
            self.assertIn("2002", by_dec)
            self.assertEqual(("Alpha One", "Dispatch"), by_dec["1001"])
            self.assertEqual(("Bravo Two", "Law"), by_dec["2002"])

    def test_read_profile_alias_seed_rows_filters_encrypted_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = os.path.join(tmp, "talkgroups.csv")
            with open(plain, "w", encoding="utf-8") as f:
                f.write("DEC,HEX,Mode,Alpha Tag,Description,Tag\n")
                f.write("3003,bbb,DE,Encrypted,Encrypted,Law\n")
                f.write("4004,fa4,D,Clear,Clear,Law\n")

            rows = digital._read_profile_alias_seed_rows(tmp)
            decs = {dec for dec, _name, _group in rows}

            self.assertNotIn("3003", decs)
            self.assertIn("4004", decs)


class HealthPayloadTests(unittest.TestCase):
    def _base_payload(self) -> dict:
        return {
            "digital_active": True,
            "digital_mixer_enabled": False,
            "digital_mixer_active": False,
            "digital_allocation_snapshot_age_ms": 0,
            "digital_last_apply_error": "",
            "icecast_active": True,
            "icecast_mounts": [],
            "icecast_expected_mounts": [],
            "combined_config_stale": False,
            "rtl_restart_required": False,
        }

    def _base_dongles(self) -> dict:
        return {
            "dongles": {
                "status": "ideal",
                "missing_expected_serials": [],
                "slow_expected_serials": [],
            }
        }

    def test_health_payload_marks_disabled_mixer_as_healthy(self):
        payload = handlers._build_health_payload(
            status_payload=self._base_payload(),
            system_stats=self._base_dongles(),
            analog_air_preflight={"state": "healthy", "reasons": []},
            analog_ground_preflight={"state": "healthy", "reasons": []},
            digital_preflight={"state": "healthy", "reasons": []},
            compile_state={"status": "healthy"},
        )

        self.assertIn("sdrtrunk", payload["subsystems"])
        self.assertIn("mixer", payload["subsystems"])
        self.assertIn("digital_allocation", payload["subsystems"])
        self.assertEqual("healthy", payload["subsystems"]["mixer"]["state"])
        reason_codes = {
            str((reason or {}).get("code") or "")
            for reason in payload["subsystems"]["mixer"].get("reasons") or []
        }
        self.assertIn("MIXER_DISABLED_BY_DESIGN", reason_codes)

    def test_health_payload_flags_enabled_inactive_mixer_and_stale_scheduler(self):
        status_payload = self._base_payload()
        status_payload["digital_mixer_enabled"] = True
        status_payload["digital_mixer_active"] = False
        status_payload["digital_allocation_snapshot_age_ms"] = 4500

        with mock.patch.object(handlers, "HEALTH_SCHEDULER_STALE_MS", 3000):
            payload = handlers._build_health_payload(
                status_payload=status_payload,
                system_stats=self._base_dongles(),
                analog_air_preflight={"state": "healthy", "reasons": []},
                analog_ground_preflight={"state": "healthy", "reasons": []},
                digital_preflight={"state": "healthy", "reasons": []},
                compile_state={"status": "healthy"},
            )

        self.assertEqual("failed", payload["subsystems"]["mixer"]["state"])
        self.assertEqual("degraded", payload["subsystems"]["digital_allocation"]["state"])

    def test_health_payload_marks_sdrtrunk_failed_when_digital_tuners_missing(self):
        status_payload = self._base_payload()
        status_payload["digital_tuner_missing_serials"] = ["56919602"]

        payload = handlers._build_health_payload(
            status_payload=status_payload,
            system_stats=self._base_dongles(),
            analog_air_preflight={"state": "healthy", "reasons": []},
            analog_ground_preflight={"state": "healthy", "reasons": []},
            digital_preflight={"state": "failed", "reasons": []},
            compile_state={"status": "healthy"},
        )

        self.assertEqual("failed", payload["subsystems"]["sdrtrunk"]["state"])
        reason_codes = {
            str((reason or {}).get("code") or "")
            for reason in payload["subsystems"]["sdrtrunk"].get("reasons") or []
        }
        self.assertIn("SDRTRUNK_TUNER_MISSING", reason_codes)

    def test_health_payload_flags_monopolized_analog_scan(self):
        status_payload = self._base_payload()
        status_payload["analog_scan_health"] = {
            "airband": {
                "monopolized": True,
                "dominant_frequency": "118.4000",
                "dominant_ratio": 0.96,
                "profile_frequency_count": 6,
            },
            "ground": {"monopolized": False},
        }

        payload = handlers._build_health_payload(
            status_payload=status_payload,
            system_stats=self._base_dongles(),
            analog_air_preflight={"state": "healthy", "reasons": []},
            analog_ground_preflight={"state": "healthy", "reasons": []},
            digital_preflight={"state": "healthy", "reasons": []},
            compile_state={"status": "healthy"},
        )

        self.assertEqual("degraded", payload["subsystems"]["analog_scan"]["state"])
        reason_codes = {
            str((reason or {}).get("code") or "")
            for reason in payload["subsystems"]["analog_scan"].get("reasons") or []
        }
        self.assertIn("ANALOG_SCAN_MONOPOLIZED", reason_codes)


class DigitalStatusAliasTests(unittest.TestCase):
    def test_digital_status_aliases_follow_latest_hit_row(self):
        payload = handlers._digital_status_with_hit_aliases(
            {
                "digital_last_label": "",
                "digital_last_time": 0,
                "digital_last_tgid": "",
            },
            [
                {"source": "airband", "freq": "118.6000", "ts": 10.0},
                {
                    "source": "digital",
                    "label_full": "Davidson County Transit Authority - Dispatch",
                    "label": "Dispatch",
                    "tgid": "10560",
                    "ts": 1234.5,
                },
            ],
        )

        self.assertEqual("Davidson County Transit Authority - Dispatch", payload["digital_last_label"])
        self.assertEqual("10560", payload["digital_last_tgid"])
        self.assertEqual("10560", payload["last_hit_digital"])
        self.assertEqual("Davidson County Transit Authority - Dispatch", payload["last_hit_digital_label"])
        self.assertEqual(1234500, payload["last_hit_digital_time"])


class LatencyToneTests(unittest.TestCase):
    def test_sanitize_simple_mount_name_rejects_paths(self):
        self.assertEqual("latency-analog.mp3", handlers._sanitize_simple_mount_name("/latency-analog.mp3"))
        self.assertEqual("", handlers._sanitize_simple_mount_name("../latency.mp3"))
        self.assertEqual("", handlers._sanitize_simple_mount_name("latency/tone.mp3"))
        self.assertEqual("", handlers._sanitize_simple_mount_name("latency tone.mp3"))

    def test_start_latency_tone_injection_errors_without_ffmpeg(self):
        with mock.patch.object(handlers.shutil, "which", return_value=None):
            ok, err, payload = handlers._start_latency_tone_injection(
                target="analog",
                mount="latency-analog.mp3",
                frequency_hz=1000,
                duration_ms=3000,
                pre_roll_ms=500,
                bitrate_kbps=32,
                sample_rate_hz=16000,
            )

        self.assertFalse(ok)
        self.assertIn("ffmpeg", err.lower())
        self.assertFalse(payload.get("active"))


if __name__ == "__main__":
    unittest.main()
