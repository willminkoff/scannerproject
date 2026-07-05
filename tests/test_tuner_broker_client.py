"""broker.client: Python lease API and the CLI run-wrapper (the SDRTrunk path).

The CLI wrapper is what lets a non-Python consumer live under broker
arbitration: it claims, spawns the real program as a child, holds the lease
(the open socket) for the child's whole lifetime, propagates the exit code,
and releases.  These tests run the wrapper as a genuine subprocess against
a live broker and watch the lease appear and disappear around the child.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import unittest

from test_tuner_broker_helpers import (
    REPO_ROOT,
    RSP_B,
    RTL_FLEX_1,
    RTL_GROUND,
    make_policy,
    short_socket_dir,
    wait_until,
)

from broker.client import (
    EXIT_BROKER_UNAVAILABLE,
    EXIT_DENIED,
    SOCKET_ENV_VAR,
    BrokerDenied,
    BrokerUnavailable,
    claim,
    list_leases,
    resolve_socket_path,
    status,
)
from broker.server import BrokerServer


class ClientTestBase(unittest.TestCase):
    policy_kwargs: dict = {}

    def setUp(self):
        self.run_dir = short_socket_dir()
        self.addCleanup(shutil.rmtree, self.run_dir, ignore_errors=True)
        self.policy = make_policy(self.run_dir, **self.policy_kwargs)
        self.sock_path = self.policy.broker.socket
        self.server = BrokerServer(self.policy)
        self.server.start()
        self.addCleanup(self.server.shutdown)

    def claim(self, **kw):
        kw.setdefault("consumer", "client-test")
        kw.setdefault("reason", "unit test")
        kw.setdefault("socket_path", self.sock_path)
        return claim(**kw)

    def held_serials(self):
        return {l["serial"] for l in list_leases(socket_path=self.sock_path)}


class PythonApiTest(ClientTestBase):
    def test_context_manager_claims_and_releases(self):
        with self.claim(serial=RTL_GROUND) as lease:
            self.assertEqual(lease.serial, RTL_GROUND)
            self.assertEqual(lease.device_id, "RTL-1")
            self.assertFalse(lease.released)
            self.assertEqual(self.held_serials(), {RTL_GROUND})
        # Explicit release on exit: freed immediately, no USB-grace wait.
        self.assertTrue(lease.released)
        self.assertEqual(self.held_serials(), set())

    def test_release_is_idempotent(self):
        lease = self.claim(serial=RTL_GROUND)
        lease.release()
        lease.release()  # must not raise
        self.assertEqual(self.held_serials(), set())

    def test_denied_raises_with_structured_fields(self):
        self.claim(serial=RSP_B, consumer="gr-demod@airband", reason="airband ST")
        with self.assertRaises(BrokerDenied) as ctx:
            self.claim(serial=RSP_B, consumer="disco")
        self.assertEqual(ctx.exception.code, "already-leased")
        self.assertIn("gr-demod@airband", ctx.exception.reason)

    def test_role_claim(self):
        with self.claim(role="flex") as lease:
            self.assertEqual(lease.serial, RTL_FLEX_1)
            self.assertEqual(lease.role, "flex")

    def test_broker_down_raises_unavailable_not_denied(self):
        with self.assertRaises(BrokerUnavailable):
            claim(consumer="x", reason="y", serial=RTL_GROUND,
                  socket_path=os.path.join(self.run_dir, "nope.sock"))

    def test_env_var_socket_override(self):
        old = os.environ.get(SOCKET_ENV_VAR)
        os.environ[SOCKET_ENV_VAR] = self.sock_path
        try:
            self.assertEqual(resolve_socket_path(), self.sock_path)
            with claim(consumer="env-test", reason="env", serial=RTL_GROUND) as lease:
                self.assertEqual(lease.serial, RTL_GROUND)
        finally:
            if old is not None:
                os.environ[SOCKET_ENV_VAR] = old
            else:
                os.environ.pop(SOCKET_ENV_VAR, None)

    def test_status_and_list(self):
        snap = status(socket_path=self.sock_path)
        self.assertEqual(len(snap["devices"]), 5)
        self.assertEqual(snap["leases"], [])
        with self.claim(serial=RSP_B):
            rows = list_leases(socket_path=self.sock_path)
            self.assertEqual([r["serial"] for r in rows], [RSP_B])

    def test_crash_without_release_frees_serial(self):
        lease = self.claim(serial=RTL_GROUND, consumer="doomed")
        # Simulate the owner dying without the courtesy of release():
        lease._sock.close()
        self.assertTrue(
            wait_until(lambda: self.held_serials() == set(), timeout=5.0),
            "lease outlived its socket",
        )


class RetryAfterTest(ClientTestBase):
    policy_kwargs = {"min_restart": 5.0}

    def test_denial_carries_retry_after(self):
        self.claim(serial=RTL_GROUND).release()
        with self.assertRaises(BrokerDenied) as ctx:
            self.claim(serial=RTL_GROUND)
        self.assertEqual(ctx.exception.code, "min-restart-interval")
        self.assertIsNotNone(ctx.exception.retry_after_sec)
        self.assertGreater(ctx.exception.retry_after_sec, 0.0)


class CliTestBase(ClientTestBase):
    def cli_env(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = REPO_ROOT
        env.pop(SOCKET_ENV_VAR, None)  # tests pass --socket explicitly
        return env

    def run_cli(self, *args, timeout=30):
        return subprocess.run(
            [sys.executable, "-m", "broker.client", *args],
            cwd=REPO_ROOT, env=self.cli_env(),
            capture_output=True, text=True, timeout=timeout,
        )

    def wrapper_args(self, *command, serial=RTL_GROUND, extra=()):
        return [
            "run", "--serial", serial, "--consumer", "sdrtrunk",
            "--reason", "p25", "--socket", self.sock_path,
            *extra, "--", *command,
        ]


class CliRunWrapperTest(CliTestBase):
    def test_lease_held_while_child_runs_released_after(self):
        proc = subprocess.Popen(
            [sys.executable, "-m", "broker.client",
             *self.wrapper_args("/bin/sleep", "2")],
            cwd=REPO_ROOT, env=self.cli_env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            self.assertTrue(
                wait_until(lambda: self.held_serials() == {RTL_GROUND}, timeout=10.0),
                "wrapper never claimed the serial",
            )
            # While the child sleeps, a rival claim is refused naming us.
            with self.assertRaises(BrokerDenied) as ctx:
                self.claim(serial=RTL_GROUND, consumer="rival")
            self.assertIn("sdrtrunk", ctx.exception.reason)
            self.assertEqual(proc.wait(timeout=30), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.assertTrue(
            wait_until(lambda: self.held_serials() == set(), timeout=5.0),
            "lease not released after child exit",
        )

    def test_child_exit_code_propagates(self):
        proc = self.run_cli(*self.wrapper_args(
            sys.executable, "-c", "import sys; sys.exit(7)"))
        self.assertEqual(proc.returncode, 7, msg=proc.stderr)
        self.assertEqual(self.held_serials(), set())

    def test_sigterm_forwarded_to_child(self):
        proc = subprocess.Popen(
            [sys.executable, "-m", "broker.client",
             *self.wrapper_args("/bin/sleep", "30")],
            cwd=REPO_ROOT, env=self.cli_env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            self.assertTrue(
                wait_until(lambda: self.held_serials() == {RTL_GROUND}, timeout=10.0)
            )
            proc.send_signal(signal.SIGTERM)
            rc = proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.assertEqual(rc, 128 + signal.SIGTERM)  # child died of SIGTERM
        self.assertTrue(wait_until(lambda: self.held_serials() == set(), timeout=5.0))

    def test_denied_claim_exits_3_and_never_runs_command(self):
        blocker = self.claim(serial=RTL_GROUND, consumer="incumbent")
        self.addCleanup(blocker.release)
        marker = os.path.join(self.run_dir, "ran.marker")
        proc = self.run_cli(*self.wrapper_args("/usr/bin/touch", marker))
        self.assertEqual(proc.returncode, EXIT_DENIED, msg=proc.stderr)
        self.assertIn("DENIED", proc.stderr)
        self.assertIn("incumbent", proc.stderr)
        self.assertFalse(os.path.exists(marker), "command ran despite denial")

    def test_require_mode_broker_down_exits_4(self):
        marker = os.path.join(self.run_dir, "ran.marker")
        proc = self.run_cli(
            "run", "--serial", RTL_GROUND, "--consumer", "sdrtrunk",
            "--reason", "p25", "--socket", os.path.join(self.run_dir, "nope.sock"),
            "--", "/usr/bin/touch", marker,
        )
        self.assertEqual(proc.returncode, EXIT_BROKER_UNAVAILABLE, msg=proc.stderr)
        self.assertFalse(os.path.exists(marker), "command ran unbrokered under --require")

    def test_best_effort_broker_down_still_runs(self):
        marker = os.path.join(self.run_dir, "ran.marker")
        proc = self.run_cli(
            "run", "--serial", RTL_GROUND, "--consumer", "sdrtrunk",
            "--reason", "p25", "--socket", os.path.join(self.run_dir, "nope.sock"),
            "--best-effort", "--", "/usr/bin/touch", marker,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("UNBROKERED", proc.stderr)
        self.assertTrue(os.path.exists(marker))

    def test_best_effort_still_respects_active_denial(self):
        # best-effort tolerates broker-DOWN, never broker-said-no.
        blocker = self.claim(serial=RTL_GROUND, consumer="incumbent")
        self.addCleanup(blocker.release)
        marker = os.path.join(self.run_dir, "ran.marker")
        proc = self.run_cli(*self.wrapper_args(
            "/usr/bin/touch", marker, extra=("--best-effort",)))
        self.assertEqual(proc.returncode, EXIT_DENIED, msg=proc.stderr)
        self.assertFalse(os.path.exists(marker))

    def test_missing_command_is_usage_error(self):
        proc = self.run_cli(
            "run", "--serial", RTL_GROUND, "--consumer", "x", "--reason", "y",
            "--socket", self.sock_path,
        )
        self.assertEqual(proc.returncode, 2, msg=proc.stderr)

    def test_missing_serial_and_role_is_usage_error(self):
        proc = self.run_cli(
            "run", "--consumer", "x", "--reason", "y",
            "--socket", self.sock_path, "--", "/bin/echo", "hi",
        )
        self.assertEqual(proc.returncode, 2, msg=proc.stderr)

    def test_unexecutable_command_exits_127(self):
        proc = self.run_cli(*self.wrapper_args("/no/such/binary"))
        self.assertEqual(proc.returncode, 127, msg=proc.stderr)
        self.assertEqual(self.held_serials(), set())  # lease not leaked


class CliStatusTest(CliTestBase):
    def test_status_prints_snapshot_json(self):
        blocker = self.claim(serial=RSP_B, consumer="gr-demod@airband")
        self.addCleanup(blocker.release)
        proc = self.run_cli("status", "--socket", self.sock_path)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        snap = json.loads(proc.stdout)
        self.assertEqual(len(snap["devices"]), 5)
        self.assertEqual([l["serial"] for l in snap["leases"]], [RSP_B])

    def test_status_broker_down_exits_4(self):
        proc = self.run_cli("status", "--socket", os.path.join(self.run_dir, "no.sock"))
        self.assertEqual(proc.returncode, EXIT_BROKER_UNAVAILABLE)


class ConcurrentClientStressTest(ClientTestBase):
    def test_thread_stampede_yields_one_lease(self):
        import threading

        grants, denials, errors = [], [], []
        mu = threading.Lock()
        barrier = threading.Barrier(8)

        def worker(i):
            barrier.wait()
            try:
                lease = self.claim(serial=RSP_B, consumer=f"stampede-{i}")
                with mu:
                    grants.append(lease)
            except BrokerDenied as exc:
                with mu:
                    denials.append(exc)
            except Exception as exc:  # pragma: no cover
                with mu:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        try:
            self.assertEqual(errors, [])
            self.assertEqual(len(grants), 1)
            self.assertEqual(len(denials), 7)
            for exc in denials:
                self.assertEqual(exc.code, "already-leased")
        finally:
            for lease in grants:
                lease.release()


if __name__ == "__main__":
    unittest.main()
