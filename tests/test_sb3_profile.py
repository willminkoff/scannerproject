"""Phase 2 tests — profile schema, translator, and the profile CLI."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from sb3 import backends, profilecmd, translator
from sb3.gitdeploy import deploy_root
from sb3.profile import Channel, ProfileError, load_profile, parse_profile
from sb3.sdrangel import SDRangelClient
from sb3.state import State

PROFILE_PATH = deploy_root() / "profiles" / "air-airband-nashville.json"


def _base_data():
    return json.loads(PROFILE_PATH.read_text())


class TestSchemaValid(unittest.TestCase):
    def test_repo_profile_parses(self):
        p = load_profile(PROFILE_PATH)
        self.assertEqual(p.name, "air.airband.nashville")
        self.assertEqual(p.role, "air")
        self.assertEqual(p.mode, "camp")
        self.assertEqual(p.serial, "95339533")
        self.assertEqual(len(p.channels), 4)

    def test_baseband_fits_all_channels(self):
        p = load_profile(PROFILE_PATH)
        for c in p.channels:
            self.assertLessEqual(abs(c.offset_from(p.center_freq_hz)) + c.rf_bw_hz / 2,
                                 p.half_window_hz)

    def test_keepalive_applied_first(self):
        p = load_profile(PROFILE_PATH)
        order = p.channels_apply_order()
        self.assertTrue(order[0].keepalive)
        self.assertFalse(any(c.keepalive for c in order[1:]))

    def test_exactly_one_keepalive(self):
        p = load_profile(PROFILE_PATH)
        self.assertEqual(len(p.keepalive_channels), 1)


class TestSchemaInvalid(unittest.TestCase):
    def _reject(self, mutate, code):
        data = _base_data()
        mutate(data)
        with self.assertRaises(ProfileError) as ctx:
            parse_profile(data)
        self.assertEqual(ctx.exception.code, code, msg=str(ctx.exception))

    def test_channel_outside_baseband_is_rejected(self):
        # log2_decim=4 → 128 kHz window; the ±525 kHz channels cannot fit.
        self._reject(lambda d: d["device"].__setitem__("log2_decim", 4),
                     "profile-channel-outside-baseband")

    def test_two_keepalive_rejected(self):
        self._reject(lambda d: d["channels"][1].__setitem__("keepalive", True),
                     "profile-keepalive-count")

    def test_zero_keepalive_rejected(self):
        self._reject(lambda d: d["channels"][0].__setitem__("keepalive", False),
                     "profile-keepalive-count")

    def test_bad_role_rejected(self):
        self._reject(lambda d: d.__setitem__("role", "banana"), "profile-bad-role")

    def test_bad_mode_rejected(self):
        self._reject(lambda d: d.__setitem__("mode", "sideways"), "profile-bad-mode")

    def test_bad_demod_rejected(self):
        self._reject(lambda d: d["channels"][0].__setitem__("demod", "QAM"),
                     "profile-bad-demod")

    def test_mixed_demods_rejected(self):
        self._reject(lambda d: d["channels"][1].__setitem__("demod", "NFM"),
                     "profile-mixed-demods")

    def test_duplicate_freq_rejected(self):
        self._reject(lambda d: d["channels"][1].__setitem__("freq_hz",
                                                            d["channels"][0]["freq_hz"]),
                     "profile-duplicate-freq")

    def test_missing_mount_rejected(self):
        self._reject(lambda d: d.pop("mount"), "profile-missing-key")

    def test_bad_hardware_rejected(self):
        self._reject(lambda d: d["device"].__setitem__("hardware_id", "FunCube"),
                     "profile-bad-hw")


class _RecordingClient(SDRangelClient):
    """Execute-mode client with _req stubbed to canned responses + recording."""

    def __init__(self, emit, *, bound_serial="OLD", channels_after=0):
        super().__init__(execute=True, emit=emit, sleep=lambda s: None)
        self._bound = bound_serial
        self._hw = "RTLSDR"
        self._state = "idle"
        self._chan_after = channels_after

    def _req(self, method, path, body=None, timeout=8.0):
        self.calls.append((method, path, body))
        if method == "GET" and path == "":
            return 200, {"devicesetlist": {"deviceSets": [{}, {}]}}
        if method == "GET" and path == "/audio":
            return 200, {"outputDevices": [
                {"index": -1, "name": "System default device", "copyToUDP": 1},
                {"index": 0, "name": "Mac mini Speakers", "copyToUDP": 0}]}
        # A rebind (PUT device) makes the deviceset report the NEW device as
        # enumerated — mirrors real SDRangel, and lets wait_device_ready pass.
        if method == "PUT" and path.endswith("/device") and body:
            self._bound = body.get("serial", self._bound)
            self._hw = body.get("hwType", self._hw)
            self._state = "idle"
            return 200, {}
        if method == "POST" and path.endswith("/device/run"):
            self._state = "running"
            return 200, {}
        if method == "GET" and path.endswith("/device/settings"):
            return 200, {"rtlSdrSettings": {"centerFrequency": 118925000}}
        if method == "GET" and path.startswith("/deviceset/"):
            return 200, {"samplingDevice": {"serial": self._bound,
                                            "hwType": self._hw, "state": self._state},
                         "channels": [{}] * self._chan_after}
        return 200, {}


class TestTranslatorApply(unittest.TestCase):
    def setUp(self):
        self.lines = []
        _sp = mock.patch("sb3.translator.time.sleep", lambda *_: None)
        _sp.start(); self.addCleanup(_sp.stop)

    def _emit(self, m):
        self.lines.append(m)

    def _prof(self):
        return load_profile(PROFILE_PATH)

    def _patched(self, *, mount_present_after=True, idx0="Mac mini Speakers"):
        # mount is 404 before, 200 after (audio chain came up)
        seq = [backends.MountState("neptune-air.mp3", 404, False)]
        seq += [backends.MountState("neptune-air.mp3", 200 if mount_present_after else 404,
                                    mount_present_after)] * 6
        return (
            mock.patch.object(backends, "mount_state", side_effect=seq),
            mock.patch.object(backends, "resolve_audio_tap", return_value=(idx0, -1)),
            mock.patch.object(backends, "icecast_mounts", return_value=["neptune-air.mp3"]),
        )

    def test_apply_rebinds_when_serial_differs(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            st = State(Path(td))
            with self._patched()[0], self._patched()[1], self._patched()[2]:
                c = _RecordingClient(self._emit, bound_serial="56919602", channels_after=4)
                rc = translator.apply(self._prof(), execute=True, emit=self._emit,
                                      state=st, client=c)
            self.assertEqual(rc, translator.OK)
            self.assertTrue(any(m == "PUT" and "/device" in p for m, p, _ in c.calls),
                            "must rebind when serial differs")

    def test_apply_no_rebind_when_serial_matches(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            st = State(Path(td))
            with self._patched()[0], self._patched()[1], self._patched()[2]:
                c = _RecordingClient(self._emit, bound_serial="95339533", channels_after=4)
                rc = translator.apply(self._prof(), execute=True, emit=self._emit,
                                      state=st, client=c)
            self.assertEqual(rc, translator.OK)
            self.assertFalse(any(m == "PUT" for m, p, _ in c.calls),
                             "must NOT rebind when serial already matches")

    def test_apply_channels_in_keepalive_first_order(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            st = State(Path(td))
            with self._patched()[0], self._patched()[1], self._patched()[2]:
                c = _RecordingClient(self._emit, bound_serial="95339533", channels_after=4)
                translator.apply(self._prof(), execute=True, emit=self._emit,
                                 state=st, client=c)
            # first channel PATCH must carry the keepalive channel's title
            patches = [b for m, p, b in c.calls
                       if m == "PATCH" and "/channel/" in p and b and "AMDemodSettings" in b]
            self.assertIn("KBNA App/Dep East", patches[0]["AMDemodSettings"]["title"])

    def test_apply_toggles_copy_to_udp(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            st = State(Path(td))
            with self._patched()[0], self._patched()[1], self._patched()[2]:
                c = _RecordingClient(self._emit, bound_serial="95339533", channels_after=4)
                translator.apply(self._prof(), execute=True, emit=self._emit,
                                 state=st, client=c)
            udp = [b for m, p, b in c.calls if p == "/audio/output/parameters"]
            self.assertEqual(udp[0]["copyToUDP"], 0)   # off first
            self.assertEqual(udp[1]["copyToUDP"], 1)   # then on

    def test_apply_removes_marker_and_records_on_success(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            st = State(Path(td))
            marker = Path(td) / "paused"
            marker.write_text("")
            with mock.patch.object(State, "pause_marker",
                                   new=property(lambda self: marker)):
                with self._patched()[0], self._patched()[1], self._patched()[2]:
                    c = _RecordingClient(self._emit, bound_serial="95339533", channels_after=4)
                    rc = translator.apply(self._prof(), execute=True, emit=self._emit,
                                          state=st, client=c)
                self.assertEqual(rc, translator.OK)
                self.assertFalse(marker.exists(), "marker removed as final step")
            self.assertIsNotNone(st.read_loaded_profile())
            self.assertEqual(st.read_loaded_profile()["name"], "air.airband.nashville")

    def test_apply_unwinds_and_keeps_marker_when_mount_fails(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            st = State(Path(td))
            marker = Path(td) / "paused"
            marker.write_text("")
            with mock.patch.object(State, "pause_marker",
                                   new=property(lambda self: marker)):
                pats = self._patched(mount_present_after=False)
                with pats[0], pats[1], pats[2]:
                    c = _RecordingClient(self._emit, bound_serial="95339533", channels_after=4)
                    rc = translator.apply(self._prof(), execute=True, emit=self._emit,
                                          state=st, client=c)
                self.assertEqual(rc, translator.INVARIANT_VIOLATED)
                self.assertTrue(marker.exists(),
                                "marker must stay if the mount never came up")
            self.assertIsNone(st.read_loaded_profile(),
                              "no loaded-profile record on failure")
            self.assertIn("unwinding", "\n".join(self.lines))

    def test_apply_refuses_when_a_different_profile_is_loaded_and_live(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            st = State(Path(td))
            st.write_loaded_profile({"name": "ground.something.else",
                                     "mount": "neptune-air.mp3"})
            with mock.patch.object(backends, "mount_state",
                                   return_value=backends.MountState("neptune-air.mp3", 200, True)):
                c = _RecordingClient(self._emit, bound_serial="95339533")
                rc = translator.apply(self._prof(), execute=True, emit=self._emit,
                                      state=st, client=c)
            self.assertEqual(rc, translator.REFUSED)
            self.assertIn("different profile", "\n".join(self.lines))

    def test_apply_takes_over_leftover_live_mount_when_no_profile_loaded(self):
        # Air mount live from a disposable NFM scanner on ds0, no profile on
        # record → apply proceeds and reconfigures ds0 (the real Neptune case
        # after the mount rename).
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            st = State(Path(td))
            seq = [backends.MountState("neptune-air.mp3", 200, True)]   # live at baseline
            seq += [backends.MountState("neptune-air.mp3", 200, True)] * 6
            with mock.patch.object(backends, "mount_state", side_effect=seq), \
                 mock.patch.object(backends, "resolve_audio_tap",
                                   return_value=("System default device", -1)), \
                 mock.patch.object(backends, "icecast_mounts",
                                   return_value=["neptune-air.mp3"]):
                c = _RecordingClient(self._emit, bound_serial="56919602", channels_after=4)
                rc = translator.apply(self._prof(), execute=True, emit=self._emit,
                                      state=st, client=c)
            self.assertEqual(rc, translator.OK)
            self.assertIn("take it over", "\n".join(self.lines))


class TestTranslatorUnload(unittest.TestCase):
    def setUp(self):
        self.lines = []
        _sp = mock.patch("sb3.translator.time.sleep", lambda *_: None)
        _sp.start(); self.addCleanup(_sp.stop)

    def _emit(self, m):
        self.lines.append(m)

    def test_unload_clears_channels_stops_udp_keeps_marker(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            st = State(Path(td))
            st.write_loaded_profile({"name": "x"})
            marker = Path(td) / "paused"
            marker.write_text("")
            with mock.patch.object(State, "pause_marker",
                                   new=property(lambda self: marker)):
                c = _RecordingClient(self._emit, bound_serial="95339533", channels_after=4)
                rc = translator.unload(load_profile(PROFILE_PATH), execute=True,
                                       emit=self._emit, state=st, client=c)
            self.assertEqual(rc, translator.OK)
            self.assertTrue(any(p == "/audio/output/parameters" and b.get("copyToUDP") == 0
                                for m, p, b in c.calls if b))
            self.assertTrue(marker.exists(), "unload must NOT restore the marker")
            self.assertIsNone(st.read_loaded_profile())


class TestObserve(unittest.TestCase):
    def setUp(self):
        self.lines = []

    def _emit(self, m):
        self.lines.append(m)

    def _ds(self, **kw):
        base = dict(index=0, hw_type="RTLSDR", serial="95339533", state="running",
                    center_hz=118925000, channels=[])
        base.update(kw)
        return backends.DevicesetState(**base)

    def test_no_profile(self):
        with mock.patch.object(State, "read_loaded_profile", return_value=None):
            self.assertEqual(translator.observe(None, emit=self._emit), "no-profile")

    def test_matches(self):
        rec = {"deviceset_index": 0, "serial": "95339533",
               "channel_freqs": [1, 2, 3, 4]}
        with mock.patch.object(State, "read_loaded_profile", return_value=rec), \
             mock.patch.object(backends, "sdrangel_devicesets", return_value=[self._ds()]), \
             mock.patch.object(backends, "sdrangel_channels", return_value=[{}] * 4):
            self.assertEqual(translator.observe(None, emit=self._emit), "matches")

    def test_drifted_on_wrong_serial(self):
        rec = {"deviceset_index": 0, "serial": "95339533", "channel_freqs": [1, 2, 3, 4]}
        with mock.patch.object(State, "read_loaded_profile", return_value=rec), \
             mock.patch.object(backends, "sdrangel_devicesets",
                               return_value=[self._ds(serial="56919602")]), \
             mock.patch.object(backends, "sdrangel_channels", return_value=[{}] * 4):
            self.assertEqual(translator.observe(None, emit=self._emit), "drifted")

    def test_phantom(self):
        rec = {"deviceset_index": 0, "serial": "95339533", "channel_freqs": []}
        with mock.patch.object(State, "read_loaded_profile", return_value=rec), \
             mock.patch.object(backends, "sdrangel_devicesets",
                               return_value=[self._ds(hw_type="AaroniaRTSA", serial=None)]):
            self.assertEqual(translator.observe(None, emit=self._emit), "phantom")


class TestProfileCLI(unittest.TestCase):
    def setUp(self):
        self.lines = []

    def _emit(self, m):
        self.lines.append(m)

    def test_resolve_dotted_name(self):
        p = profilecmd.resolve_profile_path("air.airband.nashville")
        self.assertIsNotNone(p)
        self.assertTrue(p.name.endswith("air-airband-nashville.json"))

    def test_load_dry_run_prints_rest_calls(self):
        with mock.patch.object(backends, "mount_state",
                               return_value=backends.MountState("neptune-air.mp3", 404, False)):
            rc = profilecmd.run("load", "air.airband.nashville", execute=False, emit=self._emit)
        out = "\n".join(self.lines)
        self.assertEqual(rc, translator.OK)
        self.assertIn("would: PUT /deviceset/0/device", out)
        self.assertIn("copyToUDP", out)
        self.assertIn("keepalive first", out)

    def test_invalid_profile_is_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.json"
            data = _base_data()
            data["device"]["log2_decim"] = 4   # channels no longer fit
            bad.write_text(json.dumps(data))
            rc = profilecmd.run("load", str(bad), execute=False, emit=self._emit)
        self.assertEqual(rc, translator.REFUSED)
        self.assertIn("INVALID", "\n".join(self.lines))


if __name__ == "__main__":
    unittest.main()


class TestClientHardening(unittest.TestCase):
    """wait_device_ready + REST backoff (the 2026-07-19 wedge fix)."""

    def setUp(self):
        self.lines = []

    def _emit(self, m):
        self.lines.append(m)

    def _client(self, responses):
        """responses: dict path-substring → list of (status, body) to return in order."""
        from sb3.sdrangel import SDRangelClient
        c = SDRangelClient(execute=True, emit=self._emit, sleep=lambda s: None)
        state = {k: list(v) for k, v in responses.items()}

        def fake_req(method, path, body=None, timeout=8.0):
            c.calls.append((method, path, body))
            for key, seq in state.items():
                if key in path or (key == "ROOT" and path == ""):
                    return seq.pop(0) if len(seq) > 1 else seq[0]
            return 200, {}
        c._req = fake_req
        return c

    def test_wait_device_ready_returns_true_when_device_enumerates(self):
        # first poll: still phantom; second: ready
        c = self._client({
            "ROOT": [(200, {})],
            "/deviceset/0": [
                (200, {"samplingDevice": {"hwType": "Unknown", "serial": None, "state": "idle"}}),
                (200, {"samplingDevice": {"hwType": "RTLSDR", "serial": "95339533", "state": "idle"}}),
            ],
        })
        self.assertTrue(c.wait_device_ready(0, "RTLSDR", "95339533", timeout=5))

    def test_wait_device_ready_times_out_on_error_state(self):
        c = self._client({
            "ROOT": [(200, {})],
            "/deviceset/0": [(200, {"samplingDevice": {"hwType": "RTLSDR",
                                                       "serial": "95339533",
                                                       "state": "error"}})],
        })
        self.assertFalse(c.wait_device_ready(0, "RTLSDR", "95339533", timeout=2))
        self.assertIn("NOT ready", "\n".join(self.lines))

    def test_wait_device_ready_wrong_serial_times_out(self):
        c = self._client({
            "ROOT": [(200, {})],
            "/deviceset/0": [(200, {"samplingDevice": {"hwType": "RTLSDR",
                                                       "serial": "56919602",
                                                       "state": "idle"}})],
        })
        self.assertFalse(c.wait_device_ready(0, "RTLSDR", "95339533", timeout=2))

    def test_wait_rest_healthy_recovers_after_backoff(self):
        c = self._client({"ROOT": [(None, {}), (None, {}), (200, {})]})
        self.assertTrue(c.wait_rest_healthy(timeout=30))
        self.assertIn("REST recovered", "\n".join(self.lines))

    def test_wait_rest_healthy_gives_up_cleanly(self):
        c = self._client({"ROOT": [(None, {})]})   # never recovers
        self.assertFalse(c.wait_rest_healthy(timeout=2))
        self.assertIn("did not recover", "\n".join(self.lines))

    def test_rebind_waits_for_device_ready(self):
        c = self._client({
            "ROOT": [(200, {})],
            "/device/run": [(200, {})],
            "/deviceset/0": [(200, {"samplingDevice": {"hwType": "RTLSDR",
                                                       "serial": "95339533",
                                                       "state": "idle"}})],
        })
        # PUT returns 200; the sequence for the deviceset GET reports ready.
        ok = c.rebind_device(0, "RTLSDR", "95339533")
        self.assertTrue(ok)
        self.assertTrue(any(m == "PUT" and p == "/deviceset/0/device" for m, p, _ in c.calls))
        self.assertIn("device ready", "\n".join(self.lines))


class TestSoftRecycle(unittest.TestCase):
    """ensure_running must recover GENTLY — soft stop→run before any rebind."""

    def setUp(self):
        self.lines = []

    def _emit(self, m):
        self.lines.append(m)

    def _client(self, state_seq):
        """A client whose device_state walks through state_seq on each GET."""
        from sb3.sdrangel import SDRangelClient
        c = SDRangelClient(execute=True, emit=self._emit, sleep=lambda s: None)
        seq = list(state_seq)
        t = {"now": 0.0}
        c._now = lambda: t["now"]

        def fake_req(method, path, body=None, timeout=8.0):
            c.calls.append((method, path, body))
            t["now"] += 1.0   # each op advances the clock 1s
            if path == "" or path == "/audio":
                return 200, {}
            if method == "GET" and path.startswith("/deviceset/"):
                st = seq.pop(0) if len(seq) > 1 else seq[0]
                return 200, {"samplingDevice": {"state": st, "hwType": "RTLSDR",
                                                "serial": "95339533"}}
            return 200, {}
        c._req = fake_req
        return c

    def test_soft_retry_recovers_without_rebind(self):
        # error on first check, running after the soft stop→run.
        c = self._client(["error", "running"])
        ok = c.ensure_running(0, "RTLSDR", "95339533")
        self.assertTrue(ok)
        self.assertFalse(any(m == "PUT" for m, p, _ in c.calls),
                         "soft recovery must NOT rebind")
        self.assertIn("soft retry", "\n".join(self.lines))

    def test_rebind_is_last_resort_after_soft_fails(self):
        # never reaches running via soft; must fall through to ONE rebind.
        c = self._client(["error"])   # always error
        c.ensure_running(0, "RTLSDR", "95339533", budget_sec=1000)
        puts = [1 for m, p, _ in c.calls if m == "PUT"]
        self.assertEqual(len(puts), 1, "rebind must happen at most ONCE")
        self.assertIn("last resort", "\n".join(self.lines))

    def test_budget_stops_the_hammering(self):
        c = self._client(["error"])
        # tiny budget → give up in the soft loop, no rebind
        c.ensure_running(0, "RTLSDR", "95339533", budget_sec=1.0)
        self.assertFalse(any(m == "PUT" for m, p, _ in c.calls),
                         "must not rebind once the budget is blown")
        self.assertIn("budget", "\n".join(self.lines))
