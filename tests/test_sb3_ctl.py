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

from sb3 import backends, install, killswitch, ownership, settle
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

    def test_brokers_are_stopped_last(self):
        # §4.3 step 3: lease consumers before the broker. broker/client.py holds
        # the lease socket for its child's whole lifetime; the broker dying
        # underneath it is a case nobody has specified.
        #
        # Both broker labels occupy the tail: sb3-broker is the Phase 1 stub that
        # Phase 2 replaces with the real tuner-broker, so they coexist for now.
        self.assertEqual(
            list(ownership.KILL_ORDER[-2:]),
            ["com.scannerproject.tuner-broker", "com.scannerproject.sb3-broker"],
        )

    def test_every_consumer_precedes_every_broker(self):
        order = list(ownership.KILL_ORDER)
        brokers = ("com.scannerproject.tuner-broker", "com.scannerproject.sb3-broker")
        consumers = ("com.scannerproject.disco", "com.scannerproject.acars",
                     "com.scannerproject.survey")
        first_broker = min(order.index(b) for b in brokers)
        for c in consumers:
            self.assertLess(order.index(c), first_broker,
                            f"{c} holds a lease and must stop before any broker")

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

    def test_kill_dry_run_shows_full_order_even_when_nothing_is_loaded(self):
        # The ordering is the part §6 warns is hard to retrofit, so it must be
        # reviewable whether or not agents happen to be running today.
        with mock.patch.object(backends, "launchctl_loaded", return_value=[]), \
             mock.patch.object(backends, "mount_state",
                               side_effect=lambda m, **kw: backends.MountState(m, 200, True)):
            killswitch.cmd_kill(execute=False, emit=self._emit,
                                state=State(Path("/nonexistent")), uid=501)
        out = "\n".join(self.lines)
        for label in ownership.KILL_ORDER:
            self.assertIn(label, out, f"{label} missing from the dry-run plan")
        self.assertIn("com.scannerproject.sb3-broker", out)
        self.assertIn("com.scannerproject.sb3-controller", out)

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

    def test_execute_refuses_when_unclassified_agents_are_loaded(self):
        # Phase 1.1 enables --execute, so the blanket refusal is gone. What must
        # NOT go is the refusal to act while the boundary is ambiguous: an agent
        # kill has no opinion about is a hard stop, not a shrug.
        with mock.patch.object(backends, "launchctl_loaded",
                               return_value=["com.scannerproject.mystery"]), \
             mock.patch.object(backends, "mount_state",
                               side_effect=lambda m, **kw: backends.MountState(m, 200, True)), \
             mock.patch("subprocess.run") as run, \
             mock.patch("time.sleep"):
            rc = killswitch.cmd_kill(execute=True, emit=self._emit,
                                     state=State(Path("/nonexistent")), uid=501)
        self.assertEqual(rc, killswitch.EXIT_REFUSED)
        run.assert_not_called()
        self.assertIn("REFUSING", "\n".join(self.lines))

    def test_execute_never_boots_out_a_backend_label(self):
        # The load-bearing safety claim now that --execute is live: every
        # bootout argv must name an SB3 label. Nothing can reach a backend.
        import tempfile
        booted = []

        def fake_run(cmd, **kw):
            booted.append(cmd)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(backends, "launchctl_loaded",
                               return_value=["com.scannerproject.sb3-broker",
                                             "com.scannerproject.sb3-controller",
                                             "com.scannerproject.sdrangel",
                                             "com.scannerproject.sdrtrunk",
                                             "com.scannerproject.icecast"]), \
             mock.patch.object(backends, "mount_state",
                               side_effect=lambda m, **kw: backends.MountState(m, 200, True)), \
             mock.patch.object(settle, "is_loaded", return_value=False), \
             mock.patch("subprocess.run", side_effect=fake_run), \
             mock.patch("time.sleep"):
            killswitch.cmd_kill(execute=True, emit=self._emit,
                                state=State(Path(td) / "s"), uid=501)

        self.assertTrue(booted, "expected real bootout calls")
        for cmd in booted:
            target = cmd[-1]
            for backend_label in ownership.BACKEND:
                self.assertNotIn(backend_label, target,
                                 f"kill --execute tried to touch backend {backend_label}")

    def test_execute_arms_the_sentinel_before_stopping_anything(self):
        import tempfile
        order = []
        with tempfile.TemporaryDirectory() as td:
            state = State(Path(td) / "s")
            real_arm = state.arm

            def tracking_arm():
                order.append("arm")
                real_arm()

            with mock.patch.object(state, "arm", side_effect=tracking_arm), \
                 mock.patch.object(backends, "launchctl_loaded",
                                   return_value=["com.scannerproject.sb3-broker"]), \
                 mock.patch.object(backends, "mount_state",
                                   side_effect=lambda m, **kw: backends.MountState(m, 200, True)), \
                 mock.patch.object(settle, "is_loaded", return_value=False), \
                 mock.patch("subprocess.run",
                            side_effect=lambda c, **k: (order.append("bootout"),
                                                        mock.Mock(returncode=0, stdout="", stderr=""))[1]), \
                 mock.patch("time.sleep"):
                killswitch.cmd_kill(execute=True, emit=self._emit, state=state, uid=501)
        self.assertEqual(order[0], "arm",
                         "sentinel must be armed BEFORE teardown — a half-torn-down "
                         "state must read as killed, not healthy")

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


class TestManagedAgents(unittest.TestCase):
    """The two Phase 1 lifecycle stubs must be SB3-owned and killable."""

    def test_managed_agents_are_sb3_owned(self):
        for label in ownership.MANAGED_AGENTS:
            self.assertIn(label, ownership.SB3_LAYER,
                          f"{label} is installed by SB3 and must die on kill")
            self.assertNotIn(label, ownership.BACKEND)

    def test_managed_agents_have_a_kill_position(self):
        for label in ownership.MANAGED_AGENTS:
            self.assertIn(label, ownership.KILL_ORDER)

    def test_controller_stops_before_brokers(self):
        # §4.3 step 2: stop asserting first. The controller is the reconciler's
        # seat, and a reconciler still asserting while its broker goes down is
        # the churn the ordering exists to prevent.
        order = list(ownership.KILL_ORDER)
        self.assertLess(order.index("com.scannerproject.sb3-controller"),
                        order.index("com.scannerproject.sb3-broker"))

    def test_sb3_broker_is_stopped_last(self):
        self.assertEqual(ownership.KILL_ORDER[-1], "com.scannerproject.sb3-broker")

    def test_plist_templates_exist_for_every_managed_agent(self):
        # A MANAGED_AGENTS entry with no template is an install that would
        # silently write nothing.
        for p in install.plan():
            self.assertTrue(p.template_exists,
                            f"missing plist template for {p.label}: {p.template}")


class TestInstallDryRun(unittest.TestCase):
    """install/uninstall must not touch ~/Library/LaunchAgents in Phase 1."""

    def setUp(self):
        self.lines = []

    def _emit(self, msg):
        self.lines.append(msg)

    def test_install_dry_run_writes_nothing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            agents = Path(td) / "LaunchAgents"
            agents.mkdir()
            rc = install.cmd_install(execute=False, emit=self._emit, agents_dir=agents)
            self.assertEqual(rc, killswitch.EXIT_OK)
            self.assertEqual(list(agents.iterdir()), [],
                             "dry-run install must not write any plist")
        out = "\n".join(self.lines)
        self.assertIn("DRY RUN", out)
        self.assertIn("Nothing was written", out)

    def test_install_dry_run_names_both_agents_and_targets(self):
        install.cmd_install(execute=False, emit=self._emit)
        out = "\n".join(self.lines)
        for label in ownership.MANAGED_AGENTS:
            self.assertIn(label, out)
            self.assertIn(f"{label}.plist", out)
        self.assertIn("sb3.agents.broker_stub", out)
        self.assertIn("sb3.agents.controller_stub", out)

    def test_install_does_not_load_agents(self):
        # install writes; bootstrap loads. Keeping them separate is the point.
        with mock.patch("subprocess.run") as run:
            install.cmd_install(execute=False, emit=self._emit)
        run.assert_not_called()
        self.assertIn("NOT loaded by install", "\n".join(self.lines))

    def test_install_execute_writes_valid_plists_into_the_given_dir(self):
        # NOTE: always pass agents_dir. A test that omits it writes to the
        # REAL ~/Library/LaunchAgents — which this suite did exactly once,
        # before this comment existed.
        import plistlib
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            agents = Path(td) / "LaunchAgents"
            rc = install.cmd_install(execute=True, emit=self._emit, agents_dir=agents)
            self.assertEqual(rc, killswitch.EXIT_OK)
            written = sorted(p.name for p in agents.iterdir())
            self.assertEqual(written, [f"{l}.plist"
                                       for l in sorted(ownership.MANAGED_AGENTS)])
            for f in agents.iterdir():
                parsed = plistlib.loads(f.read_bytes())
                self.assertIn(parsed["Label"], ownership.MANAGED_AGENTS)
                self.assertEqual(parsed["KeepAlive"], {"SuccessfulExit": False})
                self.assertNotIn("{{", f.read_text())
                self.assertEqual(f.stat().st_mode & 0o777, 0o644)

    def test_install_execute_still_does_not_load(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, \
             mock.patch("subprocess.run") as run:
            install.cmd_install(execute=True, emit=self._emit,
                                agents_dir=Path(td) / "LaunchAgents")
        run.assert_not_called()
        self.assertIn("Installed, NOT loaded", "\n".join(self.lines))

    def test_install_execute_refuses_an_unparseable_template(self):
        # §4.6: never ship a file we have not parsed. plutil is lenient about
        # `--` in XML comments; plistlib is not, and so is launchd.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            agents = Path(td) / "LaunchAgents"
            with mock.patch.object(Path, "read_text", return_value="<not a plist"):
                rc = install.cmd_install(execute=True, emit=self._emit, agents_dir=agents)
            self.assertEqual(rc, killswitch.EXIT_INVARIANT_VIOLATED)
            self.assertIn("does not parse", "\n".join(self.lines))
            self.assertFalse(any(agents.iterdir()) if agents.exists() else False,
                             "must not write a plist it could not parse")

    def test_uninstall_execute_removes_only_sb3_plists(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            agents = Path(td) / "LaunchAgents"
            agents.mkdir()
            for label in ownership.MANAGED_AGENTS:
                (agents / f"{label}.plist").write_text("<plist/>")
            bystander = agents / "com.scannerproject.sdrangel.plist"
            bystander.write_text("<plist/>")
            with mock.patch.object(settle, "is_loaded", return_value=False):
                rc = install.cmd_uninstall(execute=True, emit=self._emit,
                                           agents_dir=agents)
            self.assertEqual(rc, killswitch.EXIT_OK)
            for label in ownership.MANAGED_AGENTS:
                self.assertFalse((agents / f"{label}.plist").exists())
            self.assertTrue(bystander.exists(),
                            "uninstall must never remove a backend plist")

    def test_uninstall_dry_run_removes_nothing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            agents = Path(td) / "LaunchAgents"
            agents.mkdir()
            planted = agents / "com.scannerproject.sb3-broker.plist"
            planted.write_text("<plist/>")
            rc = install.cmd_uninstall(execute=False, emit=self._emit, agents_dir=agents)
            self.assertEqual(rc, killswitch.EXIT_OK)
            self.assertTrue(planted.exists(), "dry-run uninstall must not remove")
        self.assertIn("Nothing was removed", "\n".join(self.lines))

    def test_uninstall_never_targets_a_backend_agent(self):
        install.cmd_uninstall(execute=False, emit=self._emit)
        out = "\n".join(self.lines)
        for label in ownership.BACKEND:
            self.assertNotIn(f"rm {label}", out)
            self.assertNotIn(f"bootout gui/$(id -u)/{label}", out)

    def test_render_substitutes_every_placeholder(self):
        rendered = install.render(
            "{{PYTHON}} {{REPO_DIR}} {{HOME}}",
            python="/usr/bin/python3", repo="/repo", home="/home/w")
        self.assertEqual(rendered, "/usr/bin/python3 /repo /home/w")
        for token in ("{{PYTHON}}", "{{REPO_DIR}}", "{{HOME}}"):
            self.assertNotIn(token, rendered)

    def test_templates_render_to_valid_plists(self):
        import plistlib
        for p in install.plan():
            rendered = install.render(
                p.template.read_text(),
                python="/usr/bin/python3", repo="/repo", home="/home/w")
            self.assertNotIn("{{", rendered,
                             f"unsubstituted placeholder left in {p.label}")
            parsed = plistlib.loads(rendered.encode())
            self.assertEqual(parsed["Label"], p.label)
            # KeepAlive must be SuccessfulExit:false, NOT plain true — plain
            # KeepAlive respawns after a clean exit and would fight `kill`.
            self.assertEqual(parsed["KeepAlive"], {"SuccessfulExit": False},
                             f"{p.label}: KeepAlive=true would fight the kill switch")
            self.assertTrue(parsed["RunAtLoad"])
            self.assertEqual(parsed["LimitLoadToSessionType"], "Aqua")


class TestStubLifecycle(unittest.TestCase):
    """The stubs exist to prove the launchd contract kill depends on."""

    def test_stub_stops_on_signal_flag(self):
        from sb3.agents._stub import Stub
        s = Stub("t", log_dir=Path("/nonexistent"))
        self.assertFalse(s._stop)
        with mock.patch.object(s, "log"):
            s._handle_signal(15, None)
        self.assertTrue(s._stop, "SIGTERM must set the stop flag")

    def test_stub_tick_is_much_shorter_than_heartbeat(self):
        # A SIGTERM must be acted on promptly, not slept through for a whole
        # heartbeat — `launchctl bootout` does not wait around.
        from sb3.agents import _stub
        self.assertLess(_stub._TICK_SEC, 1.0)
        self.assertLess(_stub._TICK_SEC, _stub.HEARTBEAT_SEC)


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
