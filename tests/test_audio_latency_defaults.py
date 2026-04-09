import unittest
import tempfile
import os
import xml.etree.ElementTree as ET
from unittest import mock

from ui import vlc
from ui import digital
import combined_config


class CombinedConfigLatencyDefaultsTests(unittest.TestCase):
    def test_build_combined_config_defaults_to_low_latency_bitrate(self):
        profile = (
            "devices:\n"
            "({\n"
            "  type = \"rtlsdr\";\n"
            "  index = 1;\n"
            "  channels:\n"
            "  (\n"
            "    {\n"
            "      freqs = (118.6000);\n"
            "      outputs:\n"
            "      (\n"
            "        {\n"
            "          type = \"icecast\";\n"
            "          mountpoint = \"ANALOG.mp3\";\n"
            "          bitrate = 32;\n"
            "        }\n"
            "      );\n"
            "    }\n"
            "  );\n"
            "});\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            airband_path = os.path.join(tmp, "airband.conf")
            ground_path = os.path.join(tmp, "ground.conf")
            for path in (airband_path, ground_path):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(profile)
            rendered = combined_config.build_combined_config(
                airband_path=airband_path,
                ground_path=ground_path,
                mixer_name="combined",
            )
        self.assertIn("bitrate = 24;", rendered)


class VlcLatencyDefaultsTests(unittest.TestCase):
    def test_start_vlc_uses_low_latency_flags_and_default_cache(self):
        with mock.patch.object(
            vlc,
            "_refresh_target_status",
            return_value={
                "running": False,
                "process_running": False,
                "mount": "ANALOG.mp3",
                "stream_url": "http://127.0.0.1:8000/ANALOG.mp3",
                "audio_sink": "",
                "state": "idle",
            },
        ), mock.patch.object(
            vlc, "_stream_url_for", return_value="http://127.0.0.1:8000/ANALOG.mp3"
        ), mock.patch.object(
            vlc, "_vlc_launch_env", return_value={}
        ), mock.patch.object(
            vlc, "_prefer_configured_pulse_sink"
        ), mock.patch.object(
            vlc, "_write_pid"
        ), mock.patch.object(
            vlc, "_mute_sdrtrunk_pulse_streams"
        ), mock.patch.object(
            vlc,
            "_probe_target_process",
            return_value={
                "pid": 12345,
                "process_running": True,
                "verified": True,
                "actual_mount": "ANALOG.mp3",
                "actual_stream_url": "http://127.0.0.1:8000/ANALOG.mp3",
                "actual_audio_sink": "",
                "error": "",
            },
        ), mock.patch.object(
            vlc.subprocess, "Popen"
        ) as mock_popen:
            mock_popen.return_value.pid = 12345
            mock_popen.return_value.poll.return_value = None
            ok, err = vlc.start_vlc(target="analog")

        self.assertTrue(ok)
        self.assertEqual("", err)
        cmd = mock_popen.call_args.args[0]
        self.assertIn("--network-caching", cmd)
        self.assertIn(str(vlc.VLC_NETWORK_CACHING_MS), cmd)
        self.assertIn("--clock-jitter=0", cmd)
        self.assertIn("--clock-synchro=0", cmd)
        self.assertIn("--no-video", cmd)

    def test_start_vlc_fails_when_process_exits_immediately(self):
        proc = mock.Mock(pid=12345)
        proc.poll.return_value = 1
        with mock.patch.object(
            vlc,
            "_refresh_target_status",
            return_value={
                "running": False,
                "process_running": False,
                "mount": "ANALOG.mp3",
                "stream_url": "http://127.0.0.1:8000/ANALOG.mp3",
                "audio_sink": "",
                "state": "idle",
            },
        ), mock.patch.object(
            vlc, "_stream_url_for", return_value="http://127.0.0.1:8000/ANALOG.mp3"
        ), mock.patch.object(
            vlc, "_vlc_launch_env", return_value={}
        ), mock.patch.object(
            vlc, "_prefer_configured_pulse_sink"
        ), mock.patch.object(
            vlc, "_write_pid"
        ), mock.patch.object(
            vlc, "_clear_pid"
        ) as mock_clear_pid, mock.patch.object(
            vlc, "_mute_sdrtrunk_pulse_streams"
        ), mock.patch.object(
            vlc.subprocess, "Popen", return_value=proc
        ):
            ok, err = vlc.start_vlc(target="analog")

        self.assertFalse(ok)
        self.assertIn("exited immediately", err)
        mock_clear_pid.assert_called_once_with("analog")

    def test_vlc_status_returns_structured_targets(self):
        with mock.patch.object(
            vlc,
            "_probe_target_process",
            side_effect=[
                {
                    "pid": 111,
                    "process_running": True,
                    "verified": True,
                    "actual_mount": "ANALOG.mp3",
                    "actual_stream_url": "http://127.0.0.1:8000/ANALOG.mp3",
                    "actual_audio_sink": "",
                    "error": "",
                },
                {
                    "pid": None,
                    "process_running": False,
                    "verified": False,
                    "actual_mount": "",
                    "actual_stream_url": "",
                    "actual_audio_sink": "",
                    "error": "",
                },
            ],
        ):
            status = vlc.vlc_status()

        self.assertEqual("running", status["analog"]["state"])
        self.assertTrue(status["analog"]["running"])
        self.assertTrue(status["analog"]["verified"])
        self.assertEqual(111, status["analog"]["pid"])
        self.assertEqual("idle", status["digital"]["state"])
        self.assertFalse(status["digital"]["running"])
        self.assertIn("last_transition_ms", status["digital"])

    def test_vlc_status_preserves_last_error_until_cleared(self):
        try:
            vlc._set_target_runtime(
                "analog",
                state="error",
                pid=None,
                mount="ANALOG.mp3",
                stream_url="http://127.0.0.1:8000/ANALOG.mp3",
                audio_sink="",
                actual_mount="",
                actual_stream_url="",
                actual_audio_sink="",
                error="startup verification timed out",
                process_running=False,
                verified=False,
            )
            with mock.patch.object(
                vlc,
                "_probe_target_process",
                return_value={
                    "pid": None,
                    "process_running": False,
                    "verified": False,
                    "actual_mount": "",
                    "actual_stream_url": "",
                    "actual_audio_sink": "",
                    "error": "",
                },
            ):
                status = vlc.vlc_status()["analog"]

            self.assertEqual("error", status["state"])
            self.assertEqual("startup verification timed out", status["error"])
        finally:
            vlc._set_target_runtime(
                "analog",
                state="idle",
                pid=None,
                mount="ANALOG.mp3",
                stream_url="http://127.0.0.1:8000/ANALOG.mp3",
                audio_sink="",
                actual_mount="",
                actual_stream_url="",
                actual_audio_sink="",
                error="",
                process_running=False,
                verified=False,
            )

    def test_prefer_configured_pulse_sink_uses_wpctl_when_node_name_matches(self):
        status_text = "Sinks:\n  77. bluez_output.C0_28_8D_34_6E_67.1\nSink endpoints:\n"
        inspect_text = 'node.name = "bluez_output.C0_28_8D_34_6E_67.1"\n'
        with mock.patch.object(vlc, "VLC_PULSE_SINK", "bluez_output.C0_28_8D_34_6E_67.1"), \
             mock.patch.object(vlc.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"), \
             mock.patch.object(vlc, "_vlc_launch_env", return_value={"PULSE_SERVER": "unix:/tmp/pulse"}), \
             mock.patch.object(vlc, "_pulse_tool_output", side_effect=[status_text, inspect_text]), \
             mock.patch.object(vlc.subprocess, "run") as run:
            vlc._prefer_configured_pulse_sink()

        run.assert_called_once()
        self.assertEqual(["wpctl", "set-default", "77"], run.call_args.args[0])

    def test_prefer_configured_pulse_sink_falls_back_to_pactl(self):
        which_map = {"wpctl": None, "pactl": "/usr/bin/pactl"}
        with mock.patch.object(vlc, "VLC_PULSE_SINK", "bluez_output.C0_28_8D_34_6E_67.1"), \
             mock.patch.object(vlc.shutil, "which", side_effect=lambda name: which_map.get(name)), \
             mock.patch.object(vlc, "_vlc_launch_env", return_value={}), \
             mock.patch.object(vlc.subprocess, "run") as run:
            vlc._prefer_configured_pulse_sink()

        run.assert_called_once()
        self.assertEqual(
            ["pactl", "set-default-sink", "bluez_output.C0_28_8D_34_6E_67.1"],
            run.call_args.args[0],
        )


class DigitalLatencyDefaultsTests(unittest.TestCase):
    def test_sync_stream_configuration_migrates_legacy_default_bitrate(self):
        root = ET.fromstring(
            """
            <playlist>
              <stream mount_point="/DIGITAL.mp3" bitrate="32" sample_rate="16000" channels="1" delay="0" />
            </playlist>
            """
        )

        changed = digital._sync_stream_configuration(root)

        self.assertTrue(changed)
        stream = root.find("stream")
        self.assertIsNotNone(stream)
        self.assertEqual("24", stream.get("bitrate"))
        self.assertEqual("16000", stream.get("sample_rate"))

    def test_sync_stream_configuration_preserves_custom_higher_bitrate(self):
        root = ET.fromstring(
            """
            <playlist>
              <stream mount_point="/DIGITAL.mp3" bitrate="64" sample_rate="16000" channels="1" delay="0" />
            </playlist>
            """
        )
        digital._sync_stream_configuration(root)
        stream = root.find("stream")
        self.assertIsNotNone(stream)
        stream.set("bitrate", "64")

        changed = digital._sync_stream_configuration(root)

        self.assertEqual("64", stream.get("bitrate"))
        self.assertFalse(changed)


if __name__ == "__main__":
    unittest.main()
