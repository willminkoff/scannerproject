from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from ui import libacars_bridge
from ui import wxdata
from ui.wxdata import MetObservation, MetStore, RawMessage, parse_acars_message


class _FakeBackend:
    def __init__(self, *, message_result=None, vdl2_result=None):
        self.available = True
        self.name = "fake"
        self.reason = ""
        self._message_result = message_result
        self._vdl2_result = vdl2_result

    def decode_message(self, msg: dict):
        del msg
        return self._message_result

    def decode_vdl2_frame(self, frame: dict):
        del frame
        return self._vdl2_result


class _FakeBridgeModule:
    def __init__(self, *, message_result=None, vdl2_result=None):
        self.message_calls = 0
        self.vdl2_calls = 0
        self._message_result = message_result
        self._vdl2_result = vdl2_result

    def decode_message_to_observations(self, msg: dict):
        self.message_calls += 1
        del msg
        return self._message_result

    def decode_vdl2_frame_to_observations(self, frame: dict):
        self.vdl2_calls += 1
        del frame
        return self._vdl2_result


SAMPLE_NON_NATIVE_ACARS = {
    "timestamp": 1712800000,
    "flight": "AAL900",
    "tail": ".N900AA",
    "label": "5Z",
    "text": "UNPARSED HIGHER LAYER WEATHER PAYLOAD",
}

SAMPLE_DFB_ACARS = {
    "timestamp": 1712800005,
    "flight": "FFT902",
    "tail": ".N902FR",
    "label": "5Z",
    "text": (
        "#DFB/VFFT902/X36100N086450W/"
        "S FL350 270/080 RH45/"
        "T FL330 255/070 M47 DP M55"
    ),
}

SAMPLE_NATIVE_ACARS = {
    "timestamp": 1712800001,
    "flight": "DAL901",
    "tail": ".N901DL",
    "label": "H1",
    "text": "#M1BPOSN36100W086450,JONIL,192821,350,FFISK,193128,,M52,27060",
}

SAMPLE_VDL2_NON_ACARS = {
    "vdl2": {
        "t": {"sec": 1712800002},
        "freq": 136875000,
        "avlc": {
            "x25": {
                "clnp": {"pdu_type": "DT"},
                "cotp": {"type": "CC"},
            }
        },
    }
}

SAMPLE_VDL2_DFB_NON_ACARS = {
    "timestamp": 1712800006,
    "vdl2": {
        "t": {"sec": 1712800006},
        "freq": 136875000,
        "avlc": {
            "x25": {
                "clnp": {"pdu_type": "DT"},
                "cotp": {"type": "CC"},
                "text": "#DFB/VUAL777/X36100N086450W/S FL280 240/068",
            }
        },
    },
}


class LibacarsBridgeNormalizationTests(unittest.TestCase):
    def test_decode_message_accepts_partial_observation_without_temp(self):
        backend = _FakeBackend(
            message_result={
                "summary": "Normalized AMDAR profile",
                "observations": [
                    {
                        "lat": 36.10,
                        "lon": -86.70,
                        "altitude_ft": 35000,
                        "wind_dir_deg": 270,
                        "wind_speed_kt": 82,
                    }
                ],
            }
        )
        with mock.patch.object(libacars_bridge, "_BACKEND", backend):
            raw, obs = libacars_bridge.decode_message_to_observations(SAMPLE_NON_NATIVE_ACARS)

        self.assertIsNotNone(raw)
        self.assertEqual(1, len(obs))
        self.assertEqual("acars", raw.source)
        self.assertEqual("acars", obs[0].source)
        self.assertEqual(-9999.0, obs[0].temp_c)
        self.assertEqual(82.0, obs[0].wind_speed_kt)
        self.assertTrue(raw.is_met)

    def test_decode_message_noop_when_backend_unavailable(self):
        backend = libacars_bridge._UnavailableBackend("test unavailable")
        with mock.patch.object(libacars_bridge, "_BACKEND", backend):
            raw, obs = libacars_bridge.decode_message_to_observations(SAMPLE_NON_NATIVE_ACARS)

        self.assertIsNone(raw)
        self.assertEqual([], obs)

    def test_decode_message_local_dfb_payload_produces_multiple_levels_without_backend(self):
        backend = libacars_bridge._UnavailableBackend("test unavailable")
        with mock.patch.object(libacars_bridge, "_BACKEND", backend):
            raw, obs = libacars_bridge.decode_message_to_observations(SAMPLE_DFB_ACARS)

        self.assertIsNotNone(raw)
        self.assertEqual(2, len(obs))
        self.assertEqual("acars", raw.source)
        self.assertEqual("local_text", raw.decode_meta["backend"])
        self.assertEqual("libacars_bridge", raw.decode_meta["protocol_family"])
        self.assertTrue(raw.decode_meta["title"])
        self.assertEqual("acars", obs[0].source)
        self.assertEqual(35000.0, obs[0].altitude_ft)
        self.assertEqual(-9999.0, obs[0].temp_c)
        self.assertEqual(270.0, obs[0].wind_dir_deg)
        self.assertEqual(80.0, obs[0].wind_speed_kt)
        self.assertAlmostEqual(45.0, obs[0].humidity_pct)
        self.assertEqual(33000.0, obs[1].altitude_ft)
        self.assertEqual(-47.0, obs[1].temp_c)
        self.assertEqual(-55.0, obs[1].dewpoint_c)

    def test_decode_message_preserves_multiple_nested_levels_from_backend(self):
        backend = _FakeBackend(
            message_result={
                "summary": "Structured profile",
                "normalized": {
                    "flight": "AAL900",
                    "weather": {
                        "profile": {
                            "position": {"lat": 36.12, "lon": -86.72},
                            "levels": [
                                {
                                    "flight_level": 350,
                                    "wind": {"direction": 270, "speed": 80, "unit": "kt"},
                                    "temperature": {"value": -52, "unit": "c"},
                                },
                                {
                                    "altitude": {"value": 10000, "unit": "m"},
                                    "humidity": {"value": 0.45, "unit": "fraction"},
                                    "dewpoint": {"value": -58, "unit": "c"},
                                },
                            ],
                        }
                    },
                },
            }
        )
        with mock.patch.object(libacars_bridge, "_BACKEND", backend):
            raw, obs = libacars_bridge.decode_message_to_observations(SAMPLE_NON_NATIVE_ACARS)

        self.assertIsNotNone(raw)
        self.assertEqual(2, len(obs))
        self.assertEqual("fake", raw.decode_meta["backend"])
        self.assertEqual("acars", obs[0].source)
        self.assertEqual(35000.0, obs[0].altitude_ft)
        self.assertEqual(-52.0, obs[0].temp_c)
        self.assertAlmostEqual(32808.4, obs[1].altitude_ft, places=1)
        self.assertAlmostEqual(45.0, obs[1].humidity_pct)
        self.assertEqual(-58.0, obs[1].dewpoint_c)

    def test_decode_vdl2_frame_normalizes_non_acars_higher_layer_payload(self):
        backend = _FakeBackend(
            vdl2_result={
                "summary": "VDL2 normalized profile",
                "adsc": {
                    "wx": {
                        "vertical_profile": {
                            "reports": [
                                {
                                    "altitude": {"value": 280, "unit": "fl"},
                                    "wind": {"direction": 240, "speed": 68, "unit": "kt"},
                                }
                            ]
                        }
                    }
                },
                "callsign": "UAL777",
            }
        )
        with mock.patch.object(libacars_bridge, "_BACKEND", backend):
            raw, obs = libacars_bridge.decode_vdl2_frame_to_observations(SAMPLE_VDL2_NON_ACARS)

        self.assertIsNotNone(raw)
        self.assertEqual(1, len(obs))
        self.assertEqual("vdl2", raw.source)
        self.assertEqual("vdl2", obs[0].source)
        self.assertEqual("UAL777", obs[0].source_id)
        self.assertEqual(28000.0, obs[0].altitude_ft)
        self.assertEqual(-9999.0, obs[0].temp_c)
        self.assertEqual(68.0, obs[0].wind_speed_kt)

    def test_decode_vdl2_frame_local_dfb_payload_works_without_backend(self):
        backend = libacars_bridge._UnavailableBackend("test unavailable")
        with mock.patch.object(libacars_bridge, "_BACKEND", backend):
            raw, obs = libacars_bridge.decode_vdl2_frame_to_observations(SAMPLE_VDL2_DFB_NON_ACARS)

        self.assertIsNotNone(raw)
        self.assertEqual(1, len(obs))
        self.assertEqual("vdl2", raw.source)
        self.assertEqual("local_text", raw.decode_meta["backend"])
        self.assertEqual("vdl2", obs[0].source)
        self.assertEqual(28000.0, obs[0].altitude_ft)
        self.assertEqual(-9999.0, obs[0].temp_c)
        self.assertEqual(68.0, obs[0].wind_speed_kt)


class WxdataLibacarsFallbackTests(unittest.TestCase):
    def test_native_parser_wins_before_bridge_fallback(self):
        fake_bridge = _FakeBridgeModule(
            message_result=(
                RawMessage(
                    timestamp=1712800001,
                    source="acars",
                    source_id="DAL901",
                    text="bridge",
                    is_met=True,
                ),
                [
                    MetObservation(
                        timestamp=1712800001,
                        source="acars",
                        source_id="DAL901",
                        lat=36.0,
                        lon=-86.0,
                        altitude_ft=10000,
                        pressure_hpa=697.0,
                        temp_c=-10.0,
                        dewpoint_c=-20.0,
                        wind_dir_deg=270.0,
                        wind_speed_kt=40.0,
                    )
                ],
            )
        )
        with mock.patch.object(wxdata, "_LIBACARS_BRIDGE", fake_bridge):
            raw, obs = parse_acars_message(SAMPLE_NATIVE_ACARS)

        self.assertTrue(raw.is_met)
        self.assertEqual(1, len(obs))
        self.assertEqual(0, fake_bridge.message_calls)

    def test_bridge_fallback_used_when_native_parsers_yield_no_observations(self):
        bridge_raw = RawMessage(
            timestamp=1712800000,
            source="acars",
            source_id="AAL900",
            text="Normalized weather payload",
            is_met=True,
            decode_meta={"protocol_family": "libacars_bridge", "title": "Normalized payload"},
        )
        bridge_obs = [
            MetObservation(
                timestamp=1712800000,
                source="acars",
                source_id="AAL900",
                lat=36.12,
                lon=-86.72,
                altitude_ft=32000,
                pressure_hpa=274.0,
                temp_c=-9999.0,
                dewpoint_c=-9999.0,
                wind_dir_deg=255.0,
                wind_speed_kt=74.0,
            )
        ]
        fake_bridge = _FakeBridgeModule(message_result=(bridge_raw, bridge_obs))
        with mock.patch.object(wxdata, "_LIBACARS_BRIDGE", fake_bridge):
            raw, obs = parse_acars_message(SAMPLE_NON_NATIVE_ACARS)

        self.assertTrue(raw.is_met)
        self.assertEqual(1, len(obs))
        self.assertEqual(1, fake_bridge.message_calls)
        self.assertEqual("libacars_bridge", raw.decode_meta["protocol_family"])
        self.assertEqual("acars", obs[0].source)

    def test_graceful_noop_when_bridge_is_unavailable(self):
        with mock.patch.object(wxdata, "_LIBACARS_BRIDGE", None):
            raw, obs = parse_acars_message(SAMPLE_NON_NATIVE_ACARS)

        self.assertFalse(raw.is_met)
        self.assertEqual([], obs)

    def test_vdl2_non_acars_worker_can_store_bridge_observation(self):
        bridge_raw = RawMessage(
            timestamp=1712800002,
            source="vdl2",
            source_id="UAL777",
            text="Normalized VDL2 met payload",
            is_met=True,
            decode_meta={"protocol_family": "libacars_bridge", "title": "VDL2 normalized payload"},
        )
        bridge_obs = [
            MetObservation(
                timestamp=1712800002,
                source="vdl2",
                source_id="UAL777",
                lat=36.21,
                lon=-86.81,
                altitude_ft=28000,
                pressure_hpa=302.0,
                temp_c=-9999.0,
                dewpoint_c=-9999.0,
                wind_dir_deg=240.0,
                wind_speed_kt=68.0,
            )
        ]
        fake_bridge = _FakeBridgeModule(vdl2_result=(bridge_raw, bridge_obs))

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as handle:
            tmp_path = handle.name

        try:
            store = MetStore()
            store.collecting = True
            store.active_decoder = "acars"
            stop = threading.Event()

            with mock.patch.object(wxdata, "_LIBACARS_BRIDGE", fake_bridge), mock.patch.object(
                wxdata, "VDL2_OUTPUT_PATH", tmp_path
            ):
                thread = threading.Thread(target=wxdata.vdl2_reader_worker, args=(store, stop), daemon=True)
                thread.start()
                time.sleep(0.3)
                with open(tmp_path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(SAMPLE_VDL2_NON_ACARS) + "\n")
                    handle.flush()
                time.sleep(0.5)
                stop.set()
                thread.join(timeout=3)

            messages = store.get_messages(limit=10)
            sounding = store.get_sounding_data()
        finally:
            os.unlink(tmp_path)

        self.assertEqual(1, fake_bridge.vdl2_calls)
        self.assertEqual(1, len(messages))
        self.assertEqual("vdl2", messages[0]["source"])
        self.assertEqual(1, sounding["observations"])
        self.assertEqual("vdl2", sounding["levels"][0]["source"])


if __name__ == "__main__":
    unittest.main()
