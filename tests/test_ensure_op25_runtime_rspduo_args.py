"""Tests for scripts/ensure-op25-runtime.py RSPduo gr-osmosdr args translation.

Imports the runtime script as a module via importlib (filename has a dash,
so a normal import won't work).  Tests cover:

* ``_rspduo_osmosdr_args``: pure formatter, ST/MA/SL modes
* ``_select_rspduo_modes``: ST when only one tuner of a device is in the
  pool, MA + SL when both tuners of the same device are present
* ``_build_dongle_arg_map``: end-to-end dispatch (RSPduo / RTL-by-index /
  RTL-by-serial) with the right mode chosen automatically
"""

from __future__ import annotations

import importlib.util
import os
import unittest


_MODULE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "ensure-op25-runtime.py"
)


def _load_runtime_module():
    spec = importlib.util.spec_from_file_location("ensure_op25_runtime", _MODULE_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load module from {_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RspduoOsmosdrArgsTests(unittest.TestCase):
    """_rspduo_osmosdr_args is a pure formatter — mode is supplied by caller."""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_runtime_module()

    def test_tuner_1_st_mode(self):
        self.assertEqual(
            self.mod._rspduo_osmosdr_args("RSPduo Tuner 1 SER#180903EF32", mode="ST"),
            "soapy=,driver=sdrplay,serial=180903EF32,mode=ST,tuner=1",
        )

    def test_tuner_1_master_mode(self):
        self.assertEqual(
            self.mod._rspduo_osmosdr_args("RSPduo Tuner 1 SER#180903EF32", mode="MA"),
            "soapy=,driver=sdrplay,serial=180903EF32,mode=MA,tuner=1",
        )

    def test_tuner_2_slave_mode(self):
        self.assertEqual(
            self.mod._rspduo_osmosdr_args("RSPduo Tuner 2 SER#180903EF32", mode="SL"),
            "soapy=,driver=sdrplay,serial=180903EF32,mode=SL,tuner=2",
        )

    def test_default_mode_is_st(self):
        """No mode arg defaults to ST — preserves callers that don't pass it."""
        args = self.mod._rspduo_osmosdr_args("RSPduo Tuner 1 SER#ABC123")
        self.assertIn("mode=ST", args)

    def test_serial_uppercased(self):
        args = self.mod._rspduo_osmosdr_args("RSPduo Tuner 1 SER#abcdef0123", mode="MA")
        self.assertIn("serial=ABCDEF0123", args)

    def test_invalid_mode_raises(self):
        """Unknown mode strings surface as ValueError early, not at osmosdr-open time."""
        with self.assertRaises(ValueError):
            self.mod._rspduo_osmosdr_args("RSPduo Tuner 1 SER#X", mode="WAT")

    def test_rtl_serial_returns_none(self):
        for s in ("14306619", "70613472", "00000001", "80000003"):
            self.assertIsNone(self.mod._rspduo_osmosdr_args(s))

    def test_empty_or_garbage_returns_none(self):
        self.assertIsNone(self.mod._rspduo_osmosdr_args(""))
        self.assertIsNone(self.mod._rspduo_osmosdr_args(None))
        self.assertIsNone(self.mod._rspduo_osmosdr_args("RSPduo without tuner"))
        self.assertIsNone(self.mod._rspduo_osmosdr_args("RSPduo Tuner 1"))  # no SER#
        self.assertIsNone(self.mod._rspduo_osmosdr_args("Tuner 1 SER#180903EF32"))  # missing prefix


class SelectRspduoModesTests(unittest.TestCase):
    """_select_rspduo_modes chooses ST / MA / SL based on pool composition."""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_runtime_module()

    def test_empty_pool(self):
        self.assertEqual(self.mod._select_rspduo_modes([]), {})

    def test_pure_rtl_pool_yields_no_rspduo_entries(self):
        modes = self.mod._select_rspduo_modes(["14306619", "70613472", "80000003"])
        self.assertEqual(modes, {})

    def test_only_tuner_1_uses_st_mode(self):
        """One RSPduo tuner alone -> Single Tuner mode."""
        modes = self.mod._select_rspduo_modes(["RSPduo Tuner 1 SER#180903EF32"])
        self.assertEqual(modes, {"RSPduo Tuner 1 SER#180903EF32": "ST"})

    def test_only_tuner_2_uses_st_mode(self):
        modes = self.mod._select_rspduo_modes(["RSPduo Tuner 2 SER#180903EF32"])
        self.assertEqual(modes, {"RSPduo Tuner 2 SER#180903EF32": "ST"})

    def test_both_tuners_one_device_uses_master_slave(self):
        """Tuner 1 -> MA (Master), Tuner 2 -> SL (Slave)."""
        modes = self.mod._select_rspduo_modes([
            "RSPduo Tuner 1 SER#180903EF32",
            "RSPduo Tuner 2 SER#180903EF32",
        ])
        self.assertEqual(modes, {
            "RSPduo Tuner 1 SER#180903EF32": "MA",
            "RSPduo Tuner 2 SER#180903EF32": "SL",
        })

    def test_pool_order_does_not_change_mode_assignment(self):
        """Tuner 2 listed before Tuner 1 still yields Tuner 1 = MA, Tuner 2 = SL."""
        modes = self.mod._select_rspduo_modes([
            "RSPduo Tuner 2 SER#180903EF32",
            "RSPduo Tuner 1 SER#180903EF32",
        ])
        self.assertEqual(modes["RSPduo Tuner 1 SER#180903EF32"], "MA")
        self.assertEqual(modes["RSPduo Tuner 2 SER#180903EF32"], "SL")

    def test_two_rspduo_devices_each_get_master_slave(self):
        """Two physical RSPduos: each independently in Master/Slave."""
        modes = self.mod._select_rspduo_modes([
            "RSPduo Tuner 1 SER#180903EF32",
            "RSPduo Tuner 2 SER#180903EF32",
            "RSPduo Tuner 1 SER#9F00112233",
            "RSPduo Tuner 2 SER#9F00112233",
        ])
        self.assertEqual(modes, {
            "RSPduo Tuner 1 SER#180903EF32": "MA",
            "RSPduo Tuner 2 SER#180903EF32": "SL",
            "RSPduo Tuner 1 SER#9F00112233": "MA",
            "RSPduo Tuner 2 SER#9F00112233": "SL",
        })

    def test_two_rspduos_one_with_one_tuner_one_with_both(self):
        """Mixed scenario: one device fully populated (MA/SL), another with only Tuner 1 (ST)."""
        # SDRplay serials are hex strings — the regex deliberately rejects non-hex.
        modes = self.mod._select_rspduo_modes([
            "RSPduo Tuner 1 SER#180903EF32",
            "RSPduo Tuner 2 SER#180903EF32",
            "RSPduo Tuner 1 SER#9F00112233",
        ])
        self.assertEqual(modes["RSPduo Tuner 1 SER#180903EF32"], "MA")
        self.assertEqual(modes["RSPduo Tuner 2 SER#180903EF32"], "SL")
        self.assertEqual(modes["RSPduo Tuner 1 SER#9F00112233"], "ST")


class BuildDongleArgMapTests(unittest.TestCase):
    """_build_dongle_arg_map dispatches between RSPduo / RTL-by-index / RTL-by-serial."""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_runtime_module()

    def setUp(self):
        # Stub rtl_test enumeration so the test never shells out.
        self._orig_enum = self.mod._enumerate_rtlsdr_serial_index_map

    def tearDown(self):
        self.mod._enumerate_rtlsdr_serial_index_map = self._orig_enum

    def _set_rtl_index_map(self, mapping):
        self.mod._enumerate_rtlsdr_serial_index_map = lambda: dict(mapping)

    def test_pure_rtl_pool_no_index_mapping(self):
        self._set_rtl_index_map({})
        result = self.mod._build_dongle_arg_map(["14306619", "70613472"])
        self.assertEqual(
            result,
            {"14306619": "rtl=14306619", "70613472": "rtl=70613472"},
        )

    def test_rtl_with_index_mapping_uses_index(self):
        self._set_rtl_index_map({"14306619": 0, "70613472": 1})
        result = self.mod._build_dongle_arg_map(["14306619", "70613472", "80000003"])
        self.assertEqual(result, {
            "14306619": "rtl=0",
            "70613472": "rtl=1",
            "80000003": "rtl=80000003",  # absent from index map -> serial fallback
        })

    def test_single_rspduo_tuner_uses_st_mode(self):
        self._set_rtl_index_map({})
        result = self.mod._build_dongle_arg_map([
            "RSPduo Tuner 1 SER#180903EF32",
            "70613472",
        ])
        self.assertEqual(result, {
            "RSPduo Tuner 1 SER#180903EF32":
                "soapy=,driver=sdrplay,serial=180903EF32,mode=ST,tuner=1",
            "70613472": "rtl=70613472",
        })

    def test_both_rspduo_tuners_use_master_slave_mode(self):
        """Equal-priority systems on RSPduo: Tuner 1 = MA, Tuner 2 = SL."""
        self._set_rtl_index_map({})
        result = self.mod._build_dongle_arg_map([
            "RSPduo Tuner 1 SER#180903EF32",
            "RSPduo Tuner 2 SER#180903EF32",
            "70613472",
        ])
        self.assertEqual(result, {
            "RSPduo Tuner 1 SER#180903EF32":
                "soapy=,driver=sdrplay,serial=180903EF32,mode=MA,tuner=1",
            "RSPduo Tuner 2 SER#180903EF32":
                "soapy=,driver=sdrplay,serial=180903EF32,mode=SL,tuner=2",
            "70613472": "rtl=70613472",
        })

    def test_dedupe_and_skip_blank(self):
        self._set_rtl_index_map({})
        result = self.mod._build_dongle_arg_map([
            "70613472", "70613472", "", "  ", "80000003",
        ])
        self.assertEqual(
            result,
            {"70613472": "rtl=70613472", "80000003": "rtl=80000003"},
        )


if __name__ == "__main__":
    unittest.main()
