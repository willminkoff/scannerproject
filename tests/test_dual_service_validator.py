"""Tests for ``validate_dual_service_configs``.

The MA/SL split architecture introduces a cross-config invariant
that the single-config validator can't see: airband + ground must
together declare exactly one Master + one Slave on the same RSPduo
serial.  This module pins the invariant.
"""
from __future__ import annotations

import textwrap
import unittest

from ui.config_validator import validate_dual_service_configs


def _device_block(*, mode: str, tuner: str, serial: str = "1809063632",
                  channel_freq: float = 124.6, mod: str = "am") -> str:
    """Render a minimal one-device config for one band, parseable by
    the validator's underlying parser."""
    return textwrap.dedent(f'''\
        devices:
        ( {{
            type = "soapysdr";
            device_string = "driver=sdrplay,serial={serial},mode={mode},tuner={tuner}";
            sample_rate = 1000000;
            channels: ( {{ freqs = ({channel_freq}); modulation = "{mod}";
                          bandwidth = 12000; squelch_threshold = -27;
                          outputs: ( {{type="mixer"; name="combined";}} ); }} );
          }} );
    ''')


PLUGGED = {
    "rtl": {"61108285"},
    "sdrplay": {"1809063632", "180903EF32"},
}


class HappyPathTests(unittest.TestCase):
    def test_proper_master_slave_pair_passes(self) -> None:
        result = validate_dual_service_configs(
            _device_block(mode="MA", tuner="1"),
            _device_block(mode="SL", tuner="2", mod="nfm", channel_freq=151.0775),
            plugged_rtl_serials=PLUGGED["rtl"],
            plugged_sdrplay_serials=PLUGGED["sdrplay"],
        )
        self.assertTrue(
            result["ok"],
            f"healthy MA/SL pair flagged as bad: {result['issues']}"
        )
        self.assertEqual(result["issues"], [])

    def test_per_service_results_returned_for_dashboard_rendering(self) -> None:
        # The dashboard needs to render which side failed; verify the
        # per_service dict is exposed.
        result = validate_dual_service_configs(
            _device_block(mode="MA", tuner="1"),
            _device_block(mode="SL", tuner="2", mod="nfm"),
            plugged_rtl_serials=PLUGGED["rtl"],
            plugged_sdrplay_serials=PLUGGED["sdrplay"],
        )
        self.assertIn("per_service", result)
        self.assertIn("airband", result["per_service"])
        self.assertIn("ground", result["per_service"])


class CrossConfigInvariantTests(unittest.TestCase):
    def test_mismatched_serials_rejected(self) -> None:
        # MA/SL clock coordination requires the same physical RSPduo;
        # configs pointing at two different SDRs would silently misroute.
        result = validate_dual_service_configs(
            _device_block(mode="MA", tuner="1", serial="1809063632"),
            _device_block(mode="SL", tuner="2", serial="180903EF32", mod="nfm"),
            plugged_rtl_serials=PLUGGED["rtl"],
            plugged_sdrplay_serials=PLUGGED["sdrplay"],
        )
        self.assertFalse(result["ok"])
        codes = [i["code"] for i in result["issues"]]
        self.assertIn("service_serial_mismatch", codes)

    def test_airband_in_wrong_mode_rejected(self) -> None:
        # The architecture demands airband=MA; a DT-mode block would
        # work alone but can't pair with SL.
        result = validate_dual_service_configs(
            _device_block(mode="DT", tuner="1"),
            _device_block(mode="SL", tuner="2", mod="nfm"),
            plugged_rtl_serials=PLUGGED["rtl"],
            plugged_sdrplay_serials=PLUGGED["sdrplay"],
        )
        self.assertFalse(result["ok"])
        codes = [i["code"] for i in result["issues"]]
        self.assertIn("service_wrong_mode", codes)

    def test_ground_on_wrong_tuner_rejected(self) -> None:
        # ground SL must be on Tuner 2; SL on Tuner 1 is meaningless.
        result = validate_dual_service_configs(
            _device_block(mode="MA", tuner="1"),
            _device_block(mode="SL", tuner="1", mod="nfm"),  # WRONG
            plugged_rtl_serials=PLUGGED["rtl"],
            plugged_sdrplay_serials=PLUGGED["sdrplay"],
        )
        self.assertFalse(result["ok"])
        codes = [i["code"] for i in result["issues"]]
        self.assertIn("service_wrong_tuner", codes)

    def test_unplugged_serial_still_flagged_via_inner_validator(self) -> None:
        # The per-service validator must still fire — a config that
        # names an unplugged RSPduo is rejected even if the MA/SL
        # mode/tuner invariants are satisfied.
        result = validate_dual_service_configs(
            _device_block(mode="MA", tuner="1", serial="DEADBEEF"),
            _device_block(mode="SL", tuner="2", serial="DEADBEEF", mod="nfm"),
            plugged_rtl_serials=PLUGGED["rtl"],
            plugged_sdrplay_serials=PLUGGED["sdrplay"],  # DEADBEEF NOT present
        )
        self.assertFalse(result["ok"])
        codes = [i["code"] for i in result["issues"]]
        self.assertIn("sdrplay_serial_not_enumerated", codes)


if __name__ == "__main__":
    unittest.main()
