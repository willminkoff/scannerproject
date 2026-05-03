"""Tests for the per-device sample-rate override in ui.op25_adapter.

Verifies that gr-osmosdr device-args strings whose RSPduo mode is MA or SL
get the dual-tuner-valid rate instead of the caller-supplied default; ST,
RTL, and any other args pass through unchanged.
"""

from __future__ import annotations

import unittest

from ui.op25_adapter import (
    RSPDUO_DT_SAMPLE_RATE,
    _device_sample_rate_for_args,
    _master_first_device_order_key,
)


class DeviceSampleRateForArgsTests(unittest.TestCase):

    def test_rtl_args_use_default_rate(self):
        self.assertEqual(_device_sample_rate_for_args("rtl=0", 2_400_000), 2_400_000)
        self.assertEqual(_device_sample_rate_for_args("rtl=14306619", 2_048_000), 2_048_000)

    def test_rspduo_single_tuner_uses_default_rate(self):
        """ST mode supports up to ~10 MHz — no override needed."""
        args = "soapy=,driver=sdrplay,serial=180903EF32,mode=ST,tuner=1"
        self.assertEqual(_device_sample_rate_for_args(args, 2_400_000), 2_400_000)

    def test_rspduo_master_overrides_to_dt_sample_rate(self):
        args = "soapy=,driver=sdrplay,serial=180903EF32,mode=MA,tuner=1"
        self.assertEqual(
            _device_sample_rate_for_args(args, 2_400_000),
            RSPDUO_DT_SAMPLE_RATE,
        )

    def test_rspduo_slave_overrides_to_dt_sample_rate(self):
        args = "soapy=,driver=sdrplay,serial=180903EF32,mode=SL,tuner=2"
        self.assertEqual(
            _device_sample_rate_for_args(args, 2_400_000),
            RSPDUO_DT_SAMPLE_RATE,
        )

    def test_rspduo_dt_sample_rate_is_master_slave_valid(self):
        """Sanity: the override must be one of the SoapySDRPlay3 DT-mode rates."""
        # SoapySDRPlay3 dual-tuner mode allows 0.0625, 0.125, 0.25, 0.5, 1, 2 MSps.
        valid = {62_500, 125_000, 250_000, 500_000, 1_000_000, 2_000_000}
        self.assertIn(RSPDUO_DT_SAMPLE_RATE, valid)

    def test_args_substring_match_does_not_false_positive(self):
        """A literal 'mode=MA' must be present, not 'master' in some other word."""
        # Devices labelled 'master' or 'sm_assistant' shouldn't trigger override.
        for benign in (
            "rtl=0,offset=master_clock",
            "soapy=,driver=hackrf",
            "soapy=,driver=sdrplay,serial=X,mode=ST,tuner=1",  # ST, not MA/SL
        ):
            self.assertEqual(_device_sample_rate_for_args(benign, 2_400_000), 2_400_000)


class MasterFirstDeviceOrderKeyTests(unittest.TestCase):
    """SoapySDRPlay3 requires Master open before Slave; sort key enforces this."""

    def test_master_sorts_first(self):
        master = {"args": "soapy=,driver=sdrplay,serial=X,mode=MA,tuner=1"}
        self.assertEqual(_master_first_device_order_key(master), 0)

    def test_slave_sorts_last(self):
        slave = {"args": "soapy=,driver=sdrplay,serial=X,mode=SL,tuner=2"}
        self.assertEqual(_master_first_device_order_key(slave), 2)

    def test_rtl_sorts_middle(self):
        rtl = {"args": "rtl=0"}
        self.assertEqual(_master_first_device_order_key(rtl), 1)

    def test_rspduo_st_sorts_middle(self):
        st = {"args": "soapy=,driver=sdrplay,serial=X,mode=ST,tuner=1"}
        self.assertEqual(_master_first_device_order_key(st), 1)

    def test_devices_list_sort_master_first_slave_last(self):
        """End-to-end: a mixed devices list ends up Master, RTL..., Slave."""
        devices = [
            {"name": "sdr0", "args": "soapy=,driver=sdrplay,serial=A,mode=SL,tuner=2"},
            {"name": "sdr1", "args": "soapy=,driver=sdrplay,serial=A,mode=MA,tuner=1"},
            {"name": "sdr_traffic", "args": "rtl=0"},
            {"name": "sdr_traffic2", "args": "rtl=1"},
        ]
        devices.sort(key=_master_first_device_order_key)
        ordered_names = [d["name"] for d in devices]
        # Master first, RTLs preserve relative order, Slave last.
        self.assertEqual(ordered_names, ["sdr1", "sdr_traffic", "sdr_traffic2", "sdr0"])

    def test_pure_rtl_list_unchanged(self):
        """No RSPduo? Sort is a no-op (preserves caller order)."""
        devices = [
            {"name": "sdr0", "args": "rtl=0"},
            {"name": "sdr1", "args": "rtl=1"},
            {"name": "sdr_traffic", "args": "rtl=2"},
        ]
        original = [d["name"] for d in devices]
        devices.sort(key=_master_first_device_order_key)
        self.assertEqual([d["name"] for d in devices], original)


if __name__ == "__main__":
    unittest.main()
