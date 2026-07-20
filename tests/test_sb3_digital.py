"""Phase 3.3 tests — SDRTrunk log-tail observer + digital status fields."""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from unittest import mock

from sb3 import sdrtrunk_client, backends
from sb3.state import State
from sb3.ui import routes

_NOW = time.strftime("%Y%m%d %H%M%S", time.localtime())
_LOG = f"""\
20260719 164615.030 [main] INFO  i.g.d.s.t.manager.TunerManager - Discovered [1] RSP devices
20260719 164615.030 [main] INFO  i.g.d.s.t.manager.TunerManager - Tuner: RSPduo Tuner 1 SER#180903EF32 - Added / Starting ...
20260719 164615.204 [main] INFO  i.g.d.s.t.manager.TunerManager - Tuner: RSPduo Tuner 2 SER#180903EF32 - Added / Starting ...
{_NOW}.100 [NioProcessor-2] INFO  i.g.d.a.b.AudioStreamingBroadcaster - [neptune-digital] status: Connected
{_NOW}.200 [decode] INFO  i.g.d.module - GRP_VCH_GRNT_UPD talkgroup 9115
{_NOW}.300 [decode] WARN  i.g.d.module - sync loss transient
"""


def _write_log(tmp: Path) -> Path:
    p = tmp / "sdrtrunk_app.log"
    p.write_text(_LOG)
    return p


class TestObserver(unittest.TestCase):
    def test_connected_and_active_when_running(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = _write_log(Path(td))
            d = sdrtrunk_client.observe(p, running=True)
        self.assertTrue(d["digital_active"])
        self.assertEqual(d["digital_broadcaster_status"], "Connected")
        self.assertEqual(d["digital_profile"], "neptune-digital")
        self.assertIn("RSPduo Tuner 1 SER#180903EF32", d["digital_tuner_targets"])
        self.assertEqual(len(d["digital_tuner_targets"]), 2)
        self.assertTrue(d["digital_control_channel_locked"])   # recent activity
        self.assertEqual(d["digital_last_warning"], "sync loss transient")
        json.dumps(d)

    def test_not_active_when_process_down(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = _write_log(Path(td))
            d = sdrtrunk_client.observe(p, running=False)
        self.assertFalse(d["digital_active"])
        self.assertFalse(d["digital_control_channel_locked"])

    def test_missing_log_is_graceful(self):
        d = sdrtrunk_client.observe(Path("/nonexistent/sdrtrunk.log"), running=True)
        self.assertFalse(d["digital_active"])       # connected never seen
        self.assertEqual(d["digital_tuner_targets"], [])
        json.dumps(d)

    def test_stale_activity_is_not_locked(self):
        # old broadcaster line, no recent activity → not locked
        import tempfile
        old = "20200101 000000.000 [x] INFO  i.g.d.a.b.AudioStreamingBroadcaster - [neptune-digital] status: Connected\n"
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "s.log"; p.write_text(old)
            d = sdrtrunk_client.observe(p, running=True)
        # connected but activity is ancient → lock reflects staleness
        self.assertTrue(d["digital_active"])   # broadcaster still connected
        self.assertFalse(d["digital_control_channel_locked"])


class TestStatusDigitalFields(unittest.TestCase):
    def test_status_includes_digital_block(self):
        st = State(Path("/nonexistent"))
        st.read_loaded_profile = lambda: None
        with mock.patch.object(backends, "launchctl_loaded",
                               return_value=["com.scannerproject.sdrtrunk"]), \
             mock.patch.object(backends, "icecast_mounts", return_value=["neptune-trunk.mp3"]), \
             mock.patch.object(backends, "sdrangel_devicesets", return_value=[]), \
             mock.patch.object(backends, "sdrangel_channels", return_value=[]), \
             mock.patch.object(backends, "mount_state",
                               side_effect=lambda m, **kw: backends.MountState(m, 200, True)), \
             mock.patch.object(sdrtrunk_client, "observe",
                               return_value={"digital_active": True, "digital_backend": "sdrtrunk",
                                             "digital_tuner_targets": ["RSPduo Tuner 1 SER#180903EF32"]}):
            s = routes.build_status(st)
        self.assertTrue(s["digital_active"])
        self.assertEqual(s["digital_backend"], "sdrtrunk")
        self.assertIn("RSPduo Tuner 1 SER#180903EF32", s["digital_tuner_targets"])
        json.dumps(s)


if __name__ == "__main__":
    unittest.main()
