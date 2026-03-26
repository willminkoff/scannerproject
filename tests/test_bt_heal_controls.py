import unittest
from unittest import mock
import io
import json

from ui import handlers
from ui import system_stats
from ui import systemd


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class BtHealStatusTests(unittest.TestCase):
    def test_read_bt_audio_heal_status_reports_enabled_timer(self):
        def fake_capture(args):
            key = tuple(args)
            mapping = {
                ("show", "-p", "LoadState", "--value", "scanner-bt-audio-heal.timer"): _Proc(stdout="loaded\n"),
                ("show", "-p", "LoadState", "--value", "scanner-bt-audio-heal.service"): _Proc(stdout="loaded\n"),
                ("is-enabled", "scanner-bt-audio-heal.timer"): _Proc(stdout="enabled\n"),
                ("is-active", "scanner-bt-audio-heal.timer"): _Proc(stdout="active\n"),
                ("is-active", "scanner-bt-audio-heal.service"): _Proc(returncode=3, stdout="inactive\n"),
            }
            return mapping.get(key, _Proc(returncode=1, stderr="unknown command"))

        with mock.patch.object(system_stats, "_systemctl_capture", side_effect=fake_capture), \
             mock.patch.object(system_stats, "BT_HEAL_TIMER_UNIT", "scanner-bt-audio-heal.timer"), \
             mock.patch.object(system_stats, "BT_HEAL_SERVICE_UNIT", "scanner-bt-audio-heal.service"), \
             mock.patch.object(system_stats, "BT_HEAL_DEFAULT_ENABLED", False):
            payload = system_stats.read_bt_audio_heal_status()

        self.assertTrue(payload["timer_exists"])
        self.assertTrue(payload["service_exists"])
        self.assertTrue(payload["timer_enabled"])
        self.assertTrue(payload["auto_recovery_enabled"])
        self.assertEqual("on", payload["status"])
        self.assertEqual("active", payload["timer_active_state"])
        self.assertEqual("inactive", payload["service_active_state"])

    def test_read_bt_audio_heal_status_handles_missing_units(self):
        def fake_capture(args):
            key = tuple(args)
            mapping = {
                ("show", "-p", "LoadState", "--value", "scanner-bt-audio-heal.timer"): _Proc(stdout="not-found\n"),
                ("show", "-p", "LoadState", "--value", "scanner-bt-audio-heal.service"): _Proc(stdout="not-found\n"),
            }
            return mapping.get(key, _Proc(returncode=1, stderr="not found"))

        with mock.patch.object(system_stats, "_systemctl_capture", side_effect=fake_capture), \
             mock.patch.object(system_stats, "BT_HEAL_TIMER_UNIT", "scanner-bt-audio-heal.timer"), \
             mock.patch.object(system_stats, "BT_HEAL_SERVICE_UNIT", "scanner-bt-audio-heal.service"), \
             mock.patch.object(system_stats, "BT_HEAL_DEFAULT_ENABLED", False):
            payload = system_stats.read_bt_audio_heal_status()

        self.assertFalse(payload["timer_exists"])
        self.assertFalse(payload["service_exists"])
        self.assertFalse(payload["timer_enabled"])
        self.assertFalse(payload["auto_recovery_enabled"])
        self.assertEqual("off", payload["status"])
        self.assertEqual("not-found", payload["timer_active_state"])


class BtHealControlTests(unittest.TestCase):
    def test_set_bt_heal_auto_recovery_runs_enable_disable_with_sudo(self):
        calls = []

        def fake_run(args, use_sudo=False):
            calls.append((tuple(args), bool(use_sudo)))
            return _Proc(returncode=0)

        with mock.patch.object(systemd, "BT_HEAL_TIMER_UNIT", "scanner-bt-audio-heal.timer"), \
             mock.patch.object(systemd, "_run_systemctl", side_effect=fake_run):
            ok_enable, err_enable = systemd.set_bt_heal_auto_recovery(True)
            ok_disable, err_disable = systemd.set_bt_heal_auto_recovery(False)

        self.assertTrue(ok_enable)
        self.assertEqual("", err_enable)
        self.assertTrue(ok_disable)
        self.assertEqual("", err_disable)
        self.assertIn((("enable", "--now", "scanner-bt-audio-heal.timer"), True), calls)
        self.assertIn((("disable", "--now", "scanner-bt-audio-heal.timer"), True), calls)

    def test_set_bt_heal_auto_recovery_propagates_error(self):
        with mock.patch.object(systemd, "BT_HEAL_TIMER_UNIT", "scanner-bt-audio-heal.timer"), \
             mock.patch.object(
                 systemd,
                 "_run_systemctl",
                 return_value=_Proc(returncode=1, stderr="access denied"),
             ):
            ok, err = systemd.set_bt_heal_auto_recovery(True)

        self.assertFalse(ok)
        self.assertIn("access denied", err)


class HostRebootControlTests(unittest.TestCase):
    def test_reboot_host_runs_systemctl_reboot_with_sudo(self):
        calls = []

        def fake_run(args, use_sudo=False):
            calls.append((tuple(args), bool(use_sudo)))
            return _Proc(returncode=0)

        with mock.patch.object(systemd, "_run_systemctl", side_effect=fake_run):
            ok, err = systemd.reboot_host()

        self.assertTrue(ok)
        self.assertEqual("", err)
        self.assertIn((("reboot",), True), calls)

    def test_reboot_host_propagates_error(self):
        with mock.patch.object(
            systemd,
            "_run_systemctl",
            return_value=_Proc(returncode=1, stderr="not permitted"),
        ):
            ok, err = systemd.reboot_host()

        self.assertFalse(ok)
        self.assertIn("not permitted", err)


class WxDecoderControlTests(unittest.TestCase):
    def test_wx_decoder_helpers_use_sudo_for_system_units(self):
        calls = []

        def fake_run(args, use_sudo=False):
            calls.append((tuple(args), bool(use_sudo)))
            return _Proc(returncode=0)

        with mock.patch.dict(
            systemd.UNITS,
            {"acars": "acarsdec", "radiosonde": "radiosonde-auto-rx"},
            clear=False,
        ), mock.patch.object(systemd, "_run_systemctl", side_effect=fake_run):
            self.assertEqual((True, ""), systemd.start_acars())
            self.assertEqual((True, ""), systemd.stop_acars())
            self.assertEqual((True, ""), systemd.restart_acars())
            self.assertEqual((True, ""), systemd.start_radiosonde())
            self.assertEqual((True, ""), systemd.stop_radiosonde())
            self.assertEqual((True, ""), systemd.restart_radiosonde())

        self.assertIn((("start", "acarsdec"), True), calls)
        self.assertIn((("stop", "acarsdec"), True), calls)
        self.assertIn((("restart", "acarsdec"), True), calls)
        self.assertIn((("start", "radiosonde-auto-rx"), True), calls)
        self.assertIn((("stop", "radiosonde-auto-rx"), True), calls)
        self.assertIn((("restart", "radiosonde-auto-rx"), True), calls)


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
        self.sent.append((code, body, ctype))
        return code, body, ctype


class BtHealApiTests(unittest.TestCase):
    def test_api_bt_heal_status_reports_current_state(self):
        req = _FakePostRequest("/api/bt-heal", "action=status")
        payload = {
            "auto_recovery_enabled": True,
            "status": "on",
        }
        with mock.patch.object(handlers, "read_bt_audio_heal_status", return_value=payload):
            code, body, _ = handlers.Handler.do_POST(req)
        data = json.loads(body)
        self.assertEqual(200, code)
        self.assertTrue(data["ok"])
        self.assertTrue(data["enabled"])
        self.assertEqual(payload, data["bt_heal"])

    def test_api_bt_heal_toggle_flips_and_returns_refreshed_state(self):
        req = _FakePostRequest("/api/bt-heal", "action=toggle")
        before = {
            "auto_recovery_enabled": False,
            "status": "off",
        }
        after = {
            "auto_recovery_enabled": True,
            "status": "on",
        }
        with mock.patch.object(handlers, "read_bt_audio_heal_status", side_effect=[before, after]), \
             mock.patch.object(handlers, "set_bt_heal_auto_recovery", return_value=(True, "")) as set_mock:
            code, body, _ = handlers.Handler.do_POST(req)
        data = json.loads(body)
        self.assertEqual(200, code)
        self.assertTrue(data["ok"])
        self.assertTrue(data["requested_enabled"])
        self.assertTrue(data["enabled"])
        self.assertEqual(after, data["bt_heal"])
        set_mock.assert_called_once_with(True)

    def test_api_bt_heal_set_parses_enabled_bool(self):
        req = _FakePostRequest("/api/bt-heal", "action=set&enabled=true")
        refreshed = {
            "auto_recovery_enabled": True,
            "status": "on",
        }
        with mock.patch.object(
            handlers,
            "read_bt_audio_heal_status",
            side_effect=[{"auto_recovery_enabled": False, "status": "off"}, refreshed],
        ), mock.patch.object(handlers, "set_bt_heal_auto_recovery", return_value=(True, "")) as set_mock:
            code, body, _ = handlers.Handler.do_POST(req)
        data = json.loads(body)
        self.assertEqual(200, code)
        self.assertTrue(data["ok"])
        self.assertTrue(data["requested_enabled"])
        self.assertTrue(data["enabled"])
        self.assertEqual(refreshed, data["bt_heal"])
        set_mock.assert_called_once_with(True)

    def test_api_bt_heal_unknown_action_returns_400(self):
        req = _FakePostRequest("/api/bt-heal", "action=wat")
        with mock.patch.object(
            handlers,
            "read_bt_audio_heal_status",
            return_value={"auto_recovery_enabled": False, "status": "off"},
        ):
            code, body, _ = handlers.Handler.do_POST(req)
        data = json.loads(body)
        self.assertEqual(400, code)
        self.assertFalse(data["ok"])
        self.assertEqual("unknown action", data["error"])


class HostRebootApiTests(unittest.TestCase):
    def test_api_reboot_host_success(self):
        req = _FakePostRequest("/api/reboot-host", "")
        with mock.patch.object(handlers, "reboot_host", return_value=(True, "")) as reboot_mock:
            code, body, _ = handlers.Handler.do_POST(req)
        data = json.loads(body)
        self.assertEqual(200, code)
        self.assertTrue(data["ok"])
        self.assertEqual("host reboot requested", data["message"])
        reboot_mock.assert_called_once_with()

    def test_api_reboot_host_failure(self):
        req = _FakePostRequest("/api/reboot-host", "")
        with mock.patch.object(handlers, "reboot_host", return_value=(False, "sudo denied")) as reboot_mock:
            code, body, _ = handlers.Handler.do_POST(req)
        data = json.loads(body)
        self.assertEqual(500, code)
        self.assertFalse(data["ok"])
        self.assertEqual("host reboot failed", data["message"])
        self.assertIn("sudo denied", data["error"])
        reboot_mock.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
