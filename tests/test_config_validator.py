"""Tests for the combined-config pre-flight validator.

Triggering bug (2026-05-26)
---------------------------
A profile switch to the legacy ``bandscan_marine`` profile emitted a
combined config containing::

    {
      type = "rtlsdr";
      serial = "70613472";
      ...
    }

70613472 was the pre-migration ground RTL-SDR's serial; that dongle
isn't plugged in anymore.  rtl_airband crash-looped on every restart
(``RTLSDR device with serial number 70613472 not found``).  Nothing in
the apply path noticed before triggering the restart, so the failure
showed up as a 20-minute audio outage.

The validator's contract is: reject combined configs that name
devices the system can't actually open.  Tests pin both the obvious
cases (matching serial → ok; missing dongle → fail) and the harder
ones (duplicate tuner claims, DT-mode capacity, missing
enumerator).
"""
from __future__ import annotations

import textwrap
import unittest

from ui.config_validator import (
    MAX_TUNERS_PER_RSPDUO,
    _UNIVERSAL,
    validate_combined_config_text,
)


# Healthy production layout: airband on Tuner 1, ground on Tuner 2 of
# the same RSPduo, both in DT mode.
CONF_HAPPY_DT = textwrap.dedent('''\
    devices:
    (
      {
        type = "soapysdr";
        device_string = "driver=sdrplay,serial=1809063632,mode=DT,tuner=1";
        gain = 32.0;
        channels: ( { freqs = (124.6); modulation = "am"; bandwidth = 12000;
                      squelch_threshold = -27;
                      outputs: ( {type="mixer"; name="combined";} ); } );
      },
      {
        type = "soapysdr";
        device_string = "driver=sdrplay,serial=1809063632,mode=DT,tuner=2";
        gain = 29.0;
        channels: ( { freqs = (151.0775); modulation = "nfm"; bandwidth = 12500;
                      squelch_threshold = -27;
                      outputs: ( {type="mixer"; name="combined";} ); } );
      }
    )
''')


# The exact pathology from 2026-05-26 10:29: legacy rtlsdr block
# referencing a serial that isn't plugged in.
CONF_GHOST_RTLSDR = textwrap.dedent('''\
    devices:
    (
      {
        type = "soapysdr";
        device_string = "driver=sdrplay,serial=1809063632,mode=DT,tuner=1";
        gain = 32.0;
        channels: ( { freqs = (124.6); modulation = "am"; bandwidth = 12000;
                      squelch_threshold = -27;
                      outputs: ( {type="mixer"; name="combined";} ); } );
      },
      {
        type = "rtlsdr";
        serial = "70613472";
        index = 1;
        gain = 36.4;
        channels: ( { freqs = (156.8); modulation = "nfm"; bandwidth = 12500;
                      squelch_threshold = -45;
                      outputs: ( {type="mixer"; name="combined";} ); } );
      }
    )
''')


# Two blocks both claiming RSPduo serial=1809063632, tuner=1.
# Second open should fail LIBUSB_BUSY.
CONF_DUPLICATE_TUNER = textwrap.dedent('''\
    devices:
    (
      {
        type = "soapysdr";
        device_string = "driver=sdrplay,serial=1809063632,mode=DT,tuner=1";
        gain = 32.0;
        channels: ( { freqs = (124.6); modulation = "am"; bandwidth = 12000;
                      squelch_threshold = -27;
                      outputs: ( {type="mixer"; name="combined";} ); } );
      },
      {
        type = "soapysdr";
        device_string = "driver=sdrplay,serial=1809063632,mode=DT,tuner=1";
        gain = 30.0;
        channels: ( { freqs = (124.7); modulation = "am"; bandwidth = 12000;
                      squelch_threshold = -27;
                      outputs: ( {type="mixer"; name="combined";} ); } );
      }
    )
''')


# Three tuners on one RSPduo — exceeds DT capacity (max 2).
CONF_OVERCAPACITY = textwrap.dedent('''\
    devices:
    (
      {
        type = "soapysdr";
        device_string = "driver=sdrplay,serial=1809063632,mode=DT,tuner=1";
        gain = 32.0;
        channels: ( { freqs = (124.6); modulation = "am"; bandwidth = 12000;
                      squelch_threshold = -27;
                      outputs: ( {type="mixer"; name="combined";} ); } );
      },
      {
        type = "soapysdr";
        device_string = "driver=sdrplay,serial=1809063632,mode=DT,tuner=2";
        gain = 29.0;
        channels: ( { freqs = (151.0); modulation = "nfm"; bandwidth = 12500;
                      squelch_threshold = -27;
                      outputs: ( {type="mixer"; name="combined";} ); } );
      },
      {
        type = "soapysdr";
        device_string = "driver=sdrplay,serial=1809063632,mode=DT,tuner=3";
        gain = 30.0;
        channels: ( { freqs = (133.0); modulation = "am"; bandwidth = 12000;
                      squelch_threshold = -27;
                      outputs: ( {type="mixer"; name="combined";} ); } );
      }
    )
''')


CONF_EMPTY = ""


# Plugged-in device sets used across tests.
PLUGGED = {
    "rtl": {"61108285", "vdl2a001"},                # ACARS + VDL2 dongles
    "sdrplay": {"1809063632", "180903EF32"},       # both RSPduos
}


class HappyPathTests(unittest.TestCase):
    def test_two_dt_tuners_pass(self) -> None:
        result = validate_combined_config_text(
            CONF_HAPPY_DT,
            plugged_rtl_serials=PLUGGED["rtl"],
            plugged_sdrplay_serials=PLUGGED["sdrplay"],
        )
        self.assertTrue(result["ok"], f"unexpected issues: {result['issues']}")
        self.assertEqual([], result["issues"])
        self.assertEqual(2, len(result["checked_devices"]))

    def test_serials_match_case_insensitively(self) -> None:
        # Operator's combined config may have UPPER-case serials while
        # the USB sysfs reports lower-case (or vice versa).  Validator
        # must not false-alarm on case difference.
        upper_conf = CONF_HAPPY_DT.replace(
            "1809063632", "1809063632".upper()
        )
        result = validate_combined_config_text(
            upper_conf,
            plugged_rtl_serials=set(),
            plugged_sdrplay_serials={"1809063632"},  # lower-case
        )
        self.assertTrue(result["ok"])

    def test_empty_config_is_trivially_valid(self) -> None:
        # No devices declared ⇒ nothing to validate ⇒ ok.
        result = validate_combined_config_text(
            CONF_EMPTY,
            plugged_rtl_serials=set(),
            plugged_sdrplay_serials=set(),
        )
        self.assertTrue(result["ok"])
        self.assertEqual([], result["checked_devices"])


class GhostDeviceTests(unittest.TestCase):
    def test_rtlsdr_with_unplugged_serial_is_rejected(self) -> None:
        # The bandscan_marine pathology from 2026-05-26.
        result = validate_combined_config_text(
            CONF_GHOST_RTLSDR,
            plugged_rtl_serials={"61108285"},          # 70613472 NOT present
            plugged_sdrplay_serials=PLUGGED["sdrplay"],
        )
        self.assertFalse(result["ok"])
        codes = [i["code"] for i in result["issues"]]
        self.assertIn("rtlsdr_serial_not_enumerated", codes)
        offending = next(
            i for i in result["issues"]
            if i["code"] == "rtlsdr_serial_not_enumerated"
        )
        self.assertEqual(offending["serial"], "70613472")
        self.assertIn("70613472", offending["detail"])

    def test_sdrplay_with_unplugged_serial_is_rejected(self) -> None:
        result = validate_combined_config_text(
            CONF_HAPPY_DT,
            plugged_rtl_serials=PLUGGED["rtl"],
            plugged_sdrplay_serials={"DEADBEEF"},   # 1809063632 NOT present
        )
        self.assertFalse(result["ok"])
        codes = [i["code"] for i in result["issues"]]
        self.assertIn("sdrplay_serial_not_enumerated", codes)

    def test_missing_serial_in_rtlsdr_block(self) -> None:
        conf = textwrap.dedent('''\
            devices:
            (
              {
                type = "rtlsdr";
                gain = 36.4;
                channels: ( { freqs = (156.8); modulation = "nfm"; bandwidth = 12500;
                              squelch_threshold = -45;
                              outputs: ( {type="mixer"; name="combined";} ); } );
              }
            )
        ''')
        result = validate_combined_config_text(
            conf,
            plugged_rtl_serials={"anything"},
            plugged_sdrplay_serials=set(),
        )
        self.assertFalse(result["ok"])
        self.assertIn("rtlsdr_missing_serial", [i["code"] for i in result["issues"]])


class CollisionTests(unittest.TestCase):
    def test_two_blocks_same_tuner_is_rejected(self) -> None:
        result = validate_combined_config_text(
            CONF_DUPLICATE_TUNER,
            plugged_rtl_serials=set(),
            plugged_sdrplay_serials=PLUGGED["sdrplay"],
        )
        self.assertFalse(result["ok"])
        dup = [i for i in result["issues"] if i["code"] == "duplicate_tuner_claim"]
        self.assertEqual(1, len(dup))
        self.assertEqual("1809063632", dup[0]["serial"])
        self.assertEqual("1", dup[0]["tuner"])

    def test_dt_mode_capacity_exceeded(self) -> None:
        result = validate_combined_config_text(
            CONF_OVERCAPACITY,
            plugged_rtl_serials=set(),
            plugged_sdrplay_serials=PLUGGED["sdrplay"],
        )
        self.assertFalse(result["ok"])
        cap = [i for i in result["issues"] if i["code"] == "rspduo_too_many_tuners"]
        self.assertEqual(1, len(cap))
        self.assertEqual("1809063632", cap[0]["serial"])
        self.assertEqual(3, len(cap[0]["tuners"]))


class EnumeratorBehaviorTests(unittest.TestCase):
    def test_universal_set_skips_rtlsdr_check(self) -> None:
        # When the rtl enumerator fails (returns None upstream), we
        # treat the set as universal.  No rtlsdr serial should be
        # flagged.
        result = validate_combined_config_text(
            CONF_GHOST_RTLSDR,
            plugged_rtl_serials=_UNIVERSAL,             # type: ignore[arg-type]
            plugged_sdrplay_serials=PLUGGED["sdrplay"],
        )
        # 70613472 wasn't enumerated, but the universal set claims
        # it.  Result must be ok.
        self.assertTrue(result["ok"], f"unexpected issues: {result['issues']}")

    def test_enumerator_callbacks_are_invoked_when_set_omitted(self) -> None:
        calls = {"rtl": 0, "sdrplay": 0}

        def fake_rtl() -> set:
            calls["rtl"] += 1
            return {"61108285"}

        def fake_sdrplay() -> set:
            calls["sdrplay"] += 1
            return {"1809063632"}

        result = validate_combined_config_text(
            CONF_HAPPY_DT,
            rtl_enumerator=fake_rtl,
            sdrplay_enumerator=fake_sdrplay,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(1, calls["rtl"])
        self.assertEqual(1, calls["sdrplay"])

    def test_explicit_set_overrides_enumerator(self) -> None:
        # If both an explicit set AND an enumerator are supplied, the
        # explicit set wins (and the enumerator must not be called).
        def must_not_call() -> set:
            raise AssertionError("enumerator should not be invoked")

        result = validate_combined_config_text(
            CONF_HAPPY_DT,
            plugged_rtl_serials=set(),
            plugged_sdrplay_serials={"1809063632"},
            rtl_enumerator=must_not_call,
            sdrplay_enumerator=must_not_call,
        )
        self.assertTrue(result["ok"])


class EdgeCaseTests(unittest.TestCase):
    def test_max_tuners_per_rspduo_constant_is_two(self) -> None:
        # Regression guard — bumping this without thinking would
        # silently let DT-mode-incompatible configs through.  RSPduo
        # DT really is 2.
        self.assertEqual(2, MAX_TUNERS_PER_RSPDUO)

    def test_checked_devices_includes_every_block(self) -> None:
        result = validate_combined_config_text(
            CONF_OVERCAPACITY,
            plugged_rtl_serials=set(),
            plugged_sdrplay_serials=PLUGGED["sdrplay"],
        )
        self.assertEqual(3, len(result["checked_devices"]))
        for entry in result["checked_devices"]:
            self.assertIn("device_index", entry)
            self.assertIn("type", entry)
            self.assertIn("serial", entry)


if __name__ == "__main__":
    unittest.main()
