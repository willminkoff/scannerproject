"""Structural tests for the SB3 Travel Mode button (PR #37 spec).

Click behavior: one-tap location refresh. The button calls
`resolveCurrentLocationForHpFilters` / `resolveLocationDetailsForHpFilters`
(same helpers the sidecar uses), transparently turns Travel Mode ON via
`/api/hp/travel_mode/toggle`, then POSTs through `/api/hp/location/push`
so the receipt log captures the push. The button's ON/OFF visual is
driven by push freshness — "Travel: ON" when the last push receipt is
within ~60 min, "Travel: OFF" otherwise. A success ✓ or error ✗ flashes
briefly after each click before the button settles back to the
freshness-driven state.

These are text-pattern guardrails over `ui/sb3.html`; the JavaScript
runs in a real browser. The point is to catch regressions in the wiring
without shipping a JS test runner.
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

    def test_button_initial_label_is_off(self):
        match = re.search(
            r'<button[^>]*id="btn-travel-mode"[^>]*>\s*([^<]+)\s*</button>',
            self.text,
        )
        self.assertIsNotNone(match, "btn-travel-mode markup not found")
        self.assertEqual("Travel: OFF", match.group(1).strip())

    def test_button_default_state_is_off(self):
        # PR #37: ON/OFF visual returns, shifted to "fresh push within window".
        self.assertRegex(
            self.text,
            r'<button[^>]*id="btn-travel-mode"[^>]*data-travel="off"',
        )

    def test_button_no_aria_pressed(self):
        # Still not a toggle in the aria-pressed sense — visual state
        # reflects PUSH FRESHNESS, not a boolean toggle. Keep aria-pressed
        # off.
        button_block = re.search(
            r'<button[^>]*id="btn-travel-mode"[^>]*>',
            self.text,
        ).group(0)
        self.assertNotIn("aria-pressed", button_block)

    def test_title_describes_new_semantics(self):
        button_block = re.search(
            r'<button[^>]*id="btn-travel-mode"[^>]*>',
            self.text,
        ).group(0)
        self.assertIn("refresh", button_block.lower())
        self.assertRegex(button_block, r"ON\s*=")
        self.assertRegex(button_block, r"OFF\s*=")

    # ---- CSS for the new visual states -----------------------------------

    def test_css_keys_on_freshness_and_flash_states(self):
        self.assertIn('.travel-mode-btn[data-travel="on"]', self.text)
        self.assertIn('.travel-mode-btn[data-travel="off"]', self.text)
        self.assertIn('.travel-mode-btn[data-travel="busy"]', self.text)
        self.assertIn('.travel-mode-btn[data-travel="success"]', self.text)
        self.assertIn('.travel-mode-btn[data-travel="error"]', self.text)

    def test_success_uses_good_color(self):
        css_block = re.search(
            r'\.travel-mode-btn\[data-travel="success"\]\s*\{([^}]+)\}',
            self.text,
        )
        self.assertIsNotNone(css_block)
        self.assertIn("--good", css_block.group(1))

    def test_error_uses_bad_color(self):
        css_block = re.search(
            r'\.travel-mode-btn\[data-travel="error"\]\s*\{([^}]+)\}',
            self.text,
        )
        self.assertIsNotNone(css_block)
        self.assertIn("--bad", css_block.group(1))

    # ---- JS click chain ---------------------------------------------------

    def test_setup_binds_fetch_handler(self):
        self.assertRegex(
            self.text,
            r"btn\.addEventListener\(\s*'click'\s*,\s*fetchAndPushLocation\s*\)",
        )

    def test_click_calls_sidecar_geolocation_helpers(self):
        # Reuse, not reimplement: the GPS + IP-fallback + reverse-geocode
        # helpers from the sidecar's "Use Current Location + Apply" flow.
        fn_match = re.search(
            r"async function fetchAndPushLocation\(\)\s*\{(.*?)\n    \}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(fn_match, "fetchAndPushLocation not found")
        body = fn_match.group(1)
        self.assertIn("resolveCurrentLocationForHpFilters()", body)
        self.assertIn("resolveLocationDetailsForHpFilters(", body)

    def test_click_enables_travel_mode_and_pushes(self):
        # The handler must hit BOTH the toggle endpoint (transparent enable)
        # AND the gated push endpoint so the receipt log captures the click.
        fn_match = re.search(
            r"async function fetchAndPushLocation\(\)\s*\{(.*?)\n    \}",
            self.text,
            re.DOTALL,
        )
        body = fn_match.group(1)
        self.assertIn("/api/hp/travel_mode/toggle", body)
        self.assertIn("/api/hp/location/push", body)
        # And it tags the push so the SB3-button source is distinguishable
        # in the receipt log.
        self.assertIn("'sb3_button'", body)

    def test_click_flashes_success_or_error(self):
        fn_match = re.search(
            r"async function fetchAndPushLocation\(\)\s*\{(.*?)\n    \}",
            self.text,
            re.DOTALL,
        )
        body = fn_match.group(1)
        self.assertIn("'success'", body)
        self.assertIn("'error'", body)
        self.assertIn("TRAVEL_PUSH_FLASH_MS", body)

    def test_click_has_no_modals(self):
        fn_match = re.search(
            r"async function fetchAndPushLocation\(\)\s*\{(.*?)\n    \}",
            self.text,
            re.DOTALL,
        )
        body = fn_match.group(1)
        self.assertNotIn("window.confirm", body)
        # No alert either — the flash is the user-facing signal.
        self.assertNotIn("window.alert", body)

    def test_old_toggle_handler_removed(self):
        self.assertNotIn("async function toggleTravelMode(", self.text)

    # ---- renderTravelMode drives the freshness visual --------------------

    def test_render_writes_button_label_from_freshness(self):
        render_match = re.search(
            r"function renderTravelMode\(payload\)\s*\{(.*?)\n    \}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(render_match)
        body = render_match.group(1)
        self.assertIn("btn.textContent =", body)
        self.assertIn("Travel: ON", body)
        self.assertIn("Travel: OFF", body)
        self.assertIn("TRAVEL_PUSH_FRESH_WINDOW_MS", body)

    def test_render_does_not_overwrite_button_during_flash(self):
        # Concurrency guardrail — if a polling refresh fires during the
        # success/error flash window, it must NOT clobber the flash.
        render_match = re.search(
            r"function renderTravelMode\(payload\)\s*\{(.*?)\n    \}",
            self.text,
            re.DOTALL,
        )
        body = render_match.group(1)
        self.assertIn("'busy'", body)
        self.assertIn("'success'", body)
        self.assertIn("'error'", body)
        self.assertIn("inFlight", body)

    # ---- Sidecar wiring we still rely on ---------------------------------

    def test_sidecar_helpers_still_defined(self):
        self.assertIn("async function resolveCurrentLocationForHpFilters(", self.text)
        self.assertIn("async function resolveLocationDetailsForHpFilters(", self.text)

    # ---- Owntracks adapter remains dormant -------------------------------

    def test_owntracks_endpoint_not_called_from_button(self):
        # PR #37 keeps the Owntracks endpoint in code (dormant) but the
        # button doesn't drive it — that's the iOS app's responsibility.
        fn_match = re.search(
            r"async function fetchAndPushLocation\(\)\s*\{(.*?)\n    \}",
            self.text,
            re.DOTALL,
        )
        self.assertNotIn("/api/hp/owntracks", fn_match.group(1))


if __name__ == "__main__":
    unittest.main()
