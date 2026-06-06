"""Tests for ui.chirp_adapter favorite/profile activation.

Regression guard for the 2026-06-05 bug where activating a favorite pushed the
stale top-level ``state.custom_favorites`` to the chirp daemon instead of the
activated favorite's OWN channels (so single-channel "Casino" still scanned
SIC's ~20 channels). See fix in _read_favorite_freqs_for_band.
"""
import types

import pytest

from ui import chirp_adapter
from ui import hp_state


# A single-channel favorite (the "Casino" case) ...
CASINO = {
    "id": "fav-casino-artcc-test",
    "label": "Casino ARTCC (test)",
    "enabled_air": True,
    "enabled_ground": False,
    "custom_favorites": [
        {"frequency": 127.7, "alpha_tag": "Casino ARTCC",
         "id": "freq:fav-casino-artcc-test:0"},
    ],
}
# ... and a wide favorite (the "SIC" case) with several airband channels.
SIC = {
    "id": "fav-sic",
    "label": "SIC",
    "enabled_air": False,
    "enabled_ground": False,
    "custom_favorites": [
        {"frequency": f, "alpha_tag": f"sic-{f}", "id": f"freq:fav-sic:{i}"}
        for i, f in enumerate(
            [121.025, 121.125, 121.5, 123.4, 124.6, 125.125, 125.325,
             126.075, 127.175, 127.7, 127.85, 128.3, 132.05, 133.125,
             133.5, 134.025, 134.25, 134.325]
        )
    ],
}
# A deliberately-wrong global set: if the code ever reads this instead of the
# favorite's own channels, the tests below will catch it (962.x is bogus).
STALE_GLOBAL = [{"frequency": 962.1, "alpha_tag": "STALE-SHOULD-NOT-APPEAR"}]


def _install_state(monkeypatch, favorites):
    fake = types.SimpleNamespace(favorites=favorites, custom_favorites=STALE_GLOBAL)
    monkeypatch.setattr(hp_state.HPState, "load",
                        staticmethod(lambda *a, **k: fake))


def test_activating_casino_returns_only_its_channel(monkeypatch):
    _install_state(monkeypatch, [dict(CASINO), dict(SIC)])
    out = chirp_adapter._read_favorite_freqs_for_band(
        "airband", "fav-casino-artcc-test")
    freqs = sorted(c["freq_mhz"] for c in out)
    assert freqs == [127.7], f"expected only 127.7, got {freqs}"
    # the stale global must NOT leak in
    assert all(c["freq_mhz"] != 962.1 for c in out)


def test_activating_sic_returns_its_full_list(monkeypatch):
    casino = dict(CASINO); casino["enabled_air"] = False
    sic = dict(SIC); sic["enabled_air"] = True
    _install_state(monkeypatch, [casino, sic])
    out = chirp_adapter._read_favorite_freqs_for_band("airband", "fav-sic")
    freqs = sorted(c["freq_mhz"] for c in out)
    assert len(freqs) == len(SIC["custom_favorites"])  # all <137 MHz airband
    assert 127.7 in freqs and 121.025 in freqs
    assert 962.1 not in freqs


def test_favorite_not_enabled_for_band_returns_empty(monkeypatch):
    # Casino present but its enabled_air is False -> no-op fallback (return []).
    casino = dict(CASINO); casino["enabled_air"] = False
    _install_state(monkeypatch, [casino, dict(SIC)])
    out = chirp_adapter._read_favorite_freqs_for_band(
        "airband", "fav-casino-artcc-test")
    assert out == []


def test_unknown_favorite_id_returns_empty(monkeypatch):
    _install_state(monkeypatch, [dict(CASINO), dict(SIC)])
    out = chirp_adapter._read_favorite_freqs_for_band("airband", "fav-does-not-exist")
    assert out == []


def test_ground_band_filters_to_high_freqs(monkeypatch):
    # A favorite with a mix of airband (<137) and ground (>=137) channels;
    # ground activation must return only the >=137 ones.
    mixed = {
        "id": "fav-mixed", "label": "Mixed",
        "enabled_air": False, "enabled_ground": True,
        "custom_favorites": [
            {"frequency": 127.7, "alpha_tag": "air"},
            {"frequency": 138.05, "alpha_tag": "gnd1"},
            {"frequency": 140.2, "alpha_tag": "gnd2"},
        ],
    }
    _install_state(monkeypatch, [mixed])
    out = chirp_adapter._read_favorite_freqs_for_band("ground", "fav-mixed")
    freqs = sorted(c["freq_mhz"] for c in out)
    assert freqs == [138.05, 140.2]
