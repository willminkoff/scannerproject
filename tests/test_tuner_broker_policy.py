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
        # `version` is the SCHEMA version the broker enforces (stays 2); the
        # human arrangement label lives in the policy's `revision` string.
        self.assertEqual(pol.version, 2)
        # Revision 5.1 (arrangement C, 2026-07-21): 1 RSPduo (RSP-A 180903EF32
        # = digital) + 2 RTLs (airband 95339533 / ground 61108285). The Blog V4
        # 83241970 and the 12-Mb/s sounding dongle 56919602 were retired in the
        # 2026-07-19 swap. RSP-B (1809063632) is Venus's device, not this fleet's.
        self.assertEqual(len(pol.devices), 3)  # 1 RSPduo + 2 RTL (rev 5.1)
        self.assertEqual(pol.invariants.max_concurrent_dual_tuner_rspduo, 1)
        self.assertEqual(pol.invariants.rspduo_open_gap_sec, 2.0)
        self.assertEqual(pol.invariants.min_restart_interval_sec, 30.0)
        self.assertEqual(pol.broker.socket, "/opt/scannerproject/run/broker.sock")
        self.assertTrue(pol.broker.clear_locks_at_boot)

    def test_repo_policy_device_lookups(self):
        pol = load_policy(REAL_POLICY)
        # Revision 5.0 (2026-07-16): Neptune's ONE RSP is serial 180903EF32 and
        # it runs DIGITAL — dual-tuner from launch so SDRTrunk decodes 2 P25
        # systems at once via the native SDRplay API (the project's proven-clean
        # RSP path). It is the sole dual-tuner device (0x6bed invariant).
        #
        # Revision 4.1 had the two RSPduo serials REVERSED, and this test
        # asserted the reversal — so it PASSED against a wrong policy, which is
        # the only reason the error survived as long as it did. Ground truth is
        # now measured, not inferred: `ioreg -p IOUSB` on Neptune 2026-07-16
        # shows 180903EF32 on the FL1100 controller at 480 Mb/s, and no
        # 1809063632 anywhere. See docs/sb3-neptune-architecture.md §7.5.
        rsp = pol.device_by_serial("180903EF32")
        self.assertIsNotNone(rsp)
        self.assertEqual(rsp.role, "sdrtrunk-p25")
        self.assertEqual(rsp.kind, "rspduo")
        self.assertTrue(rsp.dual_tuner)
        self.assertEqual(
            [d.serial for d in pol.devices if d.dual_tuner], ["180903EF32"]
        )
        # 1809063632 is VENUS's RSPduo (airband via SDRangel there). It is not
        # part of Neptune's fleet and must NOT be in the active device list.
        self.assertIsNone(pol.device_by_serial("1809063632"))
        # RTL roles reconciled to the ACTUAL dongles after the 2026-07-19 swaps
        # (rev 5.1): airband AM on the NESDR SMArTee 95339533 (replaced the flaky
        # Blog V4 83241970), ground NFM on 61108285 (the proven 2026-06-18 fix).
        # The 12-Mb/s sounding dongle 56919602 was retired. Both analog bands are
        # RTL-only; the RSP is digital-only.
        self.assertEqual(
            [d.serial for d in pol.devices_for_role("chirp-airband")], ["95339533"]
        )
        self.assertEqual(
            [d.serial for d in pol.devices_for_role("chirp-ground")], ["61108285"]
        )
        self.assertEqual(pol.devices_for_role("flex-digital"), [])
        self.assertEqual(len(pol.devices), 3)  # 1 RSPduo + 2 RTL (rev 5.1)
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
