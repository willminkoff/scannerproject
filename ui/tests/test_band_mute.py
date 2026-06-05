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
        self.assertEqual(
            state,
            {"airband": False, "ground": False, "digital": False, "vfo": False},
        )

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
        self.assertFalse(state["vfo"])
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
            self.bm._write_state_file({
                "airband": True, "ground": False, "digital": False, "vfo": False,
            })

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
            self.bm._write_state_file({
                "airband": True, "ground": False, "digital": False, "vfo": False,
            })

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
            self.bm._write_state_file({
                "airband": False, "ground": False, "digital": True, "vfo": False,
            })
        self.bm.reconcile_once()
        # Still exists; nothing crashed.
        self.assertTrue(flag.exists())

    def test_reconcile_creates_digital_flag_when_diverged(self) -> None:
        flag = Path(os.environ["OP25_AUDIO_MUTE_FLAG"])
        # Persist digital=True but flag does not yet exist.
        with self.bm._state_lock:
            self.bm._write_state_file({
                "airband": False, "ground": False, "digital": True, "vfo": False,
            })
        self.assertFalse(flag.exists())
        self.bm.reconcile_once()
        self.assertTrue(flag.exists())

    # ---- BAND_KEYS round-trip ----------------------------------------------

    def test_band_keys_cover_all_four(self) -> None:
        self.assertEqual(
            set(self.bm.BAND_KEYS),
            {"airband", "ground", "digital", "vfo"},
        )

    def test_vfo_unit_default_is_scanner_vlc_vfo(self) -> None:
        # Catch regressions in the unit-name mapping — band_mute.py and
        # the scanner-vlc-vfo systemd unit name must stay aligned, since
        # the watcher resolves sink-input identity via the unit's MainPID.
        self.assertEqual(self.bm._BAND_UNITS["vfo"], "scanner-vlc-vfo.service")

    # ---- wpctl-native path (pactl absent, e.g. micro) ---------------------

    @mock.patch("shutil.which")
    @mock.patch("subprocess.run")
    def test_wpctl_native_path_resolves_via_status_and_inspect(
        self,
        run_mock: mock.Mock,
        which_mock: mock.Mock,
    ) -> None:
        """On a PipeWire-only host (no pactl), the module must walk
        ``wpctl status`` for stream IDs, ``wpctl inspect`` each to
        match pid, then ``wpctl set-mute`` the matched stream."""
        def which_side(prog):
            return None if prog == "pactl" else "/usr/bin/" + prog
        which_mock.side_effect = which_side

        wpctl_status = (
            "PipeWire 'pipewire-0' [1.0.5]\n"
            " \xe2\x94\x94\xe2\x94\x80 Clients:\n"
            "       68. VLC media player (LibVLC 3.0.20)    [pid:8391]\n"
            "Audio\n"
            " \xe2\x94\x9c\xe2\x94\x80 Sinks:\n"
            " \xe2\x94\x82  *   48. Built-in Audio Analog Stereo\n"
            " \xe2\x94\x82  \n"
            " \xe2\x94\x9c\xe2\x94\x80 Sink endpoints:\n"
            " \xe2\x94\x82  \n"
            " \xe2\x94\x94\xe2\x94\x80 Streams:\n"
            "        69. VLC media player (LibVLC 3.0.20)\n"
            "             70. output_FL\n"
            "             71. output_FR\n"
            "Video\n"
        )
        # Note: \xe2\x94 are utf-8 bytes for the tree-drawing glyphs that
        # wpctl emits; reproducing them keeps the regex paths under test.
        wpctl_status = wpctl_status.encode("latin-1").decode("utf-8")
        wpctl_inspect_69 = (
            'id 69, type PipeWire:Interface:Node\n'
            '    application.process.binary = "vlc"\n'
            '    application.process.id = "8391"\n'
            '  * media.class = "Stream/Output/Audio"\n'
        )

        wpctl_calls: list[list[str]] = []
        def fake_run(cmd, **kwargs):
            if cmd[0] == "systemctl":
                return _fake_completed(rc=0, stdout="8391\n")
            if cmd[:2] == ["wpctl", "status"]:
                return _fake_completed(rc=0, stdout=wpctl_status)
            if cmd[:2] == ["wpctl", "inspect"]:
                if cmd[2] == "69":
                    return _fake_completed(rc=0, stdout=wpctl_inspect_69)
                return _fake_completed(rc=0, stdout="")
            if cmd[:2] == ["wpctl", "set-mute"]:
                wpctl_calls.append(list(cmd))
                return _fake_completed(rc=0, stdout="")
            return _fake_completed(rc=0, stdout="")

        run_mock.side_effect = fake_run
        ok, msg = self.bm.set_band("airband", True)
        self.assertTrue(ok, msg=msg)
        self.assertEqual(wpctl_calls, [["wpctl", "set-mute", "69", "1"]])

    @mock.patch("shutil.which")
    @mock.patch("subprocess.run")
    def test_no_live_stream_persists_intent_and_returns_ok(
        self,
        run_mock: mock.Mock,
        which_mock: mock.Mock,
    ) -> None:
        """When the VLC service IS running but no audio stream is live
        right now (squelched), set_band should:
          - persist intent (so watcher applies on next stream creation)
          - return ok=True (not an error from the operator's POV)
          - message should hint at squelched state for diagnostics."""
        def which_side(prog):
            return None if prog == "pactl" else "/usr/bin/" + prog
        which_mock.side_effect = which_side

        empty_status_no_streams = (
            "PipeWire 'pipewire-0' [1.0.5]\n"
            "Audio\n"
            " └─ Streams:\n"
            "Video\n"
        )
        def fake_run(cmd, **kwargs):
            if cmd[0] == "systemctl":
                return _fake_completed(rc=0, stdout="8391\n")
            if cmd[:2] == ["wpctl", "status"]:
                return _fake_completed(rc=0, stdout=empty_status_no_streams)
            return _fake_completed(rc=0, stdout="")
        run_mock.side_effect = fake_run

        ok, msg = self.bm.set_band("ground", True)
        self.assertTrue(ok, msg=msg)
        self.assertIn("squelched", msg)
        self.assertTrue(self.bm.get_state()["ground"])

    # ---- VFO: parallels airband path, but resolves scanner-vlc-vfo --------

    @mock.patch("shutil.which", return_value="/usr/bin/wpctl")
    @mock.patch("subprocess.run")
    def test_vfo_mute_resolves_pid_then_mutes_sink_input(
        self,
        run_mock: mock.Mock,
        which_mock: mock.Mock,
    ) -> None:
        """VFO mute walks the same pid → sink-input → wpctl set-mute path
        as airband; verifies the new BAND_KEYS entry threads through
        _apply_band correctly and the unit-name override resolves to
        scanner-vlc-vfo.service."""
        unit_seen: list[str] = []

        def fake_run(cmd, **kwargs):
            if cmd[0] == "systemctl" and "show" in cmd:
                # Capture the unit-name so we can assert the VFO unit is
                # the one consulted (not airband / ground by accident).
                unit_seen.append(cmd[-1])
                return _fake_completed(rc=0, stdout="73307\n")
            if cmd[0] == "pactl" and cmd[1:3] == ["list", "sink-inputs"]:
                # Re-use the analog VLC fixture but swap the pid to the
                # VFO MainPID we returned above.
                return _fake_completed(
                    rc=0,
                    stdout=_PACTL_LIST_WITH_VLC_ANALOG.replace("12345", "73307"),
                )
            if cmd[0] == "wpctl" and cmd[1] == "set-mute":
                return _fake_completed(rc=0, stdout="")
            return _fake_completed(rc=0, stdout="")

        run_mock.side_effect = fake_run
        ok, msg = self.bm.set_band("vfo", True)
        self.assertTrue(ok, msg=msg)
        wpctl_calls = [
            c.args[0] for c in run_mock.call_args_list if c.args[0][0] == "wpctl"
        ]
        self.assertEqual(wpctl_calls, [["wpctl", "set-mute", "42", "1"]])
        # Confirm we queried the VFO unit, not the airband / ground ones.
        self.assertIn("scanner-vlc-vfo.service", unit_seen)
        self.assertTrue(self.bm.get_state()["vfo"])

    @mock.patch("shutil.which", return_value="/usr/bin/wpctl")
    @mock.patch("subprocess.run")
    def test_vfo_unmute_passes_zero_to_wpctl(
        self,
        run_mock: mock.Mock,
        which_mock: mock.Mock,
    ) -> None:
        def fake_run(cmd, **kwargs):
            if cmd[0] == "systemctl":
                return _fake_completed(rc=0, stdout="73307\n")
            if cmd[0] == "pactl" and cmd[1:3] == ["list", "sink-inputs"]:
                return _fake_completed(
                    rc=0,
                    stdout=_PACTL_LIST_WITH_VLC_ANALOG.replace("12345", "73307"),
                )
            return _fake_completed(rc=0, stdout="")

        run_mock.side_effect = fake_run
        self.bm.set_band("vfo", False)
        wpctl_calls = [
            c.args[0] for c in run_mock.call_args_list if c.args[0][0] == "wpctl"
        ]
        self.assertEqual(wpctl_calls, [["wpctl", "set-mute", "42", "0"]])

    @mock.patch("shutil.which", return_value="/usr/bin/wpctl")
    @mock.patch("subprocess.run")
    def test_vfo_service_inactive_persists_but_returns_err(
        self,
        run_mock: mock.Mock,
        which_mock: mock.Mock,
    ) -> None:
        # If scanner-vlc-vfo.service is down (e.g. BT speaker disconnected),
        # set_band must still persist the intent so the watcher applies
        # mute the moment the service comes back up.
        def fake_run(cmd, **kwargs):
            if cmd[0] == "systemctl":
                return _fake_completed(rc=0, stdout="0\n")
            return _fake_completed(rc=0, stdout="")

        run_mock.side_effect = fake_run
        ok, msg = self.bm.set_band("vfo", True)
        self.assertFalse(ok)
        self.assertIn("not active", msg)
        self.assertTrue(self.bm.get_state()["vfo"])

    @mock.patch("shutil.which", return_value="/usr/bin/wpctl")
    @mock.patch("subprocess.run")
    def test_reconcile_reapplies_vfo_when_diverged(
        self,
        run_mock: mock.Mock,
        which_mock: mock.Mock,
    ) -> None:
        # Persisted state: vfo muted; live sink-input shows unmuted (e.g.
        # scanner-vlc-vfo restarted).  Watcher must re-mute.
        with self.bm._state_lock:
            self.bm._write_state_file({
                "airband": False, "ground": False, "digital": False, "vfo": True,
            })

        wpctl_called: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            if cmd[0] == "systemctl":
                return _fake_completed(rc=0, stdout="73307\n")
            if cmd[0] == "pactl" and cmd[1:3] == ["list", "sink-inputs"]:
                return _fake_completed(
                    rc=0,
                    stdout=_PACTL_LIST_WITH_VLC_ANALOG.replace("12345", "73307"),
                )
            if cmd[0] == "wpctl":
                wpctl_called.append(list(cmd))
                return _fake_completed(rc=0, stdout="")
            return _fake_completed(rc=0, stdout="")

        run_mock.side_effect = fake_run
        self.bm.reconcile_once()
        self.assertEqual(wpctl_called, [["wpctl", "set-mute", "42", "1"]])

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
