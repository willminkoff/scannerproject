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
from unittest import mock


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


class BuildRuntimeProcessPlansTests(unittest.TestCase):
    """Process-partitioning logic for RSPduo-anchored systems.

    Confirms the chirp pattern: each RSPduo-anchored system gets its own
    ``multi_rx.py`` process, even when two anchors share one physical
    RSPduo.  Single-process MA/SL fails because gr-osmosdr cannot attach
    Master + Slave from the same Python process; split-process MA/SL with
    a launch gap (``OP25_RSPDUO_LAUNCH_GAP_SEC``) is the working pattern.
    """

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_runtime_module()

    @staticmethod
    def _sys(name):
        return {"name": name}

    def test_two_anchors_same_device_split_into_two_processes(self):
        # MTRTRS on Tuner 1, TACN on Tuner 2 — same physical RSPduo.
        # Must split: in-process MA + SL fails with SelectDevice().
        systems = [self._sys("MTRTRS"), self._sys("TN TACN")]
        dongle_map = {
            "MTRTRS": "RSPduo Tuner 1 SER#180903EF32",
            "TN TACN": "RSPduo Tuner 2 SER#180903EF32",
        }
        plans = self.mod._build_runtime_process_plans(
            systems, dongle_map, traffic_followers=[]
        )
        self.assertEqual(len(plans), 2, f"expected 2 plans, got {plans}")
        names_per_plan = [{s["name"] for s in p["systems"]} for p in plans]
        self.assertEqual(len(names_per_plan[0]), 1)
        self.assertEqual(len(names_per_plan[1]), 1)
        self.assertEqual(set().union(*names_per_plan), {"MTRTRS", "TN TACN"})

    def test_different_device_anchors_split_into_two_processes(self):
        # MTRTRS on RSPduo A Tuner 1, TACN on RSPduo B Tuner 1 — different
        # physical devices → one process per device.
        systems = [self._sys("MTRTRS"), self._sys("TN TACN")]
        dongle_map = {
            "MTRTRS": "RSPduo Tuner 1 SER#180903EF32",
            "TN TACN": "RSPduo Tuner 1 SER#1809063632",
        }
        plans = self.mod._build_runtime_process_plans(
            systems, dongle_map, traffic_followers=[]
        )
        self.assertEqual(len(plans), 2, f"expected 2 plans, got {plans}")
        names_per_plan = [{s["name"] for s in p["systems"]} for p in plans]
        self.assertEqual(len(names_per_plan[0]), 1)
        self.assertEqual(len(names_per_plan[1]), 1)
        self.assertEqual(set().union(*names_per_plan), {"MTRTRS", "TN TACN"})

    def test_single_anchor_is_one_process(self):
        systems = [self._sys("MTRTRS")]
        dongle_map = {"MTRTRS": "RSPduo Tuner 1 SER#180903EF32"}
        plans = self.mod._build_runtime_process_plans(
            systems, dongle_map, traffic_followers=[]
        )
        self.assertEqual(len(plans), 1)
        self.assertEqual([s["name"] for s in plans[0]["systems"]], ["MTRTRS"])

    def test_split_disabled_drops_same_box_tuner_2_anchor(self):
        # Regression for the 2026-06-12 escape-hatch bug: when split=0
        # with two anchors on the same physical RSPduo, the prior code
        # packed both into one process and triggered SelectDevice().
        # The fix degrades to the pre-2026-06-12 "one tuner per box"
        # behavior: keep the Tuner 1 anchor, drop the Tuner 2 anchor,
        # so the single process never opens MA+SL on the same device.
        systems = [self._sys("MTRTRS"), self._sys("TN TACN")]
        dongle_map = {
            "MTRTRS": "RSPduo Tuner 1 SER#180903EF32",
            "TN TACN": "RSPduo Tuner 2 SER#180903EF32",
        }
        with mock.patch.dict(
            os.environ, {"OP25_RSPDUO_SPLIT_PROCESSES": "0"}
        ):
            plans = self.mod._build_runtime_process_plans(
                systems, dongle_map, traffic_followers=[]
            )
        self.assertEqual(len(plans), 1)
        # MTRTRS (Tuner 1) survives; TACN (Tuner 2 of the same box) is dropped.
        self.assertEqual(
            {s["name"] for s in plans[0]["systems"]}, {"MTRTRS"}
        )

    def test_split_disabled_two_distinct_devices_both_kept(self):
        # When split is disabled but the two anchors are on DIFFERENT
        # physical RSPduos, both can safely live in one process (each
        # opens its own device).  The same-box drop logic only fires
        # when anchors collide on one physical device.
        systems = [self._sys("MTRTRS"), self._sys("TN TACN")]
        dongle_map = {
            "MTRTRS": "RSPduo Tuner 1 SER#180903EF32",
            "TN TACN": "RSPduo Tuner 1 SER#1809063632",
        }
        with mock.patch.dict(
            os.environ, {"OP25_RSPDUO_SPLIT_PROCESSES": "0"}
        ):
            plans = self.mod._build_runtime_process_plans(
                systems, dongle_map, traffic_followers=[]
            )
        self.assertEqual(len(plans), 1)
        self.assertEqual(
            {s["name"] for s in plans[0]["systems"]}, {"MTRTRS", "TN TACN"}
        )


class DetectTrafficDongleTests(unittest.TestCase):
    """``_detect_traffic_dongle`` must skip RSPduo tuners.

    Regression guard for P0-2 (2026-06-12): with one system + one RSPduo
    where both tuners are enumerated, the prior code returned the orphan
    RSPduo Tuner 2 as the traffic follower.  In single-system mode that
    follower lives in the SAME multi_rx.py as the control channel, so
    gr-osmosdr tried to open Master (Tuner 1, control) + Slave (Tuner 2,
    follower) on the same physical device and crashed with
    SelectDevice().  Traffic followers must be RTLs.
    """

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_runtime_module()

    def test_skips_rspduo_tuner_in_traffic_pool(self):
        assignments = {
            "traffic_pool": ["RSPduo Tuner 2 SER#180903EF32", "70613472"],
        }
        self.assertEqual(
            self.mod._detect_traffic_dongle(assignments), "70613472"
        )

    def test_returns_empty_when_pool_is_all_rspduo(self):
        assignments = {
            "traffic_pool": [
                "RSPduo Tuner 2 SER#180903EF32",
                "RSPduo Tuner 2 SER#1809063632",
            ],
        }
        self.assertEqual(self.mod._detect_traffic_dongle(assignments), "")

    def test_handles_empty_pool(self):
        self.assertEqual(
            self.mod._detect_traffic_dongle({"traffic_pool": []}), ""
        )

    def test_handles_none_assignments(self):
        self.assertEqual(self.mod._detect_traffic_dongle(None), "")


if __name__ == "__main__":
    unittest.main()
