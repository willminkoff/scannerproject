from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


class SiteAwareDigitalProfilesTests(unittest.TestCase):
    @staticmethod
    def _load_script_module(script_name: str):
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / script_name
        spec = importlib.util.spec_from_file_location(f"test_{script_name.replace('-', '_')}", script)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _write_profile_basics(profile_dir: Path) -> None:
        (profile_dir / "control_channels.txt").write_text("856.9375\n857.4375\n", encoding="utf-8")
        (profile_dir / "talkgroups.csv").write_text(
            "DEC,HEX,Mode,Alpha Tag,Description,Tag\n"
            "3207,C87,D,Police Dispatch,Police Dispatch,Svc 1\n",
            encoding="utf-8",
        )
        (profile_dir / "talkgroups_listen.json").write_text(
            json.dumps(
                {
                    "default_listen": True,
                    "items": {"3207": True},
                    "talkgroups": {"3207": {"listen": True}},
                }
            )
            + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _create_homepatrol_db(db_path: Path) -> None:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE trunk_systems (trunk_id INTEGER PRIMARY KEY, system_name TEXT)")
            conn.execute(
                "CREATE TABLE trunk_sites (site_id INTEGER PRIMARY KEY, trunk_id INTEGER, site_name TEXT, latitude REAL, longitude REAL, radius REAL)"
            )
            conn.execute("CREATE TABLE trunk_freqs (site_id INTEGER, freq_hz INTEGER, lcn TEXT)")
            conn.execute("CREATE TABLE trunk_groups (tgroup_id INTEGER PRIMARY KEY, trunk_id INTEGER, group_name TEXT)")
            conn.execute(
                "CREATE TABLE talkgroups (tgroup_id INTEGER, dec_tgid TEXT, mode TEXT, alpha_tag TEXT, service_tag TEXT)"
            )
            conn.execute("INSERT INTO trunk_systems(trunk_id, system_name) VALUES (?, ?)", (7078, "MTRTRS"))
            conn.executemany(
                "INSERT INTO trunk_sites(site_id, trunk_id, site_name, latitude, longitude, radius) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (18863, 7078, "Davidson County Simulcast", 36.17, -86.78, 20.0),
                    (41154, 7078, "Davidson County Services", 36.15, -86.81, 20.0),
                    (49999, 7078, "Zero Control Site", 36.20, -86.80, 20.0),
                ],
            )
            conn.executemany(
                "INSERT INTO trunk_freqs(site_id, freq_hz, lcn) VALUES (?, ?, ?)",
                [
                    (18863, 856937500, "1"),
                    (18863, 857437500, "2"),
                    (41154, 855912500, "1"),
                    (41154, 856937500, "2"),
                ],
            )
            conn.execute("INSERT INTO trunk_groups(tgroup_id, trunk_id, group_name) VALUES (?, ?, ?)", (1, 7078, "Police"))
            conn.execute(
                "INSERT INTO talkgroups(tgroup_id, dec_tgid, mode, alpha_tag, service_tag) VALUES (?, ?, ?, ?, ?)",
                (1, "3207", "D", "Police Dispatch", "1"),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _create_favorites_zip(zip_path: Path) -> None:
        config = "F-List\tNashville2\tfavorites_000001.hpd\t\tOn\n"
        favorite = "\n".join(
            [
                "Trunk\tTrunkId=7078\t\tMTRTRS\tOff\tP25\tP25",
                "Site\tSiteId=18863\tTrunkId=7078\tDavidson County Simulcast\tOff\t36.170000\t-86.780000\t20.0",
                "T-Freq\tTFreqId=1\tSiteId=18863\t\tOff\t856937500\t1",
                "Site\tTrunkId=7078\tOverflow Site\tOff\t36.180000\t-86.790000\t10.0",
                "T-Freq\tTFreqId=2\t\t\tOff\t857437500\t2",
                "T-Group\tTGroupId=1\tTrunkId=7078\tPolice",
                "TGID\tTGID=1\tTGroupId=1\tPolice Dispatch\tOff\t3207\tD\t1",
            ]
        ) + "\n"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("HPCOPY/favorites_lists/favorites_lists.config", config)
            zf.writestr("HPCOPY/favorites_lists/favorites_000001.hpd", favorite)

    def test_homepatrol_db_build_profile_emits_site_aware_systems_json_and_union_controls(self):
        module = self._load_script_module("homepatrol_db.py")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "homepatrol.db"
            out_dir = tmp_path / "profiles"
            self._create_homepatrol_db(db_path)

            rc = module.cmd_build_profile(
                argparse.Namespace(
                    db=str(db_path),
                    system="MTRTRS",
                    site=[],
                    group=[],
                    talkgroup=[],
                    out=str(out_dir),
                    profile_name="",
                    profile_slug="",
                    max_talkgroups=0,
                )
            )
            self.assertEqual(0, rc)

            profile_dir = out_dir / "mtrtrs"
            systems = json.loads((profile_dir / "systems.json").read_text(encoding="utf-8"))
            self.assertEqual("7078", systems["systems"][0]["system_id"])
            self.assertEqual(["41154", "18863"], [site["site_id"] for site in systems["systems"][0]["sites"]])
            self.assertEqual(
                [855912500, 856937500, 857437500],
                [int(round(float(line.strip()) * 1_000_000)) for line in (profile_dir / "control_channels.txt").read_text(encoding="utf-8").splitlines() if line.strip()],
            )

    def test_homepatrol_favorites_export_emits_site_aware_systems_json(self):
        module = self._load_script_module("homepatrol_favorites.py")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            zip_path = tmp_path / "HPCOPY.zip"
            out_dir = tmp_path / "profiles"
            self._create_favorites_zip(zip_path)

            rc = module.cmd_export(
                argparse.Namespace(
                    zip=str(zip_path),
                    favorite="Nashville2",
                    out=str(out_dir),
                    profile_name="",
                    profile_slug="",
                )
            )
            self.assertEqual(0, rc)

            profile_dir = out_dir / "nashville2"
            systems = json.loads((profile_dir / "systems.json").read_text(encoding="utf-8"))
            sites = systems["systems"][0]["sites"]
            self.assertEqual("18863", sites[0]["site_id"])
            self.assertTrue(any(str(site["site_id"]).startswith("fav:") for site in sites))
            self.assertEqual(
                [856937500, 857437500],
                [int(round(float(line.strip()) * 1_000_000)) for line in (profile_dir / "control_channels.txt").read_text(encoding="utf-8").splitlines() if line.strip()],
            )

    def test_digital_profile_audit_passes_legacy_profile_without_systems_json(self):
        module = self._load_script_module("digital_profile_audit.py")
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = Path(tmp)
            self._write_profile_basics(profile_dir)
            result = module.audit_profile(profile_dir)
            self.assertEqual([], result["errors"])

    def test_digital_profile_audit_passes_site_aware_profile(self):
        module = self._load_script_module("digital_profile_audit.py")
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = Path(tmp)
            self._write_profile_basics(profile_dir)
            (profile_dir / "systems.json").write_text(
                json.dumps(
                    {
                        "systems": [
                            {
                                "name": "MTRTRS",
                                "system_id": "7078",
                                "sites": [
                                    {
                                        "site_id": "18863",
                                        "site_name": "Davidson County Simulcast",
                                        "control_channels_hz": [856937500, 857437500],
                                        "enabled": True,
                                    },
                                    {
                                        "site_id": "41154",
                                        "site_name": "Davidson County Services",
                                        "control_channels_hz": [855912500],
                                        "enabled": False,
                                    },
                                ],
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            result = module.audit_profile(profile_dir)
            self.assertEqual([], result["errors"])

    def test_digital_profile_audit_fix_upgrades_flat_systems_json_to_sites(self):
        module = self._load_script_module("digital_profile_audit.py")
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = Path(tmp)
            self._write_profile_basics(profile_dir)
            (profile_dir / "systems.json").write_text(
                json.dumps(
                    {
                        "systems": [
                            {
                                "name": "MTRTRS",
                                "system_id": "7078",
                                "control_channels_mhz": [856.9375, 857.4375, 856.9375],
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            rc = module.main(["--profile", str(profile_dir), "--fix"])
            self.assertEqual(0, rc)

            payload = json.loads((profile_dir / "systems.json").read_text(encoding="utf-8"))
            self.assertEqual(
                {
                    "systems": [
                        {
                            "name": "MTRTRS",
                            "system_id": "7078",
                            "sites": [
                                {
                                    "site_id": "legacy:auto",
                                    "site_name": "Legacy Control Channel Set",
                                    "control_channels_hz": [856937500, 857437500],
                                    "enabled": True,
                                }
                            ],
                        }
                    ]
                },
                payload,
            )

    def test_legacy_flat_systems_json_normalizes_to_synthetic_site(self):
        module = self._load_script_module("digital_profile_audit.py")
        with tempfile.TemporaryDirectory() as tmp:
            systems_path = Path(tmp) / "systems.json"
            systems_path.write_text(
                json.dumps({"systems": [{"name": "MTRTRS", "control_channels_hz": [856937500]}]}) + "\n",
                encoding="utf-8",
            )
            payload, errors, warnings, changed = module._load_and_normalize_systems(systems_path, fix_mode=False)
            self.assertEqual([], errors)
            self.assertEqual([], warnings)
            self.assertTrue(changed)
            self.assertEqual("legacy:auto", payload["systems"][0]["sites"][0]["site_id"])


if __name__ == "__main__":
    unittest.main()
