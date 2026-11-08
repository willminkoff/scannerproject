"""Static guards on the band-scan indicator in ui/sb3.html.

Two bugs shipped in the first cut, both invisible to any Python test and to a
JS syntax check, because both were *wrong references* rather than bad syntax:

  1. It read ``state.activeTarget``. The page keeps the active tab in a bare
     module-level ``let activeTarget``, so that expression was always
     undefined and the indicator fell through to 'airband' forever -- the
     Ground tab showed airband's channels and centre frequency.

  2. It toggled a class ``is-active`` that it invented. The page already has
     ``.quick-scan-action.active``, which lights the ::after dot and runs the
     heartbeat animation, so the button's indicator light never came on.

These are grep-level assertions on purpose: there is no JS test harness in
this repo, and both regressions are visible as a single wrong token.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

_HTML = Path(__file__).resolve().parents[1] / "ui" / "sb3.html"


class ActiveTargetTests(unittest.TestCase):
    def setUp(self):
        self.src = _HTML.read_text(encoding="utf-8")

    def test_active_tab_is_a_bare_variable(self):
        """If this ever becomes state.activeTarget, the guard below must go."""
        self.assertRegex(self.src, r"\blet activeTarget\b")

    def test_nothing_reads_state_activetarget(self):
        hits = [m.start() for m in re.finditer(r"state\.activeTarget\b", self.src)]
        self.assertEqual(
            hits, [],
            "state.activeTarget is always undefined -- the tab lives in a bare "
            "`let activeTarget`. %d reference(s) found." % len(hits))

    def test_indicator_reads_the_bare_variable(self):
        m = re.search(r"function currentScanState\(\)\s*\{(.*?)\n    \}", self.src, re.S)
        self.assertIsNotNone(m, "currentScanState() missing")
        self.assertRegex(m.group(1), r"\(activeTarget === 'ground'\)")


class ButtonLightTests(unittest.TestCase):
    def setUp(self):
        self.src = _HTML.read_text(encoding="utf-8")

    def test_the_lit_dot_style_exists(self):
        """The style the button toggle has to match."""
        self.assertIn(".quick-scan-action.active::after", self.src)

    def test_button_toggle_uses_that_class(self):
        m = re.search(r"function updateBandScanButtonStates\(\)\s*\{(.*?)\n    \}", self.src, re.S)
        self.assertIsNotNone(m, "updateBandScanButtonStates() missing")
        body = m.group(1)
        self.assertRegex(body, r"classList\.toggle\('active'",
                         "must toggle 'active' -- the class that lights the dot")
        self.assertNotRegex(body, r"classList\.toggle\('is-active'",
                            "'is-active' is styled by nothing")

    def test_no_dead_is_active_rule_remains(self):
        self.assertNotRegex(self.src, r"\.quick-scan-action\.is-active\s*\{")


class IndicatorWiringTests(unittest.TestCase):
    def setUp(self):
        self.src = _HTML.read_text(encoding="utf-8")

    def test_status_element_exists(self):
        for el in ("band-scan-status", "band-scan-status-text", "band-scan-dot"):
            self.assertIn('id="%s"' % el, self.src)

    def test_renderer_runs_on_status_and_on_tab_change(self):
        self.assertGreaterEqual(
            self.src.count("renderBandScanStatus()"), 3,
            "expected the definition plus a status-poll and a tab-change call")

    def test_status_payload_fields_are_consumed(self):
        for field in ("airband_scan", "ground_scan"):
            self.assertIn("data.%s" % field, self.src)


if __name__ == "__main__":
    unittest.main()
