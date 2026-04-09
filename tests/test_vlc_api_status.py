import io
import json
import unittest
from unittest import mock

from ui import handlers


class _FakePostRequest:
    def __init__(self, path: str, body: str, ctype: str = "application/x-www-form-urlencoded"):
        self.path = path
        payload = body.encode("utf-8")
        self.headers = {
            "Content-Length": str(len(payload)),
            "Content-Type": ctype,
        }
        self.rfile = io.BytesIO(payload)
        self.sent = []

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="ignore")
        self.sent.append((code, body, ctype))
        return code, body, ctype


def _structured_targets():
    return {
        "analog": {
            "target": "analog",
            "state": "running",
            "running": True,
            "process_running": True,
            "verified": True,
            "pid": 101,
            "mount": "ANALOG.mp3",
            "actual_mount": "ANALOG.mp3",
            "stream_url": "http://127.0.0.1:8000/ANALOG.mp3",
            "actual_stream_url": "http://127.0.0.1:8000/ANALOG.mp3",
            "audio_sink": "",
            "actual_audio_sink": "",
            "error": "",
            "last_transition_ms": 1234,
        },
        "digital": {
            "target": "digital",
            "state": "idle",
            "running": False,
            "process_running": False,
            "verified": False,
            "pid": None,
            "mount": "DIGITAL.mp3",
            "actual_mount": "",
            "stream_url": "http://127.0.0.1:8000/DIGITAL.mp3",
            "actual_stream_url": "",
            "audio_sink": "",
            "actual_audio_sink": "",
            "error": "",
            "last_transition_ms": 5678,
        },
    }


class VlcApiStatusTests(unittest.TestCase):
    def test_status_returns_structured_target_payload(self):
        req = _FakePostRequest("/api/vlc", "action=status&target=analog")
        with mock.patch.object(handlers, "vlc_status", return_value=_structured_targets()):
            code, body, _ = handlers.Handler.do_POST(req)

        payload = json.loads(body)
        self.assertEqual(200, code)
        self.assertTrue(payload["ok"])
        self.assertEqual("analog", payload["target"])
        self.assertEqual("running", payload["state"])
        self.assertTrue(payload["running"])
        self.assertEqual(101, payload["pid"])
        self.assertIn("targets", payload)
        self.assertEqual("idle", payload["targets"]["digital"]["state"])

    def test_restart_uses_backend_restart_and_returns_structured_status(self):
        req = _FakePostRequest("/api/vlc", "action=restart&target=digital&mount=DIGITAL.mp3")
        targets = _structured_targets()
        targets["digital"] = {
            **targets["digital"],
            "state": "running",
            "running": True,
            "process_running": True,
            "verified": True,
            "pid": 202,
            "last_transition_ms": 9999,
        }
        with mock.patch.object(handlers, "restart_vlc", return_value=(True, "")) as restart_mock, mock.patch.object(
            handlers, "vlc_status", return_value=targets
        ):
            code, body, _ = handlers.Handler.do_POST(req)

        payload = json.loads(body)
        self.assertEqual(200, code)
        self.assertTrue(payload["ok"])
        self.assertEqual("digital", payload["target"])
        self.assertEqual("running", payload["state"])
        self.assertTrue(payload["running"])
        restart_mock.assert_called_once_with(target="digital", mount="DIGITAL.mp3")


if __name__ == "__main__":
    unittest.main()
