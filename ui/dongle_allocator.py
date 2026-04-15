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
* If 4+ systems are active and 4+ dongles are available, pin exactly the
  first 4 systems to control. Overflow systems remain unmonitored.

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

_MAX_OVERFLOW_CONTROL_SYSTEMS = 4

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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def allocate(
    digital_serials: list[str],
    systems: list[dict[str, Any]],
    *,
    persist: bool = True,
) -> dict[str, Any]:
    """Compute and optionally persist a dongle-to-system assignment map.

    Parameters
    ----------
    digital_serials:
        Available RTL-SDR serial numbers reserved for digital use,
        e.g. ``["00000001", "14306619", "56919602"]``.
    systems:
        Active trunked systems from the scan pool.  Each dict must have at
        least ``"name"`` (str) and ``"control_channels_mhz"`` (list[str]).
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
        ``digital_serials`` – echo of the input serials.
        ``system_count`` – number of systems.
        ``updated_at_ms`` – epoch millis when computed.
    """
    serials = sorted(set(s for s in digital_serials if s))
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
    elif n_systems >= _MAX_OVERFLOW_CONTROL_SYSTEMS and n_dongles >= _MAX_OVERFLOW_CONTROL_SYSTEMS:
        # Full-database cap: when 4+ systems are available and we have 4+
        # digital-capacity dongles, pin exactly 4 systems to control and let
        # lower-priority systems remain unmonitored. Any surplus dongles above
        # the first four remain available for traffic following.
        control_count = min(_MAX_OVERFLOW_CONTROL_SYSTEMS, n_systems, n_dongles)
        for i in range(control_count):
            assignments.append({
                "system_name": systems[i]["name"],
                "preferred_tuner_serial": serials[i],
                "role": "control",
                "control_channels_mhz": list(systems[i].get("control_channels_mhz") or []),
            })
        traffic_pool = serials[control_count:]
        strategy = "all_control" if not traffic_pool else "dedicated_control"
        unmonitored = [s["name"] for s in systems[control_count:]]
        if unmonitored:
            logger.warning(
                "Capped digital control monitoring at %d systems. "
                "Unmonitored systems: %s",
                control_count,
                ", ".join(unmonitored),
            )
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
        # unless we add rotation later.
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
        # Log that some systems won't have dedicated control monitoring.
        unmonitored = [s["name"] for s in systems[control_count:]]
        if unmonitored:
            logger.warning(
                "Not enough digital dongles for all systems. "
                "Unmonitored systems (no dedicated control): %s",
                ", ".join(unmonitored),
            )

    result: dict[str, Any] = {
        "assignments": assignments,
        "traffic_pool": traffic_pool,
        "strategy": strategy,
        "digital_serials": serials,
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
