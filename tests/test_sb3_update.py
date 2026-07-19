"""Tests for `sb3-ctl update` and the git-deploy introspection behind it.

The load-bearing claims:
  * dry-run fetches (a read) but never checks out or bounces agents;
  * execute REFUSES a dirty tree;
  * execute ABORTS (non-zero) if a backend PID moved across the bounce, even
    when every SB3 step itself succeeded;
  * `divergent` is a three-valued fact — True / False / UNKNOWN — never a
    silent "up-to-date" when the remote could not be reached (§4.6).
"""

from __future__ import annotations

import unittest
from unittest import mock

from sb3 import gitdeploy, update
from sb3.gitdeploy import DeployState


def _state(**kw) -> DeployState:
    base = dict(is_git=True, root="/repo", sha="a" * 40, short_sha="aaaaaaa",
                branch="sb3-phase1-scaffold", dirty=False,
                remote_sha="b" * 40, divergent=True, error=None)
    base.update(kw)
    return DeployState(**base)


class TestArgParsingBothPositions(unittest.TestCase):
    """--execute must work before AND after the subcommand."""

    def _parse(self, argv):
        from sb3.__main__ import build_parser
        return build_parser().parse_args(argv)

    def test_execute_after_subcommand(self):
        self.assertTrue(getattr(self._parse(["kill", "--execute"]), "execute", False))

    def test_execute_before_subcommand(self):
        self.assertTrue(getattr(self._parse(["--execute", "kill"]), "execute", False))

    def test_absent_execute_is_false(self):
        # default=SUPPRESS means the attribute is absent when the flag is not
        # given; main() reads it with getattr(..., False), so that is the
        # contract the test asserts too.
        self.assertFalse(getattr(self._parse(["kill"]), "execute", False))

    def test_update_is_a_subcommand(self):
        self.assertEqual(self._parse(["update", "--execute"]).cmd, "update")


class TestDeployStateThreeValued(unittest.TestCase):
    def test_divergent_true_when_shas_differ(self):
        self.assertTrue(_state(sha="a"*40, remote_sha="b"*40, divergent=True).divergent)

    def test_divergent_unknown_survives_as_none(self):
        # The whole point: remote-unreachable is UNKNOWN, not False.
        st = _state(remote_sha=None, divergent=None, error="ls-remote failed")
        self.assertIsNone(st.divergent)
        self.assertIn("remote-unknown", st.summary())


class TestUpdateDryRun(unittest.TestCase):
    def setUp(self):
        self.lines = []

    def _emit(self, m):
        self.lines.append(m)

    def test_dry_run_fetches_but_never_checks_out_or_bounces(self):
        calls = []

        def fake_git(root, *args, **kw):
            calls.append(args)
            if args[0] == "fetch":
                return 0, "", ""
            if args[:2] == ("rev-parse", "HEAD"):
                return 0, "a" * 40, ""
            if args[0] == "rev-parse":
                return 0, "b" * 40, ""
            return 0, "", ""

        with mock.patch.object(gitdeploy, "is_git_checkout", return_value=True), \
             mock.patch.object(gitdeploy, "_git", side_effect=fake_git), \
             mock.patch.object(gitdeploy, "observe", return_value=_state()), \
             mock.patch("sb3.killswitch.cmd_kill") as k, \
             mock.patch("sb3.killswitch.cmd_resume") as r:
            rc = update.cmd_update(execute=False, emit=self._emit)

        self.assertEqual(rc, update.EXIT_OK)
        fetches = [c for c in calls if c[0] == "fetch"]
        self.assertTrue(fetches, "dry-run must fetch")
        # fetch must name the branch's tracking refspec explicitly (a --depth 1
        # clone is single-branch, so origin/<branch> is otherwise absent).
        self.assertTrue(any("refs/remotes/origin/sb3-phase1-scaffold" in " ".join(c)
                            for c in fetches),
                        "fetch must explicitly update the branch tracking ref")
        self.assertFalse(any(c[0] == "checkout" for c in calls),
                         "dry-run must not checkout")
        k.assert_not_called()
        r.assert_not_called()
        self.assertIn("DRY RUN", "\n".join(self.lines))

    def test_refuses_when_not_a_git_checkout(self):
        with mock.patch.object(gitdeploy, "is_git_checkout", return_value=False):
            rc = update.cmd_update(execute=True, emit=self._emit)
        self.assertEqual(rc, update.EXIT_REFUSED)
        self.assertIn("not a git checkout", "\n".join(self.lines))

    def test_already_at_target_is_a_noop(self):
        def fake_git(root, *args, **kw):
            if args[0] == "fetch":
                return 0, "", ""
            return 0, "a" * 40, ""   # HEAD and target both aaa...
        with mock.patch.object(gitdeploy, "is_git_checkout", return_value=True), \
             mock.patch.object(gitdeploy, "_git", side_effect=fake_git), \
             mock.patch.object(gitdeploy, "observe",
                               return_value=_state(sha="a"*40, divergent=False)):
            rc = update.cmd_update(execute=False, emit=self._emit)
        self.assertEqual(rc, update.EXIT_OK)
        self.assertIn("Already at target", "\n".join(self.lines))


class TestUpdateExecuteGuards(unittest.TestCase):
    def setUp(self):
        self.lines = []

    def _emit(self, m):
        self.lines.append(m)

    def _fake_git(self, head="a"*40, target="b"*40):
        def g(root, *args, **kw):
            if args[0] == "fetch":
                return 0, "", ""
            if args[:2] == ("rev-parse", "HEAD"):
                return 0, head, ""
            if args[0] == "rev-parse":
                return 0, target, ""
            if args[0] in ("checkout", "merge"):
                return 0, "", ""
            if args[0] == "status":
                return 0, "", ""
            return 0, "", ""
        return g

    def test_refuses_a_dirty_tree(self):
        with mock.patch.object(gitdeploy, "is_git_checkout", return_value=True), \
             mock.patch.object(gitdeploy, "_git", side_effect=self._fake_git()), \
             mock.patch.object(gitdeploy, "observe",
                               return_value=_state(dirty=True)), \
             mock.patch("sb3.killswitch.cmd_kill") as k:
            rc = update.cmd_update(execute=True, emit=self._emit)
        self.assertEqual(rc, update.EXIT_REFUSED)
        self.assertIn("uncommitted changes", "\n".join(self.lines))
        k.assert_not_called()

    def test_aborts_if_backend_pid_moved_across_the_bounce(self):
        # kill and resume both succeed, but SDRangel's PID changed. That is NOT
        # a clean update — it must return non-zero.
        pid_seq = [
            {"com.scannerproject.sdrangel": "630"},   # before
            {"com.scannerproject.sdrangel": "999"},   # after (moved!)
        ]
        with mock.patch.object(gitdeploy, "is_git_checkout", return_value=True), \
             mock.patch.object(gitdeploy, "_git", side_effect=self._fake_git()), \
             mock.patch.object(gitdeploy, "observe",
                               side_effect=[_state(), _state(),            # pre + st
                                            _state(sha="b"*40, short_sha="bbbbbbb")]), \
             mock.patch.object(update, "_backend_pids", side_effect=pid_seq), \
             mock.patch("sb3.killswitch.cmd_kill", return_value=0), \
             mock.patch("sb3.killswitch.cmd_resume", return_value=0):
            rc = update.cmd_update(execute=True, emit=self._emit)
        self.assertEqual(rc, update.EXIT_INVARIANT_VIOLATED)
        self.assertIn("BACKEND MOVED", "\n".join(self.lines))

    def test_clean_update_succeeds_when_backend_holds(self):
        pids = {"com.scannerproject.sdrangel": "630",
                "com.scannerproject.sdrtrunk": "644"}
        with mock.patch.object(gitdeploy, "is_git_checkout", return_value=True), \
             mock.patch.object(gitdeploy, "_git", side_effect=self._fake_git()), \
             mock.patch.object(gitdeploy, "observe",
                               side_effect=[_state(), _state(),
                                            _state(sha="b"*40, short_sha="bbbbbbb")]), \
             mock.patch.object(update, "_backend_pids", return_value=pids), \
             mock.patch("sb3.killswitch.cmd_kill", return_value=0), \
             mock.patch("sb3.killswitch.cmd_resume", return_value=0):
            rc = update.cmd_update(execute=True, emit=self._emit)
        self.assertEqual(rc, update.EXIT_OK)
        self.assertIn("update complete", "\n".join(self.lines))

    def test_aborts_if_checkout_did_not_move_head(self):
        # git checkout returns 0 but HEAD is still the old sha — §4.6: the
        # return code is not proof.
        with mock.patch.object(gitdeploy, "is_git_checkout", return_value=True), \
             mock.patch.object(gitdeploy, "_git", side_effect=self._fake_git()), \
             mock.patch.object(gitdeploy, "observe",
                               side_effect=[_state(), _state(),
                                            _state(sha="a"*40, short_sha="aaaaaaa")]), \
             mock.patch.object(update, "_backend_pids", return_value={}), \
             mock.patch("sb3.killswitch.cmd_kill", return_value=0), \
             mock.patch("sb3.killswitch.cmd_resume", return_value=0):
            rc = update.cmd_update(execute=True, emit=self._emit)
        self.assertEqual(rc, update.EXIT_INVARIANT_VIOLATED)
        self.assertIn("expected", "\n".join(self.lines))

    def test_advance_uses_ff_only_merge_not_a_detaching_checkout(self):
        # HEAD must stay attached to the branch, so `git merge --ff-only` is
        # used, NOT `git checkout origin/<branch>` (which detaches).
        seen = []

        def g(root, *args, **kw):
            seen.append(args)
            if args[0] == "fetch":
                return 0, "", ""
            if args[:2] == ("rev-parse", "HEAD"):
                return 0, "a"*40, ""
            if args[0] == "rev-parse":
                return 0, "b"*40, ""
            return 0, "", ""

        with mock.patch.object(gitdeploy, "is_git_checkout", return_value=True), \
             mock.patch.object(gitdeploy, "_git", side_effect=g), \
             mock.patch.object(gitdeploy, "observe",
                               side_effect=[_state(), _state(),
                                            _state(sha="b"*40, short_sha="bbbbbbb")]), \
             mock.patch.object(update, "_backend_pids", return_value={}), \
             mock.patch("sb3.killswitch.cmd_kill", return_value=0), \
             mock.patch("sb3.killswitch.cmd_resume", return_value=0):
            update.cmd_update(execute=True, emit=self._emit)
        self.assertTrue(any(c[0] == "merge" and "--ff-only" in c for c in seen),
                        "advance must fast-forward the branch")
        self.assertFalse(any(c[0] == "checkout" for c in seen),
                         "advance must NOT detach HEAD via checkout origin/<branch>")

    def test_does_not_resume_if_kill_invariant_failed(self):
        with mock.patch.object(gitdeploy, "is_git_checkout", return_value=True), \
             mock.patch.object(gitdeploy, "_git", side_effect=self._fake_git()), \
             mock.patch.object(gitdeploy, "observe",
                               side_effect=[_state(), _state(),
                                            _state(sha="b"*40, short_sha="bbbbbbb")]), \
             mock.patch.object(update, "_backend_pids", return_value={}), \
             mock.patch("sb3.killswitch.cmd_kill", return_value=1), \
             mock.patch("sb3.killswitch.cmd_resume") as r:
            rc = update.cmd_update(execute=True, emit=self._emit)
        self.assertEqual(rc, 1)
        r.assert_not_called()


if __name__ == "__main__":
    unittest.main()
