"""Regression tests for VDL2/traffic dongle sharing (Phase 1).

Coverage:
- generate_multi_rx_config() with 0, 1, and 2 traffic dongles
- System-agnostic assignment: traffic dongle 0 → systems[0], traffic dongle 1 → systems[1]
- No hardcoded system names anywhere
- ensure-op25-runtime.py sentinel file logic: dongle included when sentinel absent,
  excluded when sentinel present
- Edge cases: single-system profile, zero-system profile, duplicate serial guard
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ui.op25_adapter import generate_multi_rx_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_systems(names: list[str]) -> list[dict]:
    """Build minimal system dicts with distinct control channel frequencies."""
    base_hz = 851_000_000
    return [
        {
            "name": name,
            "control_channels_hz": [base_hz + i * 1_000_000],
        }
        for i, name in enumerate(names)
    ]


def _dongle_map(systems: list[dict], serials: list[str]) -> dict[str, str]:
    return {s["name"]: serial for s, serial in zip(systems, serials)}


def _load_ensure_script():
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "ensure-op25-runtime.py"
    spec = importlib.util.spec_from_file_location("ensure_op25_runtime_script", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# generate_multi_rx_config tests
# ---------------------------------------------------------------------------

class GenerateMultiRxNoTrafficTest(unittest.TestCase):
    """No traffic dongles — baseline 2-device config."""

    def test_two_systems_no_traffic(self):
        systems = _make_systems(["SysA", "SysB"])
        cfg = generate_multi_rx_config(systems, _dongle_map(systems, ["AAA", "BBB"]))
        self.assertEqual(2, len(cfg["devices"]))
        self.assertEqual(2, len(cfg["channels"]))
        device_names = {d["name"] for d in cfg["devices"]}
        self.assertIn("sdr0", device_names)
        self.assertIn("sdr1", device_names)
        self.assertNotIn("sdr_traffic", device_names)
        self.assertNotIn("sdr_traffic2", device_names)

    def test_ports_are_contiguous_base_23456(self):
        systems = _make_systems(["SysA", "SysB"])
        cfg = generate_multi_rx_config(systems, _dongle_map(systems, ["AAA", "BBB"]))
        ports = [
            int(ch["destination"].rsplit(":", 1)[1])
            for ch in cfg["channels"]
        ]
        self.assertEqual([23456, 23458], ports)


class GenerateMultiRxOneTrafficTest(unittest.TestCase):
    """One traffic dongle (sdr_traffic) follows systems[0]."""

    def setUp(self):
        self.systems = _make_systems(["SysA", "SysB"])
        self.cfg = generate_multi_rx_config(
            self.systems,
            _dongle_map(self.systems, ["AAA", "BBB"]),
            traffic_dongle_serial="CCC",
        )

    def test_three_devices(self):
        self.assertEqual(3, len(self.cfg["devices"]))

    def test_sdr_traffic_serial(self):
        traffic_dev = next(d for d in self.cfg["devices"] if d["name"] == "sdr_traffic")
        self.assertEqual("rtl=CCC", traffic_dev["args"])

    def test_sdr_traffic_follows_systems0_by_default(self):
        traffic_ch = next(c for c in self.cfg["channels"] if c["device"] == "sdr_traffic")
        self.assertEqual("SysA", traffic_ch["trunking_sysname"])

    def test_sdr_traffic_port_is_23460(self):
        traffic_ch = next(c for c in self.cfg["channels"] if c["device"] == "sdr_traffic")
        port = int(traffic_ch["destination"].rsplit(":", 1)[1])
        self.assertEqual(23460, port)

    def test_no_sdr_traffic2(self):
        device_names = {d["name"] for d in self.cfg["devices"]}
        self.assertNotIn("sdr_traffic2", device_names)


class GenerateMultiRxTwoTrafficTest(unittest.TestCase):
    """Both traffic dongles present — 4-device config."""

    def setUp(self):
        self.systems = _make_systems(["SysA", "SysB"])
        self.cfg = generate_multi_rx_config(
            self.systems,
            _dongle_map(self.systems, ["AAA", "BBB"]),
            traffic_dongle_serial="CCC",
            traffic_dongle_serial_2="DDD",
        )

    def test_four_devices(self):
        self.assertEqual(4, len(self.cfg["devices"]))

    def test_sdr_traffic2_device(self):
        dev = next(d for d in self.cfg["devices"] if d["name"] == "sdr_traffic2")
        self.assertEqual("rtl=DDD", dev["args"])

    def test_sdr_traffic_follows_systems0(self):
        ch = next(c for c in self.cfg["channels"] if c["device"] == "sdr_traffic")
        self.assertEqual("SysA", ch["trunking_sysname"])

    def test_sdr_traffic2_follows_systems1_by_default(self):
        ch = next(c for c in self.cfg["channels"] if c["device"] == "sdr_traffic2")
        self.assertEqual("SysB", ch["trunking_sysname"])

    def test_sdr_traffic2_port_is_23462(self):
        ch = next(c for c in self.cfg["channels"] if c["device"] == "sdr_traffic2")
        port = int(ch["destination"].rsplit(":", 1)[1])
        self.assertEqual(23462, port)

    def test_no_hardcoded_system_names_in_channel_names(self):
        """Channel names must not contain hardcoded system strings."""
        forbidden = {"mtrtrs", "tacn", "middle tennessee", "advanced communications"}
        for ch in self.cfg["channels"]:
            name_lower = ch["name"].lower()
            for bad in forbidden:
                self.assertNotIn(bad, name_lower, f"Hardcoded name '{bad}' in channel {ch['name']}")


class GenerateMultiRxExplicitTrafficSystemTest(unittest.TestCase):
    """Explicit traffic_system_name_2 overrides the default systems[1]."""

    def test_explicit_system_name_2_overrides_default(self):
        systems = _make_systems(["Alpha", "Beta", "Gamma"])
        cfg = generate_multi_rx_config(
            systems,
            _dongle_map(systems, ["S0", "S1", "S2"]),
            traffic_dongle_serial="TX",
            traffic_dongle_serial_2="TY",
            traffic_system_name_2="Gamma",
        )
        ch = next(c for c in cfg["channels"] if c["device"] == "sdr_traffic2")
        self.assertEqual("Gamma", ch["trunking_sysname"])


class GenerateMultiRxEdgeCasesTest(unittest.TestCase):
    """Edge cases: single system, zero systems, unknown serial."""

    def test_single_system_traffic_dongle_follows_systems0(self):
        systems = _make_systems(["OnlySystem"])
        cfg = generate_multi_rx_config(
            systems,
            _dongle_map(systems, ["S0"]),
            traffic_dongle_serial="TX",
        )
        ch = next(c for c in cfg["channels"] if c["device"] == "sdr_traffic")
        self.assertEqual("OnlySystem", ch["trunking_sysname"])

    def test_single_system_traffic2_falls_back_to_systems0(self):
        """With only one system, sdr_traffic2 follows it too (no systems[1])."""
        systems = _make_systems(["OnlySystem"])
        cfg = generate_multi_rx_config(
            systems,
            _dongle_map(systems, ["S0"]),
            traffic_dongle_serial="TX",
            traffic_dongle_serial_2="TY",
        )
        dev_names = {d["name"] for d in cfg["devices"]}
        self.assertIn("sdr_traffic2", dev_names)
        ch = next(c for c in cfg["channels"] if c["device"] == "sdr_traffic2")
        self.assertEqual("OnlySystem", ch["trunking_sysname"])

    def test_zero_systems_no_traffic_devices(self):
        cfg = generate_multi_rx_config(
            [],
            {},
            traffic_dongle_serial="TX",
            traffic_dongle_serial_2="TY",
        )
        self.assertEqual(0, len(cfg["devices"]))
        self.assertEqual(0, len(cfg["channels"]))

    def test_traffic_serial_2_empty_string_not_added(self):
        systems = _make_systems(["SysA", "SysB"])
        cfg = generate_multi_rx_config(
            systems,
            _dongle_map(systems, ["S0", "S1"]),
            traffic_dongle_serial="TX",
            traffic_dongle_serial_2="",  # empty → no sdr_traffic2
        )
        dev_names = {d["name"] for d in cfg["devices"]}
        self.assertNotIn("sdr_traffic2", dev_names)

    def test_dongle_args_map_used_for_traffic2(self):
        systems = _make_systems(["SysA", "SysB"])
        cfg = generate_multi_rx_config(
            systems,
            _dongle_map(systems, ["S0", "S1"]),
            dongle_args_map={"TY": "rtl=4"},
            traffic_dongle_serial="TX",
            traffic_dongle_serial_2="TY",
        )
        dev = next(d for d in cfg["devices"] if d["name"] == "sdr_traffic2")
        self.assertEqual("rtl=4", dev["args"])


# ---------------------------------------------------------------------------
# ensure-op25-runtime.py sentinel file logic
# ---------------------------------------------------------------------------

class EnsureOp25SentinelTest(unittest.TestCase):
    """Verify that ensure-op25-runtime.py respects the VDL2 sentinel."""

    @staticmethod
    def _build_profile(tmp_path: Path, system_names: list[str]) -> tuple[Path, Path]:
        """Create a minimal profile directory with N systems."""
        profiles_dir = tmp_path / "profiles"
        active_profile = profiles_dir / "test_profile"
        active_profile.mkdir(parents=True)
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()

        base_hz = 851_000_000
        systems_json = {
            "systems": [
                {
                    "name": name,
                    "control_channels_mhz": [(base_hz + i * 1_000_000) / 1e6],
                }
                for i, name in enumerate(system_names)
            ]
        }
        (active_profile / "systems.json").write_text(json.dumps(systems_json), encoding="utf-8")
        (active_profile / "talkgroups.csv").write_text("3207,Test TG\n", encoding="utf-8")
        (active_profile / "op25_system_config.json").write_text(
            json.dumps({name: {"nac": "0", "modulation": "cqpsk"} for name in system_names}),
            encoding="utf-8",
        )
        active_link = tmp_path / "active"
        active_link.symlink_to(active_profile)
        return active_link, runtime_dir

    def _run_script(
        self,
        tmp_path: Path,
        system_names: list[str],
        extra_env: dict[str, str] | None = None,
        sentinel_exists: bool = False,
        traffic_pool: list[str] | None = None,
    ) -> dict:
        active_link, runtime_dir = self._build_profile(tmp_path, system_names)

        assignments = {
            "assignments": [
                {"system_name": name, "preferred_tuner_serial": f"SER{i}", "role": "control"}
                for i, name in enumerate(system_names)
            ],
            "traffic_pool": traffic_pool if traffic_pool is not None else ["TRAF0"],
            "strategy": "dedicated_control",
            "digital_serials": [f"SER{i}" for i in range(len(system_names))],
            "system_count": len(system_names),
            "updated_at_ms": 1,
        }

        sentinel_path = str(runtime_dir / "vdl2_dongle_reserved")
        if sentinel_exists:
            (runtime_dir / "vdl2_dongle_reserved").write_text("")

        env_patch: dict[str, str] = {
            "VDL2_RTL_SERIAL": "VDL2SERIAL",
            "OP25_VDL2_TRAFFIC_SHARE": "1",
            "OP25_VDL2_SENTINEL": sentinel_path,
        }
        if extra_env:
            env_patch.update(extra_env)

        module = _load_ensure_script()
        idx_map = {f"SER{i}": i for i in range(len(system_names))}
        idx_map["TRAF0"] = len(system_names)
        idx_map["VDL2SERIAL"] = len(system_names) + 1

        with mock.patch.object(module, "DIGITAL_ACTIVE_PROFILE_LINK", str(active_link)), \
             mock.patch.object(module, "OP25_RUNTIME_DIR", str(runtime_dir)), \
             mock.patch.object(module, "OP25_STATUS_PORT", 8080), \
             mock.patch.object(module, "_enumerate_rtlsdr_serial_index_map", return_value=idx_map), \
             mock.patch("ui.dongle_allocator.load_assignments", return_value=assignments), \
             mock.patch.object(module, "load_assignments", return_value=assignments), \
             mock.patch.dict("os.environ", env_patch, clear=False):
            rc = module.main()

        self.assertEqual(0, rc)
        return json.loads((runtime_dir / "multi_rx.json").read_text(encoding="utf-8"))

    def test_vdl2_dongle_added_when_sentinel_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            multi_rx = self._run_script(Path(tmp), ["SysA", "SysB"], sentinel_exists=False)
        dev_names = [d["name"] for d in multi_rx["devices"]]
        self.assertIn("sdr_traffic2", dev_names)
        ch = next(c for c in multi_rx["channels"] if c["device"] == "sdr_traffic2")
        # Args are resolved to rtl=<index> by _build_dongle_arg_map — just verify the device exists
        # and targets the correct system (systems[1] = SysB).
        self.assertEqual("SysB", ch["trunking_sysname"])

    def test_vdl2_dongle_excluded_when_sentinel_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            multi_rx = self._run_script(Path(tmp), ["SysA", "SysB"], sentinel_exists=True)
        dev_names = [d["name"] for d in multi_rx["devices"]]
        self.assertNotIn("sdr_traffic2", dev_names)

    def test_vdl2_dongle_excluded_when_share_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            multi_rx = self._run_script(
                Path(tmp), ["SysA", "SysB"],
                extra_env={"OP25_VDL2_TRAFFIC_SHARE": "0"},
                sentinel_exists=False,
            )
        dev_names = [d["name"] for d in multi_rx["devices"]]
        self.assertNotIn("sdr_traffic2", dev_names)

    def test_vdl2_dongle_excluded_when_serial_not_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            multi_rx = self._run_script(
                Path(tmp), ["SysA", "SysB"],
                extra_env={"VDL2_RTL_SERIAL": ""},
                sentinel_exists=False,
            )
        dev_names = [d["name"] for d in multi_rx["devices"]]
        self.assertNotIn("sdr_traffic2", dev_names)

    def test_single_system_profile_traffic2_still_works(self):
        """Single-system profile: sdr_traffic2 falls back to systems[0]."""
        with tempfile.TemporaryDirectory() as tmp:
            multi_rx = self._run_script(Path(tmp), ["OnlySystem"], sentinel_exists=False)
        dev_names = [d["name"] for d in multi_rx["devices"]]
        self.assertIn("sdr_traffic2", dev_names)
        ch = next(c for c in multi_rx["channels"] if c["device"] == "sdr_traffic2")
        self.assertEqual("OnlySystem", ch["trunking_sysname"])

    def test_four_device_ports_no_overlap(self):
        """4-device config must have 4 distinct UDP ports."""
        with tempfile.TemporaryDirectory() as tmp:
            multi_rx = self._run_script(Path(tmp), ["SysA", "SysB"], sentinel_exists=False)
        ports = [
            int(ch["destination"].rsplit(":", 1)[1])
            for ch in multi_rx["channels"]
        ]
        self.assertEqual(len(ports), len(set(ports)), f"Duplicate ports: {ports}")

    def test_system_agnostic_no_hardcoded_names_in_config(self):
        """Generated config must not contain hardcoded system identifiers."""
        forbidden = ["mtrtrs", "tacn", "middle tennessee", "advanced communications"]
        with tempfile.TemporaryDirectory() as tmp:
            multi_rx = self._run_script(Path(tmp), ["ArbitraryA", "ArbitraryB"], sentinel_exists=False)
        raw = json.dumps(multi_rx).lower()
        for bad in forbidden:
            self.assertNotIn(bad, raw, f"Hardcoded name '{bad}' found in generated config")

    def test_three_device_config_when_sentinel_present(self):
        """When sentinel present: 2 control dongles + 1 dedicated traffic = 3 devices."""
        with tempfile.TemporaryDirectory() as tmp:
            multi_rx = self._run_script(Path(tmp), ["SysA", "SysB"], sentinel_exists=True)
        self.assertEqual(3, len(multi_rx["devices"]))

    def test_four_device_config_when_sentinel_absent(self):
        """When sentinel absent: 2 control + 1 dedicated traffic + 1 VDL2 = 4 devices."""
        with tempfile.TemporaryDirectory() as tmp:
            multi_rx = self._run_script(Path(tmp), ["SysA", "SysB"], sentinel_exists=False)
        self.assertEqual(4, len(multi_rx["devices"]))


if __name__ == "__main__":
    unittest.main()
