#!/usr/bin/env python3
"""
Static regression tests for the Philly tab in sb3.html.
Parses the HTML/JS without a browser — checks for required IDs, CSS classes,
and JS symbols. No external dependencies.

Run: python3 test_sb3_philly_tab.py [path/to/sb3.html]
"""

import re
import sys
import unittest

SB3_PATH = sys.argv[1] if len(sys.argv) > 1 else '../../../scannerproject/ui/sb3.html'

try:
    with open(SB3_PATH, encoding='utf-8') as f:
        SB3 = f.read()
except FileNotFoundError:
    print(f'ERROR: sb3.html not found at {SB3_PATH}', file=sys.stderr)
    sys.exit(1)


def _has_id(id_):
    return f'id="{id_}"' in SB3 or f"id='{id_}'" in SB3

def _has_css(selector):
    return selector in SB3

def _has_js(symbol):
    return symbol in SB3


class TestPhillyTabHtml(unittest.TestCase):

    def test_tab_button_present(self):
        self.assertTrue(_has_id('tab-philly'), 'Missing id="tab-philly"')

    def test_controls_div_present(self):
        self.assertTrue(_has_id('controls-philly'), 'Missing id="controls-philly"')

    def test_audio_player_present(self):
        self.assertTrue(_has_id('audio-player-philly'), 'Missing id="audio-player-philly"')

    def test_icecast_dot_present(self):
        self.assertTrue(_has_id('philly-icecast-dot'), 'Missing id="philly-icecast-dot"')

    def test_service_status_present(self):
        self.assertTrue(_has_id('philly-service-status'), 'Missing id="philly-service-status"')

    def test_profile_select_present(self):
        self.assertTrue(_has_id('philly-profile-select'), 'Missing id="philly-profile-select"')

    def test_freq_list_present(self):
        self.assertTrue(_has_id('philly-freq-list'), 'Missing id="philly-freq-list"')

    def test_add_freq_form_present(self):
        self.assertTrue(_has_id('philly-add-freq-form'), 'Missing id="philly-add-freq-form"')

    def test_squelch_am_slider_present(self):
        self.assertTrue(_has_id('philly-sq-am-slider'), 'Missing id="philly-sq-am-slider"')

    def test_squelch_nfm_slider_present(self):
        self.assertTrue(_has_id('philly-sq-nfm-slider'), 'Missing id="philly-sq-nfm-slider"')

    def test_activate_button_present(self):
        self.assertTrue(_has_id('btn-philly-activate'), 'Missing id="btn-philly-activate"')

    def test_restart_button_present(self):
        self.assertTrue(_has_id('btn-philly-restart'), 'Missing id="btn-philly-restart"')

    def test_add_freq_button_present(self):
        self.assertTrue(_has_id('btn-philly-add-freq'), 'Missing id="btn-philly-add-freq"')

    def test_new_profile_button_present(self):
        self.assertTrue(_has_id('btn-philly-new-profile'), 'Missing id="btn-philly-new-profile"')

    def test_del_profile_button_present(self):
        self.assertTrue(_has_id('btn-philly-del-profile'), 'Missing id="btn-philly-del-profile"')

    def test_new_freq_hz_input_present(self):
        self.assertTrue(_has_id('philly-new-freq-hz'), 'Missing id="philly-new-freq-hz"')

    def test_new_freq_label_input_present(self):
        self.assertTrue(_has_id('philly-new-freq-label'), 'Missing id="philly-new-freq-label"')

    def test_new_freq_mode_select_present(self):
        self.assertTrue(_has_id('philly-new-freq-mode'), 'Missing id="philly-new-freq-mode"')

    def test_stream_link_present(self):
        self.assertTrue(_has_id('lnk-stream-philly'), 'Missing id="lnk-stream-philly"')

    def test_stream_link_points_to_pi(self):
        m = re.search(r'id="lnk-stream-philly"[^>]*href="([^"]+)"', SB3)
        if not m:
            m = re.search(r'href="([^"]+)"[^>]*id="lnk-stream-philly"', SB3)
        self.assertIsNotNone(m, 'lnk-stream-philly href not found')
        self.assertIn('100.69.95.13', m.group(1))


class TestPhillyTabCss(unittest.TestCase):

    def test_philly_dot_class_defined(self):
        self.assertTrue(_has_css('.philly-dot {'), 'Missing .philly-dot CSS')

    def test_philly_dot_active_class_defined(self):
        self.assertTrue(_has_css('.philly-dot.active {'), 'Missing .philly-dot.active CSS')

    def test_philly_freq_row_class_defined(self):
        self.assertTrue(_has_css('.philly-freq-row {'), 'Missing .philly-freq-row CSS')

    def test_hit_src_phi_class_defined(self):
        self.assertTrue(_has_css('.hit-src-phi {'), 'Missing .hit-src-phi CSS')


class TestPhillyTabJs(unittest.TestCase):

    def test_philly_pi_module_defined(self):
        self.assertTrue(_has_js('const PhillyPi ='), 'Missing PhillyPi module')

    def test_tab_philly_in_els(self):
        self.assertTrue(_has_js("tabPhilly: document.getElementById('tab-philly')"),
                        'tabPhilly missing from els')

    def test_controls_philly_in_els(self):
        self.assertTrue(_has_js("controlsPhilly: document.getElementById('controls-philly')"),
                        'controlsPhilly missing from els')

    def test_switch_target_handles_philly(self):
        self.assertTrue(_has_js("target === 'philly'"), "switchTarget doesn't handle 'philly'")

    def test_on_tab_open_called(self):
        self.assertTrue(_has_js('PhillyPi.onTabOpen()'), 'PhillyPi.onTabOpen() not called in switchTarget')

    def test_philly_init_called(self):
        self.assertTrue(_has_js('PhillyPi.init()'), 'PhillyPi.init() not called')

    def test_phi_source_detection(self):
        self.assertTrue(_has_js("sourceName === 'philly') return 'PHI'"),
                        "updateHitList doesn't detect philly source → PHI")

    def test_phi_css_class_applied(self):
        self.assertTrue(_has_js("source === 'PHI' ? ' hit-src-phi' : ''"),
                        'hit-src-phi class not applied to PHI hits')

    def test_pi_base_url(self):
        self.assertTrue(_has_js("'http://100.69.95.13:5050'"),
                        'PI_BASE URL not found in PhillyPi module')

    def test_poll_status_function(self):
        self.assertTrue(_has_js('async function pollStatus()'), 'pollStatus() missing')

    def test_poll_hits_function(self):
        self.assertTrue(_has_js('async function pollHits()'), 'pollHits() missing')

    def test_activate_profile_function(self):
        self.assertTrue(_has_js('async function activateProfile()'), 'activateProfile() missing')

    def test_add_freq_function(self):
        self.assertTrue(_has_js('async function addFreq()'), 'addFreq() missing')

    def test_remove_freq_function(self):
        self.assertTrue(_has_js('async function removeFreq('), 'removeFreq() missing')

    def test_apply_squelch_function(self):
        self.assertTrue(_has_js('async function applySquelch()'), 'applySquelch() missing')

    def test_restart_scanner_function(self):
        self.assertTrue(_has_js('async function restartScanner()'), 'restartScanner() missing')

    def test_load_profiles_function(self):
        self.assertTrue(_has_js('async function loadProfiles()'), 'loadProfiles() missing')

    def test_hits_injected_with_philly_source(self):
        self.assertTrue(_has_js("source: 'philly'"), "Hits not injected with source: 'philly'")

    def test_tab_philly_onclick_wired(self):
        self.assertTrue(_has_js("switchTarget('philly')"), "tab-philly onclick not wired")


if __name__ == '__main__':
    # Strip the path arg so unittest doesn't try to use it
    if len(sys.argv) > 1 and not sys.argv[1].startswith('-'):
        sys.argv.pop(1)
    unittest.main(verbosity=2)
