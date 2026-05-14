"""Phase 4 — C4 tests for interpret.py band-plan prompt augmentation.

Verifies the call_claude() prompt incorporates the new band_name / ml_class /
band_rejected fields correctly:
  - In-band detections render the "agreement" band block, no anomaly clause.
  - Out-of-band (band-plan rejected) detections render the "REJECTED" block
    AND the explicit anomaly instructions.
  - Frequencies outside the band-plan's covered range render the
    "permissive default" block.

Tests use unittest.mock to capture the request body sent to the Anthropic API
and inspect the constructed prompt without making a real network call.
"""
from __future__ import annotations

import json
import unittest
import unittest.mock as mock

from disco.src import interpret


def _base_bundle(**overrides):
    """Bundle skeleton with sensible defaults for an aviation-area detection."""
    bundle = {
        "freq_mhz": 116.9801,
        "modulation_class": "NXDN",
        "ml_class": "NXDN",
        "confidence": 0.85,
        "bandwidth_khz": 8.0,
        "snr_db": 25.0,
        "tuner": "A-T1",
        "protocol_tag": "AVIATION_NAV — unidentified (model said: NXDN)",
        "band_name": "AVIATION_NAV",
        "band_allowed_modes": ["AM_VOICE"],
        "band_rejected": True,
        "uls_callsign": None,
        "uls_entity_name": None,
        "uls_emission_designator": None,
        "uls_station_class": None,
        "uls_distance_km": None,
        "uls_source": None,
    }
    bundle.update(overrides)
    return bundle


def _capture_prompt(mock_urlopen, api_key, bundle, model="test-model"):
    """Run call_claude under a mocked urlopen and return the prompt string
    that was sent in the request body."""
    mock_response = mock.MagicMock()
    mock_response.read.return_value = json.dumps(
        {"content": [{"text": "stub interpretation"}]}
    ).encode()
    mock_urlopen.return_value.__enter__.return_value = mock_response

    interpret.call_claude(api_key, bundle, model)

    sent_request = mock_urlopen.call_args.args[0]
    body = json.loads(sent_request.data)
    return body["messages"][0]["content"]


class CallClaudePromptBandRejectedTests(unittest.TestCase):
    """The canonical NXDN-at-116.98 case — band_rejected=True."""

    @mock.patch("urllib.request.urlopen")
    def test_rejected_block_present(self, mock_urlopen):
        prompt = _capture_prompt(mock_urlopen, "FAKE_KEY", _base_bundle())
        self.assertIn("FCC band-plan check for this frequency", prompt)
        self.assertIn("Allocation: AVIATION_NAV", prompt)
        self.assertIn("Allowed modes per FCC table: AM_VOICE", prompt)
        self.assertIn("ML model output: NXDN — NOT in allowed_modes", prompt)
        self.assertIn("REJECTED", prompt)

    @mock.patch("urllib.request.urlopen")
    def test_anomaly_instructions_present(self, mock_urlopen):
        prompt = _capture_prompt(mock_urlopen, "FAKE_KEY", _base_bundle())
        self.assertIn("Band-plan anomaly", prompt)
        self.assertIn("misidentification", prompt)
        self.assertIn("spurious emission", prompt)
        self.assertIn("Do not write prose that assumes the ML class is correct", prompt)

    @mock.patch("urllib.request.urlopen")
    def test_ml_class_preserved_in_prompt(self, mock_urlopen):
        prompt = _capture_prompt(mock_urlopen, "FAKE_KEY", _base_bundle())
        # Raw ML output should be visible separately from band-plan tag,
        # so Claude can reason about model misidentification.
        self.assertIn("Modulation class (from local CNN classifier): NXDN", prompt)
        self.assertIn("Band-plan tag: AVIATION_NAV — unidentified (model said: NXDN)", prompt)


class CallClaudePromptBandAcceptedTests(unittest.TestCase):
    """In-band match (band_rejected=False) — agreement block, no anomaly clause."""

    @mock.patch("urllib.request.urlopen")
    def test_agreement_block_present(self, mock_urlopen):
        bundle = _base_bundle(
            freq_mhz=851.55,
            modulation_class="P25",
            ml_class="P25",
            band_name="PS_800_NARROW",
            band_allowed_modes=["P25", "FM_NARROW"],
            band_rejected=False,
            protocol_tag="PS_800_NARROW — P25",
        )
        prompt = _capture_prompt(mock_urlopen, "FAKE_KEY", bundle)
        self.assertIn("FCC band-plan agreement for this frequency", prompt)
        self.assertIn("Allocation: PS_800_NARROW", prompt)
        self.assertIn("ML model output: P25 (in allowed_modes for this band)", prompt)

    @mock.patch("urllib.request.urlopen")
    def test_no_anomaly_clause_when_in_band(self, mock_urlopen):
        bundle = _base_bundle(
            freq_mhz=851.55, modulation_class="P25", ml_class="P25",
            band_name="PS_800_NARROW", band_allowed_modes=["P25", "FM_NARROW"],
            band_rejected=False, protocol_tag="PS_800_NARROW — P25",
        )
        prompt = _capture_prompt(mock_urlopen, "FAKE_KEY", bundle)
        self.assertNotIn("Band-plan anomaly", prompt)
        self.assertNotIn("REJECTED", prompt)


class CallClaudePromptOutsideBandPlanTests(unittest.TestCase):
    """Frequencies outside the band-plan's covered range — permissive default."""

    @mock.patch("urllib.request.urlopen")
    def test_permissive_block_when_no_band(self, mock_urlopen):
        bundle = _base_bundle(
            freq_mhz=7.2,
            modulation_class="FM_NARROW",
            ml_class="FM_NARROW",
            band_name=None,
            band_allowed_modes=[],
            band_rejected=False,
            protocol_tag="FM_NARROW",
        )
        prompt = _capture_prompt(mock_urlopen, "FAKE_KEY", bundle)
        self.assertIn("outside the band-plan's covered range", prompt)
        self.assertIn("permissive default", prompt)
        self.assertNotIn("Band-plan anomaly", prompt)
        self.assertNotIn("REJECTED", prompt)


class CallClaudePromptBackwardsCompatTests(unittest.TestCase):
    """Bundles built without band-plan fields (e.g. from a pre-C4 cache row)
    must still produce a valid prompt — the band block is just omitted."""

    @mock.patch("urllib.request.urlopen")
    def test_bundle_without_band_fields(self, mock_urlopen):
        bundle = {
            "freq_mhz": 162.475,
            "modulation_class": "FM_NARROW",
            "confidence": 0.92,
            "bandwidth_khz": 25.0,
            "snr_db": 30.0,
            "tuner": "A-T1",
            "protocol_tag": "NOAA WX",
        }
        prompt = _capture_prompt(mock_urlopen, "FAKE_KEY", bundle)
        # ml_class falls back to modulation_class
        self.assertIn("Modulation class (from local CNN classifier): FM_NARROW", prompt)
        # No band block since band_name is missing — but the permissive default
        # block fires because ml_class IS present (FM_NARROW falls through the elif)
        self.assertIn("permissive default", prompt)


class CallClaudeNoApiKeyTests(unittest.TestCase):
    """Sanity: with no api_key, call_claude returns the stub string without
    making a network call."""

    @mock.patch("urllib.request.urlopen")
    def test_no_key_returns_stub(self, mock_urlopen):
        result = interpret.call_claude("", _base_bundle(), "model")
        self.assertEqual(result, "no key configured")
        mock_urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
