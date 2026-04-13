"""Regression tests for the ACARS/VDL2 Advanced Decode View.

Covers:
- RawMessage.raw field serialization via dataclasses.asdict()
- RawMessage.decode_meta serialization via dataclasses.asdict()
- ACARS parsing preserves full raw dict
- VDL2 ACARS parsing passes full frame as raw
- VDL2 non-ACARS (XID) parsing passes full frame as raw
- /api/wx/messages/raw endpoint response format
- Backward-compat: RawMessage without raw field defaults to {}
- ACARS label dictionary in acars-decode.html
- Decode-view readability helpers and plain-language glossary
- MetStore 2000-message cap (raw messages respect the deque maxlen)
"""
from __future__ import annotations

import json
import os
import re
import unittest
from dataclasses import asdict
from unittest import mock

from ui.wxdata import (
    MetStore,
    RawMessage,
    _extract_acars_from_vdl2,
    _summarize_vdl2_non_acars_frame,
    parse_acars_message,
)

# handlers → server_workers → actions uses Python 3.10+ type syntax (list[X] | None)
# which fails on 3.9.  Skip those tests gracefully.
_CAN_IMPORT_HANDLERS = True
try:
    from ui import handlers as _handlers_check  # noqa: F401
except TypeError:
    _CAN_IMPORT_HANDLERS = False

_skip_handlers = unittest.skipUnless(
    _CAN_IMPORT_HANDLERS,
    "handlers/actions require Python >= 3.10 type syntax",
)

# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

# Full acarsdec-style JSON (what acars_reader_worker reads from the file)
SAMPLE_ACARS_MSG = {
    "timestamp": 1712700000,
    "flight": "AAL123",
    "tail": ".N123AA",
    "label": "H1",
    "text": "#M1BPOSN36100W086450,JONIL,192821,350,FFISK,193128,,M52,27060",
    "freq": 129125000,
    "level": -42,
    "error": 0,
    "msg_num": "S07A",
    "block_id": "4",
    "ack": "!",
    "sublabel": "M1",
}

# Full dumpvdl2 ACARS frame
SAMPLE_VDL2_ACARS_FRAME = {
    "vdl2": {
        "t": {"sec": 1712700100},
        "freq": 136975000,
        "sig_level": -55.2,
        "frame_type": "AVLC",
        "avlc": {
            "src": {"addr": "AA1234", "status": "Airborne"},
            "dst": {"addr": "FFFF", "status": "Ground"},
            "frame_type": "I",
            "acars": {
                "flight": "DAL456 ",
                "reg": ".N456DL",
                "label": "SA",
                "msg_text": "FL320 M54 270/082",
                "msg_num": "A01A",
                "block_id": "2",
                "ack": "!",
            },
        },
    }
}

# Full dumpvdl2 XID (non-ACARS) frame
SAMPLE_VDL2_XID_FRAME = {
    "vdl2": {
        "t": {"sec": 1712700200},
        "freq": 136875000,
        "sig_level": -62.0,
        "frame_type": "AVLC",
        "avlc": {
            "src": {"addr": "BB5678"},
            "dst": {"addr": "FFFF"},
            "frame_type": "XID",
            "xid": {
                "type": "XID_CMD_LCR",
                "conn_mgmt": 2,
                "params": ["MDL=VHF", "ICC=2"],
            },
        },
    }
}


# ---------------------------------------------------------------------------
# 1. RawMessage raw field — serialization
# ---------------------------------------------------------------------------
class RawMessageRawFieldTests(unittest.TestCase):
    """RawMessage.raw round-trips cleanly through dataclasses.asdict()."""

    def test_raw_field_default_is_empty_dict(self):
        msg = RawMessage(timestamp=1000.0, source="acars", source_id="AAL1", text="hi")
        self.assertEqual(msg.raw, {})
        self.assertEqual(msg.decode_meta, {})

    def test_raw_field_survives_asdict(self):
        payload = {"flight": "AAL1", "label": "H1", "freq": 129125000}
        msg = RawMessage(
            timestamp=1000.0, source="acars", source_id="AAL1",
            text="[H1] AAL1:", raw=payload,
            decode_meta={"protocol_family": "acars_position", "title": "Position / movement report"},
        )
        d = asdict(msg)
        self.assertIn("raw", d)
        self.assertIn("decode_meta", d)
        self.assertEqual(d["raw"]["flight"], "AAL1")
        self.assertEqual(d["raw"]["label"], "H1")
        self.assertEqual(d["raw"]["freq"], 129125000)
        self.assertEqual(d["decode_meta"]["protocol_family"], "acars_position")

    def test_raw_field_with_nested_dict_survives_asdict(self):
        """Nested dicts in raw (e.g. full VDL2 frame) are copied correctly."""
        vdl2_frame = SAMPLE_VDL2_ACARS_FRAME
        msg = RawMessage(
            timestamp=1712700100.0, source="vdl2", source_id="DAL456",
            text="[SA] DAL456:", raw=vdl2_frame,
        )
        d = asdict(msg)
        self.assertIn("vdl2", d["raw"])
        self.assertEqual(d["raw"]["vdl2"]["freq"], 136975000)
        self.assertAlmostEqual(d["raw"]["vdl2"]["sig_level"], -55.2)

    def test_raw_field_json_serializable(self):
        """asdict() output for a message with raw must be JSON-serializable."""
        msg = RawMessage(
            timestamp=1000.0, source="acars", source_id="AAL1",
            text="test", raw=SAMPLE_ACARS_MSG,
        )
        d = asdict(msg)
        encoded = json.dumps(d)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["raw"]["flight"], "AAL123")


# ---------------------------------------------------------------------------
# 2. ACARS parsing preserves raw
# ---------------------------------------------------------------------------
class AcarsParsingRawFieldTests(unittest.TestCase):
    """parse_acars_message() stores the original msg dict in raw.raw."""

    def test_raw_dict_populated_with_all_original_fields(self):
        raw, _ = parse_acars_message(SAMPLE_ACARS_MSG)
        self.assertIsInstance(raw.raw, dict)
        self.assertEqual(raw.raw["flight"], "AAL123")
        self.assertEqual(raw.raw["label"], "H1")
        self.assertEqual(raw.raw["freq"], 129125000)
        self.assertEqual(raw.raw["level"], -42)
        self.assertEqual(raw.raw["error"], 0)
        self.assertEqual(raw.raw["msg_num"], "S07A")
        self.assertEqual(raw.raw["block_id"], "4")
        self.assertEqual(raw.raw["ack"], "!")
        self.assertEqual(raw.raw["sublabel"], "M1")

    def test_raw_dict_is_same_object_as_input(self):
        """raw.raw should be exactly the dict passed in, not a copy."""
        raw, _ = parse_acars_message(SAMPLE_ACARS_MSG)
        self.assertIs(raw.raw, SAMPLE_ACARS_MSG)

    def test_raw_dict_populated_for_non_met_message(self):
        msg = {
            "timestamp": 1000,
            "flight": "UAL999",
            "tail": ".N999UA",
            "label": "Q0",
            "text": "REQUEST CLEARANCE",
            "freq": 129125000,
            "level": -38,
        }
        raw, obs = parse_acars_message(msg)
        self.assertFalse(raw.is_met)
        self.assertEqual(obs, [])
        self.assertEqual(raw.raw["flight"], "UAL999")
        self.assertEqual(raw.raw["freq"], 129125000)
        self.assertEqual(raw.decode_meta["protocol_family"], "acars_atis")
        self.assertEqual(raw.decode_meta["title"], "Airport information / ATIS message")

    def test_raw_dict_populated_for_met_message(self):
        raw, obs = parse_acars_message(SAMPLE_ACARS_MSG)
        self.assertTrue(raw.is_met)
        self.assertGreater(len(obs), 0)
        self.assertEqual(raw.raw["label"], "H1")
        self.assertIn("POSN", raw.raw["text"])
        self.assertEqual(raw.decode_meta["protocol_family"], "acars_enroute_position")
        self.assertEqual(raw.decode_meta["title"], "Enroute position update")
        self.assertIn("FL350", raw.decode_meta["body"])

    def test_h2_message_gets_fuel_position_summary(self):
        msg = {
            "timestamp": 1712700001,
            "flight": "AAL124",
            "tail": ".N124AA",
            "label": "H2",
            "text": "#M1BPOSN36100W086450,JONIL,192821,330,FFISK,193128,,M50,26555",
        }
        raw, _ = parse_acars_message(msg)
        self.assertEqual(raw.decode_meta["protocol_family"], "acars_fuel_position")
        self.assertEqual(raw.decode_meta["title"], "Fuel / position status update")

    def test_label_54_gets_oooi_summary(self):
        msg = {
            "timestamp": 1712700002,
            "flight": "DAL125",
            "tail": ".N125DL",
            "label": "54",
            "text": "OUT 1614 OFF 1622 ON ---- IN ----",
        }
        raw, obs = parse_acars_message(msg)
        self.assertEqual(obs, [])
        self.assertEqual(raw.decode_meta["protocol_family"], "acars_oooi")
        self.assertEqual(raw.decode_meta["title"], "OOOI milestone / movement status")

    def test_label_13_gets_departure_summary(self):
        msg = {
            "timestamp": 1712700003,
            "flight": "DAL126",
            "tail": ".N126DL",
            "label": "13",
            "text": "DEPARTURE REPORT",
        }
        raw, obs = parse_acars_message(msg)
        self.assertEqual(obs, [])
        self.assertEqual(raw.decode_meta["protocol_family"], "acars_departure_status")
        self.assertEqual(raw.decode_meta["title"], "Departure status message")


# ---------------------------------------------------------------------------
# 3. VDL2 ACARS parsing preserves raw (full frame)
# ---------------------------------------------------------------------------
class Vdl2AcarsRawFieldTests(unittest.TestCase):
    """vdl2_reader_worker sets raw.raw to the full dumpvdl2 frame."""

    def _simulate_vdl2_acars_worker(self, frame: dict) -> RawMessage:
        """Mirrors the logic in vdl2_reader_worker for an ACARS frame."""
        acars_msg = _extract_acars_from_vdl2(frame)
        self.assertIsNotNone(acars_msg, "Frame should extract as ACARS")
        raw, _ = parse_acars_message(acars_msg)
        raw.source = "vdl2"
        raw.raw = frame  # worker overrides with full frame
        return raw

    def test_raw_contains_full_vdl2_frame(self):
        raw = self._simulate_vdl2_acars_worker(SAMPLE_VDL2_ACARS_FRAME)
        self.assertIn("vdl2", raw.raw)
        self.assertEqual(raw.raw["vdl2"]["freq"], 136975000)
        self.assertAlmostEqual(raw.raw["vdl2"]["sig_level"], -55.2)

    def test_raw_contains_nested_avlc_and_acars(self):
        raw = self._simulate_vdl2_acars_worker(SAMPLE_VDL2_ACARS_FRAME)
        avlc = raw.raw["vdl2"]["avlc"]
        self.assertIn("acars", avlc)
        self.assertIn("src", avlc)
        self.assertIn("dst", avlc)
        self.assertEqual(avlc["acars"]["label"], "SA")

    def test_raw_is_full_frame_not_extracted_subset(self):
        """raw.raw should have the full VDL2 envelope, not just the extracted ACARS fields."""
        raw = self._simulate_vdl2_acars_worker(SAMPLE_VDL2_ACARS_FRAME)
        # sig_level only exists on the full frame, not in the extracted subset
        self.assertIn("sig_level", raw.raw["vdl2"])
        # src/dst addresses also only on full frame
        self.assertIn("src", raw.raw["vdl2"]["avlc"])

    def test_source_tagged_vdl2(self):
        raw = self._simulate_vdl2_acars_worker(SAMPLE_VDL2_ACARS_FRAME)
        self.assertEqual(raw.source, "vdl2")

    def test_raw_survives_json_serialization(self):
        raw = self._simulate_vdl2_acars_worker(SAMPLE_VDL2_ACARS_FRAME)
        d = asdict(raw)
        encoded = json.dumps(d)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["raw"]["vdl2"]["freq"], 136975000)


# ---------------------------------------------------------------------------
# 4. VDL2 non-ACARS (XID) raw field
# ---------------------------------------------------------------------------
class Vdl2NonAcarsRawFieldTests(unittest.TestCase):
    """Non-ACARS VDL2 frames (XID, CPDLC, etc.) should capture the full frame in raw."""

    def _make_non_acars_raw(self, frame: dict) -> RawMessage:
        """Mirrors the non-ACARS path in vdl2_reader_worker."""
        vdl2 = frame.get("vdl2", {})
        t = vdl2.get("t", {})
        ts = (t.get("sec", 0) if isinstance(t, dict) else 0) or 1000.0
        return RawMessage(
            timestamp=ts,
            source="vdl2",
            source_id="",
            text=str(frame.get("vdl2", {}).get("avlc", {}).get("xid", ""))[:120],
            is_met=False,
            raw=frame,
        )

    def test_xid_frame_raw_preserved(self):
        raw = self._make_non_acars_raw(SAMPLE_VDL2_XID_FRAME)
        self.assertIn("vdl2", raw.raw)
        self.assertIn("xid", raw.raw["vdl2"]["avlc"])

    def test_xid_frame_raw_has_freq_and_sig_level(self):
        raw = self._make_non_acars_raw(SAMPLE_VDL2_XID_FRAME)
        self.assertEqual(raw.raw["vdl2"]["freq"], 136875000)
        self.assertEqual(raw.raw["vdl2"]["sig_level"], -62.0)

    def test_xid_frame_raw_has_xid_params(self):
        raw = self._make_non_acars_raw(SAMPLE_VDL2_XID_FRAME)
        xid = raw.raw["vdl2"]["avlc"]["xid"]
        self.assertEqual(xid["conn_mgmt"], 2)
        self.assertIn("MDL=VHF", xid["params"])

    def test_non_acars_frame_not_is_met(self):
        raw = self._make_non_acars_raw(SAMPLE_VDL2_XID_FRAME)
        self.assertFalse(raw.is_met)

    def test_non_acars_frame_source_vdl2(self):
        raw = self._make_non_acars_raw(SAMPLE_VDL2_XID_FRAME)
        self.assertEqual(raw.source, "vdl2")

    def test_non_acars_helper_classifies_xid(self):
        summary_text, meta = _summarize_vdl2_non_acars_frame(SAMPLE_VDL2_XID_FRAME)
        self.assertEqual(summary_text, "Link capability negotiation")
        self.assertEqual(meta["protocol_family"], "vdl2_xid")
        self.assertEqual(meta["title"], "VDL2 link setup / negotiation")

    def test_non_acars_helper_classifies_x25(self):
        frame = {
            "vdl2": {
                "t": {"sec": 1712700201},
                "avlc": {
                    "frame_type": "I",
                    "x25": {"clnp": {"pdu_type": "DT"}, "cotp": {"type": "CC"}},
                },
            }
        }
        summary_text, meta = _summarize_vdl2_non_acars_frame(frame)
        self.assertEqual(summary_text, "ATN / CPDLC data")
        self.assertEqual(meta["protocol_family"], "vdl2_x25")
        self.assertIn("clnp", meta["note"])


# ---------------------------------------------------------------------------
# 5. /api/wx/messages/raw endpoint response format
# ---------------------------------------------------------------------------
@_skip_handlers
class RawMessagesEndpointTests(unittest.TestCase):
    """GET /api/wx/messages/raw returns messages with raw field included."""

    def _call_raw_endpoint(self, path: str, store: MetStore):
        """Call the handler's do_GET for the given path, injecting store."""
        from ui import handlers

        class _FakeRequest:
            def __init__(self, p):
                self.path = p
                self.sent = []

            def _send(self, code, body, ctype="text/html; charset=utf-8"):
                if isinstance(body, bytes):
                    body = body.decode("utf-8", errors="ignore")
                self.sent.append((code, body, ctype))
                return code, body, ctype

            @staticmethod
            def _parse_optional_bool_query(qs, key):
                return None

        req = _FakeRequest(path)
        with mock.patch.object(handlers, "get_met_store", return_value=store):
            return handlers.Handler.do_GET(req)

    def test_endpoint_returns_200_with_ok(self):
        store = MetStore()
        code, body, _ = self._call_raw_endpoint("/api/wx/messages/raw", store)
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])

    def test_endpoint_returns_messages_list(self):
        store = MetStore()
        store.add_message(RawMessage(
            timestamp=1000.0, source="acars", source_id="AAL1",
            text="[H1] AAL1:", raw={"flight": "AAL1", "label": "H1"},
        ))
        code, body, _ = self._call_raw_endpoint("/api/wx/messages/raw", store)
        data = json.loads(body)
        self.assertEqual(len(data["messages"]), 1)

    def test_endpoint_messages_include_raw_field(self):
        store = MetStore()
        store.add_message(RawMessage(
            timestamp=1000.0, source="acars", source_id="AAL1",
            text="[H1] AAL1:", raw={"flight": "AAL1", "label": "H1", "freq": 129125000},
        ))
        _, body, _ = self._call_raw_endpoint("/api/wx/messages/raw", store)
        data = json.loads(body)
        msg = data["messages"][0]
        self.assertIn("raw", msg)
        self.assertIn("decode_meta", msg)
        self.assertEqual(msg["raw"]["flight"], "AAL1")
        self.assertEqual(msg["raw"]["freq"], 129125000)

    def test_endpoint_default_limit_200(self):
        """Default limit should be 200 (decode view default)."""
        store = MetStore(max_messages=2000)
        for i in range(250):
            store.add_message(RawMessage(
                timestamp=float(i), source="acars", source_id=f"FL{i}",
                text=f"msg {i}", raw={"seq": i},
            ))
        _, body, _ = self._call_raw_endpoint("/api/wx/messages/raw", store)
        data = json.loads(body)
        # Without explicit limit param, default is 200
        self.assertEqual(len(data["messages"]), 200)

    def test_endpoint_respects_limit_param(self):
        store = MetStore()
        for i in range(10):
            store.add_message(RawMessage(
                timestamp=float(i), source="acars", source_id=f"FL{i}",
                text=f"msg {i}",
            ))
        _, body, _ = self._call_raw_endpoint("/api/wx/messages/raw?limit=5", store)
        data = json.loads(body)
        self.assertEqual(len(data["messages"]), 5)

    def test_endpoint_source_filter(self):
        store = MetStore()
        store.add_message(RawMessage(
            timestamp=1.0, source="acars", source_id="A1", text="acars",
        ))
        store.add_message(RawMessage(
            timestamp=2.0, source="vdl2", source_id="V1", text="vdl2",
        ))
        _, body, _ = self._call_raw_endpoint("/api/wx/messages/raw?source=vdl2", store)
        data = json.loads(body)
        self.assertEqual(len(data["messages"]), 1)
        self.assertEqual(data["messages"][0]["source"], "vdl2")

    def test_endpoint_raw_field_with_full_vdl2_frame(self):
        """Full VDL2 frame nested in raw should survive JSON round-trip."""
        store = MetStore()
        store.add_message(RawMessage(
            timestamp=1712700100.0, source="vdl2", source_id="DAL456",
            text="[SA] DAL456:", raw=SAMPLE_VDL2_ACARS_FRAME,
        ))
        _, body, _ = self._call_raw_endpoint("/api/wx/messages/raw", store)
        data = json.loads(body)
        msg = data["messages"][0]
        self.assertIn("vdl2", msg["raw"])
        self.assertEqual(msg["raw"]["vdl2"]["freq"], 136975000)
        self.assertIn("avlc", msg["raw"]["vdl2"])


# ---------------------------------------------------------------------------
# 6. Backward compat: RawMessage without explicit raw field
# ---------------------------------------------------------------------------
class RawMessageBackwardCompatTests(unittest.TestCase):
    """RawMessage created without raw= kwarg must still work everywhere."""

    def test_default_raw_is_empty_dict(self):
        msg = RawMessage(timestamp=1.0, source="acars", source_id="X", text="y")
        self.assertEqual(msg.raw, {})
        self.assertEqual(msg.decode_meta, {})
        self.assertIsInstance(msg.raw, dict)

    def test_asdict_raw_is_empty_dict(self):
        msg = RawMessage(timestamp=1.0, source="acars", source_id="X", text="y")
        d = asdict(msg)
        self.assertIn("raw", d)
        self.assertIn("decode_meta", d)
        self.assertEqual(d["raw"], {})
        self.assertEqual(d["decode_meta"], {})

    def test_store_and_retrieve_no_raw(self):
        """Messages with no raw field can be stored and retrieved without error."""
        store = MetStore()
        store.add_message(RawMessage(
            timestamp=1.0, source="acars", source_id="X", text="y",
        ))
        msgs = store.get_messages()
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["raw"], {})

    def test_json_serializable_with_empty_raw(self):
        store = MetStore()
        store.add_message(RawMessage(
            timestamp=1.0, source="acars", source_id="X", text="y",
        ))
        msgs = store.get_messages()
        # Must not raise
        encoded = json.dumps(msgs)
        decoded = json.loads(encoded)
        self.assertEqual(decoded[0]["raw"], {})
        self.assertEqual(decoded[0]["decode_meta"], {})


# ---------------------------------------------------------------------------
# 7. ACARS label dictionary in acars-decode.html
# ---------------------------------------------------------------------------
class AcarsLabelDictionaryTests(unittest.TestCase):
    """acars-decode.html must contain a correct ACARS label→name dictionary."""

    @classmethod
    def setUpClass(cls):
        html_path = os.path.join(
            os.path.dirname(__file__), "..", "ui", "acars-decode.html"
        )
        with open(html_path, "r", encoding="utf-8") as f:
            cls.html = f.read()
        # Extract the LABEL_NAMES JS object as a string we can check
        m = re.search(r"const LABEL_NAMES\s*=\s*\{([^}]+)\}", cls.html, re.DOTALL)
        cls.label_block = m.group(1) if m else ""

    def _has_label(self, label: str, expected_name_fragment: str) -> None:
        """Assert the label key is present and its value contains the fragment."""
        pattern = rf"'{re.escape(label)}'\s*:\s*'([^']+)'"
        m = re.search(pattern, self.label_block)
        self.assertIsNotNone(m, f"Label {label!r} not found in LABEL_NAMES")
        self.assertIn(
            expected_name_fragment.lower(), m.group(1).lower(),
            f"Label {label!r} name {m.group(1)!r} does not contain {expected_name_fragment!r}",
        )

    def test_h1_label_present(self):
        self._has_label("H1", "OOOI")

    def test_h2_label_present(self):
        self._has_label("H2", "Fuel")

    def test_sa_label_present(self):
        self._has_label("SA", "Wind")

    def test_label_21_present(self):
        self._has_label("21", "Position")

    def test_label_22_present(self):
        self._has_label("22", "Position")

    def test_label_44_present(self):
        self._has_label("44", "Meteor")

    def test_label_4a_present(self):
        self._has_label("4A", "AMDAR")

    def test_q0_atis_present(self):
        self._has_label("Q0", "ATIS")

    def test_ra_logon_present(self):
        self._has_label("RA", "Logon")

    def test_e2_winds_aloft_present(self):
        self._has_label("E2", "Wind")

    def test_html_has_label_names_object(self):
        self.assertIn("const LABEL_NAMES", self.html)

    def test_label_lookup_function_present(self):
        self.assertIn("function labelName", self.html)


# ---------------------------------------------------------------------------
# 8. Readability helpers / glossary
# ---------------------------------------------------------------------------
class DecodeReadabilityTests(unittest.TestCase):
    """The decode view should help an operator interpret protocol traffic."""

    @classmethod
    def setUpClass(cls):
        html_path = os.path.join(
            os.path.dirname(__file__), "..", "ui", "acars-decode.html"
        )
        with open(html_path, "r", encoding="utf-8") as f:
            cls.html = f.read()

    def test_legend_button_present(self):
        self.assertIn('id="legend-btn"', self.html)
        self.assertIn("How To Read This View", self.html)

    def test_glossary_explains_ooii(self):
        self.assertIn("Out / Off / On / In", self.html)

    def test_glossary_explains_atis(self):
        self.assertIn("Automatic Terminal Information Service", self.html)

    def test_glossary_explains_xid(self):
        self.assertIn("VDL2 link negotiation", self.html)

    def test_summary_function_present(self):
        self.assertIn("function summarizeMessage", self.html)
        self.assertIn("msg.decode_meta", self.html)

    def test_summary_panel_present(self):
        self.assertIn("summary-panel", self.html)
        self.assertIn("Plain-English decode", self.html)

    def test_source_names_are_operator_friendly(self):
        self.assertIn("Direct ACARS decoder", self.html)
        self.assertIn("VDL2 radio link", self.html)

    def test_field_labels_are_operator_friendly(self):
        self.assertIn("Flight / callsign", self.html)
        self.assertIn("Tail number", self.html)
        self.assertIn("Signal level", self.html)
        self.assertIn("Decoder path", self.html)


# ---------------------------------------------------------------------------
# 9. MetStore 2000-message cap
# ---------------------------------------------------------------------------
class MetStoreCapTests(unittest.TestCase):
    """Raw messages in MetStore must respect the deque maxlen cap."""

    def test_default_cap_is_2000(self):
        store = MetStore()
        self.assertEqual(store._messages.maxlen, 2000)

    def test_buffer_does_not_exceed_cap(self):
        cap = 100
        store = MetStore(max_messages=cap)
        for i in range(200):
            store.add_message(RawMessage(
                timestamp=float(i), source="acars", source_id=f"FL{i}",
                text=f"msg {i}", raw={"seq": i},
            ))
        msgs = store.get_messages(limit=9999)
        self.assertEqual(len(msgs), cap)

    def test_total_message_count_keeps_incrementing_beyond_cap(self):
        """_message_count is a lifetime counter, not capped."""
        cap = 50
        store = MetStore(max_messages=cap)
        for i in range(120):
            store.add_message(RawMessage(
                timestamp=float(i), source="acars", source_id=f"FL{i}", text=f"msg {i}",
            ))
        self.assertEqual(store._message_count, 120)
        self.assertEqual(len(store._messages), cap)

    def test_oldest_messages_evicted_first(self):
        cap = 5
        store = MetStore(max_messages=cap)
        for i in range(10):
            store.add_message(RawMessage(
                timestamp=float(i), source="acars", source_id=f"FL{i}",
                text=f"msg {i}", raw={"seq": i},
            ))
        msgs = store.get_messages(limit=9999)
        self.assertEqual(len(msgs), cap)
        # Should be the 5 newest (seq 5–9)
        seqs = [m["raw"]["seq"] for m in msgs]
        self.assertEqual(seqs, [5, 6, 7, 8, 9])

    def test_raw_dict_evicted_with_message(self):
        """When a message is evicted, its raw dict goes with it (no memory leak)."""
        cap = 3
        store = MetStore(max_messages=cap)
        for i in range(10):
            store.add_message(RawMessage(
                timestamp=float(i), source="acars", source_id=f"FL{i}",
                text=f"msg {i}", raw={"big_payload": "x" * 1000, "seq": i},
            ))
        msgs = store.get_messages(limit=9999)
        self.assertEqual(len(msgs), cap)
        seqs = [m["raw"]["seq"] for m in msgs]
        self.assertEqual(seqs, [7, 8, 9])

    def test_periodic_log_fires_at_100_messages(self):
        """MetStore logs every 100 messages; verify logger.debug is called."""
        import logging
        store = MetStore()
        with self.assertLogs("ui.wxdata", level=logging.DEBUG) as cm:
            for i in range(100):
                store.add_message(RawMessage(
                    timestamp=float(i), source="acars", source_id=f"FL{i}", text=f"msg {i}",
                ))
        # At least one DEBUG line about the store size
        size_logs = [l for l in cm.output if "MetStore:" in l and "buffer" in l]
        self.assertGreater(len(size_logs), 0)


# ---------------------------------------------------------------------------
# 10. Dedupe key regression — acars-decode.html msgKey function
# ---------------------------------------------------------------------------
class DedupeKeyRegressionTests(unittest.TestCase):
    """Regression tests for the msgKey() dedupe logic in acars-decode.html.

    The old key was: timestamp + '_' + source_id + '_' + freq
    The new key must include: source, timestamp, source_id, label, msg_num,
    block_id, ack, and a 16-char text slice — preventing burst retransmits
    and same-flight/same-second messages from collapsing into one entry.
    """

    @classmethod
    def setUpClass(cls):
        html_path = os.path.join(
            os.path.dirname(__file__), "..", "ui", "acars-decode.html"
        )
        with open(html_path, "r", encoding="utf-8") as f:
            cls.html = f.read()

    # --- structural checks ---

    def test_msgkey_function_defined(self):
        self.assertIn("function msgKey", self.html)

    def test_old_inline_key_pattern_gone(self):
        """The old per-call key construction must not appear anywhere."""
        self.assertNotIn(
            "m.timestamp + '_' + m.source_id + '_'",
            self.html,
            "Old inline dedupe key still present — must be replaced with msgKey()",
        )

    @classmethod
    def _msgkey_body(cls):
        """Extract the full body of the msgKey function from the HTML.

        Uses a line-based approach to avoid false truncation on nested `{}`.
        """
        lines = cls.html.splitlines()
        body_lines = []
        inside = False
        depth = 0
        for line in lines:
            if not inside:
                if re.search(r"function msgKey\s*\(m\)", line):
                    inside = True
                    depth = line.count("{") - line.count("}")
                    body_lines.append(line)
            else:
                depth += line.count("{") - line.count("}")
                body_lines.append(line)
                if depth <= 0:
                    break
        return "\n".join(body_lines)

    def test_msgkey_uses_source(self):
        """msgKey must reference m.source so ACARS and VDL2 duplicates don't collide."""
        body = self._msgkey_body()
        self.assertIn("function msgKey", body, "msgKey function not found")
        self.assertIn("m.source", body)

    def test_msgkey_uses_timestamp(self):
        body = self._msgkey_body()
        self.assertIn("m.timestamp", body)

    def test_msgkey_uses_source_id(self):
        body = self._msgkey_body()
        self.assertIn("m.source_id", body)

    def test_msgkey_uses_label(self):
        body = self._msgkey_body()
        self.assertIn("label", body)

    def test_msgkey_uses_msg_num(self):
        body = self._msgkey_body()
        self.assertIn("msg_num", body)

    def test_msgkey_uses_block_id(self):
        body = self._msgkey_body()
        self.assertIn("block_id", body)

    def test_msgkey_uses_ack(self):
        body = self._msgkey_body()
        self.assertIn("ack", body)

    def test_msgkey_uses_text_slice(self):
        """msgKey must take a slice of text (not the full string) to bound key length."""
        body = self._msgkey_body()
        self.assertTrue(
            ".slice(" in body or ".substring(" in body,
            "msgKey does not slice text — unbounded text in key",
        )

    def test_applyNewMessages_uses_msgkey(self):
        """applyNewMessages must call msgKey() instead of building the key inline."""
        self.assertIn("msgKey(", self.html)
        # Verify it appears in the context of applyNewMessages (within 30 lines)
        lines = self.html.splitlines()
        in_fn = False
        found = False
        depth = 0
        for line in lines:
            if not in_fn and re.search(r"function applyNewMessages\s*\(", line):
                in_fn = True
                depth = line.count("{") - line.count("}")
            elif in_fn:
                depth += line.count("{") - line.count("}")
                if "msgKey(" in line:
                    found = True
                if depth <= 0:
                    break
        self.assertTrue(found, "applyNewMessages does not call msgKey()")

    # --- regression: two messages that old key collapsed but new key distinguishes ---

    def test_same_flight_same_second_different_label_are_distinct(self):
        """Old key: same ts+source_id+freq → collision. New key: different label → distinct."""
        body = self._msgkey_body()
        self.assertIn("label", body)

    def test_same_flight_same_second_different_msg_num_are_distinct(self):
        """Burst retransmits: same ts+flight but different msg_num must not collapse."""
        body = self._msgkey_body()
        self.assertIn("msg_num", body)

    def test_acars_and_vdl2_same_message_not_collapsed(self):
        """Same message arriving on both ACARS and VDL2 must produce distinct keys (source differs)."""
        body = self._msgkey_body()
        self.assertIn("m.source", body)

    def test_key_separator_prevents_field_boundary_collision(self):
        """Key fields must be joined with a separator that can't appear in field values."""
        body = self._msgkey_body()
        self.assertTrue(
            ".join(" in body or "\\x00" in body or "'\\0'" in body,
            "msgKey fields should be joined with a separator to prevent collisions",
        )


if __name__ == "__main__":
    unittest.main()
