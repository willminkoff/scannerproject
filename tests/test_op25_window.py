"""op25's tuning window must hold the whole site.

generate_multi_rx_config used to open the default 2.4 MS/s centred on the
selected control channel.  On an SDRplay that is a 1.536 MHz IF filter
(SoapySDRPlay3 picks the filter from the rate), so most of a wide site --
control channels and voice -- was outside it, and every grant out there
hard-retuned the device and left it parked on that grant.
"""
from __future__ import annotations

import unittest

from ui.op25_adapter import (
    RSPDUO_DT_SAMPLE_RATE,
    _device_window,
    _usable_window_hz,
    generate_multi_rx_config,
)

ST = "soapy=,driver=sdrplay,serial=1809063632,mode=ST,tuner=1"
MA = "soapy=,driver=sdrplay,serial=1809063632,mode=MA,tuner=1"
SL = "soapy=,driver=sdrplay,serial=1809063632,mode=SL,tuner=2"
RTL = "rtl=0"
OFFSET = 25_000

CAPE_MAY = [  # NJICS Cape May County Simulcast, 5.17 MHz
    769531250, 770118750, 770781250, 771168750, 771418750, 771693750, 771981250,
    772231250, 772556250, 772993750, 773306250, 773493750, 773918750, 774706250,
]
AC_SIMULCAST = [856443750, 855218750, 855718750, 856218750, 857443750]  # 2.23 MHz
BALTIMORE_COUNTY = [854012500, 855512500, 857962500, 859487500, 860962500]  # 6.95 MHz


def _get_decim(speed):
    """op25 p25_demodulator.get_decim -- None means no two-stage decimator."""
    for i_f in (24000, 25000, 32000):
        if speed % i_f == 0 and not (speed // i_f) & 1:
            return True
    return None


class WindowTests(unittest.TestCase):
    def assertHolds(self, args, channels, center, rate):
        half = _usable_window_hz(args, rate) / 2
        lo_edge, hi_edge = center + OFFSET - half, center + OFFSET + half
        for ch in channels:
            self.assertTrue(lo_edge < ch < hi_edge, f"{ch} outside {lo_edge}-{hi_edge}")

    def test_cape_may_opens_6msps_on_its_middle(self):
        center, rate = _device_window(ST, CAPE_MAY, 2_400_000, OFFSET)
        self.assertEqual(rate, 6_000_000)
        self.assertEqual(center, (769531250 + 774706250) // 2)
        self.assertHolds(ST, CAPE_MAY, center, rate)

    def test_ac_simulcast_outgrows_the_1536k_filter(self):
        center, rate = _device_window(ST, AC_SIMULCAST, 2_400_000, OFFSET)
        self.assertEqual(rate, 5_000_000)
        self.assertHolds(ST, AC_SIMULCAST, center, rate)

    def test_baltimore_county_opens_8msps(self):
        center, rate = _device_window(ST, BALTIMORE_COUNTY, 2_400_000, OFFSET)
        self.assertEqual(rate, 8_000_000)
        self.assertHolds(ST, BALTIMORE_COUNTY, center, rate)

    def test_narrow_site_keeps_default_rate(self):
        chans = [851012500, 851512500]
        center, rate = _device_window(ST, chans, 2_400_000, OFFSET)
        self.assertEqual(rate, 2_400_000)
        self.assertEqual(center, 851262500)

    def test_single_channel_is_unchanged(self):
        self.assertEqual(_device_window(ST, [851012500], 2_400_000, OFFSET), (851012500, 2_400_000))

    def test_center_ignores_which_control_channel_is_selected(self):
        a = _device_window(ST, CAPE_MAY, 2_400_000, OFFSET)
        b = _device_window(ST, CAPE_MAY[5:] + CAPE_MAY[:5], 2_400_000, OFFSET)
        self.assertEqual(a, b)

    def test_master_slave_never_leave_2msps(self):
        for args in (MA, SL):
            center, rate = _device_window(args, CAPE_MAY, 2_400_000, OFFSET)
            self.assertEqual(rate, RSPDUO_DT_SAMPLE_RATE)
            # Can't hold the site -> stay on the selected control channel.
            self.assertEqual(center, CAPE_MAY[0])

    def test_master_centres_a_site_that_fits(self):
        chans = [851012500, 851612500]
        self.assertEqual(_device_window(MA, chans, 2_400_000, OFFSET), (851312500, RSPDUO_DT_SAMPLE_RATE))

    def test_too_wide_for_anything_falls_back_to_selected_cc(self):
        chans = [851000000, 860500000]  # 9.5 MHz
        self.assertEqual(_device_window(ST, chans, 2_400_000, OFFSET), (851000000, 2_400_000))

    def test_rtl_rate_is_never_raised(self):
        self.assertEqual(_device_window(RTL, AC_SIMULCAST, 2_400_000, OFFSET), (856443750, 2_400_000))
        chans = [851012500, 851912500]
        self.assertEqual(_device_window(RTL, chans, 2_400_000, OFFSET), (851462500, 2_400_000))

    def test_every_rate_has_an_op25_two_stage_decimator(self):
        for chans in (CAPE_MAY, AC_SIMULCAST, BALTIMORE_COUNTY, [851012500]):
            _, rate = _device_window(ST, chans, 2_400_000, OFFSET)
            self.assertTrue(_get_decim(rate), rate)
            self.assertTrue(2_000_000 <= rate <= 10_660_000)  # SoapySDRPlay3 ST range


class GeneratorTests(unittest.TestCase):
    def test_device_and_follower_use_the_window(self):
        config = generate_multi_rx_config(
            [{"name": "NJICS", "control_channels_hz": CAPE_MAY}],
            {"NJICS": "1809063632"},
            dongle_args_map={"1809063632": ST, "8000": RTL},
            traffic_dongle_serial="8000",
            traffic_system_name="NJICS",
            sample_rate=2_400_000,
            offset=OFFSET,
        )
        main, follower = config["devices"]
        self.assertEqual((main["frequency"], main["rate"]), (772118750, 6_000_000))
        # RTL follower can't hold 5.17 MHz -> stays on the selected CC at its rate.
        self.assertEqual((follower["frequency"], follower["rate"]), (CAPE_MAY[0], 2_400_000))
        # Control-channel hunt list is untouched.
        self.assertEqual(
            config["trunking"]["chans"][0]["control_channel_list"].count(","), len(CAPE_MAY) - 1
        )


if __name__ == "__main__":
    unittest.main()
