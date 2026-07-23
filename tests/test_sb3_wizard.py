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


class TestChannelsDispatch(unittest.TestCase):
    """Both shapes must reach the right query, and empty must explain itself.

    Regression: the endpoint handled only the digital shape, so every ANALOG
    system returned empty AND claimed the database was missing — wrong on both
    counts. And a digital system whose talkgroups are all LTR-format returned a
    bare empty list, which in the UI is indistinguishable from a broken wizard.
    """

    class _FakeWiz:
        def __init__(self, rows=None):
            self.rows = rows if rows is not None else []
            self.calls = []

        def get_channels(self, system_type, system_id, text_filter=""):
            self.calls.append((system_type, system_id))
            return ("Sys", list(self.rows))

    def _with(self, wiz):
        orig = wizard._wizard
        wizard._wizard = lambda: wiz
        self.addCleanup(lambda: setattr(wizard, "_wizard", orig))

    def test_analog_reaches_the_analog_query_with_its_string_key(self):
        w = self._FakeWiz([{"id": "c1"}])
        self._with(w)
        out = wizard.channels("analog", "CountyId:2446")
        self.assertEqual(w.calls, [("analog", "CountyId:2446")])
        self.assertEqual(len(out["channels"]), 1)
        self.assertNotIn("note", out)

    def test_digital_reaches_the_digital_query(self):
        w = self._FakeWiz([{"id": "tgid:6495:1"}])
        self._with(w)
        out = wizard.channels("digital", "6495")
        self.assertEqual(w.calls, [("digital", "6495")])
        self.assertEqual(len(out["channels"]), 1)

    def test_empty_result_explains_itself_instead_of_going_silent(self):
        self._with(self._FakeWiz([]))
        out = wizard.channels("digital", "2881")
        self.assertEqual(out["channels"], [])
        self.assertIn("note", out)
        self.assertIn("LTR", out["note"])
        # Must NOT claim the database is missing — it answered.
        self.assertNotIn("not present", out["note"])

    def test_bad_id_for_type_is_reported_not_raised(self):
        class _Raiser:
            def get_channels(self, system_type, system_id, text_filter=""):
                raise ValueError("invalid literal for int()")
        self._with(_Raiser())
        out = wizard.channels("digital", "CountyId:2446")
        self.assertTrue(out["ok"])
        self.assertIn("not valid", out["note"])

    def test_limit_is_applied(self):
        self._with(self._FakeWiz([{"id": i} for i in range(10)]))
        self.assertEqual(len(wizard.channels("digital", "1", limit=3)["channels"]), 3)


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


class TestStreamProxyFlag(unittest.TestCase):
    """Audio players are silent unless /api/status says the proxy is off.

    sb3.html defaults streamProxyEnabled to true and only lowers it from
    data.stream_proxy_enabled. SB3 serves no /stream/<mount> and no /hls/, so
    omitting the field pointed every player at origin/stream/<mount> → 404 with
    no error surfaced. Regression-guard the flag AND its value.
    """

    def test_status_declares_no_stream_proxy(self):
        from unittest import mock

        from sb3 import backends
        from sb3.ui import routes

        class _S:
            def read_loaded_profiles(self):
                return {}

            def is_killed(self):
                return False

        with mock.patch.object(backends, "launchctl_loaded", return_value=[]), \
             mock.patch.object(backends, "icecast_mounts", return_value=[]), \
             mock.patch.object(backends, "sdrangel_devicesets", return_value=[]), \
             mock.patch.object(backends, "sdrangel_channels", return_value=[]), \
             mock.patch.object(backends, "mount_state",
                               side_effect=lambda m, **kw: backends.MountState(m, 200, True)):
            s = routes.build_status(_S())
        self.assertIn("stream_proxy_enabled", s)
        self.assertIs(s["stream_proxy_enabled"], False)
        # the page needs both of these to build the direct icecast URL
        self.assertEqual(s["icecast_port"], 8000)
        self.assertTrue(s["stream_mount"])
