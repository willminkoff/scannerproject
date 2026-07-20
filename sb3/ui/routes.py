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
        # per-channel state for the Airband channel list (Phase 3.2)
        "channels": _channel_states(profile.get("deviceset_index", 0)),
        # mount detail for a debug panel / SITREP
        "mounts": {m.mount: m.http_status for m in (air, trunk)},
    }


def _channel_states(idx: int) -> list:
    """Per-channel index/title/demod/freq for the UI channel list. Read-only.

    Uses only the fields present in the deviceset channel list (index, title,
    id, deltaFrequency) so the status poll stays a single GET — squelch/volume
    live at /channel/N/settings and are not fetched here (write-only in 3.2).
    """
    out = []
    center = next((ds.center_hz for ds in backends.sdrangel_devicesets()
                   if ds.index == idx), None)
    for ch in backends.sdrangel_channels(idx):
        off = ch.get("deltaFrequency")
        freq_hz = (center + off) if (center is not None and off is not None) else None
        out.append({
            "index": ch.get("index"),
            "title": ch.get("title", ch.get("id", "?")),
            "demod": ch.get("id"),
            "freq_hz": freq_hz,
        })
    return out


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


# ===========================================================================
# Phase 3.2 — Airband tab write path
# ===========================================================================
#
# The main-app controls POST application/x-www-form-urlencoded via postAPI():
#   /api/apply        target,gain,squelch_mode=dbfs,squelch_dbfs
#   /api/apply-batch  target,gain,squelch_mode,squelch_dbfs,cutoff_hz
#   /api/filter       target,cutoff_hz
#   /api/tune         target,freq   (freq in MHz)
#   /api/volume       action=set&level=<0-100>  |  action=get
#   /api/hits         GET → {items:[...]}
#
# "Human is right" (§4.2): these edit LIVE SDRangel but NEVER rewrite the
# profile JSON. A UI squelch tweak makes the loaded profile 'drifted' in value;
# SB3 does not fight it.

from ..profile import DEMOD_CHANNEL, HW_SETTINGS_KEY  # noqa: E402
from ..profilecmd import resolve_profile_path  # noqa: E402
from ..profile import load_profile, ProfileError  # noqa: E402
from ..sdrangel import SDRangelClient  # noqa: E402

# bounds
AIRBAND_MIN_HZ, AIRBAND_MAX_HZ = 108_000_000, 137_000_000
SQUELCH_MIN, SQUELCH_MAX = -100.0, 0.0
CUTOFF_MIN, CUTOFF_MAX = 1_000, 25_000
VOLUME_MIN, VOLUME_MAX = 0.0, 5.0
GAIN_MIN, GAIN_MAX = 0.0, 50.0     # RTL dB range


class WriteError(Exception):
    """A bad request (400) or unhealthy backend (503) — carries an HTTP code."""

    def __init__(self, code: int, msg: str):
        self.code = code
        self.msg = msg
        super().__init__(msg)


def _client() -> SDRangelClient:
    return SDRangelClient(execute=True, emit=lambda m: None)


def _require(form: Dict, key: str) -> str:
    if key not in form:
        raise WriteError(400, f"missing field: {key}")
    return form[key]


def _num(form: Dict, key: str, lo: float, hi: float) -> float:
    raw = _require(form, key)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise WriteError(400, f"{key} not a number: {raw!r}")
    if not (lo <= v <= hi):
        raise WriteError(400, f"{key}={v} out of range [{lo}, {hi}]")
    return v


def _keepalive_offset(state: State, center_hz: int) -> "int | None":
    """The keepalive channel's inputFrequencyOffset, so writes can spare it.

    Raising the keepalive channel's squelch would let the mount drop when every
    real channel gates closed — the whole reason it exists. So squelch/volume
    writes skip it. Determined from the loaded profile file (the runtime record
    doesn't carry the keepalive flag).
    """
    rec = state.read_loaded_profile()
    if not rec:
        return None
    path = resolve_profile_path(rec.get("name", ""))
    if not path:
        return None
    try:
        prof = load_profile(path)
    except ProfileError:
        return None
    ka = prof.keepalive_channels
    return ka[0].offset_from(center_hz) if ka else None


def _airband_deviceset(client: SDRangelClient, state: State):
    """Return (idx, hw, channels, center_hz) for the airband deviceset, or raise."""
    rec = state.read_loaded_profile() or {}
    idx = rec.get("deviceset_index", 0)
    sd = client.sampling_device(idx)
    if not sd:
        raise WriteError(503, "SDRangel unreachable")
    hw = sd.get("hwType")
    if hw not in HW_SETTINGS_KEY:
        raise WriteError(503, f"ds{idx} has no real device (hw={hw})")
    center = sd.get("centerFrequency") or (rec.get("center_freq_hz") or 0)
    return idx, hw, client.list_channels(idx), int(center)


def _only_airband(target: str):
    if target != "airband":
        raise WriteError(400, f"target {target!r} not supported yet "
                              f"(only 'airband' is deployed)")


def apply_controls(form: Dict, state: State, *, with_filter: bool = False) -> Dict:
    """/api/apply and /api/apply-batch — device gain + per-channel squelch (+cutoff).

    Gain is device-level. Squelch is applied to every REAL channel (the keepalive
    channel is spared, to keep the mount up). cutoff_hz → rfBandwidth on the same
    real channels.
    """
    _only_airband(form.get("target", "airband"))
    gain = _num(form, "gain", GAIN_MIN, GAIN_MAX)
    squelch = _num(form, "squelch_dbfs", SQUELCH_MIN, SQUELCH_MAX)
    cutoff = _num(form, "cutoff_hz", CUTOFF_MIN, CUTOFF_MAX) if with_filter else None

    c = _client()
    idx, hw, channels, center = _airband_deviceset(c, state)
    ka_off = _keepalive_offset(state, center)

    # device gain (RTL gain field is tenths of dB)
    if not c.patch_device(idx, hw, HW_SETTINGS_KEY[hw], {"gain": int(round(gain * 10))}):
        raise WriteError(503, "gain PATCH failed (SDRangel unhealthy)")

    touched, skipped_ka = 0, 0
    for ch in channels:
        ch_idx = ch.get("index")
        ctype = ch.get("id", "AMDemod")
        skey = ctype + "Settings"
        # skip the keepalive channel for squelch (never raise its squelch)
        _, chset = c._req("GET", f"/deviceset/{idx}/channel/{ch_idx}/settings")
        off = (chset.get(skey, {}) or {}).get("inputFrequencyOffset")
        patch = {}
        if ka_off is not None and off == ka_off:
            skipped_ka += 1
        else:
            patch["squelch"] = squelch
        if cutoff is not None:
            patch["rfBandwidth"] = int(cutoff)
        if patch and c.patch_channel(idx, ch_idx, ctype, skey, patch):
            touched += 1

    return {"ok": True, "applied_gain": gain, "applied_squelch_dbfs": squelch,
            "cutoff_hz": cutoff, "channels_touched": touched,
            "keepalive_spared": skipped_ka, "restart_ok": True}


def apply_filter(form: Dict, state: State) -> Dict:
    """/api/filter — rfBandwidth on every real channel."""
    _only_airband(form.get("target", "airband"))
    cutoff = _num(form, "cutoff_hz", CUTOFF_MIN, CUTOFF_MAX)
    c = _client()
    idx, hw, channels, _ = _airband_deviceset(c, state)
    touched = 0
    for ch in channels:
        ctype = ch.get("id", "AMDemod")
        if c.patch_channel(idx, ch.get("index"), ctype, ctype + "Settings",
                           {"rfBandwidth": int(cutoff)}):
            touched += 1
    return {"ok": True, "cutoff_hz": cutoff, "channels_touched": touched}


def tune(form: Dict, state: State) -> Dict:
    """/api/tune — retune the device CENTER (freq in MHz) for camp mode.

    Camp mode has fixed channels around one center; 'tune' moves that center.
    Bounds-checked to the airband.
    """
    _only_airband(form.get("target", "airband"))
    freq_mhz = _num(form, "freq", AIRBAND_MIN_HZ / 1e6, AIRBAND_MAX_HZ / 1e6)
    center_hz = int(round(freq_mhz * 1e6))
    c = _client()
    idx, hw, _, _ = _airband_deviceset(c, state)
    if not c.patch_device(idx, hw, HW_SETTINGS_KEY[hw], {"centerFrequency": center_hz}):
        raise WriteError(503, "tune PATCH failed (SDRangel unhealthy)")
    return {"ok": True, "center_hz": center_hz, "freq_mhz": freq_mhz}


def volume(form: Dict, state: State) -> Dict:
    """/api/volume — action=set&level=<0-100> maps to per-channel volume; get reads it."""
    action = form.get("action", "get")
    c = _client()
    idx, hw, channels, center = _airband_deviceset(c, state)
    ka_off = _keepalive_offset(state, center)
    if action == "get":
        vols = []
        for ch in channels:
            ctype = ch.get("id", "AMDemod")
            _, chset = c._req("GET", f"/deviceset/{idx}/channel/{ch.get('index')}/settings")
            v = (chset.get(ctype + "Settings", {}) or {}).get("volume")
            if v is not None:
                vols.append(float(v))
        avg = sum(vols) / len(vols) if vols else 0.0
        return {"ok": True, "level": int(round(avg / VOLUME_MAX * 100))}
    # set
    level = _num(form, "level", 0, 100)
    vol = round(level / 100.0 * VOLUME_MAX, 2)      # 0-100 → 0.0-5.0
    touched = 0
    for ch in channels:
        ch_idx = ch.get("index")
        ctype = ch.get("id", "AMDemod")
        skey = ctype + "Settings"
        _, chset = c._req("GET", f"/deviceset/{idx}/channel/{ch_idx}/settings")
        off = (chset.get(skey, {}) or {}).get("inputFrequencyOffset")
        # keep the keepalive channel quiet (its low volume is intentional)
        if ka_off is not None and off == ka_off:
            continue
        if c.patch_channel(idx, ch_idx, ctype, skey, {"volume": vol}):
            touched += 1
    return {"ok": True, "level": int(level), "volume": vol, "channels_touched": touched}


def hits(state: State) -> Dict:
    """/api/hits — recent activity. SB3 has no hit log yet; empty feed (valid shape)."""
    return {"ok": True, "items": []}
