"""Tests for the OP25 backend adapter."""
import json
import os
import tempfile
import unittest
from unittest import mock

from ui.op25_adapter import (
    Op25Adapter,
    _parse_control_channels,
    _read_system_definitions,
    _read_talkgroup_labels,
    generate_trunk_tsv,
    generate_tgid_tags_tsv,
)


class ParseControlChannelsTests(unittest.TestCase):
    def test_mhz_list(self):
        self.assertEqual(
            [851012500, 856462500],
            _parse_control_channels([851.0125, 856.4625]),
        )

    def test_hz_list(self):
        self.assertEqual(
            [851012500, 856462500],
            _parse_control_channels([851012500, 856462500]),
        )

    def test_string_list(self):
        self.assertEqual(
            [851012500],
            _parse_control_channels(["851.0125"]),
        )

    def test_comma_separated_string(self):
        self.assertEqual(
            [851012500, 856462500],
            _parse_control_channels("851.0125, 856.4625"),
        )

    def test_empty(self):
        self.assertEqual([], _parse_control_channels(None))
        self.assertEqual([], _parse_control_channels([]))
        self.assertEqual([], _parse_control_channels(""))

    def test_deduplication(self):
        self.assertEqual(
            [851012500],
            _parse_control_channels([851.0125, 851.0125]),
        )


class ReadSystemDefinitionsTests(unittest.TestCase):
    def test_reads_systems_json(self):
        with tempfile.TemporaryDirectory() as d:
            data = {
                "systems": [
                    {"name": "TACN", "control_channels_mhz": [851.0125, 856.4625]},
                    {"name": "MTRTRS", "control_channels_mhz": [855.9125]},
                ]
            }
            with open(os.path.join(d, "systems.json"), "w") as f:
                json.dump(data, f)
            systems = _read_system_definitions(d)
            self.assertEqual(2, len(systems))
            self.assertEqual("TACN", systems[0]["name"])
            self.assertEqual([851012500, 856462500], systems[0]["control_channels_hz"])
            self.assertEqual("MTRTRS", systems[1]["name"])

    def test_empty_dir(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual([], _read_system_definitions(d))

    def test_deduplicates_names(self):
        with tempfile.TemporaryDirectory() as d:
            data = {
                "systems": [
                    {"name": "SYS1", "control_channels_mhz": [851.0]},
                    {"name": "SYS1", "control_channels_mhz": [852.0]},
                ]
            }
            with open(os.path.join(d, "systems.json"), "w") as f:
                json.dump(data, f)
            self.assertEqual(1, len(_read_system_definitions(d)))


class ReadTalkgroupLabelsTests(unittest.TestCase):
    def test_reads_csv(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "talkgroups.csv"), "w") as f:
                f.write("# comment\n12345,Fire Dispatch\n54321,EMS\n")
            labels = _read_talkgroup_labels(d)
            self.assertEqual("Fire Dispatch", labels["12345"])
            self.assertEqual("EMS", labels["54321"])

    def test_reads_tsv(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "talkgroups.tsv"), "w") as f:
                f.write("12345\tFire Dispatch\n")
            labels = _read_talkgroup_labels(d)
            self.assertEqual("Fire Dispatch", labels["12345"])


class GenerateTrunkTsvTests(unittest.TestCase):
    def test_basic_generation(self):
        systems = [
            {"name": "SYS_A", "control_channels_hz": [851012500, 856462500]},
            {"name": "SYS_B", "control_channels_hz": [855912500]},
        ]
        tsv = generate_trunk_tsv(systems)
        lines = tsv.strip().split("\n")
        self.assertEqual(2, len(lines))
        parts_a = lines[0].split("\t")
        self.assertEqual("SYS_A", parts_a[0])
        self.assertEqual("851012500", parts_a[1])
        self.assertEqual("cqpsk", parts_a[4])
        parts_b = lines[1].split("\t")
        self.assertEqual("SYS_B", parts_b[0])
        self.assertEqual("855912500", parts_b[1])

    def test_dongle_mapping(self):
        systems = [
            {"name": "SYS_A", "control_channels_hz": [851012500]},
        ]
        assignments = {
            "assignments": [
                {"system_name": "SYS_A", "preferred_tuner_serial": "14306619"},
            ],
        }
        tsv = generate_trunk_tsv(systems, dongle_assignments=assignments)
        parts = tsv.strip().split("\t")
        self.assertEqual("rtl:14306619", parts[-1])

    def test_op25_overrides(self):
        systems = [
            {"name": "SYS_A", "control_channels_hz": [851012500]},
        ]
        overrides = {
            "SYS_A": {"nac": "0x293", "modulation": "c4fm"},
        }
        tsv = generate_trunk_tsv(systems, op25_overrides=overrides)
        parts = tsv.strip().split("\t")
        self.assertEqual("0x293", parts[3])
        self.assertEqual("c4fm", parts[4])

    def test_empty_systems(self):
        self.assertEqual("", generate_trunk_tsv([]))


class GenerateTgidTagsTsvTests(unittest.TestCase):
    def test_basic(self):
        labels = {"100": "Police", "200": "Fire"}
        tsv = generate_tgid_tags_tsv(labels)
        lines = tsv.strip().split("\n")
        self.assertEqual(2, len(lines))
        self.assertIn("100\tPolice", lines[0])
        self.assertIn("200\tFire", lines[1])


class Op25AdapterPropertiesTests(unittest.TestCase):
    def test_supports_multi_system(self):
        with mock.patch("ui.op25_adapter.validate_digital_service_name", return_value=True):
            adapter = Op25Adapter()
            self.assertTrue(adapter.supports_multi_system)

    def test_name(self):
        with mock.patch("ui.op25_adapter.validate_digital_service_name", return_value=True):
            adapter = Op25Adapter()
            self.assertEqual("op25", adapter.name)

    def test_apply_system_is_noop(self):
        with mock.patch("ui.op25_adapter.validate_digital_service_name", return_value=True):
            adapter = Op25Adapter()
            ok, err, changed = adapter.apply_system("SYS_A", [851012500])
            self.assertTrue(ok)
            self.assertEqual("", err)
            self.assertFalse(changed)

    def test_retune_not_supported(self):
        with mock.patch("ui.op25_adapter.validate_digital_service_name", return_value=True):
            adapter = Op25Adapter()
            ok, err = adapter.retune_control_frequency(851.0125)
            self.assertFalse(ok)
            self.assertIn("not supported", err)


class Op25AdapterLogParsingTests(unittest.TestCase):
    def test_parse_tsbk_line(self):
        with mock.patch("ui.op25_adapter.validate_digital_service_name", return_value=True):
            adapter = Op25Adapter()
            event = adapter._parse_log_line(
                "2026-03-26 14:05:32 tsbk_handler(): cc 851012500 tg 12345 freq 855462500"
            )
            self.assertIsNotNone(event)
            self.assertEqual("12345", event["tgid"])
            self.assertEqual(855462500, event["frequency_hz"])
            self.assertEqual("P25", event["mode"])

    def test_parse_voice_grant(self):
        with mock.patch("ui.op25_adapter.validate_digital_service_name", return_value=True):
            adapter = Op25Adapter()
            event = adapter._parse_log_line(
                "2026-03-26 14:05:32 voice grant: tg 54321 freq 856737500"
            )
            self.assertIsNotNone(event)
            self.assertEqual("54321", event["tgid"])

    def test_parse_non_event_line(self):
        with mock.patch("ui.op25_adapter.validate_digital_service_name", return_value=True):
            adapter = Op25Adapter()
            self.assertIsNone(adapter._parse_log_line("INFO: starting up"))


class Op25AdapterActivateSystemsTests(unittest.TestCase):
    def test_activate_writes_config_and_restarts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = os.path.join(tmpdir, "profiles", "test_profile")
            os.makedirs(profile_dir)
            runtime_dir = os.path.join(tmpdir, "runtime")
            link_path = os.path.join(tmpdir, "active")
            os.symlink(profile_dir, link_path)

            with mock.patch("ui.op25_adapter.validate_digital_service_name", return_value=True):
                adapter = Op25Adapter()
                adapter._active_link = link_path
                adapter._profiles_dir = os.path.join(tmpdir, "profiles")
                adapter._runtime_dir = runtime_dir

                with mock.patch.object(adapter, "isActive", return_value=True), \
                     mock.patch.object(adapter, "restart", return_value=(True, "")):
                    systems = [
                        {"name": "SYS_A", "control_channels_hz": [851012500]},
                        {"name": "SYS_B", "control_channels_hz": [855912500]},
                    ]
                    ok, err, changed = adapter.activate_systems(systems)

            self.assertTrue(ok)
            self.assertTrue(changed)
            trunk_path = os.path.join(runtime_dir, "trunk.tsv")
            self.assertTrue(os.path.isfile(trunk_path))
            with open(trunk_path) as fh:
                content = fh.read()
            self.assertIn("SYS_A", content)
            self.assertIn("SYS_B", content)
            self.assertIn("851012500", content)


if __name__ == "__main__":
    unittest.main()
