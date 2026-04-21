"""Tests for ui/dongle_power.py — SDR dongle power control module."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Module loading (same pattern as test_sb3_power_control.py)
# ---------------------------------------------------------------------------
_MODULE_PATH = Path(__file__).resolve().parents[1] / "ui" / "dongle_power.py"
_SPEC = importlib.util.spec_from_file_location("dongle_power", _MODULE_PATH)
dongle_power = importlib.util.module_from_spec(_SPEC)

# Stub heavy dependencies so the module loads without a live system.
_config_stub = MagicMock()
_config_stub.UNITS = {
    "rtl": "rtl-airband",
    "ground": "rtl-airband-ground",
    "digital": "scanner-digital-op25",
    "digital_audio": "scanner-digital-op25-audio",
    "acars": "acarsdec",
    "vdl2": "dumpvdl2",
}
_config_stub.UI_PORT = 5050

sys.modules["ui"] = MagicMock()
sys.modules["ui.config"] = _config_stub
sys.modules["ui.systemd"] = MagicMock()

assert _SPEC.loader is not None
_SPEC.loader.exec_module(dongle_power)

# Inject the stubs the module resolved via import
dongle_power.UNITS = _config_stub.UNITS
dongle_power.UI_PORT = _config_stub.UI_PORT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ok_result(returncode=0):
    r = MagicMock()
    r.returncode = returncode
    r.stdout = ""
    r.stderr = ""
    return r


# ---------------------------------------------------------------------------
# get_power_state
# ---------------------------------------------------------------------------

class TestGetPowerState(unittest.TestCase):
    def test_all_core_active_returns_on(self):
        with patch.object(dongle_power, "unit_active", return_value=True), \
             patch.object(dongle_power, "_load_state", return_value={}):
            self.assertEqual(dongle_power.get_power_state(), "on")

    def test_no_core_active_returns_off(self):
        with patch.object(dongle_power, "unit_active", return_value=False), \
             patch.object(dongle_power, "_load_state", return_value={}):
            self.assertEqual(dongle_power.get_power_state(), "off")

    def test_partial_active_returns_partial(self):
        active_units = {"rtl-airband", "rtl-airband-ground"}

        def _fake_active(unit):
            return unit in active_units

        with patch.object(dongle_power, "unit_active", side_effect=_fake_active), \
             patch.object(dongle_power, "_load_state", return_value={}):
            self.assertEqual(dongle_power.get_power_state(), "partial")

    def test_state_file_off_but_services_on_updates_to_on(self):
        saved = []

        def _fake_save(state, set_by="api"):
            saved.append((state, set_by))

        with patch.object(dongle_power, "unit_active", return_value=True), \
             patch.object(dongle_power, "_load_state", return_value={"state": "off"}), \
             patch.object(dongle_power, "_save_state", side_effect=_fake_save):
            result = dongle_power.get_power_state()
        self.assertEqual(result, "on")
        self.assertEqual(saved[0], ("on", "auto-detected"))


# ---------------------------------------------------------------------------
# power_off
# ---------------------------------------------------------------------------

class TestPowerOff(unittest.TestCase):
    def _run_power_off(self, active_units):
        stopped = []

        def _fake_active(unit):
            return unit in active_units

        def _fake_stop(unit, use_sudo=False):
            stopped.append((unit, use_sudo))
            return True, ""

        with patch.object(dongle_power, "unit_active", side_effect=_fake_active), \
             patch.object(dongle_power, "_stop_unit", side_effect=_fake_stop), \
             patch.object(dongle_power, "_save_state"):
            ok, lines = dongle_power.power_off()
        return ok, lines, stopped

    def test_stops_active_sdr_services(self):
        active = {"rtl-airband", "scanner-digital-op25", "scanner-digital-op25-audio"}
        ok, lines, stopped = self._run_power_off(active)
        self.assertTrue(ok)
        stopped_units = [u for u, _ in stopped]
        self.assertIn("rtl-airband", stopped_units)
        self.assertIn("scanner-digital-op25", stopped_units)
        self.assertIn("scanner-digital-op25-audio", stopped_units)

    def test_digital_audio_stops_before_digital(self):
        active = {"scanner-digital-op25", "scanner-digital-op25-audio"}
        _, _, stopped = self._run_power_off(active)
        units = [u for u, _ in stopped]
        self.assertLess(units.index("scanner-digital-op25-audio"), units.index("scanner-digital-op25"))

    def test_skips_inactive_services(self):
        _, _, stopped = self._run_power_off(set())
        self.assertEqual(stopped, [])

    def test_reports_failure_and_returns_false(self):
        def _fake_stop(unit, use_sudo=False):
            return False, "permission denied"

        with patch.object(dongle_power, "unit_active", return_value=True), \
             patch.object(dongle_power, "_stop_unit", side_effect=_fake_stop), \
             patch.object(dongle_power, "_save_state"):
            ok, lines = dongle_power.power_off()
        self.assertFalse(ok)
        self.assertTrue(any("Errors" in ln for ln in lines))

    def test_digital_and_acars_stop_with_sudo(self):
        active = {"scanner-digital-op25", "acarsdec", "dumpvdl2"}
        _, _, stopped = self._run_power_off(active)
        sudo_map = {u: s for u, s in stopped}
        self.assertTrue(sudo_map.get("scanner-digital-op25"))
        self.assertTrue(sudo_map.get("acarsdec"))
        self.assertTrue(sudo_map.get("dumpvdl2"))

    def test_rtl_stops_without_sudo(self):
        _, _, stopped = self._run_power_off({"rtl-airband"})
        sudo_map = {u: s for u, s in stopped}
        self.assertFalse(sudo_map.get("rtl-airband"))

    def test_saves_off_state(self):
        saves = []
        with patch.object(dongle_power, "unit_active", return_value=False), \
             patch.object(dongle_power, "_save_state", side_effect=lambda s, **kw: saves.append(s)):
            dongle_power.power_off()
        self.assertEqual(saves, ["off"])


# ---------------------------------------------------------------------------
# power_on
# ---------------------------------------------------------------------------

class TestPowerOn(unittest.TestCase):
    def _run_power_on(self, active_units=None):
        started = []

        def _fake_active(unit):
            return unit in (active_units or set())

        def _fake_start(unit, use_sudo=False):
            started.append((unit, use_sudo))
            return True, ""

        with patch.object(dongle_power, "unit_active", side_effect=_fake_active), \
             patch.object(dongle_power, "_start_unit", side_effect=_fake_start), \
             patch.object(dongle_power, "_save_state"):
            ok, lines = dongle_power.power_on()
        return ok, lines, started

    def test_starts_core_sdr_services(self):
        ok, lines, started = self._run_power_on()
        self.assertTrue(ok)
        started_units = [u for u, _ in started]
        self.assertIn("rtl-airband", started_units)
        self.assertIn("rtl-airband-ground", started_units)
        self.assertIn("scanner-digital-op25", started_units)
        self.assertIn("scanner-digital-op25-audio", started_units)

    def test_rtl_starts_before_digital(self):
        _, _, started = self._run_power_on()
        units = [u for u, _ in started]
        self.assertLess(units.index("rtl-airband"), units.index("scanner-digital-op25"))

    def test_does_not_start_acars_or_vdl2(self):
        _, _, started = self._run_power_on()
        started_units = [u for u, _ in started]
        self.assertNotIn("acarsdec", started_units)
        self.assertNotIn("dumpvdl2", started_units)

    def test_skips_already_active_services(self):
        all_units = {
            "rtl-airband", "rtl-airband-ground",
            "scanner-digital-op25", "scanner-digital-op25-audio",
        }
        _, lines, started = self._run_power_on(active_units=all_units)
        self.assertEqual(started, [])
        self.assertTrue(any("already running" in ln for ln in lines))

    def test_saves_on_state(self):
        saves = []
        with patch.object(dongle_power, "unit_active", return_value=False), \
             patch.object(dongle_power, "_start_unit", return_value=(True, "")), \
             patch.object(dongle_power, "_save_state", side_effect=lambda s, **kw: saves.append(s)):
            dongle_power.power_on()
        self.assertEqual(saves, ["on"])

    def test_reports_failure_and_returns_false(self):
        with patch.object(dongle_power, "unit_active", return_value=False), \
             patch.object(dongle_power, "_start_unit", return_value=(False, "unit not found")), \
             patch.object(dongle_power, "_save_state"):
            ok, lines = dongle_power.power_on()
        self.assertFalse(ok)
        self.assertTrue(any("Errors" in ln for ln in lines))


# ---------------------------------------------------------------------------
# Schedule: _parse_hhmm
# ---------------------------------------------------------------------------

class TestParseHhmm(unittest.TestCase):
    def test_valid_times(self):
        self.assertEqual(dongle_power._parse_hhmm("00:00"), (0, 0))
        self.assertEqual(dongle_power._parse_hhmm("06:30"), (6, 30))
        self.assertEqual(dongle_power._parse_hhmm("23:59"), (23, 59))

    def test_invalid_returns_none(self):
        self.assertIsNone(dongle_power._parse_hhmm(""))
        self.assertIsNone(dongle_power._parse_hhmm("25:00"))
        self.assertIsNone(dongle_power._parse_hhmm("12:60"))
        self.assertIsNone(dongle_power._parse_hhmm("abc"))
        self.assertIsNone(dongle_power._parse_hhmm("1200"))


# ---------------------------------------------------------------------------
# Schedule: save_schedule / _sync_crontab
# ---------------------------------------------------------------------------

class TestSaveSchedule(unittest.TestCase):
    def test_writes_config_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "dongle-schedule.json"
            cron_calls = []

            with patch.object(dongle_power, "DONGLE_SCHEDULE_CONFIG_PATH", str(cfg_path)), \
                 patch.object(dongle_power, "_sync_crontab", side_effect=lambda c: cron_calls.append(c) or (True, "")):
                ok, err = dongle_power.save_schedule("00:00", "06:00", True)

            self.assertTrue(ok)
            saved = json.loads(cfg_path.read_text())
            self.assertTrue(saved["enabled"])
            self.assertEqual(saved["auto_off"], "00:00")
            self.assertEqual(saved["auto_on"], "06:00")

    def test_sync_crontab_adds_tagged_entries(self):
        cron_written = []

        def _fake_crontab_l():
            r = MagicMock()
            r.returncode = 0
            r.stdout = "# existing cron entry\n"
            return r

        def _fake_crontab_write(args, **kwargs):
            if args == ["crontab", "-"]:
                cron_written.append(kwargs.get("input", ""))
            return _make_ok_result()

        with patch.object(dongle_power.subprocess, "run", side_effect=lambda args, **kw: (
            _fake_crontab_l() if args == ["crontab", "-l"] else _fake_crontab_write(args, **kw)
        )):
            ok, err = dongle_power._sync_crontab(
                {"enabled": True, "auto_off": "00:00", "auto_on": "06:00"}
            )

        self.assertTrue(ok)
        self.assertEqual(len(cron_written), 1)
        written = cron_written[0]
        self.assertIn("# sb3-dongle-schedule", written)
        self.assertIn("action=off", written)
        self.assertIn("action=on", written)
        self.assertIn("0 0 * * *", written)
        self.assertIn("0 6 * * *", written)
        self.assertIn("# existing cron entry", written)

    def test_sync_crontab_removes_old_entries_on_disable(self):
        existing = (
            "0 0 * * * /usr/bin/curl -s -X POST http://127.0.0.1:5050/api/dongles/power -d 'action=off' > /dev/null 2>&1 # sb3-dongle-schedule\n"
            "0 6 * * * /usr/bin/curl -s -X POST http://127.0.0.1:5050/api/dongles/power -d 'action=on' > /dev/null 2>&1 # sb3-dongle-schedule\n"
            "# other cron job\n"
        )
        cron_written = []

        def _fake_run(args, **kwargs):
            if args == ["crontab", "-l"]:
                r = MagicMock()
                r.returncode = 0
                r.stdout = existing
                return r
            if args == ["crontab", "-"]:
                cron_written.append(kwargs.get("input", ""))
            return _make_ok_result()

        with patch.object(dongle_power.subprocess, "run", side_effect=_fake_run):
            ok, err = dongle_power._sync_crontab({"enabled": False, "auto_off": "", "auto_on": ""})

        self.assertTrue(ok)
        self.assertEqual(len(cron_written), 1)
        written = cron_written[0]
        self.assertNotIn("sb3-dongle-schedule", written)
        self.assertIn("# other cron job", written)

    def test_sync_crontab_replaces_existing_schedule(self):
        existing = (
            "0 0 * * * /usr/bin/curl ... # sb3-dongle-schedule\n"
            "0 6 * * * /usr/bin/curl ... # sb3-dongle-schedule\n"
        )
        cron_written = []

        def _fake_run(args, **kwargs):
            if args == ["crontab", "-l"]:
                r = MagicMock()
                r.returncode = 0
                r.stdout = existing
                return r
            cron_written.append(kwargs.get("input", ""))
            return _make_ok_result()

        with patch.object(dongle_power.subprocess, "run", side_effect=_fake_run):
            dongle_power._sync_crontab({"enabled": True, "auto_off": "01:00", "auto_on": "07:00"})

        written = cron_written[0]
        # cron format is "minute hour * * *"; 01:00 → "0 1 * * *", 07:00 → "0 7 * * *"
        self.assertIn("0 1 * * *", written)
        self.assertIn("0 7 * * *", written)
        # Old ones gone
        self.assertEqual(written.count("sb3-dongle-schedule"), 2)

    def test_sync_crontab_skips_invalid_times(self):
        cron_written = []

        def _fake_run(args, **kwargs):
            if args == ["crontab", "-l"]:
                r = MagicMock()
                r.returncode = 0
                r.stdout = ""
                return r
            cron_written.append(kwargs.get("input", ""))
            return _make_ok_result()

        with patch.object(dongle_power.subprocess, "run", side_effect=_fake_run):
            dongle_power._sync_crontab({"enabled": True, "auto_off": "bad", "auto_on": ""})

        written = cron_written[0] if cron_written else ""
        self.assertNotIn("sb3-dongle-schedule", written)


# ---------------------------------------------------------------------------
# State file helpers
# ---------------------------------------------------------------------------

class TestStateFile(unittest.TestCase):
    def test_load_state_returns_empty_dict_on_missing_file(self):
        with patch.object(dongle_power, "DONGLE_POWER_STATE_PATH", "/nonexistent/path/state.json"):
            result = dongle_power._load_state()
        self.assertEqual(result, {})

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = str(Path(tmpdir) / "dongle_power.json")
            with patch.object(dongle_power, "DONGLE_POWER_STATE_PATH", state_path):
                dongle_power._save_state("off", set_by="test")
                result = dongle_power._load_state()
        self.assertEqual(result["state"], "off")
        self.assertEqual(result["set_by"], "test")
        self.assertIn("set_at", result)


if __name__ == "__main__":
    unittest.main()
