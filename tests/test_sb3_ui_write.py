"""Phase 3.2 tests — the Airband tab write path (form-encoded → SDRangel)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from sb3 import backends
from sb3.state import State
from sb3.ui import routes


# A fake SDRangelClient recording PATCH calls; models a 2-channel airband ds0
# where channel 0 is the keepalive (offset -525000) and channel 1 is real.
class _FakeClient:
    def __init__(self, healthy=True, has_device=True):
        self.calls = []
        self._healthy = healthy
        self._has_device = has_device

    def wait_rest_healthy(self, timeout=30.0):
        return self._healthy

    def sampling_device(self, idx):
        if not self._has_device:
            return {}
        return {"hwType": "RTLSDR", "centerFrequency": 118925000}

    def list_channels(self, idx):
        return [{"index": 0, "id": "AMDemod", "title": "KA", "deltaFrequency": -525000},
                {"index": 1, "id": "AMDemod", "title": "Tower", "deltaFrequency": -325000}]

    def _req(self, method, path, body=None, timeout=8.0):
        self.calls.append((method, path, body))
        # channel settings GET → return the offset so keepalive detection works
        if method == "GET" and "/channel/0/settings" in path:
            return 200, {"AMDemodSettings": {"inputFrequencyOffset": -525000, "volume": 0.4}}
        if method == "GET" and "/channel/1/settings" in path:
            return 200, {"AMDemodSettings": {"inputFrequencyOffset": -325000, "volume": 3.0}}
        return 200, {}

    def patch_device(self, idx, hw, skey, patch):
        self.calls.append(("PATCH", f"/deviceset/{idx}/device/settings", {skey: patch}))
        return self._healthy

    def patch_channel(self, idx, ch, ctype, skey, patch):
        self.calls.append(("PATCH", f"/deviceset/{idx}/channel/{ch}/settings", {skey: patch}))
        return self._healthy


def _state_with_profile(role="air"):
    rec = {"name": "air.airband.nashville", "role": role,
           "deviceset_index": 0 if role == "air" else 1, "center_freq_hz": 118925000}
    st = State(Path("/nonexistent"))
    st.read_loaded_profile = lambda r=None: (rec if r in (None, role) else None)
    st.read_loaded_profiles = lambda: {role: rec}
    return st


def _patch_keepalive(offset=-525000):
    # mock _keepalive_offset (state, role, center) → channel 0 (offset -525000)
    return mock.patch.object(routes, "_keepalive_offset", return_value=offset)


class TestApplyControls(unittest.TestCase):
    def test_gain_and_squelch_applied_keepalive_spared(self):
        c = _FakeClient()
        with mock.patch.object(routes, "_client", return_value=c), _patch_keepalive():
            out = routes.apply_controls(
                {"target": "airband", "gain": "29.7", "squelch_dbfs": "-55"},
                _state_with_profile())
        self.assertTrue(out["ok"])
        self.assertEqual(out["applied_gain"], 29.7)
        self.assertEqual(out["keepalive_spared"], 1)   # ch0 spared
        self.assertEqual(out["channels_touched"], 1)   # only ch1 got squelch
        # device gain PATCH present, tenths of dB
        dev = [b for m, p, b in c.calls if p.endswith("/device/settings")]
        self.assertEqual(dev[0]["rtlSdrSettings"]["gain"], 297)
        # ch1 squelch PATCH present; ch0 (keepalive) NOT squelched
        ch1 = [b for m, p, b in c.calls if "/channel/1/settings" in p and "AMDemodSettings" in (b or {})]
        self.assertEqual(ch1[0]["AMDemodSettings"]["squelch"], -55.0)
        ch0_sq = [b for m, p, b in c.calls
                  if "/channel/0/settings" in p and (b or {}).get("AMDemodSettings", {}).get("squelch") is not None]
        self.assertEqual(ch0_sq, [], "keepalive channel must NOT be squelched")

    def test_gain_out_of_range_is_400(self):
        with mock.patch.object(routes, "_client", return_value=_FakeClient()):
            with self.assertRaises(routes.WriteError) as ctx:
                routes.apply_controls({"target": "airband", "gain": "99", "squelch_dbfs": "-55"},
                                      _state_with_profile())
            self.assertEqual(ctx.exception.code, 400)

    def test_squelch_out_of_range_is_400(self):
        with mock.patch.object(routes, "_client", return_value=_FakeClient()):
            with self.assertRaises(routes.WriteError) as ctx:
                routes.apply_controls({"target": "airband", "gain": "20", "squelch_dbfs": "-500"},
                                      _state_with_profile())
            self.assertEqual(ctx.exception.code, 400)

    def test_missing_field_is_400(self):
        with mock.patch.object(routes, "_client", return_value=_FakeClient()):
            with self.assertRaises(routes.WriteError) as ctx:
                routes.apply_controls({"target": "airband", "gain": "20"}, _state_with_profile())
            self.assertEqual(ctx.exception.code, 400)

    def test_unknown_target_rejected(self):
        with self.assertRaises(routes.WriteError) as ctx:
            routes.apply_controls({"target": "banana", "gain": "20", "squelch_dbfs": "-55"},
                                  _state_with_profile())
        self.assertEqual(ctx.exception.code, 400)

    def test_sdrangel_down_is_503(self):
        with mock.patch.object(routes, "_client", return_value=_FakeClient(has_device=False)):
            with self.assertRaises(routes.WriteError) as ctx:
                routes.apply_controls({"target": "airband", "gain": "20", "squelch_dbfs": "-55"},
                                      _state_with_profile())
            self.assertEqual(ctx.exception.code, 503)

    def test_apply_batch_adds_cutoff(self):
        c = _FakeClient()
        with mock.patch.object(routes, "_client", return_value=c), _patch_keepalive():
            out = routes.apply_controls(
                {"target": "airband", "gain": "20", "squelch_dbfs": "-55", "cutoff_hz": "8000"},
                _state_with_profile(), with_filter=True)
        self.assertEqual(out["cutoff_hz"], 8000.0)
        ch1 = [b for m, p, b in c.calls if "/channel/1/settings" in p and "AMDemodSettings" in (b or {})]
        self.assertEqual(ch1[0]["AMDemodSettings"]["rfBandwidth"], 8000)


class TestTune(unittest.TestCase):
    def test_tune_sets_center_from_mhz(self):
        c = _FakeClient()
        with mock.patch.object(routes, "_client", return_value=c):
            out = routes.tune({"target": "airband", "freq": "119.350"}, _state_with_profile())
        self.assertEqual(out["center_hz"], 119350000)
        dev = [b for m, p, b in c.calls if p.endswith("/device/settings")]
        self.assertEqual(dev[0]["rtlSdrSettings"]["centerFrequency"], 119350000)

    def test_tune_out_of_airband_is_400(self):
        with mock.patch.object(routes, "_client", return_value=_FakeClient()):
            with self.assertRaises(routes.WriteError) as ctx:
                routes.tune({"target": "airband", "freq": "462.5"}, _state_with_profile())
            self.assertEqual(ctx.exception.code, 400)


class TestFilterAndVolume(unittest.TestCase):
    def test_filter_sets_bandwidth_all_channels(self):
        c = _FakeClient()
        with mock.patch.object(routes, "_client", return_value=c):
            out = routes.apply_filter({"target": "airband", "cutoff_hz": "10000"},
                                      _state_with_profile())
        self.assertEqual(out["channels_touched"], 2)

    def test_volume_set_spares_keepalive(self):
        c = _FakeClient()
        with mock.patch.object(routes, "_client", return_value=c), _patch_keepalive():
            out = routes.volume({"action": "set", "level": "40"}, _state_with_profile())
        self.assertEqual(out["volume"], 2.0)          # 40/100 * 5.0
        self.assertEqual(out["channels_touched"], 1)  # keepalive ch0 spared

    def test_volume_out_of_range_is_400(self):
        with mock.patch.object(routes, "_client", return_value=_FakeClient()):
            with self.assertRaises(routes.WriteError) as ctx:
                routes.volume({"action": "set", "level": "500"}, _state_with_profile())
            self.assertEqual(ctx.exception.code, 400)


class TestHits(unittest.TestCase):
    def test_hits_empty_valid_shape(self):
        out = routes.hits(_state_with_profile())
        self.assertTrue(out["ok"])
        self.assertEqual(out["items"], [])
        json.dumps(out)


class TestStatusChannels(unittest.TestCase):
    def test_status_includes_channel_list(self):
        st = _state_with_profile()
        with mock.patch.object(backends, "launchctl_loaded", return_value=[]), \
             mock.patch.object(backends, "icecast_mounts", return_value=[]), \
             mock.patch.object(backends, "sdrangel_devicesets",
                               return_value=[backends.DevicesetState(0, "RTLSDR", "95339533",
                                                                     "running", 118925000, [])]), \
             mock.patch.object(backends, "sdrangel_channels",
                               return_value=[{"index": 0, "id": "AMDemod", "title": "KA",
                                              "deltaFrequency": -525000}]), \
             mock.patch.object(backends, "mount_state",
                               side_effect=lambda m, **kw: backends.MountState(m, 200, True)):
            s = routes.build_status(st)
        self.assertEqual(len(s["channels"]), 1)
        self.assertEqual(s["channels"][0]["freq_hz"], 118400000)  # center + delta
        json.dumps(s)


# ---------------------------------------------------------------------------
# VFO role — single free-tuning NFM receiver on DS1, sharing the analog mount.
# ---------------------------------------------------------------------------

class _VfoFakeClient(_FakeClient):
    """RTL on DS1 with ONE NFMDemod channel (no keepalive — VFO is hunt mode)."""
    def sampling_device(self, idx):
        if not self._has_device:
            return {}
        return {"hwType": "RTLSDR", "centerFrequency": 146620000}

    def list_channels(self, idx):
        return [{"index": 0, "id": "NFMDemod", "title": "VFO", "deltaFrequency": -100000}]

    def _req(self, method, path, body=None, timeout=8.0):
        self.calls.append((method, path, body))
        if method == "GET" and "/channel/0/settings" in path:
            return 200, {"NFMDemodSettings": {"inputFrequencyOffset": -100000, "volume": 3.0}}
        return 200, {}


def _state_with_vfo():
    rec = {"name": "vfo.default", "role": "vfo",
           "deviceset_index": 1, "center_freq_hz": 146620000}
    st = State(Path("/nonexistent"))
    st.read_loaded_profile = lambda r=None: (rec if r in (None, "vfo") else None)
    st.read_loaded_profiles = lambda: {"vfo": rec}
    return st


class TestVfoWritePath(unittest.TestCase):
    def test_tune_vfo_moves_lo_and_channel_offset(self):
        # VFO tune keeps the DC-spike dodge: device LO = listen + 100 kHz, and the
        # channel offset = -100 kHz. So tuning 146.520 lands the LO at 146.620.
        c = _VfoFakeClient()
        with mock.patch.object(routes, "_client", return_value=c):
            out = routes.tune({"target": "vfo", "freq": "146.520"}, _state_with_vfo())
        self.assertEqual(out["listen_hz"], 146520000)
        self.assertEqual(out["center_hz"], 146620000)          # +100 kHz dodge
        dev = [b for m, p, b in c.calls if p.endswith("/device/settings")]
        self.assertEqual(dev[0]["rtlSdrSettings"]["centerFrequency"], 146620000)
        ch = [b for m, p, b in c.calls if "/channel/0/settings" in p and m == "PATCH"]
        self.assertEqual(ch[0]["NFMDemodSettings"]["inputFrequencyOffset"], -100000)

    def test_tune_vfo_out_of_range_is_400(self):
        with mock.patch.object(routes, "_client", return_value=_VfoFakeClient()):
            with self.assertRaises(routes.WriteError) as ctx:
                routes.tune({"target": "vfo", "freq": "10.0"}, _state_with_vfo())
            self.assertEqual(ctx.exception.code, 400)

    def test_apply_vfo_gain_and_squelch(self):
        # hunt mode → no keepalive → squelch applies to the single channel.
        c = _VfoFakeClient()
        with mock.patch.object(routes, "_client", return_value=c), \
             mock.patch.object(routes, "_keepalive_offset", return_value=None):
            out = routes.apply_controls(
                {"target": "vfo", "gain": "35.0", "squelch_dbfs": "-55"},
                _state_with_vfo())
        self.assertEqual(out["applied_gain"], 35.0)
        self.assertEqual(out["keepalive_spared"], 0)
        self.assertEqual(out["channels_touched"], 1)
        dev = [b for m, p, b in c.calls if p.endswith("/device/settings")]
        self.assertEqual(dev[0]["rtlSdrSettings"]["gain"], 350)   # tenths of dB

    def test_vfo_status_live_in_build_status(self):
        st = _state_with_vfo()
        with mock.patch.object(backends, "launchctl_loaded", return_value=[]), \
             mock.patch.object(backends, "icecast_mounts", return_value=[]), \
             mock.patch.object(backends, "sdrangel_devicesets",
                               return_value=[backends.DevicesetState(1, "RTLSDR", "95339533",
                                                                     "running", 146620000, [])]), \
             mock.patch.object(backends, "sdrangel_channels", return_value=[]), \
             mock.patch.object(backends, "mount_state",
                               side_effect=lambda m, **kw: backends.MountState(m, 200, True)):
            s = routes.build_status(st)
        self.assertEqual(s["vfo_status"], "live")
        self.assertTrue(s["vfo_device_online"])
        self.assertEqual(s["vfo_stream_mount"], routes.AIR_MOUNT)  # shares analog mount
        self.assertEqual(s["profile_vfo"], "vfo.default")
        json.dumps(s)


if __name__ == "__main__":
    unittest.main()


class TestKillGuard(unittest.TestCase):
    """do_POST must refuse writes with 409 while SB3 is killed (kill invariant)."""

    def test_post_refused_when_killed(self):
        import threading, urllib.request, urllib.error
        from sb3.ui import server
        srv = server.make_server(port=0)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with mock.patch.object(State, "is_killed", return_value=True):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/tune",
                    data=b"target=airband&freq=118.925",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    method="POST")
                try:
                    urllib.request.urlopen(req, timeout=5)
                    self.fail("expected 409")
                except urllib.error.HTTPError as e:
                    self.assertEqual(e.code, 409)
                    body = json.loads(e.read())
                    self.assertEqual(body["error"], "sb3-killed")
        finally:
            srv.shutdown(); srv.server_close()
