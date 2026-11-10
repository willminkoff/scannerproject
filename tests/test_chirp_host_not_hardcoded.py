"""The chirp command paths must not know a real radio's address by default.

Three times on 2026-11-09 and once more on 2026-11-10 08:05, airband's
10-channel pool was wiped down to a single ``vfo`` at 119.35 MHz. The chirp
command-audit log caught the last one exactly:

    08:05:25  remove_channel x10  from=<laptop>   (the whole n4-* pool)
    08:05:26  add_channel n=1 vfo@119.35  from=<laptop>

That source is a laptop, not the sb3-ui host -- so the commands never went
through the UI at all. The cause was a constant:

    _CHIRP_HOSTS_BY_PORT = {7400: "100.114.219.115", 7401: "127.0.0.1"}

Airband's real address, baked in, with no override. Importing ``sb3.ui.routes``
was therefore enough to be able to reach the live radio, and anything that
exercised the tune or apply path did reach it -- ``pytest`` included, since
``_tune_chirp_band`` removes every channel then adds one called ``vfo`` and
tests/test_tune_snapshot_and_logging.py uses ``119.35`` as its fixture.

Ground never once suffered this, because ground was already ``127.0.0.1``. That
asymmetry was the whole bug, so these tests pin the symmetry: with nothing set
in the environment, EVERY band resolves to loopback, and reaching a real radio
takes a deliberate deployment setting.

They drive the real modules and assert on their real module-level state. They
are deliberately not a re-implementation of the lookup -- this repo has twice
shipped a green suite over broken code by exec'ing functions out of the AST.
"""

from __future__ import annotations

import importlib
import ipaddress
import os
import unittest
from unittest import mock

import sb3.chirp_applier as chirp_applier
import sb3.ui.routes as routes

_HOST_VARS = ("SB3_CHIRP_AIRBAND_HOST", "SB3_CHIRP_GROUND_HOST")


def _reload(module, env):
    """Reload ``module`` with exactly ``env`` applied over a cleared host env."""
    patched = {k: v for k, v in os.environ.items() if k not in _HOST_VARS}
    patched.update(env)
    with mock.patch.dict(os.environ, patched, clear=True):
        return importlib.reload(module)


def _is_routable(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return True  # a hostname is just as routable as a dotted quad
    return not (addr.is_loopback or addr.is_unspecified)


class ChirpHostDefaultsToLoopback(unittest.TestCase):
    """With an empty environment, nothing may point at a real radio."""

    @classmethod
    def tearDownClass(cls):
        # Leave both modules reflecting the ambient environment again, so a
        # later test in the same process does not inherit a cleared one.
        importlib.reload(routes)
        importlib.reload(chirp_applier)

    def test_routes_host_table_is_all_loopback_without_env(self):
        mod = _reload(routes, {})
        for port, host in mod._CHIRP_HOSTS_BY_PORT.items():
            self.assertFalse(
                _is_routable(host),
                f"port {port} resolves to {host!r} with no environment set; a "
                f"test importing this module could command a real radio",
            )

    def test_applier_band_table_is_all_loopback_without_env(self):
        mod = _reload(chirp_applier, {})
        for band, cfg in mod._BAND_CFG.items():
            host = str(cfg.get("host", ""))
            self.assertFalse(
                _is_routable(host),
                f"band {band!r} resolves to {host!r} with no environment set",
            )

    def test_every_band_is_covered_not_just_airband(self):
        # The original bug was one band pointing off-box while the other did
        # not. Assert the tables are complete, so a new band cannot quietly
        # reintroduce the asymmetry by having no entry at all.
        mod = _reload(chirp_applier, {})
        self.assertEqual(set(mod._BAND_CFG), set(mod._CHIRP_HOST_ENV))
        rmod = _reload(routes, {})
        self.assertEqual(
            set(rmod._CHIRP_HOSTS_BY_PORT), set(rmod._CHIRP_HOST_ENV_BY_PORT)
        )

    def test_vfo_shares_airbands_variable_because_it_is_airband(self):
        # vfo is not a separate daemon: _BAND_CFG points it at port 7400, the
        # airband daemon. If it ever gets its own variable, a deployment that
        # sets only SB3_CHIRP_AIRBAND_HOST would send vfo tunes to loopback.
        mod = _reload(chirp_applier, {})
        self.assertEqual(mod._BAND_CFG["vfo"]["port"], mod._BAND_CFG["airband"]["port"])
        self.assertEqual(mod._CHIRP_HOST_ENV["vfo"], mod._CHIRP_HOST_ENV["airband"])


class ChirpHostHonoursTheEnvironment(unittest.TestCase):
    """Production still has to be able to reach the real radio."""

    @classmethod
    def tearDownClass(cls):
        importlib.reload(routes)
        importlib.reload(chirp_applier)

    def test_routes_uses_the_airband_variable(self):
        mod = _reload(routes, {"SB3_CHIRP_AIRBAND_HOST": "10.1.2.3"})
        self.assertEqual(mod._CHIRP_HOSTS_BY_PORT[7400], "10.1.2.3")
        self.assertEqual(mod._chirp_host(7400), "10.1.2.3")
        self.assertEqual(mod._CHIRP_HOSTS_BY_PORT[7401], "127.0.0.1")

    def test_applier_uses_the_airband_variable_for_airband_and_vfo(self):
        mod = _reload(chirp_applier, {"SB3_CHIRP_AIRBAND_HOST": "10.1.2.3"})
        self.assertEqual(mod._BAND_CFG["airband"]["host"], "10.1.2.3")
        self.assertEqual(mod._BAND_CFG["vfo"]["host"], "10.1.2.3")
        self.assertEqual(mod._BAND_CFG["ground"]["host"], "127.0.0.1")

    def test_ground_variable_is_independent(self):
        mod = _reload(routes, {"SB3_CHIRP_GROUND_HOST": "10.9.9.9"})
        self.assertEqual(mod._CHIRP_HOSTS_BY_PORT[7401], "10.9.9.9")
        self.assertEqual(mod._CHIRP_HOSTS_BY_PORT[7400], "127.0.0.1")

    def test_blank_and_whitespace_fall_back_to_loopback(self):
        # An env var exported empty by a launchd plist or a `export VAR=` line
        # must not become the literal host "" -- sendto("") raises, and a
        # half-configured deployment should fail safe, not fail weird.
        for blank in ("", "   ", "\t"):
            with self.subTest(blank=repr(blank)):
                mod = _reload(routes, {"SB3_CHIRP_AIRBAND_HOST": blank})
                self.assertEqual(mod._CHIRP_HOSTS_BY_PORT[7400], "127.0.0.1")

    def test_value_is_stripped(self):
        mod = _reload(routes, {"SB3_CHIRP_AIRBAND_HOST": "  10.1.2.3  "})
        self.assertEqual(mod._CHIRP_HOSTS_BY_PORT[7400], "10.1.2.3")


class NoLiveAddressSurvivesInTheCommandPaths(unittest.TestCase):
    """A reintroduced constant must fail here even if the tables look right."""

    def test_no_routable_ipv4_literal_in_the_host_tables(self):
        import re

        quad = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
        for path, anchor in (
            ("sb3/ui/routes.py", "_CHIRP_HOSTS_BY_PORT = {"),
            ("sb3/chirp_applier.py", "_BAND_CFG = {"),
        ):
            with self.subTest(path=path):
                src = open(
                    os.path.join(os.path.dirname(__file__), "..", path),
                    encoding="utf-8",
                ).read()
                start = src.index(anchor)
                block = src[start : src.index("\n}\n", start)]
                bad = [h for h in quad.findall(block) if _is_routable(h)]
                self.assertEqual(
                    bad, [], f"{path}: hardcoded routable address(es) {bad} in {anchor}"
                )


if __name__ == "__main__":
    unittest.main()
