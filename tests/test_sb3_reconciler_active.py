"""Phase 4.2 tests — the ACTIVE reconciler: actions, and every brake on them.

4.1's job was to prove the thing could not act. 4.2's job is to prove that when
it does act, it acts on exactly the five RECOVERABLE categories, through exactly
one REST base, at a bounded rate, and never on anything a human is holding.

The tests that matter most are not the happy paths — they are the ones that
assert the reconciler DOESN'T do something: doesn't act when disabled, doesn't
act on BENIGN, doesn't act on BROKEN, doesn't act on an untrustworthy reading,
doesn't act while quarantined, and cannot reach SDRTrunk.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from sb3 import backends
from sb3.profile import parse_profile
from sb3.reconciler import actions as A
from sb3.reconciler import classifier as C
from sb3.reconciler import config as CFG
from sb3.reconciler import safety as S
from sb3.reconciler.observer import Observer

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

VFO_PROFILE = {
    "name": "vfo.default", "role": "vfo", "mode": "hunt", "deviceset_index": 1,
    "device": {"hardware_id": "RTLSDR", "serial": "95339533",
               "center_freq_hz": 146620000, "sample_rate_hz": 2048000,
               "gain_tenths_db": 350, "agc": False, "dc_block": True},
    "channels": [{"freq_hz": 146520000, "title": "VFO", "demod": "NFM",
                  "rf_bw_hz": 12500, "squelch_db": -55, "volume": 3.0}],
    "audio_device": {"strategy": "system_default"},
    "copy_to_udp": {"address": "127.0.0.1", "port": 9998},
    "mount": "neptune-analog.mp3",
}

AIR_PROFILE = {
    "name": "air.test", "role": "air", "mode": "camp", "deviceset_index": 0,
    "device": {"hardware_id": "SDRplayV3", "serial": "2405265A60",
               "center_freq_hz": 118925000, "sample_rate_hz": 2048000},
    "channels": [
        {"freq_hz": 118400000, "title": "KA", "demod": "AM", "rf_bw_hz": 8000,
         "squelch_db": -100, "volume": 0.4, "keepalive": True},
        {"freq_hz": 119350000, "title": "Tower", "demod": "AM", "rf_bw_hz": 8000,
         "squelch_db": -55, "volume": 3.0},
    ],
    "audio_device": {"strategy": "system_default"},
    "copy_to_udp": {"address": "127.0.0.1", "port": 9998},
    "mount": "neptune-analog.mp3",
}


def _ctx(action, *, profile=VFO_PROFILE, dry_run=True, **kw):
    prof = parse_profile(profile)
    base = dict(role=prof.role, action=action, profile=prof,
                deviceset_index=prof.deviceset_index, mount=prof.mount,
                dry_run=dry_run, audio_name="System default device",
                audio_index=-1, udp_address="127.0.0.1", udp_port=9998)
    base.update(kw)
    return A.ActionContext(**base)


def _paths(res):
    return [(m, p) for m, p, _b in res.calls]


# ---------------------------------------------------------------------------
# per-action REST sequences
# ---------------------------------------------------------------------------

class TestActionSequences(unittest.TestCase):
    """dry_run maps to SDRangelClient(execute=False): identical code path,
    every call recorded instead of sent. So these assert the REAL sequence."""

    def test_unbound_device_posts_run(self):
        res = A.run_action(_ctx("unbound_device"), emit=lambda m: None)
        self.assertTrue(res.ok)
        self.assertIn(("POST", "/deviceset/1/device/run"), _paths(res))

    def test_mount_404_toggles_copy_to_udp_zero_then_one(self):
        res = A.run_action(_ctx("mount_404_with_healthy_backend"),
                           emit=lambda m: None)
        self.assertTrue(res.ok)
        patches = [b for m, p, b in res.calls
                   if p == "/audio/output/parameters" and b]
        # the 0→1 toggle is what actually starts SDRangel's sender thread
        self.assertEqual(patches[-2]["copyToUDP"], 0)
        self.assertEqual(patches[-1]["copyToUDP"], 1)
        self.assertEqual(patches[-1]["udpPort"], 9998)
        self.assertEqual(patches[-1]["index"], -1)

    def test_missing_channel_posts_channel_then_patches_settings(self):
        ctx = _ctx("missing_channel", missing_offsets=(-100000,))
        res = A.run_action(ctx, emit=lambda m: None)
        self.assertTrue(res.ok, res.detail)
        self.assertIn(("POST", "/deviceset/1/channel"), _paths(res))
        patch = [b for m, p, b in res.calls
                 if p.endswith("/settings") and m == "PATCH"][0]
        s = patch["NFMDemodSettings"]
        self.assertEqual(s["inputFrequencyOffset"], -100000)
        self.assertEqual(s["squelch"], -55)
        self.assertEqual(s["volume"], 3.0)

    def test_missing_channel_ignores_offsets_not_in_profile(self):
        ctx = _ctx("missing_channel", missing_offsets=(-999999,))
        res = A.run_action(ctx, emit=lambda m: None)
        self.assertFalse(res.ok)
        self.assertEqual(res.detail, "no_matching_channel_in_profile")

    def test_missing_keepalive_readds_the_keepalive_channel(self):
        ctx = _ctx("missing_keepalive", profile=AIR_PROFILE)
        res = A.run_action(ctx, emit=lambda m: None)
        self.assertTrue(res.ok, res.detail)
        patch = [b for m, p, b in res.calls
                 if p.endswith("/settings") and m == "PATCH"][0]
        s = patch["AMDemodSettings"]
        # the keepalive is the wide-open one that holds the mount up
        self.assertEqual(s["squelch"], -100)
        self.assertEqual(s["inputFrequencyOffset"], 118400000 - 118925000)

    def test_missing_keepalive_on_profile_without_one(self):
        res = A.run_action(_ctx("missing_keepalive"), emit=lambda m: None)
        self.assertFalse(res.ok)
        self.assertEqual(res.detail, "profile_has_no_keepalive")

    def test_phantom_deviceset_rebinds_and_reapplies(self):
        res = A.run_action(_ctx("phantom_deviceset"), emit=lambda m: None)
        paths = _paths(res)
        self.assertIn(("PUT", "/deviceset/1/device"), paths)
        self.assertIn(("POST", "/deviceset/1/device/run"), paths)
        self.assertTrue(any(m == "POST" and p == "/deviceset/1/channel"
                            for m, p in paths))

    def test_unknown_action_has_no_handler(self):
        res = A.run_action(_ctx("reboot_the_box"), emit=lambda m: None)
        self.assertFalse(res.ok)
        self.assertEqual(res.detail, "no_handler")

    def test_action_never_raises(self):
        # A crash inside an action must not kill the observer loop.
        # Patch the HANDLERS entry, not the module attribute: HANDLERS binds
        # the function objects at import time.
        def boom(ctx, *, emit):
            raise RuntimeError("boom")
        with mock.patch.dict(A.HANDLERS, {"unbound_device": boom}):
            res = A.run_action(_ctx("unbound_device"), emit=lambda m: None)
        self.assertFalse(res.ok)
        self.assertIn("exception=", res.detail)

    def test_handlers_match_configurable_categories(self):
        # A category can never be enabled in config without a handler behind it.
        self.assertEqual(set(A.HANDLERS), set(CFG.ACTIONABLE))


class TestIssueMapping(unittest.TestCase):
    def test_priority_is_causal_phantom_before_channels(self):
        self.assertEqual(
            A.action_for(["channel_missing", "phantom_deviceset"]),
            "phantom_deviceset")

    def test_not_running_before_mount(self):
        self.assertEqual(
            A.action_for(["mount_absent", "deviceset_not_running"]),
            "unbound_device")

    def test_serial_mismatch_is_deliberately_not_actionable(self):
        # Rebinding a radio unattended is the operation with the worst failure
        # mode on this fleet; it stays logged and unfixed.
        self.assertIsNone(A.action_for(["serial_mismatch"]))

    def test_no_issues_no_action(self):
        self.assertIsNone(A.action_for([]))


# ---------------------------------------------------------------------------
# BRAKE 1 — path allowlist (trunk protection at runtime)
# ---------------------------------------------------------------------------

class TestPathAuditor(unittest.TestCase):
    def test_allows_deviceset_and_audio(self):
        calls = [("POST", "/deviceset/1/device/run", None),
                 ("PATCH", "/audio/output/parameters", {}),
                 ("GET", "", None)]
        self.assertEqual(S.PathAuditor.audit(calls), [])

    def test_rejects_anything_else(self):
        for bad in ("/sdrtrunk/stop", "/../../etc/passwd", "/preset",
                    "http://127.0.0.1:8000/neptune-trunk.mp3"):
            v = S.PathAuditor.audit([("POST", bad, None)])
            self.assertTrue(v, f"{bad!r} should have been rejected")

    def test_rejects_a_client_pointed_off_sdrangel(self):
        v = S.PathAuditor.audit([], base="http://127.0.0.1:8000/")
        self.assertTrue(v)
        self.assertIn("base", v[0])

    def test_real_actions_stay_inside_the_allowlist(self):
        """Every shipped action, audited against what it actually calls."""
        for action in CFG.ACTIONABLE:
            ctx = _ctx(action, profile=AIR_PROFILE, missing_offsets=(-525000,))
            res = A.run_action(ctx, emit=lambda m: None)
            self.assertEqual(
                S.PathAuditor.audit(res.calls, res.base or S.EXPECTED_BASE), [],
                f"action {action} called outside the SDRangel allowlist")


# ---------------------------------------------------------------------------
# BRAKE 2 — backend PID guard
# ---------------------------------------------------------------------------

class TestBackendGuard(unittest.TestCase):
    def test_detects_a_moved_pid(self):
        g = S.BackendGuard()
        moved = g.compare({"sdrtrunk": "637", "icecast": "621"},
                          {"sdrtrunk": "999", "icecast": "621"})
        self.assertEqual(moved, ["sdrtrunk:637→999"])

    def test_detects_a_vanished_process(self):
        g = S.BackendGuard()
        self.assertEqual(g.compare({"icecast": "621"}, {"icecast": None}),
                         ["icecast:621→None"])

    def test_unchanged_is_clean(self):
        g = S.BackendGuard()
        self.assertEqual(g.compare({"sdrtrunk": "637"}, {"sdrtrunk": "637"}), [])

    def test_trip_pauses_and_expires(self):
        now = [1000.0]
        g = S.BackendGuard(pause_seconds=300, clock=lambda: now[0])
        self.assertFalse(g.paused())
        msg = g.trip(["sdrtrunk:637→999"])
        self.assertIn("EMERGENCY", msg)
        self.assertTrue(g.paused())
        now[0] += 299
        self.assertTrue(g.paused())
        now[0] += 2
        self.assertFalse(g.paused())

    def test_sdrangel_is_not_a_protected_process(self):
        # The reconciler drives SDRangel; a rebind may legitimately churn it.
        self.assertNotIn("sdrangel", backends.PROTECTED_PROCESSES)
        self.assertIn("sdrtrunk", backends.PROTECTED_PROCESSES)
        self.assertIn("icecast", backends.PROTECTED_PROCESSES)
        self.assertIn("sdrplay_apiservice", backends.PROTECTED_PROCESSES)


# ---------------------------------------------------------------------------
# BRAKE 3 — rate limiter
# ---------------------------------------------------------------------------

class TestRateLimiter(unittest.TestCase):
    def test_backoff_progression_30_60_120_240_capped(self):
        rl = S.RateLimiter(base=30, cap=240)
        self.assertEqual([rl.backoff_for(n) for n in (1, 2, 3, 4, 5, 6)],
                         [30, 60, 120, 240, 240, 240])

    def test_blocks_until_the_backoff_elapses(self):
        now = [0.0]
        rl = S.RateLimiter(base=30, cap=240, clock=lambda: now[0])
        self.assertTrue(rl.allowed("air"))
        rl.record_action("air")
        self.assertFalse(rl.allowed("air"))
        now[0] += 29
        self.assertFalse(rl.allowed("air"))
        now[0] += 2
        self.assertTrue(rl.allowed("air"))

    def test_alarms_and_stops_retrying_past_the_cap(self):
        now = [0.0]
        rl = S.RateLimiter(base=30, cap=240, clock=lambda: now[0])
        for _ in range(5):
            rl.record_action("air")
            now[0] += 1000
        self.assertTrue(rl.alarmed.get("air"))
        self.assertFalse(rl.allowed("air"), "an alarmed role must stop retrying")

    def test_a_clean_pass_resets_the_backoff(self):
        now = [0.0]
        rl = S.RateLimiter(base=30, cap=240, clock=lambda: now[0])
        rl.record_action("air")
        rl.record_action("air")
        self.assertEqual(rl.streak("air"), 2)
        rl.record_clean("air")
        self.assertEqual(rl.streak("air"), 0)
        self.assertTrue(rl.allowed("air"))

    def test_roles_are_limited_independently(self):
        now = [0.0]
        rl = S.RateLimiter(base=30, cap=240, clock=lambda: now[0])
        rl.record_action("air")
        self.assertFalse(rl.allowed("air"))
        self.assertTrue(rl.allowed("vfo"))


# ---------------------------------------------------------------------------
# BRAKE 4 — failure counter / quarantine
# ---------------------------------------------------------------------------

class TestFailureCounter(unittest.TestCase):
    def test_quarantines_on_the_third_consecutive_failure(self):
        fc = S.FailureCounter(threshold=3)
        self.assertFalse(fc.record_failure("air", "missing_channel"))
        self.assertFalse(fc.record_failure("air", "missing_channel"))
        self.assertFalse(fc.quarantined("air", "missing_channel"))
        self.assertTrue(fc.record_failure("air", "missing_channel"))
        self.assertTrue(fc.quarantined("air", "missing_channel"))

    def test_success_resets_the_streak(self):
        fc = S.FailureCounter(threshold=3)
        fc.record_failure("air", "x")
        fc.record_failure("air", "x")
        fc.record_success("air", "x")
        self.assertEqual(fc.failures("air", "x"), 0)
        self.assertFalse(fc.record_failure("air", "x"))

    def test_quarantine_is_per_role_and_action(self):
        fc = S.FailureCounter(threshold=1)
        fc.record_failure("air", "missing_channel")
        self.assertTrue(fc.quarantined("air", "missing_channel"))
        self.assertFalse(fc.quarantined("vfo", "missing_channel"))
        self.assertFalse(fc.quarantined("air", "unbound_device"))

    def test_release_clears_it(self):
        fc = S.FailureCounter(threshold=1)
        fc.record_failure("air", "x")
        self.assertTrue(fc.release("air", "x"))
        self.assertFalse(fc.quarantined("air", "x"))
        self.assertFalse(fc.release("air", "x"))

    def test_quarantine_survives_a_restart(self):
        """It must persist, or `resume-action` is a no-op and quarantine
        silently evaporates whenever the agent restarts."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "q.json"
            fc = S.FailureCounter(threshold=1, path=p)
            fc.record_failure("air", "missing_channel", "boom")
            self.assertTrue(p.is_file())
            fresh = S.FailureCounter(threshold=1, path=p)
            self.assertTrue(fresh.quarantined("air", "missing_channel"))
            fresh.release("air", "missing_channel")
            self.assertFalse(S.FailureCounter(threshold=1, path=p)
                             .quarantined("air", "missing_channel"))


# ---------------------------------------------------------------------------
# BRAKE 5 — broken-state pause + shared-tap dedupe
# ---------------------------------------------------------------------------

class TestBrokenStatePause(unittest.TestCase):
    def test_no_action_when_rest_unreachable(self):
        ok, why = S.readings_trustworthy(sdrangel_reachable=False, mount_status=200)
        self.assertFalse(ok)
        self.assertEqual(why, "sdrangel_rest_unreachable")

    def test_no_action_when_mount_status_indeterminate(self):
        ok, why = S.readings_trustworthy(sdrangel_reachable=True, mount_status=None)
        self.assertFalse(ok)
        self.assertEqual(why, "mount_status_indeterminate")

    def test_ok_when_both_readable(self):
        ok, _ = S.readings_trustworthy(sdrangel_reachable=True, mount_status=404)
        self.assertTrue(ok)


class TestSharedTapDedupe(unittest.TestCase):
    def test_two_roles_on_one_tap_yield_one_action(self):
        # Air and VFO share idx -1 → :9998. Both see the mount dark; the second
        # toggle would stop the sender the first just started.
        air = _ctx("mount_404_with_healthy_backend", profile=AIR_PROFILE)
        vfo = _ctx("mount_404_with_healthy_backend", profile=VFO_PROFILE)
        kept, dropped = S.dedupe_tap_actions(
            [("air", "mount_404_with_healthy_backend", air),
             ("vfo", "mount_404_with_healthy_backend", vfo)])
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, ["vfo"])

    def test_different_taps_both_survive(self):
        a = _ctx("mount_404_with_healthy_backend", profile=AIR_PROFILE)
        b = _ctx("mount_404_with_healthy_backend", profile=VFO_PROFILE,
                 udp_port=9999, audio_index=0)
        kept, dropped = S.dedupe_tap_actions(
            [("air", "mount_404_with_healthy_backend", a),
             ("ground", "mount_404_with_healthy_backend", b)])
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, [])

    def test_other_actions_are_never_deduped(self):
        a = _ctx("missing_channel")
        kept, dropped = S.dedupe_tap_actions(
            [("air", "missing_channel", a), ("vfo", "missing_channel", a)])
        self.assertEqual(len(kept), 2)


# ---------------------------------------------------------------------------
# config — the three off-switches
# ---------------------------------------------------------------------------

class TestConfig(unittest.TestCase):
    def test_ships_disabled(self):
        cfg = CFG.ReconcilerConfig({})
        self.assertFalse(cfg.enabled)
        self.assertFalse(cfg.may_act())
        self.assertEqual(cfg.describe(), "DISABLED")

    def test_missing_config_file_is_disabled(self):
        cfg = CFG.load(Path("/nonexistent/reconciler.json"))
        self.assertFalse(cfg.may_act())

    def test_malformed_config_fails_closed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text("{ this is not json")
            self.assertFalse(CFG.load(p).may_act())

    def test_sentinel_beats_enabled(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            sent = Path(d) / ".sb3-reconciler-passive"
            sent.touch()
            cfg = CFG.ReconcilerConfig({"enabled": True}, sentinel=sent)
            self.assertTrue(cfg.enabled)
            self.assertFalse(cfg.may_act(), "the sentinel must win over config")
            self.assertIn("PASSIVE", cfg.describe())

    def test_unknown_category_is_never_actionable(self):
        cfg = CFG.ReconcilerConfig(
            {"enabled": True, "actions": {"reboot_the_box": True}})
        self.assertFalse(cfg.action_enabled("reboot_the_box"))

    def test_dry_run_is_reported_distinctly(self):
        cfg = CFG.ReconcilerConfig({"enabled": True, "dry_run": True})
        self.assertTrue(cfg.may_act())
        self.assertEqual(cfg.describe(), "DRY-RUN")

    def test_save_is_atomic_and_roundtrips(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "reconciler.json"
            CFG.save({"enabled": True, "actions": {"unbound_device": True}}, p)
            self.assertTrue(p.is_file())
            self.assertTrue(CFG.load(p).enabled)
            # no temp turds left behind
            self.assertEqual([f.name for f in p.parent.iterdir()], [p.name])

    def test_partial_config_keeps_defaults(self):
        cfg = CFG.ReconcilerConfig({"enabled": True})
        self.assertEqual(cfg.quarantine_threshold, 3)
        self.assertEqual(cfg.max_backoff, 240)


# ---------------------------------------------------------------------------
# observer act_phase — the brakes wired together
# ---------------------------------------------------------------------------

class _FakeState:
    def __init__(self, profiles, killed=False):
        self._p, self._k = profiles, killed

    def read_loaded_profiles(self):
        return self._p

    def is_killed(self):
        return self._k


VFO_REC = {"name": "vfo.default", "role": "vfo", "mode": "hunt",
           "deviceset_index": 1, "serial": "95339533",
           "center_freq_hz": 146620000, "mount": "neptune-analog.mp3",
           "channel_freqs": [146520000]}


def _obs_with(rc, tmp):
    o = Observer(config={}, state=_FakeState({"vfo": VFO_REC}),
                 log_path=Path(tmp) / "r.log", rc=rc)
    o.failures = S.FailureCounter(threshold=rc.quarantine_threshold)
    return o


def _recoverable(**kw):
    base = dict(role="vfo", profile_name="vfo.default", deviceset_index=1,
                mode="hunt", want_serial="95339533", want_center_hz=146620000,
                want_mount="neptune-analog.mp3", expected_channels=[],
                sdrangel_reachable=True, ds_present=True, ds_phantom=True,
                ds_hw_type="AaroniaRTSA", ds_serial=None, ds_state="idle",
                ds_center_hz=1450000, live_channels=[], mount_status=404,
                mount_present=False)
    base.update(kw)
    return C.RoleObservation(**base)


class TestActPhase(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.no_pid_move = mock.patch.object(
            backends, "process_pids", return_value={"sdrtrunk": "637"})

    def _run(self, rc, cls=None, obs=None):
        o = _obs_with(rc, self.tmp)
        obs = obs or _recoverable()
        cls = cls or C.classify_role(obs)
        with self.no_pid_move:
            return o, o.act_phase("T", [("vfo", VFO_REC, obs, cls)], True)

    def test_disabled_takes_no_action(self):
        _, lines = self._run(CFG.ReconcilerConfig({"enabled": False}))
        self.assertEqual(lines, [])

    def test_sentinel_takes_no_action(self):
        sent = Path(self.tmp) / "passive"
        sent.touch()
        rc = CFG.ReconcilerConfig({"enabled": True}, sentinel=sent)
        _, lines = self._run(rc)
        self.assertEqual(lines, [])

    def test_benign_is_left_alone(self):
        rc = CFG.ReconcilerConfig({"enabled": True, "dry_run": True})
        benign = C.Classification(C.BENIGN, [], ["ch0.squelch:-55→-42"], {})
        o, lines = self._run(rc, cls=benign)
        self.assertEqual([l for l in lines if "action=" in l], [])
        self.assertEqual(o.actions_taken, 0)

    def test_broken_is_never_actioned(self):
        rc = CFG.ReconcilerConfig({"enabled": True, "dry_run": True})
        broken = C.Classification(C.BROKEN, ["sdrangel_rest_down"], [], {})
        o, lines = self._run(rc, cls=broken)
        self.assertEqual(o.actions_taken, 0)

    def test_recoverable_acts_in_dry_run(self):
        rc = CFG.ReconcilerConfig({"enabled": True, "dry_run": True})
        o, lines = self._run(rc)
        act = [l for l in lines if "scope=action" in l and "mode=DRYRUN" in l]
        self.assertTrue(act, lines)
        self.assertIn("action=phantom_deviceset", act[0])
        self.assertEqual(o.actions_taken, 1)

    def test_disabled_category_is_skipped(self):
        rc = CFG.ReconcilerConfig({"enabled": True, "dry_run": True,
                                   "actions": {"phantom_deviceset": False}})
        o, lines = self._run(rc)
        self.assertTrue(any("reason=action_disabled" in l for l in lines))
        self.assertEqual(o.actions_taken, 0)

    def test_untrustworthy_reading_skips(self):
        rc = CFG.ReconcilerConfig({"enabled": True, "dry_run": True})
        obs = _recoverable(mount_status=None)
        o, lines = self._run(rc, obs=obs)
        self.assertTrue(any("mount_status_indeterminate" in l for l in lines))
        self.assertEqual(o.actions_taken, 0)

    def test_quarantined_pair_is_skipped(self):
        rc = CFG.ReconcilerConfig({"enabled": True, "dry_run": True})
        o = _obs_with(rc, self.tmp)
        o.failures.record_failure("vfo", "phantom_deviceset")
        o.failures.record_failure("vfo", "phantom_deviceset")
        o.failures.record_failure("vfo", "phantom_deviceset")
        obs = _recoverable()
        with self.no_pid_move:
            lines = o.act_phase("T", [("vfo", VFO_REC, obs,
                                       C.classify_role(obs))], True)
        self.assertTrue(any("reason=quarantined" in l for l in lines))
        self.assertEqual(o.actions_taken, 0)

    def test_rate_limit_blocks_the_second_consecutive_pass(self):
        rc = CFG.ReconcilerConfig({"enabled": True, "dry_run": True})
        o = _obs_with(rc, self.tmp)
        obs = _recoverable()
        cls = C.classify_role(obs)
        with self.no_pid_move:
            o.act_phase("T", [("vfo", VFO_REC, obs, cls)], True)
            lines2 = o.act_phase("T", [("vfo", VFO_REC, obs, cls)], True)
        self.assertTrue(any("reason=rate_limited" in l for l in lines2), lines2)
        self.assertEqual(o.actions_taken, 1)

    def test_backend_pid_move_triggers_emergency_and_pauses(self):
        rc = CFG.ReconcilerConfig({"enabled": True, "dry_run": True})
        o = _obs_with(rc, self.tmp)
        obs = _recoverable()
        cls = C.classify_role(obs)
        # SDRTrunk moves during the action — the thing that must never happen.
        with mock.patch.object(backends, "process_pids",
                               side_effect=[{"sdrtrunk": "637"},
                                            {"sdrtrunk": "999"}]):
            lines = o.act_phase("T", [("vfo", VFO_REC, obs, cls)], True)
        self.assertTrue(any("state=EMERGENCY" in l for l in lines), lines)
        self.assertTrue(any("sdrtrunk:637→999" in l for l in lines))
        self.assertTrue(o.guard.paused())
        # and the pause blocks the next pass entirely
        with self.no_pid_move:
            nxt = o.act_phase("T", [("vfo", VFO_REC, obs, cls)], True)
        self.assertTrue(any("reason=emergency_pause" in l for l in nxt))

    def test_emergency_is_not_counted_as_an_ordinary_failure(self):
        rc = CFG.ReconcilerConfig({"enabled": True, "dry_run": True})
        o = _obs_with(rc, self.tmp)
        obs = _recoverable()
        with mock.patch.object(backends, "process_pids",
                               side_effect=[{"sdrtrunk": "637"},
                                            {"sdrtrunk": "999"}]):
            o.act_phase("T", [("vfo", VFO_REC, obs, C.classify_role(obs))], True)
        self.assertEqual(o.failures.failures("vfo", "phantom_deviceset"), 0)

    def test_clean_role_resets_backoff(self):
        rc = CFG.ReconcilerConfig({"enabled": True, "dry_run": True})
        o = _obs_with(rc, self.tmp)
        obs = _recoverable()
        with self.no_pid_move:
            o.act_phase("T", [("vfo", VFO_REC, obs, C.classify_role(obs))], True)
            self.assertEqual(o.limiter.streak("vfo"), 1)
            clean = C.Classification(C.CLEAN, [], [], {})
            o.act_phase("T", [("vfo", VFO_REC, obs, clean)], True)
        self.assertEqual(o.limiter.streak("vfo"), 0)


# ---------------------------------------------------------------------------
# the retune trap — the single worst thing an active reconciler could do
# ---------------------------------------------------------------------------

class TestRetuneIsNeverUndone(unittest.TestCase):
    """A user retuning a channel must never be read as a missing channel.

    If it were, Phase 4.2 would helpfully put the channel back where the
    profile says — undoing the tuning the user just did, every 30 seconds.
    """

    def _obs(self, live_offsets, expected):
        return C.RoleObservation(
            role="vfo", profile_name="p", deviceset_index=1, mode="hunt",
            want_serial="95339533", want_center_hz=146620000,
            want_mount="m", expected_channels=expected,
            sdrangel_reachable=True, ds_present=True, ds_phantom=False,
            ds_hw_type="RTLSDR", ds_serial="95339533", ds_state="running",
            ds_center_hz=146620000,
            live_channels=[C.LiveChannel(i, off, -55.0, 3.0)
                           for i, off in enumerate(live_offsets)],
            mount_status=200, mount_present=True)

    def test_retuned_channel_is_benign_with_no_missing_offsets(self):
        exp = [C.ExpectedChannel(-100000, -55.0, 3.0, 12500)]
        cls = C.classify_role(self._obs([-125000], exp))
        self.assertEqual(cls.state, C.BENIGN)
        self.assertEqual(cls.missing_offsets, ())
        self.assertIsNone(A.action_for(cls.issues))

    def test_genuinely_missing_channel_reports_its_offset(self):
        exp = [C.ExpectedChannel(-100000, -55.0, 3.0, 12500),
               C.ExpectedChannel(-50000, -55.0, 3.0, 12500)]
        cls = C.classify_role(self._obs([-100000], exp))
        self.assertEqual(cls.state, C.RECOVERABLE)
        self.assertEqual(cls.missing_offsets, (-50000,))
        self.assertEqual(A.action_for(cls.issues), "missing_channel")


if __name__ == "__main__":
    unittest.main()
