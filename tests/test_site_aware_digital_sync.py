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
    counts = {21557: 14, 26838: 4, 18863: 23, 41154: 10}
    bases = {21557: 769_500_000, 26838: 769_100_000, 18863: 856_000_000, 41154: 851_900_000}
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
    def test_sea_isle_picks_the_local_simulcast(self):
        hit = self.resolve([NJICS], self._at(SEA_ISLE))[NJICS]
        self.assertEqual(hit["site_name"], "Cape May County Simulcast")
        self.assertEqual(len(hit["controls"]), 14)

    def test_radius_first_ranking_does_not_win(self):
        """STR I Trailer's 85-mile radius must not beat the local cell."""
        hit = self.resolve([NJICS], self._at(SEA_ISLE))[NJICS]
        self.assertNotEqual(hit["site_name"], "STR I Trailer")

    def test_distance_first_ranking_does_not_win(self):
        """Davidson County Services is nearer but is not the trunk site."""
        hit = self.resolve([MTRTRS], self._at(NASHVILLE))[MTRTRS]
        self.assertEqual(hit["site_name"], "Davidson County Simulcast")
        self.assertEqual(len(hit["controls"]), 23)

    def test_one_site_per_system_by_default(self):
        """Mixing two sites' control channels is what caused the NAC spin."""
        hit = self.resolve([MTRTRS], self._at(NASHVILLE))[MTRTRS]
        self.assertEqual(hit["site_count"], 1)

    def test_a_system_from_another_state_is_not_resolved(self):
        self.assertNotIn(NJICS, self.resolve([NJICS], self._at(NASHVILLE)))

    def test_each_location_gets_its_own_system(self):
        """The whole point: drive somewhere, get that place's sites."""
        self.assertEqual(set(self.resolve([NJICS, MTRTRS], self._at(SEA_ISLE))), {NJICS})
        self.assertEqual(set(self.resolve([NJICS, MTRTRS], self._at(NASHVILLE))), {MTRTRS})

    def test_zip_is_used_when_coordinates_are_missing(self):
        state = {"lat": 0, "lon": 0, "zip": "08243", "range_miles": 50.0}
        routes_center = routes._hp_sync_center
        try:
            routes._hp_sync_center = lambda s: SEA_ISLE if s.get("zip") == "08243" else None
            hit = self.resolve([NJICS], state)[NJICS]
        finally:
            routes._hp_sync_center = routes_center
        self.assertEqual(hit["site_name"], "Cape May County Simulcast")

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
    def test_defaults_to_one(self):
        prior = os.environ.pop("SB3_OP25_SITES_PER_SYSTEM", None)
        try:
            self.assertEqual(routes._op25_sites_per_system(), 1)
        finally:
            if prior is not None:
                os.environ["SB3_OP25_SITES_PER_SYSTEM"] = prior

    def test_garbage_value_is_not_fatal(self):
        prior = os.environ.get("SB3_OP25_SITES_PER_SYSTEM")
        os.environ["SB3_OP25_SITES_PER_SYSTEM"] = "banana"
        try:
            self.assertEqual(routes._op25_sites_per_system(), 1)
        finally:
            if prior is None:
                os.environ.pop("SB3_OP25_SITES_PER_SYSTEM", None)
            else:
                os.environ["SB3_OP25_SITES_PER_SYSTEM"] = prior


if __name__ == "__main__":
    unittest.main()
