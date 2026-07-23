"""Phase 4.1 tests — the passive reconciler: classification, ownership, passivity.

The most important test in this file is TestPassivity: it proves, structurally,
that the reconciler package cannot write to a backend.  Phase 4.1's entire value
is that it observes a live box for a night without touching it, so "it is
passive" has to be an assertion, not a claim in a docstring.
"""

from __future__ import annotations

import ast
import plistlib
import unittest
from pathlib import Path
from unittest import mock

from sb3 import backends, ownership
from sb3.reconciler import classifier as C
from sb3.reconciler.observer import Observer, load_config

REPO = Path(__file__).resolve().parent.parent
RECONCILER_DIR = REPO / "sb3" / "reconciler"


# ---------------------------------------------------------------------------
# helpers — build observations concisely
# ---------------------------------------------------------------------------

def _ch(offset=-100000, sq=-55.0, vol=3.0, bw=12500, ka=False):
    return C.ExpectedChannel(offset_hz=offset, squelch_db=sq, volume=vol,
                             rf_bw_hz=bw, keepalive=ka)


def _live(index=0, offset=-100000, sq=-55.0, vol=3.0, bw=12500):
    return C.LiveChannel(index=index, offset_hz=offset, squelch_db=sq,
                         volume=vol, rf_bw_hz=bw)


def _obs(**kw):
    base = dict(
        role="vfo", profile_name="vfo.default", deviceset_index=1, mode="hunt",
        want_serial="95339533", want_center_hz=146620000,
        want_mount="neptune-analog.mp3",
        expected_channels=[_ch()],
        sdrangel_reachable=True, ds_present=True, ds_phantom=False,
        ds_hw_type="RTLSDR", ds_serial="95339533", ds_state="running",
        ds_center_hz=146620000,
        live_channels=[_live()],
        mount_status=200, mount_present=True,
    )
    base.update(kw)
    return C.RoleObservation(**base)


# ---------------------------------------------------------------------------
# CLEAN + BENIGN — the user is driving
# ---------------------------------------------------------------------------

class TestClean(unittest.TestCase):
    def test_matching_state_is_clean(self):
        cls = C.classify_role(_obs())
        self.assertEqual(cls.state, C.CLEAN)
        self.assertEqual(cls.issues, [])
        self.assertEqual(cls.drifts, [])

    def test_float32_roundtrip_is_not_drift(self):
        # SDRangel returns 0.4 as 0.4000000059604645. Comparing with == would
        # report BENIGN drift forever on a channel nobody touched.
        cls = C.classify_role(_obs(
            expected_channels=[_ch(sq=-100.0, vol=0.4)],
            live_channels=[_live(sq=-100.0, vol=0.4000000059604645)]))
        self.assertEqual(cls.state, C.CLEAN, cls.drifts)


class TestBenign(unittest.TestCase):
    def test_squelch_nudge_is_benign(self):
        cls = C.classify_role(_obs(live_channels=[_live(sq=-42.0)]))
        self.assertEqual(cls.state, C.BENIGN)
        self.assertIn("ch0.squelch:-55→-42", cls.drifts)
        self.assertEqual(cls.issues, [])

    def test_volume_change_within_order_of_magnitude_is_benign(self):
        cls = C.classify_role(_obs(live_channels=[_live(vol=1.5)]))
        self.assertEqual(cls.state, C.BENIGN)
        self.assertIn("ch0.volume:3→1.5", cls.drifts)

    def test_user_retuned_vfo_center_is_benign(self):
        # `tune target=vfo` moves the LO by design; the recorded centre goes
        # stale immediately and that is intent, not drift to be corrected.
        cls = C.classify_role(_obs(ds_center_hz=145100000))
        self.assertEqual(cls.state, C.BENIGN)
        self.assertTrue(any(d.startswith("center_mhz:") for d in cls.drifts))

    def test_channel_offset_move_is_benign(self):
        cls = C.classify_role(_obs(live_channels=[_live(offset=-125000)]))
        self.assertEqual(cls.state, C.BENIGN)
        self.assertIn("ch0.offset:-100000→-125000", cls.drifts)

    def test_extra_channel_is_benign_not_missing(self):
        cls = C.classify_role(_obs(live_channels=[_live(), _live(index=1)]))
        self.assertEqual(cls.state, C.BENIGN)
        self.assertIn("channels:1→2", cls.drifts)


# ---------------------------------------------------------------------------
# RECOVERABLE — the profile is not live
# ---------------------------------------------------------------------------

class TestRecoverable(unittest.TestCase):
    def test_phantom_deviceset(self):
        cls = C.classify_role(_obs(ds_phantom=True, ds_hw_type="AaroniaRTSA",
                                   ds_serial=None))
        self.assertEqual(cls.state, C.RECOVERABLE)
        self.assertIn("phantom_deviceset", cls.issues)
        self.assertEqual(cls.facts["current_hw"], "AaroniaRTSA")

    def test_deviceset_missing(self):
        cls = C.classify_role(_obs(ds_present=False, ds_serial=None,
                                   ds_state=None, ds_center_hz=None,
                                   live_channels=[]))
        self.assertEqual(cls.state, C.RECOVERABLE)
        self.assertIn("deviceset_missing", cls.issues)

    def test_serial_mismatch_is_structural(self):
        cls = C.classify_role(_obs(ds_serial="61108285"))
        self.assertEqual(cls.state, C.RECOVERABLE)
        self.assertIn("serial_mismatch", cls.issues)

    def test_deviceset_not_running(self):
        cls = C.classify_role(_obs(ds_state="idle"))
        self.assertEqual(cls.state, C.RECOVERABLE)
        self.assertIn("deviceset_not_running", cls.issues)

    def test_channel_missing(self):
        cls = C.classify_role(_obs(expected_channels=[_ch(), _ch(offset=-50000)],
                                   live_channels=[_live()]))
        self.assertEqual(cls.state, C.RECOVERABLE)
        self.assertIn("channel_missing", cls.issues)
        self.assertEqual(cls.facts["channels"], "1/2")

    def test_mount_absent_behind_healthy_deviceset(self):
        # The silent-outage shape: everything reports healthy, no audio exists.
        cls = C.classify_role(_obs(mount_status=404, mount_present=False))
        self.assertEqual(cls.state, C.RECOVERABLE)
        self.assertIn("mount_absent", cls.issues)

    def test_squelch_dragged_below_noise_floor_is_not_benign(self):
        # -55 -> -95 is 40 dB, past SQUELCH_BENIGN_MAX_DELTA_DB: the observed
        # "squelch -80 flapping" failure, not ordinary tuning.
        cls = C.classify_role(_obs(live_channels=[_live(sq=-95.0)]))
        self.assertEqual(cls.state, C.RECOVERABLE)
        self.assertIn("ch0_squelch_out_of_band", cls.issues)

    def test_keepalive_squelch_raised(self):
        cls = C.classify_role(_obs(
            mode="camp",
            expected_channels=[_ch(sq=-100.0, vol=0.4, ka=True)],
            live_channels=[_live(sq=-30.0, vol=0.4)]))
        self.assertEqual(cls.state, C.RECOVERABLE)
        self.assertIn("ch0_keepalive_squelch_raised", cls.issues)

    def test_volume_zeroed_is_not_a_taste_change(self):
        cls = C.classify_role(_obs(live_channels=[_live(vol=0.0)]))
        self.assertEqual(cls.state, C.RECOVERABLE)
        self.assertIn("ch0_volume_zero", cls.issues)


# ---------------------------------------------------------------------------
# BROKEN — the backend itself is not answering
# ---------------------------------------------------------------------------

class TestBroken(unittest.TestCase):
    def test_sdrangel_rest_down_short_circuits(self):
        # With REST down every other read is empty; deriving "phantom" from an
        # empty read would be a guess dressed as a fact.
        cls = C.classify_role(_obs(sdrangel_reachable=False, ds_present=False,
                                   live_channels=[]))
        self.assertEqual(cls.state, C.BROKEN)
        self.assertEqual(cls.issues, ["sdrangel_rest_down"])

    def test_all_mounts_down(self):
        cls = C.classify_system(sdrangel_reachable=True,
                                mounts_present=[False, False, False])
        self.assertEqual(cls.state, C.BROKEN)
        self.assertIn("all_mounts_down", cls.issues)

    def test_backend_pid_churn(self):
        cls = C.classify_system(sdrangel_reachable=True, mounts_present=[True],
                                backend_pids_changed=True)
        self.assertEqual(cls.state, C.BROKEN)
        self.assertIn("backend_pid_churn", cls.issues)

    def test_missing_backend_agent(self):
        cls = C.classify_system(sdrangel_reachable=True, mounts_present=[True],
                                missing_backends=["com.scannerproject.icecast"])
        self.assertEqual(cls.state, C.BROKEN)
        self.assertIn("backend_agent_missing", cls.issues)

    def test_system_clean_when_all_well(self):
        cls = C.classify_system(sdrangel_reachable=True, mounts_present=[True, True])
        self.assertEqual(cls.state, C.CLEAN)


class TestSeverityPrecedence(unittest.TestCase):
    def test_most_severe_finding_wins(self):
        # A pass that is simultaneously "user retuned" and "DS is phantom" is
        # not a BENIGN pass.
        cls = C.classify_role(_obs(ds_phantom=True, ds_hw_type="AaroniaRTSA",
                                   ds_center_hz=145100000))
        self.assertEqual(cls.state, C.RECOVERABLE)

    def test_severity_order(self):
        self.assertLess(C.SEVERITY[C.CLEAN], C.SEVERITY[C.BENIGN])
        self.assertLess(C.SEVERITY[C.BENIGN], C.SEVERITY[C.RECOVERABLE])
        self.assertLess(C.SEVERITY[C.RECOVERABLE], C.SEVERITY[C.BROKEN])


# ---------------------------------------------------------------------------
# digital role (SDRTrunk)
# ---------------------------------------------------------------------------

class TestDigital(unittest.TestCase):
    def test_running_and_live_is_clean(self):
        cls = C.classify_digital(C.DigitalObservation(
            profile_name="sdrtrunk", running=True, mount_status=200,
            mount_present=True, connected=True))
        self.assertEqual(cls.state, C.CLEAN)

    def test_not_running_is_broken(self):
        cls = C.classify_digital(C.DigitalObservation(
            profile_name="sdrtrunk", running=False, mount_status=404,
            mount_present=False))
        self.assertEqual(cls.state, C.BROKEN)
        self.assertIn("sdrtrunk_not_running", cls.issues)

    def test_running_but_mount_dark_is_recoverable(self):
        cls = C.classify_digital(C.DigitalObservation(
            profile_name="sdrtrunk", running=True, mount_status=404,
            mount_present=False, connected=True))
        self.assertEqual(cls.state, C.RECOVERABLE)
        self.assertIn("mount_absent", cls.issues)

    def test_unknown_broadcaster_status_is_not_a_fault(self):
        # None = the log tail showed no broadcaster line; that is "unknown",
        # not "disconnected".
        cls = C.classify_digital(C.DigitalObservation(
            profile_name="sdrtrunk", running=True, mount_status=200,
            mount_present=True, connected=None))
        self.assertEqual(cls.state, C.CLEAN)


# ---------------------------------------------------------------------------
# log line format
# ---------------------------------------------------------------------------

class TestFormatLine(unittest.TestCase):
    def test_clean_line_shape(self):
        line = C.format_line("2026-07-22T04:32:00Z",
                             C.Classification(C.CLEAN, [], [], {}),
                             role="air", profile="air.airband.nashville.rsp1b",
                             ds=0)
        self.assertEqual(
            line,
            "ts=2026-07-22T04:32:00Z profile=air.airband.nashville.rsp1b "
            "role=air ds=0 state=CLEAN")

    def test_benign_line_carries_drift(self):
        cls = C.classify_role(_obs(live_channels=[_live(sq=-42.0)]))
        line = C.format_line("2026-07-22T04:32:30Z", cls, role="vfo",
                             profile="vfo.default", ds=1)
        self.assertIn("state=BENIGN", line)
        self.assertIn("drift=[ch0.squelch:-55→-42]", line)

    def test_recoverable_line_carries_issue_and_facts(self):
        cls = C.classify_role(_obs(ds_phantom=True, ds_hw_type="AaroniaRTSA"))
        line = C.format_line("2026-07-22T04:33:00Z", cls, role="air",
                             profile="air.airband.nashville.rsp1b", ds=0)
        self.assertIn("state=RECOVERABLE", line)
        self.assertIn("issue=phantom_deviceset", line)
        self.assertIn("current_hw=AaroniaRTSA", line)

    def test_line_is_single_line_and_greppable(self):
        cls = C.classify_role(_obs(live_channels=[_live(sq=-42.0, vol=1.0)]))
        line = C.format_line("T", cls, role="vfo", profile="p", ds=1)
        self.assertNotIn("\n", line)

    def test_no_duplicate_keys_in_any_line(self):
        """A repeated key (ds=0 … ds=0) makes the line ambiguous to parse.

        Checked across every category, because the duplicate came from a
        classifier adding a fact format_line already emits.
        """
        cases = [
            _obs(),                                                  # CLEAN
            _obs(live_channels=[_live(sq=-42.0)]),                   # BENIGN
            _obs(ds_phantom=True, ds_hw_type="AaroniaRTSA"),         # RECOVERABLE
            _obs(sdrangel_reachable=False, ds_present=False,
                 live_channels=[]),                                  # BROKEN
            _obs(mount_status=404, mount_present=False),             # mount
            _obs(ds_serial="61108285"),                              # serial
        ]
        for obs in cases:
            line = C.format_line("T", C.classify_role(obs), role=obs.role,
                                 profile=obs.profile_name,
                                 ds=obs.deviceset_index)
            keys = [tok.split("=", 1)[0] for tok in line.split(" ") if "=" in tok
                    and not tok.startswith("[")]
            dupes = {k for k in keys if keys.count(k) > 1}
            self.assertFalse(dupes, f"duplicate key(s) {dupes} in: {line}")


# ---------------------------------------------------------------------------
# ownership — kill/resume must manage the reconciler
# ---------------------------------------------------------------------------

LABEL = "com.scannerproject.sb3-reconciler"


class TestOwnership(unittest.TestCase):
    def test_classified_as_sb3_layer(self):
        self.assertEqual(ownership.classify(LABEL), "sb3")

    def test_is_a_managed_agent(self):
        self.assertIn(LABEL, ownership.MANAGED_AGENTS)
        self.assertEqual(ownership.MANAGED_AGENTS[LABEL], "sb3.reconciler")

    def test_kill_stops_it(self):
        self.assertIn(LABEL, ownership.KILL_ORDER)
        seq = ownership.kill_sequence([LABEL, "com.scannerproject.sb3-broker"])
        self.assertIn(LABEL, seq)

    def test_killed_before_the_broker(self):
        # Brokers are LAST (§4.3): killing one before its children yanks the
        # lease socket out from under a live child.
        order = list(ownership.KILL_ORDER)
        self.assertLess(order.index(LABEL),
                        order.index("com.scannerproject.sb3-broker"))

    def test_resume_brings_it_back_after_the_layer_it_observes(self):
        # resume walks KILL_ORDER reversed, so an observer stopped early comes
        # back late — it should not be classifying a half-restored layer.
        resume_order = [l for l in reversed(ownership.KILL_ORDER)
                        if l in ownership.MANAGED_AGENTS]
        self.assertIn(LABEL, resume_order)
        self.assertLess(resume_order.index("com.scannerproject.sb3-broker"),
                        resume_order.index(LABEL))
        self.assertLess(resume_order.index("com.scannerproject.sb3-ui"),
                        resume_order.index(LABEL))

    def test_never_in_the_backend_set(self):
        self.assertNotIn(LABEL, ownership.BACKEND)
        ownership.assert_disjoint()

    def test_plist_template_exists_and_parses(self):
        tmpl = REPO / ownership.PLIST_TEMPLATE_DIR / f"{LABEL}.plist"
        self.assertTrue(tmpl.is_file(), f"missing template {tmpl}")
        rendered = (tmpl.read_text()
                    .replace("{{PYTHON}}", "/usr/bin/python3")
                    .replace("{{REPO_DIR}}", "/repo")
                    .replace("{{HOME}}", "/home"))
        self.assertNotIn("{{", rendered)
        parsed = plistlib.loads(rendered.encode())
        self.assertEqual(parsed["Label"], LABEL)
        self.assertEqual(parsed["ProgramArguments"][1:],
                         ["-m", "sb3.reconciler"])
        # KeepAlive must respawn on CRASH only, or bootout becomes a fight.
        self.assertEqual(parsed["KeepAlive"], {"SuccessfulExit": False})


# ---------------------------------------------------------------------------
# PASSIVITY — the load-bearing test
# ---------------------------------------------------------------------------

def _code_without_docstrings(path: Path) -> str:
    """Source with comments and docstrings removed, so the scan sees only code.

    Comments and docstrings in this package legitimately NAME the write verbs
    (explaining that they are forbidden). Scanning raw text would therefore
    flag the very documentation of the rule. Round-tripping through ast drops
    comments, and docstrings are stripped explicitly.
    """
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", [])
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


class TestPassivity(unittest.TestCase):
    """Phase 4.1 observes and does not act. Proven structurally, not promised."""

    WRITE_VERBS = ("PATCH", "POST", "PUT", "DELETE")
    WRITE_ENTRYPOINTS = (
        "SDRangelClient",       # the writing REST client
        "patch_device", "patch_channel", "set_copy_to_udp",
        "translator.apply", "urlopen", "Request",
    )

    def _sources(self):
        files = sorted(RECONCILER_DIR.glob("*.py"))
        self.assertTrue(files, "no reconciler sources found")
        return files

    def test_no_http_write_verbs_in_code(self):
        for path in self._sources():
            code = _code_without_docstrings(path)
            for verb in self.WRITE_VERBS:
                self.assertNotIn(
                    verb, code,
                    f"{path.name} contains HTTP write verb {verb!r} — the "
                    f"Phase 4.1 reconciler must be observe-only")

    def test_no_writing_entrypoints(self):
        for path in self._sources():
            code = _code_without_docstrings(path)
            for name in self.WRITE_ENTRYPOINTS:
                self.assertNotIn(
                    name, code,
                    f"{path.name} references {name!r} — the reconciler must "
                    f"read only through sb3.backends")

    def test_backends_module_itself_has_no_write_verbs(self):
        # The reconciler reads exclusively through sb3.backends, so that
        # module's read-only contract is part of this guarantee.
        code = _code_without_docstrings(REPO / "sb3" / "backends.py")
        for verb in ("PATCH", "POST", "PUT", "DELETE"):
            self.assertNotIn(verb, code,
                             f"sb3.backends gained write verb {verb!r}")

    def test_imports_only_read_only_modules(self):
        forbidden = {"sb3.translator", "translator", "sdrangel"}
        for path in self._sources():
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    self.assertNotIn(node.module.split(".")[-1], forbidden,
                                     f"{path.name} imports {node.module}")
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        self.assertNotIn(a.name.split(".")[-1], forbidden,
                                         f"{path.name} imports {a.name}")


# ---------------------------------------------------------------------------
# observer — one pass, driven with fakes
# ---------------------------------------------------------------------------

class _FakeState:
    def __init__(self, profiles, killed=False):
        self._p = profiles
        self._killed = killed

    def read_loaded_profiles(self):
        return self._p

    def is_killed(self):
        return self._killed


AIR = {"name": "air.airband.nashville.rsp1b", "role": "air", "mode": "camp",
       "deviceset_index": 0, "serial": "2405265A60",
       "center_freq_hz": 118925000, "mount": "neptune-analog.mp3",
       "channel_freqs": [118400000]}
VFO = {"name": "vfo.default", "role": "vfo", "mode": "hunt",
       "deviceset_index": 1, "serial": "95339533",
       "center_freq_hz": 146620000, "mount": "neptune-analog.mp3",
       "channel_freqs": [146520000]}


def _patch_backends(devicesets, *, mount=200, loaded=(), channels=(),
                    settings=None):
    settings = settings or {}
    return (
        mock.patch.object(backends, "launchctl_loaded", return_value=list(loaded)),
        mock.patch.object(backends, "sdrangel_devicesets", return_value=devicesets),
        mock.patch.object(backends, "sdrangel_channels", return_value=list(channels)),
        mock.patch.object(backends, "sdrangel_channel_settings",
                          side_effect=lambda d, c, **kw: settings.get((d, c))),
        mock.patch.object(backends, "mount_state",
                          side_effect=lambda m, **kw: backends.MountState(
                              m, mount, 200 <= (mount or 0) < 300)),
    )


class TestObserverPass(unittest.TestCase):
    def _run_once(self, obs, patches):
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        return obs.classify_once()

    def test_killed_sentinel_short_circuits(self):
        obs = Observer(config={}, state=_FakeState({"vfo": VFO}, killed=True),
                       log_path=Path("/tmp/x.log"))
        lines = obs.classify_once()
        self.assertEqual(len(lines), 1)
        self.assertIn("sb3_killed", lines[0])
        self.assertIn("scope=system", lines[0])

    def test_pass_emits_a_line_per_role_plus_digital(self):
        ds = [backends.DevicesetState(0, "SDRplayV3", "2405265A60", "running",
                                      118925000, []),
              backends.DevicesetState(1, "RTLSDR", "95339533", "running",
                                      146620000, [])]
        obs = Observer(config={}, state=_FakeState({"air": AIR, "vfo": VFO}),
                       log_path=Path("/tmp/x.log"))
        lines = self._run_once(obs, _patch_backends(
            ds, loaded=["com.scannerproject.sdrtrunk"]))
        roles = [l for l in lines if "role=" in l]
        self.assertTrue(any("role=air" in l for l in roles))
        self.assertTrue(any("role=vfo" in l for l in roles))
        self.assertTrue(any("role=digital" in l for l in roles))

    def test_phantom_deviceset_surfaces_in_the_line(self):
        ds = [backends.DevicesetState(1, "AaroniaRTSA", None, "idle", 1450000, [])]
        obs = Observer(config={}, state=_FakeState({"vfo": VFO}),
                       log_path=Path("/tmp/x.log"))
        lines = self._run_once(obs, _patch_backends(ds))
        vfo_line = next(l for l in lines if "role=vfo" in l)
        self.assertIn("state=RECOVERABLE", vfo_line)
        self.assertIn("phantom_deviceset", vfo_line)

    def test_sdrangel_unreachable_is_broken(self):
        obs = Observer(config={}, state=_FakeState({"vfo": VFO}),
                       log_path=Path("/tmp/x.log"))
        lines = self._run_once(obs, _patch_backends([], mount=404))
        vfo_line = next(l for l in lines if "role=vfo" in l)
        self.assertIn("state=BROKEN", vfo_line)
        self.assertIn("sdrangel_rest_down", vfo_line)

    def test_backend_churn_detected_across_passes(self):
        ds = [backends.DevicesetState(1, "RTLSDR", "95339533", "running",
                                      146620000, [])]
        obs = Observer(config={}, state=_FakeState({"vfo": VFO}),
                       log_path=Path("/tmp/x.log"))
        p1 = _patch_backends(ds, loaded=["com.scannerproject.icecast"])
        for p in p1:
            p.start()
        obs.classify_once()               # pass 1 records the backend set
        for p in p1:
            p.stop()
        p2 = _patch_backends(ds, loaded=[])   # icecast vanished
        lines = self._run_once(obs, p2)
        sys_line = next(l for l in lines if "scope=system" in l)
        self.assertIn("state=BROKEN", sys_line)
        self.assertIn("backend_pid_churn", sys_line)


class TestConfig(unittest.TestCase):
    def test_missing_config_yields_defaults(self):
        self.assertEqual(load_config(Path("/nonexistent/config.json")), {})

    def test_defaults_applied_when_config_empty(self):
        obs = Observer(config={}, state=_FakeState({}), log_path=Path("/tmp/x.log"))
        self.assertEqual(obs.poll_sec, 30.0)
        self.assertEqual(obs.log_max_bytes, 10 * 1024 * 1024)

    def test_config_overrides(self):
        obs = Observer(config={"poll_interval_sec": 5, "log_max_bytes": 1234},
                       state=_FakeState({}), log_path=Path("/tmp/x.log"))
        self.assertEqual(obs.poll_sec, 5.0)
        self.assertEqual(obs.log_max_bytes, 1234)


if __name__ == "__main__":
    unittest.main()
