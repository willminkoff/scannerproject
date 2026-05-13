"""Dongle allocator for digital RTL-SDR tuner assignment.

Assigns available digital dongles to trunked-system roles (control vs traffic)
based on the active scan pool.  Persists assignments to a runtime state file
so that ``ensure-digital-runtime.py`` and ``digital.py`` agree on which dongle
is preferred-tuner for which system.

Allocation strategy
-------------------
* Sort active systems by scan-pool priority order (first = highest).
* Sort available digital serials lexicographically for determinism.
* Reserve **at least one** dongle for traffic when possible.
* Assign the first *N* serials as control tuners for the first *N* systems
  where *N* = min(system_count, digital_dongle_count - 1).
* If only one system is active, assign one control dongle and leave the rest
  for traffic.
* If systems >= dongles, all dongles go to control (no dedicated traffic).

The assignment map is written atomically to ``DONGLE_ASSIGNMENTS_PATH`` and
read by callers via :func:`load_assignments` or :func:`preferred_tuner_for_system`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default path -- overridable via env var for tests
# ---------------------------------------------------------------------------
DONGLE_ASSIGNMENTS_PATH = os.getenv(
    "DONGLE_ASSIGNMENTS_PATH",
    os.path.join(
        os.path.expanduser("~"),
        ".local",
        "state",
        "scannerproject",
        "airband_ui_dongle_assignments.json",
    ),
).strip()

_LOCK = threading.Lock()
_CACHED_ASSIGNMENTS: dict[str, Any] | None = None
_CACHED_ASSIGNMENTS_MTIME: float = 0.0


def _format_system_for_audit(system: dict[str, Any]) -> str:
    name = str(system.get("name") or "?").strip() or "?"
    sites = system.get("sites") or []
    primary = sites[0] if (isinstance(sites, list) and sites and isinstance(sites[0], dict)) else {}
    site_name = str(primary.get("site_name") or "?").strip() or "?"
    raw_dist = primary.get("distance_miles")
    try:
        dist = float(raw_dist)
        if dist != dist or dist < 0:
            raise ValueError
        return f"{name}@{site_name}({dist:.1f}mi)"
    except (TypeError, ValueError):
        return f"{name}@{site_name}(?mi)"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def allocate(
    digital_serials: list[str],
    systems: list[dict[str, Any]],
    *,
    priority_serials: list[str] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Compute and optionally persist a dongle-to-system assignment map.

    Parameters
    ----------
    digital_serials:
        Available tuner identifiers reserved for digital use.  Historically
        these were RTL-SDR EEPROM serials like ``"14306619"``; non-RTL
        identifiers (e.g. SDRTrunk uniqueIDs such as
        ``"RSPduo Tuner 1 SER#180903EF32"``) are also accepted and are
        passed through verbatim to the playlist's ``preferred_tuner``
        attribute.
    systems:
        Active trunked systems from the scan pool.  Each dict must have at
        least ``"name"`` (str) and ``"control_channels_mhz"`` (list[str]).
    priority_serials:
        Tuner identifiers that should be preferred for control-channel
        assignment (e.g. RSPduo tuners with their 12-bit ADC and lower
        noise figure are higher-quality control receivers than RTL-SDR's
        8-bit ADC).  These are assigned to systems before any entries in
        ``digital_serials``.  Within priority and non-priority tiers, sort
        order is lexicographic for determinism.  An identifier appearing
        in both lists is deduplicated (priority wins).  Defaults to no
        priority entries (legacy behavior — flat sorted pool).
    persist:
        When *True* (default) the result is written to
        ``DONGLE_ASSIGNMENTS_PATH``.

    Returns
    -------
    dict with keys:
        ``assignments`` – list of per-system dicts with ``system_name``,
        ``preferred_tuner_serial``, ``role`` (``"control"``).
        ``traffic_pool`` – list of serial strings not pinned to control.
        ``strategy`` – ``"dedicated_control"`` | ``"all_control"`` |
        ``"single_system"`` | ``"no_systems"`` | ``"no_dongles"``.
        ``digital_serials`` – ordered final pool: priority entries first
        (sorted), then non-priority entries (sorted).  Reflects what the
        allocator actually used, not the raw input.
        ``priority_serials`` – echo of the priority input (sorted, deduped).
        ``system_count`` – number of systems.
        ``updated_at_ms`` – epoch millis when computed.
    """
    priority_set = {s for s in (priority_serials or []) if s}
    regular_set = {s for s in digital_serials if s} - priority_set
    # Priority entries assigned first, then regulars; lex sort within tier
    # keeps determinism for a given pool composition.
    priority_sorted = sorted(priority_set)
    regular_sorted = sorted(regular_set)
    serials = priority_sorted + regular_sorted
    n_dongles = len(serials)
    n_systems = len(systems)
    now_ms = int(time.time() * 1000)

    assignments: list[dict[str, Any]] = []
    traffic_pool: list[str] = []

    if n_dongles == 0:
        strategy = "no_dongles"
    elif n_systems == 0:
        strategy = "no_systems"
        traffic_pool = list(serials)
    elif n_systems == 1:
        # One system: first dongle is control, rest are traffic.
        strategy = "single_system"
        assignments.append({
            "system_name": systems[0]["name"],
            "preferred_tuner_serial": serials[0],
            "role": "control",
            "control_channels_mhz": list(systems[0].get("control_channels_mhz") or []),
        })
        traffic_pool = serials[1:]
    elif n_systems < n_dongles:
        # Fewer systems than dongles: dedicate one per system, rest are traffic.
        strategy = "dedicated_control"
        for i, system in enumerate(systems):
            assignments.append({
                "system_name": system["name"],
                "preferred_tuner_serial": serials[i],
                "role": "control",
                "control_channels_mhz": list(system.get("control_channels_mhz") or []),
            })
        traffic_pool = serials[n_systems:]
    elif n_systems == n_dongles:
        # Exact match: every dongle on control, none reserved for traffic.
        # SDRTrunk may still temporarily pull one for voice grants.
        strategy = "all_control"
        for i, system in enumerate(systems):
            assignments.append({
                "system_name": system["name"],
                "preferred_tuner_serial": serials[i],
                "role": "control",
                "control_channels_mhz": list(system.get("control_channels_mhz") or []),
            })
    else:
        # More systems than dongles: pin (n_dongles - 1) to top-priority
        # systems for control, keep 1 for traffic.  Remaining systems get
        # no dedicated control dongle -- SDRTrunk will not monitor them
        # unless we add rotation later.  Caller is expected to deliver
        # `systems` sorted closest-first (see favorites_runtime), so the
        # tail dropped here is the geographically farthest set.
        strategy = "dedicated_control"
        control_count = max(1, n_dongles - 1)
        for i in range(control_count):
            assignments.append({
                "system_name": systems[i]["name"],
                "preferred_tuner_serial": serials[i],
                "role": "control",
                "control_channels_mhz": list(systems[i].get("control_channels_mhz") or []),
            })
        traffic_pool = serials[control_count:]
        # Audit log: who was kept and who was dropped, with primary-site
        # distance so an operator can grep the journal for unexpected drops.
        kept = [_format_system_for_audit(s) for s in systems[:control_count]]
        dropped = [_format_system_for_audit(s) for s in systems[control_count:]]
        if dropped:
            logger.info(
                "Dongle over-subscription: %d systems > %d dongles. "
                "Kept %d (closest): %s. Dropped %d: %s",
                n_systems,
                n_dongles,
                control_count,
                ", ".join(kept),
                len(dropped),
                ", ".join(dropped),
            )

    result: dict[str, Any] = {
        "assignments": assignments,
        "traffic_pool": traffic_pool,
        "strategy": strategy,
        "digital_serials": serials,
        "priority_serials": priority_sorted,
        "system_count": n_systems,
        "updated_at_ms": now_ms,
    }

    if persist:
        _persist(result)

    return result


def load_assignments() -> dict[str, Any] | None:
    """Load the most recent assignment map from disk (with mtime cache)."""
    global _CACHED_ASSIGNMENTS, _CACHED_ASSIGNMENTS_MTIME
    path = DONGLE_ASSIGNMENTS_PATH
    if not path or not os.path.isfile(path):
        return None
    try:
        st = os.stat(path)
        mtime = st.st_mtime
    except OSError:
        return None

    with _LOCK:
        if _CACHED_ASSIGNMENTS is not None and mtime == _CACHED_ASSIGNMENTS_MTIME:
            return dict(_CACHED_ASSIGNMENTS)

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return None
        with _LOCK:
            _CACHED_ASSIGNMENTS = data
            _CACHED_ASSIGNMENTS_MTIME = mtime
        return dict(data)
    except Exception:
        logger.debug("Failed reading dongle assignments from %s", path, exc_info=True)
        return None


def preferred_tuner_for_system(system_name: str) -> str:
    """Return the preferred tuner serial for *system_name*, or ``""``."""
    assignments = load_assignments()
    if not assignments:
        return ""
    for entry in assignments.get("assignments") or []:
        if str(entry.get("system_name") or "").strip().lower() == system_name.strip().lower():
            return str(entry.get("preferred_tuner_serial") or "").strip()
    return ""


def traffic_pool_serials() -> list[str]:
    """Return the list of serials available for traffic duty."""
    assignments = load_assignments()
    if not assignments:
        return []
    return list(assignments.get("traffic_pool") or [])


def assigned_digital_tuner_ids(assignments: dict[str, Any] | None = None) -> list[str]:
    """Return the ordered set of digital tuner identifiers from allocator state."""
    payload = assignments if isinstance(assignments, dict) else load_assignments()
    if not isinstance(payload, dict):
        return []

    ordered: list[str] = []

    def _append(value: Any) -> None:
        token = str(value or "").strip()
        if token and token not in ordered:
            ordered.append(token)

    for raw in payload.get("digital_serials") or []:
        _append(raw)

    for row in payload.get("assignments") or []:
        if not isinstance(row, dict):
            continue
        _append(row.get("preferred_tuner_serial"))

    for raw in payload.get("traffic_pool") or []:
        _append(raw)

    return ordered


def tuner_id_looks_like_rtl_serial(value: Any) -> bool:
    """Best-effort classifier for raw RTL EEPROM serial strings."""
    token = str(value or "").strip()
    return token.isdigit() and len(token) >= 6


def assigned_digital_rtl_serials(assignments: dict[str, Any] | None = None) -> list[str]:
    """Return allocator digital tuner identifiers that look like RTL serials."""
    return [
        token
        for token in assigned_digital_tuner_ids(assignments)
        if tuner_id_looks_like_rtl_serial(token)
    ]


def current_strategy() -> str:
    """Return the active allocation strategy label."""
    assignments = load_assignments()
    if not assignments:
        return "unknown"
    return str(assignments.get("strategy") or "unknown")


def invalidate_cache() -> None:
    """Force next :func:`load_assignments` to re-read from disk."""
    global _CACHED_ASSIGNMENTS, _CACHED_ASSIGNMENTS_MTIME
    with _LOCK:
        _CACHED_ASSIGNMENTS = None
        _CACHED_ASSIGNMENTS_MTIME = 0.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _persist(data: dict[str, Any]) -> None:
    """Atomically write the assignment map to disk."""
    global _CACHED_ASSIGNMENTS, _CACHED_ASSIGNMENTS_MTIME
    path = DONGLE_ASSIGNMENTS_PATH
    if not path:
        return
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp, path)
        try:
            st = os.stat(path)
            with _LOCK:
                _CACHED_ASSIGNMENTS = dict(data)
                _CACHED_ASSIGNMENTS_MTIME = st.st_mtime
        except OSError:
            pass
        logger.info(
            "Dongle assignments persisted: strategy=%s systems=%d traffic=%d path=%s",
            data.get("strategy"),
            data.get("system_count"),
            len(data.get("traffic_pool") or []),
            path,
        )
    except Exception:
        logger.error("Failed persisting dongle assignments to %s", path, exc_info=True)
        try:
            os.unlink(tmp)
        except OSError:
            pass
