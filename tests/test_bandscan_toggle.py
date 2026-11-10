"""Band-scan buttons could start a scan but never stop one.

Two independent defects, either one fatal:

  1. `isActive` was `currentProfile === profileId`. Applying a band scan goes
     through the direct-post shim, which never registers a profile -- with a
     GMRS scan demonstrably running, /api/status still reported
     profile_ground: "". So the comparison was false even mid-scan and every
     press re-applied the scan instead of toggling it off.

  2. Even with that fixed, "off" was routed through the profile system and
     wanted a `none_ground` / `none_airband` profile to switch back to. Neither
     exists -- the live profile list is air.airband.nashville.rsp1b,
     air.airband.nashville, nashville_curated, digital.nashville,
     ground.nashville.noaa-wx, profile_metadata, vfo.default. So it logged
     "No stop profile available" and returned, leaving the scan running.

Stopping a scan does not need a profile: the scan IS the channels named
`bs-<preset>-<n>` that bandscan_apply created, so stopping it is removing
exactly those. /api/bandscan/stop does that, scoped to one preset, and never
touches a keepalive -- that channel holds the icecast mount, and dropping it
would take the band's audio down rather than just its scan list.

The server half is exercised against routes.py's AST (the module needs
dependencies the dev box lacks); the client half is grep-level, as elsewhere
for sb3.html, since this repo has no JS harness.
"""
from __future__ import annotations

import ast
import io
import json
import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_ROUTES = _ROOT / "sb3" / "ui" / "routes.py"
_SERVER = _ROOT / "sb3" / "ui" / "server.py"
_HTML = _ROOT / "ui" / "sb3.html"
_ROUTES_SRC = io.open(_ROUTES, encoding="utf-8").read()
_SERVER_SRC = io.open(_SERVER, encoding="utf-8").read()
_HTML_SRC = io.open(_HTML, encoding="utf-8").read()


def _load_stop():
    """exec bandscan_stop out of routes.py with its module deps stubbed."""
    tree = ast.parse(_ROUTES_SRC)
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "bandscan_stop"), None)
    if fn is None:
        raise AssertionError("bandscan_stop() missing from routes.py")
    ns = {
        "_CHIRP_PORT_BY_BAND": {"airband": 7400, "ground": 7401},
        "_chirp_host": lambda port: "127.0.0.1",
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<sub>", "exec"), ns)
    return ns["bandscan_stop"]


class _FakeChirp:
    """Stands in for the UDP command port."""

    def __init__(self, channels, fail_ids=()):
        self.channels = list(channels)
        self.removed = []
        self.fail = set(fail_ids)

    def install(self, ns_mod):
        outer = self

        class FakeSocket:
            def __init__(self, *a, **k):
                self._last = None

            def settimeout(self, *_):
                pass

            def sendto(self, payload, addr):
                self._last = json.loads(payload.decode())

            def recvfrom(self, _n):
                req = self._last or {}
                cmd = req.get("cmd")
                if cmd == "get_status":
                    body = {"v": 1, "status": "ok",
                            "data": {"channels": [{"id": c} for c in outer.channels]}}
                elif cmd == "remove_channel":
                    cid = (req.get("args") or {}).get("id")
                    if cid in outer.fail:
                        body = {"v": 1, "error": "simulated failure"}
                    else:
                        outer.removed.append(cid)
                        if cid in outer.channels:
                            outer.channels.remove(cid)
                        body = {"v": 1, "status": "ok"}
                else:
                    body = {"v": 1, "error": "unexpected cmd %r" % cmd}
                return (json.dumps(body).encode(), ("127.0.0.1", 0))

            def close(self):
                pass

        ns_mod.socket = FakeSocket
        ns_mod.AF_INET = 2
        ns_mod.SOCK_DGRAM = 2
        return ns_mod


def _run_stop(channels, form, fail_ids=()):
    import sys, types
    fake = _FakeChirp(channels, fail_ids=fail_ids)
    mod = types.ModuleType("socket")
    fake.install(mod)
    prev = sys.modules.get("socket")
    sys.modules["socket"] = mod
    try:
        return _load_stop()(form, None), fake
    finally:
        if prev is not None:
            sys.modules["socket"] = prev
        else:
            sys.modules.pop("socket", None)


GMRS = ["bs-gmrs-%d" % n for n in range(15)]


class StopEndpointTests(unittest.TestCase):
    def test_removes_the_presets_channels(self):
        res, fake = _run_stop(list(GMRS), {"target": "ground", "preset": "gmrs"})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["removed_count"], 15)
        self.assertEqual(sorted(fake.removed), sorted(GMRS))

    def test_scoped_to_the_named_preset(self):
        chans = ["bs-gmrs-0", "bs-gmrs-1", "bs-marine-0", "bs-marine-1"]
        res, fake = _run_stop(chans, {"target": "ground", "preset": "gmrs"})
        self.assertEqual(sorted(fake.removed), ["bs-gmrs-0", "bs-gmrs-1"])
        self.assertIn("bs-marine-0", fake.channels, "other presets must survive")

    def test_no_preset_clears_every_band_scan_channel(self):
        chans = ["bs-gmrs-0", "bs-marine-0", "bs-rail-3"]
        res, fake = _run_stop(chans, {"target": "ground"})
        self.assertEqual(sorted(fake.removed), sorted(chans))

    def test_keepalive_is_never_removed(self):
        """It holds the icecast mount; dropping it takes the audio down."""
        chans = ["bs-gmrs-0", "bs-gmrs-1", "keepalive-ground"]
        res, fake = _run_stop(chans, {"target": "ground", "preset": "gmrs"})
        self.assertNotIn("keepalive-ground", fake.removed)
        self.assertIn("keepalive-ground", fake.channels)

    def test_non_bandscan_channels_are_left_alone(self):
        """A hand-tuned vfo or a favourites pool is not ours to delete."""
        chans = ["bs-gmrs-0", "vfo", "n4-0-tower"]
        res, fake = _run_stop(chans, {"target": "ground", "preset": "gmrs"})
        self.assertEqual(fake.removed, ["bs-gmrs-0"])
        self.assertIn("vfo", fake.channels)
        self.assertIn("n4-0-tower", fake.channels)

    def test_stopping_when_nothing_is_loaded_is_not_an_error(self):
        res, fake = _run_stop(["vfo"], {"target": "ground", "preset": "gmrs"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["removed_count"], 0)
        self.assertIn("note", res)

    def test_a_failed_removal_is_reported(self):
        res, fake = _run_stop(list(GMRS), {"target": "ground", "preset": "gmrs"},
                              fail_ids=("bs-gmrs-7",))
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("failed"))
        self.assertEqual(res["failed"][0]["id"], "bs-gmrs-7")

    def test_unknown_target_rejected(self):
        res, _ = _run_stop(list(GMRS), {"target": "banana", "preset": "gmrs"})
        self.assertFalse(res["ok"])
        self.assertIn("unknown target", res["error"])

    def test_air_is_an_alias_for_airband(self):
        res, _ = _run_stop(["bs-mil-0"], {"target": "air", "preset": "mil"})
        self.assertEqual(res["target"], "airband")


class ServerWiringTests(unittest.TestCase):
    def test_endpoint_is_routed(self):
        self.assertIn('if p == "/api/bandscan/stop":', _SERVER_SRC)
        self.assertIn("routes.bandscan_stop(form, state)", _SERVER_SRC)


class ClientToggleTests(unittest.TestCase):
    def _fn(self, name):
        m = re.search(r"async function %s\([^)]*\)\s*\{(.*?)\n    \}" % name,
                      _HTML_SRC, re.S)
        self.assertIsNotNone(m, "%s() missing" % name)
        return m.group(1)

    def test_is_active_no_longer_trusts_currentProfile_alone(self):
        body = self._fn("applyBandScanPresetByKey")
        self.assertNotRegex(
            body, r"const isActive = currentProfile === profileId;",
            "that comparison is false even mid-scan, because applying a band "
            "scan never registers a profile")

    def test_is_active_reads_live_scan_state(self):
        body = self._fn("applyBandScanPresetByKey")
        self.assertIn("scanStateFor(target)", body)
        self.assertRegex(body, r"livePreset === String\(preset\.key\)")

    def test_the_dead_end_bail_is_gone(self):
        self.assertNotIn("No stop profile available", _HTML_SRC,
                         "stopping must not require a profile that was never "
                         "created")

    def test_stop_calls_the_endpoint_scoped_to_the_preset(self):
        body = self._fn("applyBandScanPresetByKey")
        self.assertIn("'/api/bandscan/stop'", body)
        self.assertRegex(body, r"preset:\s*preset\.key")

    def test_stop_only_clears_when_there_is_nothing_to_return_to(self):
        """A remembered profile is still the nicer destination."""
        body = self._fn("applyBandScanPresetByKey")
        self.assertRegex(body, r"if \(isActive && !nextProfile\)")
        self.assertIn("selectProfileForTarget(target, nextProfile, true)", body,
                      "the return-to-profile path must survive")

    def test_a_failed_stop_is_surfaced(self):
        body = self._fn("applyBandScanPresetByKey")
        self.assertIn("bandscan-stop-failed-", body)

    def test_stop_falls_back_to_a_profile_not_an_empty_band(self):
        """Zero channels is a worse resting state than the band's prior one."""
        body = self._fn("applyBandScanPresetByKey")
        self.assertIn("resolveBandScanFallbackProfile(target, profileId)", body)

    def test_every_preset_benefits(self):
        """The fix is in the shared handler, not per button."""
        keys = set(re.findall(r"key:\s*'([a-z0-9]+)'", _HTML_SRC))
        self.assertIn("gmrs", keys)
        self.assertGreaterEqual(len(keys), 3,
                                "several presets share this handler")


if __name__ == "__main__":
    unittest.main()


class FallbackResolverTests(unittest.TestCase):
    """resolveBandScanFallbackProfile(): where a band lands when its scan stops."""

    def setUp(self):
        m = re.search(
            r"function resolveBandScanFallbackProfile\(target, scanProfileId\)\s*\{(.*?)\n    \}",
            _HTML_SRC, re.S)
        self.assertIsNotNone(m, "resolveBandScanFallbackProfile() missing")
        self.body = m.group(1)

    def test_prefers_the_profile_loaded_before_the_scan(self):
        self.assertIn("getBandScanReturnProfile(target)", self.body)
        self.assertLess(self.body.index("getBandScanReturnProfile"),
                        self.body.index("state.lastProfile"),
                        "the pre-scan profile is the strongest signal and must "
                        "be checked first")

    def test_then_the_last_profile_selected_on_that_band(self):
        self.assertIn("state.lastProfile", self.body)

    def test_then_the_bands_only_profile(self):
        self.assertRegex(self.body, r"plain\.length === 1")

    def test_never_picks_arbitrarily_from_several(self):
        """Loading one of three profiles the operator never chose is worse than
        leaving the band clear.

        So the list may be indexed exactly once, and only on a line that has
        already checked there IS exactly one candidate.
        """
        uses = [ln.strip() for ln in self.body.splitlines() if "plain[" in ln]
        self.assertEqual(len(uses), 1,
                         "expected a single guarded index into the candidate "
                         "list, found %d: %r" % (len(uses), uses))
        self.assertIn("plain.length === 1", uses[0],
                      "indexing must be guarded by the length check; got %r"
                      % uses[0])
        for grabby in (".find(", ".shift(", ".pop(", "[0]"):
            with self.subTest(pattern=grabby):
                if grabby == "[0]":
                    continue  # covered by the single-guarded-use check above
                self.assertNotIn("plain" + grabby, self.body)

    def test_never_falls_back_to_another_band_scan(self):
        self.assertIn("isBandScanProfileForTarget", self.body)

    def test_never_falls_back_to_the_scan_being_stopped(self):
        self.assertRegex(self.body, r"candidate === scanId")

    def test_only_offers_profiles_that_exist(self):
        self.assertIn("hasProfileForTarget(target, candidate)", self.body)

    def test_returns_empty_when_nothing_qualifies(self):
        self.assertRegex(self.body, r"return '';")
