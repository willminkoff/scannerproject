from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ui import favorites_runtime
from ui import handlers
from ui.digital_dongles import digital_first_serials


def _load_ensure_script():
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "ensure-op25-runtime.py"
    spec = importlib.util.spec_from_file_location("ensure_op25_runtime_script", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class Vdl2DigitalFirstAccountingTests(unittest.TestCase):
    def test_helper_adds_vdl2_when_unreserved(self):
        env = {
            "VDL2_RTL_SERIAL": "VDL2SERIAL",
            "OP25_VDL2_TRAFFIC_SHARE": "1",
            "OP25_VDL2_SENTINEL": "/tmp/not-reserved",
        }
        serials = digital_first_serials(["AAA", "BBB", "CCC"], env=env, exists=lambda _p: False)
        self.assertEqual(["AAA", "BBB", "CCC", "VDL2SERIAL"], serials)

    def test_helper_skips_vdl2_when_reserved(self):
        env = {
            "VDL2_RTL_SERIAL": "VDL2SERIAL",
            "OP25_VDL2_TRAFFIC_SHARE": "1",
            "OP25_VDL2_SENTINEL": "/tmp/reserved",
        }
        serials = digital_first_serials(["AAA", "BBB", "CCC"], env=env, exists=lambda _p: True)
        self.assertEqual(["AAA", "BBB", "CCC"], serials)

    def test_favorites_runtime_counts_vdl2_as_digital_when_free(self):
        with mock.patch.object(favorites_runtime, "DIGITAL_RTL_SERIAL", "AAA"), \
             mock.patch.object(favorites_runtime, "DIGITAL_RTL_SERIAL_SECONDARY", "BBB"), \
             mock.patch.object(favorites_runtime, "DIGITAL_RTL_SERIAL_TERTIARY", "CCC"), \
             mock.patch.dict("os.environ", {
                 "VDL2_RTL_SERIAL": "VDL2SERIAL",
                 "OP25_VDL2_TRAFFIC_SHARE": "1",
                 "OP25_VDL2_SENTINEL": "/tmp/not-reserved",
             }, clear=False), \
             mock.patch("ui.digital_dongles.os.path.exists", return_value=False):
            self.assertEqual(["AAA", "BBB", "CCC", "VDL2SERIAL"], favorites_runtime._digital_serials())

    def test_handlers_include_vdl2_in_digital_targets_when_free(self):
        with mock.patch.object(handlers, "DIGITAL_RTL_SERIAL", "AAA"), \
             mock.patch.object(handlers, "DIGITAL_RTL_SERIAL_SECONDARY", "BBB"), \
             mock.patch.object(handlers, "DIGITAL_RTL_SERIAL_TERTIARY", "CCC"), \
             mock.patch.dict("os.environ", {
                 "VDL2_RTL_SERIAL": "VDL2SERIAL",
                 "OP25_VDL2_TRAFFIC_SHARE": "1",
                 "OP25_VDL2_SENTINEL": "/tmp/not-reserved",
             }, clear=False), \
             mock.patch("ui.digital_dongles.os.path.exists", return_value=False):
            self.assertEqual(["AAA", "BBB", "CCC", "VDL2SERIAL"], handlers._configured_digital_serials())
            self.assertIn("VDL2SERIAL", handlers._digital_tuner_targets())

    def test_ensure_runtime_does_not_double_count_vdl2_when_allocator_already_has_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            profiles_dir = tmp_path / "profiles"
            active_profile = profiles_dir / "test_profile"
            active_profile.mkdir(parents=True)
            runtime_dir = tmp_path / "runtime"
            runtime_dir.mkdir()

            systems_json = {
                "systems": [
                    {"name": "SysA", "control_channels_mhz": [851.0]},
                    {"name": "SysB", "control_channels_mhz": [852.0]},
                    {"name": "SysC", "control_channels_mhz": [853.0]},
                    {"name": "SysD", "control_channels_mhz": [854.0]},
                ]
            }
            (active_profile / "systems.json").write_text(json.dumps(systems_json), encoding="utf-8")
            (active_profile / "talkgroups.csv").write_text("3207,Test TG\n", encoding="utf-8")
            (active_profile / "op25_system_config.json").write_text(
                json.dumps({name: {"nac": "0", "modulation": "cqpsk"} for name in ["SysA", "SysB", "SysC", "SysD"]}),
                encoding="utf-8",
            )
            active_link = tmp_path / "active"
            active_link.symlink_to(active_profile)

            assignments = {
                "assignments": [
                    {"system_name": "SysA", "preferred_tuner_serial": "SER0", "role": "control"},
                    {"system_name": "SysB", "preferred_tuner_serial": "SER1", "role": "control"},
                    {"system_name": "SysC", "preferred_tuner_serial": "SER2", "role": "control"},
                    {"system_name": "SysD", "preferred_tuner_serial": "VDL2SERIAL", "role": "control"},
                ],
                "traffic_pool": [],
                "strategy": "all_control",
                "digital_serials": ["SER0", "SER1", "SER2", "VDL2SERIAL"],
                "system_count": 4,
                "updated_at_ms": 1,
            }

            module = _load_ensure_script()
            idx_map = {"SER0": 0, "SER1": 1, "SER2": 2, "VDL2SERIAL": 3}

            with mock.patch.object(module, "DIGITAL_ACTIVE_PROFILE_LINK", str(active_link)), \
                 mock.patch.object(module, "OP25_RUNTIME_DIR", str(runtime_dir)), \
                 mock.patch.object(module, "OP25_STATUS_PORT", 8080), \
                 mock.patch.object(module, "_enumerate_rtlsdr_serial_index_map", return_value=idx_map), \
                 mock.patch("ui.dongle_allocator.load_assignments", return_value=assignments), \
                 mock.patch.object(module, "load_assignments", return_value=assignments), \
                 mock.patch.dict("os.environ", {
                     "VDL2_RTL_SERIAL": "VDL2SERIAL",
                     "OP25_VDL2_TRAFFIC_SHARE": "1",
                     "OP25_VDL2_SENTINEL": str(tmp_path / "vdl2_dongle_reserved"),
                 }, clear=False):
                rc = module.main()

            self.assertEqual(0, rc)
            multi_rx = json.loads((runtime_dir / "multi_rx.json").read_text(encoding="utf-8"))
            device_args = [dev["args"] for dev in multi_rx["devices"]]
            self.assertEqual(1, device_args.count("rtl=3"))
            self.assertNotIn("sdr_traffic2", {dev["name"] for dev in multi_rx["devices"]})


if __name__ == "__main__":
    unittest.main()
