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
        profs = {profile["role"]: profile} if profile else {}
        st.read_loaded_profile = lambda role=None: (profs.get(role) if role else profs.get("air"))
        st.read_loaded_profiles = lambda: profs
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

    def test_neptune_ground_bridge_is_classified_backend(self):
        # Regression: the Ground bridge shipped as a plist + GUARDED_MOUNTS entry
        # but was missing from BACKEND, so classify() fail-closed and blocked
        # every kill/resume once RunAtLoad loaded it on Neptune (2026-07-21).
        self.assertEqual(ownership.classify("com.scannerproject.neptune-ground-bridge"),
                         "backend")

    def test_every_shipped_plist_label_is_classifiable(self):
        # Any launchd label we ship a plist for MUST be in SB3_LAYER or BACKEND,
        # or classify() hard-fails at runtime and jams kill/resume/update. This
        # guards the whole class of "added a plist, forgot to classify it" bugs.
        import re
        root = Path(__file__).resolve().parent.parent / "macos" / "launchd"
        plists = list(root.glob("*.plist")) + list(root.glob("sb3/*.plist"))
        self.assertTrue(plists, "expected shipped launchd plists")
        for plist in plists:
            text = plist.read_text()
            m = re.search(r"<key>Label</key>\s*<string>([^<]+)</string>", text)
            self.assertIsNotNone(m, f"{plist.name} has no Label")
            label = m.group(1).strip()
            try:
                ownership.classify(label)   # must not raise
            except ownership.UnclassifiedLabel:
                self.fail(f"{plist.name} label {label!r} is not in SB3_LAYER or BACKEND")


class TestGroundStatusField(unittest.TestCase):
    """build_status.ground_status drives the Ground offline banner (Phase 3.4/3.5)."""

    def _status(self, *, ground_profile, ground_mount, devicesets):
        st = State(Path("/nonexistent"))
        profs = {}
        if ground_profile:
            profs["ground"] = ground_profile
        st.read_loaded_profiles = lambda: profs
        st.read_loaded_profile = lambda role=None: profs.get(role) if role else profs.get("air")
        mounts = {
            "neptune-air.mp3": _mount("neptune-air.mp3", None, False),
            "neptune-trunk.mp3": _mount("neptune-trunk.mp3", 200, True),
            "neptune-ground.mp3": ground_mount,
        }
        with mock.patch.object(backends, "launchctl_loaded", return_value=[]), \
             mock.patch.object(backends, "icecast_mounts", return_value=[]), \
             mock.patch.object(backends, "sdrangel_devicesets", return_value=devicesets), \
             mock.patch.object(backends, "mount_state", side_effect=lambda m, **kw: mounts[m]):
            return routes.build_status(st)

    def test_no_ground_profile_reports_not_loaded(self):
        s = self._status(ground_profile=None,
                         ground_mount=_mount("neptune-ground.mp3", 404, False),
                         devicesets=[_ds()])
        self.assertEqual(s["ground_status"], "not loaded")
        self.assertFalse(s["ground_device_online"])

    def test_ground_loaded_but_device_offline(self):
        prof = {"name": "ground.x", "role": "ground", "deviceset_index": 1}
        s = self._status(ground_profile=prof,
                         ground_mount=_mount("neptune-ground.mp3", 404, False),
                         devicesets=[_ds()])   # only DS0 present; no DS1 → not running
        self.assertEqual(s["ground_status"], "loaded — device offline")
        self.assertFalse(s["ground_device_online"])

    def test_ground_live(self):
        prof = {"name": "ground.x", "role": "ground", "deviceset_index": 1}
        s = self._status(ground_profile=prof,
                         ground_mount=_mount("neptune-ground.mp3", 200, True),
                         devicesets=[_ds(), _ds(index=1, serial="61108285")])
        self.assertEqual(s["ground_status"], "live")
        self.assertTrue(s["ground_device_online"])


class TestSystemAndStubs(unittest.TestCase):
    def test_build_system_shape(self):
        st = State(Path("/nonexistent"))
        with mock.patch.object(backends, "launchctl_loaded",
                               return_value=["com.scannerproject.sb3-ui",
                                             "com.scannerproject.sdrangel"]):
            sysd = routes.build_system(st)
        self.assertTrue(sysd["ok"])
        for k in ("host", "platform", "python", "load_avg", "deploy", "agents"):
            self.assertIn(k, sysd)
        self.assertIn("com.scannerproject.sb3-ui", sysd["agents"]["sb3_up"])
        self.assertIn("com.scannerproject.sdrangel", sysd["agents"]["backend_up"])
        json.dumps(sysd)   # serialisable

    def test_hp_state_is_sane_off_default(self):
        d = routes.hp_state(State(Path("/nonexistent")))
        self.assertTrue(d["ok"])
        self.assertFalse(d["travel_mode_enabled"])
        self.assertEqual(d["state"]["zip"], "")
        self.assertIsNone(d["travel_mode_last_push"])

    def test_ask_claude_stub_is_graceful_200(self):
        d = routes.ask_claude_stub({"session_id": "sid-1"}, State(Path("/nonexistent")))
        self.assertFalse(d["ok"])
        self.assertIn("wired", d["error"])
        self.assertEqual(d["session_id"], "sid-1")


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


class TestResumeToleratesMissingPlist(unittest.TestCase):
    """resume must bring back installed agents even if one plist is missing.

    Regression guard for the 2026-07-20 deploy: adding sb3-ui to MANAGED_AGENTS
    then resuming before `install --execute` aborted resume mid-loop and left
    the controller down.
    """

    def test_missing_plist_is_skipped_not_fatal(self):
        from pathlib import Path
        from sb3 import killswitch, settle
        lines = []
        # broker plist "exists", ui plist "missing"
        def fake_target(label):
            return Path("/exists.plist") if "broker" in label else Path("/nope.plist")
        with mock.patch("sb3.killswitch.install_mod_target", side_effect=fake_target), \
             mock.patch("sb3.killswitch.install_mod_managed",
                        return_value={"com.scannerproject.sb3-broker": "x",
                                      "com.scannerproject.sb3-ui": "y"}), \
             mock.patch.object(Path, "exists", lambda self: "exists" in str(self)), \
             mock.patch.object(settle, "is_loaded", return_value=False), \
             mock.patch.object(settle, "bootstrap", return_value=True) as boot, \
             mock.patch.object(backends, "sdrangel_devicesets", return_value=[]), \
             mock.patch.object(backends, "mount_state",
                               side_effect=lambda m, **kw: _mount(m, 200, True)), \
             mock.patch.object(State, "read_loaded_profile", return_value=None), \
             mock.patch.object(State, "clear"), mock.patch.object(State, "is_killed", return_value=True):
            rc = killswitch.cmd_resume_execute(emit=lines.append,
                                               state=State(Path("/x")), uid=501)
        out = "\n".join(lines)
        # broker (installed) got bootstrapped; ui (missing plist) skipped, not fatal
        self.assertTrue(boot.called, "installed agent must still be brought up")
        self.assertIn("skipped", out)
        self.assertEqual(rc, killswitch.EXIT_OK)
