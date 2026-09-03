"""sb3.ui.routes — build the JSON payloads sb3.html polls, from live backend state.

Every builder here is READ-ONLY and defensive: sb3.html null-checks every field
with a fallback, so a partial payload degrades gracefully rather than breaking
the page. The status/heartbeat/profiles builders populate the fields the
heartbeat card and the analog/ground/digital status renderers consume.

The write endpoints (apply/filter/tune/volume) have their real bodies wired onto
sb3.sdrangel + sb3.translator (Phase 3.2/3.3). Endpoints the inherited UI still
calls but SB3 does not yet implement return a graceful marker — see
sb3.ui.server: unknown GET /api/* → 200 {ok:false, not-implemented}, unknown
POST /api/* → 501 — so the page degrades rather than breaking.
"""

from __future__ import annotations

import datetime
import os
import platform
import socket
from typing import Dict

from .. import backends, gitdeploy, ownership, sdrtrunk_client, op25_client
from ..state import State

GUARDED = ownership.GUARDED_MOUNTS  # ("venus-digital.mp3", "neptune-analog.mp3", ...)
AIR_MOUNT = "neptune-analog.mp3"     # analog scanner mount (renamed from neptune-air.mp3 2026-07-21)
GROUND_MOUNT = "neptune-ground.mp3"
DIGITAL_MOUNT = "venus-digital.mp3"


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
    ground = backends.mount_state(GROUND_MOUNT)
    devicesets = backends.sdrangel_devicesets()
    profiles = state.read_loaded_profiles()
    profile = profiles.get("air") or {}
    ground_profile = profiles.get("ground") or {}
    vfo_profile = profiles.get("vfo") or {}

    def _running(rec):
        d = next((x for x in devicesets if x.index == rec.get("deviceset_index", -99)), None)
        return bool(d and not d.is_phantom and d.state == "running")
    air_running = _running(profile) if profile else False
    ground_running = _running(ground_profile) if ground_profile else False
    vfo_running = _running(vfo_profile) if vfo_profile else False

    sdrangel_up = "com.scannerproject.sdrangel" in loaded
    sdrtrunk_up = ("com.scannerproject.sdrtrunk" in loaded) or bool(os.environ.get("SB3_SDRTRUNK_REMOTE_URL"))

    # Per-role one-word status the UI surfaces (e.g. the Ground offline banner).
    # Derived only from data already fetched above — no extra SDRangel call, so
    # the status poll never probes USB out from under a running device.
    def _role_status(loaded_profile: bool, running: bool, mount_present: bool) -> str:
        if not loaded_profile:
            return "not loaded"
        if running and mount_present:
            return "live"
        if running and not mount_present:
            return "running — no audio"
        return "loaded — device offline"
    air_status = _role_status(bool(profile), air_running, air.present)
    ground_status = _role_status(bool(ground_profile), ground_running, ground.present)
    # VFO shares AIR_MOUNT with Air (both DS route audio to the same idx -1 tap →
    # :9998 → neptune-analog.mp3). So its "has audio" signal is air.present — the
    # shared mount — not a mount of its own. vfo_running reflects its own DS1.
    vfo_status = _role_status(bool(vfo_profile), vfo_running, air.present)

    return {
        "ok": True,
        "server_time": _now_iso(),
        # analog presence/activity (Air role is on RTL; Ground not deployed yet)
        "airband_present": sdrangel_up,
        "airband_active": air_running,
        "ground_present": bool(ground_profile),
        "ground_active": ground_running,
        "rtl_active": air_running or ground_running,
        # per-role status strings (additive; Ground offline banner reads these)
        "air_status": air_status,
        "ground_status": ground_status,
        "vfo_status": vfo_status,
        "air_device_online": air_running,
        "ground_device_online": ground_running,
        "vfo_device_online": vfo_running,
        "vfo_active": vfo_running,
        # VFO shares the analog mount with Air (no mount of its own)
        "vfo_stream_mount": AIR_MOUNT,
        # icecast + mounts
        "icecast_active": trunk.present or air.present,
        "icecast_mounts": icecast_mounts,
        "icecast_expected_mounts": list(GUARDED),
        "icecast_port": 8000,
        # SB3 has no /stream/<mount> proxy and no /hls/ endpoint — it serves the
        # UI and /api/* only. sb3.html defaults streamProxyEnabled to TRUE and
        # only lowers it from THIS field, so omitting it left every player
        # pointed at origin/stream/<mount> → 404: controls rendered, play did
        # nothing, silently. Saying false selects the page's own
        # shouldPreferDirectIcecastStream() path, which builds
        # http://<host>:8000/<mount> — the live icecast mount. Both the analog
        # and digital players read this one flag.
        "stream_proxy_enabled": False,
        "stream_mount": AIR_MOUNT,
        "ground_stream_mount": GROUND_MOUNT,
        "digital_stream_mount": DIGITAL_MOUNT,
        # loaded profiles (per role)
        "profile_airband": profile.get("name", ""),
        "profile_ground": ground_profile.get("name", ""),
        "profile_vfo": vfo_profile.get("name", ""),
        # digital (SDRTrunk) — from the log-tail observer (Phase 3.3)
        "digital_present": sdrtrunk_up,
        **op25_client.observe(running=sdrtrunk_up),
        # SB3 self
        "sb3": {
            "killed": state.is_killed(),
            "loaded_profile": profile.get("name"),
            "agents_up": sorted(l for l in loaded if l in ownership.SB3_LAYER),
        },
        # per-channel state for the Airband channel list (Phase 3.2)
        "channels": _channel_states(profile.get("deviceset_index", 0)),
        # VFO channel (single NFM receiver on DS1) — its tuned freq for the card
        "vfo_channels": (_channel_states(vfo_profile.get("deviceset_index", 1))
                         if vfo_profile else []),
        # Stop/Start VFO button state. audioMute on DS1 ch0 IS the stop switch:
        # muting silences the VFO's contribution to the shared analog mount
        # without touching the deviceset, so restart is instant and reversible.
        "vfo_muted": (_vfo_muted(vfo_profile.get("deviceset_index", 1))
                      if vfo_profile else False),
        # mount detail for a debug panel / SITREP
        "mounts": {m.mount: m.http_status for m in (air, trunk, ground)},
    }


def _vfo_muted(idx: int) -> bool:
    """True if DS1 ch0 is muted (the VFO is 'stopped'). Read-only.

    Fail-closed toward "running": if the deviceset, the channel, or SDRangel
    itself cannot be read, report False. A button that wrongly reads "Start VFO"
    invites one harmless extra click; one that wrongly reads "Stop VFO" tells
    the user audio is live when it may not be.
    """
    try:
        chans = backends.sdrangel_channels(idx)
        if not chans:
            return False
        ch_idx = chans[0].get("index", 0)
        body = backends.channel_settings_body(
            backends.sdrangel_channel_settings(idx, ch_idx))
        return bool(body.get("audioMute"))
    except Exception:
        return False


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
    """/api/profiles — analog profile registry (airband/ground/vfo)."""
    import json as _json
    from ..profilecmd import user_profile_dir as _user_dir

    profs = state.read_loaded_profiles()
    active_air = (profs.get("air") or {}).get("name", "")
    active_ground = (profs.get("ground") or {}).get("name", "")
    active_vfo = (profs.get("vfo") or {}).get("name", "")

    seen = set()
    all_profs = []
    from pathlib import Path as _Path
    _root = _Path(__file__).resolve().parent.parent.parent
    for source, d in (("repo", _root / "profiles"), ("user", _user_dir())):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.json")):
            stem = f.stem
            if stem in seen:
                continue
            seen.add(stem)
            try:
                data = _json.loads(f.read_text())
            except Exception:
                continue
            role = str(data.get("role") or "").strip().lower()
            all_profs.append({
                "id": stem,
                "name": data.get("name") or stem,
                "role": role,
                "sub_role": data.get("sub_role") or "",
                "description": data.get("description") or data.get("_comment", "")[:200],
                "source": source,
            })

    return {
        "ok": True,
        "active_airband_id": active_air,
        "active_ground_id": active_ground,
        "active_vfo_id": active_vfo,
        "profiles_airband": [p for p in all_profs if p["role"] == "air"],
        "profiles_ground": [p for p in all_profs if p["role"] == "ground"],
        "profiles_vfo": [p for p in all_profs if p["role"] == "vfo"],
        # legacy: some UI code reads .profiles as a flat list
        "profiles": all_profs,
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
VHF_MIN_HZ, VHF_MAX_HZ = 136_000_000, 174_000_000   # Ground VHF (incl. NOAA WX 162)
# VFO is a free-tuning receiver on the RTL; bound it to the dongle's usable range.
VFO_MIN_HZ, VFO_MAX_HZ = 24_000_000, 1_766_000_000
# DC-spike dodge: the RTL LO sits this far ABOVE the listen freq so the NFM demod
# is off the dongle's center DC spike (device center = freq + offset; ch = -offset).
VFO_LO_DODGE_HZ = 100_000
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


# The UI's target strings map to SB3 roles.
_TARGET_ROLE = {"airband": "air", "ground": "ground", "vfo": "vfo"}


def _role_for(target: str) -> str:
    role = _TARGET_ROLE.get(target)
    if role is None:
        raise WriteError(400, f"unknown target {target!r} (want airband|ground|vfo)")
    return role


def _keepalive_offset(state: State, role: str, center_hz: int) -> "int | None":
    """The role's keepalive channel offset, so writes can spare it.

    Raising the keepalive channel's squelch would let the mount drop when every
    real channel gates closed — the whole reason it exists. So squelch/volume
    writes skip it, for whichever role (Air or Ground) is being written.
    """
    rec = state.read_loaded_profile(role)
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


def _role_deviceset(client: SDRangelClient, state: State, role: str):
    """Return (idx, hw, channels, center_hz) for the role's deviceset, or raise."""
    rec = state.read_loaded_profile(role)
    if not rec:
        raise WriteError(400, f"no {role} profile loaded")
    idx = rec.get("deviceset_index", 0)
    sd = client.sampling_device(idx)
    if not sd:
        raise WriteError(503, "SDRangel unreachable")
    hw = sd.get("hwType")
    if hw not in HW_SETTINGS_KEY:
        raise WriteError(503, f"ds{idx} has no real device (hw={hw})")
    center = sd.get("centerFrequency") or (rec.get("center_freq_hz") or 0)
    return idx, hw, client.list_channels(idx), int(center)


def _apply_via_chirp(band: str, gain: float, squelch: float, cutoff, port: int) -> Dict:
    """Send set_sdr_gain + set_global_squelch_dbfs to chirp's UDP cmd port.

    Neptune-side airband/ground run under chirp, not SDRangel. sb3-ui's
    apply_controls historically only knew the SDRangel path; this shim
    routes controls to chirp when the target band is chirp-backed.
    """
    import socket, json as _json
    channels_applied = 0
    errors = []

    def _send(cmd: str, args: dict):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(2.0)
            msg = _json.dumps({"v": 1, "id": cmd, "cmd": cmd, "args": args}) + "\n"
            s.sendto(msg.encode(), ("127.0.0.1", port))
            data, _ = s.recvfrom(4096)
            return _json.loads(data.decode())
        finally:
            s.close()

    try:
        r = _send("set_sdr_gain", {"db": float(gain)})
        if r.get("error"): errors.append(f"gain: {r['error']}")
    except Exception as exc:
        errors.append(f"gain send failed: {exc!r}")
    try:
        r = _send("set_global_squelch_dbfs", {"dbfs": float(squelch)})
        if r.get("error"):
            errors.append(f"squelch: {r['error']}")
        else:
            channels_applied = int((r.get("data") or {}).get("channels_applied", 0))
    except Exception as exc:
        errors.append(f"squelch send failed: {exc!r}")
    return {
        "ok": not errors,
        "applied_gain": gain,
        "applied_squelch_dbfs": squelch,
        "cutoff_hz": cutoff,
        "channels_touched": channels_applied,
        "keepalive_spared": 0,
        "restart_ok": True,
        "backend": f"chirp:{band}",
        "errors": errors,
    }


def apply_controls(form: Dict, state: State, *, with_filter: bool = False) -> Dict:
    """/api/apply and /api/apply-batch — device gain + per-channel squelch (+cutoff).

    Airband/ground on Neptune run under chirp; when a chirp daemon is
    reachable for the requested band, controls are dispatched there
    instead of SDRangel. The keepalive-sparing SDRangel path remains for
    bands that still use SDRangel.
    """
    role = _role_for(form.get("target", "airband"))
    gain = _num(form, "gain", GAIN_MIN, GAIN_MAX)
    squelch = _num(form, "squelch_dbfs", SQUELCH_MIN, SQUELCH_MAX)
    cutoff = _num(form, "cutoff_hz", CUTOFF_MIN, CUTOFF_MAX) if with_filter else None

    # chirp bands: airband on :7400, ground on :7401 (env-overridable).
    import os as _os
    chirp_ports = {
        "air": int(_os.environ.get("SB3_CHIRP_AIRBAND_PORT", "7400")),
        "ground": int(_os.environ.get("SB3_CHIRP_GROUND_PORT", "7401")),
    }
    if role in chirp_ports:
        # Probe cmd port before committing to chirp path; if unreachable, fall
        # through to the SDRangel path for backwards compatibility.
        import socket as _socket
        _probe = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        _probe.settimeout(0.5)
        try:
            import json as _pj
            _probe.sendto((_pj.dumps({"v":1,"id":"p","cmd":"get_status","args":{}}) + "\n").encode(), ("127.0.0.1", chirp_ports[role]))
            _probe.recvfrom(4096)
            _probe.close()
            return _apply_via_chirp(role, gain, squelch, cutoff, chirp_ports[role])
        except Exception:
            _probe.close()
            # fall through to SDRangel path

    c = _client()
    idx, hw, channels, center = _role_deviceset(c, state, role)
    ka_off = _keepalive_offset(state, role, center)

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
    role = _role_for(form.get("target", "airband"))
    cutoff = _num(form, "cutoff_hz", CUTOFF_MIN, CUTOFF_MAX)
    c = _client()
    idx, hw, channels, _ = _role_deviceset(c, state, role)
    touched = 0
    for ch in channels:
        ctype = ch.get("id", "AMDemod")
        if c.patch_channel(idx, ch.get("index"), ctype, ctype + "Settings",
                           {"rfBandwidth": int(cutoff)}):
            touched += 1
    return {"ok": True, "cutoff_hz": cutoff, "channels_touched": touched}


def tune(form: Dict, state: State) -> Dict:
    """/api/tune — retune (freq in MHz).

    Air/Ground are camp mode: fixed channels around one center; 'tune' moves that
    device center, bounds-checked to the band.

    VFO is a single free-tuning receiver (hunt mode): 'tune' moves the LISTEN
    frequency, keeping the DC-spike dodge — device LO = freq + VFO_LO_DODGE_HZ and
    the channel offset = -VFO_LO_DODGE_HZ, so the NFM demod never sits on the
    dongle's center DC spike at any tuned frequency.
    """
    role = _role_for(form.get("target", "airband"))
    if role == "vfo":
        return _tune_vfo(form, state)

    # Chirp-backed bands: dispatch to chirp cmd port (Ground = VFO tune).
    if role in ("air", "ground"):
        band = "airband" if role == "air" else role
        port = _CHIRP_PORT_BY_BAND.get(band)
        if port and _chirp_probe(port):
            lo, hi = (AIRBAND_MIN_HZ, AIRBAND_MAX_HZ) if role == "air" else (VHF_MIN_HZ, VHF_MAX_HZ)
            freq_mhz = _num(form, "freq", lo / 1e6, hi / 1e6)
            result = _tune_chirp_band(role, freq_mhz)
            result["backend"] = f"chirp:{band}"
            return result

    lo, hi = (AIRBAND_MIN_HZ, AIRBAND_MAX_HZ) if role == "air" else (VHF_MIN_HZ, VHF_MAX_HZ)
    freq_mhz = _num(form, "freq", lo / 1e6, hi / 1e6)
    center_hz = int(round(freq_mhz * 1e6))
    c = _client()
    idx, hw, _, _ = _role_deviceset(c, state, role)
    if not c.patch_device(idx, hw, HW_SETTINGS_KEY[hw], {"centerFrequency": center_hz}):
        raise WriteError(503, "tune PATCH failed (SDRangel unhealthy)")
    return {"ok": True, "center_hz": center_hz, "freq_mhz": freq_mhz}


def _tune_vfo(form: Dict, state: State) -> Dict:
    """VFO retune: move device LO + channel offset together (DC-dodge preserved)."""
    freq_mhz = _num(form, "freq", VFO_MIN_HZ / 1e6, VFO_MAX_HZ / 1e6)
    listen_hz = int(round(freq_mhz * 1e6))
    center_hz = listen_hz + VFO_LO_DODGE_HZ
    c = _client()
    idx, hw, channels, _ = _role_deviceset(c, state, "vfo")
    if not channels:
        raise WriteError(503, "VFO deviceset has no channel")
    if not c.patch_device(idx, hw, HW_SETTINGS_KEY[hw], {"centerFrequency": center_hz}):
        raise WriteError(503, "VFO tune device PATCH failed (SDRangel unhealthy)")
    ch = channels[0]
    ctype = ch.get("id", "NFMDemod")
    if not c.patch_channel(idx, ch.get("index"), ctype, ctype + "Settings",
                           {"inputFrequencyOffset": -VFO_LO_DODGE_HZ}):
        raise WriteError(503, "VFO tune channel PATCH failed (SDRangel unhealthy)")
    return {"ok": True, "center_hz": center_hz, "listen_hz": listen_hz,
            "freq_mhz": freq_mhz}


def vfo_mute(form: Dict, state: State) -> Dict:
    """/api/vfo/mute — Stop/Start the VFO by muting DS1 ch0.

    ``state=on``  → audioMute 1 → VFO STOPPED (silent in the shared mount)
    ``state=off`` → audioMute 0 → VFO RUNNING

    Muting the channel rather than stopping the deviceset is deliberate: DS1 is
    left running, so restart is one PATCH with no device re-open, no USB churn,
    and no risk to the shared analog mount that Air's keepalive holds up.

    Idempotent — asking for the state it is already in still returns ok, so a
    double-tap or a retry is harmless. Fail-CLOSED: if the VFO deviceset or its
    channel cannot be resolved, refuse loudly rather than reporting success for
    a write that never landed.
    """
    raw = str(form.get("state", form.get("on", ""))).strip().lower()
    if raw in ("on", "1", "true", "mute", "stop"):
        want_mute = True
    elif raw in ("off", "0", "false", "unmute", "start"):
        want_mute = False
    else:
        raise WriteError(400, "state must be on|off (on = muted/stopped)")

    c = _client()
    idx, _hw, channels, _center = _role_deviceset(c, state, "vfo")
    if not channels:
        raise WriteError(500, f"VFO deviceset ds{idx} has no channel to mute")
    ch = channels[0]
    ch_idx = ch.get("index", 0)
    ctype = ch.get("id", "NFMDemod")

    if not c.patch_channel(idx, ch_idx, ctype, ctype + "Settings",
                           {"audioMute": 1 if want_mute else 0}):
        raise WriteError(503, "audioMute PATCH failed (SDRangel unhealthy)")

    # Read back — never report a write as landed without confirming it.
    body = backends.channel_settings_body(
        backends.sdrangel_channel_settings(idx, ch_idx))
    now_muted = bool(body.get("audioMute"))
    if now_muted != want_mute:
        raise WriteError(503, f"audioMute did not take (wanted {want_mute}, "
                              f"reads {now_muted})")
    return {"ok": True, "muted": now_muted,
            "running": not now_muted, "deviceset_index": idx,
            "channel_index": ch_idx}


def volume(form: Dict, state: State) -> Dict:
    """/api/volume — action=set&level=<0-100> maps to per-channel volume; get reads it."""
    action = form.get("action", "get")
    role = _role_for(form.get("target", "airband"))
    c = _client()
    idx, hw, channels, center = _role_deviceset(c, state, role)
    ka_off = _keepalive_offset(state, role, center)
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


def build_system(state: State) -> Dict:
    """/api/system — host, load, deploy, agent roster. Read-only; enriches the
    system card. Every field is cheap and local (no network, no USB probe).

    Fetched by the UI with `.catch(() => null)`, so a partial payload is safe.
    """
    loaded = set(backends.launchctl_loaded())
    sb3_up = sorted(l for l in loaded if l in ownership.SB3_LAYER)
    backend_up = sorted(l for l in loaded if l in ownership.BACKEND)
    try:
        load1, load5, load15 = os.getloadavg()
    except (OSError, AttributeError):
        load1 = load5 = load15 = None
    dep = gitdeploy.observe(check_remote=False)
    return {
        "ok": True,
        "server_time": _now_iso(),
        "host": socket.gethostname(),
        "platform": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "load_avg": None if load1 is None else [round(load1, 2), round(load5, 2), round(load15, 2)],
        "cpu_count": os.cpu_count(),
        "deploy": {
            "sha": dep.short_sha,
            "branch": dep.branch,
            "dirty": dep.dirty,
            "killed": state.is_killed(),
        },
        "agents": {
            "sb3_up": sb3_up,
            "sb3_total": len(ownership.SB3_LAYER),
            "backend_up": backend_up,
            "backend_total": len(ownership.BACKEND),
        },
    }


def hp_state(state: State) -> Dict:
    """/api/hp/state — Travel Mode state. SB3 has no HomePatrol location backend,
    so this returns a sane 'off' default (200) that renderTravelMode() reads:
    ZIP '--', button OFF, no last-push line. The location-push write path stays
    unimplemented (POST → 501), so the button reports cleanly when tapped.
    """
    return {
        "ok": True,
        "enabled": False,
        "home": "Nashville",
        "state": {"zip": ""},
        "travel_mode_enabled": False,
        "travel_mode_last_push": None,
    }


# ---- Favorites Builder wizard (location → systems → channels) --------------
# Thin delegators onto sb3.ui.wizard, which serves static countries/states and
# real counties/systems/channels when the HomePatrol dump is present. Kept here
# so server.py routes everything through routes.* uniformly.

def wizard_countries() -> Dict:
    from . import wizard
    return wizard.countries()


def wizard_states(country_id: int) -> Dict:
    from . import wizard
    return wizard.states(country_id)


def wizard_counties(state_id: int) -> Dict:
    from . import wizard
    return wizard.counties(state_id)


def wizard_systems(state_id: int, county_id: int, system_type: str,
                   scope: str) -> Dict:
    from . import wizard
    return wizard.systems(state_id, county_id, system_type, scope)


def wizard_channels(system_type: str, system_id: str, limit: int = 5000) -> Dict:
    from . import wizard
    return wizard.channels(system_type, system_id, limit)


def wizard_scan_state(state: State) -> Dict:
    from . import wizard
    return wizard.scan_state(state)


def wizard_save_profile(payload: Dict, state: State) -> Dict:
    """POST /api/scan/state — turn a wizard channel pick into a real profile.

    Sub-phase 1 is ANALOG ONLY. The payload is
    ``{name, device_serial, channels:[{freq_hz,label}], description?}``.

    Refuses to clobber: an existing file at the target path — in either the repo
    set or the user set — is a 409 naming the path, never an overwrite. Losing a
    profile you spent ten minutes picking, because the name collided, is not a
    recoverable mistake.
    """
    import json as _json
    import os as _os

    from ..profilecmd import user_profile_dir
    from . import profilegen

    try:
        profile = profilegen.build_profile(payload or {})
    except profilegen.GenError as exc:
        raise WriteError(exc.code, exc.reason)

    name = profile["name"]

    # No-clobber, checked against BOTH sets so a user file can never shadow-
    # collide with a curated one either.
    repo_hit = gitdeploy.deploy_root() / "profiles" / f"{name}.json"
    if repo_hit.is_file():
        raise WriteError(409, f"a repo profile named {name!r} already exists "
                              f"({repo_hit}) — choose another name")
    target = user_profile_dir() / f"{name}.json"
    if target.exists():
        raise WriteError(409, f"profile {name!r} already exists at {target} — "
                              f"choose another name or delete that file first")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write via a temp file + rename so a crash mid-write cannot leave a
        # half-profile that `profile load` would then try to parse.
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(_json.dumps(profile, indent=2) + "\n")
        _os.chmod(tmp, 0o644)
        _os.replace(tmp, target)
    except OSError as exc:
        raise WriteError(500, f"could not write {target}: {exc}")

    dev = profilegen.DEVICES[profile["device"]["serial"]]
    return {
        "ok": True,
        "profile_id": name,
        "path": str(target),
        "channels": len(profile["channels"]),
        "center_freq_hz": profile["device"]["center_freq_hz"],
        "sample_rate_hz": profile["device"]["sample_rate_hz"],
        "demod": profile["channels"][0]["demod"],
        "role": profile["role"],
        "deviceset_index": profile["deviceset_index"],
        "replaces": dev["replaces"],
        "load_command": f"sb3-ctl profile load {name} --execute",
    }


def wizard_devices() -> Dict:
    """GET /api/scan/devices — the device dropdown for the save modal."""
    from . import profilegen
    return {"ok": True, "devices": profilegen.describe_devices()}


def ask_claude_stub(form: Dict, state: State) -> Dict:
    """/api/ask-claude — not wired into SB3. Return a graceful 200 the chat panel
    renders as a message turn (data.ok=false → data.error shown), rather than a
    501 the client surfaces as a raw HTTP error.
    """
    return {
        "ok": False,
        "error": "Ask Claude isn't wired into SB3 yet.",
        "session_id": (form or {}).get("session_id", ""),
    }


def hits(state: State) -> Dict:
    """/api/hits — unified recent activity across all bands.

    Reads: chirp-airband hits.jsonl, chirp-ground.hits.jsonl, and op25 digital
    /hits from Venus (SB3_OP25_REMOTE_URL). Merges + sorts by ts descending.
    """
    import json as _json, os as _os, urllib.request as _ur, urllib.error as _ue
    from pathlib import Path as _Path

    LIMIT = 200
    items = []

    def _tail_jsonl(path, band, keep=100):
        try:
            with open(path, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 200_000))
                data = fh.read().decode("utf-8", errors="ignore")
        except OSError:
            return
        lines = data.splitlines()[-keep:]
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                obj = _json.loads(ln)
            except Exception:
                continue
            evt = obj.get("evt")
            if evt and evt != "hit_end":
                continue
            ts_val = obj.get("ts")
            if not ts_val:
                end_ms = obj.get("end_ts_ms") or 0
                ts_val = end_ms / 1000.0 if end_ms else 0.0
            fmhz = float(obj.get("freq_mhz") or 0.0)
            items.append({
                "band": band,
                "source": band,
                "ts": float(ts_val),
                "freq_mhz": fmhz,
                "freq": f"{fmhz:.3f}",
                "channel_id": obj.get("ch") or "",
                "duration_s": float(obj.get("duration_s") or 0.0),
                "peak_dbfs": obj.get("peak_dbfs"),
                "kind": "voice",
            })

    _tail_jsonl(_Path.home() / "Library" / "Logs" / "chirp" / "hits.jsonl", "airband")
    _tail_jsonl(_Path.home() / "Library" / "Logs" / "chirp" / "ground.hits.jsonl", "ground")

    remote = _os.environ.get("SB3_OP25_REMOTE_URL", "").rstrip("/")
    if remote:
        try:
            with _ur.urlopen(f"{remote}/hits?limit=100", timeout=3) as resp:
                d = _json.loads(resp.read().decode("utf-8"))
            for h in d.get("items", []) or []:
                fmhz = float(h.get("freq_mhz") or 0.0)
                tg = h.get("tg")
                items.append({
                    "band": "digital",
                    "source": "digital",
                    "type": "digital",
                    "ts": float(h.get("ts") or 0.0),
                    "freq_mhz": fmhz,
                    "freq": f"{fmhz:.3f}",
                    "channel_id": str(tg or "?"),
                    "tgid": tg,
                    "talkgroup": tg,
                    "rid": h.get("rid"),
                    "slot": h.get("slot"),
                    "prio": h.get("prio"),
                    "kind": "voice",
                })
        except (_ue.URLError, TimeoutError, OSError):
            pass

    items.sort(key=lambda x: x.get("ts") or 0.0, reverse=True)
    return {"ok": True, "items": items[:LIMIT]}


# ---- digital fallback endpoints (Phase 3.3) --------------------------------
# The status loop hits these only when the /api/status snapshot lacks a
# scheduler/preflight block. Minimal valid shapes so the Digital tab degrades
# quietly; SB3 does not (yet) drive SDRTrunk scheduling.

def digital_scheduler(state: State) -> Dict:
    return {"ok": True, "enabled": False, "entries": []}


def digital_preflight(state: State) -> Dict:
    return {"ok": True, "checks": [], "ready": True}


def digital_profiles(state: State) -> Dict:
    """/api/digital/profiles — digital profile registry."""
    import json as _json
    from pathlib import Path as _Path
    from ..profilecmd import user_profile_dir as _user_dir

    profs = state.read_loaded_profiles()
    active = (profs.get("digital") or {}).get("name", "")

    seen = set()
    out = []
    _root = _Path(__file__).resolve().parent.parent.parent
    for source, d in (("repo", _root / "profiles"), ("user", _user_dir())):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.json")):
            stem = f.stem
            if stem in seen:
                continue
            seen.add(stem)
            try:
                data = _json.loads(f.read_text())
            except Exception:
                continue
            if str(data.get("role") or "").strip().lower() != "digital":
                continue
            out.append({
                "id": stem,
                "name": data.get("name") or stem,
                "sub_role": data.get("sub_role") or "",
                "backend": data.get("backend") or "",
                "description": data.get("description") or data.get("_comment", "")[:200],
                "source": source,
            })
    return {"ok": True, "profiles": out, "active_digital_id": active}

def apply_profile(form, state):
    """POST /api/profile/apply — take {name: <profile_name>} and dispatch to
    the analog (chirp) or digital (sdrtrunk) applier based on profile role.
    """
    import json as _json
    from pathlib import Path as _Path
    from ..profilecmd import resolve_profile_path as _resolve

    name = (form.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}

    path = _resolve(name)
    if not path or not path.is_file():
        return {"ok": False, "error": f"profile not found: {name}"}

    try:
        profile = _json.loads(path.read_text())
    except Exception as exc:
        return {"ok": False, "error": f"profile parse failed: {exc}"}

    role = profile.get("role")
    try:
        if role in ("air", "vfo", "ground"):
            from ..chirp_applier import apply_profile_to_chirp
            band = "airband" if role == "air" else role
            return apply_profile_to_chirp(profile, band=band)
        if role == "digital":
            backend = str(profile.get("backend") or "sdrtrunk").strip().lower()
            if backend == "op25":
                from ..op25_applier import apply_digital_profile_op25
                return apply_digital_profile_op25(profile)
            from ..sdrtrunk_applier import apply_digital_profile
            return apply_digital_profile(profile)
        return {"ok": False, "error": f"unsupported role: {role!r}"}
    except Exception as exc:
        return {"ok": False, "error": f"applier raised: {exc}"}


def _hp_state_module():
    from ui import hp_state as _hp
    return _hp


def _hp_state_load():
    return _hp_state_module().HPState.load()


def hp_scan_state_get():
    try:
        state = _hp_state_load()
    except Exception as exc:
        return {"ok": False, "error": "load failed: " + repr(exc), "state": {}}
    return {"ok": True, "state": state.to_dict(), "backend": "sb3", "persisted": True}


def hp_scan_state_save(body):
    try:
        state = _hp_state_load()
    except Exception as exc:
        return {"ok": False, "error": "load failed: " + repr(exc)}
    try:
        from ui import handlers as _handlers
        _handlers._apply_hp_state_form(state, body or {})
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    try:
        state.save()
    except Exception as exc:
        return {"ok": False, "error": "save failed: " + repr(exc)}

    # Runtime sync: push the saved favorites through to whichever runtime
    # backends are wired (rtl-airband configs, chirp, etc.). Best-effort:
    # a sync failure does not fail the save.
    sync_payload = None
    sync_error = None
    try:
        request_id = _handlers._enqueue_favorites_runtime_sync()
        _handlers._wait_for_favorites_runtime_sync(
            request_id, float(getattr(_handlers, "HP_STATE_SYNC_WAIT_SEC", 10.0))
        )
        sync_payload = _handlers._snapshot_favorites_runtime_sync(request_id)
    except Exception as exc:
        sync_error = repr(exc)

    resp = {"ok": True, "state": state.to_dict()}
    if sync_payload is not None:
        resp["favorites_runtime_sync"] = sync_payload
    if sync_error:
        resp["sync_error"] = sync_error
    return resp


def hp_service_types_get():
    try:
        from ui.service_types import get_all_service_types
        return {"ok": True, "service_types": get_all_service_types()}
    except Exception as exc:
        return {"ok": False, "error": "service_types unavailable: " + repr(exc), "service_types": []}


# ---------------------------------------------------------------------------
# Chirp tune helpers (used by /api/tune for chirp-backed bands).
# ---------------------------------------------------------------------------

_CHIRP_PORT_BY_BAND = {"airband": 7400, "ground": 7401}


def _chirp_probe(port: int) -> bool:
    import socket as _s, json as _j
    try:
        sk = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        sk.settimeout(0.5)
        sk.sendto((_j.dumps({"v": 1, "id": "p", "cmd": "get_status", "args": {}}) + "\n").encode(),
                  ("127.0.0.1", port))
        sk.recvfrom(4096)
        sk.close()
        return True
    except Exception:
        return False


def _chirp_send(port: int, cmd: str, args: dict, timeout: float = 3.0) -> dict:
    import socket as _s, json as _j
    sk = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
    try:
        sk.settimeout(timeout)
        sk.sendto(_j.dumps({"v": 1, "id": cmd, "cmd": cmd, "args": args}).encode(),
                  ("127.0.0.1", port))
        data, _ = sk.recvfrom(65535)
        return _j.loads(data)
    finally:
        sk.close()


def _tune_chirp_band(role: str, freq_mhz: float) -> Dict:
    """VFO-style hot-tune on a chirp band: replace non-keepalive channels
    with one 'vfo' channel at freq_mhz. Keepalive is preserved so the mount
    stays up between transmissions."""
    band = "airband" if role == "air" else role
    port = _CHIRP_PORT_BY_BAND.get(band)
    if not port:
        return {"ok": False, "error": f"no chirp port for band={band!r}"}
    freq_hz = int(round(freq_mhz * 1_000_000))

    try:
        status = _chirp_send(port, "get_status", {})
    except Exception as exc:
        return {"ok": False, "error": f"chirp status failed: {exc!r}"}
    if status.get("status") != "ok":
        return {"ok": False, "error": f"chirp status non-ok: {status}"}

    channels = status.get("data", {}).get("channels", [])
    if isinstance(channels, dict):
        channel_ids = list(channels.keys())
    else:
        channel_ids = [c.get("id") for c in channels if c.get("id")]

    removed = 0
    for cid in channel_ids:
        cid_str = str(cid or "")
        # Preserve keepalive channels (they hold the mount up during quiet).
        if "keepalive" in cid_str.lower():
            continue
        # Preserve any existing 'vfo' channel — we'll retune it.
        if cid_str == "vfo":
            continue
        try:
            _chirp_send(port, "remove_channel", {"id": cid_str})
            removed += 1
        except Exception:
            pass

    # Retune existing vfo channel OR add a new one.
    if "vfo" in [str(c) for c in channel_ids]:
        r = _chirp_send(port, "set_freq", {"id": "vfo", "freq_hz": freq_hz})
        added_or_moved = "moved"
    else:
        mode = "am" if band == "airband" else "nfm"
        r = _chirp_send(port, "add_channel", {"channels": [{
            "id": "vfo",
            "freq_mhz": round(freq_hz / 1e6, 6),
            "mode": mode,
            "squelch_dbfs": -55.0,
            "gain_db": 3.0,
        }]})
        added_or_moved = "added"

    ok = (r.get("status") == "ok") or (r.get("ok") is True)
    return {
        "ok": bool(ok),
        "freq_hz": freq_hz,
        "freq_mhz": round(freq_hz / 1e6, 6),
        "removed_channels": removed,
        "vfo_channel": added_or_moved,
        "chirp_response": r,
    }
