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

    def test_exactly_one_writer_of_the_active_class(self):
        """Two writers is how the light "pulsed then stopped".

        updateQuickScanButtons() decided from state[target].currentProfile,
        which the direct-post shim never sets, so it computed false and
        stripped `active` off a button a live scan had just lit.
        """
        writers = re.findall(r"classList\.toggle\('active'[^)]*\)", self.src)
        btn_writers = [w for w in writers if "isActive" in w]
        self.assertEqual(
            len(btn_writers), 1,
            "expected one writer of `active` on the scan buttons, found %d: %s"
            % (len(btn_writers), btn_writers))

    def test_the_writer_decides_from_the_radio_not_the_profile_system(self):
        m = re.search(r"function updateQuickScanButtons\(\)\s*\{(.*?)\n    \}", self.src, re.S)
        self.assertIsNotNone(m, "updateQuickScanButtons() missing")
        body = m.group(1)
        self.assertIn("scanStateFor(", body,
                      "must read live scan state off the daemon")
        self.assertNotIn("currentProfile", body,
                         "the profile system is bypassed by the shim; it can "
                         "only ever yield false here")
        self.assertRegex(body, r"classList\.toggle\('active'")

    def test_delegating_helper_does_not_also_toggle(self):
        m = re.search(r"function updateBandScanButtonStates\(\)\s*\{(.*?)\n    \}", self.src, re.S)
        self.assertIsNotNone(m, "updateBandScanButtonStates() missing")
        self.assertNotRegex(m.group(1), r"classList\.toggle\(",
                            "it must delegate, not write the class itself")

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

    def test_indicator_is_painted_where_status_actually_arrives(self):
        """It was wired only into refresh(), which the SSE path never calls,
        so it went stale the moment the page settled."""
        sse = re.search(r"es\.addEventListener\('status'.*?\n      \}\);", self.src, re.S)
        self.assertIsNotNone(sse, "SSE status handler missing")
        self.assertIn("renderBandScanStatus()", sse.group(0),
                      "the SSE status handler must repaint the indicator")

    def test_status_payload_fields_are_consumed(self):
        for field in ("airband_scan", "ground_scan"):
            self.assertIn("data.%s" % field, self.src)


if __name__ == "__main__":
    unittest.main()
