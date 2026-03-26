import os
import tempfile
import unittest
from unittest import mock

from ui import actions
from ui import favorites_runtime
from ui import handlers
from ui import managed_analog_controls
from ui import profile_config


def _write_runtime_profile(path, *, airband, freqs, labels, squelch_dbfs, gain=32.8):
    freqs_text = ", ".join(f"{float(freq):.4f}" for freq in freqs)
    labels_text = ", ".join(f'"{label}"' for label in labels)
    modulation = '"am"' if airband else '"nfm"'
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
                    f"      modulation = {modulation};",
                    f"      squelch_threshold = {int(round(float(squelch_dbfs)))};  # UI_CONTROLLED",
                    "    }",
                    "  );",
                    "});",
                    "",
                ]
            )
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


class StatusAlignmentTests(unittest.TestCase):
    def test_health_payload_flags_monopolized_analog_scan(self):
        payload = handlers._build_health_payload(
            status_payload={
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
                "analog_scan_health": {
                    "airband": {
                        "monopolized": True,
                        "dominant_frequency": "118.4000",
                        "dominant_ratio": 0.96,
                        "profile_frequency_count": 6,
                    },
                    "ground": {"monopolized": False},
                },
            },
            system_stats={
                "dongles": {
                    "status": "ideal",
                    "missing_expected_serials": [],
                    "slow_expected_serials": [],
                }
            },
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


if __name__ == "__main__":
    unittest.main()
