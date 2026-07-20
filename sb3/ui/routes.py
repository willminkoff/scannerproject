"""sb3.ui.routes — build the JSON payloads sb3.html polls, from live backend state.

Every builder here is READ-ONLY and defensive: sb3.html null-checks every field
with a fallback, so a partial payload degrades gracefully rather than breaking
the page. Phase 3.1 populates the fields the heartbeat card and the analog
status renderers actually consume; the rest are added as the tabs come live in
3.2/3.3.

The write endpoints (scan/squelch/digital-restart) are seeded from
macos/scannerctl/app.py but return a "not wired yet" marker in 3.1 — they get
their real bodies (onto sb3.sdrangel + sb3.translator) in Phase 3.2.
"""

from __future__ import annotations

import datetime
from typing import Dict

from .. import backends, ownership
from ..state import State

GUARDED = ownership.GUARDED_MOUNTS  # ("neptune-trunk.mp3", "neptune-air.mp3")
AIR_MOUNT = "neptune-air.mp3"
DIGITAL_MOUNT = "neptune-trunk.mp3"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def build_status(state: State) -> Dict:
    """The /api/status payload — analog + digital health, mounts, loaded profile.

    Shaped to the fields sb3.html's refresh loop reads (all optional there).
    """
    loaded = set(backends.launchctl_loaded())
    icecast_mounts = backends.icecast_mounts()
    air = backends.mount_state(AIR_MOUNT)
    trunk = backends.mount_state(DIGITAL_MOUNT)
    devicesets = backends.sdrangel_devicesets()
    profile = state.read_loaded_profile() or {}

    # Is the Air deviceset live (real device bound + running)?
    air_ds = next((d for d in devicesets if d.index == profile.get("deviceset_index", 0)), None)
    air_running = bool(air_ds and not air_ds.is_phantom and air_ds.state == "running")

    sdrangel_up = "com.scannerproject.sdrangel" in loaded
    sdrtrunk_up = "com.scannerproject.sdrtrunk" in loaded

    return {
        "ok": True,
        "server_time": _now_iso(),
        # analog presence/activity (Air role is on RTL; Ground not deployed yet)
        "airband_present": sdrangel_up,
        "airband_active": air_running,
        "ground_present": False,          # Ground role not yet deployed (§ role map)
        "ground_active": False,
        "rtl_active": air_running,
        # icecast + mounts
        "icecast_active": trunk.present or air.present,
        "icecast_mounts": icecast_mounts,
        "icecast_expected_mounts": list(GUARDED),
        "icecast_port": 8000,
        "stream_mount": AIR_MOUNT,
        "digital_stream_mount": DIGITAL_MOUNT,
        # loaded profile (Phase 2 record)
        "profile_airband": profile.get("name", "") if profile.get("role") == "air" else "",
        "profile_ground": "",
        # digital (SDRTrunk) — coarse for now
        "digital_present": sdrtrunk_up,
        "digital_active": trunk.present,
        # SB3 self
        "sb3": {
            "killed": state.is_killed(),
            "loaded_profile": profile.get("name"),
            "agents_up": sorted(l for l in loaded if l in ownership.SB3_LAYER),
        },
        # mount detail for a debug panel / SITREP
        "mounts": {m.mount: m.http_status for m in (air, trunk)},
    }


def build_heartbeat(state: State) -> Dict:
    """The /api/heartbeat payload — the health card's state machine.

    state ∈ {quiet, rf_degraded, wedged, error}. sb3.html renders headline +
    explanation + evidence and colours the card. Read-only, never acts.
    """
    trunk = backends.mount_state(DIGITAL_MOUNT)
    air = backends.mount_state(AIR_MOUNT)
    profile = state.read_loaded_profile() or {}
    sdrangel_reachable = bool(backends.sdrangel_devicesets()) or air.http_status is not None

    evidence = [
        f"{DIGITAL_MOUNT}: {trunk.http_status}",
        f"{AIR_MOUNT}: {air.http_status}",
    ]

    # Digital is the always-on core; if it's down, that's the loudest signal.
    if not trunk.present:
        return {
            "state": "wedged",
            "headline": "Digital mount is down.",
            "explanation": f"{DIGITAL_MOUNT} is not 200 — SDRTrunk or icecast may be wedged.",
            "recovery": "Check SDRTrunk and icecast; SB3 does not own these.",
            "evidence": evidence,
            "since": _now_iso(),
        }
    # A loaded Air profile whose mount is dark = degraded (not necessarily wedged).
    if profile.get("role") == "air" and not air.present:
        return {
            "state": "rf_degraded",
            "headline": "Air profile loaded but its mount is silent.",
            "explanation": f"{profile.get('name')} is recorded loaded, but {AIR_MOUNT} "
                           f"is {air.http_status}. The audio chain may have dropped.",
            "recovery": "sb3-ctl profile status; re-load if it drifted.",
            "evidence": evidence,
            "since": _now_iso(),
        }
    if not sdrangel_reachable:
        return {
            "state": "error",
            "headline": "SDRangel REST unreachable.",
            "explanation": "Could not read devicesets or mounts from the backend.",
            "recovery": "Confirm SDRangel is running.",
            "evidence": evidence,
            "since": _now_iso(),
        }
    return {
        "state": "quiet",
        "headline": "All mounts live.",
        "explanation": "Digital and Air are both streaming; SB3 is hands-off.",
        "recovery": None,
        "evidence": evidence,
        "since": _now_iso(),
    }


def build_profiles(state: State) -> Dict:
    """Minimal /api/profiles so sb3.html's profile lookups don't error.

    Full profile registry + editor is Phase 3.2+. For now, report the one
    loaded Air profile as the active airband profile.
    """
    profile = state.read_loaded_profile() or {}
    active_air = profile.get("name", "") if profile.get("role") == "air" else ""
    return {
        "ok": True,
        "active_airband_id": active_air,
        "active_ground_id": "",
        "profiles": [],   # registry not exposed yet
    }


# ---- write endpoints (seeded from scannerctl; wired for real in Phase 3.2) ---

def not_wired(name: str) -> Dict:
    return {"ok": False, "error": "not-wired-in-3.1",
            "note": f"{name} lands in Phase 3.2 (analog write path)"}
