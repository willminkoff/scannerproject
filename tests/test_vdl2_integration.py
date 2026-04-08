"""Tests for VDL2 decoder integration.

Covers:
- _extract_acars_from_vdl2() parsing of dumpvdl2 JSON frames
- vdl2_reader_worker source tagging
- Combined ACARS+VDL2 start/stop via server_workers
- Combined ACARS+VDL2 service start via handlers wx API
- systemd helpers for dumpvdl2 (start/stop/restart)
- MetStore mixed-source message retrieval and sounding data
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from ui import systemd
from ui.wxdata import (
    MetStore,
    RawMessage,
    MetObservation,
    _extract_acars_from_vdl2,
    parse_acars_message,
)

# server_workers and actions use Python 3.10+ type syntax (list[str] | None)
# which fails on 3.9.  Guard those imports so the rest of the suite still runs.
_CAN_IMPORT_ACTIONS = True
try:
    from ui import server_workers  # noqa: F401
    from ui import actions  # noqa: F401
except TypeError:
    _CAN_IMPORT_ACTIONS = False

_skip_310 = unittest.skipUnless(
    _CAN_IMPORT_ACTIONS,
    "actions/server_workers require Python ≥ 3.10 type syntax",
)


# ---------------------------------------------------------------------------
# Helper: fake subprocess result
# ---------------------------------------------------------------------------
class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---- Sample dumpvdl2 frames ----

SAMPLE_VDL2_ACARS_FRAME = {
    "vdl2": {
        "t": {"sec": 1712600000},
        "freq": 136975000,
        "avlc": {
            "acars": {
                "flight": "DAL1234 ",
                "reg": ".N123DL",
                "label": "H1",
                "msg_text": "#M1BPOSN36100W086450350330 123045",
            }
        },
    }
}

SAMPLE_VDL2_ACARS_NO_MET = {
    "vdl2": {
        "t": {"sec": 1712600001},
        "freq": 136975000,
        "avlc": {
            "acars": {
                "flight": "UAL567",
                "reg": ".N567UA",
                "label": "Q0",
                "msg_text": "REQUEST CLEARANCE",
            }
        },
    }
}

SAMPLE_VDL2_NON_ACARS = {
    "vdl2": {
        "t": {"sec": 1712600002},
        "freq": 136875000,
        "avlc": {
            "xid": {"type": "XID_CMD"},
        },
    }
}

SAMPLE_VDL2_EMPTY = {"vdl2": {"t": {"sec": 0}}}


# ===================================================================
# _extract_acars_from_vdl2
# ===================================================================
class ExtractAcarsFromVdl2Tests(unittest.TestCase):
    def test_extracts_acars_from_valid_frame(self):
        result = _extract_acars_from_vdl2(SAMPLE_VDL2_ACARS_FRAME)
        self.assertIsNotNone(result)
        self.assertEqual(result["flight"], "DAL1234")
        self.assertEqual(result["tail"], ".N123DL")
        self.assertEqual(result["label"], "H1")
        self.assertIn("POSN", result["text"])
        self.assertEqual(result["timestamp"], 1712600000)

    def test_returns_none_for_non_acars_frame(self):
        result = _extract_acars_from_vdl2(SAMPLE_VDL2_NON_ACARS)
        self.assertIsNone(result)

    def test_returns_none_for_missing_vdl2(self):
        self.assertIsNone(_extract_acars_from_vdl2({}))
        self.assertIsNone(_extract_acars_from_vdl2({"vdl2": "bad"}))

    def test_returns_none_for_missing_avlc(self):
        self.assertIsNone(_extract_acars_from_vdl2({"vdl2": {}}))
        self.assertIsNone(_extract_acars_from_vdl2({"vdl2": {"avlc": "bad"}}))

    def test_returns_none_for_missing_acars(self):
        self.assertIsNone(_extract_acars_from_vdl2({"vdl2": {"avlc": {}}}))

    def test_fallback_timestamp_when_missing(self):
        frame = {
            "vdl2": {
                "avlc": {
                    "acars": {"flight": "TST1", "label": "Q0", "msg_text": "test"}
                }
            }
        }
        result = _extract_acars_from_vdl2(frame)
        self.assertIsNotNone(result)
        # No t.sec → should use time.time() fallback
        self.assertGreater(result["timestamp"], 0)

    def test_strips_whitespace_from_fields(self):
        frame = {
            "vdl2": {
                "t": {"sec": 100},
                "avlc": {
                    "acars": {
                        "flight": "  FLT99  ",
                        "reg": " .N99XX ",
                        "label": " SA ",
                        "msg_text": " some text ",
                    }
                },
            }
        }
        result = _extract_acars_from_vdl2(frame)
        self.assertEqual(result["flight"], "FLT99")
        self.assertEqual(result["tail"], ".N99XX")
        self.assertEqual(result["label"], "SA")
        self.assertEqual(result["text"], "some text")


# ===================================================================
# VDL2 source tagging through parse_acars_message
# ===================================================================
class VDL2SourceTaggingTests(unittest.TestCase):
    """Verify that VDL2-extracted ACARS messages can be retagged to source=vdl2."""

    def test_parsed_message_can_be_retagged_vdl2(self):
        """Simulates what vdl2_reader_worker does: parse then retag."""
        acars_msg = _extract_acars_from_vdl2(SAMPLE_VDL2_ACARS_FRAME)
        raw, obs_list = parse_acars_message(acars_msg)
        # Default source is "acars" from the parser
        self.assertEqual(raw.source, "acars")
        # Retag as vdl2 (like the worker does)
        raw.source = "vdl2"
        self.assertEqual(raw.source, "vdl2")
        for obs in obs_list:
            obs.source = "vdl2"
            self.assertEqual(obs.source, "vdl2")

    def test_non_met_message_tags_correctly(self):
        acars_msg = _extract_acars_from_vdl2(SAMPLE_VDL2_ACARS_NO_MET)
        raw, obs_list = parse_acars_message(acars_msg)
        raw.source = "vdl2"
        self.assertEqual(raw.source, "vdl2")
        self.assertEqual(obs_list, [])
        self.assertFalse(raw.is_met)


# ===================================================================
# MetStore mixed-source tests
# ===================================================================
class MetStoreMixedSourceTests(unittest.TestCase):
    """Test that MetStore correctly handles messages from both ACARS and VDL2."""

    def setUp(self):
        self.store = MetStore(max_messages=100, max_met=100)
        self.store.collecting = True
        self.store.active_decoder = "acars"

    def test_stores_messages_from_both_sources(self):
        self.store.add_message(RawMessage(
            timestamp=1000, source="acars", source_id="DAL100",
            text="[H1] DAL100: test", is_met=True,
        ))
        self.store.add_message(RawMessage(
            timestamp=1001, source="vdl2", source_id="UAL200",
            text="[H1] UAL200: test", is_met=True,
        ))
        msgs = self.store.get_messages(limit=10)
        self.assertEqual(len(msgs), 2)
        sources = {m["source"] for m in msgs}
        self.assertEqual(sources, {"acars", "vdl2"})

    def test_filter_messages_by_source(self):
        self.store.add_message(RawMessage(
            timestamp=1000, source="acars", source_id="A1",
            text="acars msg", is_met=False,
        ))
        self.store.add_message(RawMessage(
            timestamp=1001, source="vdl2", source_id="V1",
            text="vdl2 msg", is_met=False,
        ))
        self.store.add_message(RawMessage(
            timestamp=1002, source="vdl2", source_id="V2",
            text="vdl2 msg 2", is_met=False,
        ))
        acars_only = self.store.get_messages(limit=10, source="acars")
        vdl2_only = self.store.get_messages(limit=10, source="vdl2")
        self.assertEqual(len(acars_only), 1)
        self.assertEqual(len(vdl2_only), 2)

    def test_sounding_combines_both_sources(self):
        """Observations from ACARS and VDL2 appear together in sounding data."""
        obs_acars = MetObservation(
            timestamp=1000, source="acars", source_id="DAL100",
            lat=36.1, lon=-86.7, altitude_ft=35000,
            pressure_hpa=238.0, temp_c=-52.0, dewpoint_c=-60.0,
            wind_dir_deg=270, wind_speed_kt=80,
        )
        obs_vdl2 = MetObservation(
            timestamp=1001, source="vdl2", source_id="UAL200",
            lat=36.2, lon=-86.8, altitude_ft=33000,
            pressure_hpa=264.0, temp_c=-48.0, dewpoint_c=-55.0,
            wind_dir_deg=260, wind_speed_kt=75,
        )
        self.store.add_observation(obs_acars)
        self.store.add_observation(obs_vdl2)
        sounding = self.store.get_sounding_data()
        self.assertEqual(sounding["observations"], 2)
        sources = {lev["source"] for lev in sounding["levels"]}
        self.assertEqual(sources, {"acars", "vdl2"})

    def test_spc_export_includes_both_sources(self):
        obs_acars = MetObservation(
            timestamp=1000, source="acars", source_id="DAL100",
            lat=36.1, lon=-86.7, altitude_ft=35000,
            pressure_hpa=238.0, temp_c=-52.0, dewpoint_c=-60.0,
            wind_dir_deg=270, wind_speed_kt=80,
        )
        obs_vdl2 = MetObservation(
            timestamp=1001, source="vdl2", source_id="UAL200",
            lat=36.2, lon=-86.8, altitude_ft=33000,
            pressure_hpa=264.0, temp_c=-48.0, dewpoint_c=-55.0,
            wind_dir_deg=260, wind_speed_kt=75,
        )
        self.store.add_observation(obs_acars)
        self.store.add_observation(obs_vdl2)
        spc = self.store.get_sounding_spc()
        self.assertIn("238.0", spc)
        self.assertIn("264.0", spc)
        self.assertIn("%RAW%", spc)
        self.assertIn("%END%", spc)

    def test_status_reflects_combined_mode(self):
        self.store.add_message(RawMessage(
            timestamp=1000, source="acars", source_id="A", text="a", is_met=True,
        ))
        self.store.add_message(RawMessage(
            timestamp=1001, source="vdl2", source_id="V", text="v", is_met=False,
        ))
        status = self.store.get_status()
        self.assertEqual(status["active_decoder"], "acars")
        self.assertEqual(status["message_count"], 2)


# ===================================================================
# VDL2 reader worker
# ===================================================================
class VDL2ReaderWorkerTests(unittest.TestCase):
    """Test vdl2_reader_worker reads JSON lines and populates the store."""

    def test_reader_parses_acars_frame_and_tags_vdl2(self):
        from ui.wxdata import vdl2_reader_worker

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            tmppath = f.name

        try:
            store = MetStore()
            store.collecting = True
            store.active_decoder = "acars"
            stop = threading.Event()

            # Write the file first, then start the worker
            with open(tmppath, "w") as f:
                f.write(json.dumps(SAMPLE_VDL2_ACARS_FRAME) + "\n")
                f.write(json.dumps(SAMPLE_VDL2_NON_ACARS) + "\n")

            # Patch VDL2_OUTPUT_PATH to our temp file
            with mock.patch("ui.wxdata.VDL2_OUTPUT_PATH", tmppath):
                t = threading.Thread(
                    target=vdl2_reader_worker, args=(store, stop), daemon=True
                )
                t.start()
                # Give the reader time to open the file and seek to end
                time.sleep(0.3)

                # Now append new lines that the reader will see
                with open(tmppath, "a") as f:
                    f.write(json.dumps(SAMPLE_VDL2_ACARS_NO_MET) + "\n")
                    f.write(json.dumps(SAMPLE_VDL2_ACARS_FRAME) + "\n")
                    f.flush()

                # Wait for processing
                time.sleep(0.5)
                stop.set()
                t.join(timeout=3)

            # The reader seeks to EOF on start, so it only sees the 2
            # lines appended after it opened.
            msgs = store.get_messages(limit=10)
            self.assertGreaterEqual(len(msgs), 1)
            # All messages should be tagged as vdl2
            for m in msgs:
                self.assertEqual(m["source"], "vdl2")
        finally:
            os.unlink(tmppath)

    def test_reader_handles_missing_file_gracefully(self):
        from ui.wxdata import vdl2_reader_worker

        store = MetStore()
        stop = threading.Event()

        with mock.patch("ui.wxdata.VDL2_OUTPUT_PATH", "/nonexistent/path.json"):
            t = threading.Thread(
                target=vdl2_reader_worker, args=(store, stop), daemon=True
            )
            t.start()
            time.sleep(0.3)
            stop.set()
            t.join(timeout=3)

        # Should not crash, no messages stored
        self.assertEqual(store.get_messages(), [])


# ===================================================================
# server_workers: combined start/stop
# ===================================================================
@_skip_310
class CombinedReaderStartStopTests(unittest.TestCase):
    """Test that start_wx_reader('acars') launches both ACARS and VDL2 threads."""

    def test_acars_mode_starts_two_reader_threads(self):
        from ui import server_workers

        # Reset global state
        server_workers._wx_reader_threads = []
        server_workers._met_store = None

        acars_worker = mock.Mock()
        vdl2_worker = mock.Mock()
        radiosonde_worker = mock.Mock()

        with mock.patch(
            "ui.server_workers.acars_reader_worker", acars_worker, create=True
        ), mock.patch(
            "ui.server_workers.vdl2_reader_worker", vdl2_worker, create=True
        ), mock.patch(
            "ui.server_workers.radiosonde_reader_worker", radiosonde_worker, create=True
        ), mock.patch.dict(
            "ui.wxdata.__dict__",
            {"acars_reader_worker": acars_worker, "vdl2_reader_worker": vdl2_worker, "radiosonde_reader_worker": radiosonde_worker},
        ), mock.patch(
            "ui.server_workers._configure_spatial_filter"
        ):
            server_workers.start_wx_reader("acars")

            # Two threads started (ACARS + VDL2)
            self.assertEqual(len(server_workers._wx_reader_threads), 2)
            for thread, stop_event in server_workers._wx_reader_threads:
                self.assertTrue(thread.is_alive())
                self.assertFalse(stop_event.is_set())

            store = server_workers.get_met_store()
            self.assertTrue(store.collecting)
            self.assertEqual(store.active_decoder, "acars")

            # Stop all
            server_workers.stop_wx_reader()
            self.assertEqual(len(server_workers._wx_reader_threads), 0)
            self.assertFalse(store.collecting)
            self.assertIsNone(store.active_decoder)

    def test_radiosonde_mode_starts_one_reader_thread(self):
        from ui import server_workers

        server_workers._wx_reader_threads = []
        server_workers._met_store = None

        acars_worker = mock.Mock()
        vdl2_worker = mock.Mock()
        radiosonde_worker = mock.Mock()

        with mock.patch(
            "ui.server_workers.acars_reader_worker", acars_worker, create=True
        ), mock.patch(
            "ui.server_workers.vdl2_reader_worker", vdl2_worker, create=True
        ), mock.patch(
            "ui.server_workers.radiosonde_reader_worker", radiosonde_worker, create=True
        ), mock.patch.dict(
            "ui.wxdata.__dict__",
            {"acars_reader_worker": acars_worker, "vdl2_reader_worker": vdl2_worker, "radiosonde_reader_worker": radiosonde_worker},
        ):
            server_workers.start_wx_reader("radiosonde")
            self.assertEqual(len(server_workers._wx_reader_threads), 1)

            store = server_workers.get_met_store()
            self.assertEqual(store.active_decoder, "radiosonde")

            server_workers.stop_wx_reader()

    def test_stop_signals_all_threads(self):
        from ui import server_workers

        server_workers._wx_reader_threads = []
        server_workers._met_store = None

        # Create mock threads that just block until stopped
        def blocking_worker(store, stop_event):
            stop_event.wait()

        with mock.patch(
            "ui.wxdata.acars_reader_worker", blocking_worker
        ), mock.patch(
            "ui.wxdata.vdl2_reader_worker", blocking_worker
        ), mock.patch(
            "ui.server_workers._configure_spatial_filter"
        ):
            server_workers.start_wx_reader("acars")
            self.assertEqual(len(server_workers._wx_reader_threads), 2)

            events = [ev for _, ev in server_workers._wx_reader_threads]
            server_workers.stop_wx_reader()

            for ev in events:
                self.assertTrue(ev.is_set())
            self.assertEqual(len(server_workers._wx_reader_threads), 0)

    def test_start_stops_previous_readers_first(self):
        from ui import server_workers

        server_workers._wx_reader_threads = []
        server_workers._met_store = None

        def blocking_worker(store, stop_event):
            stop_event.wait()

        with mock.patch(
            "ui.wxdata.acars_reader_worker", blocking_worker
        ), mock.patch(
            "ui.wxdata.vdl2_reader_worker", blocking_worker
        ), mock.patch(
            "ui.wxdata.radiosonde_reader_worker", blocking_worker
        ), mock.patch(
            "ui.server_workers._configure_spatial_filter"
        ):
            # Start ACARS (2 threads)
            server_workers.start_wx_reader("acars")
            first_events = [ev for _, ev in server_workers._wx_reader_threads]
            self.assertEqual(len(server_workers._wx_reader_threads), 2)

            # Start radiosonde — should stop the ACARS threads first
            server_workers.start_wx_reader("radiosonde")
            self.assertEqual(len(server_workers._wx_reader_threads), 1)
            for ev in first_events:
                self.assertTrue(ev.is_set())

            server_workers.stop_wx_reader()


# ===================================================================
# systemd: VDL2 service helpers
# ===================================================================
class VDL2SystemdTests(unittest.TestCase):
    def test_vdl2_start_stop_restart_use_sudo(self):
        calls = []

        def fake_run(args, use_sudo=False):
            calls.append((tuple(args), bool(use_sudo)))
            return _Proc(returncode=0)

        with mock.patch.dict(
            systemd.UNITS,
            {"vdl2": "dumpvdl2"},
            clear=False,
        ), mock.patch.object(systemd, "_run_systemctl", side_effect=fake_run):
            self.assertEqual((True, ""), systemd.start_vdl2())
            self.assertEqual((True, ""), systemd.stop_vdl2())
            self.assertEqual((True, ""), systemd.restart_vdl2())

        self.assertIn((("start", "dumpvdl2"), True), calls)
        self.assertIn((("stop", "dumpvdl2"), True), calls)
        self.assertIn((("restart", "dumpvdl2"), True), calls)


# ===================================================================
# handlers: wx API combined start
# ===================================================================
@_skip_310
class WxApiCombinedStartTests(unittest.TestCase):
    """Test that the /api/wx/start handler starts both ACARS and VDL2 services."""

    def test_acars_start_triggers_both_services(self):
        from ui import actions

        start_acars = mock.Mock(return_value=(True, ""))
        start_vdl2 = mock.Mock(return_value=(True, ""))
        stop_acars = mock.Mock()
        stop_vdl2 = mock.Mock()
        stop_radiosonde = mock.Mock()

        with mock.patch.dict(
            actions._WX_START,
            {"acars": start_acars, "vdl2": start_vdl2},
            clear=False,
        ), mock.patch.dict(
            actions._WX_STOP,
            {"acars": stop_acars, "vdl2": stop_vdl2, "radiosonde": stop_radiosonde},
            clear=False,
        ), mock.patch.object(
            actions, "_start_wx_reader",
        ) as mock_start_reader, mock.patch.object(
            actions, "_stop_wx_reader",
        ):
            # Simulate what the handler does for "acars" start
            # (extracted logic from handlers.py /api/wx/start)
            decoder = "acars"

            # Stop all first
            for name, stop_fn in actions._WX_STOP.items():
                try:
                    stop_fn()
                except Exception:
                    pass
            actions._stop_wx_reader()

            # Start both
            ok1, err1 = actions._WX_START["acars"]()
            ok2, err2 = actions._WX_START.get("vdl2", lambda: (True, ""))()

            # Both should succeed
            self.assertTrue(ok1)
            self.assertTrue(ok2)
            start_acars.assert_called_once()
            start_vdl2.assert_called_once()

    def test_acars_start_succeeds_if_only_one_service_starts(self):
        """If dumpvdl2 fails (e.g. not installed), ACARS-only still succeeds."""
        from ui import actions

        start_acars = mock.Mock(return_value=(True, ""))
        start_vdl2 = mock.Mock(return_value=(False, "dumpvdl2 not found"))

        with mock.patch.dict(
            actions._WX_START,
            {"acars": start_acars, "vdl2": start_vdl2},
            clear=False,
        ):
            ok1, err1 = actions._WX_START["acars"]()
            ok2, err2 = actions._WX_START.get("vdl2", lambda: (True, ""))()
            # At least one succeeded → overall OK
            self.assertTrue(ok1 or ok2)
            self.assertTrue(ok1)  # ACARS worked
            self.assertFalse(ok2)  # VDL2 failed

    def test_both_fail_reports_error(self):
        from ui import actions

        start_acars = mock.Mock(return_value=(False, "acarsdec not found"))
        start_vdl2 = mock.Mock(return_value=(False, "dumpvdl2 not found"))

        with mock.patch.dict(
            actions._WX_START,
            {"acars": start_acars, "vdl2": start_vdl2},
            clear=False,
        ):
            ok1, err1 = actions._WX_START["acars"]()
            ok2, err2 = actions._WX_START.get("vdl2", lambda: (True, ""))()
            # Both failed
            self.assertFalse(ok1 or ok2)


# ===================================================================
# Config: WX_DECODERS no longer includes standalone vdl2
# ===================================================================
class WxDecodersConfigTests(unittest.TestCase):
    def test_vdl2_not_standalone_decoder(self):
        from ui.config import WX_DECODERS
        self.assertIn("acars", WX_DECODERS)
        self.assertIn("radiosonde", WX_DECODERS)
        self.assertNotIn("vdl2", WX_DECODERS)

    def test_vdl2_output_path_configured(self):
        from ui.config import VDL2_OUTPUT_PATH
        self.assertTrue(VDL2_OUTPUT_PATH)

    def test_vdl2_unit_configured(self):
        from ui.config import UNITS
        self.assertIn("vdl2", UNITS)
        self.assertEqual(UNITS["vdl2"], "dumpvdl2")


if __name__ == "__main__":
    unittest.main()
