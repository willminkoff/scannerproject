"""Tests for the OP25 backend adapter."""
import json
import os
import tempfile
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ui.op25_adapter import (
    Op25Adapter,
    _default_gain_mode_for_args,
    _default_gains_for_args,
    _parse_control_channels,
    _read_system_definitions,
    _read_talkgroup_labels,
    _multi_rx_udp_ports,
    _resolve_gain_mode_for_args,
    _resolve_gains_for_args,
    generate_multi_rx_config,
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
    def test_reads_legacy_systems_json(self):
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

    def test_reads_site_aware_systems_json_and_flattens_enabled_sites(self):
        with tempfile.TemporaryDirectory() as d:
            data = {
                "systems": [
                    {
                        "name": "MTRTRS",
                        "system_id": "7078",
                        "sites": [
                            {
                                "site_id": "18863",
                                "site_name": "Davidson County Simulcast",
                                "control_channels_hz": [856937500, 857437500],
                                "enabled": True,
                            },
                            {
                                "site_id": "41154",
                                "site_name": "Davidson County Services",
                                "control_channels_hz": [855912500, 856937500],
                                "enabled": False,
                            },
                        ],
                    }
                ]
            }
            with open(os.path.join(d, "systems.json"), "w") as f:
                json.dump(data, f)
            systems = _read_system_definitions(d)
            self.assertEqual(
                [{"name": "MTRTRS", "control_channels_hz": [856937500, 857437500]}],
                systems,
            )

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
        self.assertEqual(3, len(lines))
        header = lines[0].split("\t")
        self.assertEqual('"Sysname"', header[0])
        parts_a = lines[1].split("\t")
        self.assertEqual('"SYS_A"', parts_a[0])
        self.assertEqual('"851.01250,856.46250"', parts_a[1])
        self.assertEqual('"cqpsk"', parts_a[4])
        parts_b = lines[2].split("\t")
        self.assertEqual('"SYS_B"', parts_b[0])
        self.assertEqual('"855.91250"', parts_b[1])

    def test_op25_overrides(self):
        systems = [
            {"name": "SYS_A", "control_channels_hz": [851012500]},
        ]
        overrides = {
            "SYS_A": {"nac": "0x293", "modulation": "c4fm"},
        }
        tsv = generate_trunk_tsv(systems, op25_overrides=overrides)
        parts = tsv.strip().split("\n")[1].split("\t")
        self.assertEqual('"0x293"', parts[3])
        self.assertEqual('"c4fm"', parts[4])

    def test_tgid_tags_path(self):
        systems = [
            {"name": "SYS_A", "control_channels_hz": [851012500]},
        ]
        tsv = generate_trunk_tsv(systems, tgid_tags_path="/tmp/tgid_tags.tsv")
        parts = tsv.strip().split("\n")[1].split("\t")
        self.assertEqual('"/tmp/tgid_tags.tsv"', parts[5])

    def test_empty_systems(self):
        self.assertEqual("", generate_trunk_tsv([]))


class GenerateMultiRxConfigTests(unittest.TestCase):
    def test_basic_generation(self):
        systems = [
            {"name": "SYS_A", "control_channels_hz": [851012500]},
            {"name": "SYS_B", "control_channels_hz": [855912500]},
        ]
        config = generate_multi_rx_config(
            systems,
            {"SYS_A": "14306619", "SYS_B": "56919602"},
            traffic_dongle_serial="00000001",
            traffic_system_name="SYS_A",
            tgid_tags_path="/run/scannerproject/op25/tgid_tags.tsv",
            http_port=8080,
        )
        self.assertEqual(3, len(config["devices"]))
        self.assertEqual(3, len(config["channels"]))
        self.assertEqual("rtl=14306619", config["devices"][0]["args"])
        self.assertEqual("rtl=00000001", config["devices"][2]["args"])
        self.assertEqual("SYS_A", config["channels"][0]["trunking_sysname"])
        self.assertEqual("SYS_A", config["channels"][2]["trunking_sysname"])
        self.assertEqual(
            "/run/scannerproject/op25/tgid_tags.tsv",
            config["trunking"]["chans"][0]["tgid_tags_file"],
        )

    def test_udp_ports(self):
        config = {
            "channels": [
                {"destination": "udp://127.0.0.1:23456"},
                {"destination": "udp://127.0.0.1:23458"},
            ]
        }
        self.assertEqual([23456, 23458], _multi_rx_udp_ports(config))

    def test_rspduo_uses_sdrplay_native_gain_elements(self):
        """RSPduo args must produce IFGR/RFGR (not LNA) gains and gain_mode=False.

        Setting ``gains: "LNA:36"`` on a SoapySDRPlay3 source silently no-ops
        because the driver has no element by that name, leaving the device
        at its default-AGC setpoint.  This is the regression that prevented
        control-channel lock with the dual-tuner RSPduo split runtime.
        """
        systems = [
            {"name": "TACN", "control_channels_hz": [769456250]},
            {"name": "MTRTRS", "control_channels_hz": [856487500]},
        ]
        rspduo_args = {
            "RSPduo Tuner 1 SER#180903EF32": "soapy=,driver=sdrplay,serial=180903EF32,mode=MA,tuner=1",
            "RSPduo Tuner 2 SER#180903EF32": "soapy=,driver=sdrplay,serial=180903EF32,mode=SL,tuner=2",
        }
        config = generate_multi_rx_config(
            systems,
            {
                "TACN": "RSPduo Tuner 1 SER#180903EF32",
                "MTRTRS": "RSPduo Tuner 2 SER#180903EF32",
            },
            dongle_args_map=rspduo_args,
            http_port=8080,
        )
        self.assertEqual(2, len(config["devices"]))
        for dev in config["devices"]:
            self.assertIn("driver=sdrplay", dev["args"])
            self.assertEqual("IFGR:40,RFGR:0", dev["gains"])
            self.assertFalse(dev["gain_mode"])
            self.assertNotIn("LNA", dev["gains"])

    def test_rtl_keeps_legacy_lna_gain_default(self):
        """RTL-SDR continues to receive LNA:36 + gain_mode=True (regression guard)."""
        systems = [{"name": "SYS_A", "control_channels_hz": [851012500]}]
        config = generate_multi_rx_config(
            systems,
            {"SYS_A": "14306619"},
            http_port=8080,
        )
        dev = config["devices"][0]
        self.assertEqual("rtl=14306619", dev["args"])
        self.assertEqual("LNA:36", dev["gains"])
        self.assertTrue(dev["gain_mode"])

    def test_per_system_gain_override_wins_over_default(self):
        """``op25_system_config.json`` -> gains/gain_mode still wins."""
        systems = [{"name": "TACN", "control_channels_hz": [769456250]}]
        rspduo_args = {
            "RSPduo Tuner 1 SER#180903EF32": "soapy=,driver=sdrplay,serial=180903EF32,mode=MA,tuner=1",
        }
        config = generate_multi_rx_config(
            systems,
            {"TACN": "RSPduo Tuner 1 SER#180903EF32"},
            dongle_args_map=rspduo_args,
            op25_overrides={"TACN": {"gains": "IFGR:25,RFGR:1", "gain_mode": True}},
            http_port=8080,
        )
        dev = config["devices"][0]
        self.assertEqual("IFGR:25,RFGR:1", dev["gains"])
        self.assertTrue(dev["gain_mode"])

    def test_legacy_lna_override_dropped_on_rspduo(self):
        """Legacy ``"LNA:42"`` from a pre-RSPduo profile must not silently
        no-op on a SoapySDRPlay3 device — the override is dropped and the
        SDRplay default is used instead.

        Regression: the live ``op25_system_config.json`` carried a
        ``"gains": "LNA:42"`` override from RTL-SDR days; after the
        dongle behind TACN/MTRTRS migrated to RSPduo, multi_rx applied
        ``LNA:42`` verbatim, the SoapySDRPlay3 plugin had no element by
        that name, the call became a no-op, and the device ran at the
        driver's default-AGC setpoint — too low to recover the control
        channel.
        """
        systems = [{"name": "TACN", "control_channels_hz": [769456250]}]
        rspduo_args = {
            "RSPduo Tuner 1 SER#180903EF32": "soapy=,driver=sdrplay,serial=180903EF32,mode=MA,tuner=1",
        }
        config = generate_multi_rx_config(
            systems,
            {"TACN": "RSPduo Tuner 1 SER#180903EF32"},
            dongle_args_map=rspduo_args,
            op25_overrides={"TACN": {"gains": "LNA:42"}},
            http_port=8080,
        )
        dev = config["devices"][0]
        self.assertEqual("IFGR:40,RFGR:0", dev["gains"])
        self.assertNotIn("LNA", dev["gains"])
        self.assertFalse(dev["gain_mode"])

    def test_mixed_override_keeps_only_valid_elements(self):
        """A mixed ``LNA:42,IFGR:25`` override on SDRplay drops the LNA part
        and keeps the IFGR part."""
        systems = [{"name": "TACN", "control_channels_hz": [769456250]}]
        rspduo_args = {
            "RSPduo Tuner 1 SER#180903EF32": "soapy=,driver=sdrplay,serial=180903EF32,mode=MA,tuner=1",
        }
        config = generate_multi_rx_config(
            systems,
            {"TACN": "RSPduo Tuner 1 SER#180903EF32"},
            dongle_args_map=rspduo_args,
            op25_overrides={"TACN": {"gains": "LNA:42,IFGR:25"}},
            http_port=8080,
        )
        self.assertEqual("IFGR:25", config["devices"][0]["gains"])

    def test_traffic_follower_gets_backend_appropriate_default(self):
        """The traffic follower picks RTL vs SDRplay defaults from its own args."""
        systems = [{"name": "TACN", "control_channels_hz": [769456250]}]
        rspduo_args = {
            "RSPduo Tuner 1 SER#180903EF32": "soapy=,driver=sdrplay,serial=180903EF32,mode=MA,tuner=1",
        }
        config = generate_multi_rx_config(
            systems,
            {"TACN": "RSPduo Tuner 1 SER#180903EF32"},
            dongle_args_map=rspduo_args,
            traffic_dongle_serial="70613472",  # bare RTL serial
            traffic_system_name="TACN",
            http_port=8080,
        )
        # Index 0: control source on RSPduo MA -> SDRplay defaults.
        self.assertEqual("IFGR:40,RFGR:0", config["devices"][0]["gains"])
        self.assertFalse(config["devices"][0]["gain_mode"])
        # Index 1: traffic follower on bare RTL -> legacy defaults.
        self.assertEqual("rtl=70613472", config["devices"][1]["args"])
        self.assertEqual("LNA:36", config["devices"][1]["gains"])
        self.assertTrue(config["devices"][1]["gain_mode"])


class DefaultGainsForArgsTests(unittest.TestCase):
    def test_rtl_serial(self):
        self.assertEqual("LNA:36", _default_gains_for_args("rtl=70613472"))

    def test_rtl_index(self):
        self.assertEqual("LNA:36", _default_gains_for_args("rtl=0"))

    def test_rspduo_master(self):
        self.assertEqual(
            "IFGR:40,RFGR:0",
            _default_gains_for_args(
                "soapy=,driver=sdrplay,serial=180903EF32,mode=MA,tuner=1"
            ),
        )

    def test_rspduo_slave(self):
        self.assertEqual(
            "IFGR:40,RFGR:0",
            _default_gains_for_args(
                "soapy=,driver=sdrplay,serial=180903EF32,mode=SL,tuner=2"
            ),
        )

    def test_rspduo_single_tuner(self):
        self.assertEqual(
            "IFGR:40,RFGR:0",
            _default_gains_for_args(
                "soapy=,driver=sdrplay,serial=180903EF32,mode=ST,tuner=1"
            ),
        )

    def test_empty_args_falls_back_to_rtl(self):
        self.assertEqual("LNA:36", _default_gains_for_args(""))


class DefaultGainModeForArgsTests(unittest.TestCase):
    def test_rtl_keeps_agc_on(self):
        self.assertTrue(_default_gain_mode_for_args("rtl=70613472"))

    def test_sdrplay_disables_agc(self):
        self.assertFalse(
            _default_gain_mode_for_args(
                "soapy=,driver=sdrplay,serial=180903EF32,mode=MA,tuner=1"
            )
        )


class ResolveGainsForArgsTests(unittest.TestCase):
    RSPDUO = "soapy=,driver=sdrplay,serial=180903EF32,mode=MA,tuner=1"
    RTL = "rtl=70613472"

    def test_empty_override_returns_default(self):
        self.assertEqual("LNA:36", _resolve_gains_for_args(self.RTL, None))
        self.assertEqual("LNA:36", _resolve_gains_for_args(self.RTL, ""))
        self.assertEqual("IFGR:40,RFGR:0", _resolve_gains_for_args(self.RSPDUO, ""))

    def test_lna_override_dropped_on_sdrplay(self):
        self.assertEqual(
            "IFGR:40,RFGR:0",
            _resolve_gains_for_args(self.RSPDUO, "LNA:42"),
        )

    def test_lna_override_kept_on_rtl(self):
        self.assertEqual("LNA:42", _resolve_gains_for_args(self.RTL, "LNA:42"))

    def test_ifgr_override_kept_on_sdrplay(self):
        self.assertEqual(
            "IFGR:25,RFGR:0",
            _resolve_gains_for_args(self.RSPDUO, "IFGR:25,RFGR:0"),
        )

    def test_ifgr_override_dropped_on_rtl(self):
        # IFGR is meaningless to the RTL-SDR osmosdr source; it must be
        # dropped and the RTL default used instead.
        self.assertEqual(
            "LNA:36",
            _resolve_gains_for_args(self.RTL, "IFGR:25"),
        )

    def test_mixed_override_keeps_only_valid_elements(self):
        self.assertEqual(
            "IFGR:25",
            _resolve_gains_for_args(self.RSPDUO, "LNA:42,IFGR:25"),
        )

    def test_whitespace_and_case_in_override(self):
        self.assertEqual(
            "IFGR:25,RFGR:1",
            _resolve_gains_for_args(self.RSPDUO, "  ifgr:25 , rfgr:1 "),
        )

    def test_malformed_override_falls_back_to_default(self):
        # No "Name:Value" parts present at all → use default.
        self.assertEqual(
            "IFGR:40,RFGR:0",
            _resolve_gains_for_args(self.RSPDUO, "garbage,nonsense"),
        )


class ResolveGainModeForArgsTests(unittest.TestCase):
    RSPDUO = "soapy=,driver=sdrplay,serial=180903EF32,mode=MA,tuner=1"
    RTL = "rtl=70613472"

    def test_no_override_uses_default(self):
        self.assertTrue(_resolve_gain_mode_for_args(self.RTL, None))
        self.assertFalse(_resolve_gain_mode_for_args(self.RSPDUO, None))

    def test_explicit_override_wins(self):
        # User can force AGC on for SDRplay if they want.
        self.assertTrue(_resolve_gain_mode_for_args(self.RSPDUO, True))
        self.assertFalse(_resolve_gain_mode_for_args(self.RTL, False))


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
            self.assertIn("851.01250", content)


class Op25AdapterEventExtractionTests(unittest.TestCase):
    def test_events_from_status_merges_call_log_and_channel_update(self):
        adapter = Op25Adapter()
        now_sec = time.time()
        status = {
            "call_log": [
                {
                    "tgid": "47008",
                    "tgtag": "District 3: Dispatch 1",
                    "freq": 769831250,
                    "time": now_sec,
                    "system": "6355:1",
                },
            ],
            "channel_update": {
                "0": {
                    "tgid": "47008",
                    "tag": "District 3: Dispatch 1",
                    "freq": 769831250,
                    "system": "6355:1",
                    "mode": "P25",
                },
                "1": {
                    "tgid": "3207",
                    "tag": "",
                    "freq": 855912500,
                    "system": "7078:2",
                    "mode": "P25",
                },
                "channels": ["0", "1"],
            },
        }

        with mock.patch.object(adapter, "_resolve_tg_label", side_effect=lambda tgid: {
            "3207": "Police Dispatch",
            "47008": "District 3: Dispatch 1",
        }.get(str(tgid), "")):
            events = adapter._events_from_status(status)

        self.assertEqual(2, len(events))
        by_tgid = {str(event.get("tgid")): event for event in events}
        self.assertIn("47008", by_tgid)
        self.assertIn("3207", by_tgid)
        self.assertEqual("6355:1", by_tgid["47008"]["system"])
        self.assertEqual("7078:2", by_tgid["3207"]["system"])
        self.assertEqual("Police Dispatch", by_tgid["3207"]["label"])
        self.assertEqual(855912500, by_tgid["3207"]["frequency_hz"])

    def test_poll_status_merges_multi_instance_ports(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir)
            (runtime_dir / "instances.json").write_text(
                json.dumps(
                    [
                        {"http_status_port": 8080, "system_name": "SYS_A", "udp_audio_port": 23456},
                        {"http_status_port": 8081, "system_name": "SYS_B", "udp_audio_port": 23458},
                    ]
                ),
                encoding="utf-8",
            )

            with mock.patch("ui.op25_adapter.validate_digital_service_name", return_value=True):
                adapter = Op25Adapter()
            adapter._runtime_dir = str(runtime_dir)

            now_sec = time.time()
            port_status = {
                8080: {
                    "trunk_update": {
                        "systems": {
                            "SYS_A": {
                                "system": "SYS_A",
                                "last_tsbk": now_sec,
                                "rxchan": 851012500,
                            }
                        }
                    },
                    "channel_update": {
                        "0": {
                            "tgid": "100",
                            "tag": "Dispatch A",
                            "freq": 851012500,
                            "system": "SYS_A",
                            "mode": "P25",
                        }
                    },
                    "call_log": [
                        {"tgid": "100", "tgtag": "Dispatch A", "time": now_sec, "system": "SYS_A"}
                    ],
                },
                8081: {
                    "trunk_update": {
                        "systems": {
                            "SYS_B": {
                                "system": "SYS_B",
                                "last_tsbk": now_sec,
                                "rxchan": 852012500,
                            }
                        }
                    },
                    "channel_update": {
                        "0": {
                            "tgid": "200",
                            "tag": "Dispatch B",
                            "freq": 852012500,
                            "system": "SYS_B",
                            "mode": "P25",
                        }
                    },
                    "call_log": [
                        {"tgid": "200", "tgtag": "Dispatch B", "time": now_sec, "system": "SYS_B"}
                    ],
                },
            }

            with mock.patch.object(
                adapter,
                "_fetch_update_json",
                side_effect=lambda *, port=None: port_status.get(int(port or 0), {}),
            ), mock.patch.object(adapter, "_fetch_json", return_value={}):
                status = adapter._poll_op25_status()

        trunk_systems = status["trunk_update"]["systems"]
        self.assertEqual({"SYS_A", "SYS_B"}, set(trunk_systems.keys()))
        self.assertEqual([8080, 8081], status["op25_instance_ports"])
        self.assertTrue(status["control_channel_locked"])
        self.assertEqual(2, len([row for row in status["channel_update"].values() if isinstance(row, dict)]))

    def test_events_from_status_include_verbose_grouped_metadata(self):
        adapter = Op25Adapter()
        now_sec = time.time()
        status = {
            "call_log": [
                {
                    "tgid": "47008",
                    "tgtag": "District 3: Dispatch 1",
                    "freq": 769831250,
                    "time": now_sec,
                    "system": "6355:1",
                },
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir)
            (profile_dir / "talkgroups_with_group.csv").write_text(
                "Group,DEC,HEX,Mode,Alpha Tag,Description,Tag\n"
                "\"TN - State Highway Patrol, District 3: Nashville\",47008,b7a0,D,District 3: Dispatch 1,District 3: Dispatch 1,\n",
                encoding="utf-8",
            )
            with mock.patch.object(adapter, "_read_active_profile_dir", return_value=str(profile_dir)):
                events = adapter._events_from_status(status)

        self.assertEqual(1, len(events))
        event = events[0]
        self.assertEqual("District 3: Dispatch 1", event["label"])
        self.assertEqual("TN - State Highway Patrol, District 3: Nashville", event["agency"])
        self.assertEqual("District 3: Dispatch 1", event["department"])
        self.assertEqual(
            "TN - State Highway Patrol, District 3: Nashville - District 3: Dispatch 1",
            event["label_full"],
        )


if __name__ == "__main__":
    unittest.main()
