import importlib.util
import pathlib
from unittest import TestCase, mock


MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "sdrtrunk-local-monitor.py"
SPEC = importlib.util.spec_from_file_location("sdrtrunk_local_monitor", MODULE_PATH)
assert SPEC and SPEC.loader
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)


class SdrtrunkLocalMonitorTests(TestCase):
    def test_parse_pw_dump_audio_objects_extracts_audio_streams(self):
        payload = """
[
  {
    "id": 51,
    "info": {
      "props": {
        "media.class": "Audio/Sink",
        "application.name": "pipewire"
      }
    }
  },
  {
    "id": 77,
    "info": {
      "props": {
        "media.class": "Stream/Output/Audio",
        "application.name": "OpenJDK Platform binary",
        "application.process.binary": "java",
        "application.process.id": "42561",
        "node.name": "java-output",
        "node.description": "SDRTrunk Audio"
      }
    }
  }
]
"""
        items = monitor._parse_pw_dump_audio_objects(payload)
        self.assertEqual(
            [
                {
                    "id": "77",
                    "backend": "wpctl",
                    "app_name": "OpenJDK Platform binary",
                    "proc_binary": "java",
                    "proc_pid": "42561",
                    "node_name": "java-output",
                    "node_description": "SDRTrunk Audio",
                }
            ],
            items,
        )

    def test_find_sdrtrunk_audio_objects_matches_java_pid(self):
        audio_objects = [
            {
                "id": "88",
                "backend": "wpctl",
                "app_name": "OpenJDK Platform binary",
                "proc_binary": "java",
                "proc_pid": "42561",
                "node_name": "java-output",
                "node_description": "",
            },
            {
                "id": "91",
                "backend": "wpctl",
                "app_name": "Firefox",
                "proc_binary": "firefox",
                "proc_pid": "500",
                "node_name": "firefox-output",
                "node_description": "",
            },
        ]
        with mock.patch.object(monitor, "_list_sink_inputs", return_value=audio_objects), mock.patch.object(
            monitor, "_is_sdrtrunk_java_pid", side_effect=lambda pid: pid == "42561"
        ):
            items = monitor._find_sdrtrunk_audio_objects()
        self.assertEqual(["88"], [item["id"] for item in items])

    def test_set_sink_mute_uses_wpctl_backend(self):
        with mock.patch.object(monitor.subprocess, "run") as run:
            run.return_value.returncode = 0
            ok = monitor._set_sink_mute({"backend": "wpctl", "id": "77"}, True)
        self.assertTrue(ok)
        run.assert_called_once()
        self.assertEqual(["wpctl", "set-mute", "77", "1"], run.call_args.args[0])
