"""Disco Phase 4 — band-plan-first classifier Layer 1 lookup.

Loads the FCC band-plan YAML and provides pure functions for resolving a
detection's (class_name, freq_hz) pair against allocation rules.

No global state, no I/O after load_band_plan. Callers pass the plan to every
function so unit tests can construct synthetic plans without touching disk.

Typical usage:
    from disco.src.band_plan import load_band_plan, tag_for
    plan = load_band_plan("/path/to/us_band_plan.yaml")
    tag = tag_for(class_name, freq_hz, plan)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import yaml


@dataclass(frozen=True)
class Band:
    name: str
    freq_min_hz: int
    freq_max_hz: int
    allowed_modes: frozenset
    notes: str = ""


def load_band_plan(path: str) -> List[Band]:
    """Load and parse the FCC band-plan YAML. Bands are returned in file order;
    callers should order narrower/more-specific bands first since band_for()
    returns the first match."""
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    bands: List[Band] = []
    for entry in data.get("bands", []):
        bands.append(
            Band(
                name=entry["name"],
                freq_min_hz=int(entry["freq_min_hz"]),
                freq_max_hz=int(entry["freq_max_hz"]),
                allowed_modes=frozenset(entry.get("allowed_modes", [])),
                notes=entry.get("notes", ""),
            )
        )
    return bands


def band_for(freq_hz: float, plan: List[Band]) -> Optional[Band]:
    """Return the first band entry whose range contains freq_hz, or None if
    no band covers the frequency (permissive default — caller decides)."""
    for band in plan:
        if band.freq_min_hz <= freq_hz <= band.freq_max_hz:
            return band
    return None


def is_mode_allowed(class_name: str, freq_hz: float, plan: List[Band]) -> bool:
    """True if class_name is in the allowed_modes for the band covering
    freq_hz. Returns True when no band covers the frequency — the band plan
    cannot reject what it does not know about."""
    band = band_for(freq_hz, plan)
    if band is None:
        return True
    return class_name in band.allowed_modes


def tag_for(class_name: str, freq_hz: float, plan: List[Band]) -> str:
    """Produce the canonical Layer-2 label for a detection.

    Three cases:
      - In a band, mode allowed:   "<BAND_NAME> — <class_name>"
      - In a band, mode rejected:  "<BAND_NAME> — unidentified"
      - Outside all bands:         "<class_name>" unmodified (permissive default)

    The raw ML output is no longer embedded in the tag string (C6 cleanup —
    the tag column is operator-facing and the parenthetical was noise). The
    underlying ml_class is still available via detections.modulation_class
    for retrain-set curation, and interpret.py reads it from there directly.
    """
    band = band_for(freq_hz, plan)
    if band is None:
        return class_name
    if class_name in band.allowed_modes:
        return f"{band.name} — {class_name}"
    return f"{band.name} — unidentified"
