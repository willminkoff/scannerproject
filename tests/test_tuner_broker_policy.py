"""broker.policy hard-fail loader: every malformation must raise, none may default.

The policy loader guards the broker against the project's signature bug
shape — the silent fallback (SB6's exclusion resolver resolving to an empty
set on any read error, the icecast write-to-file fallback, the UNITS ghost
names).  A broker on a guessed policy would LOOK like arbitration while
enforcing nothing, so these tests insist on a PolicyError (with a stable
code) for every defect, plus exit code 3 from ``python -m broker``.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from test_tuner_broker_helpers import REPO_ROOT, make_policy_dict

from broker.policy import (
    DEFAULT_POLICY_PATH,
    POLICY_ENV_VAR,
    PolicyError,
    load_policy,
    parse_policy,
    resolve_policy_path,
)

REAL_POLICY = os.path.join(REPO_ROOT, "etc", "mac", "sdr_fleet_policy.json")


def _valid() -> dict:
    return make_policy_dict("/tmp/x")


class RealRepoPolicyTest(unittest.TestCase):
    """The committed etc/mac/sdr_fleet_policy.json must always parse —
    if the loader and the repo policy ever drift apart, the broker agent
    boot-loops on exit 3 and every SDR consumer is dead in the water."""

    def test_repo_policy_parses(self):
        pol = load_policy(REAL_POLICY)
        self.assertEqual(pol.version, 2)
        self.assertEqual(len(pol.devices), 6)
        self.assertEqual(pol.invariants.max_concurrent_dual_tuner_rspduo, 1)
        self.assertEqual(pol.invariants.rspduo_open_gap_sec, 2.0)
        self.assertEqual(pol.invariants.min_restart_interval_sec, 30.0)
        self.assertEqual(pol.broker.socket, "/opt/scannerproject/run/broker.sock")
        self.assertTrue(pol.broker.clear_locks_at_boot)

    def test_repo_policy_device_lookups(self):
        pol = load_policy(REAL_POLICY)
        rsp_a = pol.device_by_serial("180903EF32")
        self.assertIsNotNone(rsp_a)
        self.assertEqual(rsp_a.role, "sdrtrunk-p25")
        self.assertEqual(rsp_a.kind, "rspduo")
        # Policy v2.1 (2026-07-05): RSP-A is dual-tuner from launch so SDRTrunk
        # decodes 2 P25 systems at once (Will's ">=2 digital systems"
        # requirement). It is the sole dual-tuner device (0x6bed invariant).
        self.assertTrue(rsp_a.dual_tuner)
        self.assertEqual(
            [d.serial for d in pol.devices if d.dual_tuner], ["180903EF32"]
        )
        # RTL roles reconciled to the ACTUAL dongles enumerated on the M1
        # (2026-07-05): 3 RTLs attached (61108285, 56919602, 83241970), no 4th.
        # The stable micro-confirmed dongle is on the 24/7 ground band; the
        # flex-digital (3rd-P25 growth) slot has no dongle until a 4th is added.
        self.assertEqual(
            [d.serial for d in pol.devices_for_role("chirp-ground")], ["61108285"]
        )
        self.assertEqual(pol.devices_for_role("flex-digital"), [])
        self.assertEqual(len(pol.devices), 5)  # 2 RSPduo + 3 RTL
        self.assertIsNone(pol.device_by_serial("nope"))
        self.assertEqual(pol.devices_for_role("nope"), [])


class ParsePolicyValidationTest(unittest.TestCase):
    def _expect_code(self, data, code):
        with self.assertRaises(PolicyError) as ctx:
            parse_policy(data, path="<test>")
        self.assertEqual(ctx.exception.code, code, msg=str(ctx.exception))
        return ctx.exception

    def test_valid_fake_policy_parses(self):
        pol = parse_policy(_valid(), path="<test>")
        self.assertEqual(len(pol.devices), 5)
        self.assertEqual(pol.known_roles(),
                         ["sdrtrunk-p25", "chirp-airband", "chirp-ground", "flex"])
        self.assertIn("180903EF32(sdrtrunk-p25)", pol.known_devices_summary())

    def test_top_level_not_object(self):
        self._expect_code(["not", "an", "object"], "policy-bad-shape")

    def test_unsupported_version(self):
        data = _valid()
        data["version"] = 1
        self._expect_code(data, "policy-version")

    def test_version_bool_rejected(self):
        data = _valid()
        data["version"] = True  # bool is an int subclass; still not a version
        self._expect_code(data, "policy-version")

    def test_missing_invariants(self):
        data = _valid()
        del data["invariants"]
        self._expect_code(data, "policy-missing-key")

    def test_missing_broker_block(self):
        data = _valid()
        del data["broker"]
        self._expect_code(data, "policy-missing-key")

    def test_empty_devices(self):
        data = _valid()
        data["devices"] = []
        self._expect_code(data, "policy-no-devices")

    def test_duplicate_serial(self):
        # Two roles believing they own one physical device is exactly the
        # collision lie the broker exists to kill — refuse to arbitrate it.
        data = _valid()
        data["devices"][3]["serial"] = data["devices"][2]["serial"]
        exc = self._expect_code(data, "policy-duplicate-serial")
        self.assertIn(data["devices"][2]["serial"], exc.detail)

    def test_duplicate_id(self):
        data = _valid()
        data["devices"][4]["id"] = "RTL-4"
        data["devices"][4]["serial"] = "99999999"
        self._expect_code(data, "policy-duplicate-id")

    def test_rspduo_requires_dual_tuner_flag(self):
        # The flag feeds the 0x6bed invariant; omission on an RSPduo is
        # ambiguity, and ambiguity is a third state.
        data = _valid()
        del data["devices"][0]["dual_tuner"]
        self._expect_code(data, "policy-missing-key")

    def test_dual_tuner_true_on_rtlsdr_is_impossible(self):
        data = _valid()
        data["devices"][2]["dual_tuner"] = True
        self._expect_code(data, "policy-bad-value")

    def test_unknown_kind(self):
        data = _valid()
        data["devices"][2]["kind"] = "hackrf"
        self._expect_code(data, "policy-unknown-kind")

    def test_bool_as_seconds_rejected(self):
        data = _valid()
        data["invariants"]["rspduo_open_gap_sec"] = True
        self._expect_code(data, "policy-bad-type")

    def test_negative_seconds_rejected(self):
        data = _valid()
        data["invariants"]["min_restart_interval_sec"] = -1
        self._expect_code(data, "policy-bad-value")

    def test_negative_max_dual_rejected(self):
        data = _valid()
        data["invariants"]["max_concurrent_dual_tuner_rspduo"] = -1
        self._expect_code(data, "policy-bad-value")

    def test_empty_socket_rejected(self):
        data = _valid()
        data["broker"]["socket"] = "  "
        self._expect_code(data, "policy-empty-value")

    def test_device_entry_not_object(self):
        data = _valid()
        data["devices"][0] = "RSP-A"
        self._expect_code(data, "policy-bad-shape")


class LoadPolicyIOTest(unittest.TestCase):
    def test_missing_file(self):
        with self.assertRaises(PolicyError) as ctx:
            load_policy("/nonexistent/fleet_policy.json")
        self.assertEqual(ctx.exception.code, "policy-missing")

    def test_invalid_json(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{ not json")
            path = fh.name
        try:
            with self.assertRaises(PolicyError) as ctx:
                load_policy(path)
            self.assertEqual(ctx.exception.code, "policy-invalid-json")
        finally:
            os.unlink(path)

    def test_resolve_order_explicit_env_default(self):
        old = os.environ.get(POLICY_ENV_VAR)
        try:
            os.environ[POLICY_ENV_VAR] = "/from/env.json"
            self.assertEqual(resolve_policy_path("/explicit.json"), "/explicit.json")
            self.assertEqual(resolve_policy_path(), "/from/env.json")
            del os.environ[POLICY_ENV_VAR]
            self.assertEqual(resolve_policy_path(), DEFAULT_POLICY_PATH)
        finally:
            if old is not None:
                os.environ[POLICY_ENV_VAR] = old
            else:
                os.environ.pop(POLICY_ENV_VAR, None)

    def test_policy_error_to_dict_is_structured(self):
        exc = PolicyError("policy-missing", "gone", path="/x.json")
        d = exc.to_dict()
        self.assertEqual(d["error"], "fleet-policy")
        self.assertEqual(d["code"], "policy-missing")
        self.assertEqual(d["path"], "/x.json")


class DaemonHardFailTest(unittest.TestCase):
    """``python -m broker`` must exit 3 on a policy problem — the launchd
    agent surfaces that in tuner-broker.log instead of running a broker
    that enforces nothing."""

    def test_exit_code_3_on_missing_policy(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = REPO_ROOT
        env.pop(POLICY_ENV_VAR, None)
        proc = subprocess.run(
            [sys.executable, "-m", "broker", "--policy", "/nonexistent/policy.json"],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 3, msg=proc.stderr)
        diag = json.loads(proc.stderr.strip().splitlines()[0])
        self.assertEqual(diag["error"], "fleet-policy")
        self.assertEqual(diag["code"], "policy-missing")

    def test_exit_code_3_on_malformed_policy_via_env(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"version": 2}, fh)  # missing everything else
            path = fh.name
        env = dict(os.environ)
        env["PYTHONPATH"] = REPO_ROOT
        env[POLICY_ENV_VAR] = path
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "broker"],
                cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(proc.returncode, 3, msg=proc.stderr)
            diag = json.loads(proc.stderr.strip().splitlines()[0])
            self.assertEqual(diag["code"], "policy-missing-key")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
