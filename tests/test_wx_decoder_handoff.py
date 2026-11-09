"""The ACARS/radiosonde handoff: loaded is not running, and the RSPduo is a pair.

Three operator-visible symptoms on 2026-11-08 ("radiosonde mode doesn't work.
also I hit poll acars and it still says radiosonde. even though it's still
logging acars data.") came from four defects:

  1. `_wx_decoder_launchctl_list` matched a `launchctl list` line on its LAST
     field and never read the PID column, which is "-" for a loaded-but-idle
     job. So "loaded" counted as "running", and because radiosonde was tested
     first -- and is permanently loaded -- the mode label was pinned to it.
  2. `_wx_decoder_switch` opened with `if <that predicate>: return ok`, so a
     switch to the already-loaded acars label returned success having done
     nothing at all.
  3. The target was started with `launchctl bootstrap`, which cannot start a
     `RunAtLoad=false` job -- and radiosonde's plist sets exactly that
     (`runs = 0`, `last exit code = (never exited)`). `bootstrap` on an
     already-loaded label also exits non-zero, and the old code never checked
     returncode, so it reported success either way.
  4. The release step flipped between two labels, so switching to radiosonde
     stopped acarsdec and left **dumpvdl2** holding the same RSPduo
     (serial 180903EF32, mode=MA master + mode=SL tuner=2 slave). The radio was
     never actually freed, and radiosonde needs it in mode=ST -- the whole
     device.

Fixing (1) alone would have been worse than the bug: the early return would
stop firing, acarsdec would be booted out, the sdrplay API kicked, and the
bootstrap would still fail silently -- leaving neither decoder running.

sb3/ui/routes.py imports dependencies this dev box lacks, so these tests exec
the functions out of its AST with injected fakes.
"""
from __future__ import annotations

import ast
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path

_ROUTES = Path(__file__).resolve().parents[1] / "sb3" / "ui" / "routes.py"
_SRC = io.open(_ROUTES, encoding="utf-8").read()

_FUNCS = ["_wx_launchctl_states", "_wx_service_state", "_wx_decoder_active",
          "_wx_decoder_switch", "_wx_msg_is_met",
          "_wx_write_intent", "_wx_read_intent"]
_CONSTS = {"_WX_DECODER_LABELS", "_WX_DECODER_SERVICES", "_WX_INTENT_PATH"}

ACARS = "com.scannerproject.acarsdec"
VDL2 = "com.scannerproject.dumpvdl2"
SONDE = "com.scannerproject.radiosonde"


def _load(ns=None):
    tree = ast.parse(_SRC)
    body = [n for n in tree.body
            if (isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") in _CONSTS for t in n.targets))
            or (isinstance(n, ast.FunctionDef) and n.name in _FUNCS)]
    got = {n.name for n in body if isinstance(n, ast.FunctionDef)}
    missing = set(_FUNCS) - got
    if missing:
        raise AssertionError("routes.py missing %s" % sorted(missing))
    ns = {} if ns is None else ns
    # routes.py aliases pathlib.Path as _WxPath; _WX_INTENT_PATH needs it at
    # exec time. Tests redirect the path itself so nothing touches $HOME.
    ns.setdefault("_WxPath", Path)
    exec(compile(ast.Module(body=body, type_ignores=[]), "<routes-sub>", "exec"), ns)
    return ns


def _fake_subprocess(stdout="", record=None):
    mod = types.ModuleType("subprocess")
    mod.PIPE = -1

    class R:
        def __init__(self, out):
            self.stdout, self.stderr, self.returncode = out, "", 0

    def run(args, **kw):
        if record is not None:
            record.append(list(args))
        return R(stdout)
    mod.run = run
    return mod


# Verbatim shape of `launchctl list` on neptune 2026-11-08.
LIST_OUT = (
    "PID\tStatus\tLabel\n"
    "17893\t0\tcom.scannerproject.dumpvdl2\n"
    "17890\t0\tcom.scannerproject.acarsdec\n"
    "-\t0\tcom.scannerproject.radiosonde\n"
)


class StateParsingTests(unittest.TestCase):
    def setUp(self):
        self.ns = _load()
        sys.modules["subprocess"] = _fake_subprocess(LIST_OUT)

    def tearDown(self):
        import subprocess  # noqa: F401 — restore the real module
        sys.modules["subprocess"] = __import__("subprocess")

    def test_pid_column_distinguishes_running_from_loaded(self):
        st = self.ns["_wx_launchctl_states"]()
        self.assertEqual(st.get(ACARS), "running")
        self.assertEqual(st.get(VDL2), "running")
        self.assertEqual(st.get(SONDE), "loaded",
                         "PID '-' means loaded but NOT running")

    def test_header_row_is_not_a_service(self):
        st = self.ns["_wx_launchctl_states"]()
        self.assertNotIn("Label", st)

    def test_absent_label_is_absent_not_false(self):
        f = self.ns["_wx_service_state"]
        self.assertEqual(f("com.example.nope", self.ns["_wx_launchctl_states"]()), "absent")

    def test_three_states_are_all_reachable(self):
        st = self.ns["_wx_launchctl_states"]()
        f = self.ns["_wx_service_state"]
        self.assertEqual({f(ACARS, st), f(SONDE, st), f("com.example.nope", st)},
                         {"running", "loaded", "absent"})


class ActiveDecoderTests(unittest.TestCase):
    def setUp(self):
        self.ns = _load()

    def test_reports_the_running_decoder_not_the_loaded_one(self):
        """The exact live situation: acars running, radiosonde merely loaded."""
        st = {ACARS: "running", VDL2: "running", SONDE: "loaded"}
        self.assertEqual(self.ns["_wx_decoder_active"](st), "acars")

    def test_radiosonde_wins_when_it_is_actually_running(self):
        self.assertEqual(self.ns["_wx_decoder_active"]({SONDE: "running"}), "radiosonde")

    def test_none_when_nothing_runs(self):
        st = {ACARS: "loaded", SONDE: "loaded"}
        self.assertEqual(self.ns["_wx_decoder_active"](st), "none")

    def test_empty_state_is_none(self):
        self.assertEqual(self.ns["_wx_decoder_active"]({}), "none")


class ServiceSetTests(unittest.TestCase):
    def setUp(self):
        self.ns = _load()

    def test_acars_mode_owns_both_halves_of_the_rspduo_pair(self):
        """Releasing acarsdec alone left dumpvdl2 holding the tuner."""
        self.assertEqual(self.ns["_WX_DECODER_SERVICES"]["acars"], [ACARS, VDL2])

    def test_master_starts_before_slave(self):
        svc = self.ns["_WX_DECODER_SERVICES"]["acars"]
        self.assertLess(svc.index(ACARS), svc.index(VDL2),
                        "acarsdec opens mode=MA (master); dumpvdl2 mode=SL "
                        "(slave) must follow it")

    def test_radiosonde_is_exclusive(self):
        self.assertEqual(self.ns["_WX_DECODER_SERVICES"]["radiosonde"], [SONDE])


class _FakeBackend:
    def __init__(self, fail_start=()):
        self.calls = []
        self._fail = set(fail_start)

    def stop(self, label, use_sudo=False):
        self.calls.append(("stop", label))
        return True, ""

    def start(self, label, use_sudo=False):
        self.calls.append(("start", label))
        if label in self._fail:
            return False, "simulated start failure"
        return True, ""


class SwitchTests(unittest.TestCase):
    """Drive _wx_decoder_switch with fakes and assert the sequence."""

    def _run(self, target, states, after=None, fail_start=()):
        ns = _load()
        # Never write the operator's real intent file from a test.
        self.intent_file = Path(tempfile.mkdtemp()) / "wx_decoder_intent"
        ns["_WX_INTENT_PATH"] = self.intent_file
        backend = _FakeBackend(fail_start=fail_start)
        seq = [dict(states)] + [dict(a) for a in (after or [])]

        def states_fn(*a, **k):
            return seq[0] if len(seq) == 1 else seq.pop(0)
        ns["_wx_launchctl_states"] = states_fn

        sb = types.ModuleType("ui.service_backend")
        sb.LaunchdBackend = lambda: backend
        pkg = types.ModuleType("ui")
        pkg.service_backend = sb
        sys.modules["ui"] = pkg
        sys.modules["ui.service_backend"] = sb

        kicks = []
        sys.modules["subprocess"] = _fake_subprocess(LIST_OUT, record=kicks)
        tm = types.ModuleType("time")
        tm.sleep = lambda *_: None
        tm.time = lambda: 0.0
        sys.modules["time"] = tm
        try:
            res = ns["_wx_decoder_switch"](target)
        finally:
            for m in ("ui", "ui.service_backend", "subprocess", "time"):
                sys.modules.pop(m, None)
            sys.modules["subprocess"] = __import__("subprocess")
            sys.modules["time"] = __import__("time")
        return res, backend.calls, kicks

    def test_loaded_target_does_not_short_circuit(self):
        """The core regression: radiosonde is LOADED, so the old code returned
        'already loaded' and never switched."""
        res, calls, _ = self._run(
            "radiosonde",
            {ACARS: "running", VDL2: "running", SONDE: "loaded"},
            after=[{SONDE: "running"}])
        self.assertTrue(any(c == ("start", SONDE) for c in calls),
                        "must actually start radiosonde; calls=%r" % (calls,))
        self.assertTrue(res["ok"], res)

    def test_switching_to_radiosonde_releases_BOTH_rspduo_users(self):
        _, calls, _ = self._run(
            "radiosonde",
            {ACARS: "running", VDL2: "running", SONDE: "loaded"},
            after=[{SONDE: "running"}])
        stopped = [l for verb, l in calls if verb == "stop"]
        self.assertIn(ACARS, stopped)
        self.assertIn(VDL2, stopped,
                      "dumpvdl2 holds the same RSPduo in slave mode; leaving it "
                      "running means the tuner is never freed")

    def test_switching_to_acars_starts_master_then_slave(self):
        _, calls, _ = self._run(
            "acars",
            {SONDE: "running", ACARS: "loaded", VDL2: "loaded"},
            after=[{ACARS: "running"}])
        started = [l for verb, l in calls if verb == "start"]
        self.assertEqual(started, [ACARS, VDL2])

    def test_already_running_target_is_a_noop(self):
        res, calls, kicks = self._run("acars", {ACARS: "running", VDL2: "running"})
        self.assertTrue(res["ok"])
        self.assertEqual(calls, [], "must not touch services that are already up")
        self.assertEqual(kicks, [], "must not kick the sdrplay API for a no-op")

    def test_sdrplay_is_kicked_only_when_something_was_stopped(self):
        """The KeepAlive-blip hazard: acarsdec and dumpvdl2 restart themselves.
        An unconditional kick would yank the SDRplay API out from under
        dumpvdl2 and chirp-ground during a healthy 10 s respawn."""
        _, _, kicks = self._run(
            "radiosonde",
            {ACARS: "loaded", VDL2: "loaded", SONDE: "loaded"},
            after=[{SONDE: "running"}])
        joined = " ".join(" ".join(k) for k in kicks)
        self.assertNotIn("com.sdrplay.service", joined,
                         "nothing was running, so nothing was taken away")

    def test_sdrplay_is_kicked_when_a_device_was_taken(self):
        _, _, kicks = self._run(
            "radiosonde",
            {ACARS: "running", VDL2: "running", SONDE: "loaded"},
            after=[{SONDE: "running"}])
        joined = " ".join(" ".join(k) for k in kicks)
        self.assertIn("com.sdrplay.service", joined)

    def test_a_failed_start_is_reported_not_swallowed(self):
        res, _, _ = self._run(
            "radiosonde",
            {ACARS: "running", VDL2: "running", SONDE: "loaded"},
            fail_start=(SONDE,))
        self.assertFalse(res["ok"])
        self.assertIn("error", res)
        self.assertIn("simulated start failure", res["error"])

    def test_target_that_never_comes_up_is_a_failure(self):
        """The old code returned ok:true on a bootstrap that did nothing."""
        res, _, _ = self._run(
            "radiosonde",
            {ACARS: "running", VDL2: "running", SONDE: "loaded"},
            after=[{SONDE: "loaded"}])
        self.assertFalse(res["ok"], "target never reached running")
        self.assertIn("error", res)

    def test_unknown_target_is_rejected(self):
        ns = _load()
        res = ns["_wx_decoder_switch"]("banana")
        self.assertFalse(res["ok"])
        self.assertIn("unknown target", res["error"])

    def test_intent_is_recorded_before_the_slow_work(self):
        """The watchdog cannot tell a dead radiosonde session from a deliberate
        ACARS one without this, so it must be written even if the switch then
        fails partway."""
        self._run("radiosonde",
                  {ACARS: "running", VDL2: "running", SONDE: "loaded"},
                  fail_start=(SONDE,))
        self.assertEqual(self.intent_file.read_text(encoding="utf-8").strip(),
                         "radiosonde",
                         "intent must be recorded even when the start fails")

    def test_intent_round_trips(self):
        ns = _load()
        f = Path(tempfile.mkdtemp()) / "intent"
        ns["_WX_INTENT_PATH"] = f
        ns["_wx_write_intent"]("radiosonde")
        self.assertEqual(ns["_wx_read_intent"](), "radiosonde")

    def test_unknown_intent_reads_back_as_acars(self):
        ns = _load()
        f = Path(tempfile.mkdtemp()) / "intent"
        f.write_text("banana\n", encoding="utf-8")
        ns["_WX_INTENT_PATH"] = f
        self.assertEqual(ns["_wx_read_intent"](), "acars",
                         "acars is the KeepAlive=true baseline")

    def test_steps_are_reported_for_diagnosis(self):
        res, _, _ = self._run(
            "radiosonde",
            {ACARS: "running", VDL2: "running", SONDE: "loaded"},
            after=[{SONDE: "running"}])
        self.assertTrue(res.get("steps"), "the endpoint must say what it did")


class MetHeuristicTests(unittest.TestCase):
    """met_count was pinned at 0 because the test looked at the wrong fields."""

    # Verbatim key set of an acarsdec record on neptune.
    ACARS_KEYS = ["ack", "app", "assstat", "block_id", "channel", "end", "error",
                  "flight", "freq", "label", "level", "libacars", "mfi", "mode",
                  "msgno", "station_id", "sublabel", "tail"]
    # What the OLD heuristic looked for -- MetObservation's OUTPUT fields.
    OLD_KEYS = ("temp_c", "temperature_c", "wind_speed_kt", "wind_dir_deg",
                "altitude_ft", "pressure_hpa", "amdar", "bufr")

    def test_a_real_acars_record_has_none_of_the_old_keys(self):
        """Why the old heuristic could never fire, stated as data."""
        self.assertEqual(set(self.ACARS_KEYS) & set(self.OLD_KEYS), set())

    def test_helper_consults_the_parser(self):
        src = _SRC.split("def _wx_msg_is_met", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("parse_acars_message", src,
                      "met detection must use the parser, not a key guess")

    def test_helper_is_defined_once_so_both_callers_are_fixed(self):
        self.assertEqual(_SRC.count("def _wx_msg_is_met"), 1)
        self.assertGreaterEqual(_SRC.count("_wx_msg_is_met("), 3,
                                "status count and per-row is_met both use it")

    def test_non_dict_and_empty_are_safe(self):
        ns = _load()
        f = ns["_wx_msg_is_met"]
        for bad in (None, "", [], 0, 3.5):
            with self.subTest(value=bad):
                self.assertFalse(f(bad))

    def test_counts_are_labelled_as_a_window(self):
        """message_count is a 200 kB tail, not a total -- say so in the payload."""
        self.assertIn('"counts_are_recent_window": True', _SRC)


if __name__ == "__main__":
    unittest.main()
