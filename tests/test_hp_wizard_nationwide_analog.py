"""Tests for nationwide-scope analog systems in the Favorites Builder wizard.

The bug these guard against: ``get_analog_systems`` filtered on
``state_id = ?`` alone, but HPDB files the nationwide conventional systems —
every ARTCC among them — under the ``_MultipleStates`` pseudo-state
(``state_id = 0``).  Picking a real state and the Nationwide scope therefore
returned an empty list, so New York (ZNY) and Washington (ZDC) Center were
unreachable from the UI no matter which state was selected.  The digital query
already allowed ``ts.state_id = 0``; the analog one did not.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from ui.hp_favorites_wizard import HPFavoritesWizard

_NJ = 31


class TestNationwideAnalogSystems(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp()) / "hpdb.db"
        conn = sqlite3.connect(tmp)
        conn.execute(
            """
            CREATE TABLE conventional_systems (
                system_key TEXT PRIMARY KEY,
                source_file TEXT NOT NULL,
                system_name TEXT NOT NULL,
                state_id INTEGER,
                county_id INTEGER,
                agency_id INTEGER,
                category TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO conventional_systems VALUES (?,?,?,?,?,?,?)",
            [
                # Nationwide: filed under _MultipleStates / state_id 0.
                ("AgencyId:2246", "_MultipleStates.hpd",
                 "New York (ZNY) Air Route Traffic Control Center", 0, None, 2246, "Conventional"),
                ("AgencyId:2247", "_MultipleStates.hpd",
                 "Washington (ZDC) Air Route Traffic Control Center", 0, None, 2247, "Conventional"),
                # Statewide and county systems in the state actually selected.
                ("AgencyId:9001", "NewJersey.hpd", "NJ State Police", _NJ, None, 9001, "Conventional"),
                ("CountyId:555", "NewJersey.hpd", "Cape May County Fire", _NJ, 555, None, "Conventional"),
                # A nationwide system must not leak in from some other state.
                ("AgencyId:9002", "Nevada.hpd", "Nevada Highway Patrol", 32, None, 9002, "Conventional"),
            ],
        )
        conn.commit()
        conn.close()
        self.wiz = HPFavoritesWizard(str(tmp))

    def _names(self, **kwargs) -> set[str]:
        return {r["name"] for r in self.wiz.get_analog_systems(**kwargs)}

    def test_nationwide_scope_reaches_the_artccs(self):
        names = self._names(state_id=_NJ, county_id=0, scope="nationwide")
        self.assertIn("New York (ZNY) Air Route Traffic Control Center", names)
        self.assertIn("Washington (ZDC) Air Route Traffic Control Center", names)

    def test_nationwide_scope_works_from_any_state(self):
        """The ARTCCs are not NJ-specific — selecting Nevada must find them too."""
        names = self._names(state_id=32, county_id=0, scope="nationwide")
        self.assertIn("Washington (ZDC) Air Route Traffic Control Center", names)

    def test_nationwide_rows_are_tagged_nationwide(self):
        rows = self.wiz.get_analog_systems(state_id=_NJ, county_id=0, scope="nationwide")
        self.assertTrue(rows)
        self.assertEqual({r["scope"] for r in rows}, {"nationwide"})

    def test_text_filter_still_applies_to_nationwide(self):
        names = self._names(state_id=_NJ, county_id=0, scope="nationwide", text_filter="washington")
        self.assertEqual(names, {"Washington (ZDC) Air Route Traffic Control Center"})

    def test_nationwide_systems_do_not_leak_into_statewide(self):
        names = self._names(state_id=_NJ, county_id=0, scope="statewide")
        self.assertEqual(names, {"NJ State Police"})

    def test_nationwide_systems_do_not_leak_into_county(self):
        names = self._names(state_id=_NJ, county_id=555, scope="county")
        self.assertEqual(names, {"Cape May County Fire"})

    def test_other_states_still_excluded(self):
        names = self._names(state_id=_NJ, county_id=0, scope="statewide")
        self.assertNotIn("Nevada Highway Patrol", names)


if __name__ == "__main__":
    unittest.main()
