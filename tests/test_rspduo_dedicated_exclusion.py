"""Regression tests for the rtl-airband-claims-this-device exclusion.

Bug context (2026-05-25)
------------------------
After migrating rtl-airband off RTL-SDR dongles onto an RSPduo in
Dual-Tuner (DT) mode, the OP25 dongle allocator could still see that
RSPduo and would try to allocate one of its tuners to a digital
system when the scan pool had 2+ systems active.  Result: rtl-airband
crash-loops on ``no sdrplay device matches`` because OP25 grabbed the
device.

Design
------
The combined rtl-airband config IS the declaration of which devices
rtl-airband owns.  Reading from it (single source of truth) avoids
the duplicated-state failure mode of carrying the same information
in a separate env var.  An env var (``RTL_AIRBAND_RSPDUO_SERIAL``)
remains as an opt-in override for cases where the combined config
can't be parsed or the operator wants to pre-reserve a device.

This module exercises:
1. ``serials_claimed_by_combined_config`` extracts serials from both
   ``type="rtlsdr"; serial="..."`` and ``type="soapysdr";
   device_string="...,serial=X,..."`` blocks.
2. ``_rtl_airband_dedicated_rspduo_serials`` unions the config-derived
   set with the optional env-var override.
3. ``_rspduo_usb_serials`` filters the excluded set out of the
   USB-sysfs probe result.
4. ``_rspduo_tuner_ids`` correctly advertises both tuners of a single
   surviving RSPduo when ``OP25_RSPDUO_SPLIT_PROCESSES=1``.
"""
from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from unittest import mock

from ui import favorites_runtime
from ui import combined_status


def _write_conf(text: str) -> str:
    fh = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".conf", delete=False
    )
    try:
        fh.write(text)
    finally:
        fh.close()
    return fh.name


CONF_TWO_SOAPY_DEVICES = textwrap.dedent('''\
    log_scan_activity = true;
    devices:
    (
      {
        type = "soapysdr";
        device_string = "driver=sdrplay,serial=1809063632,mode=DT,tuner=1";
        sample_rate = 1000000;
        gain = 40;
        channels: ( { freqs = (124.6); modulation = "am"; bandwidth = 12000;
                      squelch_threshold = -25;
                      outputs: ( {type="mixer"; name="combined";} ); } );
      },
      {
        type = "soapysdr";
        device_string = "driver=sdrplay,serial=1809063632,mode=DT,tuner=2";
        sample_rate = 1000000;
        gain = 35;
        channels: ( { freqs = (151.0775); modulation = "nfm"; bandwidth = 12500;
                      squelch_threshold = -25;
                      outputs: ( {type="mixer"; name="combined";} ); } );
      }
    )
''')


CONF_LEGACY_RTL = textwrap.dedent('''\
    devices:
    (
      {
        type = "rtlsdr";
        serial = "83241970";
        index = 0;
        gain = 32.800;
        channels: ( { freqs = (124.6); modulation = "am"; bandwidth = 12000;
                      squelch_threshold = -45;
                      outputs: ( {type="mixer"; name="combined";} ); } );
      }
    )
''')


class SerialsClaimedByCombinedConfigTests(unittest.TestCase):
    def test_extracts_serial_from_soapysdr_device_string(self) -> None:
        path = _write_conf(CONF_TWO_SOAPY_DEVICES)
        try:
            serials = combined_status.serials_claimed_by_combined_config(path)
        finally:
            os.unlink(path)
        self.assertEqual({"1809063632"}, serials)

    def test_extracts_serial_from_legacy_rtlsdr_block(self) -> None:
        path = _write_conf(CONF_LEGACY_RTL)
        try:
            serials = combined_status.serials_claimed_by_combined_config(path)
        finally:
            os.unlink(path)
        self.assertEqual({"83241970"}, serials)

    def test_missing_config_returns_empty(self) -> None:
        serials = combined_status.serials_claimed_by_combined_config(
            "/tmp/this-conf-does-not-exist-xyz.conf"
        )
        self.assertEqual(set(), serials)

    def test_read_combined_devices_exposes_soapy_kwargs(self) -> None:
        # The structured kwargs let callers reason about backend mode
        # without re-parsing the raw device_string.
        path = _write_conf(CONF_TWO_SOAPY_DEVICES)
        try:
            devices = combined_status.read_combined_devices(path)
        finally:
            os.unlink(path)
        self.assertEqual(2, len(devices))
        self.assertEqual("soapysdr", devices[0]["device_type"])
        self.assertEqual("sdrplay", devices[0]["soapy_driver"])
        self.assertEqual("1809063632", devices[0]["soapy_kwargs"]["serial"])
        self.assertEqual("DT", devices[0]["soapy_kwargs"]["mode"])
        self.assertEqual("1", devices[0]["soapy_kwargs"]["tuner"])
        self.assertEqual("2", devices[1]["soapy_kwargs"]["tuner"])


class DedicatedSerialsHelperTests(unittest.TestCase):
    def test_derives_from_active_combined_config(self) -> None:
        path = _write_conf(CONF_TWO_SOAPY_DEVICES)
        try:
            # Patch COMBINED_CONFIG_PATH so the helper reads our fixture.
            with mock.patch.object(combined_status, "COMBINED_CONFIG_PATH", path):
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("RTL_AIRBAND_RSPDUO_SERIAL", None)
                    serials = favorites_runtime._rtl_airband_dedicated_rspduo_serials()
        finally:
            os.unlink(path)
        self.assertEqual(
            {"1809063632"},
            serials,
            "must pick up the rtl-airband-claimed serial from the conf without an env var",
        )

    def test_env_var_overrides_union_with_config(self) -> None:
        # Env var adds entries that aren't in the config (e.g. operator
        # is reserving a device pre-emptively before profile creation).
        path = _write_conf(CONF_TWO_SOAPY_DEVICES)
        try:
            with mock.patch.object(combined_status, "COMBINED_CONFIG_PATH", path), \
                 mock.patch.dict(os.environ, {"RTL_AIRBAND_RSPDUO_SERIAL": "DEADBEEF"}):
                serials = favorites_runtime._rtl_airband_dedicated_rspduo_serials()
        finally:
            os.unlink(path)
        self.assertEqual(
            {"1809063632", "DEADBEEF"},
            serials,
            "env var should union with config-derived set, not replace it",
        )

    def test_returns_empty_when_no_conf_and_no_env(self) -> None:
        with mock.patch.object(combined_status, "COMBINED_CONFIG_PATH", "/tmp/nope.conf"), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RTL_AIRBAND_RSPDUO_SERIAL", None)
            serials = favorites_runtime._rtl_airband_dedicated_rspduo_serials()
        self.assertEqual(set(), serials)


class RspduoUsbSerialsFilterTests(unittest.TestCase):
    def test_usb_serials_filters_config_derived_set(self) -> None:
        path = _write_conf(CONF_TWO_SOAPY_DEVICES)
        try:
            # Two RSPduos enumerate; one is rtl-airband's per the conf.
            with mock.patch.object(combined_status, "COMBINED_CONFIG_PATH", path), \
                 mock.patch.dict(os.environ, {}, clear=False), \
                 mock.patch.object(
                     favorites_runtime,
                     "_rspduo_usb_sample",
                     return_value=(2, ["180903EF32", "1809063632"]),
                 ):
                os.environ.pop("RTL_AIRBAND_RSPDUO_SERIAL", None)
                serials = favorites_runtime._rspduo_usb_serials()
        finally:
            os.unlink(path)
        self.assertEqual(["180903EF32"], serials)


class TunerIdsExpansionTests(unittest.TestCase):
    def test_one_rspduo_max_two_advertises_both_tuners_with_split_processes(self) -> None:
        # The rtl-airband RSPduo is filtered out via config; the
        # remaining one should expose Tuner 1 AND Tuner 2 when split
        # processes are enabled.
        path = _write_conf(CONF_TWO_SOAPY_DEVICES)
        try:
            with mock.patch.object(combined_status, "COMBINED_CONFIG_PATH", path), \
                 mock.patch.dict(os.environ, {"OP25_RSPDUO_SPLIT_PROCESSES": "1"}), \
                 mock.patch.object(
                     favorites_runtime,
                     "_rspduo_usb_sample",
                     return_value=(2, ["180903EF32", "1809063632"]),
                 ):
                os.environ.pop("RTL_AIRBAND_RSPDUO_SERIAL", None)
                ids = favorites_runtime._rspduo_tuner_ids(max_tuners=2)
        finally:
            os.unlink(path)
        self.assertEqual(
            [
                "RSPduo Tuner 1 SER#180903EF32",
                "RSPduo Tuner 2 SER#180903EF32",
            ],
            ids,
        )

    def test_one_rspduo_max_two_without_split_processes_only_tuner_1(self) -> None:
        # Master+Slave coexistence requires split processes; without
        # that flag the second tuner of the same box must not be
        # advertised, even when there's only one RSPduo in the pool.
        path = _write_conf(CONF_TWO_SOAPY_DEVICES)
        try:
            with mock.patch.object(combined_status, "COMBINED_CONFIG_PATH", path), \
                 mock.patch.dict(os.environ, {"OP25_RSPDUO_SPLIT_PROCESSES": "0"}), \
                 mock.patch.object(
                     favorites_runtime,
                     "_rspduo_usb_sample",
                     return_value=(2, ["180903EF32", "1809063632"]),
                 ):
                os.environ.pop("RTL_AIRBAND_RSPDUO_SERIAL", None)
                ids = favorites_runtime._rspduo_tuner_ids(max_tuners=2)
        finally:
            os.unlink(path)
        self.assertEqual(["RSPduo Tuner 1 SER#180903EF32"], ids)


if __name__ == "__main__":
    unittest.main()
