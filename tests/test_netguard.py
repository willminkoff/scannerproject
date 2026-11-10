"""The guard that stops a test reaching a real radio.

Every address in this file is a reserved documentation address -- 192.0.2.0/24
and 203.0.113.0/24 (RFC 5737), 2001:db8::/32 (RFC 3849), .invalid (RFC 2606).
None of them is a host. That is deliberate and it is the lesson from
2026-11-10: a test that proves a block works must not depend on the block
working in order to be safe. If a bug disarmed netguard tomorrow, the worst
this file can do is fail to connect to nothing.

The live rack's addresses appear nowhere here, on the same grounds.

Armed -- the way the suite actually runs -- this file takes 0.16 s, because a
refusal happens before the syscall. Disarmed it takes ~48 s and 19 of these
tests fail, which is correct: they are asserting that the guard refuses, and it
is not there to. Nearly all of that time is one test calling the real
``_venus_power``, whose own ``timeout=45`` this file has no business editing.
"""

from __future__ import annotations

import contextlib
import os
import socket
import threading
import unittest
from unittest import mock

import pytest

import netguard

# Reserved for documentation. Not hosts.
OFF_BOX_V4 = "203.0.113.1"
OFF_BOX_V4_ALT = "192.0.2.7"
OFF_BOX_V6 = "2001:db8::1"
OFF_BOX_NAME = "scanner-rack.invalid"

A_PORT = 7400  # chirp's airband command port, for realism only

# Every blocking test caps its own timeout. With the guard armed nothing waits
# at all -- the refusal happens before the syscall. The cap is for a DISARMED
# run (SB3_TESTS_ALLOW_NETWORK=1): a connect with no timeout blocks for ~75 s
# on macOS, and these tests took the suite from 44 s to 6m45s before it was set.
TIMEOUT = 0.25


@contextlib.contextmanager
def expect_refusal():
    """Trigger a refusal, assert it was raised AND recorded, then clear it.

    The autouse fixture in the root conftest fails any test whose ledger grew,
    which is right for every test except these: here a recorded violation is
    the pass condition. Clearing the entries keeps this file from failing
    itself.
    """
    before = netguard.violation_count()
    with pytest.raises(netguard.NetworkUseInTest):
        yield
    assert netguard.violation_count() > before, "refusal was not recorded on the ledger"
    del netguard._violations[before:]


def udp():
    return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


class TheGuardIsActuallyArmed(unittest.TestCase):
    def test_conftest_armed_it(self):
        # If this fails, nothing else in this file is meaningful -- and neither
        # is the protection the rest of the suite is relying on.
        self.assertTrue(
            netguard.is_installed(),
            "netguard is not armed; the root conftest did not install it",
        )

    def test_the_wrappers_are_the_ones_in_place(self):
        self.assertEqual(socket.socket.sendto.__name__, "sendto")
        self.assertIn("netguard", getattr(socket.socket.connect, "__doc__", "") or "")


class ItBlocksOffBoxAddresses(unittest.TestCase):
    def test_udp_sendto_is_blocked(self):
        # The airband wipe's exact path: a UDP datagram at the chirp command
        # bus. There is no connect() to intercept -- sendto IS the whole call.
        with udp() as s, expect_refusal():
            s.sendto(b'{"v":1,"cmd":"remove_channel"}', (OFF_BOX_V4, A_PORT))

    def test_udp_sendto_with_flags_is_blocked(self):
        # sendto(data, flags, address) -- the address moves to the third arg.
        with udp() as s, expect_refusal():
            s.sendto(b"x", 0, (OFF_BOX_V4, A_PORT))

    def test_tcp_connect_is_blocked(self):
        with socket.socket() as s, expect_refusal():
            s.settimeout(TIMEOUT)
            s.connect((OFF_BOX_V4, 9200))

    def test_connect_ex_is_blocked(self):
        # connect_ex returns an errno instead of raising, so a caller that
        # ignores the result would never notice an Exception-based guard.
        with socket.socket() as s, expect_refusal():
            s.settimeout(TIMEOUT)
            s.connect_ex((OFF_BOX_V4, 9200))

    def test_urlopen_is_blocked(self):
        # The /power/off path. urllib -> http.client -> socket.create_connection
        # -> sock.connect, so wrapping connect is enough to stop all of it.
        import urllib.request

        with expect_refusal():
            urllib.request.urlopen(f"http://{OFF_BOX_V4}:9200/power/off", timeout=TIMEOUT)

    def test_create_connection_is_blocked(self):
        with expect_refusal():
            socket.create_connection((OFF_BOX_V4_ALT, 9200), timeout=TIMEOUT)

    def test_a_hostname_is_blocked(self):
        # Resolving the name to decide would itself be a network call, so any
        # name that is not a known loopback alias is refused outright.
        with socket.socket() as s, expect_refusal():
            s.settimeout(TIMEOUT)
            s.connect((OFF_BOX_NAME, 5050))

    def test_ipv6_is_blocked(self):
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s, expect_refusal():
            s.settimeout(TIMEOUT)
            s.connect((OFF_BOX_V6, 9200, 0, 0))

    def test_the_refusal_names_the_offending_line(self):
        before = netguard.violation_count()
        with pytest.raises(netguard.NetworkUseInTest) as caught:
            with udp() as s:
                s.sendto(b"x", (OFF_BOX_V4, A_PORT))
        del netguard._violations[before:]
        text = str(caught.value)
        self.assertIn("tests/test_netguard.py", text)
        self.assertIn(OFF_BOX_V4, text)
        # It has to say what to do next, or it just looks like a flaky test.
        self.assertIn("mock.patch", text)
        self.assertIn(netguard.MARKER, text)


class ExceptionHandlersCannotHideIt(unittest.TestCase):
    """Why the refusal is a BaseException and not an Exception.

    sb3/ui/routes.py:_venus_power ends in ``except Exception as exc: return
    {"ok": False, "error": ...}``. An Exception-based guard would be caught
    there: the POST would be blocked, the function would return its tidy
    failure dict, and a test asserting ``ok is False`` would PASS -- green,
    with the guard completely invisible and the real refusal untested.
    """

    def test_not_an_exception_subclass(self):
        self.assertTrue(issubclass(netguard.NetworkUseInTest, BaseException))
        self.assertFalse(issubclass(netguard.NetworkUseInTest, Exception))

    def test_bare_except_exception_does_not_swallow_it(self):
        before = netguard.violation_count()
        swallowed = False
        with self.assertRaises(netguard.NetworkUseInTest):
            try:
                with udp() as s:
                    s.sendto(b"x", (OFF_BOX_V4, A_PORT))
            except Exception:  # noqa: BLE001 -- the point of the test
                swallowed = True
        del netguard._violations[before:]
        self.assertFalse(swallowed, "except Exception caught the refusal")

    def test_an_oserror_handler_does_not_swallow_it(self):
        # Network code reasonably catches OSError/socket.error.
        before = netguard.violation_count()
        with self.assertRaises(netguard.NetworkUseInTest):
            try:
                with socket.socket() as s:
                    s.settimeout(TIMEOUT)
                    s.connect((OFF_BOX_V4, 9200))
            except OSError:
                self.fail("OSError handler caught the refusal")
        del netguard._violations[before:]

    def test_a_venus_power_shaped_caller_cannot_report_a_tidy_failure(self):
        import urllib.request

        def like_venus_power(url):
            try:
                with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
                    return {"ok": True, "body": resp.read()}
            except Exception as exc:  # noqa: BLE001 -- copied from the real code
                return {"ok": False, "error": repr(exc)}

        before = netguard.violation_count()
        with self.assertRaises(netguard.NetworkUseInTest):
            like_venus_power(f"http://{OFF_BOX_V4}:9200/power/off")
        del netguard._violations[before:]


class TheLedgerBacksUpTheException(unittest.TestCase):
    """A blocked call on a background thread never fails the test by raising.

    The exception dies with the thread. The ledger is what turns it into a
    failure, via the autouse fixture in the root conftest.
    """

    def test_a_thread_still_lands_on_the_ledger(self):
        before = netguard.violation_count()

        def worker():
            try:
                with udp() as s:
                    s.sendto(b"x", (OFF_BOX_V4, A_PORT))
            except BaseException:  # noqa: BLE001 -- a real thread would just die
                pass

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=5)
        after = netguard.violations()
        self.assertEqual(len(after), before + 1, "the thread's attempt was not recorded")
        self.assertEqual(after[-1]["op"], "sendto")
        del netguard._violations[before:]

    def test_a_recorded_violation_fails_the_test_it_came_from(self):
        # Pin the backstop itself: the fixture formats whatever is on the
        # ledger into a failure message. If this ever stops mentioning the
        # address, a swallowed violation would fail with an empty reason.
        fake = {
            "op": "sendto",
            "family": int(socket.AF_INET),
            "address": (OFF_BOX_V4, A_PORT),
            "frames": ["sb3/ui/routes.py:1 in _chirp_send"],
        }
        text = netguard.format_violations([fake])
        self.assertIn(OFF_BOX_V4, text)
        self.assertIn("sb3/ui/routes.py:1", text)


class ItAllowsLocalTraffic(unittest.TestCase):
    """The existing suite opens real loopback sockets everywhere -- chirp's
    phase1-4 tests, the tuner broker, the push guards. A guard that broke those
    would be turned off within a day."""

    def test_loopback_udp_roundtrip(self):
        with udp() as server:
            server.bind(("127.0.0.1", 0))
            port = server.getsockname()[1]
            with udp() as client:
                client.sendto(b"ping", ("127.0.0.1", port))
            data, _ = server.recvfrom(64)
        self.assertEqual(data, b"ping")

    def test_loopback_tcp_connect(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            with socket.socket() as client:
                client.connect(("127.0.0.1", port))

    def test_localhost_by_name_is_allowed(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            with socket.socket() as client:
                client.connect(("localhost", port))

    def test_binding_to_all_interfaces_is_not_blocked(self):
        # Listening harms nothing, and the chirp daemons' own tests bind.
        with udp() as s:
            s.bind(("0.0.0.0", 0))

    def test_unix_sockets_are_not_blocked(self):
        # tests/test_tuner_broker_server.py connects to an AF_UNIX path.
        self.assertTrue(netguard.is_local(socket.AF_UNIX, "/tmp/sb3-broker.sock"))

    def test_other_families_are_not_blocked(self):
        # The project does Bluetooth audio; non-IP families do not route.
        for family in (socket.AF_UNIX, getattr(socket, "AF_NETLINK", socket.AF_UNIX)):
            with self.subTest(family=int(family)):
                self.assertTrue(netguard.is_local(family, ("anything", 1)))


class AddressClassification(unittest.TestCase):
    def test_local_addresses(self):
        for host in (
            "127.0.0.1",
            "127.1.2.3",
            "0.0.0.0",
            "::1",
            "::",
            "::ffff:127.0.0.1",
            "localhost",
            "LOCALHOST",
            "  127.0.0.1  ",
            "",
        ):
            with self.subTest(host=host):
                self.assertTrue(netguard.is_local(socket.AF_INET, (host, 1)))

    def test_off_box_addresses(self):
        for host in (
            OFF_BOX_V4,
            OFF_BOX_V4_ALT,
            OFF_BOX_V6,
            OFF_BOX_NAME,
            "10.0.0.1",
            "192.168.1.1",
            "100.64.0.1",  # the tailnet range the rack lives on
            "::ffff:203.0.113.1",
        ):
            with self.subTest(host=host):
                self.assertFalse(netguard.is_local(socket.AF_INET, (host, 1)))

    def test_private_and_tailnet_addresses_are_off_box(self):
        # "It's only the LAN" is how the rack got commanded in the first place.
        self.assertFalse(netguard.is_local(socket.AF_INET, ("192.168.0.50", 7400)))

    def test_unrecognised_shapes_fail_closed(self):
        for address in (None, 12345, object(), (b"\x00\x01", 1)):
            with self.subTest(address=repr(address)[:40]):
                self.assertFalse(netguard.is_local(socket.AF_INET, address))

    def test_an_ipv6_scope_suffix_is_stripped(self):
        self.assertTrue(netguard.is_local(socket.AF_INET6, ("::1%lo0", 1, 0, 0)))


class TheEscapeHatches(unittest.TestCase):
    def test_the_allowed_block_lifts_the_guard(self):
        sock = udp()
        self.addCleanup(sock.close)
        before = netguard.violation_count()
        with netguard.allowed():
            # No packet is sent -- the check is what is being exercised.
            netguard._check(sock, "sendto", (OFF_BOX_V4, A_PORT))
        self.assertEqual(netguard.violation_count(), before)

    def test_the_guard_comes_back_after_the_block(self):
        sock = udp()
        self.addCleanup(sock.close)
        with netguard.allowed():
            netguard._check(sock, "sendto", (OFF_BOX_V4, A_PORT))
        before = netguard.violation_count()
        with pytest.raises(netguard.NetworkUseInTest):
            netguard._check(sock, "sendto", (OFF_BOX_V4, A_PORT))
        del netguard._violations[before:]

    def test_nested_allowed_blocks_restore_correctly(self):
        sock = udp()
        self.addCleanup(sock.close)
        with netguard.allowed():
            with netguard.allowed():
                netguard._check(sock, "sendto", (OFF_BOX_V4, A_PORT))
            netguard._check(sock, "sendto", (OFF_BOX_V4, A_PORT))
        before = netguard.violation_count()
        with pytest.raises(netguard.NetworkUseInTest):
            netguard._check(sock, "sendto", (OFF_BOX_V4, A_PORT))
        del netguard._violations[before:]

    def test_the_env_var_disarms_the_guard(self):
        for value in ("1", "true", "yes", "on"):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {netguard.ENV_OPT_OUT: value}):
                    self.assertTrue(netguard.opted_out())

    def test_falsey_env_values_leave_it_armed(self):
        for value in ("", "0", "false", "no", "off", "   "):
            with self.subTest(value=repr(value)):
                with mock.patch.dict(os.environ, {netguard.ENV_OPT_OUT: value}):
                    self.assertFalse(netguard.opted_out())

    def test_install_is_a_no_op_when_opted_out(self):
        # Pin that the opt-out is checked at install time, so a disarmed run
        # does not leave half-patched sockets behind.
        netguard.uninstall()
        self.addCleanup(netguard.install)
        with mock.patch.dict(os.environ, {netguard.ENV_OPT_OUT: "1"}):
            self.assertFalse(netguard.install())
        self.assertFalse(netguard.is_installed())


class ItCoversTheRealProductionPaths(unittest.TestCase):
    """Synthetic sockets prove the wrappers work. This proves they sit in front
    of the code that actually caused the outages.

    Each test points the production module at a documentation address via the
    environment variable the deployment really uses, then calls the real
    function. The rack's own addresses are never involved -- the only thing
    swapped in is a destination that is guaranteed not to be a host.
    """

    @classmethod
    def tearDownClass(cls):
        import importlib

        import sb3.ui.routes as routes

        importlib.reload(routes)

    @staticmethod
    def _routes_with(env):
        import importlib

        import sb3.ui.routes as routes

        keep = {
            k: v
            for k, v in os.environ.items()
            if k not in ("SB3_CHIRP_AIRBAND_HOST", "SB3_VENUS_POWER_URL")
        }
        keep.update(env)
        with mock.patch.dict(os.environ, keep, clear=True):
            return importlib.reload(routes)

    def test_chirp_send_is_blocked(self):
        # _chirp_send is what carried remove_channel x10 to the live airband
        # pool. It has no exception handling of its own, so the refusal
        # propagates straight out.
        mod = self._routes_with({"SB3_CHIRP_AIRBAND_HOST": OFF_BOX_V4})
        self.assertEqual(mod._chirp_host(A_PORT), OFF_BOX_V4)
        with expect_refusal():
            mod._chirp_send(A_PORT, "remove_channel", {"id": "n4-anything"}, timeout=0.1)

    def test_chirp_probe_cannot_report_a_tidy_false(self):
        # _chirp_probe ends in `except Exception: return False`. An
        # Exception-based guard would be caught there and the probe would just
        # report "radio not reachable" -- blocked, silent, and invisible to
        # whoever ran the suite.
        mod = self._routes_with({"SB3_CHIRP_AIRBAND_HOST": OFF_BOX_V4})
        with expect_refusal():
            mod._chirp_probe(A_PORT)

    def test_venus_power_cannot_report_a_tidy_failure(self):
        # The /power/off path, through the real function and its real
        # `except Exception` handler.
        mod = self._routes_with({"SB3_VENUS_POWER_URL": f"http://{OFF_BOX_V4}:9200"})
        self.assertEqual(mod._VENUS_POWER_URL, f"http://{OFF_BOX_V4}:9200")
        with expect_refusal():
            mod._venus_power("off")

    def test_the_applier_band_table_is_covered_too(self):
        import importlib

        import sb3.chirp_applier as chirp_applier

        keep = {k: v for k, v in os.environ.items() if k != "SB3_CHIRP_AIRBAND_HOST"}
        keep["SB3_CHIRP_AIRBAND_HOST"] = OFF_BOX_V4
        with mock.patch.dict(os.environ, keep, clear=True):
            mod = importlib.reload(chirp_applier)
        self.addCleanup(importlib.reload, chirp_applier)
        self.assertEqual(mod._BAND_CFG["airband"]["host"], OFF_BOX_V4)
        with udp() as s, expect_refusal():
            s.sendto(b"x", (mod._BAND_CFG["airband"]["host"], mod._BAND_CFG["airband"]["port"]))


@pytest.mark.allow_network
class TheMarkerLiftsTheGuard(unittest.TestCase):
    """Marked so the autouse fixture wraps it in netguard.allowed().

    No packet is sent: the point is that the CHECK stops refusing, which is
    what a test with a genuine need for the network would rely on.
    """

    def test_the_marked_test_is_not_refused(self):
        sock = udp()
        self.addCleanup(sock.close)
        before = netguard.violation_count()
        netguard._check(sock, "sendto", (OFF_BOX_V4, A_PORT))
        self.assertEqual(netguard.violation_count(), before)


if __name__ == "__main__":
    unittest.main()
