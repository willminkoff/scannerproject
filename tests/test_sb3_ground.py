"""Phase 3.3 Ground — profile, multi-profile state, 2-DS coexistence."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sb3.gitdeploy import deploy_root
from sb3.profile import load_profile
from sb3.state import State

GROUND = deploy_root() / "profiles" / "ground.nashville.noaa-wx.json"
AIR = deploy_root() / "profiles" / "air-airband-nashville.json"


class TestGroundProfile(unittest.TestCase):
    def test_parses_and_fits(self):
        p = load_profile(GROUND)
        self.assertEqual(p.role, "ground")
        self.assertEqual(p.sub_role, "noaa-wx")
        self.assertEqual(p.deviceset_index, 1)
        self.assertEqual(p.serial, "61108285")
        self.assertEqual(p.udp_port, 9999)          # distinct from Air's 9998
        self.assertEqual(p.mount, "neptune-ground.mp3")
        self.assertEqual(p.audio_strategy, "index:0")
        self.assertEqual(p.channels[0].demod, "NFM")
        for c in p.channels:                        # camp-mode fit
            self.assertLessEqual(abs(c.offset_from(p.center_freq_hz)) + c.rf_bw_hz / 2,
                                 p.half_window_hz)

    def test_keepalive_is_out_of_band_and_first(self):
        p = load_profile(GROUND)
        self.assertEqual(len(p.keepalive_channels), 1)
        ka = p.keepalive_channels[0]
        self.assertEqual(ka.freq_hz, 162375000)     # guard region, no real tx
        self.assertTrue(p.channels_apply_order()[0].keepalive)

    def test_air_and_ground_use_different_ports_and_devices(self):
        air, gnd = load_profile(AIR), load_profile(GROUND)
        self.assertNotEqual(air.udp_port, gnd.udp_port)
        self.assertNotEqual(air.deviceset_index, gnd.deviceset_index)
        self.assertNotEqual(air.serial, gnd.serial)
        self.assertNotEqual(air.mount, gnd.mount)


class TestMultiProfileState(unittest.TestCase):
    def test_air_and_ground_coexist(self):
        with tempfile.TemporaryDirectory() as td:
            st = State(Path(td))
            st.write_loaded_profile({"name": "air.x", "role": "air", "deviceset_index": 0})
            st.write_loaded_profile({"name": "gnd.x", "role": "ground", "deviceset_index": 1})
            self.assertEqual(st.read_loaded_profile("air")["name"], "air.x")
            self.assertEqual(st.read_loaded_profile("ground")["name"], "gnd.x")
            self.assertEqual(st.read_loaded_profile()["name"], "air.x")   # default = air
            self.assertEqual(set(st.read_loaded_profiles()), {"air", "ground"})

    def test_clear_one_role_keeps_the_other(self):
        with tempfile.TemporaryDirectory() as td:
            st = State(Path(td))
            st.write_loaded_profile({"name": "air.x", "role": "air"})
            st.write_loaded_profile({"name": "gnd.x", "role": "ground"})
            st.clear_loaded_profile("ground")
            self.assertIsNone(st.read_loaded_profile("ground"))
            self.assertIsNotNone(st.read_loaded_profile("air"))

    def test_migration_from_flat_record(self):
        # a Phase-2 flat record on disk reads as {'air': record}
        with tempfile.TemporaryDirectory() as td:
            st = State(Path(td))
            st.state_dir.mkdir(parents=True, exist_ok=True)
            st.loaded_profile_path.write_text(json.dumps(
                {"name": "air.old", "role": "air", "deviceset_index": 0}))
            self.assertEqual(st.read_loaded_profile("air")["name"], "air.old")
            self.assertEqual(st.read_loaded_profiles()["air"]["name"], "air.old")


class TestCopyToUdpSamePortOnly(unittest.TestCase):
    def test_only_same_port_senders_disabled(self):
        # Air on idx-1 → 9998 must survive when Ground arms idx0 → 9999.
        from sb3.sdrangel import SDRangelClient
        calls = []

        class C(SDRangelClient):
            def __init__(self):
                super().__init__(execute=True, emit=lambda m: None, sleep=lambda s: None)
            def wait_rest_healthy(self, timeout=30.0):
                return True
            def audio_outputs(self):
                return [{"index": -1, "copyToUDP": 1, "udpPort": 9998},   # Air
                        {"index": 0, "copyToUDP": 0, "udpPort": 9998}]
            def _req(self, method, path, body=None, timeout=8.0):
                calls.append((method, path, body))
                return 200, {}

        c = C()
        c.set_copy_to_udp(address="127.0.0.1", port=9999, audio_index=0)
        # No PATCH should have disabled idx-1 (it is on 9998, a DIFFERENT port)
        disabled = [b for m, p, b in calls
                    if p == "/audio/output/parameters" and b.get("copyToUDP") == 0
                    and b.get("index") == -1]
        self.assertEqual(disabled, [], "Air's idx-1 (:9998) must not be disabled by a :9999 arm")


if __name__ == "__main__":
    unittest.main()
