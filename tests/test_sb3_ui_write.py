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


def _state_with_profile():
    st = State(Path("/nonexistent"))
    st.read_loaded_profile = lambda: {"name": "air.airband.nashville",
                                      "deviceset_index": 0, "center_freq_hz": 118925000}
    return st


def _patch_keepalive(offset=-525000):
    # mock _keepalive_offset to report channel 0 (offset -525000) as keepalive
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

    def test_non_airband_target_rejected(self):
        with self.assertRaises(routes.WriteError) as ctx:
            routes.apply_controls({"target": "ground", "gain": "20", "squelch_dbfs": "-55"},
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


if __name__ == "__main__":
    unittest.main()
