"""Dongle power must not be reachable by accident.

``_venus_power()`` POSTs ``/power/on`` and ``/power/off`` to the box that owns
the SDR dongles. The far end does not confirm and does not dry-run: the request
IS the action. Until this was fixed the destination was a constant --

    _VENUS_POWER_URL = "http://100.114.219.115:9200"

-- so merely importing ``sb3.ui.routes`` was enough to be able to cut power to
the radios. The companion defect in the same module (``_CHIRP_HOSTS_BY_PORT``,
see tests/test_chirp_host_not_hardcoded.py) proved that "only sb3-ui imports
this" is not a safe assumption: a pytest run wiped the live airband pool three
times before anyone worked out where the commands were coming from.

The chirp fix defaults to loopback, which is safe there because nothing answers
on a dev box. That reasoning does NOT carry over here -- some unrelated service
listening on 9200 would quietly receive a power command -- so this one refuses
instead. These tests pin the refusal, and in particular pin that refusing means
sending nothing at all rather than sending somewhere harmless.
"""

from __future__ import annotations

import importlib
import ipaddress
import os
import re
import unittest
from unittest import mock

import sb3.ui.routes as routes

_VAR = "SB3_VENUS_POWER_URL"


def _reload(env):
    patched = {k: v for k, v in os.environ.items() if k != _VAR}
    patched.update(env)
    with mock.patch.dict(os.environ, patched, clear=True):
        return importlib.reload(routes)


class PowerUrlHasNoDefault(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        importlib.reload(routes)

    def test_unset_environment_yields_no_destination(self):
        mod = _reload({})
        self.assertEqual(mod._VENUS_POWER_URL, "")

    def test_blank_environment_yields_no_destination(self):
        for blank in ("", "   ", "\t"):
            with self.subTest(blank=repr(blank)):
                self.assertEqual(_reload({_VAR: blank})._VENUS_POWER_URL, "")

    def test_refusing_means_sending_nothing(self):
        # The point of the whole change: not "post somewhere harmless" but
        # "do not open a socket at all".
        mod = _reload({})
        for action in ("on", "off"):
            with self.subTest(action=action):
                with mock.patch("urllib.request.urlopen") as urlopen:
                    out = mod._venus_power(action)
                urlopen.assert_not_called()
                self.assertFalse(out.get("ok"))
                self.assertIn(_VAR, out.get("error", ""))

    def test_refusal_is_a_normal_return_not_an_exception(self):
        # Callers treat this like any other backend failure; raising here would
        # surface as a 500 from the /api/dongles/power route instead of a
        # readable message.
        #
        # urlopen is mocked even though a correct _venus_power never reaches
        # it. An earlier version of this test left it real, reasoning that the
        # refusal made it unreachable -- which makes the test safe only if the
        # behaviour it is checking already works. Run against the unfixed
        # module on 2026-11-10 it did exactly what this file exists to prevent:
        # POSTed /power/off to the live rack and took the airband radio, op25
        # and two icecast mounts down for three minutes. A test for a safety
        # property must not rely on that property to be safe.
        mod = _reload({})
        with mock.patch("urllib.request.urlopen") as urlopen:
            out = mod._venus_power("off")
        urlopen.assert_not_called()
        self.assertIsInstance(out, dict)
        self.assertIs(out.get("ok"), False)


class PowerUrlHonoursTheEnvironment(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        importlib.reload(routes)

    def _posted_url(self, mod, action="off"):
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b"{}"
            mod._venus_power(action)
        self.assertTrue(urlopen.called, "expected a request to be sent")
        return urlopen.call_args[0][0].full_url

    def test_configured_host_is_used(self):
        mod = _reload({_VAR: "http://10.1.2.3:9200"})
        self.assertEqual(self._posted_url(mod), "http://10.1.2.3:9200/power/off")

    def test_trailing_slash_does_not_double_up(self):
        # A plist value pasted with a trailing slash previously produced
        # ".../power//off", which the far end 404s.
        mod = _reload({_VAR: "http://10.1.2.3:9200/"})
        self.assertEqual(self._posted_url(mod), "http://10.1.2.3:9200/power/off")

    def test_value_is_stripped(self):
        mod = _reload({_VAR: "  http://10.1.2.3:9200  "})
        self.assertEqual(self._posted_url(mod), "http://10.1.2.3:9200/power/off")


class NoLiveAddressSurvivesInThePowerSection(unittest.TestCase):
    def test_no_routable_ipv4_literal_near_the_power_url(self):
        src = open(
            os.path.join(os.path.dirname(__file__), "..", "sb3", "ui", "routes.py"),
            encoding="utf-8",
        ).read()
        start = src.index("# Where dongle power is controlled")
        block = src[start : src.index("_NEPTUNE_POWER_OFF_LABELS", start)]
        bad = []
        for host in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", block):
            try:
                addr = ipaddress.ip_address(host)
            except ValueError:
                continue
            if not (addr.is_loopback or addr.is_unspecified):
                bad.append(host)
        self.assertEqual(bad, [], f"hardcoded routable address(es) {bad} in the power section")


if __name__ == "__main__":
    unittest.main()
