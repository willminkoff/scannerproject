"""Structural tests for the SB3 Travel Mode button rewire (PR #36).

The button on the main SB3 display used to be a toggle that flipped
travel_mode_enabled via /api/hp/travel_mode/toggle. PR #36 replaced that
with a one-tap "Refresh Location" action that mirrors the sidecar's
"Use Current Location + Apply" button — clicking it calls
autoLocateHpFiltersPanel() directly so the two surfaces stay in lock-step.

These tests lock down the wiring as text-pattern assertions over the
HTML. They're a structural guardrail, not a DOM-event simulation: the
JavaScript runs in a real browser, but if any of these patterns break
(button renamed, handler unbound, sidecar function renamed) the rewire
is gone and we want to catch that.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_SB3_PATH = _ROOT / "ui" / "sb3.html"


class Sb3TravelModeButtonGuardrailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = _SB3_PATH.read_text(encoding="utf-8")

    # ---- Button HTML ------------------------------------------------------

    def test_button_label_is_refresh_location(self):
        # The visible label is the action it performs, not a toggle state.
        match = re.search(
            r'<button[^>]*id="btn-travel-mode"[^>]*>\s*([^<]+)\s*</button>',
            self.text,
        )
        self.assertIsNotNone(match, "btn-travel-mode markup not found")
        label = match.group(1).strip()
        self.assertIn("Refresh Location", label)
        self.assertNotIn("Travel: ON", self.text.split("</body>")[0])
        self.assertNotIn("Travel: OFF", self.text.split("</body>")[0])

    def test_button_starts_in_ready_state(self):
        # Initial data-travel must be 'ready', not the old 'off'.
        self.assertRegex(
            self.text,
            r'<button[^>]*id="btn-travel-mode"[^>]*data-travel="ready"',
        )

    def test_button_no_longer_has_aria_pressed(self):
        # aria-pressed is a toggle pattern; the button isn't a toggle anymore.
        button_block = re.search(
            r'<button[^>]*id="btn-travel-mode"[^>]*>',
            self.text,
        ).group(0)
        self.assertNotIn("aria-pressed", button_block)

    def test_title_attribute_describes_refresh_action(self):
        button_block = re.search(
            r'<button[^>]*id="btn-travel-mode"[^>]*>',
            self.text,
        ).group(0)
        self.assertIn("Refresh scanner location", button_block)

    # ---- CSS keyed on the new attribute values ----------------------------

    def test_css_keys_on_ready_and_busy(self):
        self.assertIn('.travel-mode-btn[data-travel="ready"]', self.text)
        self.assertIn('.travel-mode-btn[data-travel="busy"]', self.text)

    def test_css_no_longer_keys_on_on_off(self):
        self.assertNotIn('.travel-mode-btn[data-travel="off"]', self.text)
        self.assertNotIn('.travel-mode-btn[data-travel="on"]', self.text)

    # ---- JS handler binding ------------------------------------------------

    def test_setup_binds_fetch_handler_not_toggle(self):
        # setupTravelMode() must register the new handler.
        self.assertRegex(
            self.text,
            r"btn\.addEventListener\(\s*'click'\s*,\s*fetchAndPushLocation\s*\)",
        )
        # The old toggle handler must not still be bound.
        self.assertNotIn("addEventListener('click', toggleTravelMode)", self.text)

    def test_fetch_handler_calls_autolocate(self):
        # The new handler MUST call autoLocateHpFiltersPanel — that's the
        # whole point: lock-step with the sidecar button.
        fn_match = re.search(
            r"async function fetchAndPushLocation\(\)\s*\{(.*?)\n    \}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(fn_match, "fetchAndPushLocation function not found")
        body = fn_match.group(1)
        self.assertIn("autoLocateHpFiltersPanel()", body)

    def test_old_toggle_handler_is_gone(self):
        # The toggleTravelMode function must be removed (not just unbound).
        self.assertNotIn("async function toggleTravelMode(", self.text)
        # And the toggle endpoint must not be hit from sb3.html anymore.
        self.assertNotIn("/api/hp/travel_mode/toggle", self.text)

    def test_no_confirm_modal_on_click(self):
        # Will explicitly wanted one-tap — no window.confirm gating.
        fn_match = re.search(
            r"async function fetchAndPushLocation\(\)\s*\{(.*?)\n    \}",
            self.text,
            re.DOTALL,
        )
        self.assertNotIn("window.confirm", fn_match.group(1))

    # ---- Sidecar function we rely on still exists ------------------------

    def test_sidecar_autolocate_function_present(self):
        # If the sidecar ever renames or removes autoLocateHpFiltersPanel
        # this guardrail catches the rewire becoming a dangling reference.
        self.assertIn("async function autoLocateHpFiltersPanel(", self.text)

    def test_sidecar_chain_endpoints_present(self):
        # The sidecar pipeline hits these endpoints; assert they're still
        # the ones the rewire delegates to.
        self.assertIn("/api/hp/location/ip", self.text)
        self.assertIn("/api/hp/location/reverse", self.text)
        self.assertIn("/api/scan/state", self.text)

    # ---- Render helper updates --------------------------------------------

    def test_render_no_longer_writes_button_text(self):
        # renderTravelMode used to overwrite the button label from the
        # backend's travel_mode_enabled flag; under the new model the label
        # is static ("📍 Refresh Location") and renderTravelMode only
        # updates the ZIP + last-push meta.
        render_match = re.search(
            r"function renderTravelMode\(payload\)\s*\{(.*?)\n    \}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(render_match, "renderTravelMode not found")
        body = render_match.group(1)
        self.assertNotIn("btn.textContent =", body)
        self.assertNotIn("data-travel'", body)  # no setAttribute on the button
        # Still updates the meta block.
        self.assertIn("zipEl.textContent", body)
        self.assertIn("lastEl", body)


if __name__ == "__main__":
    unittest.main()
