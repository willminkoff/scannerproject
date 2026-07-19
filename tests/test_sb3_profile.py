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
        self.assertEqual(p.serial, "83241970")
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
        self._chan_after = channels_after

    def _req(self, method, path, body=None, timeout=8.0):
        self.calls.append((method, path, body))
        if method == "GET" and path == "":
            return 200, {}
        if method == "GET" and path.endswith("/device/settings"):
            return 200, {"rtlSdrSettings": {"centerFrequency": 118925000}}
        if method == "GET" and path.startswith("/deviceset/"):
            return 200, {"samplingDevice": {"serial": self._bound},
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
            mock.patch.object(backends, "resolve_idx0_audio_name", return_value=idx0),
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
                c = _RecordingClient(self._emit, bound_serial="83241970", channels_after=4)
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
                c = _RecordingClient(self._emit, bound_serial="83241970", channels_after=4)
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
                c = _RecordingClient(self._emit, bound_serial="83241970", channels_after=4)
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
                    c = _RecordingClient(self._emit, bound_serial="83241970", channels_after=4)
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
                    c = _RecordingClient(self._emit, bound_serial="83241970", channels_after=4)
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
                c = _RecordingClient(self._emit, bound_serial="83241970")
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
                 mock.patch.object(backends, "resolve_idx0_audio_name",
                                   return_value="Mac mini Speakers"), \
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
                c = _RecordingClient(self._emit, bound_serial="83241970", channels_after=4)
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
        base = dict(index=0, hw_type="RTLSDR", serial="83241970", state="running",
                    center_hz=118925000, channels=[])
        base.update(kw)
        return backends.DevicesetState(**base)

    def test_no_profile(self):
        with mock.patch.object(State, "read_loaded_profile", return_value=None):
            self.assertEqual(translator.observe(None, emit=self._emit), "no-profile")

    def test_matches(self):
        rec = {"deviceset_index": 0, "serial": "83241970",
               "channel_freqs": [1, 2, 3, 4]}
        with mock.patch.object(State, "read_loaded_profile", return_value=rec), \
             mock.patch.object(backends, "sdrangel_devicesets", return_value=[self._ds()]), \
             mock.patch.object(backends, "sdrangel_channels", return_value=[{}] * 4):
            self.assertEqual(translator.observe(None, emit=self._emit), "matches")

    def test_drifted_on_wrong_serial(self):
        rec = {"deviceset_index": 0, "serial": "83241970", "channel_freqs": [1, 2, 3, 4]}
        with mock.patch.object(State, "read_loaded_profile", return_value=rec), \
             mock.patch.object(backends, "sdrangel_devicesets",
                               return_value=[self._ds(serial="56919602")]), \
             mock.patch.object(backends, "sdrangel_channels", return_value=[{}] * 4):
            self.assertEqual(translator.observe(None, emit=self._emit), "drifted")

    def test_phantom(self):
        rec = {"deviceset_index": 0, "serial": "83241970", "channel_freqs": []}
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
