"""Regression tests for the OP25 runtime bootstrap script."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class EnsureOp25RuntimeTests(unittest.TestCase):
    def test_bootstrap_writes_coherent_runtime_artifacts(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "ensure-op25-runtime.py"

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

            env = os.environ.copy()
            env.update({
                "DIGITAL_ACTIVE_PROFILE_LINK": str(active_link),
                "DIGITAL_PROFILES_DIR": str(profiles_dir),
                "OP25_RUNTIME_DIR": str(runtime_dir),
                "DONGLE_ASSIGNMENTS_PATH": str(assignments_path),
                "OP25_STATUS_PORT": "8080",
            })

            result = subprocess.run(
                [sys.executable, str(script)],
                cwd=str(repo_root),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                0,
                result.returncode,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )

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
            self.assertIn("3207\tPolice Dispatch", tags)
            self.assertIn("3209\tPolice Tactical 1", tags)
            self.assertEqual("3207\n3209\n", whitelist)
            self.assertIn("multi_rx.py", start_sh)
            self.assertTrue(instances)
            self.assertEqual("7078:1", instances[0]["system_name"])


if __name__ == "__main__":
    unittest.main()
