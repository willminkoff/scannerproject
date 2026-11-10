"""The four addresses that had an env override and still defaulted to the rack.

3106e18 and 19a2c58 each moved one hardcoded address into the environment, and
the commit message for the second claimed it was the last one. It was not. Four
more variables HAD an override and DEFAULTED to a live host, which is not the
same as defaulting safely:

    sb3/ui/routes.py      SB3_CHIRP_AIRBAND_LOG_URL -> the rack's :9200
    sb3/ui/routes.py      SB3_OP25_APPLY_URL        -> the rack's :9200
    sb3/op25_applier.py   SB3_OP25_APPLY_URL        -> the rack's :9200
    sb3/ui/routes.py      SB3_DISCO_URL             -> the rack's :8092
    disco/src/listen.py   DISCO_ICECAST_HOST        -> micro, a RETIRED box

tests/netguard.py found the first of them by refusing the socket:
tests/test_sb3_ui_write.py's hits test had been fetching the real airband log
off the rack on every run, and only ever passed because the rack was up.

Two shapes, chosen by whether arrival of the request CHANGES anything:

  reads  (airband log, disco card, icecast host) default to "" and the caller
         skips the feature. Loopback would be wrong even here -- the airband
         log's body is parsed into the operator's hit list, so a stranger on
         :9200 could inject phantom traffic.
  writes (op25 apply) REFUSE. /apply-profile rewrites talkgroups, repoints
         ACTIVE_LINK and restarts three units; loopback is specifically unsafe
         because op25-log-server and sdrtrunk-log-server both default to 9200.

Every call site that could open a socket is mocked, and the mock is asserted
never to have been called. That is not belt-and-braces: the first draft of
tests/test_power_url_not_hardcoded.py left one unmocked on the reasoning that
the refusal made it unreachable, and took the rack down for three minutes.
"""

from __future__ import annotations

import importlib
import importlib.util
import ipaddress
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

from sb3 import op25_applier
from sb3.state import State
from sb3.ui import routes

_REPO = Path(__file__).resolve().parent.parent
_ADDR_VARS = (
    "SB3_CHIRP_AIRBAND_LOG_URL",
    "SB3_OP25_APPLY_URL",
    "SB3_DISCO_URL",
    "DISCO_ICECAST_HOST",
    "DISCO_ICECAST_PORT",
    "DISCO_STREAM_MOUNT",
)

BLANKS = ("", "   ", "\t", "\n")


def _cleared(**overrides):
    """os.environ with every address var removed, then overrides applied."""
    keep = {k: v for k, v in os.environ.items() if k not in _ADDR_VARS}
    keep.update(overrides)
    return mock.patch.dict(os.environ, keep, clear=True)


def _state():
    # Same shape as tests/test_sb3_ui_write.py's helper, inlined so this file
    # does not depend on another test module's import side effects.
    rec = {"name": "air.airband.nashville", "role": "air",
           "deviceset_index": 0, "center_freq_hz": 118925000}
    st = State(Path("/nonexistent"))
    st.read_loaded_profile = lambda r=None: (rec if r in (None, "air") else None)
    st.read_loaded_profiles = lambda: {"air": rec}
    return st


def _is_routable(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return bool(host.strip())  # a hostname routes just as well
    return not (addr.is_loopback or addr.is_unspecified)


# ---------------------------------------------------------------------------
# SB3_OP25_APPLY_URL -- a write, so it refuses
# ---------------------------------------------------------------------------


class Op25ApplyTargetResolution(unittest.TestCase):
    def test_unset_resolves_to_refusal(self):
        with _cleared():
            self.assertEqual(op25_applier.resolve_apply_target(), "")

    def test_blank_resolves_to_refusal(self):
        for blank in BLANKS:
            with self.subTest(blank=repr(blank)):
                with _cleared(SB3_OP25_APPLY_URL=blank):
                    self.assertEqual(op25_applier.resolve_apply_target(), "")

    def test_a_schemeless_value_resolves_to_refusal(self):
        # The whole reason the guard checks for a scheme rather than just
        # truthiness. ".rstrip('/')" does not strip whitespace, so "   " is
        # truthy; Request("   /apply-profile") then raises ValueError for a
        # missing scheme, from outside the try block at both call sites.
        for bad in ("100.114.219.115:9200", "   /apply-profile", "ftp://host",
                    "//host:9200", "host:9200"):
            with self.subTest(bad=bad):
                with _cleared(SB3_OP25_APPLY_URL=bad):
                    self.assertEqual(op25_applier.resolve_apply_target(), "")

    def test_a_configured_url_is_used(self):
        with _cleared(SB3_OP25_APPLY_URL="http://10.1.2.3:9200"):
            self.assertEqual(op25_applier.resolve_apply_target(),
                             "http://10.1.2.3:9200")

    def test_padding_and_trailing_slash_are_normalised(self):
        with _cleared(SB3_OP25_APPLY_URL="  http://10.1.2.3:9200/  "):
            self.assertEqual(op25_applier.resolve_apply_target(),
                             "http://10.1.2.3:9200")

    def test_a_profile_may_override_the_environment(self):
        with _cleared(SB3_OP25_APPLY_URL="http://10.1.2.3:9200"):
            self.assertEqual(
                op25_applier.resolve_apply_target({"target_host_url": "http://10.9.9.9:9200"}),
                "http://10.9.9.9:9200",
            )

    def test_an_empty_profile_override_falls_back_to_the_environment(self):
        # The `or` in the old line 83 collapsed an unset variable AND a profile
        # carrying "" or null into the same empty default, then built the
        # request anyway.
        for empty in ("", "   ", None):
            with self.subTest(empty=repr(empty)):
                with _cleared(SB3_OP25_APPLY_URL="http://10.1.2.3:9200"):
                    self.assertEqual(
                        op25_applier.resolve_apply_target({"target_host_url": empty}),
                        "http://10.1.2.3:9200",
                    )

    def test_an_empty_profile_override_with_no_environment_refuses(self):
        with _cleared():
            self.assertEqual(
                op25_applier.resolve_apply_target({"target_host_url": ""}), "")

    def test_the_frozen_module_constant_is_gone(self):
        # It was module level, so it latched at import: a test or caller could
        # not change it without importlib.reload.
        self.assertFalse(hasattr(op25_applier, "DEFAULT_TARGET_URL"))


class Op25ApplyRefusesBeforeOpeningASocket(unittest.TestCase):
    def test_the_applier_refuses_and_sends_nothing(self):
        profile = {"name": "p", "systems": [
            {"name": "NJICS", "control_channels_mhz": [769.1]}]}
        with _cleared():
            with mock.patch("urllib.request.urlopen") as urlopen:
                out = op25_applier.apply_digital_profile_op25(profile)
        urlopen.assert_not_called()
        self.assertIs(out.get("ok"), False)
        self.assertIn("SB3_OP25_APPLY_URL", out.get("error", ""))

    def test_refusal_is_a_normal_return_not_an_exception(self):
        # A ValueError from a malformed URL used to escape this module
        # entirely: the try opens after the Request is built, and its handlers
        # name only HTTPError/URLError/TimeoutError/OSError.
        with _cleared(SB3_OP25_APPLY_URL="   "):
            with mock.patch("urllib.request.urlopen") as urlopen:
                out = op25_applier.apply_digital_profile_op25({"name": "p"})
        urlopen.assert_not_called()
        self.assertIsInstance(out, dict)
        self.assertIs(out.get("ok"), False)


class TheRefusalKeepsTheSiteSelectionEvidence(unittest.TestCase):
    """_hp_push_op25's return value carries diag: which site each system landed
    on, which fell back to stored controls, which was out of range. A refusal
    that dropped it would discard exactly the evidence the function exists to
    surface -- and no existing test covered the refusal branch at all."""

    def _push_unlocated(self):
        digital = {"123": {"name": "NJICS", "ccs": [769.1, 769.2], "tgs": []}}
        with mock.patch.object(routes, "_resolve_site_controls", return_value={}), \
             mock.patch.object(routes, "_hp_sync_center", return_value=None), \
             mock.patch("urllib.request.urlopen") as urlopen:
            out = routes._hp_push_op25(digital, _state())
        return out, urlopen

    def test_it_refuses_without_sending(self):
        with _cleared():
            out, urlopen = self._push_unlocated()
        urlopen.assert_not_called()
        self.assertIs(out.get("ok"), False)
        self.assertIn("SB3_OP25_APPLY_URL", out.get("error", ""))

    def test_the_refusal_still_carries_diag(self):
        with _cleared():
            out, _ = self._push_unlocated()
        self.assertIn("unlocated_used_stored_controls", out,
                      f"diag was dropped by the refusal branch: {sorted(out)}")
        self.assertTrue(out["unlocated_used_stored_controls"])

    def test_json_serialisable(self):
        with _cleared():
            out, _ = self._push_unlocated()
        json.dumps(out)


# ---------------------------------------------------------------------------
# SB3_CHIRP_AIRBAND_LOG_URL -- a read, so it skips
# ---------------------------------------------------------------------------


class AirbandLogTailSkipsWhenUnset(unittest.TestCase):
    def test_no_socket_is_opened_for_the_airband_tail(self):
        with _cleared():
            with mock.patch("urllib.request.urlopen") as urlopen:
                out = routes.hits(_state())
        urlopen.assert_not_called()
        self.assertIs(out.get("ok"), True)

    def test_blank_is_treated_as_unset(self):
        for blank in BLANKS:
            with self.subTest(blank=repr(blank)):
                with _cleared(SB3_CHIRP_AIRBAND_LOG_URL=blank):
                    with mock.patch("urllib.request.urlopen") as urlopen:
                        routes.hits(_state())
                urlopen.assert_not_called()

    def test_a_configured_url_is_fetched(self):
        # The feature must still work -- this is what Neptune's plist provides.
        with _cleared(SB3_CHIRP_AIRBAND_LOG_URL="http://10.1.2.3:9200/x.log"):
            with mock.patch("urllib.request.urlopen") as urlopen:
                urlopen.return_value.__enter__.return_value.read.return_value = b""
                routes.hits(_state())
        self.assertTrue(urlopen.called, "a configured airband log was not fetched")
        self.assertEqual(urlopen.call_args[0][0], "http://10.1.2.3:9200/x.log")


# ---------------------------------------------------------------------------
# SB3_DISCO_URL -- a read, and unset is not the same as broken
# ---------------------------------------------------------------------------


class DiscoCardDistinguishesUnsetFromBroken(unittest.TestCase):
    def _disco_card(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = OSError("no")
            out = routes.build_subsystems()
        return out["subsystems"]["disco"], urlopen

    def test_unset_reports_unknown_not_bad(self):
        # It used to say "bad: unreachable", which reads as "disco is broken"
        # when the truth is "nobody told sb3-ui where disco is". The op25 cards
        # in the same function already model the right answer.
        with _cleared():
            card, _ = self._disco_card()
        self.assertEqual(card.get("state"), "unknown", card)
        self.assertIn("SB3_DISCO_URL", json.dumps(card))

    def test_unset_opens_no_socket_for_disco(self):
        # build_subsystems probes several things; only the disco one is in
        # scope here, so assert on the destination rather than call count.
        with _cleared():
            with mock.patch("urllib.request.urlopen") as urlopen:
                urlopen.side_effect = OSError("no")
                routes.build_subsystems()
        for call in urlopen.call_args_list:
            self.assertNotIn("8092", str(call))

    def test_configured_but_down_still_reports_bad(self):
        with _cleared(SB3_DISCO_URL="http://10.1.2.3:8092"):
            card, _ = self._disco_card()
        self.assertEqual(card.get("state"), "bad", card)


# ---------------------------------------------------------------------------
# DISCO_ICECAST_HOST -- the default was a box that no longer exists
# ---------------------------------------------------------------------------


def _load_listen(env):
    """Import disco/src/listen.py by path under *env*.

    By path because disco/ and disco/src/ have no __init__.py, and on its own
    so this file never drags dashboard.py (or anything sb3) into the import.
    """
    path = _REPO / "disco" / "src" / "listen.py"
    name = "_listen_under_test"
    with _cleared(**env):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        # Registered BEFORE exec_module: @dataclass resolves its annotations
        # through sys.modules[cls.__module__], and raises AttributeError on
        # None if the module is not there yet.
        sys.modules[name] = mod
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.modules.pop(name, None)
    return mod


class DiscoIcecastHostHasNoLiveDefault(unittest.TestCase):
    def test_unset_yields_no_host(self):
        self.assertEqual(_load_listen({}).ICECAST_HOST_PUBLIC, "")

    def test_the_default_is_not_routable(self):
        # It was 100.67.20.40 -- "micro", retired. The override existed and the
        # default pointed at a host that no longer answers.
        self.assertFalse(_is_routable(_load_listen({}).ICECAST_HOST_PUBLIC))

    def test_stream_url_is_empty_without_a_host(self):
        mod = _load_listen({"DISCO_STREAM_MOUNT": "disco.mp3"})
        self.assertEqual(mod._stream_url(), "",
                         "a mount alone used to produce a URL at the retired box")

    def test_stream_url_is_empty_without_a_mount(self):
        mod = _load_listen({"DISCO_ICECAST_HOST": "10.1.2.3"})
        self.assertEqual(mod._stream_url(), "")

    def test_stream_url_works_when_both_are_set(self):
        mod = _load_listen({"DISCO_ICECAST_HOST": "10.1.2.3",
                            "DISCO_STREAM_MOUNT": "disco.mp3"})
        self.assertEqual(mod._stream_url(), "http://10.1.2.3:8000/disco.mp3")

    def test_whitespace_values_are_stripped_not_interpolated(self):
        mod = _load_listen({"DISCO_ICECAST_HOST": "  ", "DISCO_STREAM_MOUNT": "  "})
        self.assertEqual(mod.ICECAST_HOST_PUBLIC, "")
        self.assertEqual(mod._stream_url(), "",
                         "whitespace used to interpolate into 'http://   :8000/'")

    def test_a_malformed_port_does_not_kill_the_import(self):
        # A bare int() here raised at import, and dashboard.py catches that at
        # its own import and sets _LISTEN_AVAILABLE = False -- the headphones
        # button silently disappears rather than anything reporting why.
        for bad in ("", "  ", "abc", "80a0"):
            with self.subTest(bad=bad):
                mod = _load_listen({"DISCO_ICECAST_PORT": bad})
                self.assertEqual(mod.ICECAST_PORT_PUBLIC, 8000)

    def test_a_valid_port_is_honoured(self):
        self.assertEqual(_load_listen({"DISCO_ICECAST_PORT": " 8010 "}).ICECAST_PORT_PUBLIC,
                         8010)


# ---------------------------------------------------------------------------
# a reintroduced literal must fail here even if behaviour looks right
# ---------------------------------------------------------------------------


class NoLiveAddressSurvivesInTheseSites(unittest.TestCase):
    QUAD = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

    def _scan(self, rel, start_anchor, end_anchor):
        src = (_REPO / rel).read_text(encoding="utf-8")
        start = src.index(start_anchor)
        block = src[start:src.index(end_anchor, start)]
        bad = []
        for host in self.QUAD.findall(block):
            try:
                addr = ipaddress.ip_address(host)
            except ValueError:
                continue
            if not (addr.is_loopback or addr.is_unspecified):
                bad.append(host)
        return bad

    def test_no_routable_literal_near_the_airband_log_tail(self):
        self.assertEqual(
            self._scan("sb3/ui/routes.py", "_airband_log = ", '_tail_jsonl(_Path.home()'),
            [])

    def test_no_routable_literal_near_the_op25_apply_post(self):
        self.assertEqual(
            self._scan("sb3/ui/routes.py", "target = resolve_apply_target()",
                       "def _hp_auto_sync"),
            [])

    def test_no_routable_literal_near_the_disco_card(self):
        self.assertEqual(
            self._scan("sb3/ui/routes.py", 'disco_url = ', 'out["adsb"]'), [])

    def test_no_routable_literal_in_the_applier_resolver(self):
        self.assertEqual(
            self._scan("sb3/op25_applier.py", "def resolve_apply_target",
                       "def _profile_to_blob"),
            [])

    def test_no_routable_literal_near_the_icecast_host(self):
        self.assertEqual(
            self._scan("disco/src/listen.py", "ICECAST_HOST_PUBLIC = ",
                       "PROFILES_DIR = "),
            [])


if __name__ == "__main__":
    unittest.main()
