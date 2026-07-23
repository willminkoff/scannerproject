"""Tests for the Favorites Builder wizard endpoints (sb3.ui.wizard).

The bug these guard against: every wizard endpoint fell through to the
not-implemented catch-all, so the State dropdown came up empty. These assert the
static tier is always served and the DB tier degrades to an empty-list-plus-note
rather than an error, on a host with no HomePatrol dump (which is the CI case
and Neptune's case).
"""

from __future__ import annotations

import unittest

from sb3.ui import wizard


class TestStaticTier(unittest.TestCase):
    def test_countries_has_usa(self):
        c = wizard.countries()
        self.assertTrue(c["ok"])
        self.assertTrue(any(r["country_id"] == 1 for r in c["countries"]))

    def test_states_returns_51_us_rows(self):
        s = wizard.states(1)
        self.assertTrue(s["ok"])
        self.assertEqual(len(s["states"]), 51)   # 50 states + DC

    def test_states_use_radioreference_ids(self):
        rows = {r["abbr"]: r["state_id"] for r in wizard.states(1)["states"]}
        # The IDs that anchor forward-compat with a real dump.
        self.assertEqual(rows["AL"], 1)
        self.assertEqual(rows["DC"], 9)
        self.assertEqual(rows["TN"], 43)          # the local anchor
        self.assertEqual(rows["WY"], 51)

    def test_states_sorted_by_name(self):
        names = [r["name"] for r in wizard.states(1)["states"]]
        self.assertEqual(names, sorted(names))

    def test_every_state_has_name_and_abbr(self):
        for r in wizard.states(1)["states"]:
            self.assertTrue(r["name"])
            self.assertEqual(len(r["abbr"]), 2)

    def test_non_us_country_says_so_without_pretending(self):
        s = wizard.states(2)
        self.assertTrue(s["ok"])
        self.assertEqual(s["states"], [])
        self.assertIn("note", s)


class TestDbTierDegradesGracefully(unittest.TestCase):
    """With no HomePatrol dump present, deeper stages must not error."""

    def setUp(self):
        # Guarantee the no-DB path regardless of the host running the tests.
        self._orig = wizard._resolve_db_path
        wizard._resolve_db_path = lambda: None
        self.addCleanup(lambda: setattr(wizard, "_resolve_db_path", self._orig))

    def test_counties_empty_but_selectable_with_note(self):
        c = wizard.counties(43)
        self.assertTrue(c["ok"])
        self.assertEqual([r["county_id"] for r in c["counties"]], [0])  # All Counties
        self.assertIn("note", c)

    def test_systems_empty_with_note(self):
        s = wizard.systems(43, 0, "digital", "statewide")
        self.assertTrue(s["ok"])
        self.assertEqual(s["systems"], [])
        self.assertIn("note", s)

    def test_channels_empty_with_note(self):
        c = wizard.channels("digital", "123")
        self.assertTrue(c["ok"])
        self.assertEqual(c["channels"], [])
        self.assertIn("note", c)

    def test_channels_non_integer_system_id_does_not_raise(self):
        c = wizard.channels("digital", "not-a-number")
        self.assertTrue(c["ok"])
        self.assertEqual(c["channels"], [])

    def test_scan_state_is_empty_but_valid(self):
        s = wizard.scan_state(None)
        self.assertTrue(s["ok"])
        self.assertEqual(s["state"], {})
        self.assertFalse(s["persisted"])


class TestZeroByteDbIsTreatedAsAbsent(unittest.TestCase):
    def test_placeholder_db_does_not_count(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            placeholder = Path(d) / "hpdb.db"
            placeholder.write_bytes(b"")            # 0 bytes, the gitignored stub
            orig = wizard.os.getenv
            wizard.os.getenv = lambda k, default="": (str(placeholder)
                                                      if k == "HPDB_DB_PATH" else default)
            try:
                self.assertIsNone(wizard._resolve_db_path())
            finally:
                wizard.os.getenv = orig


class TestServerRouting(unittest.TestCase):
    """The endpoints reach the payloads through the real server + query parsing."""

    @classmethod
    def setUpClass(cls):
        import threading
        from sb3.ui import server
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.srv.server_address[1]
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def _get(self, path):
        import json
        import urllib.request
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return r.status, json.load(r)

    def test_states_endpoint_populates(self):
        st, body = self._get("/api/scan/favorites-wizard/states?country_id=1")
        self.assertEqual(st, 200)
        self.assertEqual(len(body["states"]), 51)

    def test_state_id_query_is_parsed(self):
        st, body = self._get("/api/scan/favorites-wizard/counties?state_id=43")
        self.assertEqual(st, 200)
        self.assertTrue(body["ok"])

    def test_no_longer_not_implemented(self):
        # The regression: this used to return the catch-all marker.
        _st, body = self._get("/api/scan/favorites-wizard/countries")
        self.assertNotIn("not-implemented-in-3.1", str(body))
        self.assertTrue(body["ok"])


if __name__ == "__main__":
    unittest.main()
