"""LeaseLedger invariants, headless, on a fake clock — no sockets, no sleeps.

Covers the whole claim-time gauntlet from broker/leases.py:
already-leased naming the holder, the 0x6bed dual-tuner limit, the rspduo
open-gap serialization (as WAIT outcomes advanced by a FakeClock), the
min-restart-interval anti-churn denial with its retry horizon (the 2026-06
apiService daemon-wedge history), unknown serial/role listing the fleet,
role-based claiming with hot-spare fallthrough, the belt-and-braces flock,
and the atomically-mirrored state file.
"""

import fcntl
import json
import os
import shutil
import tempfile
import threading
import unittest

from test_tuner_broker_helpers import (
    RSP_A,
    RSP_B,
    RTL_FLEX_1,
    RTL_FLEX_2,
    RTL_GROUND,
    FakeClock,
    make_policy,
)

from broker.leases import (
    DENIED,
    DENY_ALREADY_LEASED,
    DENY_BAD_REQUEST,
    DENY_DUAL_TUNER_FORBIDDEN,
    DENY_DUAL_TUNER_LIMIT,
    DENY_EXTERNAL_LOCK,
    DENY_MIN_RESTART_INTERVAL,
    DENY_ROLE_EXHAUSTED,
    DENY_UNKNOWN_DEVICE,
    DENY_UNKNOWN_ROLE,
    GRANTED,
    WAIT,
    LeaseLedger,
)


class LedgerTestBase(unittest.TestCase):
    policy_kwargs: dict = {}

    def setUp(self):
        self.run_dir = tempfile.mkdtemp(prefix="ledger")
        self.addCleanup(shutil.rmtree, self.run_dir, ignore_errors=True)
        self.clock = FakeClock()
        self.policy = make_policy(self.run_dir, **self.policy_kwargs)
        self.ledger = LeaseLedger(self.policy, monotonic=self.clock)
        self.ledger.startup()

    def claim(self, **kw):
        kw.setdefault("consumer", "test-consumer")
        kw.setdefault("reason", "unit test")
        return self.ledger.claim(**kw)

    def grant(self, **kw):
        out = self.claim(**kw)
        self.assertEqual(out.kind, GRANTED, msg=getattr(out.denial, "reason", None))
        return out.lease


class GrantDenyReleaseTest(LedgerTestBase):
    def test_grant_release_regrant(self):
        lease = self.grant(serial=RTL_GROUND)
        self.assertEqual(lease.device.id, "RTL-1")
        self.assertIsNotNone(self.ledger.find_lease(RTL_GROUND))
        self.assertTrue(self.ledger.release(lease.lease_id))
        self.assertIsNone(self.ledger.find_lease(RTL_GROUND))
        # min_restart=0 in this policy: immediate re-claim is fine.
        self.grant(serial=RTL_GROUND, consumer="second")

    def test_release_is_idempotent(self):
        lease = self.grant(serial=RTL_GROUND)
        self.assertTrue(self.ledger.release(lease.lease_id))
        self.assertFalse(self.ledger.release(lease.lease_id))
        self.assertFalse(self.ledger.release("never-existed"))

    def test_unknown_serial_denied_listing_fleet(self):
        out = self.claim(serial="DEADBEEF")
        self.assertEqual(out.kind, DENIED)
        self.assertEqual(out.denial.code, DENY_UNKNOWN_DEVICE)
        # The denial must let the caller fix its config without reading the
        # policy by hand: every known serial(role) pair is in the reason.
        for serial in (RSP_A, RSP_B, RTL_GROUND, RTL_FLEX_1, RTL_FLEX_2):
            self.assertIn(serial, out.denial.reason)

    def test_unknown_role_denied_listing_roles(self):
        out = self.claim(role="p25-trunk")
        self.assertEqual(out.denial.code, DENY_UNKNOWN_ROLE)
        self.assertIn("flex", out.denial.reason)
        self.assertIn("chirp-airband", out.denial.reason)

    def test_already_leased_names_holder_and_age(self):
        self.grant(serial=RSP_B, consumer="gr-demod@airband", reason="airband ST")
        self.clock.advance(42.0)
        out = self.claim(serial=RSP_B, consumer="disco")
        self.assertEqual(out.denial.code, DENY_ALREADY_LEASED)
        self.assertIn("gr-demod@airband", out.denial.reason)
        self.assertIn("airband ST", out.denial.reason)
        self.assertIn("42.0s", out.denial.reason)

    def test_bad_requests(self):
        self.assertEqual(self.claim(serial=RSP_B, consumer="  ").denial.code, DENY_BAD_REQUEST)
        self.assertEqual(self.claim(serial=RSP_B, reason="").denial.code, DENY_BAD_REQUEST)
        self.assertEqual(self.claim().denial.code, DENY_BAD_REQUEST)  # no serial, no role


class DualTunerInvariantTest(LedgerTestBase):
    """The 0x6bed class: never more than max_concurrent_dual_tuner_rspduo
    dual-tuner leases through the single apiService."""

    policy_kwargs = {"rsp_b_dual_capable": True, "max_dual": 1}

    def test_dual_claim_on_capable_device_grants(self):
        lease = self.grant(serial=RSP_A, dual_tuner=True)
        self.assertTrue(lease.dual_tuner)

    def test_dual_claim_beyond_limit_denied_naming_holder(self):
        self.grant(serial=RSP_A, consumer="sdrtrunk", dual_tuner=True)
        out = self.claim(serial=RSP_B, consumer="chirp", dual_tuner=True)
        self.assertEqual(out.denial.code, DENY_DUAL_TUNER_LIMIT)
        self.assertIn("0x6bed", out.denial.reason)
        self.assertIn("sdrtrunk", out.denial.reason)
        # Single-tuner use of the same device is still fine.
        self.grant(serial=RSP_B, consumer="chirp", dual_tuner=False)

    def test_dual_slot_frees_on_release(self):
        first = self.grant(serial=RSP_A, dual_tuner=True)
        self.ledger.release(first.lease_id)
        self.grant(serial=RSP_B, dual_tuner=True)


class DualTunerForbiddenTest(LedgerTestBase):
    policy_kwargs = {"rsp_b_dual_capable": False}

    def test_dual_claim_on_single_tuner_policy_device_denied(self):
        # RSP-B is dual_tuner=false in policy (the real fleet keeps BOTH
        # false until the D1 ladder gate) — a dual claim is a policy
        # violation, not a scheduling problem.
        out = self.claim(serial=RSP_B, dual_tuner=True)
        self.assertEqual(out.denial.code, DENY_DUAL_TUNER_FORBIDDEN)
        self.assertIn("policy", out.denial.reason)

    def test_dual_claim_on_rtl_denied(self):
        out = self.claim(serial=RTL_GROUND, dual_tuner=True)
        self.assertEqual(out.denial.code, DENY_DUAL_TUNER_FORBIDDEN)


class DualTunerZeroBudgetTest(LedgerTestBase):
    policy_kwargs = {"max_dual": 0}

    def test_even_first_dual_claim_denied_at_zero(self):
        out = self.claim(serial=RSP_A, dual_tuner=True)
        self.assertEqual(out.denial.code, DENY_DUAL_TUNER_LIMIT)


class OpenGapSerializationTest(LedgerTestBase):
    """(d): rspduo grants spaced >= rspduo_open_gap_sec on the monotonic
    clock — the apiService cannot take concurrent sdrplay_api_Open calls."""

    policy_kwargs = {"gap": 2.0}

    def test_second_rspduo_claim_waits_out_the_gap(self):
        self.grant(serial=RSP_A)
        out = self.claim(serial=RSP_B)
        self.assertEqual(out.kind, WAIT)
        self.assertAlmostEqual(out.wait_sec, 2.0, places=6)

        self.clock.advance(1.25)
        out = self.claim(serial=RSP_B)
        self.assertEqual(out.kind, WAIT)
        self.assertAlmostEqual(out.wait_sec, 0.75, places=6)

        self.clock.advance(0.75)
        self.grant(serial=RSP_B)

    def test_gap_restarts_after_each_rspduo_grant(self):
        self.grant(serial=RSP_A)
        self.clock.advance(2.0)
        self.grant(serial=RSP_B)
        self.ledger.release(self.ledger.find_lease(RSP_A).lease_id)
        # RSP-B's grant just reset the spacing window: an immediate RSP-A
        # re-claim must wait the full gap again.
        out = self.claim(serial=RSP_A, consumer="again")
        self.assertEqual(out.kind, WAIT)
        self.assertAlmostEqual(out.wait_sec, 2.0, places=6)

    def test_rtl_claims_are_never_gap_blocked(self):
        self.grant(serial=RSP_A)
        # No wait: only kind=rspduo funnels through the apiService.
        self.grant(serial=RTL_GROUND)
        self.grant(serial=RTL_FLEX_1)


class MinRestartIntervalTest(LedgerTestBase):
    """(e) anti-churn: rapid open/close wedges the apiService."""

    policy_kwargs = {"min_restart": 30.0}

    def test_reclaim_within_interval_denied_with_retry_after(self):
        lease = self.grant(serial=RSP_B)
        self.clock.advance(5.0)
        self.ledger.release(lease.lease_id)
        self.clock.advance(10.0)
        out = self.claim(serial=RSP_B, consumer="again")
        self.assertEqual(out.denial.code, DENY_MIN_RESTART_INTERVAL)
        self.assertAlmostEqual(out.denial.retry_after_sec, 20.0, places=3)
        self.assertIn("wedge", out.denial.reason)

    def test_reclaim_after_interval_grants(self):
        lease = self.grant(serial=RSP_B)
        self.ledger.release(lease.lease_id)
        self.clock.advance(30.1)
        self.grant(serial=RSP_B, consumer="again")

    def test_auto_release_also_arms_the_interval(self):
        # A crash-release is MORE churn-suspicious than a clean one, not less.
        lease = self.grant(serial=RTL_GROUND)
        self.ledger.release(lease.lease_id, auto=True)
        out = self.claim(serial=RTL_GROUND, consumer="again")
        self.assertEqual(out.denial.code, DENY_MIN_RESTART_INTERVAL)

    def test_interval_is_per_serial(self):
        lease = self.grant(serial=RTL_GROUND)
        self.ledger.release(lease.lease_id)
        # Other serials are untouched by RTL-1's cooldown.
        self.grant(serial=RTL_FLEX_1)


class RoleClaimTest(LedgerTestBase):
    def test_role_claim_takes_first_candidate(self):
        lease = self.grant(role="flex")
        self.assertEqual(lease.device.serial, RTL_FLEX_1)

    def test_role_claim_falls_through_to_hot_spare(self):
        # Policy-order fallthrough IS hot-spare promotion: first flex RTL
        # is taken, the claim lands on the second.
        self.grant(role="flex", consumer="vdl2")
        lease = self.grant(role="flex", consumer="disco")
        self.assertEqual(lease.device.serial, RTL_FLEX_2)

    def test_role_exhausted_reports_every_candidate(self):
        self.grant(role="flex", consumer="vdl2")
        self.grant(role="flex", consumer="disco")
        out = self.claim(role="flex", consumer="acars")
        self.assertEqual(out.denial.code, DENY_ROLE_EXHAUSTED)
        self.assertIn(RTL_FLEX_1, out.denial.reason)
        self.assertIn(RTL_FLEX_2, out.denial.reason)
        self.assertIn("vdl2", out.denial.reason)
        self.assertIn("disco", out.denial.reason)

    def test_single_candidate_role_denial_passes_through(self):
        self.grant(role="chirp-ground", consumer="gr-demod@ground")
        out = self.claim(role="chirp-ground", consumer="other")
        # Not role-exhausted noise — the single candidate's own denial.
        self.assertEqual(out.denial.code, DENY_ALREADY_LEASED)
        self.assertIn("gr-demod@ground", out.denial.reason)


class RoleExhaustedRetryHorizonTest(LedgerTestBase):
    policy_kwargs = {"min_restart": 30.0}

    def test_role_exhausted_carries_min_retry_after(self):
        a = self.grant(role="flex", consumer="vdl2")
        self.ledger.release(a.lease_id)          # RTL-4 cooling down (30s)
        self.grant(role="flex", consumer="disco")  # RTL-5 now held
        self.clock.advance(10.0)
        out = self.claim(role="flex", consumer="acars")
        self.assertEqual(out.denial.code, DENY_ROLE_EXHAUSTED)
        # RTL-4 frees in 20s; RTL-5 has no horizon — report the known one.
        self.assertAlmostEqual(out.denial.retry_after_sec, 20.0, places=3)


class FlockBeltAndBracesTest(LedgerTestBase):
    def _lock_path(self, serial):
        return os.path.join(self.policy.broker.lock_dir, f"{serial}.lock")

    def test_flock_held_while_leased(self):
        lease = self.grant(serial=RTL_GROUND)
        path = self._lock_path(RTL_GROUND)
        self.assertTrue(os.path.exists(path))
        fd = os.open(path, os.O_RDWR)
        try:
            with self.assertRaises(OSError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        # ...and free after release.
        self.ledger.release(lease.lease_id)
        fd = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def test_foreign_flock_denies_claim(self):
        # A process that bypassed the broker but honors the flock convention
        # still excludes us — denial, not a USB fight.
        path = self._lock_path(RTL_GROUND)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            out = self.claim(serial=RTL_GROUND)
            self.assertEqual(out.denial.code, DENY_EXTERNAL_LOCK)
            self.assertIn("non-broker", out.denial.reason)
        finally:
            os.close(fd)

    def test_clear_locks_at_boot(self):
        stray = self._lock_path("99999999")
        with open(stray, "w") as fh:
            fh.write("stale\n")
        fresh = LeaseLedger(make_policy(self.run_dir), monotonic=self.clock)
        fresh.startup()
        self.assertFalse(os.path.exists(stray))

    def test_clear_locks_at_boot_false_keeps_files(self):
        stray = self._lock_path("99999999")
        with open(stray, "w") as fh:
            fh.write("stale\n")
        fresh = LeaseLedger(
            make_policy(self.run_dir, clear_locks=False), monotonic=self.clock
        )
        fresh.startup()
        self.assertTrue(os.path.exists(stray))


class StateFileTest(LedgerTestBase):
    def _read_state(self):
        with open(self.policy.broker.state_file, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def test_state_written_at_startup(self):
        state = self._read_state()
        self.assertEqual(state["v"], 1)
        self.assertEqual(len(state["devices"]), 5)
        self.assertEqual(state["leases"], [])

    def test_state_tracks_grant_deny_release(self):
        lease = self.grant(serial=RSP_B, consumer="gr-demod@airband", reason="airband ST")
        state = self._read_state()
        self.assertEqual(len(state["leases"]), 1)
        row = state["leases"][0]
        self.assertEqual(row["serial"], RSP_B)
        self.assertEqual(row["consumer"], "gr-demod@airband")
        self.assertEqual(row["reason"], "airband ST")
        dev_row = next(d for d in state["devices"] if d["serial"] == RSP_B)
        self.assertTrue(dev_row["leased"])
        self.assertEqual(dev_row["holder"], "gr-demod@airband")

        self.claim(serial=RSP_B, consumer="disco")  # denied
        state = self._read_state()
        self.assertEqual(state["counters"]["denials"], 1)
        self.assertEqual(state["recent_denials"][-1]["code"], DENY_ALREADY_LEASED)
        self.assertEqual(state["recent_denials"][-1]["consumer"], "disco")

        self.ledger.release(lease.lease_id)
        state = self._read_state()
        self.assertEqual(state["leases"], [])
        self.assertEqual(state["counters"]["releases"], 1)

    def test_auto_release_counted_separately(self):
        lease = self.grant(serial=RTL_GROUND)
        self.ledger.release(lease.lease_id, auto=True)
        counters = self._read_state()["counters"]
        self.assertEqual(counters["auto_releases"], 1)
        self.assertEqual(counters["releases"], 0)

    def test_state_file_always_parses_under_churn(self):
        """Atomicity (tmp+os.replace): a reader can NEVER see a torn write,
        no matter when it looks."""
        stop = threading.Event()
        errors = []

        def churn():
            try:
                for i in range(150):
                    out = self.claim(serial=RTL_FLEX_1, consumer=f"churn-{i}")
                    if out.kind == GRANTED:
                        self.ledger.release(out.lease.lease_id)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                stop.set()

        worker = threading.Thread(target=churn)
        worker.start()
        reads = 0
        while not stop.is_set():
            state = self._read_state()  # raises on any torn/partial JSON
            self.assertEqual(state["v"], 1)
            reads += 1
        worker.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertGreater(reads, 0)
        leftovers = [n for n in os.listdir(self.run_dir) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])


class ConcurrentClaimStressTest(LedgerTestBase):
    def test_exactly_one_winner_per_serial(self):
        grants, denials = [], []
        barrier = threading.Barrier(8)

        def worker(i):
            barrier.wait()
            out = self.claim(serial=RTL_GROUND, consumer=f"worker-{i}")
            (grants if out.kind == GRANTED else denials).append(out)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(len(grants), 1)
        self.assertEqual(len(denials), 7)
        for out in denials:
            self.assertEqual(out.denial.code, DENY_ALREADY_LEASED)

    def test_role_stress_grants_each_candidate_once(self):
        grants, denials = [], []
        barrier = threading.Barrier(8)
        mu = threading.Lock()

        def worker(i):
            barrier.wait()
            out = self.claim(role="flex", consumer=f"worker-{i}")
            with mu:
                (grants if out.kind == GRANTED else denials).append(out)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(len(grants), 2)  # two flex candidates, one lease each
        self.assertEqual({g.lease.device.serial for g in grants}, {RTL_FLEX_1, RTL_FLEX_2})
        self.assertEqual(len(denials), 6)


if __name__ == "__main__":
    unittest.main()
