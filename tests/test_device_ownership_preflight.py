"""Regression tests for the RTL device-ownership preflight.

Bug context (2026-05-25)
------------------------
A profile switch left rtl-airband stuck.  Root cause: ``acarsdec.service``
was still running from a prior ACARS-mode ground profile and held the
RTL the new airband profile was trying to claim.  rtl-airband
crash-looped on LIBUSB_BUSY but the failure was invisible (``rtl_active``
just reflected ``systemctl is-active``, which stayed True through
each ``Restart=on-failure`` cycle).

Fix: before every rtl-airband restart, walk the about-to-be-loaded
combined config to extract target serials, then stop any active known
claimant (acarsdec, dumpvdl2, radiosonde-auto-rx) whose configured
serial matches one of the targets.  Idempotent and logged.

This module exercises the discovery logic and the safety behaviors
around unresolved env vars.
"""
from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from unittest import mock

from ui import device_ownership as dev_own


def _write_env(env_lines: str) -> str:
    fh = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".conf", delete=False
    )
    try:
        fh.write(env_lines)
    finally:
        fh.close()
    return fh.name


class EnvFileParsingTests(unittest.TestCase):
    def test_read_env_file_handles_comments_and_quotes(self) -> None:
        path = _write_env(textwrap.dedent('''\
            # comment line
            ACARS_RTL_SERIAL=83241970
            VDL2_RTL_SERIAL="11223344"
            EMPTY=
            QUOTED='with spaces'
            BAD_LINE_WITHOUT_EQ
        '''))
        try:
            env = dev_own._read_env_file(path)
        finally:
            os.unlink(path)
        self.assertEqual("83241970", env["ACARS_RTL_SERIAL"])
        self.assertEqual("11223344", env["VDL2_RTL_SERIAL"])
        self.assertEqual("with spaces", env["QUOTED"])
        self.assertEqual("", env["EMPTY"])
        self.assertNotIn("BAD_LINE_WITHOUT_EQ", env)

    def test_missing_env_file_returns_empty_dict(self) -> None:
        env = dev_own._read_env_file("/tmp/this-path-does-not-exist-xyz")
        self.assertEqual({}, env)


class ResolveClaimantSerialTests(unittest.TestCase):
    def test_primary_env_wins_over_fallback(self) -> None:
        env = {"ACARS_RTL_SERIAL": "primary", "GROUND_RTL_SERIAL": "fallback"}
        self.assertEqual(
            "primary",
            dev_own._resolve_claimant_serial(env, "ACARS_RTL_SERIAL", "GROUND_RTL_SERIAL"),
        )

    def test_fallback_used_when_primary_unset(self) -> None:
        env = {"GROUND_RTL_SERIAL": "fallback"}
        self.assertEqual(
            "fallback",
            dev_own._resolve_claimant_serial(env, "ACARS_RTL_SERIAL", "GROUND_RTL_SERIAL"),
        )

    def test_empty_primary_falls_through(self) -> None:
        env = {"ACARS_RTL_SERIAL": "", "GROUND_RTL_SERIAL": "fallback"}
        self.assertEqual(
            "fallback",
            dev_own._resolve_claimant_serial(env, "ACARS_RTL_SERIAL", "GROUND_RTL_SERIAL"),
        )


class DiscoverClaimantsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env_path = _write_env(textwrap.dedent('''\
            ACARS_RTL_SERIAL=83241970
            VDL2_RTL_SERIAL=11223344
        '''))
        self.addCleanup(lambda: os.unlink(self.env_path))

    def test_active_claimant_with_overlapping_serial_is_reported(self) -> None:
        # acarsdec active, holding 83241970, target list includes it.
        active_map = {
            "acarsdec.service": True,
            "dumpvdl2.service": False,
            "radiosonde-auto-rx.service": False,
        }
        with mock.patch(
            "ui.device_ownership.unit_active",
            side_effect=lambda u: active_map.get(u, False),
        ):
            claimants = dev_own.discover_active_claimants(
                ["83241970", "70613472"],
                env_path=self.env_path,
            )
        self.assertEqual(1, len(claimants))
        self.assertEqual("acarsdec.service", claimants[0][0])
        self.assertEqual("83241970", claimants[0][1])
        self.assertEqual("env", claimants[0][2])

    def test_inactive_claimant_is_not_reported(self) -> None:
        # acarsdec inactive even though it would claim the target.
        with mock.patch(
            "ui.device_ownership.unit_active",
            return_value=False,
        ):
            claimants = dev_own.discover_active_claimants(
                ["83241970"],
                env_path=self.env_path,
            )
        self.assertEqual([], claimants)

    def test_target_serial_not_matching_is_skipped(self) -> None:
        # acarsdec active holding 83241970, but target only lists a different serial.
        with mock.patch(
            "ui.device_ownership.unit_active",
            side_effect=lambda u: u == "acarsdec.service",
        ):
            claimants = dev_own.discover_active_claimants(
                ["99999999"],
                env_path=self.env_path,
            )
        self.assertEqual([], claimants)

    def test_unresolved_serial_active_service_is_flagged_unknown(self) -> None:
        # Env file has no ACARS_RTL_SERIAL or GROUND_RTL_SERIAL, but
        # acarsdec is active.  Unit's hardcoded fallback may collide,
        # so it should be flagged "unknown" for safety.
        empty_env = _write_env("")
        try:
            with mock.patch(
                "ui.device_ownership.unit_active",
                side_effect=lambda u: u == "acarsdec.service",
            ):
                claimants = dev_own.discover_active_claimants(
                    ["any_serial"],
                    env_path=empty_env,
                )
        finally:
            os.unlink(empty_env)
        self.assertEqual(1, len(claimants))
        self.assertEqual("acarsdec.service", claimants[0][0])
        self.assertEqual("", claimants[0][1])
        self.assertEqual("unknown", claimants[0][2])

    def test_empty_target_serials_returns_empty(self) -> None:
        with mock.patch(
            "ui.device_ownership.unit_active",
            return_value=True,
        ):
            claimants = dev_own.discover_active_claimants([], env_path=self.env_path)
        self.assertEqual([], claimants)


class ReleaseRtlSerialsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env_path = _write_env("ACARS_RTL_SERIAL=83241970\n")
        self.addCleanup(lambda: os.unlink(self.env_path))

    def test_release_stops_overlapping_claimant(self) -> None:
        stop_calls: list[tuple[str, bool]] = []

        def fake_stop(unit, use_sudo=False):
            stop_calls.append((unit, use_sudo))
            return True, ""

        with mock.patch(
            "ui.device_ownership.unit_active",
            side_effect=lambda u: u == "acarsdec.service",
        ), mock.patch(
            "ui.device_ownership._stop_unit",
            side_effect=fake_stop,
        ):
            actions = dev_own.release_rtl_serials(
                ["83241970"],
                env_path=self.env_path,
                grace_sec=0,
            )
        self.assertEqual([("acarsdec.service", True)], stop_calls,
                         "stop must use sudo for decoder units")
        self.assertEqual(1, len(actions))
        self.assertEqual("acarsdec.service", actions[0]["unit"])
        self.assertTrue(actions[0]["stopped"])
        self.assertEqual("", actions[0]["error"])

    def test_release_with_no_claimants_returns_empty(self) -> None:
        with mock.patch("ui.device_ownership.unit_active", return_value=False):
            actions = dev_own.release_rtl_serials(
                ["83241970"],
                env_path=self.env_path,
                grace_sec=0,
            )
        self.assertEqual([], actions)

    def test_release_records_failure_without_raising(self) -> None:
        with mock.patch(
            "ui.device_ownership.unit_active",
            side_effect=lambda u: u == "acarsdec.service",
        ), mock.patch(
            "ui.device_ownership._stop_unit",
            return_value=(False, "permission denied"),
        ):
            actions = dev_own.release_rtl_serials(
                ["83241970"],
                env_path=self.env_path,
                grace_sec=0,
            )
        self.assertEqual(1, len(actions))
        self.assertFalse(actions[0]["stopped"])
        self.assertEqual("permission denied", actions[0]["error"])


if __name__ == "__main__":
    unittest.main()
