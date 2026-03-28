"""Regression tests for the OP25 runtime bootstrap script."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


class EnsureOp25RuntimeTests(unittest.TestCase):
    @staticmethod
    def _load_script_module():
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "ensure-op25-runtime.py"
        spec = importlib.util.spec_from_file_location("ensure_op25_runtime_script", script)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        return module

    def test_build_dongle_arg_map_prefers_runtime_indices(self):
        module = self._load_script_module()
        sample = (
            "Found 5 device(s):\n"
            "  0:  Nooelec, NESDR SMArt v5, SN: 70613472\n"
            "  1:  RTLSDRBlog, Blog V4, SN: 00000002\n"
            "  2:  Nooelec, NESDR SMArt v5, SN: 14306619\n"
            "  3:  Nooelec, NESDR SMArt v5, SN: 56919602\n"
            "  4:  RTLSDRBlog, Blog V4, SN: 83241970\n"
        )
        parsed = module._parse_rtl_test_device_map(sample)
        self.assertEqual(2, parsed["14306619"])
        self.assertEqual(3, parsed["56919602"])

        with unittest.mock.patch.object(
            module,
            "_enumerate_rtlsdr_serial_index_map",
            return_value=parsed,
        ):
            args_map = module._build_dongle_arg_map(["14306619", "56919602", "missing"])

        self.assertEqual("rtl=2", args_map["14306619"])
        self.assertEqual("rtl=3", args_map["56919602"])
        self.assertEqual("rtl=missing", args_map["missing"])

    def test_bootstrap_writes_coherent_runtime_artifacts(self):
        repo_root = Path(__file__).resolve().parents[1]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            profiles_dir = tmp_path / "profiles"
            active_profile = profiles_dir / "hp3_favorites_digital"
            active_profile.mkdir(parents=True)
            runtime_dir = tmp_path / "runtime"
            runtime_dir.mkdir()

            (active_profile / "systems.json").write_text(
                json.dumps({
                    "systems": [
                        {
                            "name": "7078:1",
                            "control_channels_mhz": [769.11875, 769.23125],
                        }
                    ]
                }),
                encoding="utf-8",
            )
            (active_profile / "talkgroups.csv").write_text(
                "3207,Police Dispatch\n3209,Police Tactical 1\n",
                encoding="utf-8",
            )
            (active_profile / "op25_system_config.json").write_text(
                json.dumps({
                    "7078:1": {
                        "nac": "0",
                        "modulation": "cqpsk",
                    }
                }),
                encoding="utf-8",
            )

            active_link = tmp_path / "active"
            active_link.symlink_to(active_profile)

            assignments_path = tmp_path / "assignments.json"
            assignments_path.write_text(
                json.dumps({
                    "assignments": [
                        {
                            "system_name": "7078:1",
                            "preferred_tuner_serial": "14306619",
                            "role": "control",
                        }
                    ],
                    "traffic_pool": ["56919602"],
                    "strategy": "single_system",
                    "digital_serials": ["14306619", "56919602"],
                    "system_count": 1,
                    "updated_at_ms": 1,
                }),
                encoding="utf-8",
            )

            stale_trunk = runtime_dir / "trunk.tsv"
            stale_trunk.write_text(
                '"Sysname"\t"Control Channel List"\t"Offset"\t"NAC"\t"Modulation"\t"TGID Tags File"\t"Whitelist"\t"Blacklist"\t"Center Frequency"\n'
                '"6355:1"\t"856.48750"\t"0"\t"0"\t"cqpsk"\t""\t""\t""\t""\n',
                encoding="utf-8",
            )

            module = self._load_script_module()
            with unittest.mock.patch.object(module, "DIGITAL_ACTIVE_PROFILE_LINK", str(active_link)), \
                unittest.mock.patch.object(module, "OP25_RUNTIME_DIR", str(runtime_dir)), \
                unittest.mock.patch.object(module, "OP25_STATUS_PORT", 8080), \
                unittest.mock.patch.object(module, "_enumerate_rtlsdr_serial_index_map", return_value={"14306619": 2, "56919602": 3}), \
                unittest.mock.patch("ui.dongle_allocator.load_assignments", return_value=json.loads(assignments_path.read_text(encoding="utf-8"))), \
                unittest.mock.patch.object(module, "load_assignments", return_value=json.loads(assignments_path.read_text(encoding="utf-8"))):
                rc = module.main()
            self.assertEqual(0, rc)

            trunk = (runtime_dir / "trunk.tsv").read_text(encoding="utf-8")
            multi_rx = json.loads((runtime_dir / "multi_rx.json").read_text(encoding="utf-8"))
            tags = (runtime_dir / "tgid_tags.tsv").read_text(encoding="utf-8")
            whitelist = (runtime_dir / "whitelist.tsv").read_text(encoding="utf-8")
            start_sh = (runtime_dir / "start.sh").read_text(encoding="utf-8")
            instances = json.loads((runtime_dir / "instances.json").read_text(encoding="utf-8"))

            self.assertIn('"7078:1"', trunk)
            self.assertNotIn('"6355:1"', trunk)
            self.assertEqual("7078:1", multi_rx["trunking"]["chans"][0]["sysname"])
            self.assertEqual("7078:1", multi_rx["channels"][0]["trunking_sysname"])
            self.assertEqual("rtl=2", multi_rx["devices"][0]["args"])
            self.assertEqual("rtl=3", multi_rx["devices"][1]["args"])
            self.assertIn("3207\tPolice Dispatch", tags)
            self.assertIn("3209\tPolice Tactical 1", tags)
            self.assertEqual("3207\n3209\n", whitelist)
            self.assertIn("multi_rx.py", start_sh)
            self.assertTrue(instances)
            self.assertEqual("7078:1", instances[0]["system_name"])


if __name__ == "__main__":
    unittest.main()
