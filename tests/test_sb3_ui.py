"""Phase 3.1 tests — the sb3.ui shim: route payloads, server boot, ownership."""

from __future__ import annotations

import json
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from sb3 import backends, ownership
from sb3.state import State
from sb3.ui import routes, server


def _ds(**kw):
    base = dict(index=0, hw_type="RTLSDR", serial="95339533", state="running",
               center_hz=118925000, channels=[{}, {}, {}, {}])
    base.update(kw)
    return backends.DevicesetState(**base)


def _mount(name, code, present):
    return backends.MountState(name, code, present)


class TestStatusPayload(unittest.TestCase):
    def _state(self, profile=None):
        st = State(Path("/nonexistent"))
        st.read_loaded_profile = lambda: profile
        return st

    def _patch_backends(self, *, air, trunk, loaded, devicesets):
        return (
            mock.patch.object(backends, "launchctl_loaded", return_value=loaded),
            mock.patch.object(backends, "icecast_mounts",
                              return_value=[m for m in ("neptune-trunk.mp3", "neptune-air.mp3")]),
            mock.patch.object(backends, "sdrangel_devicesets", return_value=devicesets),
            mock.patch.object(backends, "mount_state",
                              side_effect=lambda m, **kw: air if m == "neptune-air.mp3" else trunk),
        )

    def test_status_healthy_air_loaded(self):
        prof = {"name": "air.airband.nashville", "role": "air", "deviceset_index": 0}
        pats = self._patch_backends(
            air=_mount("neptune-air.mp3", 200, True),
            trunk=_mount("neptune-trunk.mp3", 200, True),
            loaded=["com.scannerproject.sdrangel", "com.scannerproject.sdrtrunk",
                    "com.scannerproject.sb3-ui"],
            devicesets=[_ds()])
        with pats[0], pats[1], pats[2], pats[3]:
            s = routes.build_status(self._state(prof))
        self.assertTrue(s["ok"])
        self.assertTrue(s["airband_present"])
        self.assertTrue(s["airband_active"])          # device running, not phantom
        self.assertEqual(s["profile_airband"], "air.airband.nashville")
        self.assertTrue(s["icecast_active"])
        self.assertIn("com.scannerproject.sb3-ui", s["sb3"]["agents_up"])
        self.assertEqual(s["mounts"]["neptune-air.mp3"], 200)
        # payload must be JSON-serialisable
        json.dumps(s)

    def test_status_is_valid_json_with_no_backends(self):
        pats = self._patch_backends(
            air=_mount("neptune-air.mp3", None, False),
            trunk=_mount("neptune-trunk.mp3", None, False),
            loaded=[], devicesets=[])
        with pats[0], pats[1], pats[2], pats[3]:
            s = routes.build_status(self._state(None))
        self.assertFalse(s["airband_active"])
        self.assertEqual(s["profile_airband"], "")
        json.dumps(s)   # must not raise

    def test_phantom_deviceset_is_not_active(self):
        prof = {"name": "x", "role": "air", "deviceset_index": 0}
        pats = self._patch_backends(
            air=_mount("neptune-air.mp3", 404, False),
            trunk=_mount("neptune-trunk.mp3", 200, True),
            loaded=["com.scannerproject.sdrangel"],
            devicesets=[_ds(hw_type="AaroniaRTSA", serial=None, state="idle")])
        with pats[0], pats[1], pats[2], pats[3]:
            s = routes.build_status(self._state(prof))
        self.assertFalse(s["airband_active"])


class TestHeartbeatPayload(unittest.TestCase):
    VALID_STATES = {"quiet", "rf_degraded", "wedged", "error"}

    def _hb(self, *, air, trunk, profile, devicesets):
        st = State(Path("/nonexistent"))
        st.read_loaded_profile = lambda: profile
        with mock.patch.object(backends, "sdrangel_devicesets", return_value=devicesets), \
             mock.patch.object(backends, "mount_state",
                               side_effect=lambda m, **kw: air if m == "neptune-air.mp3" else trunk):
            return routes.build_heartbeat(st)

    def test_all_live_is_quiet(self):
        hb = self._hb(air=_mount("neptune-air.mp3", 200, True),
                      trunk=_mount("neptune-trunk.mp3", 200, True),
                      profile={"name": "x", "role": "air"}, devicesets=[_ds()])
        self.assertEqual(hb["state"], "quiet")
        self.assertIn(hb["state"], self.VALID_STATES)

    def test_digital_down_is_wedged(self):
        hb = self._hb(air=_mount("neptune-air.mp3", 200, True),
                      trunk=_mount("neptune-trunk.mp3", 404, False),
                      profile=None, devicesets=[_ds()])
        self.assertEqual(hb["state"], "wedged")

    def test_air_profile_loaded_but_mount_dark_is_degraded(self):
        hb = self._hb(air=_mount("neptune-air.mp3", 404, False),
                      trunk=_mount("neptune-trunk.mp3", 200, True),
                      profile={"name": "air.x", "role": "air"}, devicesets=[_ds()])
        self.assertEqual(hb["state"], "rf_degraded")

    def test_heartbeat_shape_always_complete(self):
        hb = self._hb(air=_mount("neptune-air.mp3", 200, True),
                      trunk=_mount("neptune-trunk.mp3", 200, True),
                      profile=None, devicesets=[_ds()])
        for key in ("state", "headline", "explanation", "recovery", "evidence", "since"):
            self.assertIn(key, hb)
        self.assertIsInstance(hb["evidence"], list)
        json.dumps(hb)


class TestOwnership(unittest.TestCase):
    def test_sb3_ui_is_owned_and_managed(self):
        self.assertIn("com.scannerproject.sb3-ui", ownership.SB3_LAYER)
        self.assertIn("com.scannerproject.sb3-ui", ownership.KILL_ORDER)
        self.assertIn("com.scannerproject.sb3-ui", ownership.MANAGED_AGENTS)
        self.assertNotIn("com.scannerproject.sb3-ui", ownership.BACKEND)

    def test_sb3_ui_stops_before_brokers(self):
        order = list(ownership.KILL_ORDER)
        self.assertLess(order.index("com.scannerproject.sb3-ui"),
                        order.index("com.scannerproject.tuner-broker"))
        self.assertLess(order.index("com.scannerproject.sb3-ui"),
                        order.index("com.scannerproject.sb3-broker"))

    def test_kill_dry_run_lists_sb3_ui(self):
        from sb3 import killswitch
        lines = []
        with mock.patch.object(backends, "launchctl_loaded",
                               return_value=["com.scannerproject.sb3-ui"]), \
             mock.patch.object(backends, "mount_state",
                               side_effect=lambda m, **kw: _mount(m, 200, True)):
            killswitch.cmd_kill(execute=False, emit=lines.append,
                                state=State(Path("/nonexistent")), uid=501)
        out = "\n".join(lines)
        self.assertIn("com.scannerproject.sb3-ui", out)

    def test_ui_plist_template_exists(self):
        from sb3 import install
        labels = {p.label for p in install.plan()}
        self.assertIn("com.scannerproject.sb3-ui", labels)
        p = next(p for p in install.plan() if p.label == "com.scannerproject.sb3-ui")
        self.assertTrue(p.template_exists, f"missing plist for sb3-ui at {p.template}")


class TestServerBoots(unittest.TestCase):
    def test_server_boots_and_serves(self):
        srv = server.make_server(port=0)   # ephemeral port
        port = srv.server_address[1]
        import threading
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with mock.patch.object(backends, "launchctl_loaded", return_value=[]), \
                 mock.patch.object(backends, "icecast_mounts", return_value=[]), \
                 mock.patch.object(backends, "sdrangel_devicesets", return_value=[]), \
                 mock.patch.object(backends, "mount_state",
                                   side_effect=lambda m, **kw: _mount(m, None, False)):
                r = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=5)
                data = json.loads(r.read())
                self.assertTrue(data["ok"])
                r2 = urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5)
                self.assertTrue(json.loads(r2.read())["ok"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_serves_sb3_html_at_root(self):
        # the served file is the real repo ui/sb3.html
        self.assertTrue(server.SB3_HTML.name == "sb3.html")
        self.assertTrue(server.SB3_HTML.exists(), f"{server.SB3_HTML} must exist")


if __name__ == "__main__":
    unittest.main()
