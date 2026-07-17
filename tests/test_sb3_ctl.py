"""Phase 1 invariants for sb3-ctl.

The load-bearing test in this file is TestDryRunTouchesNothing: it asserts that
no code path reachable from `kill` without --execute can call subprocess or
sleep. Everything else is scaffolding around that claim.
"""

from __future__ import annotations

import unittest
from pathlib import Path
import urllib.error
from unittest import mock

from sb3 import backends, killswitch, ownership, settle
from sb3.state import State


class TestOwnershipBoundary(unittest.TestCase):
    """The §4.2 boundary must be real, not a diagram."""

    def test_sets_are_disjoint(self):
        ownership.assert_disjoint()
        self.assertFalse(ownership.SB3_LAYER & ownership.BACKEND)

    def test_backends_are_never_in_the_kill_order(self):
        # The single most important assertion here: no path exists by which
        # `kill` stops SDRangel, SDRTrunk, icecast, or a bridge.
        for label in ownership.KILL_ORDER:
            self.assertNotIn(label, ownership.BACKEND,
                             f"{label} is BACKEND but appears in KILL_ORDER")

    def test_kill_order_covers_every_sb3_agent(self):
        self.assertEqual(set(ownership.KILL_ORDER), set(ownership.SB3_LAYER),
                         "every SB3-owned agent must have a defined stop position")

    def test_broker_is_stopped_last(self):
        # §4.3 step 3: lease consumers before the broker. broker/client.py holds
        # the lease socket for its child's whole lifetime; the broker dying
        # underneath it is a case nobody has specified.
        self.assertEqual(ownership.KILL_ORDER[-1],
                         "com.scannerproject.tuner-broker")

    def test_consumers_precede_broker(self):
        order = list(ownership.KILL_ORDER)
        broker_at = order.index("com.scannerproject.tuner-broker")
        for consumer in ("com.scannerproject.disco", "com.scannerproject.acars",
                         "com.scannerproject.survey"):
            self.assertLess(order.index(consumer), broker_at,
                            f"{consumer} must stop before the broker")

    def test_sdrangel_and_sdrtrunk_are_backend(self):
        # The two that must survive a kill, named explicitly so a careless edit
        # to the sets trips a test rather than a live box.
        self.assertIn("com.scannerproject.sdrangel", ownership.BACKEND)
        self.assertIn("com.scannerproject.sdrtrunk", ownership.BACKEND)

    def test_unclassified_label_raises(self):
        with self.assertRaises(ownership.UnclassifiedLabel):
            ownership.classify("com.scannerproject.brand-new-thing")

    def test_classify_all_buckets_without_raising(self):
        got = ownership.classify_all([
            "com.scannerproject.sdrangel",
            "com.scannerproject.tuner-broker",
            "com.scannerproject.mystery",
        ])
        self.assertEqual(got["backend"], ["com.scannerproject.sdrangel"])
        self.assertEqual(got["sb3"], ["com.scannerproject.tuner-broker"])
        self.assertEqual(got["unclassified"], ["com.scannerproject.mystery"])

    def test_kill_sequence_uses_canonical_order_not_caller_order(self):
        loaded = ["com.scannerproject.tuner-broker", "com.scannerproject.disco"]
        self.assertEqual(
            ownership.kill_sequence(loaded),
            ["com.scannerproject.disco", "com.scannerproject.tuner-broker"],
        )

    def test_kill_sequence_ignores_backend_labels(self):
        self.assertEqual(ownership.kill_sequence(["com.scannerproject.sdrtrunk"]), [])


class TestDryRunTouchesNothing(unittest.TestCase):
    """Phase 1's core safety claim, asserted rather than promised."""

    def setUp(self):
        self.lines = []

    def _emit(self, msg):
        self.lines.append(msg)

    def test_kill_dry_run_never_calls_subprocess_or_sleep(self):
        with mock.patch("subprocess.run") as run, \
             mock.patch("time.sleep") as sleep, \
             mock.patch.object(backends, "launchctl_loaded",
                               return_value=["com.scannerproject.tuner-broker",
                                             "com.scannerproject.sdrangel"]), \
             mock.patch.object(backends, "mount_state",
                               side_effect=lambda m, **kw: backends.MountState(m, 200, True)):
            rc = killswitch.cmd_kill(execute=False, emit=self._emit,
                                     state=State(Path("/nonexistent")), uid=501)
        run.assert_not_called()
        sleep.assert_not_called()
        self.assertEqual(rc, killswitch.EXIT_OK)

    def test_kill_dry_run_prints_would_for_every_action(self):
        with mock.patch.object(backends, "launchctl_loaded",
                               return_value=["com.scannerproject.tuner-broker"]), \
             mock.patch.object(backends, "mount_state",
                               side_effect=lambda m, **kw: backends.MountState(m, 200, True)):
            killswitch.cmd_kill(execute=False, emit=self._emit,
                                state=State(Path("/nonexistent")), uid=501)
        out = "\n".join(self.lines)
        self.assertIn("DRY RUN", out)
        self.assertIn("would: launchctl bootout gui/501/com.scannerproject.tuner-broker", out)
        self.assertIn("Nothing was stopped", out)

    def test_kill_dry_run_never_writes_the_sentinel(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            state = State(Path(td) / "state")
            with mock.patch.object(backends, "launchctl_loaded", return_value=[]), \
                 mock.patch.object(backends, "mount_state",
                                   side_effect=lambda m, **kw: backends.MountState(m, 200, True)):
                killswitch.cmd_kill(execute=False, emit=self._emit, state=state, uid=501)
            self.assertFalse(state.killed_path.exists(),
                             "dry run must not arm the sentinel")

    def test_execute_is_refused(self):
        rc = killswitch.cmd_kill(execute=True, emit=self._emit)
        self.assertEqual(rc, killswitch.EXIT_REFUSED)
        self.assertIn("REFUSED", "\n".join(self.lines))

    def test_bootout_dry_run_does_not_call_subprocess(self):
        with mock.patch("subprocess.run") as run:
            ok = settle.bootout("com.scannerproject.disco", 501,
                                execute=False, emit=self._emit)
        run.assert_not_called()
        self.assertTrue(ok)
        self.assertIn("would: launchctl bootout", self.lines[0])

    def test_drain_dry_run_does_not_sleep(self):
        with mock.patch("time.sleep") as sleep:
            settle.drain(execute=False, emit=self._emit)
        sleep.assert_not_called()


class TestInvariantVerification(unittest.TestCase):
    """`kill` must prove the invariant, not assume it (§4.3 step 6)."""

    def setUp(self):
        self.lines = []

    def _emit(self, msg):
        self.lines.append(msg)

    def test_dropped_mount_fails_the_check(self):
        before = {"neptune-trunk.mp3": backends.MountState("neptune-trunk.mp3", 200, True)}
        with mock.patch.object(backends, "mount_state",
                               return_value=backends.MountState("neptune-trunk.mp3", 404, False)):
            rc = killswitch.verify_mounts(before, emit=self._emit)
        self.assertEqual(rc, killswitch.EXIT_INVARIANT_VIOLATED)
        self.assertIn("INVARIANT VIOLATED", "\n".join(self.lines))

    def test_held_mount_passes(self):
        before = {"neptune-trunk.mp3": backends.MountState("neptune-trunk.mp3", 200, True)}
        with mock.patch.object(backends, "mount_state",
                               return_value=backends.MountState("neptune-trunk.mp3", 200, True)):
            rc = killswitch.verify_mounts(before, emit=self._emit)
        self.assertEqual(rc, killswitch.EXIT_OK)

    def test_already_down_mount_is_not_our_fault(self):
        # neptune-angel.mp3 is deliberately paused. `kill` is accountable for
        # what it breaks, not for what it inherited.
        before = {"neptune-angel.mp3": backends.MountState("neptune-angel.mp3", 404, False)}
        with mock.patch.object(backends, "mount_state",
                               return_value=backends.MountState("neptune-angel.mp3", 404, False)):
            rc = killswitch.verify_mounts(before, emit=self._emit)
        self.assertEqual(rc, killswitch.EXIT_OK)
        self.assertIn("not ours", "\n".join(self.lines))


class TestFailClosedSentinel(unittest.TestCase):
    """§4.4: absence of the sentinel is NOT permission to act."""

    def test_missing_sentinel_reads_as_not_killed(self):
        self.assertFalse(State(Path("/nonexistent/sb3")).is_killed())

    def test_unreadable_sentinel_fails_closed(self):
        state = State(Path("/nonexistent/sb3"))
        with mock.patch.object(Path, "is_file", side_effect=OSError("boom")):
            self.assertTrue(state.is_killed(),
                            "an unreadable sentinel must fail CLOSED (refuse to act)")

    def test_resume_without_sentinel_does_not_assert(self):
        lines = []
        rc = killswitch.cmd_resume(emit=lines.append, state=State(Path("/nonexistent")))
        self.assertEqual(rc, killswitch.EXIT_OK)
        self.assertIn("absence is NOT permission to reconcile", "\n".join(lines))


class TestMountProbeUsesGet(unittest.TestCase):
    """Icecast answers HEAD with 400 — a HEAD probe neuters the invariant check.

    Regression guard for a real bug caught during Phase 1 bring-up against live
    Neptune: with a HEAD probe every mount read 400, so `present` was uniformly
    False and verify_mounts() would have compared False->False on a genuinely
    dropped mount and called it "already down; not ours".
    """

    def test_mount_probe_does_not_use_head(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["method"] = req.get_method()
            captured["range"] = req.headers.get("Range")
            raise urllib.error.URLError("stop here")

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            backends.mount_state("neptune-trunk.mp3")
        self.assertEqual(captured["method"], "GET",
                         "icecast answers HEAD with 400 — the probe must GET")
        self.assertEqual(captured["range"], "bytes=0-0",
                         "range-limit the GET so a live stream is not consumed")

    def test_400_is_not_treated_as_present(self):
        self.assertFalse(backends.MountState("m", 400, False).present)


class TestPhantomDeviceset(unittest.TestCase):
    """The failure mode Phase 0 found live on Neptune 2026-07-16."""

    def test_aaronia_with_no_serial_is_phantom(self):
        ds = backends.DevicesetState(0, "AaroniaRTSA", None, "idle", 1450000, ["135.100 KPHL Tower"])
        self.assertTrue(ds.is_phantom)

    def test_real_device_is_not_phantom(self):
        ds = backends.DevicesetState(0, "RTLSDR", "83241970", "running", 124000000, [])
        self.assertFalse(ds.is_phantom)


if __name__ == "__main__":
    unittest.main()
