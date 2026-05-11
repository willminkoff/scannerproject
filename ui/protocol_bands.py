"""Protocol-to-band viability lookup for trunk-site picker.

Only protocols with a tight, empirically validated band signature get a
restriction; everything else falls through to allow-all. The single
non-trivial rule today is: P25 Phase 2 (P25X2_TDMA in HomePatrol DB)
cannot use a VHF control channel.

Set ``HP_DISABLE_BAND_FILTER=1`` to bypass entirely.
"""
from __future__ import annotations

import os


BAND_DEFS: dict[str, tuple[float, float]] = {
    "VHF_LOW":   (30.0,  50.0),
    "VHF_MID":   (50.0,  136.0),
    "VHF_HIGH":  (136.0, 174.0),
    "VHF_OOB":   (174.0, 380.0),
    "UHF_FED":   (380.0, 410.0),
    "UHF_BUS":   (410.0, 470.0),
    "UHF_TBAND": (470.0, 512.0),
    "UHF_OOB":   (512.0, 758.0),
    "700":       (758.0, 806.0),
    "800":       (806.0, 896.0),
    "900":       (896.0, 940.0),
}

_NON_VHF: frozenset[str] = frozenset(
    tag for tag in BAND_DEFS if not tag.startswith("VHF")
)

# Only entries with a known-too-narrow band signature appear here. Anything
# not in this dict (including NULL / unknown protocol) falls through to
# allow-all in is_site_viable().
PROTOCOL_BANDS: dict[str, frozenset[str]] = {
    "P25X2_TDMA": _NON_VHF,
}


def band_of(mhz: float) -> str | None:
    for tag, (lo, hi) in BAND_DEFS.items():
        if lo <= mhz < hi:
            return tag
    return None


def _filter_disabled() -> bool:
    raw = os.getenv("HP_DISABLE_BAND_FILTER", "").strip().lower()
    return raw not in ("", "0", "false", "no")


def has_band_rule(protocol: str | None) -> bool:
    """True iff there's a viability rule we'd actually apply to this protocol."""
    if _filter_disabled():
        return False
    proto = (protocol or "").strip()
    return proto in PROTOCOL_BANDS


def is_site_viable(protocol: str | None, control_channels_mhz) -> bool:
    """True iff the site's freqs are plausible for the protocol.

    Semantics:
      - bypass env var set         → True
      - unknown protocol           → True (no opinion)
      - known protocol, no freqs   → False (we have a rule but no evidence)
      - known protocol, any freq
        falls inside a viable band → True
      - otherwise                  → False
    """
    if _filter_disabled():
        return True
    viable = PROTOCOL_BANDS.get((protocol or "").strip())
    if viable is None:
        return True
    if not control_channels_mhz:
        return False
    for raw in control_channels_mhz:
        try:
            mhz = float(raw)
        except (TypeError, ValueError):
            continue
        tag = band_of(mhz)
        if tag in viable:
            return True
    return False
