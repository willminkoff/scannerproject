from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from ui.hp_favorites_wizard import HPFavoritesWizard


class HPFavoritesWizardTests(unittest.TestCase):
    def _build_db(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        try:
            conn.executescript(
                """
                CREATE TABLE trunk_systems (
                    trunk_id INTEGER,
                    system_name TEXT
                );
                CREATE TABLE trunk_groups (
                    tgroup_id INTEGER,
                    trunk_id INTEGER,
                    group_name TEXT
                );
                CREATE TABLE talkgroups (
                    tgroup_id INTEGER,
                    dec_tgid TEXT,
                    alpha_tag TEXT,
                    service_tag INTEGER,
                    mode TEXT
                );
                CREATE TABLE trunk_sites (
                    site_id INTEGER,
                    trunk_id INTEGER
                );
                CREATE TABLE trunk_freqs (
                    site_id INTEGER,
                    freq_hz INTEGER
                );
                """
            )
            conn.execute(
                "INSERT INTO trunk_systems(trunk_id, system_name) VALUES (?, ?)",
                (7078, "Middle Tennessee Regional Trunked Radio System"),
            )
            conn.execute(
                "INSERT INTO trunk_groups(tgroup_id, trunk_id, group_name) VALUES (?, ?, ?)",
                (9001, 7078, "Vanderbilt University"),
            )
            conn.execute(
                "INSERT INTO talkgroups(tgroup_id, dec_tgid, alpha_tag, service_tag, mode) VALUES (?, ?, ?, ?, ?)",
                (9001, "3207", "Police Dispatch", 1, "D"),
            )
            conn.execute(
                "INSERT INTO talkgroups(tgroup_id, dec_tgid, alpha_tag, service_tag, mode) VALUES (?, ?, ?, ?, ?)",
                (9001, "3208", "Police Dispatch (Encrypted)", 1, "DE"),
            )
            conn.execute(
                "INSERT INTO trunk_sites(site_id, trunk_id) VALUES (?, ?)",
                (41154, 7078),
            )
            conn.execute(
                "INSERT INTO trunk_freqs(site_id, freq_hz) VALUES (?, ?)",
                (41154, 856937500),
            )
            conn.commit()
        finally:
            conn.close()

    def test_get_digital_channels_filters_encrypted_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "wizard.sqlite"
            self._build_db(db_path)
            wizard = HPFavoritesWizard(str(db_path))

            system_name, channels = wizard.get_digital_channels(7078)

        self.assertEqual("Middle Tennessee Regional Trunked Radio System", system_name)
        self.assertEqual(1, len(channels))
        self.assertEqual(3207, channels[0]["talkgroup"])
        self.assertEqual("Police Dispatch", channels[0]["alpha_tag"])


if __name__ == "__main__":
    unittest.main()
