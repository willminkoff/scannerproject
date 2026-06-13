"""Unit tests for ``_chirp_dedicated_rspduo_serials`` — P1-8 cleanup.

The resolver must:
- Honor ``CHIRP_CONFIG_DIR`` (set by tests/dev to point at an alternate
  chirp config tree).
- Honor ``CHIRP_SDR_DEVICE_ARGS`` (matches the chirp daemon's own env
  override at ``chirp/daemon.py:load_config``).
- Honor ``CHIRP_RSPDUO_SERIAL`` (operator escape hatch).
- Union all sources (no precedence-clobbering — every claim from any
  source counts; a missing source is silently ignored except an empty
  TOTAL result, which gets a WARNING per rule 4).
- Log a WARNING when the resolved set is empty (no silent empty results).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
from unittest import mock

from ui import favorites_runtime as fr


class ChirpConfigDirOverrideTests(unittest.TestCase):
    """``CHIRP_CONFIG_DIR`` must actually steer the file lookup."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.config_dir = os.path.join(self.tmpdir.name, "config")
        os.makedirs(self.config_dir)
        # Write airband + ground configs with distinct serials.
        with open(os.path.join(self.config_dir, "airband.json"), "w") as f:
            json.dump(
                {"sdr": {"device_args": "soapy=,driver=sdrplay,serial=ABCDEF1234,mode=MA,tuner=1"}},
                f,
            )
        with open(os.path.join(self.config_dir, "ground.json"), "w") as f:
            json.dump(
                {"sdr": {"device_args": "soapy=,driver=sdrplay,serial=ABCDEF1234,mode=SL,tuner=2"}},
                f,
            )

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_reads_both_configs_and_unions_serials(self):
        with mock.patch.dict(
            os.environ,
            {"CHIRP_CONFIG_DIR": self.config_dir},
            clear=False,
        ):
            # Wipe the two env overrides so only the config files contribute.
            os.environ.pop("CHIRP_SDR_DEVICE_ARGS", None)
            os.environ.pop("CHIRP_RSPDUO_SERIAL", None)
            result = fr._chirp_dedicated_rspduo_serials()
        # Both files name the same physical RSPduo → one serial in the set.
        self.assertEqual(result, {"ABCDEF1234"})

    def test_distinct_serials_across_bands_yields_both(self):
        # Edge case: someone has chirp running with airband on one box and
        # ground on a different box.  Exclusion set should cover both.
        with open(os.path.join(self.config_dir, "ground.json"), "w") as f:
            json.dump(
                {"sdr": {"device_args": "soapy=,driver=sdrplay,serial=99887766,mode=ST,tuner=1"}},
                f,
            )
        with mock.patch.dict(
            os.environ,
            {"CHIRP_CONFIG_DIR": self.config_dir},
            clear=False,
        ):
            os.environ.pop("CHIRP_SDR_DEVICE_ARGS", None)
            os.environ.pop("CHIRP_RSPDUO_SERIAL", None)
            result = fr._chirp_dedicated_rspduo_serials()
        self.assertEqual(result, {"ABCDEF1234", "99887766"})


class ChirpSdrDeviceArgsEnvTests(unittest.TestCase):
    """``CHIRP_SDR_DEVICE_ARGS`` must contribute its serial."""

    def test_env_args_serial_is_extracted(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Point CHIRP_CONFIG_DIR at an empty dir so file path doesn't fire.
            with mock.patch.dict(
                os.environ,
                {
                    "CHIRP_CONFIG_DIR": tmp,
                    "CHIRP_SDR_DEVICE_ARGS":
                        "soapy=,driver=sdrplay,serial=DEADBEEF01,mode=MA,tuner=1",
                },
                clear=False,
            ):
                os.environ.pop("CHIRP_RSPDUO_SERIAL", None)
                result = fr._chirp_dedicated_rspduo_serials()
        self.assertEqual(result, {"DEADBEEF01"})

    def test_env_args_unioned_with_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "airband.json"), "w") as f:
                json.dump(
                    {"sdr": {"device_args": "soapy=,driver=sdrplay,serial=AAAA1111"}},
                    f,
                )
            with mock.patch.dict(
                os.environ,
                {
                    "CHIRP_CONFIG_DIR": tmp,
                    "CHIRP_SDR_DEVICE_ARGS":
                        "soapy=,driver=sdrplay,serial=BBBB2222",
                },
                clear=False,
            ):
                os.environ.pop("CHIRP_RSPDUO_SERIAL", None)
                result = fr._chirp_dedicated_rspduo_serials()
        self.assertEqual(result, {"AAAA1111", "BBBB2222"})


class ChirpRspduoSerialEnvTests(unittest.TestCase):
    """``CHIRP_RSPDUO_SERIAL`` is the escape hatch — comma- or
    semicolon-separated list, all uppercased."""

    def test_single_serial(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"CHIRP_CONFIG_DIR": tmp, "CHIRP_RSPDUO_SERIAL": "abcdef1234"},
                clear=False,
            ):
                os.environ.pop("CHIRP_SDR_DEVICE_ARGS", None)
                result = fr._chirp_dedicated_rspduo_serials()
        # Uppercased on the way in to match SoapySDR's reported casing.
        self.assertEqual(result, {"ABCDEF1234"})

    def test_comma_and_semicolon_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {
                    "CHIRP_CONFIG_DIR": tmp,
                    "CHIRP_RSPDUO_SERIAL": "AAA1,BBB2; CCC3 ,",
                },
                clear=False,
            ):
                os.environ.pop("CHIRP_SDR_DEVICE_ARGS", None)
                result = fr._chirp_dedicated_rspduo_serials()
        self.assertEqual(result, {"AAA1", "BBB2", "CCC3"})


class ChirpEmptyResultWarningTests(unittest.TestCase):
    """An empty exclusion set must emit a WARNING (rule 4 — no silent
    empty results)."""

    def test_empty_logs_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Config dir exists but is empty; both env overrides unset.
            with mock.patch.dict(
                os.environ,
                {"CHIRP_CONFIG_DIR": tmp},
                clear=False,
            ):
                os.environ.pop("CHIRP_SDR_DEVICE_ARGS", None)
                os.environ.pop("CHIRP_RSPDUO_SERIAL", None)
                with self.assertLogs(fr.logger, level=logging.WARNING) as cm:
                    result = fr._chirp_dedicated_rspduo_serials()
        self.assertEqual(result, set())
        # The warning must clearly mention "EMPTY" and at least one
        # remediation hint.
        joined = "\n".join(cm.output)
        self.assertIn("EMPTY", joined)
        self.assertTrue(
            "CHIRP_RSPDUO_SERIAL" in joined
            or "CHIRP_SDR_DEVICE_ARGS" in joined,
            msg=f"warning text missing remediation hint: {joined}",
        )

    def test_non_empty_does_not_log_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"CHIRP_CONFIG_DIR": tmp, "CHIRP_RSPDUO_SERIAL": "AAAA"},
                clear=False,
            ):
                os.environ.pop("CHIRP_SDR_DEVICE_ARGS", None)
                # Use assertNoLogs (3.10+) via guard: capture and confirm.
                logger = logging.getLogger(fr.logger.name)
                handler_records: list[logging.LogRecord] = []

                class _Cap(logging.Handler):
                    def emit(self, record):
                        if record.levelno >= logging.WARNING:
                            handler_records.append(record)

                cap = _Cap()
                logger.addHandler(cap)
                try:
                    fr._chirp_dedicated_rspduo_serials()
                finally:
                    logger.removeHandler(cap)
        # No WARNINGs from THIS function (cross-test leakage filtered).
        self.assertFalse(
            any("_chirp_dedicated_rspduo_serials" in r.getMessage() for r in handler_records),
            msg=[r.getMessage() for r in handler_records],
        )


class ChirpConfigUnreadableTests(unittest.TestCase):
    """File errors must be logged (not silently swallowed)."""

    def test_unreadable_file_logs_warning_but_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Write a malformed JSON to airband.json so the read raises
            # JSONDecodeError (caught + logged) but ground.json is missing
            # (caught + INFO-level).
            with open(os.path.join(tmp, "airband.json"), "w") as f:
                f.write("{not json")
            with mock.patch.dict(
                os.environ,
                {"CHIRP_CONFIG_DIR": tmp, "CHIRP_RSPDUO_SERIAL": "FALLBACK1"},
                clear=False,
            ):
                os.environ.pop("CHIRP_SDR_DEVICE_ARGS", None)
                with self.assertLogs(fr.logger, level=logging.INFO) as cm:
                    result = fr._chirp_dedicated_rspduo_serials()
        # Result still includes the env fallback.
        self.assertEqual(result, {"FALLBACK1"})
        # And the bad file produced a log line.
        self.assertTrue(
            any("airband.json" in line for line in cm.output),
            msg=cm.output,
        )


if __name__ == "__main__":
    unittest.main()
