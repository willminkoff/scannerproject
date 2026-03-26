import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from ui import actions
from ui import handlers
from ui import scanner


_PROFILE_TEMPLATE = """airband = {airband};

devices:
({{
  type = "rtlsdr";
  index = {index};
  mode = "scan";
  gain = 32.800;  # UI_CONTROLLED
  channels:
  (
    {{
      freqs = ({freqs});
      labels = ({labels});
      squelch_threshold = -52;  # UI_CONTROLLED
    }}
  );
}});
"""


def _write_profile(path: str, *, airband: bool, freqs: list[float], labels: list[str]) -> None:
    freqs_text = ", ".join(f"{float(freq):.4f}" for freq in freqs)
    labels_text = ", ".join(f'"{label}"' for label in labels)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            _PROFILE_TEMPLATE.format(
                airband="true" if airband else "false",
                index=0 if airband else 1,
                freqs=freqs_text,
                labels=labels_text,
            )
        )


def _write_stats(path: str, counters: dict[str, int], when_ts: float) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for freq, count in counters.items():
            handle.write(
                f'channel_activity_counter{{freq="{freq}",label="Workflow {freq}"}}\t{int(count)}\n'
            )
    ns = int(when_ts * 1_000_000_000)
    os.utime(path, ns=(ns, ns))


def _reset_handler_caches() -> None:
    with handlers._CACHE_LOCK:
        handlers._STATUS_CACHE["ts"] = 0.0
        handlers._STATUS_CACHE["payload"] = None
        handlers._HITS_CACHE["ts"] = 0.0
        handlers._HITS_CACHE["payload"] = None


class _FakeGetRequest:
    def __init__(self, path: str):
        self.path = path
        self.sent = []

    def _parse_optional_bool_query(self, qs, key):
        return handlers.Handler._parse_optional_bool_query(qs, key)

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="ignore")
        self.sent.append((code, body, ctype))
        return code, body, ctype


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


class _SilentDigitalManager:
    def isActive(self):
        return False

    def getRecentEvents(self, limit: int = 20):
        del limit
        return []


class _RecordingDigitalManager:
    def __init__(self):
        self.calls: list[object] = []

    def start(self):
        self.calls.append("start")
        return True, ""

    def stop(self):
        self.calls.append("stop")
        return True, ""

    def restart(self):
        self.calls.append("restart")
        return True, ""

    def setProfile(self, profile_id: str):
        self.calls.append(("profile", str(profile_id)))
        return True, ""


class ProfileApiWorkflowTests(unittest.TestCase):
    def setUp(self):
        scanner._reset_live_analog_hit_state()
        _reset_handler_caches()

    def tearDown(self):
        scanner._reset_live_analog_hit_state()
        _reset_handler_caches()

    def test_api_profile_switch_invalidates_cached_hits_payload(self):
        with handlers._CACHE_LOCK:
            handlers._HITS_CACHE["ts"] = time.monotonic()
            handlers._HITS_CACHE["payload"] = {
                "items": [{"freq": "118.6000", "label": "Tower"}],
            }

        req = _FakePostRequest("/api/profile", "profile=next&target=airband")
        with mock.patch.object(handlers, "gate_action", return_value={"ok": True}), mock.patch.object(
            handlers,
            "enqueue_action",
            return_value={
                "status": 200,
                "payload": {"ok": True, "changed": True, "profile_switched": True},
            },
        ), mock.patch.object(
            handlers,
            "set_active_analog_profile",
            return_value={"ok": True, "target": "airband", "profile": "next"},
        ):
            code, body, _ = handlers.Handler.do_POST(req)
            payload = json.loads(body)
            self.assertEqual(200, code)
            self.assertTrue(payload["ok"])

            with mock.patch.object(
                handlers,
                "_build_hits_payload",
                return_value={"items": [{"freq": "119.3500", "label": "West"}]},
            ) as build_hits:
                get_code, get_body, _ = handlers.Handler.do_GET(_FakeGetRequest("/api/hits"))

        hits_payload = json.loads(get_body)
        self.assertEqual(200, get_code)
        self.assertEqual("119.3500", hits_payload["items"][0]["freq"])
        build_hits.assert_called_once()

    def test_api_profile_switch_clears_stale_hit_and_tracks_new_active_frequency(self):
        with tempfile.TemporaryDirectory() as tmp:
            current_path = os.path.join(tmp, "current_air.conf")
            next_path = os.path.join(tmp, "next_air.conf")
            ground_path = os.path.join(tmp, "ground.conf")
            active_link = os.path.join(tmp, "active_air.conf")
            stats_path = os.path.join(tmp, "stats.txt")
            _write_profile(current_path, airband=True, freqs=[118.6], labels=["Tower"])
            _write_profile(next_path, airband=True, freqs=[119.35], labels=["West"])
            _write_profile(ground_path, airband=False, freqs=[162.55], labels=["WX"])
            os.symlink(current_path, active_link)

            profiles_airband = [
                {"id": "current", "label": "Current", "path": current_path},
                {"id": "next", "label": "Next", "path": next_path},
            ]
            profiles_ground = [
                {"id": "ground", "label": "Ground", "path": ground_path},
            ]
            base = time.time()

            with mock.patch.object(
                actions,
                "split_profiles",
                return_value=([], profiles_airband, profiles_ground),
            ), mock.patch.object(
                actions,
                "CONFIG_SYMLINK",
                active_link,
            ), mock.patch.object(
                actions,
                "read_active_config_path",
                side_effect=lambda: os.path.realpath(active_link),
            ), mock.patch.object(
                actions,
                "write_combined_config",
                return_value=False,
            ), mock.patch.object(
                actions,
                "restart_rtl",
                return_value=(True, ""),
            ), mock.patch.object(
                handlers,
                "split_profiles",
                return_value=([], profiles_airband, profiles_ground),
            ), mock.patch.object(
                handlers,
                "read_active_config_path",
                side_effect=lambda: os.path.realpath(active_link),
            ), mock.patch.object(
                handlers,
                "GROUND_CONFIG_PATH",
                ground_path,
            ), mock.patch.object(
                handlers,
                "gate_action",
                return_value={"ok": True},
            ), mock.patch.object(
                handlers,
                "enqueue_action",
                side_effect=actions.execute_action,
            ), mock.patch.object(
                handlers,
                "set_active_analog_profile",
                return_value={"ok": True, "target": "airband", "profile": "next"},
            ), mock.patch.object(
                handlers,
                "get_digital_manager",
                return_value=_SilentDigitalManager(),
            ), mock.patch.object(
                handlers,
                "_HITS_CACHE_TTL_SEC",
                0.0,
            ), mock.patch.object(
                scanner,
                "read_active_config_path",
                side_effect=lambda: os.path.realpath(active_link),
            ), mock.patch.object(
                scanner,
                "GROUND_CONFIG_PATH",
                ground_path,
            ), mock.patch.object(
                scanner,
                "LAST_HIT_AIRBAND_PATH",
                os.path.join(tmp, "missing-air.txt"),
            ), mock.patch.object(
                scanner,
                "LAST_HIT_GROUND_PATH",
                os.path.join(tmp, "missing-ground.txt"),
            ), mock.patch.object(
                scanner,
                "ANALOG_AUTO_SQUELCH_STATS_PATH",
                stats_path,
            ), mock.patch.object(
                scanner,
                "read_hit_list_for_unit",
                return_value=[],
            ):
                _write_stats(stats_path, {"118.6000": 0, "119.3500": 0}, base - 2.0)
                scanner.refresh_analog_hit_state(now=base - 1.8)

                _write_stats(stats_path, {"118.6000": 120, "119.3500": 0}, base - 1.0)
                scanner.refresh_analog_hit_state(now=base - 0.8)
                before_code, before_body, _ = handlers.Handler.do_GET(_FakeGetRequest("/api/hits"))
                before_hits = json.loads(before_body)["items"]

                switch_code, switch_body, _ = handlers.Handler.do_POST(
                    _FakePostRequest("/api/profile", "profile=next&target=airband")
                )
                after_switch_code, after_switch_body, _ = handlers.Handler.do_GET(_FakeGetRequest("/api/hits"))
                after_switch_hits = json.loads(after_switch_body)["items"]

                _write_stats(stats_path, {"118.6000": 120, "119.3500": 35}, base + 0.2)
                scanner.refresh_analog_hit_state(now=base + 0.3)
                after_new_code, after_new_body, _ = handlers.Handler.do_GET(_FakeGetRequest("/api/hits"))
                after_new_hits = json.loads(after_new_body)["items"]
                switch_payload = json.loads(switch_body)
                switched_target = os.path.realpath(active_link)
                expected_target = os.path.realpath(next_path)

        self.assertEqual(200, before_code)
        self.assertEqual("118.6000", before_hits[0]["freq"])
        self.assertEqual(200, switch_code)
        self.assertTrue(switch_payload["ok"])
        self.assertTrue(switch_payload["profile_switched"])
        self.assertEqual(200, after_switch_code)
        self.assertEqual([], after_switch_hits)
        self.assertEqual(200, after_new_code)
        self.assertEqual("119.3500", after_new_hits[0]["freq"])
        self.assertEqual(expected_target, switched_target)


class WxApiWorkflowTests(unittest.TestCase):
    def setUp(self):
        _reset_handler_caches()

    def tearDown(self):
        _reset_handler_caches()

    def test_wx_status_accepts_acars_absolute_path_fallback(self):
        class _Store:
            def get_status(self):
                return {
                    "collecting": False,
                    "active_decoder": None,
                    "message_count": 0,
                    "met_count": 0,
                    "last_message_time": 0.0,
                }

        def fake_isfile(path: str) -> bool:
            return path == "/usr/local/bin/acarsdec"

        with mock.patch.object(handlers, "get_met_store", return_value=_Store()), mock.patch.object(
            handlers.shutil,
            "which",
            return_value=None,
        ), mock.patch.object(
            handlers.os.path,
            "isfile",
            side_effect=fake_isfile,
        ):
            code, body, _ = handlers.Handler.do_GET(_FakeGetRequest("/api/wx/status"))

        self.assertEqual(200, code)
        payload = json.loads(body)
        self.assertTrue(payload["acars_installed"])
        self.assertFalse(payload["radiosonde_installed"])


class DigitalApiWorkflowTests(unittest.TestCase):
    def setUp(self):
        _reset_handler_caches()

    def tearDown(self):
        _reset_handler_caches()

    def test_api_digital_lifecycle_sequence_uses_manager_and_invalidates_runtime_caches(self):
        manager = _RecordingDigitalManager()

        def prime_caches():
            with handlers._CACHE_LOCK:
                handlers._STATUS_CACHE["ts"] = time.monotonic()
                handlers._STATUS_CACHE["payload"] = {"digital_active": False}
                handlers._HITS_CACHE["ts"] = time.monotonic()
                handlers._HITS_CACHE["payload"] = {"items": [{"freq": "Dispatch"}]}

        with mock.patch.object(
            handlers,
            "get_digital_manager",
            return_value=manager,
        ), mock.patch.object(
            handlers,
            "gate_action",
            return_value={"ok": True},
        ), mock.patch.object(
            handlers,
            "validate_digital_profile_id",
            return_value=True,
        ), mock.patch.object(
            handlers,
            "set_active_digital_profile",
            return_value={"ok": True, "profile": "metro"},
        ):
            prime_caches()
            start_code, start_body, _ = handlers.Handler.do_POST(_FakePostRequest("/api/digital/start", ""))
            with handlers._CACHE_LOCK:
                self.assertIsNone(handlers._STATUS_CACHE["payload"])
                self.assertIsNone(handlers._HITS_CACHE["payload"])

            prime_caches()
            profile_code, profile_body, _ = handlers.Handler.do_POST(
                _FakePostRequest("/api/digital/profile", "profileId=metro")
            )
            with handlers._CACHE_LOCK:
                self.assertIsNone(handlers._STATUS_CACHE["payload"])
                self.assertIsNone(handlers._HITS_CACHE["payload"])

            prime_caches()
            restart_code, restart_body, _ = handlers.Handler.do_POST(_FakePostRequest("/api/digital/restart", ""))
            with handlers._CACHE_LOCK:
                self.assertIsNone(handlers._STATUS_CACHE["payload"])
                self.assertIsNone(handlers._HITS_CACHE["payload"])

            prime_caches()
            stop_code, stop_body, _ = handlers.Handler.do_POST(_FakePostRequest("/api/digital/stop", ""))
            with handlers._CACHE_LOCK:
                self.assertIsNone(handlers._STATUS_CACHE["payload"])
                self.assertIsNone(handlers._HITS_CACHE["payload"])

        start_payload = json.loads(start_body)
        profile_payload = json.loads(profile_body)
        restart_payload = json.loads(restart_body)
        stop_payload = json.loads(stop_body)
        self.assertEqual(200, start_code)
        self.assertTrue(start_payload["ok"])
        self.assertEqual(200, profile_code)
        self.assertTrue(profile_payload["ok"])
        self.assertEqual({"ok": True, "profile": "metro"}, profile_payload["v3_compile"])
        self.assertEqual(200, restart_code)
        self.assertTrue(restart_payload["ok"])
        self.assertEqual(200, stop_code)
        self.assertTrue(stop_payload["ok"])
        self.assertEqual(
            ["start", ("profile", "metro"), "restart", "stop"],
            manager.calls,
        )


if __name__ == "__main__":
    unittest.main()
