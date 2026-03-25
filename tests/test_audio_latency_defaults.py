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
        with mock.patch.object(vlc, "_target_running", return_value=False), mock.patch.object(
            vlc, "_stream_url_for", return_value="http://127.0.0.1:8000/ANALOG.mp3"
        ), mock.patch.object(
            vlc, "_vlc_launch_env", return_value={}
        ), mock.patch.object(
            vlc, "_write_pid"
        ), mock.patch.object(
            vlc, "_mute_sdrtrunk_pulse_streams"
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
        with mock.patch.object(vlc, "_target_running", return_value=False), mock.patch.object(
            vlc, "_stream_url_for", return_value="http://127.0.0.1:8000/ANALOG.mp3"
        ), mock.patch.object(
            vlc, "_vlc_launch_env", return_value={}
        ), mock.patch.object(
            vlc, "_write_pid"
        ), mock.patch.object(
            vlc, "_clear_pid"
        ) as mock_clear_pid, mock.patch.object(
            vlc, "_mute_sdrtrunk_pulse_streams"
        ), mock.patch.object(
            vlc.time, "sleep"
        ), mock.patch.object(
            vlc.subprocess, "Popen", return_value=proc
        ):
            ok, err = vlc.start_vlc(target="analog")

        self.assertFalse(ok)
        self.assertIn("exited immediately", err)
        mock_clear_pid.assert_called_once_with("analog")


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
