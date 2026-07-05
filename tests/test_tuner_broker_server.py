"""BrokerServer over real AF_UNIX sockets: protocol, crash-safety, timing.

The one property everything else hangs on: **the socket IS the lease** —
these tests kill client connections and watch the broker auto-release, no
reaper, no stale third state.  Timing-sensitive invariants (rspduo
open-gap, min-restart-interval, usb_release_grace) run against small real
windows here; the fake-clock equivalents live in test_tuner_broker_leases.
"""

import json
import os
import shutil
import socket
import threading
import time
import unittest

from test_tuner_broker_helpers import (
    RSP_A,
    RSP_B,
    RTL_FLEX_1,
    RTL_FLEX_2,
    RTL_GROUND,
    make_policy,
    short_socket_dir,
    wait_until,
)

from broker.server import BrokerAlreadyRunning, BrokerServer


def _read_line(sock) -> dict:
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("server closed the connection")
        buf += chunk
    return json.loads(buf.decode("utf-8"))


class ServerTestBase(unittest.TestCase):
    policy_kwargs: dict = {}

    def setUp(self):
        self.run_dir = short_socket_dir()
        self.addCleanup(shutil.rmtree, self.run_dir, ignore_errors=True)
        self.policy = make_policy(self.run_dir, **self.policy_kwargs)
        self.sock_path = self.policy.broker.socket
        self.server = BrokerServer(self.policy)
        self.server.start()
        self.addCleanup(self.server.shutdown)
        self._socks = []
        self.addCleanup(self._close_socks)

    def _close_socks(self):
        for s in self._socks:
            try:
                s.close()
            except OSError:
                pass

    def connect(self) -> socket.socket:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10.0)
        s.connect(self.sock_path)
        self._socks.append(s)
        return s

    def rpc(self, sock, **msg) -> dict:
        msg.setdefault("v", 1)
        sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))
        return _read_line(sock)

    def claim(self, sock, **kw):
        kw.setdefault("consumer", "test-consumer")
        kw.setdefault("reason", "wire test")
        return self.rpc(sock, cmd="claim", **kw)

    def oneshot(self, **msg) -> dict:
        """RPC on a throwaway connection, closed immediately — the poll
        helpers below run hundreds of times inside wait_until, and keeping
        those sockets open would exhaust fds long before any timeout."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10.0)
        try:
            s.connect(self.sock_path)
            msg.setdefault("v", 1)
            s.sendall((json.dumps(msg) + "\n").encode("utf-8"))
            return _read_line(s)
        finally:
            s.close()

    def leases(self) -> list:
        return self.oneshot(cmd="status")["status"]["leases"]

    def counters(self) -> dict:
        return self.oneshot(cmd="status")["status"]["counters"]


class WireProtocolTest(ServerTestBase):
    def test_claim_grant_release_roundtrip(self):
        s = self.connect()
        resp = self.claim(s, serial=RTL_GROUND, consumer="gr-demod@ground")
        self.assertTrue(resp["ok"], msg=resp)
        granted = resp["granted"]
        self.assertEqual(granted["serial"], RTL_GROUND)
        self.assertEqual(granted["device_id"], "RTL-1")
        self.assertEqual(granted["role"], "chirp-ground")
        self.assertEqual(len(self.leases()), 1)

        resp = self.rpc(s, cmd="release", lease_id=granted["lease_id"])
        self.assertTrue(resp["ok"], msg=resp)
        self.assertEqual(resp["released"], granted["lease_id"])
        self.assertEqual(self.leases(), [])

    def test_unknown_serial_denied_with_fleet_listing(self):
        resp = self.claim(self.connect(), serial="DEADBEEF")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["denied"]["code"], "unknown-device")
        self.assertIn(RTL_GROUND, resp["denied"]["reason"])

    def test_already_leased_denial_names_holder(self):
        self.claim(self.connect(), serial=RSP_B, consumer="gr-demod@airband",
                   reason="airband ST tuner1")
        resp = self.claim(self.connect(), serial=RSP_B, consumer="disco")
        self.assertEqual(resp["denied"]["code"], "already-leased")
        self.assertIn("gr-demod@airband", resp["denied"]["reason"])
        self.assertIn("airband ST tuner1", resp["denied"]["reason"])

    def test_role_claim_and_hot_spare_fallthrough(self):
        first = self.claim(self.connect(), role="flex", consumer="vdl2")
        second = self.claim(self.connect(), role="flex", consumer="disco")
        self.assertEqual(first["granted"]["serial"], RTL_FLEX_1)
        self.assertEqual(second["granted"]["serial"], RTL_FLEX_2)
        third = self.claim(self.connect(), role="flex", consumer="acars")
        self.assertEqual(third["denied"]["code"], "role-exhausted")

    def test_dual_tuner_refusal_over_the_wire(self):
        resp = self.claim(self.connect(), serial=RSP_B, dual_tuner=True)
        self.assertEqual(resp["denied"]["code"], "dual-tuner-forbidden")

    def test_one_lease_per_connection(self):
        s = self.connect()
        first = self.claim(s, serial=RTL_GROUND)
        self.assertTrue(first["ok"])
        second = self.claim(s, serial=RTL_FLEX_1)
        self.assertEqual(second["error"]["code"], "lease-already-held")
        # The original lease is untouched by the refused second claim.
        self.assertEqual(len(self.leases()), 1)

    def test_release_wrong_lease_id_refused(self):
        s = self.connect()
        granted = self.claim(s, serial=RTL_GROUND)["granted"]
        resp = self.rpc(s, cmd="release", lease_id="bogus")
        self.assertEqual(resp["error"]["code"], "not-lease-owner")
        self.assertEqual(len(self.leases()), 1)
        ok = self.rpc(s, cmd="release", lease_id=granted["lease_id"])
        self.assertTrue(ok["ok"])

    def test_release_without_lease_refused(self):
        resp = self.rpc(self.connect(), cmd="release", lease_id="whatever")
        self.assertEqual(resp["error"]["code"], "no-lease-held")

    def test_status_and_list_shapes(self):
        self.claim(self.connect(), serial=RSP_A, consumer="sdrtrunk", reason="p25")
        s = self.connect()
        status = self.rpc(s, cmd="status")["status"]
        self.assertEqual({d["serial"] for d in status["devices"]},
                         {RSP_A, RSP_B, RTL_GROUND, RTL_FLEX_1, RTL_FLEX_2})
        rsp_a_row = next(d for d in status["devices"] if d["serial"] == RSP_A)
        self.assertTrue(rsp_a_row["leased"])
        self.assertEqual(rsp_a_row["holder"], "sdrtrunk")
        listing = self.rpc(s, cmd="list")["leases"]
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["consumer"], "sdrtrunk")

    def test_bad_json_gets_error_then_disconnect(self):
        s = self.connect()
        s.sendall(b"{this is not json\n")
        resp = _read_line(s)
        self.assertEqual(resp["error"]["code"], "bad-json")
        self.assertEqual(s.recv(4096), b"")  # server hung up on the desynced stream

    def test_unsupported_version_refused(self):
        resp = self.rpc(self.connect(), v=99, cmd="status")
        self.assertEqual(resp["error"]["code"], "unsupported-version")

    def test_unknown_cmd_keeps_connection_usable(self):
        s = self.connect()
        resp = self.rpc(s, cmd="frobnicate")
        self.assertEqual(resp["error"]["code"], "unknown-cmd")
        self.assertTrue(self.rpc(s, cmd="status")["ok"])

    def test_missing_consumer_denied_not_crashed(self):
        resp = self.rpc(self.connect(), cmd="claim", serial=RTL_GROUND, reason="x")
        self.assertEqual(resp["denied"]["code"], "bad-request")


class CrashSafetyTest(ServerTestBase):
    """The keystone property: connection drop == lease release."""

    def test_connection_drop_auto_releases(self):
        s = self.connect()
        self.claim(s, serial=RSP_B, consumer="doomed")
        self.assertEqual(len(self.leases()), 1)
        s.close()  # simulated crash: no release command was ever sent
        self.assertTrue(
            wait_until(lambda: self.leases() == [], timeout=5.0),
            "lease survived its owner's death",
        )
        self.assertEqual(self.counters()["auto_releases"], 1)
        self.assertEqual(self.counters()["releases"], 0)

    def test_serial_reclaimable_after_owner_crash(self):
        s = self.connect()
        self.claim(s, serial=RTL_GROUND, consumer="doomed")
        s.close()
        self.assertTrue(wait_until(lambda: self.leases() == [], timeout=5.0))
        resp = self.claim(self.connect(), serial=RTL_GROUND, consumer="successor")
        self.assertTrue(resp["ok"], msg=resp)

    def test_shutdown_drops_connections_and_socket(self):
        s = self.connect()
        self.claim(s, serial=RTL_GROUND)
        self.server.shutdown()
        self.assertFalse(os.path.exists(self.sock_path))
        # The dropped connection's lease goes through the normal
        # auto-release path (daemon thread, after grace).
        self.assertTrue(
            wait_until(lambda: self.server.ledger.find_lease(RTL_GROUND) is None,
                       timeout=5.0)
        )


class ReleaseGraceTest(ServerTestBase):
    policy_kwargs = {"grace": 0.6}

    def test_auto_release_waits_out_usb_grace(self):
        s = self.connect()
        self.claim(s, serial=RTL_GROUND, consumer="doomed")
        s.close()
        # Immediately after the drop the serial must STILL be held — the
        # grace gives the kernel a beat to finish USB teardown before the
        # next claimant opens the device.
        self.assertEqual(len(self.leases()), 1)
        self.assertTrue(wait_until(lambda: self.leases() == [], timeout=5.0))

    def test_explicit_release_skips_the_grace(self):
        s = self.connect()
        granted = self.claim(s, serial=RTL_GROUND)["granted"]
        self.rpc(s, cmd="release", lease_id=granted["lease_id"])
        # A clean handoff frees the serial immediately, no 0.6s pause.
        self.assertEqual(self.leases(), [])


class OpenGapWireTest(ServerTestBase):
    policy_kwargs = {"gap": 0.4}

    def test_second_rspduo_grant_is_paced(self):
        t_first = time.monotonic()
        first = self.claim(self.connect(), serial=RSP_A, consumer="sdrtrunk")
        self.assertTrue(first["ok"], msg=first)
        second = self.claim(
            self.connect(), serial=RSP_B, consumer="chirp", wait_timeout_sec=5.0
        )
        elapsed = time.monotonic() - t_first
        self.assertTrue(second["ok"], msg=second)
        self.assertGreaterEqual(
            elapsed, 0.35,
            f"second rspduo grant came only {elapsed:.3f}s after the first "
            f"(policy gap 0.4s) — apiService open-gap not enforced",
        )

    def test_gap_does_not_pace_rtl_claims(self):
        self.claim(self.connect(), serial=RSP_A, consumer="sdrtrunk")
        t0 = time.monotonic()
        resp = self.claim(self.connect(), serial=RTL_GROUND, consumer="chirp")
        self.assertTrue(resp["ok"])
        self.assertLess(time.monotonic() - t0, 0.3)

    def test_impatient_claim_denied_open_gap_timeout(self):
        self.claim(self.connect(), serial=RSP_A, consumer="sdrtrunk")
        t0 = time.monotonic()
        resp = self.claim(
            self.connect(), serial=RSP_B, consumer="chirp", wait_timeout_sec=0.05
        )
        self.assertLess(time.monotonic() - t0, 1.0)  # denied NOW, not after a doomed wait
        self.assertEqual(resp["denied"]["code"], "open-gap-timeout")
        self.assertGreater(resp["denied"]["retry_after_sec"], 0.0)


class MinRestartWireTest(ServerTestBase):
    policy_kwargs = {"min_restart": 1.0}

    def test_reclaim_churn_denied_then_allowed(self):
        s = self.connect()
        granted = self.claim(s, serial=RTL_GROUND)["granted"]
        self.rpc(s, cmd="release", lease_id=granted["lease_id"])
        resp = self.claim(self.connect(), serial=RTL_GROUND, consumer="again")
        self.assertEqual(resp["denied"]["code"], "min-restart-interval")
        retry = resp["denied"]["retry_after_sec"]
        self.assertGreater(retry, 0.0)
        self.assertLessEqual(retry, 1.0)
        time.sleep(retry + 0.1)
        resp = self.claim(self.connect(), serial=RTL_GROUND, consumer="again")
        self.assertTrue(resp["ok"], msg=resp)


class SingletonBrokerTest(ServerTestBase):
    def test_second_broker_on_live_socket_refuses(self):
        with self.assertRaises(BrokerAlreadyRunning):
            BrokerServer(self.policy).start()

    def test_stale_socket_file_is_replaced(self):
        self.server.shutdown()
        # Fabricate the crashed-broker leftover: a bound-then-abandoned file.
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(self.sock_path)
        dead.close()
        revived = BrokerServer(self.policy)
        revived.start()
        self.addCleanup(revived.shutdown)
        self.assertTrue(self.rpc(self.connect(), cmd="status")["ok"])


class ConcurrentWireStressTest(ServerTestBase):
    def test_exactly_one_winner_across_connections(self):
        results = []
        mu = threading.Lock()
        barrier = threading.Barrier(8)

        def worker(i):
            s = self.connect()
            barrier.wait()
            resp = self.claim(s, serial=RSP_B, consumer=f"worker-{i}")
            with mu:
                results.append(resp)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        grants = [r for r in results if r.get("ok")]
        denials = [r for r in results if not r.get("ok")]
        self.assertEqual(len(grants), 1, msg=results)
        self.assertEqual(len(denials), 7)
        for d in denials:
            self.assertEqual(d["denied"]["code"], "already-leased")

    def test_role_stress_fills_both_flex_slots_exactly(self):
        results = []
        mu = threading.Lock()
        barrier = threading.Barrier(8)

        def worker(i):
            s = self.connect()
            barrier.wait()
            resp = self.claim(s, role="flex", consumer=f"worker-{i}")
            with mu:
                results.append(resp)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        grants = [r["granted"]["serial"] for r in results if r.get("ok")]
        self.assertEqual(sorted(grants), sorted([RTL_FLEX_1, RTL_FLEX_2]))
        self.assertEqual(len(results), 8)


if __name__ == "__main__":
    unittest.main()
