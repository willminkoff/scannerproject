import unittest
from unittest import mock
import importlib.util
from pathlib import Path
import tempfile

from ui import digital
from ui import system_stats
from ui import v3_preflight

_ENSURE_DIGITAL_RUNTIME_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ensure-digital-runtime.py"
_ENSURE_DIGITAL_RUNTIME_SPEC = importlib.util.spec_from_file_location(
    "ensure_digital_runtime_test_module",
    _ENSURE_DIGITAL_RUNTIME_PATH,
)
ensure_digital_runtime = importlib.util.module_from_spec(_ENSURE_DIGITAL_RUNTIME_SPEC)
assert _ENSURE_DIGITAL_RUNTIME_SPEC and _ENSURE_DIGITAL_RUNTIME_SPEC.loader
_ENSURE_DIGITAL_RUNTIME_SPEC.loader.exec_module(ensure_digital_runtime)


class DigitalTunerSupportTests(unittest.TestCase):
    def test_expected_serials_include_tertiary(self):
        with mock.patch.object(digital, "DIGITAL_RTL_SERIAL", "56919602"), mock.patch.object(
            digital, "DIGITAL_RTL_SERIAL_SECONDARY", "49571227"
        ), mock.patch.object(
            digital, "DIGITAL_RTL_SERIAL_TERTIARY", "10371955"
        ):
            self.assertEqual(
                ["56919602", "49571227", "10371955"],
                digital._digital_expected_rtl_serials(),
            )

    def test_voice_tuner_serials_include_secondary_and_tertiary(self):
        with mock.patch.object(digital, "DIGITAL_RTL_SERIAL", "56919602"), mock.patch.object(
            digital, "DIGITAL_RTL_SERIAL_SECONDARY", "49571227"
        ), mock.patch.object(
            digital, "DIGITAL_RTL_SERIAL_TERTIARY", "10371955"
        ):
            self.assertEqual(
                ["49571227", "10371955"],
                digital._digital_voice_tuner_serials(),
            )

    def test_auto_extra_digital_tuner_is_adopted_for_expected_and_voice_lists(self):
        dongles = {
            "present_paths": [
                {"serial": "14306619"},
                {"serial": "56919602"},
                {"serial": "70613472"},
                {"serial": "00000002"},
                {"serial": "49571227"},
            ]
        }
        with mock.patch.object(digital, "AIRBAND_RTL_SERIAL", "00000002"), mock.patch.object(
            digital, "GROUND_RTL_SERIAL", "70613472"
        ), mock.patch.object(
            digital, "DIGITAL_RTL_SERIAL", "56919602"
        ), mock.patch.object(
            digital, "DIGITAL_RTL_SERIAL_SECONDARY", "49571227"
        ), mock.patch.object(
            digital, "DIGITAL_RTL_SERIAL_TERTIARY", ""
        ), mock.patch.object(
            digital, "_DIGITAL_AUTO_ADOPT_EXTRA_TUNERS", True
        ):
            self.assertEqual(
                ["56919602", "49571227", "14306619"],
                digital._digital_expected_rtl_serials(dongles=dongles),
            )
            self.assertEqual(
                ["49571227", "14306619"],
                digital._digital_voice_tuner_serials(dongles=dongles),
            )

    def test_voice_tuner_serials_filter_missing_and_busy(self):
        with mock.patch.object(digital, "DIGITAL_RTL_SERIAL", "56919602"), mock.patch.object(
            digital, "DIGITAL_RTL_SERIAL_SECONDARY", "49571227"
        ), mock.patch.object(
            digital, "DIGITAL_RTL_SERIAL_TERTIARY", "10371955"
        ):
            self.assertEqual(
                ["10371955"],
                digital._digital_voice_tuner_serials(missing_serials={"49571227"}),
            )
            self.assertEqual([], digital._digital_voice_tuner_serials(tuner_busy=True))


class SystemStatsExpectedSerialTests(unittest.TestCase):
    def test_expected_rtl_serials_include_tertiary_alias(self):
        env = {
            "DIGITAL_RTL_SERIAL": "56919602",
            "DIGITAL_RTL_SERIAL_2": "49571227",
            "DIGITAL_RTL_SERIAL_3": "10371955",
        }
        with mock.patch.dict("os.environ", env, clear=False):
            self.assertEqual(
                ["56919602", "49571227", "10371955"],
                system_stats._expected_rtl_serials(),
            )


class EnsureDigitalRuntimeAutoExtraTests(unittest.TestCase):
    def test_discover_tuner_uid_state_adopts_extra_non_analog_tuner(self):
        serial_map = {
            "14306619": "RTL-2832 USB Bus:1 Port:6.1.1",
            "56919602": "RTL-2832 USB Bus:1 Port:6.1.2",
            "70613472": "RTL-2832 USB Bus:1 Port:6.1.3",
            "00000002": "RTL-2832 USB Bus:1 Port:6.3",
            "49571227": "RTL-2832 USB Bus:1 Port:6.4",
        }
        with mock.patch.object(ensure_digital_runtime, "AIRBAND_RTL_SERIAL", "00000002"), mock.patch.object(
            ensure_digital_runtime, "GROUND_RTL_SERIAL", "70613472"
        ), mock.patch.object(
            ensure_digital_runtime, "DIGITAL_RTL_SERIAL", "56919602"
        ), mock.patch.object(
            ensure_digital_runtime, "DIGITAL_RTL_SERIAL_SECONDARY", "49571227"
        ), mock.patch.object(
            ensure_digital_runtime, "DIGITAL_RTL_SERIAL_TERTIARY", ""
        ), mock.patch.object(
            ensure_digital_runtime, "DIGITAL_AUTO_ADOPT_EXTRA_TUNERS", True
        ), mock.patch.object(
            ensure_digital_runtime, "_discover_rtl_unique_ids_by_serial", return_value=serial_map
        ):
            payload = ensure_digital_runtime._discover_tuner_uid_state()

        self.assertEqual(
            [
                "RTL-2832 USB Bus:1 Port:6.1.1",
                "RTL-2832 USB Bus:1 Port:6.1.2",
                "RTL-2832 USB Bus:1 Port:6.4",
            ],
            payload["digital_uids"],
        )
        self.assertEqual(["14306619"], payload["digital_auto_extra_serials"])
        self.assertEqual("configured_plus_auto_extra", payload["digital_uid_source"])


class EnsureDigitalRuntimeLocalAudioPreferenceTests(unittest.TestCase):
    def test_detect_preferred_local_audio_mixer_prefers_hdmi_over_analog(self):
        cards_text = """
 0 [PCH            ]: HDA-Intel - HDA Intel PCH
"""
        pcm_text = """
00-00: ALC3204 Analog : ALC3204 Analog : playback 1 : capture 1
00-07: HDMI 1 : HDMI 1 : playback 1
"""
        self.assertEqual(
            "PCH [plughw:0,7] - STEREO",
            ensure_digital_runtime._detect_preferred_local_audio_mixer(cards_text, pcm_text),
        )

    def test_detect_preferred_local_audio_mixer_returns_empty_without_playback(self):
        cards_text = """
 0 [PCH            ]: HDA-Intel - HDA Intel PCH
"""
        pcm_text = """
00-00: ALC3204 Analog : ALC3204 Analog : capture 1
"""
        self.assertEqual(
            "",
            ensure_digital_runtime._detect_preferred_local_audio_mixer(cards_text, pcm_text),
        )

    def test_write_java_pref_entries_preserves_java_prefs_doctype(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "prefs.xml"
            ensure_digital_runtime._write_java_pref_entries(
                target,
                {
                    "audio.playback.mixer.channel.configuration": "PCH [plughw:0,7] - STEREO",
                },
            )
            text = target.read_text(encoding="utf-8")
        self.assertIn('<!DOCTYPE map SYSTEM "http://java.sun.com/dtd/preferences.dtd">', text)
        self.assertIn('key="audio.playback.mixer.channel.configuration"', text)
        self.assertIn('value="PCH [plughw:0,7] - STEREO"', text)


class DigitalPreflightTertiaryTests(unittest.TestCase):
    def test_preflight_flags_tertiary_missing_and_uses_dynamic_dongle_count(self):
        dongles = {
            "status": "critical",
            "expected_count": 5,
            "expected_serials": ["00000002", "70613472", "56919602", "49571227", "10371955"],
            "missing_expected_serials": ["10371955"],
            "slow_expected_serials": [],
        }
        with mock.patch.object(v3_preflight, "DIGITAL_PREFERRED_TUNER", ""), mock.patch.object(
            v3_preflight, "DIGITAL_RTL_DEVICE", ""
        ), mock.patch.object(
            v3_preflight, "DIGITAL_RTL_SERIAL", "56919602"
        ), mock.patch.object(
            v3_preflight, "DIGITAL_RTL_SERIAL_SECONDARY", "49571227"
        ), mock.patch.object(
            v3_preflight, "DIGITAL_RTL_SERIAL_TERTIARY", "10371955"
        ):
            payload = v3_preflight.evaluate_digital_preflight(
                profile_id="",
                strict=False,
                dongles=dongles,
                compile_state={},
                manager_preflight={},
            )

        reasons = {item["code"]: item for item in payload["reasons"]}
        self.assertIn("DONGLE_CRITICAL", reasons)
        self.assertIn("5 configured role-bound dongles", reasons["DONGLE_CRITICAL"]["hint"])
        self.assertIn("DIGITAL_TERTIARY_SERIAL_MISSING", reasons)


if __name__ == "__main__":
    unittest.main()
