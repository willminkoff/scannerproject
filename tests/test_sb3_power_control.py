import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "desktop" / "sb3_power.py"
SPEC = importlib.util.spec_from_file_location("sb3_power", MODULE_PATH)
sb3_power = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = sb3_power
SPEC.loader.exec_module(sb3_power)


class Sb3PowerControlTests(unittest.TestCase):
    def test_restore_keys_prefers_saved_snapshot(self):
        specs = [
            sb3_power.UnitSpec("icecast", "icecast2", restore_default=True),
            sb3_power.UnitSpec("rtl", "rtl-airband", restore_default=True),
            sb3_power.UnitSpec("digital", "scanner-digital", restore_default=True),
        ]
        status = {
            "icecast": {"unit": "icecast2", "exists": True, "enabled": True},
            "rtl": {"unit": "rtl-airband", "exists": True, "enabled": True},
            "digital": {"unit": "scanner-digital", "exists": True, "enabled": True},
        }
        restore = sb3_power.restore_keys_from_state(specs, status, {"restore_units": ["icecast", "digital"]})
        self.assertEqual(restore, ["icecast", "digital"])

    def test_default_restore_uses_existing_default_units(self):
        specs = [
            sb3_power.UnitSpec("icecast", "icecast2", restore_default=True),
            sb3_power.UnitSpec("keepalive", "icecast-keepalive", restore_default=True),
            sb3_power.UnitSpec("ground", "rtl-airband-ground", restore_default=False),
        ]
        status = {
            "icecast": {"unit": "icecast2", "exists": True, "enabled": True},
            "keepalive": {"unit": "icecast-keepalive", "exists": True, "enabled": False},
            "ground": {"unit": "rtl-airband-ground", "exists": True, "enabled": False},
        }
        self.assertEqual(
            sb3_power.default_restore_keys(specs, status),
            ["icecast", "keepalive"],
        )

    def test_stop_stack_saves_active_restore_set_and_stops_in_order(self):
        specs = [
            sb3_power.UnitSpec("ui", "airband-ui"),
            sb3_power.UnitSpec("rtl", "rtl-airband", holds_tuner=True),
            sb3_power.UnitSpec("digital", "scanner-digital", holds_tuner=True),
            sb3_power.UnitSpec("icecast", "icecast2"),
        ]
        status = {
            "ui": {"unit": "airband-ui", "exists": True, "active": True, "enabled": True, "holds_tuner": False},
            "rtl": {"unit": "rtl-airband", "exists": True, "active": True, "enabled": True, "holds_tuner": True},
            "digital": {"unit": "scanner-digital", "exists": True, "active": True, "enabled": True, "holds_tuner": True},
            "icecast": {"unit": "icecast2", "exists": True, "active": True, "enabled": True, "holds_tuner": False},
        }
        commands = []

        class Result:
            def __init__(self):
                self.returncode = 0
                self.stdout = ""
                self.stderr = ""

        def fake_run(args):
            commands.append(tuple(args))
            return Result()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "sb3-state.json"
            with patch.object(sb3_power, "collect_unit_status", return_value=status), \
                 patch.object(sb3_power, "run_systemctl", side_effect=fake_run), \
                 patch.object(sb3_power, "_wait_for_state", return_value=True):
                ok, _ = sb3_power.stop_stack(specs, state_path=state_path)
            self.assertTrue(ok)
            self.assertEqual(
                commands,
                [
                    ("stop", "airband-ui"),
                    ("stop", "scanner-digital"),
                    ("stop", "rtl-airband"),
                    ("stop", "icecast2"),
                ],
            )
            saved = sb3_power.load_state(state_path)
            self.assertEqual(saved["restore_units"], ["ui", "rtl", "digital", "icecast"])

    def test_start_stack_restores_snapshot_order(self):
        specs = [
            sb3_power.UnitSpec("icecast", "icecast2", restore_default=True),
            sb3_power.UnitSpec("rtl", "rtl-airband", restore_default=True, holds_tuner=True),
            sb3_power.UnitSpec("digital", "scanner-digital", restore_default=True, holds_tuner=True),
            sb3_power.UnitSpec("ui", "airband-ui", restore_default=True),
        ]
        status = {
            "icecast": {"unit": "icecast2", "exists": True, "active": False, "enabled": True, "holds_tuner": False},
            "rtl": {"unit": "rtl-airband", "exists": True, "active": False, "enabled": True, "holds_tuner": True},
            "digital": {"unit": "scanner-digital", "exists": True, "active": False, "enabled": True, "holds_tuner": True},
            "ui": {"unit": "airband-ui", "exists": True, "active": False, "enabled": True, "holds_tuner": False},
        }
        commands = []

        class Result:
            def __init__(self):
                self.returncode = 0
                self.stdout = ""
                self.stderr = ""

        def fake_run(args):
            commands.append(tuple(args))
            return Result()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "sb3-state.json"
            sb3_power.save_state({"restore_units": ["icecast", "digital", "ui"]}, path=state_path)
            with patch.object(sb3_power, "collect_unit_status", return_value=status), \
                 patch.object(sb3_power, "run_systemctl", side_effect=fake_run), \
                 patch.object(sb3_power, "_wait_for_state", return_value=True):
                ok, _ = sb3_power.start_stack(specs, state_path=state_path)
            self.assertTrue(ok)
            self.assertEqual(
                commands,
                [
                    ("start", "icecast2"),
                    ("start", "scanner-digital"),
                    ("start", "airband-ui"),
                ],
            )

    def test_resolve_state_path_uses_explicit_state_home(self):
        path = sb3_power.resolve_state_path("/tmp/sb3-user")
        self.assertEqual(path, Path("/tmp/sb3-user/.local/state/sb3-power/state.json"))

    def test_verify_outcome_off_reports_remaining_active_units(self):
        specs = [
            sb3_power.UnitSpec("rtl", "rtl-airband", holds_tuner=True),
            sb3_power.UnitSpec("digital", "scanner-digital", holds_tuner=True),
            sb3_power.UnitSpec("ui", "airband-ui"),
        ]
        status = {
            "rtl": {"unit": "rtl-airband", "exists": True, "active": False},
            "digital": {"unit": "scanner-digital", "exists": True, "active": True},
            "ui": {"unit": "airband-ui", "exists": True, "active": False},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            with patch.object(sb3_power, "collect_unit_status", return_value=status):
                ok, lines = sb3_power._verify_outcome("off", specs, state_path)
            self.assertFalse(ok)
            self.assertIn("SB3 is not fully off yet. Still active:", lines)
            self.assertIn("- scanner-digital", lines)

    def test_invoke_elevated_uses_provided_state_home(self):
        class ProcResult:
            def __init__(self, returncode=0, stdout="ok\n", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        with tempfile.TemporaryDirectory() as tmpdir:
            expected_state_path = Path(tmpdir) / ".local/state/sb3-power/state.json"
            with patch.object(sb3_power, "PKEXEC_BIN", None), \
                 patch.object(sb3_power, "SUDO_BIN", "/usr/bin/sudo"), \
                 patch.object(sb3_power.subprocess, "run", return_value=ProcResult()) as mock_run, \
                 patch.object(sb3_power, "_verify_outcome", return_value=(True, [])) as mock_verify:
                ok, _ = sb3_power.invoke_elevated("off", state_home=tmpdir)
            self.assertTrue(ok)
            call_args = mock_run.call_args[0][0]
            self.assertIn("--state-home", call_args)
            self.assertIn(tmpdir, call_args)
            self.assertEqual(mock_verify.call_args[0][2], expected_state_path)

    def test_invoke_elevated_fails_when_off_verification_fails(self):
        class ProcResult:
            def __init__(self, returncode=0, stdout="off complete\n", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        with patch.object(sb3_power, "PKEXEC_BIN", None), \
             patch.object(sb3_power, "SUDO_BIN", "/usr/bin/sudo"), \
             patch.object(sb3_power.subprocess, "run", return_value=ProcResult()), \
             patch.object(
                 sb3_power,
                 "_verify_outcome",
                 return_value=(False, ["SB3 is not fully off yet. Still active:", "- scanner-digital"]),
             ):
            ok, lines = sb3_power.invoke_elevated("off", state_home="/tmp/sb3-user")
        self.assertFalse(ok)
        self.assertIn("SB3 is not fully off yet. Still active:", lines)
        self.assertIn("- scanner-digital", lines)

    def test_invoke_elevated_uses_askpass_when_sudo_noninteractive_fails(self):
        class ProcResult:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        calls = []

        def fake_run(args, **kwargs):
            calls.append((list(args), dict(kwargs)))
            if len(calls) == 1:
                return ProcResult(returncode=1, stderr="sudo: a password is required")
            return ProcResult(returncode=0, stdout="off complete\n")

        with patch.object(sb3_power, "PKEXEC_BIN", None), \
             patch.object(sb3_power, "SUDO_BIN", "/usr/bin/sudo"), \
             patch.object(sb3_power, "ZENITY_BIN", "/usr/bin/zenity"), \
             patch.object(sb3_power.subprocess, "run", side_effect=fake_run), \
             patch.object(sb3_power.sys.stdin, "isatty", return_value=False), \
             patch.object(sb3_power, "_verify_outcome", return_value=(True, [])), \
             patch.dict(sb3_power.os.environ, {"DISPLAY": ":0"}, clear=False):
            ok, _ = sb3_power.invoke_elevated("off", state_home="/tmp/sb3-user")
        self.assertTrue(ok)
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(calls[0][0][0:2], ["/usr/bin/sudo", "-n"])
        self.assertEqual(calls[1][0][0:2], ["/usr/bin/sudo", "-A"])

    def test_stop_stack_errors_when_unit_never_reaches_inactive(self):
        specs = [
            sb3_power.UnitSpec("rtl", "rtl-airband", holds_tuner=True),
            sb3_power.UnitSpec("digital", "scanner-digital", holds_tuner=True),
        ]
        status = {
            "rtl": {"unit": "rtl-airband", "exists": True, "active": True, "enabled": True, "holds_tuner": True},
            "digital": {"unit": "scanner-digital", "exists": True, "active": True, "enabled": True, "holds_tuner": True},
        }

        class Result:
            def __init__(self):
                self.returncode = 0
                self.stdout = ""
                self.stderr = ""

        commands = []

        def fake_run(args):
            commands.append(tuple(args))
            return Result()

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "sb3-state.json"
            with patch.object(sb3_power, "collect_unit_status", return_value=status), \
                 patch.object(sb3_power, "run_systemctl", side_effect=fake_run), \
                 patch.object(sb3_power, "_wait_for_state", return_value=False), \
                 patch.object(sb3_power, "unit_active", return_value=True):
                ok, lines = sb3_power.stop_stack(specs, state_path=state_path)
        self.assertFalse(ok)
        self.assertIn(("kill", "--kill-who=all", "--signal=SIGKILL", "rtl-airband"), commands)
        self.assertIn(("kill", "--kill-who=all", "--signal=SIGKILL", "scanner-digital"), commands)
        self.assertTrue(any("did not reach inactive state" in line for line in lines))

    def test_stop_stack_force_stop_can_recover_lingering_unit(self):
        specs = [
            sb3_power.UnitSpec("digital", "scanner-digital", holds_tuner=True),
        ]
        status = {
            "digital": {"unit": "scanner-digital", "exists": True, "active": True, "enabled": True, "holds_tuner": True},
        }

        class Result:
            def __init__(self):
                self.returncode = 0
                self.stdout = ""
                self.stderr = ""

        commands = []

        def fake_run(args):
            commands.append(tuple(args))
            return Result()

        def fake_wait(unit, *, expect_active, timeout_sec=20.0):
            _ = unit
            _ = expect_active
            # Normal stop checks fail, force-stop verification passes.
            return timeout_sec < 10.0

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "sb3-state.json"
            with patch.object(sb3_power, "collect_unit_status", return_value=status), \
                 patch.object(sb3_power, "run_systemctl", side_effect=fake_run), \
                 patch.object(sb3_power, "_wait_for_state", side_effect=fake_wait), \
                 patch.object(sb3_power, "unit_active", return_value=True):
                ok, lines = sb3_power.stop_stack(specs, state_path=state_path)
        self.assertTrue(ok)
        self.assertIn(("kill", "--kill-who=all", "--signal=SIGKILL", "scanner-digital"), commands)
        self.assertTrue(any("Escalating stop for lingering SB3 services..." in line for line in lines))


if __name__ == "__main__":
    unittest.main()
