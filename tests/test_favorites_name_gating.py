"""Regression tests for favorites_name being pinned at a disabled tile.

Bug context
-----------
A user in Sea Isle City NJ had ``SIC`` as their only enabled favorite tile,
yet on two occasions the favorites_runtime sync wrote the *Nashville_Main*
tile's airband entries into ``rtl_airband_hp3_favorites_airband.conf``.
The cause was ``state.favorites_name`` being set to ``"Nashville_Main"``
(via a stale browser POST / multi-tab race) even though that tile was
``enabled=False``.  The active-tile resolver picked the named tile
regardless of its enabled flag, and the sync dutifully wrote its content.

This module exercises both defensive layers:

* ``ui.handlers._apply_hp_state_form`` should refuse to set
  ``state.favorites_name`` to a tile that is not currently enabled.
* ``ui.scan_mode_controller._resolve_active_favorites_entries`` should
  ignore a name-matched tile that is disabled and fall back to the
  first-enabled tile.
"""
from __future__ import annotations

import unittest

from ui import handlers
from ui.hp_state import HPState
from ui.scan_mode_controller import ScanModeController


def _build_state_with_two_tiles(*, active_name: str = "SIC") -> HPState:
    """Return an HPState with Nashville (disabled) + SIC (enabled)."""
    state = HPState.default()
    state.enabled_service_tags = [2, 3, 4, 15]
    state.favorites = [
        {
            "id": "fav-nashville-main",
            "label": "Nashville_Main",
            "enabled": False,
            "custom_favorites": [
                {
                    "id": "freq:bna:118.4",
                    "kind": "conventional",
                    "system_name": "Nashville International Airport (BNA)",
                    "alpha_tag": "Approach/Departure - East",
                    "service_tag": 15,
                    "frequency": 118.4,
                    "control_channels": [],
                }
            ],
        },
        {
            "id": "fav-sic",
            "label": "SIC",
            "enabled": True,
            "custom_favorites": [
                {
                    "id": "freq:sic:121.9",
                    "kind": "conventional",
                    "system_name": "Atlantic City International Airport (ACY)",
                    "alpha_tag": "Ground Control",
                    "service_tag": 15,
                    "frequency": 121.9,
                    "control_channels": [],
                }
            ],
        },
    ]
    state.favorites_name = active_name
    return state


class FavoritesNameGatingTests(unittest.TestCase):
    # ------------------------------------------------------------------
    # Layer 1: handlers._apply_hp_state_form refuses disabled-tile names
    # ------------------------------------------------------------------

    def test_apply_form_rejects_favorites_name_pointing_at_disabled_tile(self) -> None:
        state = _build_state_with_two_tiles(active_name="SIC")
        handlers._apply_hp_state_form(state, {"favorites_name": "Nashville_Main"})
        self.assertEqual(
            "SIC",
            state.favorites_name,
            "favorites_name pointing at disabled tile must be ignored",
        )

    def test_apply_form_accepts_favorites_name_pointing_at_enabled_tile(self) -> None:
        state = _build_state_with_two_tiles(active_name="My Favorites")
        handlers._apply_hp_state_form(state, {"favorites_name": "SIC"})
        self.assertEqual("SIC", state.favorites_name)

    def test_apply_form_accepts_favorites_name_case_insensitively(self) -> None:
        # Real-world POSTs sometimes have casing drift; the lookup should
        # treat them as equivalent.  We preserve the incoming casing in
        # state.favorites_name (the resolver lower-cases for comparison).
        state = _build_state_with_two_tiles(active_name="Old")
        handlers._apply_hp_state_form(state, {"favorites_name": "sic"})
        self.assertEqual("sic", state.favorites_name)

    def test_apply_form_blank_favorites_name_falls_back_to_default(self) -> None:
        state = _build_state_with_two_tiles(active_name="SIC")
        handlers._apply_hp_state_form(state, {"favorites_name": "   "})
        self.assertEqual("My Favorites", state.favorites_name)

    def test_apply_form_no_favorites_name_key_leaves_existing_value(self) -> None:
        state = _build_state_with_two_tiles(active_name="SIC")
        handlers._apply_hp_state_form(state, {"strict_location": False})
        self.assertEqual("SIC", state.favorites_name)

    # ------------------------------------------------------------------
    # Layer 2: resolver ignores a disabled tile even when name matches
    # ------------------------------------------------------------------

    def test_resolver_ignores_disabled_tile_named_by_favorites_name(self) -> None:
        # Simulate the bug scenario directly: state.favorites_name was
        # mutated to a disabled tile name by some path that bypasses
        # _apply_hp_state_form.  The resolver must still refuse it.
        state = _build_state_with_two_tiles(active_name="SIC")
        state.favorites_name = "Nashville_Main"  # bypass Layer 1

        entries = ScanModeController._resolve_active_favorites_entries(state)

        # Expect SIC's entries (the only enabled tile), NOT Nashville's.
        self.assertEqual(1, len(entries))
        self.assertEqual(121.9, entries[0]["frequency"])
        self.assertEqual(
            "Atlantic City International Airport (ACY)",
            entries[0]["system_name"],
        )

    def test_resolver_uses_named_enabled_tile_when_match(self) -> None:
        state = _build_state_with_two_tiles(active_name="SIC")
        entries = ScanModeController._resolve_active_favorites_entries(state)
        self.assertEqual(1, len(entries))
        self.assertEqual(121.9, entries[0]["frequency"])

    def test_resolver_falls_back_to_first_enabled_when_no_name_match(self) -> None:
        # Name doesn't match any tile -> fall back to first enabled (SIC).
        state = _build_state_with_two_tiles(active_name="does-not-exist")
        entries = ScanModeController._resolve_active_favorites_entries(state)
        self.assertEqual(1, len(entries))
        self.assertEqual(121.9, entries[0]["frequency"])


if __name__ == "__main__":
    unittest.main()
