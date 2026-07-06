"""SB7.2 workstream C tests: ServiceBackend abstraction + UNITS ghost-name
fix + sdrplay restart mutex.

Everything here is headless — subprocess is mocked throughout; no
systemctl/launchctl/pgrep is ever spawned.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import threading
import time
import unittest
from unittest import mock

from ui import service_backend
from ui import systemd
from ui.service_backend import LaunchdBackend, SystemdBackend


def _proc(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


# ===========================================================================
# SystemdBackend — the verbatim-moved subprocess logic
# ===========================================================================

class SystemdBackendRunTests(unittest.TestCase):
    def test_run_invokes_systemctl_with_args(self):
        backend = SystemdBackend()
        with mock.patch.object(
            service_backend.subprocess, "run", return_value=_proc([])
        ) as run:
            backend.run(["is-enabled", "foo"], use_sudo=False)
        cmd = run.call_args[0][0]
        self.assertEqual(["systemctl", "is-enabled", "foo"], cmd)

    def test_run_prefixes_sudo_when_requested(self):
        backend = SystemdBackend()
        with mock.patch.object(
            service_backend.subprocess, "run", return_value=_proc([])
        ) as run:
            backend.run(["restart", "foo"], use_sudo=True)
        cmd = run.call_args[0][0]
        self.assertEqual(["sudo", "systemctl", "restart", "foo"], cmd)


class SystemdBackendPrimitiveTests(unittest.TestCase):
    def setUp(self):
        self.backend = SystemdBackend()

    def test_active_true_on_zero_exit(self):
        with mock.patch.object(
            service_backend.subprocess, "run", return_value=_proc([], 0)
        ) as run:
            self.assertTrue(self.backend.active("foo"))
        self.assertEqual(
            ["systemctl", "is-active", "--quiet", "foo"], run.call_args[0][0]
        )

    def test_active_false_on_nonzero_exit(self):
        with mock.patch.object(
            service_backend.subprocess, "run", return_value=_proc([], 3)
        ):
            self.assertFalse(self.backend.active("foo"))

    def test_exists_true_when_loadstate_loaded(self):
        with mock.patch.object(
            service_backend.subprocess, "run", return_value=_proc([], 0, stdout="loaded\n")
        ):
            self.assertTrue(self.backend.exists("foo"))

    def test_exists_false_when_not_found(self):
        with mock.patch.object(
            service_backend.subprocess, "run", return_value=_proc([], 0, stdout="not-found\n")
        ):
            self.assertFalse(self.backend.exists("foo"))

    def test_exists_false_on_error_exit(self):
        with mock.patch.object(
            service_backend.subprocess, "run", return_value=_proc([], 1, stdout="")
        ):
            self.assertFalse(self.backend.exists("foo"))

    def test_enabled_true_only_for_enabled_states(self):
        for state, expected in (
            ("enabled", True),
            ("enabled-runtime", True),
            ("disabled", False),
            ("static", False),
            ("masked", False),
        ):
            with mock.patch.object(
                self.backend, "run", return_value=_proc([], 0, stdout=f"{state}\n")
            ):
                self.assertEqual(expected, self.backend.enabled("foo"), state)

    def test_enabled_false_when_run_raises(self):
        with mock.patch.object(self.backend, "run", side_effect=OSError("boom")):
            self.assertFalse(self.backend.enabled("foo"))

    def test_start_stop_restart_success_and_error_strings(self):
        for verb, method in (
            ("start", self.backend.start),
            ("stop", self.backend.stop),
            ("restart", self.backend.restart),
        ):
            with mock.patch.object(
                self.backend, "run", return_value=_proc([], 0)
            ) as run:
                self.assertEqual((True, ""), method("foo", use_sudo=True))
            self.assertEqual(([verb, "foo"],), run.call_args[0])
            self.assertTrue(run.call_args[1]["use_sudo"])

            # stderr preferred over stdout for the error detail
            with mock.patch.object(
                self.backend, "run", return_value=_proc([], 1, stdout="out", stderr="err")
            ):
                self.assertEqual((False, "err"), method("foo"))

            # empty output falls back to the code-bearing message
            with mock.patch.object(
                self.backend, "run", return_value=_proc([], 5)
            ):
                ok, err = method("foo")
                self.assertFalse(ok)
                self.assertEqual(f"{verb} failed (code 5)", err)

            # exception maps to (False, str(e))
            with mock.patch.object(self.backend, "run", side_effect=OSError("boom")):
                self.assertEqual((False, "boom"), method("foo"))

    def test_kill_sends_sigkill_with_sudo_and_swallows_errors(self):
        with mock.patch.object(self.backend, "run", return_value=_proc([], 0)) as run:
            self.backend.kill("foo")
        self.assertEqual((["kill", "-s", "SIGKILL", "foo"],), run.call_args[0])
        self.assertTrue(run.call_args[1]["use_sudo"])
        # never raises
        with mock.patch.object(self.backend, "run", side_effect=OSError("boom")):
            self.backend.kill("foo")
        # empty/blank unit is a no-op
        with mock.patch.object(self.backend, "run") as run:
            self.backend.kill("")
            self.backend.kill("   ")
        run.assert_not_called()

    def test_reset_failed_filters_empty_names(self):
        with mock.patch.object(self.backend, "run", return_value=_proc([], 0)) as run:
            self.backend.reset_failed(["a", "", None, " b "])
        self.assertEqual((["reset-failed", "a", "b"],), run.call_args[0])
        self.assertTrue(run.call_args[1]["use_sudo"])
        with mock.patch.object(self.backend, "run") as run:
            self.backend.reset_failed(["", None])
        run.assert_not_called()
        # never raises
        with mock.patch.object(self.backend, "run", side_effect=OSError("boom")):
            self.backend.reset_failed(["a"])

    def test_active_enter_epoch_sudo_fallback_contract(self):
        # Mirrors tests/test_systemd.py::UnitActiveEnterEpochTests at the
        # backend level: sudo retry ONLY on permission-flavored failures.
        with mock.patch.object(
            self.backend, "run", return_value=_proc([], 0, stdout="1234567\n")
        ) as run:
            self.assertEqual(1.234567, self.backend.active_enter_epoch("u"))
        run.assert_called_once()

        with mock.patch.object(
            self.backend,
            "run",
            return_value=_proc([], 1, stderr="Unit u could not be found."),
        ) as run:
            self.assertIsNone(self.backend.active_enter_epoch("u"))
        run.assert_called_once()

        with mock.patch.object(
            self.backend,
            "run",
            side_effect=[
                _proc([], 1, stderr="Interactive authentication required."),
                _proc([], 0, stdout="7654321\n"),
            ],
        ) as run:
            self.assertEqual(7.654321, self.backend.active_enter_epoch("u"))
        self.assertEqual(2, run.call_count)

    def test_restart_count_parse_and_sudo_fallback(self):
        with mock.patch.object(
            self.backend, "run", return_value=_proc([], 0, stdout="7\n")
        ):
            self.assertEqual(7, self.backend.restart_count("u"))

        with mock.patch.object(
            self.backend, "run", return_value=_proc([], 1, stderr="not found")
        ) as run:
            self.assertIsNone(self.backend.restart_count("u"))
        run.assert_called_once()

        with mock.patch.object(
            self.backend,
            "run",
            side_effect=[
                _proc([], 1, stderr="Access denied"),
                _proc([], 0, stdout="3\n"),
            ],
        ) as run:
            self.assertEqual(3, self.backend.restart_count("u"))
        self.assertEqual(2, run.call_count)


# ===========================================================================
# LaunchdBackend
# ===========================================================================

_LAUNCHCTL_PRINT_RUNNING = """\
com.scannerproject.rtl-airband = {
\tactive count = 1
\tpath = /Users/x/Library/LaunchAgents/com.scannerproject.rtl-airband.plist
\tstate = running

\tprogram = /opt/chirp/bin/gr-demod
\tpid = 4242
}
"""

_LAUNCHCTL_PRINT_WAITING = """\
com.scannerproject.rtl-airband = {
\tactive count = 0
\tstate = waiting
}
"""


class _LaunchctlFake:
    """Records launchctl invocations and replays canned results keyed on
    the full argv (minus the leading 'launchctl')."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if cmd[0] == "launchctl":
            key = tuple(cmd[1:])
            if key in self.responses:
                return self.responses[key]
            return _proc(cmd, 1, stderr="Could not find service in domain for port")
        if cmd[0] == "ps":
            key = tuple(cmd)
            if key in self.responses:
                return self.responses[key]
        return _proc(cmd, 1, stderr="unexpected command")


class LaunchdBackendTests(unittest.TestCase):
    UNIT = "com.scannerproject.rtl-airband"
    DOMAIN = "gui/501"
    TARGET = f"{DOMAIN}/{UNIT}"

    def _backend(self):
        with mock.patch.dict(
            os.environ, {"SCANNER_LAUNCHD_DOMAIN": self.DOMAIN}
        ):
            return LaunchdBackend()

    def test_domain_defaults_to_gui_uid(self):
        env = {k: v for k, v in os.environ.items() if k != "SCANNER_LAUNCHD_DOMAIN"}
        with mock.patch.dict(os.environ, env, clear=True):
            backend = LaunchdBackend()
        self.assertEqual(f"gui/{os.getuid()}", backend._domain)

    def test_domain_env_override(self):
        self.assertEqual(self.DOMAIN, self._backend()._domain)

    def test_active_true_only_when_state_running(self):
        backend = self._backend()
        fake = _LaunchctlFake(
            {("print", self.TARGET): _proc([], 0, stdout=_LAUNCHCTL_PRINT_RUNNING)}
        )
        with mock.patch.object(service_backend.subprocess, "run", side_effect=fake):
            self.assertTrue(backend.active(self.UNIT))

        fake = _LaunchctlFake(
            {("print", self.TARGET): _proc([], 0, stdout=_LAUNCHCTL_PRINT_WAITING)}
        )
        with mock.patch.object(service_backend.subprocess, "run", side_effect=fake):
            self.assertFalse(backend.active(self.UNIT))

        # not loaded at all
        fake = _LaunchctlFake({})
        with mock.patch.object(service_backend.subprocess, "run", side_effect=fake):
            self.assertFalse(backend.active(self.UNIT))

    def test_exists_follows_print_exit_code(self):
        backend = self._backend()
        fake = _LaunchctlFake(
            {("print", self.TARGET): _proc([], 0, stdout=_LAUNCHCTL_PRINT_WAITING)}
        )
        with mock.patch.object(service_backend.subprocess, "run", side_effect=fake):
            self.assertTrue(backend.exists(self.UNIT))
        fake = _LaunchctlFake({})
        with mock.patch.object(service_backend.subprocess, "run", side_effect=fake):
            self.assertFalse(backend.exists(self.UNIT))

    def test_enabled_reads_print_disabled_overrides(self):
        backend = self._backend()
        disabled_listing = (
            "disabled services = {\n"
            f'\t"{self.UNIT}" => disabled\n'
            '\t"com.other.svc" => enabled\n'
            "}\n"
        )
        fake = _LaunchctlFake(
            {("print-disabled", self.DOMAIN): _proc([], 0, stdout=disabled_listing)}
        )
        with mock.patch.object(service_backend.subprocess, "run", side_effect=fake):
            self.assertFalse(backend.enabled(self.UNIT))

        # "=> true" wording (newer macOS) also reads as disabled
        listing_true = f'\t"{self.UNIT}" => true\n'
        fake = _LaunchctlFake(
            {("print-disabled", self.DOMAIN): _proc([], 0, stdout=listing_true)}
        )
        with mock.patch.object(service_backend.subprocess, "run", side_effect=fake):
            self.assertFalse(backend.enabled(self.UNIT))

        # absent from the override list => enabled by default
        fake = _LaunchctlFake(
            {("print-disabled", self.DOMAIN): _proc([], 0, stdout="disabled services = {\n}\n")}
        )
        with mock.patch.object(service_backend.subprocess, "run", side_effect=fake):
            self.assertTrue(backend.enabled(self.UNIT))

    def test_start_kickstarts_loaded_service(self):
        backend = self._backend()
        fake = _LaunchctlFake({("kickstart", self.TARGET): _proc([], 0)})
        with mock.patch.object(service_backend.subprocess, "run", side_effect=fake):
            self.assertEqual((True, ""), backend.start(self.UNIT))
        self.assertEqual([["launchctl", "kickstart", self.TARGET]], fake.calls)

    def test_start_bootstraps_from_launchagents_when_not_loaded(self):
        backend = self._backend()
        plist = os.path.expanduser(f"~/Library/LaunchAgents/{self.UNIT}.plist")
        fake = _LaunchctlFake(
            {
                ("bootstrap", self.DOMAIN, plist): _proc([], 0),
            }
        )
        # first kickstart misses (not in responses -> "Could not find
        # service"), bootstrap succeeds, second kickstart succeeds.
        seq = {"n": 0}

        def run_side_effect(cmd, **kwargs):
            if cmd[:2] == ["launchctl", "kickstart"]:
                seq["n"] += 1
                fake.calls.append(list(cmd))
                if seq["n"] == 1:
                    return _proc(cmd, 113, stderr="Could not find service in domain")
                return _proc(cmd, 0)
            return fake(cmd, **kwargs)

        with mock.patch.object(
            service_backend.subprocess, "run", side_effect=run_side_effect
        ), mock.patch.object(service_backend.os.path, "isfile", return_value=True):
            self.assertEqual((True, ""), backend.start(self.UNIT))
        self.assertIn(["launchctl", "bootstrap", self.DOMAIN, plist], fake.calls)
        self.assertEqual(2, seq["n"])

    def test_start_reports_missing_plist(self):
        backend = self._backend()
        fake = _LaunchctlFake({})
        with mock.patch.object(
            service_backend.subprocess, "run", side_effect=fake
        ), mock.patch.object(service_backend.os.path, "isfile", return_value=False):
            ok, err = backend.start(self.UNIT)
        self.assertFalse(ok)
        self.assertIn("no plist", err)

    def test_stop_boots_out_and_treats_not_loaded_as_success(self):
        backend = self._backend()
        fake = _LaunchctlFake({("bootout", self.TARGET): _proc([], 0)})
        with mock.patch.object(service_backend.subprocess, "run", side_effect=fake):
            self.assertEqual((True, ""), backend.stop(self.UNIT))
        self.assertEqual([["launchctl", "bootout", self.TARGET]], fake.calls)

        # already stopped: systemctl stop of an inactive unit exits 0, so
        # bootout's "No such process" must map to success too.
        fake = _LaunchctlFake(
            {("bootout", self.TARGET): _proc([], 3, stderr="Boot-out failed: 3: No such process")}
        )
        with mock.patch.object(service_backend.subprocess, "run", side_effect=fake):
            self.assertEqual((True, ""), backend.stop(self.UNIT))

        # real failure propagates
        fake = _LaunchctlFake(
            {("bootout", self.TARGET): _proc([], 1, stderr="Operation not permitted")}
        )
        with mock.patch.object(service_backend.subprocess, "run", side_effect=fake):
            ok, err = backend.stop(self.UNIT)
        self.assertFalse(ok)
        self.assertIn("not permitted", err)

    def test_restart_uses_kickstart_k(self):
        backend = self._backend()
        fake = _LaunchctlFake({("kickstart", "-k", self.TARGET): _proc([], 0)})
        with mock.patch.object(service_backend.subprocess, "run", side_effect=fake):
            self.assertEqual((True, ""), backend.restart(self.UNIT))
        self.assertEqual([["launchctl", "kickstart", "-k", self.TARGET]], fake.calls)

    def test_restart_falls_back_to_start_when_not_loaded(self):
        backend = self._backend()
        fake = _LaunchctlFake({})  # kickstart -k misses
        with mock.patch.object(
            service_backend.subprocess, "run", side_effect=fake
        ), mock.patch.object(
            backend, "start", return_value=(True, "")
        ) as start:
            self.assertEqual((True, ""), backend.restart(self.UNIT))
        start.assert_called_once_with(self.UNIT)

    def test_kill_sends_sigkill_and_swallows_errors(self):
        backend = self._backend()
        fake = _LaunchctlFake({("kill", "SIGKILL", self.TARGET): _proc([], 0)})
        with mock.patch.object(service_backend.subprocess, "run", side_effect=fake):
            backend.kill(self.UNIT)
        self.assertEqual([["launchctl", "kill", "SIGKILL", self.TARGET]], fake.calls)
        with mock.patch.object(
            service_backend.subprocess, "run", side_effect=OSError("boom")
        ):
            backend.kill(self.UNIT)  # must not raise
        backend.kill("")  # blank unit: no-op, no subprocess

    def test_reset_failed_is_a_noop(self):
        backend = self._backend()
        with mock.patch.object(service_backend.subprocess, "run") as run:
            backend.reset_failed(["a", "b"])
        run.assert_not_called()

    def test_active_enter_epoch_from_pid_lstart(self):
        backend = self._backend()
        lstart = "Fri Jul  4 09:15:02 2026"
        fake = _LaunchctlFake(
            {
                ("print", self.TARGET): _proc([], 0, stdout=_LAUNCHCTL_PRINT_RUNNING),
                ("ps", "-o", "lstart=", "-p", "4242"): _proc([], 0, stdout=lstart + "\n"),
            }
        )
        with mock.patch.object(service_backend.subprocess, "run", side_effect=fake):
            epoch = backend.active_enter_epoch(self.UNIT)
        self.assertEqual(time.mktime(time.strptime(lstart)), epoch)

    def test_active_enter_epoch_none_when_no_pid_or_not_loaded(self):
        backend = self._backend()
        fake = _LaunchctlFake(
            {("print", self.TARGET): _proc([], 0, stdout=_LAUNCHCTL_PRINT_WAITING)}
        )
        with mock.patch.object(service_backend.subprocess, "run", side_effect=fake):
            self.assertIsNone(backend.active_enter_epoch(self.UNIT))
        fake = _LaunchctlFake({})
        with mock.patch.object(service_backend.subprocess, "run", side_effect=fake):
            self.assertIsNone(backend.active_enter_epoch(self.UNIT))

    def test_restart_count_is_best_effort_zero(self):
        backend = self._backend()
        with mock.patch.object(service_backend.subprocess, "run") as run:
            self.assertEqual(0, backend.restart_count(self.UNIT))
        run.assert_not_called()

    def test_run_escape_hatch_raises_not_implemented(self):
        backend = self._backend()
        with self.assertRaises(NotImplementedError):
            backend.run(["enable", "--now", "some.timer"], use_sudo=True)


# ===========================================================================
# get_backend() dispatch + caching
# ===========================================================================

class GetBackendDispatchTests(unittest.TestCase):
    def setUp(self):
        service_backend._reset_backend_for_tests()
        self.addCleanup(service_backend._reset_backend_for_tests)

    def _without_backend_env(self):
        env = {
            k: v
            for k, v in os.environ.items()
            if k != "SCANNER_SERVICE_BACKEND"
        }
        return mock.patch.dict(os.environ, env, clear=True)

    def test_defaults_to_systemd(self):
        with self._without_backend_env():
            backend = service_backend.get_backend()
        self.assertIsInstance(backend, SystemdBackend)
        self.assertEqual("systemd", backend.name)

    def test_selects_launchd(self):
        with mock.patch.dict(os.environ, {"SCANNER_SERVICE_BACKEND": "launchd"}):
            backend = service_backend.get_backend()
        self.assertIsInstance(backend, LaunchdBackend)
        self.assertEqual("launchd", backend.name)

    def test_backend_is_cached_until_reset(self):
        with self._without_backend_env():
            first = service_backend.get_backend()
        # A changed env does NOT re-dispatch while the cache holds...
        with mock.patch.dict(os.environ, {"SCANNER_SERVICE_BACKEND": "launchd"}):
            self.assertIs(first, service_backend.get_backend())
            # ...until the test-only reset hook clears it.
            service_backend._reset_backend_for_tests()
            self.assertIsInstance(service_backend.get_backend(), LaunchdBackend)

    def test_unknown_backend_raises_value_error(self):
        with mock.patch.dict(os.environ, {"SCANNER_SERVICE_BACKEND": "initd"}):
            with self.assertRaises(ValueError):
                service_backend.get_backend()


# ===========================================================================
# ui/systemd.py shims delegate to the backend
# ===========================================================================

class SystemdModuleShimTests(unittest.TestCase):
    """The module-level primitive names survive SB7.2 as pure delegators —
    every call site and the recovery-cascade test corpus reference them by
    name, so the shims are the compatibility contract."""

    def _fake_backend(self):
        backend = mock.Mock(spec=service_backend.ServiceBackend)
        backend.name = "fake"
        return backend

    def test_primitives_delegate_with_arguments(self):
        backend = self._fake_backend()
        backend.active.return_value = True
        backend.exists.return_value = True
        backend.enabled.return_value = True
        backend.start.return_value = (True, "")
        backend.stop.return_value = (True, "")
        backend.restart.return_value = (True, "")
        backend.active_enter_epoch.return_value = 12.5
        backend.restart_count.return_value = 4
        backend.run.return_value = _proc([], 0)

        with mock.patch.object(systemd, "get_backend", return_value=backend):
            self.assertTrue(systemd.unit_active("u"))
            self.assertTrue(systemd.unit_exists("u"))
            self.assertTrue(systemd.unit_enabled("u", use_sudo=True))
            self.assertEqual((True, ""), systemd._start_unit("u", use_sudo=True))
            self.assertEqual((True, ""), systemd._stop_unit("u"))
            self.assertEqual((True, ""), systemd._restart_unit("u", use_sudo=True))
            systemd._kill_unit("u")
            systemd._reset_failed_units(["a", "b"])
            self.assertEqual(12.5, systemd.unit_active_enter_epoch("u"))
            self.assertEqual(4, systemd.unit_restart_count("u"))
            systemd._run_systemctl(["reboot"], use_sudo=True)

        backend.active.assert_called_once_with("u")
        backend.exists.assert_called_once_with("u")
        backend.enabled.assert_called_once_with("u", use_sudo=True)
        backend.start.assert_called_once_with("u", use_sudo=True)
        backend.stop.assert_called_once_with("u", use_sudo=False)
        backend.restart.assert_called_once_with("u", use_sudo=True)
        backend.kill.assert_called_once_with("u")
        backend.reset_failed.assert_called_once_with(["a", "b"])
        backend.active_enter_epoch.assert_called_once_with("u")
        backend.restart_count.assert_called_once_with("u")
        backend.run.assert_called_once_with(["reboot"], use_sudo=True)


class SdrplayHealthProbeBackendGatingTests(unittest.TestCase):
    """The journalctl -k segfault scan is systemd-only; on launchd the
    probe reports healthy once the daemon process exists, so the
    HTTP/stats post-start probes carry wedge detection."""

    def test_launchd_backend_skips_journalctl(self):
        pgrep_hit = _proc(["pgrep"], 0, stdout="12345\n")
        recorded = []

        def fake_run(args, **kwargs):
            recorded.append(list(args))
            if args and args[0] == "pgrep":
                return pgrep_hit
            raise AssertionError(f"unexpected subprocess on launchd: {args}")

        backend = mock.Mock(spec=service_backend.ServiceBackend)
        backend.name = "launchd"
        with mock.patch.object(systemd, "get_backend", return_value=backend), \
             mock.patch.object(systemd.subprocess, "run", side_effect=fake_run):
            healthy, reason = systemd._sdrplay_daemon_healthy()

        self.assertTrue(healthy)
        self.assertIn("skipped", reason)
        self.assertEqual([["pgrep", "-x", "sdrplay_apiServ"]], recorded)

    def test_launchd_backend_still_unhealthy_when_daemon_missing(self):
        pgrep_miss = _proc(["pgrep"], 1)
        backend = mock.Mock(spec=service_backend.ServiceBackend)
        backend.name = "launchd"
        with mock.patch.object(systemd, "get_backend", return_value=backend), \
             mock.patch.object(systemd.subprocess, "run", return_value=pgrep_miss):
            healthy, reason = systemd._sdrplay_daemon_healthy()
        self.assertFalse(healthy)
        self.assertIn("not running", reason)


# ===========================================================================
# Task 3: sdrplay restart mutex
# ===========================================================================

class SdrplayRestartMutexTests(unittest.TestCase):
    """restart_digital() and restart_rtl_airband/_ground() can be invoked
    concurrently from separate API handler threads, and their escalation
    paths BOTH bounce the shared sdrplay daemon.  _SDRPLAY_RESTART_LOCK
    must serialize the bounce/escalation critical sections — this test
    drives two flows head-to-head and asserts the daemon bounce never
    runs concurrently."""

    def test_concurrent_escalations_serialize_daemon_bounce(self):
        state = {"depth": 0, "max_depth": 0, "bounces": 0}
        state_lock = threading.Lock()
        barrier = threading.Barrier(2, timeout=10)
        # systemd.time IS this module's time — patching its sleep below
        # would neuter the overlap-window sleep too, so grab the real one.
        real_sleep = time.sleep

        def fake_restart_unit(unit, use_sudo=False):
            if unit != "sdrplay":
                return True, ""
            with state_lock:
                state["depth"] += 1
                state["bounces"] += 1
                state["max_depth"] = max(state["max_depth"], state["depth"])
            # Widen the overlap window: without the mutex, the second
            # thread's bounce lands inside this sleep with near-certainty.
            real_sleep(0.05)
            with state_lock:
                state["depth"] -= 1
            return True, ""

        results = {}

        def run_restart_digital():
            barrier.wait()
            results["digital"] = systemd.restart_digital(reason="mutex-test")

        def run_restart_ground():
            barrier.wait()
            results["ground"] = systemd.restart_rtl_ground(reason="mutex-test")

        env = {
            # Probes never run here (starts are forced to fail before the
            # probe stage), but pin them off so a future refactor can't
            # accidentally reintroduce real HTTP/stats polling.
            "OP25_POST_START_PROBE_ENABLED": "0",
            "RTL_POST_START_PROBE_ENABLED": "0",
        }
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(systemd, "unit_exists", return_value=True), \
             mock.patch.object(systemd, "unit_active", return_value=False), \
             mock.patch.object(systemd, "_sdrplay_daemon_alive", return_value=True), \
             mock.patch.object(systemd, "_sdrplay_daemon_healthy", return_value=(True, "test: healthy")), \
             mock.patch.object(systemd, "_stop_unit", return_value=(True, "")), \
             mock.patch.object(systemd, "_kill_unit", return_value=None), \
             mock.patch.object(systemd, "_reset_failed_units", return_value=None), \
             mock.patch.object(systemd, "_start_unit", return_value=(False, "forced start failure")), \
             mock.patch.object(systemd, "_restart_unit", side_effect=fake_restart_unit), \
             mock.patch.object(systemd.time, "sleep", return_value=None):
            t1 = threading.Thread(target=run_restart_digital)
            t2 = threading.Thread(target=run_restart_ground)
            t1.start()
            t2.start()
            t1.join(timeout=30)
            t2.join(timeout=30)

        self.assertFalse(t1.is_alive(), "restart_digital thread wedged")
        self.assertFalse(t2.is_alive(), "restart_rtl_ground thread wedged")

        # Both flows failed all the way through (gentle + 2 escalations)
        # because every start was forced to fail...
        self.assertFalse(results["digital"][0])
        self.assertIn("wedge recovery exhausted", results["digital"][1])
        self.assertFalse(results["ground"][0])
        self.assertIn("wedge recovery exhausted", results["ground"][1])

        # ...so each flow bounced the daemon once per escalation (2 each;
        # gentle skipped it because alive+healthy) — and the bounces NEVER
        # overlapped across threads.
        self.assertEqual(4, state["bounces"])
        self.assertEqual(
            1,
            state["max_depth"],
            "sdrplay daemon bounce ran concurrently from two restart flows "
            "— _SDRPLAY_RESTART_LOCK is not serializing the critical section",
        )

    def test_lock_is_reentrant_for_single_flow_escalation(self):
        """The escalation wrapper holds the lock around the whole attempt
        while the daemon check-and-bounce block re-acquires it — an RLock
        requirement.  A plain Lock would deadlock a single escalation."""
        with mock.patch.object(systemd, "unit_exists", return_value=True), \
             mock.patch.object(systemd, "unit_active", return_value=False), \
             mock.patch.object(systemd, "_sdrplay_daemon_alive", return_value=True), \
             mock.patch.object(systemd, "_sdrplay_daemon_healthy", return_value=(True, "test: healthy")), \
             mock.patch.object(systemd, "_stop_unit", return_value=(True, "")), \
             mock.patch.object(systemd, "_kill_unit", return_value=None), \
             mock.patch.object(systemd, "_reset_failed_units", return_value=None), \
             mock.patch.object(systemd, "_start_unit", return_value=(False, "forced start failure")), \
             mock.patch.object(systemd, "_restart_unit", return_value=(True, "")), \
             mock.patch.object(systemd.time, "sleep", return_value=None):
            done = {}

            def run():
                done["result"] = systemd.restart_digital(reason="reentrancy-test")

            t = threading.Thread(target=run)
            t.start()
            t.join(timeout=15)
        self.assertFalse(t.is_alive(), "single-flow escalation deadlocked on the bounce lock")
        self.assertFalse(done["result"][0])


# ===========================================================================
# Task 2: UNITS ghost-name fix
# ===========================================================================

class UnitsDefaultsTests(unittest.TestCase):
    """The 30-minute outage P0: UNITS defaults named the RETIRED
    rtl-airband MA/SL units while the live analog daemons are chirp's
    gr-demod@airband / gr-demod@ground.  These pin the corrected defaults
    (and would have caught the original staleness)."""

    def _reload_config_with_scrubbed_env(self):
        scrub = {"UNIT_RTL", "UNIT_RTL_AIRBAND", "UNIT_RTL_GROUND", "UNIT_GROUND"}
        env = {k: v for k, v in os.environ.items() if k not in scrub}
        with mock.patch.dict(os.environ, env, clear=True):
            config = importlib.reload(importlib.import_module("ui.config"))
            units = dict(config.UNITS)
        # Re-reload with the real env so later tests see env-accurate
        # config state (mirrors tests/test_config_digital_defaults.py).
        importlib.reload(importlib.import_module("ui.config"))
        return units

    def test_analog_defaults_are_the_live_chirp_daemons(self):
        units = self._reload_config_with_scrubbed_env()
        self.assertEqual("gr-demod@airband", units["rtl"])
        self.assertEqual("gr-demod@airband", units["rtl_airband"])
        self.assertEqual("gr-demod@ground", units["rtl_ground"])
        self.assertEqual("gr-demod@ground", units["ground"])

    def test_retired_ghost_units_are_gone_from_defaults(self):
        units = self._reload_config_with_scrubbed_env()
        self.assertNotIn("rtl-airband-airband", units.values())
        self.assertNotIn("rtl-airband-ground", units.values())

    def test_env_overrides_still_win(self):
        env = dict(os.environ)
        env["UNIT_RTL_AIRBAND"] = "custom-airband.service"
        with mock.patch.dict(os.environ, env, clear=True):
            config = importlib.reload(importlib.import_module("ui.config"))
            self.assertEqual("custom-airband.service", config.UNITS["rtl_airband"])
        importlib.reload(importlib.import_module("ui.config"))

    def test_digital_audio_key_defined_exactly_once(self):
        # The UNITS literal used to define "digital_audio" twice (lines
        # 363 + 370 pre-fix) — a silent last-one-wins duplicate.  Dicts
        # can't show duplicates at runtime, so guard at the source level
        # (same style as the sb3 layout guardrail tests).
        config_path = os.path.join(
            os.path.dirname(os.path.abspath(systemd.__file__)), "config.py"
        )
        with open(config_path, "r", encoding="utf-8") as fh:
            source = fh.read()
        self.assertEqual(
            1,
            source.count('"digital_audio": os.getenv('),
            "UNITS must define digital_audio exactly once",
        )


if __name__ == "__main__":
    unittest.main()
