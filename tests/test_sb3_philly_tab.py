#!/usr/bin/env python3
"""Static regression tests for WeatherDash + Philly elements in sb3.html."""

from __future__ import annotations

from pathlib import Path
import unittest


SB3 = Path(__file__).resolve().parents[1] / "ui" / "sb3.html"
TEXT = SB3.read_text(encoding="utf-8")


def _has(fragment: str) -> bool:
    return fragment in TEXT


class WeatherDashButtonTests(unittest.TestCase):
    def test_weather_dash_button_present(self):
        self.assertTrue(_has('id="btn-weather-dash"'))

    def test_weather_dash_button_targets_will_rog(self):
        self.assertTrue(_has("window.open('http://will-rog:5051/'"))


class PhillyTabTests(unittest.TestCase):
    def test_tab_button_present(self):
        self.assertTrue(_has('id="tab-philly"'))

    def test_controls_panel_present(self):
        self.assertTrue(_has('id="controls-philly"'))

    def test_stream_player_present(self):
        self.assertTrue(_has('id="audio-player-philly"'))

    def test_profile_select_present(self):
        self.assertTrue(_has('id="philly-profile-select"'))

    def test_module_present(self):
        self.assertTrue(_has("const PhillyPi = (() => {"))

    def test_switch_target_handles_philly(self):
        self.assertTrue(_has("target === 'philly'"))
        self.assertTrue(_has("PhillyPi.onTabOpen()"))

    def test_hits_marked_as_phi(self):
        self.assertTrue(_has("sourceName === 'philly') return 'PHI'"))
        self.assertTrue(_has("source === 'PHI' ? ' hit-src-phi' : ''"))


if __name__ == "__main__":
    unittest.main()
