import os
import tempfile
import unittest
from unittest import mock

from ui import actions
from ui import handlers
from ui.hp_state import HPState
from ui import profile_config


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
            actions, "write_combined_config", return_value=False
        ), mock.patch.object(
            actions, "restart_rtl", return_value=(True, "")
        ):
            result = actions.action_apply_controls("ground", 43.4, "dbfs", 10.0, -64.0)

        self.assertEqual(200, result["status"])
        self.assertTrue(result["payload"]["ok"])
        resolve_path.assert_called_once_with("ground")
        write_controls.assert_called_once_with("/tmp/effective.conf", 43.4, "dbfs", 10.0, -64.0)

    def test_action_apply_batch_writes_to_resolved_path(self):
        with mock.patch.object(actions, "resolve_controls_path", return_value="/tmp/effective.conf") as resolve_path, mock.patch.object(
            actions, "write_controls", return_value=True
        ) as write_controls, mock.patch.object(
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

    def test_parse_service_tags_normalizes_json_csv_and_scalar(self):
        self.assertEqual([2, 15, 3], handlers.parse_service_tags("[2, \"15\", 3, \"x\", 2]"))
        self.assertEqual([4, 15, 30], handlers.parse_service_tags("4,15,30,4"))
        self.assertEqual([7], handlers.parse_service_tags("7"))


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


if __name__ == "__main__":
    unittest.main()
