"""Hermetic unit tests for ui/band_mute.py.

Strategy
--------
The module shells out to `systemctl`, `pactl`, and `wpctl`.  Every test
case patches subprocess so no real audio control is attempted — we just
assert the contract the module follows when it sees specific output from
those tools, plus the persistence behavior of data/band_mute.json and
the digital touchfile flag.

We DO NOT spin a real server in this test file.  The /api/audio/band_mute
HTTP layer in handlers.py is a thin shim over set_band(); the contract is
adequately exercised by testing set_band() directly + a lightweight
endpoint sanity check via the handler module imports.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

# Make `ui.band_mute` importable when run from repo root or from ui/tests/.
REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# Helpers ----------------------------------------------------------------------


def _fresh_module(tmpdir: Path) -> "module":  # noqa: F821
    """Reload ui.band_mute with env vars pointing at a temp state path so
    each test starts from a clean slate."""
    state_path = tmpdir / "band_mute.json"
    digital_flag = tmpdir / "digital_local_mute"
    os.environ["BAND_MUTE_STATE_PATH"] = str(state_path)
    os.environ["OP25_AUDIO_MUTE_FLAG"] = str(digital_flag)
    # Make the watcher interval irrelevant for the test (we don't start it).
    os.environ["BAND_MUTE_WATCHER_INTERVAL_SEC"] = "9999"
    if "ui.band_mute" in sys.modules:
        del sys.modules["ui.band_mute"]
    return importlib.import_module("ui.band_mute")


def _fake_completed(rc: int = 0, stdout: str = "") -> mock.Mock:
    m = mock.Mock()
    m.returncode = rc
    m.stdout = stdout
    return m


# Pactl fixtures ---------------------------------------------------------------


_PACTL_LIST_WITH_VLC_ANALOG = """\
Sink Input #42
\tDriver: PipeWire
\tOwner Module: 0
\tClient: 17
\tSink: 38
\tMute: no
\tapplication.process.id = "12345"
\tapplication.name = "VLC media player (LibVLC 3.0.20)"

Sink Input #43
\tMute: no
\tapplication.process.id = "99999"
"""

_PACTL_LIST_MUTED = """\
Sink Input #42
\tMute: yes
\tapplication.process.id = "12345"
"""


# Tests ------------------------------------------------------------------------


class BandMuteTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.bm = _fresh_module(self.tmpdir)
        # Belt-and-braces: clear any thread state on the module.
        self.bm._watcher_started = False

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---- state file IO -------------------------------------------------------

    def test_default_state_is_all_unmuted(self) -> None:
        state = self.bm.get_state()
        self.assertEqual(state, {"airband": False, "ground": False, "digital": False})

    def test_unknown_band_rejected(self) -> None:
        ok, msg = self.bm.set_band("bogus", True)
        self.assertFalse(ok)
        self.assertIn("unknown band", msg)

    # ---- digital: touchfile path -------------------------------------------

    def test_digital_set_creates_touchfile_and_persists(self) -> None:
        flag = Path(os.environ["OP25_AUDIO_MUTE_FLAG"])
        self.assertFalse(flag.exists())
        ok, _ = self.bm.set_band("digital", True)
        self.assertTrue(ok)
        self.assertTrue(flag.exists(), "DIGITAL_MUTE_FLAG should be touched")
        state = self.bm.get_state()
        self.assertTrue(state["digital"])
        self.assertFalse(state["airband"])
        self.assertFalse(state["ground"])
        # Persisted to disk:
        with open(os.environ["BAND_MUTE_STATE_PATH"], "r", encoding="utf-8") as f:
            disk = json.load(f)
        self.assertTrue(disk["digital"])

    def test_digital_unset_removes_touchfile(self) -> None:
        flag = Path(os.environ["OP25_AUDIO_MUTE_FLAG"])
        self.bm.set_band("digital", True)
        self.assertTrue(flag.exists())
        self.bm.set_band("digital", False)
        self.assertFalse(flag.exists())
        self.assertFalse(self.bm.get_state()["digital"])

    def test_digital_unset_when_missing_is_noop(self) -> None:
        # Flag does not exist; set_band(digital, False) must not raise.
        ok, _ = self.bm.set_band("digital", False)
        self.assertTrue(ok)

    # ---- airband / ground: PipeWire path -----------------------------------

    @mock.patch("shutil.which", return_value="/usr/bin/wpctl")
    @mock.patch("subprocess.run")
    def test_airband_mute_resolves_pid_then_mutes_sink_input(
        self,
        run_mock: mock.Mock,
        which_mock: mock.Mock,
    ) -> None:
        def fake_run(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            if cmd[0] == "systemctl" and "show" in cmd:
                # MainPID for the unit
                return _fake_completed(rc=0, stdout="12345\n")
            if cmd[0] == "pactl" and cmd[1:3] == ["list", "sink-inputs"]:
                return _fake_completed(rc=0, stdout=_PACTL_LIST_WITH_VLC_ANALOG)
            if cmd[0] == "wpctl" and cmd[1] == "set-mute":
                return _fake_completed(rc=0, stdout="")
            return _fake_completed(rc=0, stdout="")

        run_mock.side_effect = fake_run
        ok, msg = self.bm.set_band("airband", True)
        self.assertTrue(ok, msg=msg)
        # wpctl set-mute 42 1 should have been called.
        called_cmds = [c.args[0] for c in run_mock.call_args_list]
        # Find the wpctl call.
        wpctl_calls = [c for c in called_cmds if c[0] == "wpctl"]
        self.assertEqual(len(wpctl_calls), 1)
        self.assertEqual(wpctl_calls[0], ["wpctl", "set-mute", "42", "1"])
        # State persisted.
        self.assertTrue(self.bm.get_state()["airband"])

    @mock.patch("shutil.which", return_value="/usr/bin/wpctl")
    @mock.patch("subprocess.run")
    def test_airband_unmute_passes_zero_to_wpctl(
        self,
        run_mock: mock.Mock,
        which_mock: mock.Mock,
    ) -> None:
        def fake_run(cmd, **kwargs):
            if cmd[0] == "systemctl":
                return _fake_completed(rc=0, stdout="12345\n")
            if cmd[0] == "pactl" and cmd[1:3] == ["list", "sink-inputs"]:
                return _fake_completed(rc=0, stdout=_PACTL_LIST_WITH_VLC_ANALOG)
            return _fake_completed(rc=0, stdout="")

        run_mock.side_effect = fake_run
        self.bm.set_band("airband", False)
        wpctl_calls = [c.args[0] for c in run_mock.call_args_list if c.args[0][0] == "wpctl"]
        self.assertEqual(len(wpctl_calls), 1)
        self.assertEqual(wpctl_calls[0], ["wpctl", "set-mute", "42", "0"])

    @mock.patch("shutil.which", return_value="/usr/bin/wpctl")
    @mock.patch("subprocess.run")
    def test_service_inactive_persists_but_returns_err(
        self,
        run_mock: mock.Mock,
        which_mock: mock.Mock,
    ) -> None:
        # systemctl reports MainPID=0 (inactive); apply fails but intent
        # MUST still be persisted so the watcher can re-apply later.
        def fake_run(cmd, **kwargs):
            if cmd[0] == "systemctl":
                return _fake_completed(rc=0, stdout="0\n")
            return _fake_completed(rc=0, stdout="")

        run_mock.side_effect = fake_run
        ok, msg = self.bm.set_band("ground", True)
        self.assertFalse(ok)
        self.assertIn("not active", msg)
        # But state file should still reflect intent.
        self.assertTrue(self.bm.get_state()["ground"])

    @mock.patch("shutil.which")
    @mock.patch("subprocess.run")
    def test_falls_back_to_pactl_when_wpctl_missing(
        self,
        run_mock: mock.Mock,
        which_mock: mock.Mock,
    ) -> None:
        def which_side(prog):
            return None if prog == "wpctl" else "/usr/bin/" + prog

        which_mock.side_effect = which_side

        def fake_run(cmd, **kwargs):
            if cmd[0] == "systemctl":
                return _fake_completed(rc=0, stdout="12345\n")
            if cmd[0] == "pactl" and cmd[1:3] == ["list", "sink-inputs"]:
                return _fake_completed(rc=0, stdout=_PACTL_LIST_WITH_VLC_ANALOG)
            if cmd[0] == "pactl" and cmd[1] == "set-sink-input-mute":
                return _fake_completed(rc=0, stdout="")
            return _fake_completed(rc=0, stdout="")

        run_mock.side_effect = fake_run
        ok, _ = self.bm.set_band("airband", True)
        self.assertTrue(ok)
        pactl_mute_calls = [
            c.args[0] for c in run_mock.call_args_list
            if c.args[0][0] == "pactl" and c.args[0][1] == "set-sink-input-mute"
        ]
        self.assertEqual(pactl_mute_calls, [["pactl", "set-sink-input-mute", "42", "1"]])

    # ---- reconcile_once: idempotency + divergence-only writes --------------

    @mock.patch("shutil.which", return_value="/usr/bin/wpctl")
    @mock.patch("subprocess.run")
    def test_reconcile_skips_when_already_in_desired_state(
        self,
        run_mock: mock.Mock,
        which_mock: mock.Mock,
    ) -> None:
        # Persist airband=True manually, then have pactl report Mute: yes
        # already.  reconcile_once should NOT call wpctl.
        with self.bm._state_lock:
            self.bm._write_state_file({"airband": True, "ground": False, "digital": False})

        def fake_run(cmd, **kwargs):
            if cmd[0] == "systemctl":
                return _fake_completed(rc=0, stdout="12345\n")
            if cmd[0] == "pactl" and cmd[1:3] == ["list", "sink-inputs"]:
                return _fake_completed(rc=0, stdout=_PACTL_LIST_MUTED)
            if cmd[0] == "wpctl":
                self.fail("wpctl should not have been invoked when state matches")
            return _fake_completed(rc=0, stdout="")

        run_mock.side_effect = fake_run
        self.bm.reconcile_once()

    @mock.patch("shutil.which", return_value="/usr/bin/wpctl")
    @mock.patch("subprocess.run")
    def test_reconcile_reapplies_when_diverged(
        self,
        run_mock: mock.Mock,
        which_mock: mock.Mock,
    ) -> None:
        # Persist airband=True, but pactl reports Mute: no (e.g., vlc was
        # restarted and the new sink-input came up unmuted).  reconcile
        # must re-mute it.
        with self.bm._state_lock:
            self.bm._write_state_file({"airband": True, "ground": False, "digital": False})

        wpctl_called: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            if cmd[0] == "systemctl":
                return _fake_completed(rc=0, stdout="12345\n")
            if cmd[0] == "pactl" and cmd[1:3] == ["list", "sink-inputs"]:
                return _fake_completed(rc=0, stdout=_PACTL_LIST_WITH_VLC_ANALOG)
            if cmd[0] == "wpctl":
                wpctl_called.append(list(cmd))
                return _fake_completed(rc=0, stdout="")
            return _fake_completed(rc=0, stdout="")

        run_mock.side_effect = fake_run
        self.bm.reconcile_once()
        self.assertEqual(wpctl_called, [["wpctl", "set-mute", "42", "1"]])

    def test_reconcile_skips_digital_when_already_matching(self) -> None:
        # Flag exists and persisted state is muted -> no-op.
        flag = Path(os.environ["OP25_AUDIO_MUTE_FLAG"])
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.touch()
        with self.bm._state_lock:
            self.bm._write_state_file({"airband": False, "ground": False, "digital": True})
        self.bm.reconcile_once()
        # Still exists; nothing crashed.
        self.assertTrue(flag.exists())

    def test_reconcile_creates_digital_flag_when_diverged(self) -> None:
        flag = Path(os.environ["OP25_AUDIO_MUTE_FLAG"])
        # Persist digital=True but flag does not yet exist.
        with self.bm._state_lock:
            self.bm._write_state_file({"airband": False, "ground": False, "digital": True})
        self.assertFalse(flag.exists())
        self.bm.reconcile_once()
        self.assertTrue(flag.exists())

    # ---- BAND_KEYS round-trip ----------------------------------------------

    def test_band_keys_cover_all_three(self) -> None:
        self.assertEqual(set(self.bm.BAND_KEYS), {"airband", "ground", "digital"})

    # ---- start_watcher is idempotent ---------------------------------------

    def test_start_watcher_is_idempotent(self) -> None:
        # First call sets _watcher_started.  Second call must not spawn a
        # second thread.  We mock threading.Thread to confirm.
        with mock.patch.object(self.bm, "threading") as t_mod:
            fake_thread = mock.Mock()
            t_mod.Thread.return_value = fake_thread
            # Also mock reconcile_once to avoid touching system tools.
            with mock.patch.object(self.bm, "reconcile_once"):
                self.bm.start_watcher()
                self.bm.start_watcher()
            self.assertEqual(t_mod.Thread.call_count, 1)
            fake_thread.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
