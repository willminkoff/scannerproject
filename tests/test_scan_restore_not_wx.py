"""Stopping a band scan goes back to favorites, not to Weather Radio.

Ground was reported "stuck on WX" twice. Neptune's log shows exactly what
happened both times:

    MUTATE 2026-11-10 07:30:22 /api/profile profile=ground.nashville.noaa-wx target=ground
    MUTATE 2026-11-12 07:25:17 /api/profile profile=ground.nashville.noaa-wx target=ground

Nobody chose Weather Radio. The band-scan OFF path landed on a PROFILE because
a profile was the only thing it knew how to land on, and ground has exactly ONE
profile -- so resolveBandScanFallbackProfile's last rule, "the band's only
profile when it has exactly one", had exactly one possible destination. GMRS
off, Marine off, Rail off, CB off: all four arrive at NOAA Weather Radio.

The operator's real channels are FAVORITES, and favorites only ever reached
chirp through _hp_auto_sync, whose sole caller is POST /api/scan/state. So
nothing put them back; ground sat on WX until someone happened to save
favorites from their phone again.

POST /api/scan/restore restores ONE band's favorites. One band matters:
_hp_auto_sync pushes airband, ground AND digital, so reusing it would rewrite
airband's profile-derived pool and POST /apply-profile to the digital radio --
restarting sdrplay and two op25 units -- as a side effect of stopping a ground
scan.

Every socket is faked. Ground's chirp daemon listens on loopback, which
tests/netguard.py permits, so the guard would NOT catch a real push here.
"""

from __future__ import annotations

import json
import re
import socket as _socket
import unittest
from unittest import mock

from sb3.ui import routes
from sb3.ui import server as ui_server

ARES = [
    {"freq_mhz": 145.47, "label": "County ARES Secondary"},
    {"freq_mhz": 146.55, "label": "County ARES Tactical Simplex"},
    {"freq_mhz": 146.67, "label": "County ARES Primary"},
    {"freq_mhz": 442.80, "label": "MTEARS Nashville (Davidson Co) (19)"},
]
AIR = [{"freq_mhz": 118.6, "label": "Tower"}]


class _Bus:
    """Captures chirp datagrams instead of sending them."""

    def __init__(self, pool_ids=(), pool_free=15, max_channels=15,
                 reject=None, lo=None):
        """reject: a cmd name chirp should answer status "rejected" for.

        The first version of this fake answered {"status": "ok"} to
        everything, so no test here could produce chirp's real refusals --
        "channel pool exhausted", "channel mode mismatch", "channel already
        exists" (chirp/daemon.py) -- which are all-or-nothing AFTER
        _hp_push_chirp has already emptied the pool. That is the state this
        change now reaches on every band-scan stop, so it has to be testable.
        """
        self.sent = []
        self.addrs = []
        self._pool = list(pool_ids)
        self._free = pool_free
        self._max = max_channels
        self._reject = reject
        self._lo = lo
        bus = self

        class _FakeSock:
            def __init__(self, *a, **kw):
                pass

            def settimeout(self, *a):
                pass

            def close(self):
                pass

            def sendto(self, payload, addr):
                bus.sent.append(json.loads(payload.decode()))
                bus.addrs.append(addr)

            def recvfrom(self, _n):
                cmd = bus.sent[-1]["cmd"]
                if cmd == "get_status":
                    data = {"pool_free": bus._free, "max_channels": bus._max,
                            "channels": [{"id": i} for i in bus._pool]}
                    if bus._lo is not None:
                        data["lo_scheduler"] = bus._lo
                    return (json.dumps({"data": data}).encode(), ("127.0.0.1", 0))
                if bus._reject == cmd:
                    return (json.dumps({
                        "status": "rejected",
                        "error": "channel already exists: ['n4-0-x']",
                    }).encode(), ("127.0.0.1", 0))
                return (json.dumps({"status": "ok"}).encode(), ("127.0.0.1", 0))

        self.sock = _FakeSock

    def __enter__(self):
        self._real = _socket.socket
        _socket.socket = self.sock
        return self

    def __exit__(self, *exc):
        _socket.socket = self._real
        return False

    def cmds(self, name):
        return [m for m in self.sent if m["cmd"] == name]

    def ports(self):
        return sorted({a[1] for a in self.addrs})


def _with_favorites(airs, grounds, digital=None):
    """Patch the favorites source, not hp_state.json on disk."""
    return mock.patch.object(
        routes, "_hp_extract_pool",
        return_value=(airs, grounds, digital if digital is not None else {}))


def _with_state():
    return mock.patch.object(
        routes, "_hp_state_load",
        return_value=mock.Mock(to_dict=lambda: {"custom_favorites": []}))


def _with_readable_state():
    """The route now verifies the state FILE is readable before trusting an
    empty favorites list, so tests have to supply one."""
    import contextlib, json as _j, tempfile

    @contextlib.contextmanager
    def _cm():
        mod = routes._hp_state_module()
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            _j.dump({"custom_favorites": []}, fh)
            path = fh.name
        with mock.patch.object(mod, "_DEFAULT_STATE_PATH", path):
            yield path

    return _cm()


class TheRouteIsReachable(unittest.TestCase):
    def test_it_is_dispatched(self):
        src = open(ui_server.__file__, encoding="utf-8").read()
        self.assertIn('p == "/api/scan/restore"', src)
        self.assertIn("routes.hp_scan_restore(form, state)", src)

    def test_it_is_logged_as_a_mutation(self):
        # Anything that rewrites a radio's channel pool has to appear in the
        # MUTATE log, or the next "who changed my band?" is unanswerable --
        # which is the whole point of 600b0f1.
        self.assertIn("/api/scan/restore", ui_server.Handler.MUTATING_PATHS)


class ItRestoresTheRightBand(unittest.TestCase):
    def test_ground_favorites_go_to_the_ground_daemon(self):
        with _with_state(), _with_favorites(AIR, ARES), _Bus() as bus:
            out = routes.hp_scan_restore({"target": "ground"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["channels"], 4)
        self.assertEqual(bus.ports(), [7401], "ground must only touch 7401")
        added = bus.cmds("add_channel")[0]["args"]["channels"]
        self.assertEqual([round(c["freq_mhz"], 2) for c in added],
                         [145.47, 146.55, 146.67, 442.8])

    def test_airband_favorites_go_to_the_airband_daemon(self):
        with _with_state(), _with_favorites(AIR, ARES), _Bus() as bus:
            out = routes.hp_scan_restore({"target": "airband"})
        self.assertEqual(out["channels"], 1)
        self.assertEqual(bus.ports(), [7400])

    def test_air_is_accepted_as_an_alias(self):
        with _with_state(), _with_favorites(AIR, ARES), _Bus() as bus:
            self.assertEqual(routes.hp_scan_restore({"target": "air"})["target"],
                             "airband")
            self.assertEqual(bus.ports(), [7400])

    def test_restoring_ground_never_touches_airband_or_digital(self):
        # The reason this is not _hp_auto_sync: that pushes all three, so
        # stopping a ground scan would rewrite airband's pool and POST
        # /apply-profile to the digital radio, restarting sdrplay and two
        # op25 units.
        with _with_state(), _with_favorites(AIR, ARES), \
             mock.patch.object(routes, "_hp_push_op25") as push_op25, \
             mock.patch("urllib.request.urlopen") as urlopen, _Bus() as bus:
            routes.hp_scan_restore({"target": "ground"})
        push_op25.assert_not_called()
        urlopen.assert_not_called()
        self.assertNotIn(7400, bus.ports())

    def test_an_unknown_target_is_refused_without_a_socket(self):
        for bad in ("", "digital", "wx", "GROUNDS"):
            with self.subTest(bad=bad):
                with _Bus() as bus:
                    out = routes.hp_scan_restore({"target": bad})
                self.assertFalse(out["ok"])
                self.assertEqual(bus.sent, [])


class NoFavoritesIsNotAnError(unittest.TestCase):
    """The caller needs to tell 'nothing to restore' apart from 'it broke', so
    it can fall back to the old profile behaviour instead of emptying the
    band."""

    def test_empty_reports_zero_channels_and_sends_nothing(self):
        with _with_state(), _with_favorites([], []), _Bus() as bus:
            out = routes.hp_scan_restore({"target": "ground"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["channels"], 0)
        self.assertIs(out["empty"], True)
        self.assertEqual(bus.sent, [], "an empty restore must not open a socket")

    def test_an_unreadable_state_file_is_an_error_not_an_empty(self):
        """The defect that made the fix able to recreate the outage.

        HPState.load() NEVER raises -- a missing file, unparseable JSON or a
        non-dict payload all return cls.default(), whose custom_favorites is
        []. So the route would have answered empty:True over a broken state
        file, the UI would have fallen back to a profile, and for ground that
        profile is Weather Radio. The first draft of this test patched
        _hp_state_load to raise OSError, which production never does, so it
        passed while validating a fiction.

        The transport is mocked even though a correct route never reaches it:
        loopback is where ground's real daemon lives and tests/netguard.py
        permits loopback, so the guard would NOT stop a real push from here.
        """
        import tempfile
        mod = routes._hp_state_module()
        with tempfile.TemporaryDirectory() as d:
            for label, payload in (("missing", None), ("corrupt", "{not json"),
                                   ("not-an-object", "[1, 2, 3]")):
                with self.subTest(case=label):
                    path = f"{d}/{label}.json"
                    if payload is not None:
                        open(path, "w", encoding="utf-8").write(payload)
                    with mock.patch.object(mod, "_DEFAULT_STATE_PATH", path), _Bus() as bus:
                        out = routes.hp_scan_restore({"target": "ground"})
                    self.assertFalse(out["ok"], out)
                    self.assertNotIn("empty", out)
                    self.assertEqual(bus.sent, [])

    def test_a_raising_loader_is_still_an_error(self):
        with mock.patch.object(routes, "_hp_state_load", side_effect=OSError("nope")), \
             _with_readable_state(), _Bus() as bus:
            out = routes.hp_scan_restore({"target": "ground"})
        self.assertFalse(out["ok"])
        self.assertEqual(bus.sent, [])


class AKeepaliveNoLongerStrandsAnLoCluster(unittest.TestCase):
    """It was spared on the belief that it holds the icecast mount up. That is
    false on the chirp path -- chirp/dsp/icecast_sink.py:32-38 emits valid MP3
    frames from constant-zero PCM -- and verified live on 2026-11-12: ground's
    keepalive was removed and neptune-ground.mp3 kept serving HTTP 200.

    Leaving it in cost real airtime: on 2026-11-10 one channel at 162.375,
    stranded 16 MHz from a 145-146 MHz favorites group, owned its own LO
    cluster and 38.5% of the scheduler while MTEARS on 442.80 got 2.1%."""

    def _removed(self, pool_ids):
        with _Bus(pool_ids=pool_ids) as bus:
            routes._hp_push_chirp(7401, ARES, "nfm")
        return [m["args"]["id"] for m in bus.cmds("remove_channel")]

    def test_the_wx_keepalive_is_removed(self):
        removed = self._removed(["wx-keepalive-(out-of-band)", "noaa-wx-162.400"])
        self.assertIn("wx-keepalive-(out-of-band)", removed)

    def test_every_keepalive_spelling_is_removed(self):
        ids = ["wx-keepalive-(out-of-band)", "KEEPALIVE", "ka-keepalive-1", "keepalive"]
        removed = self._removed(ids)
        for i in ids:
            with self.subTest(id=i):
                self.assertIn(i, removed)

    def test_a_stale_vfo_is_still_removed(self):
        # Unchanged behaviour; pinned so the edit above did not cost it.
        self.assertIn("vfo", self._removed(["vfo", "noaa-wx-162.400"]))

    def test_the_old_pool_is_fully_replaced(self):
        old = ["wx-keepalive-(out-of-band)"] + [f"noaa-wx-162.{n}" for n in
                                                (400, 425, 450, 475, 500, 525)]
        removed = self._removed(old)
        self.assertEqual(sorted(removed), sorted(old),
                         "a WX channel survived into the favorites pool")


class ItReportsWhatChirpAccepted(unittest.TestCase):
    """`channels` used to be len(wanted) -- what was asked for. The UI logs
    that number, so a trimmed restore told the operator channels were scanning
    that are not on the radio. _hp_push_chirp trims twice: to pool capacity and
    to the LO cluster budget."""

    def test_capacity_trim_is_reported_not_hidden(self):
        many = [{"freq_mhz": 145.0 + i / 100.0, "label": f"f{i}"} for i in range(20)]
        with _with_readable_state(), _with_favorites([], many), \
             _Bus(pool_free=6, max_channels=6) as bus:
            out = routes.hp_scan_restore({"target": "ground"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["channels"], 6, "must report what chirp took")
        self.assertEqual(out["requested"], 20)
        self.assertEqual(out["dropped"], 14)

    def test_cluster_trim_is_reported(self):
        spread = [{"freq_mhz": f, "label": f"f{f}"}
                  for f in (145.47, 162.4, 443.0, 462.5, 155.3)]
        with _with_readable_state(), _with_favorites([], spread), \
             _Bus(lo={"max_clusters": 2, "iq_bw_hz": 2.0e6}) as bus:
            out = routes.hp_scan_restore({"target": "ground"})
        self.assertGreater(out["dropped"], 0, "an over-budget spread must report a drop")
        self.assertEqual(out["channels"] + out["dropped"], out["requested"])


class AChirpRefusalIsNotASuccess(unittest.TestCase):
    """chirp rejects an add_channel batch all-or-nothing, AFTER _hp_push_chirp
    has already emptied the pool. If the route called that ok, the UI would
    land the WX profile on an empty band and log a clean stop."""

    def test_a_rejected_batch_reports_not_ok(self):
        with _with_readable_state(), _with_favorites([], ARES), \
             _Bus(reject="add_channel") as bus:
            out = routes.hp_scan_restore({"target": "ground"})
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["channels"], 0)

    def test_a_raising_push_is_a_structured_error_not_a_500(self):
        # The removal loop runs before the add, so a mid-rewrite failure can
        # leave the band empty. Propagating it gave the server's blanket
        # handler a bare HTTP 500 that told the caller nothing.
        with _with_readable_state(), _with_favorites([], ARES), \
             mock.patch.object(routes, "_hp_push_chirp", side_effect=TimeoutError("wedged")):
            out = routes.hp_scan_restore({"target": "ground"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["channels"], 0)
        self.assertIn("raised", out["error"])


def _live_toggle_body():
    """The implementation that actually RUNS.

    ui/sb3.html declares `async function applyBandScanPresetByKey` and then
    __BANDSCAN_DIRECT_POST_SHIM__ assigns window.applyBandScanPresetByKey,
    shadowing it. Every caller resolves the global at call time, so only the
    shim runs -- its own comment says so, and a fix was once applied to the
    dead copy with no effect (2026-11-09), then again while fixing this very
    WX fallback. tests/test_bandscan_toggle.py keeps the same helper; grepping
    the whole file is how a green suite gets shipped over dead code.
    """
    src = _html()
    start = src.index("// __BANDSCAN_DIRECT_POST_SHIM__")
    return src[start:src.index("</script>", start)]


def _html():
    import os
    return open(os.path.join(os.path.dirname(__file__), "..", "ui", "sb3.html"),
                encoding="utf-8").read()


class TheLiveToggleRestoresFavoritesFirst(unittest.TestCase):
    """Source assertions, and worth being honest about: there is no JS harness
    for sb3.html, so these catch the ordering being reversed or a call site
    being deleted. They would NOT catch the restore being called with a broken
    argument, or a runtime error inside it."""

    @classmethod
    def setUpClass(cls):
        cls.src = _html()
        cls.live = _live_toggle_body()

    def test_the_helper_returns_a_three_way_outcome(self):
        self.assertIn("async function restoreFavoritesForTarget(target)", self.src)
        self.assertIn("'/api/scan/restore'", self.src)
        for key in ("{ restored: n }", "{ empty: true }", "{ failed: true }"):
            with self.subTest(key=key):
                self.assertIn(key, self.src)

    def test_the_live_implementation_calls_the_restore(self):
        self.assertIn("restoreFavoritesForTarget(target)", self.live)

    def test_the_restore_precedes_the_profile_resolution_numerically(self):
        # Ordering IS the fix, and the first version of this test could not
        # fail: it indexed into a slice that began with the marker itself, so
        # its assertGreater(resolve_at, 0) was trivially true, and it SKIPPED
        # the check entirely when the resolve came first.
        r = self.live.find("restoreFavoritesForTarget(target)")
        f = self.live.find("resolveBandScanFallbackProfile(target")
        self.assertNotEqual(r, -1, "the live toggle does not restore favorites")
        self.assertNotEqual(f, -1, "the live toggle has no profile fallback at all")
        self.assertLess(r, f, "the profile fallback runs BEFORE the restore, so "
                              "ground still lands on Weather Radio")

    def test_a_failed_restore_does_not_apply_a_profile(self):
        # Treating a chirp timeout as "no favorites" is what recreates the bug.
        self.assertIn("outcome.failed", self.live)
        fail_at = self.live.index("outcome.failed")
        prof_at = self.live.index("'/api/profile'")
        self.assertLess(fail_at, prof_at)
        self.assertIn("'/api/bandscan/stop'", self.live[fail_at:prof_at])

    def test_only_a_genuine_empty_falls_through_to_a_profile(self):
        self.assertIn("outcome.empty", self.live)

    def test_the_dead_declaration_does_not_pretend_to_restore(self):
        # Keeping a restore in the shadowed copy would invite the next person
        # to "fix" it there.
        m = re.search(r"async function applyBandScanPresetByKey\([^)]*\)\s*\{(.*?)\n    \}",
                      self.src, re.S)
        self.assertIsNotNone(m)
        self.assertNotIn("restoreFavoritesForTarget", m.group(1))
        self.assertIn("this whole function is DEAD", m.group(1))

    def test_the_toggle_is_serialized(self):
        self.assertIn("serializeBandScanToggle", self.live)

    def test_the_gmrs_button_fires_the_toggle_once(self):
        # It carried BOTH an inline onclick and an addEventListener, so one
        # click ran the toggle twice; concurrently, against a threading
        # server, the loser fell through to the WX fallback.
        btn = self.src[self.src.index('id="btn-bandscan-gmrs"'):]
        btn = btn[:btn.index("</button>")]
        self.assertNotIn("onclick", btn)


if __name__ == "__main__":
    unittest.main()
