"""Tests for location-aware trunked control-channel resolution.

The bug these guard against: a trunked favorite froze its system's ENTIRE
control-channel list into the saved entry at build time (171 frequencies for
NJICS — every site in New Jersey), and the op25 push handed that whole list to
the decoder.  op25 locked one site, latched its NAC, then sat on a frequency
belonging to a different site and rejected every frame:

    p25_framer::nid_codeword: NAC check fail: received=39b, expected=39a

The decoder reported a locked control channel the whole time and produced
pure silence.  Control channels are a fact about *where the box is*, so they
are now resolved at push time from HPDB rather than stored in the favorite.

The ranking is the subtle part and has its own tests below: the site gate in
ui.scan_mode_controller ranks widest-radius-first, which picks the wrong site
for NJICS, and ranking by distance alone picks the wrong site for MTRTRS.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
import urllib.request
from pathlib import Path

from sb3.ui import routes

# Coordinates used throughout.
SEA_ISLE = (39.1534, -74.6927)
NASHVILLE = (36.1627, -86.7816)

NJICS, MTRTRS = 7021, 7078


def _build_db(path: Path) -> None:
    """A miniature HPDB holding the three sites the ranking must separate."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE trunk_sites (
            site_id INTEGER PRIMARY KEY, source_file TEXT NOT NULL, trunk_id INTEGER,
            site_name TEXT NOT NULL, latitude REAL, longitude REAL, radius REAL,
            site_mode TEXT, bandplan TEXT, width TEXT, shape TEXT);
        CREATE TABLE trunk_freqs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_file TEXT NOT NULL,
            site_id INTEGER NOT NULL, tfreq_id TEXT, freq_hz INTEGER, lcn TEXT,
            UNIQUE(site_id, freq_hz, lcn));
        CREATE TABLE trunk_systems (
            trunk_id INTEGER PRIMARY KEY, source_file TEXT NOT NULL,
            system_name TEXT, protocol TEXT, state_id INTEGER);
        CREATE TABLE counties (county_id INTEGER PRIMARY KEY, state_id INTEGER, name TEXT);
        CREATE TABLE states (state_id INTEGER PRIMARY KEY, country_id INTEGER, name TEXT, abbr TEXT);
        CREATE TABLE entity_areas (
            entity_kind TEXT, entity_id INTEGER, record_type TEXT,
            state_id INTEGER, county_id INTEGER);
        """
    )
    sites = [
        # NJICS: the correct site is NEARER but has a much SMALLER radius than
        # the deployable trailer, so radius-first ranking gets this wrong.
        (21557, NJICS, "Cape May County Simulcast", 39.126, -74.8112, 17.0),
        (26838, NJICS, "STR I Trailer", 39.90, -75.50, 85.0),
        # The site that ACTUALLY decodes at Sea Isle City: outside its own
        # advertised radius (20.4 mi away, 10 mi radius), so a strict radius
        # gate would exclude it entirely.
        (42190, NJICS, "Atlantic City Simulcast", 39.3805, -74.4492, 10.0),
        # MTRTRS: the correct site is FARTHER than the small services cell, so
        # distance-first ranking gets this wrong.
        (18863, MTRTRS, "Davidson County Simulcast", 36.1660, -86.7840, 25.0),
        (41154, MTRTRS, "Davidson County Services", 36.1600, -86.7800, 18.2),
    ]
    for site_id, trunk_id, name, lat, lon, radius in sites:
        conn.execute(
            "INSERT INTO trunk_sites VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (site_id, "_MultipleStates.hpd", trunk_id, name, lat, lon, radius,
             "AUTO", "Standard", "Wide", None),
        )
    # Frequency counts are the discriminator: main simulcast cells carry more.
    counts = {21557: 14, 26838: 4, 42190: 6, 18863: 23, 41154: 10}
    bases = {21557: 769_500_000, 26838: 769_100_000, 42190: 855_200_000,
             18863: 856_000_000, 41154: 851_900_000}
    for site_id, n in counts.items():
        for i in range(n):
            conn.execute(
                "INSERT INTO trunk_freqs(source_file, site_id, tfreq_id, freq_hz, lcn) VALUES (?,?,?,?,?)",
                ("_MultipleStates.hpd", site_id, "0", bases[site_id] + i * 12_500, str(i + 1)),
            )
    conn.execute("INSERT INTO trunk_systems VALUES (?,?,?,?,?)",
                 (NJICS, "_MultipleStates.hpd", "NJICS", "P25X2_TDMA", 0))
    conn.execute("INSERT INTO trunk_systems VALUES (?,?,?,?,?)",
                 (MTRTRS, "_MultipleStates.hpd", "MTRTRS", "P25", 0))
    conn.commit()
    conn.close()


class _DBCase(unittest.TestCase):
    def setUp(self):
        self.db = Path(tempfile.mkdtemp()) / "hpdb.db"
        _build_db(self.db)

    def resolve(self, system_ids, state):
        return routes._resolve_site_controls(system_ids, state, db_path=str(self.db))

    @staticmethod
    def _at(lat_lon, **extra):
        state = {"lat": lat_lon[0], "lon": lat_lon[1], "range_miles": 50.0,
                 "nationwide_systems": True}
        state.update(extra)
        return state


class TestSiteSelection(_DBCase):
    def names(self, system_id, state):
        return [c["site_name"] for c in self.resolve([system_id], state).get(system_id, [])]

    def test_local_simulcast_is_surveyed_first(self):
        self.assertEqual(self.names(NJICS, self._at(SEA_ISLE))[0],
                         "Cape May County Simulcast")

    def test_the_site_that_actually_decodes_is_a_candidate(self):
        """Atlantic City is OUTSIDE its advertised radius but is what works.

        A strict radius gate drops it, which is why candidacy is not gated on
        the site's own radius.
        """
        self.assertIn("Atlantic City Simulcast", self.names(NJICS, self._at(SEA_ISLE)))

    def test_bigger_cell_beats_nearer_cell_inside_radius(self):
        """Davidson County Services is nearer and is the wrong site."""
        self.assertEqual(self.names(MTRTRS, self._at(NASHVILLE))[0],
                         "Davidson County Simulcast")

    def test_several_candidates_are_offered_for_survey(self):
        self.assertGreater(len(self.names(NJICS, self._at(SEA_ISLE))), 1)

    def test_a_system_from_another_state_is_not_resolved(self):
        self.assertNotIn(NJICS, self.resolve([NJICS], self._at(NASHVILLE)))

    def test_each_location_gets_its_own_system(self):
        """The whole point: drive somewhere, get that place's sites."""
        self.assertEqual(set(self.resolve([NJICS, MTRTRS], self._at(SEA_ISLE))), {NJICS})
        self.assertEqual(set(self.resolve([NJICS, MTRTRS], self._at(NASHVILLE))), {MTRTRS})

    def test_small_scan_range_does_not_shrink_the_survey(self):
        """range_miles is a favorites filter; a control channel carries further."""
        self.assertIn("Atlantic City Simulcast",
                      self.names(NJICS, self._at(SEA_ISLE, range_miles=15.0)))

    def test_zip_is_used_when_coordinates_are_missing(self):
        state = {"lat": 0, "lon": 0, "zip": "08243", "range_miles": 50.0}
        prior = routes._hp_sync_center
        try:
            routes._hp_sync_center = lambda s: SEA_ISLE if s.get("zip") == "08243" else None
            names = [c["site_name"] for c in self.resolve([NJICS], state)[NJICS]]
        finally:
            routes._hp_sync_center = prior
        self.assertIn("Cape May County Simulcast", names)

    def test_no_location_resolves_nothing(self):
        self.assertEqual(self.resolve([NJICS], {"lat": 0, "lon": 0, "zip": ""}), {})


class TestPushFallbackPolicy(_DBCase):
    """What happens to a system the resolver cannot place.

    _hp_push_op25 POSTs to a live op25 server, so every test here MUST stub
    the transport.  An earlier draft of this file defined a fake urlopen and
    forgot to install it; the suite then pushed a three-frequency test profile
    to the real decoder and restarted it.  Hence the belt-and-braces below:
    urlopen is patched *and* the target URL is redirected to a dead address.
    """

    def setUp(self):
        super().setUp()
        self.posted = []
        self._real_urlopen = urllib.request.urlopen

        def _blocked(req, timeout=0, *a, **kw):
            self.posted.append(getattr(req, "full_url", str(req)))
            raise AssertionError(
                "test attempted a real op25 push to %s" % getattr(req, "full_url", "?")
            )

        urllib.request.urlopen = _blocked
        self.addCleanup(setattr, urllib.request, "urlopen", self._real_urlopen)

        prior = os.environ.get("SB3_OP25_APPLY_URL")
        os.environ["SB3_OP25_APPLY_URL"] = "http://127.0.0.1:1"  # nothing listens here
        if prior is None:
            self.addCleanup(os.environ.pop, "SB3_OP25_APPLY_URL", None)
        else:
            self.addCleanup(os.environ.__setitem__, "SB3_OP25_APPLY_URL", prior)

    def _push(self, state):
        digital = {str(NJICS): {"name": "NJICS", "ccs": [769.1, 769.2, 769.3], "tgs": []}}
        orig = routes._resolve_site_controls
        try:
            routes._resolve_site_controls = lambda ids, st, db_path=None: orig(
                ids, st, db_path=str(self.db))
            return routes._hp_push_op25(digital, state)
        finally:
            routes._resolve_site_controls = orig

    def test_located_but_out_of_range_is_skipped_not_guessed(self):
        """Falling back to the stored statewide list is what broke op25."""
        out = self._push(self._at(NASHVILLE))
        self.assertIn("skipped_no_site_in_range", out)
        self.assertNotIn("sites", out)

    def test_unlocated_falls_back_to_stored_controls(self):
        out = self._push({"lat": 0, "lon": 0, "zip": ""})
        self.assertIn("unlocated_used_stored_controls", out)


class TestSitesPerSystemKnob(unittest.TestCase):
    def _with(self, value):
        prior = os.environ.get("SB3_OP25_SITES_PER_SYSTEM")
        if value is None:
            os.environ.pop("SB3_OP25_SITES_PER_SYSTEM", None)
        else:
            os.environ["SB3_OP25_SITES_PER_SYSTEM"] = value
        if prior is None:
            self.addCleanup(os.environ.pop, "SB3_OP25_SITES_PER_SYSTEM", None)
        else:
            self.addCleanup(os.environ.__setitem__, "SB3_OP25_SITES_PER_SYSTEM", prior)

    def test_defaults_to_several_candidates(self):
        """Candidates to survey, not channels scanned at once."""
        self._with(None)
        self.assertGreater(routes._op25_sites_per_system(), 1)

    def test_garbage_value_is_not_fatal(self):
        self._with("banana")
        self.assertGreater(routes._op25_sites_per_system(), 0)

    def test_never_zero(self):
        self._with("0")
        self.assertEqual(routes._op25_sites_per_system(), 1)


class TestPushEmitsSites(_DBCase):
    """The wiring that was missing.

    ui.op25_adapter already implements a site selector that scores candidates
    on real decode (control_locked, last_tsbk_age_sec, recent_monitored_tg_hits)
    and demotes ones that go stale.  It was idle because the push handed it a
    single flat control_channels_mhz list, which the adapter normalises to one
    pseudo-candidate named "Legacy Control Channel Set" — nothing to choose
    between, so nothing to promote or demote.
    """

    def setUp(self):
        super().setUp()
        self.sent = {}
        real = urllib.request.urlopen

        def _capture(req, timeout=0, *a, **kw):
            self.sent = json.loads(req.data.decode())
            raise RuntimeError("captured; no real push")

        urllib.request.urlopen = _capture
        self.addCleanup(setattr, urllib.request, "urlopen", real)

    def _push(self, state):
        digital = {str(NJICS): {"name": "NJICS", "ccs": [769.1], "tgs": []}}
        orig = routes._resolve_site_controls
        try:
            routes._resolve_site_controls = lambda ids, st, db_path=None: orig(
                ids, st, db_path=str(self.db))
            routes._hp_push_op25(digital, state)
        finally:
            routes._resolve_site_controls = orig
        return self.sent

    def test_push_carries_per_site_candidates(self):
        system = self._push(self._at(SEA_ISLE))["systems"][0]
        self.assertIn("sites", system)
        self.assertGreater(len(system["sites"]), 1)

    def test_flat_list_is_kept_for_the_log_server(self):
        """op25-log-server writes control_channels.txt from this key alone."""
        system = self._push(self._at(SEA_ISLE))["systems"][0]
        self.assertTrue(system.get("control_channels_mhz"))

    def test_flat_list_is_one_site_not_a_merge(self):
        """Merging sites is what produced the NAC check failures."""
        system = self._push(self._at(SEA_ISLE))["systems"][0]
        flat = set(system["control_channels_mhz"])
        per_site = [set(s["control_channels_mhz"]) for s in system["sites"]]
        self.assertIn(flat, per_site)
        self.assertEqual(flat, per_site[0])

    def test_each_site_carries_what_the_selector_scores_on(self):
        site = self._push(self._at(SEA_ISLE))["systems"][0]["sites"][0]
        for field in ("site_id", "site_name", "control_channels_mhz",
                      "latitude", "longitude", "distance_miles", "enabled"):
            self.assertIn(field, site)

    def test_sites_do_not_collapse_to_the_legacy_pseudo_candidate(self):
        ids = {s["site_id"] for s in self._push(self._at(SEA_ISLE))["systems"][0]["sites"]}
        self.assertNotIn("legacy:auto", ids)


if __name__ == "__main__":
    unittest.main()


class TestChirpCapacityTrim(unittest.TestCase):
    """Chirp rejects an oversized add_channel batch in full, not partially.

    The trim was hardcoded to 15 -- ground's capacity -- while airband runs
    max_channels=12 with a keepalive permanently holding one slot. An airband
    push therefore asked for 12 slots when 11 existed and chirp answered
    "channel pool exhausted: need 12, have 11", rejecting the whole batch and
    leaving the band with nothing but its keepalive.
    """

    def _run(self, n_freqs, pool_free, max_channels, removed_ids=()):
        import socket as _socket
        sent = []
        status = {"data": {"pool_free": pool_free, "max_channels": max_channels,
                           "channels": [{"id": i} for i in removed_ids]}}

        class _FakeSock:
            def __init__(self, *a, **kw): pass
            def settimeout(self, *a): pass
            def close(self): pass
            def sendto(self, payload, addr): sent.append(json.loads(payload.decode()))
            def recvfrom(self, _n):
                cmd = sent[-1]["cmd"]
                if cmd == "get_status":
                    return (json.dumps(status).encode(), addr_stub)
                return (json.dumps({"status": "ok"}).encode(), addr_stub)

        addr_stub = ("127.0.0.1", 7400)
        real = _socket.socket
        _socket.socket = _FakeSock
        try:
            freqs = [{"freq_mhz": 118.0 + i / 100.0, "label": f"ch{i}"} for i in range(n_freqs)]
            out = routes._hp_push_chirp(7400, freqs, "am")
        finally:
            _socket.socket = real
        adds = [m for m in sent if m["cmd"] == "add_channel"]
        return out, (adds[0]["args"]["channels"] if adds else [])

    def test_airband_batch_fits_the_real_pool(self):
        out, batch = self._run(n_freqs=16, pool_free=11, max_channels=12)
        self.assertEqual(len(batch), 11)
        self.assertEqual(out["capacity"], 11)
        self.assertEqual(out["dropped_over_capacity"], 5)

    def test_removed_slots_count_as_free(self):
        """Channels we just removed free their slots for the new batch."""
        out, batch = self._run(n_freqs=16, pool_free=0, max_channels=12,
                               removed_ids=("n4-0-a", "n4-1-b", "n4-2-c"))
        self.assertEqual(out["removed"], 3)
        self.assertEqual(len(batch), 3)

    def test_keepalive_is_never_removed(self):
        out, _ = self._run(n_freqs=2, pool_free=11, max_channels=12,
                           removed_ids=("keepalive-analog", "vfo", "n4-0-a"))
        self.assertEqual(out["removed"], 1)

    def test_under_capacity_is_untouched(self):
        out, batch = self._run(n_freqs=4, pool_free=11, max_channels=12)
        self.assertEqual(len(batch), 4)
        self.assertNotIn("dropped_over_capacity", out)

    def test_no_free_slots_reports_instead_of_pushing(self):
        out, batch = self._run(n_freqs=4, pool_free=0, max_channels=12)
        self.assertFalse(out["ok"])
        self.assertEqual(batch, [])
