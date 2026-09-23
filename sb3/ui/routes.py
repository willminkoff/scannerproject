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

    # Fetch live chirp squelch/gain from cmd ports (best-effort, 0.5s timeout).
    # Per-port chirp host mapping: airband lives on Venus, ground on Neptune.
    _CHIRP_HOSTS = {7400: "100.114.219.115", 7401: "127.0.0.1"}
    def _chirp_snap(port):
        import socket as _s, json as _j
        try:
            sk = _s.socket(_s.AF_INET, _s.SOCK_DGRAM); sk.settimeout(0.5)
            sk.sendto((_j.dumps({"v":1,"id":"p","cmd":"get_status","args":{}}) + chr(10)).encode(), (_CHIRP_HOSTS.get(port, "127.0.0.1"), port))
            data, _ = sk.recvfrom(65535); sk.close()
            return _j.loads(data.decode("utf-8", errors="ignore")).get("data", {}) or {}
        except Exception:
            return {}
    _air_snap = _chirp_snap(7400)
    _gnd_snap = _chirp_snap(7401)
    _air_sq = _air_snap.get("global_squelch_dbfs")
    _gnd_sq = _gnd_snap.get("global_squelch_dbfs")
    # # __SB3_INTUITIVE_GAIN__ — prefer the new intuitive field from chirp
    _air_amp = None
    _gnd_amp = None
    try:
        _air_amp = (_air_snap.get("source") or {}).get("sdr_gain_amp_db")
    except Exception:
        _air_amp = None
    try:
        _gnd_amp = (_gnd_snap.get("source") or {}).get("sdr_gain_amp_db")
    except Exception:
        _gnd_amp = None
    _air_gain = _LAST_APPLIED_GAIN.get(7400, _air_amp if _air_amp is not None else _air_snap.get("master_gain_db"))
    _gnd_gain = _LAST_APPLIED_GAIN.get(7401, _gnd_amp if _gnd_amp is not None else _gnd_snap.get("master_gain_db"))

    # Chirp-based liveness override: if chirp-ground/airband cmd port answers
    # AND has channels, consider the role live regardless of legacy profile
    # tracking (which was tied to the retired rtl-airband/SDRTrunk flow).
    def _chirp_live(snap):
        if not snap:
            return False
        chans = snap.get("channels", [])
        n = len(chans) if isinstance(chans, list) else len(chans.keys())
        return n > 0
    if _chirp_live(_air_snap):
        air_status = "live"
        air_running = True
    if _chirp_live(_gnd_snap):
        ground_status = "live"
        ground_running = True

    return {
        "ok": True,
        "airband_squelch_dbfs": float(_air_sq) if _air_sq is not None else None,
        "airband_applied_squelch_dbfs": float(_air_sq) if _air_sq is not None else None,
        "airband_gain": float(_air_gain) if _air_gain is not None else None,
        "airband_applied_gain": float(_air_gain) if _air_gain is not None else None,
        "airband_gain_db": float(_air_gain) if _air_gain is not None else None,
        "airband_applied_gain_db": float(_air_gain) if _air_gain is not None else None,
        "ground_squelch_dbfs": float(_gnd_sq) if _gnd_sq is not None else None,
        "ground_applied_squelch_dbfs": float(_gnd_sq) if _gnd_sq is not None else None,
        "ground_gain": float(_gnd_gain) if _gnd_gain is not None else None,
        "ground_applied_gain": float(_gnd_gain) if _gnd_gain is not None else None,
        "ground_gain_db": float(_gnd_gain) if _gnd_gain is not None else None,
        "ground_applied_gain_db": float(_gnd_gain) if _gnd_gain is not None else None,
        "server_time": _now_iso(),
        # analog presence/activity (Air role is on RTL; Ground not deployed yet)
        # 'present' reflects live chirp instance with channels loaded.
        # sdrangel_up / ground_profile are stale signals from before
        # Neptune consolidated on the chirp path.
        "airband_present": _chirp_live(_air_snap) or sdrangel_up,
        "airband_active": air_running,
        "ground_present": _chirp_live(_gnd_snap) or bool(ground_profile),
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
            s.sendto(msg.encode(), (_chirp_host(port), port))
            data, _ = s.recvfrom(4096)
            return _json.loads(data.decode())
        finally:
            s.close()

    try:
        # # __SB3_INTUITIVE_GAIN__
        # sb3-ui now speaks the SDRangel-style intuitive scale
        # (0=min amp .. 59=max amp). Chirp will convert to IFGR
        # internally. Fall back to legacy set_sdr_gain if the
        # daemon does not yet expose the new command (e.g. it
        # is running an older build).
        _amp_val = float(gain)
        _amp_val = max(0.0, min(59.0, _amp_val))
        r = _send("set_sdr_amp_db", {"db": _amp_val})
        if r.get("status") == "rejected" and (r.get("error") or "").startswith("unknown cmd"):
            # older chirp — send inverted IFGR value
            r = _send("set_sdr_gain", {"db": 59.0 - _amp_val})
        if not r.get("error"):
            _LAST_APPLIED_GAIN[int(port)] = _amp_val
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
            _probe.sendto((_pj.dumps({"v":1,"id":"p","cmd":"get_status","args":{}}) + "\n").encode(), (_chirp_host(chirp_ports[role]), chirp_ports[role]))
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
    """/api/system — host, load, deploy, agent roster + sitrep telemetry.

    Every field is cheap and local (no network, no USB probe). Optional
    telemetry (memory / disk / uptime / cpu_usage) uses only stdlib and
    shell fallbacks so no new dependencies are pulled in. Fields that can't
    be filled cheaply on macOS (cpu_temp_c) are simply omitted; the UI
    renders '--' for anything missing.
    """
    import shutil as _shutil
    import subprocess as _sp
    import re as _re

    loaded = set(backends.launchctl_loaded())
    sb3_up = sorted(l for l in loaded if l in ownership.SB3_LAYER)
    backend_up = sorted(l for l in loaded if l in ownership.BACKEND)
    try:
        load1, load5, load15 = os.getloadavg()
    except (OSError, AttributeError):
        load1 = load5 = load15 = None
    dep = gitdeploy.observe(check_remote=False)

    # ------ memory (macOS vm_stat + sysctl) ------
    memory = None
    try:
        # Total physical bytes
        total_bytes = int(_sp.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip())
        # vm_stat pages
        pagesize = 4096
        try:
            pagesize = int(_sp.run(
                ["sysctl", "-n", "hw.pagesize"],
                capture_output=True, text=True, timeout=2,
            ).stdout.strip())
        except Exception:
            pass
        vm = _sp.run(["vm_stat"], capture_output=True, text=True, timeout=2).stdout
        counts = {}
        for line in vm.splitlines():
            m = _re.match(r"([A-Za-z ()\-]+):\s+(\d+)\.", line)
            if m:
                counts[m.group(1).strip()] = int(m.group(2)) * pagesize
        # macOS 'used' ≈ wired + active + compressed
        wired = counts.get("Pages wired down", 0)
        active = counts.get("Pages active", 0)
        compressed = counts.get("Pages occupied by compressor", 0)
        used_bytes = wired + active + compressed
        total_kb = total_bytes // 1024
        used_kb = used_bytes // 1024
        used_percent = (used_bytes / total_bytes * 100.0) if total_bytes else 0.0
        memory = {
            "total_kb": total_kb,
            "used_kb": used_kb,
            "used_percent": round(used_percent, 1),
        }
    except Exception:
        memory = None

    # ------ disk (/) ------
    disk = None
    try:
        du = _shutil.disk_usage("/")
        disk = {
            "total_bytes": int(du.total),
            "used_bytes": int(du.used),
            "free_bytes": int(du.free),
            "used_percent": round(du.used / du.total * 100.0, 1) if du.total else 0.0,
        }
    except Exception:
        disk = None

    # ------ uptime ------
    uptime_s = None
    try:
        bt = _sp.run(
            ["sysctl", "-n", "kern.boottime"],
            capture_output=True, text=True, timeout=2,
        ).stdout
        m = _re.search(r"sec\s*=\s*(\d+)", bt)
        if m:
            import time as _t
            uptime_s = int(_t.time()) - int(m.group(1))
    except Exception:
        uptime_s = None

    # ------ cpu_usage (rough: iostat -c 2 diff) ------
    cpu_usage = None
    try:
        out = _sp.run(
            ["iostat", "-c", "2", "-w", "1"],
            capture_output=True, text=True, timeout=4,
        ).stdout
        # last row has us sy id — cpu_usage = us + sy
        rows = [ln.split() for ln in out.strip().splitlines()
                if ln.strip() and ln.strip()[0].isdigit()]
        if rows:
            last = rows[-1]
            # iostat -c format: ... us sy id (last 3 tokens)
            try:
                us = float(last[-3]); sy = float(last[-2])
                cpu_usage = round(us + sy, 1)
            except Exception:
                cpu_usage = None
    except Exception:
        cpu_usage = None

    return {
        "ok": True,
        "server_time": _now_iso(),
        "host": socket.gethostname(),
        "platform": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        # Legacy list shape for the old system card, PLUS object shape for
        # the sitrep telemetry. Neither caller has to change.
        "load_avg": (None if load1 is None else {
            "one": round(load1, 2),
            "five": round(load5, 2),
            "fifteen": round(load15, 2),
        }),
        "load_avg_list": None if load1 is None else [round(load1, 2), round(load5, 2), round(load15, 2)],
        "cpu_count": os.cpu_count(),
        "cpu_usage": cpu_usage,
        "memory": memory,
        "disk": disk,
        "uptime_s": uptime_s,
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




from pathlib import Path as _PathTop
_HP_STATE_PATH = _PathTop("/Users/willminkoff/scannerproject/data/hp_state.json")


def _build_tg_label_map():
    """Read hp_state.json custom_favorites → {tgid_str: alpha_tag}.

    Cached module-side would be nicer but this file is <100KB so re-reading
    it per /api/hits call is fine and keeps stale-cache bugs out.
    """
    import json as _j
    try:
        d = _j.loads(_HP_STATE_PATH.read_text())
    except Exception:
        return {}
    m = {}
    for cf in d.get("custom_favorites", []) or []:
        tg = str(cf.get("talkgroup") or "").strip()
        alpha = str(cf.get("alpha_tag") or "").strip()
        if tg and alpha:
            m[tg] = alpha
    return m


def _fmt_hit_time(ts):
    import time as _t
    try:
        if not ts:
            return ""
        return _t.strftime("%H:%M:%S", _t.localtime(float(ts)))
    except Exception:
        return ""




def _pretty_chirp_label(ch):
    """Turn a chirp channel id into a human label.

    Examples:
        n4-0-tower                         -> Tower
        n4-3-mtears-nashville-(davidson-co) -> Mtears Nashville (Davidson Co)
        kbna-tower                         -> Kbna Tower
    """
    import re as _re
    s = str(ch or "").strip()
    if not s:
        return ""
    s = _re.sub(r"^n\d+-\d+-", "", s)
    s = s.replace("-", " ")
    words = []
    for w in s.split():
        if w.startswith("(") and w.endswith(")"):
            inner = w[1:-1]
            words.append("(" + " ".join(x.capitalize() for x in inner.split(" ")) + ")")
        else:
            words.append(w.capitalize())
    return " ".join(words)


def hits(state: State) -> Dict:
    """/api/hits — unified recent activity across all bands.

    Reads: chirp-airband hits.jsonl, chirp-ground.hits.jsonl, and op25 digital
    /hits from Venus (SB3_OP25_REMOTE_URL). Merges + sorts by ts descending.
    """
    import json as _json, os as _os, urllib.request as _ur, urllib.error as _ue
    from pathlib import Path as _Path

    LIMIT = 200
    items = []
    _tg_label_map = _build_tg_label_map()

    def _tail_jsonl(path, band, keep=100):
        # Accept either a filesystem path or an http:// URL (used for cross-host
        # airband log tail from Venus).
        try:
            if isinstance(path, str) and path.startswith("http"):
                import urllib.request as _ur
                with _ur.urlopen(path, timeout=3) as resp:
                    data = resp.read().decode("utf-8", errors="ignore")
            else:
                with open(path, "rb") as fh:
                    fh.seek(0, 2)
                    size = fh.tell()
                    fh.seek(max(0, size - 200_000))
                    data = fh.read().decode("utf-8", errors="ignore")
        except (OSError, Exception):
            return
        # Filter for hit_start events first, THEN keep the last N. Otherwise
        # cluster_hop noise dominates the tail and hit_starts get truncated.
        all_lines = data.splitlines()
        hit_lines = [ln for ln in all_lines if b"hit_start" in ln.encode() or "hit_start" in ln]
        lines = hit_lines[-keep:]
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                obj = _json.loads(ln)
            except Exception:
                continue
            evt = obj.get("evt")
            if evt != "hit_start":
                continue
            ts_val = obj.get("ts")
            if not ts_val:
                end_ms = obj.get("end_ts_ms") or 0
                ts_val = end_ms / 1000.0 if end_ms else 0.0
            fmhz = float(obj.get("freq_mhz") or 0.0)
            _ch = obj.get("ch") or ""
            _dur = float(obj.get("duration_s") or 0.0)
            _pretty = _pretty_chirp_label(_ch)
            items.append({
                "band": band,
                "source": band,
                "ts": float(ts_val),
                "time": _fmt_hit_time(ts_val),
                "duration": round(_dur, 1),
                "duration_s": _dur,
                "label": _pretty,
                "label_full": _pretty,
                "freq_mhz": fmhz,
                "freq": f"{fmhz:.3f}",
                "channel_id": _ch,
                "duration_s": float(obj.get("duration_s") or 0.0),
                "peak_dbfs": obj.get("peak_dbfs"),
                "kind": "voice",
            })

    _tail_jsonl(
        _os.environ.get(
            "SB3_CHIRP_AIRBAND_LOG_URL",
            "http://100.114.219.115:9200/chirp/airband.out.log?tail=200000",
        ),
        "airband",
    )
    _tail_jsonl(_Path.home() / "Library" / "Logs" / "chirp" / "ground.out.log", "ground")

    remote = _os.environ.get("SB3_OP25_REMOTE_URL", "").rstrip("/")
    if remote:
        try:
            with _ur.urlopen(f"{remote}/hits?limit=100", timeout=3) as resp:
                d = _json.loads(resp.read().decode("utf-8"))
            for h in d.get("items", []) or []:
                fmhz = float(h.get("freq_mhz") or 0.0)
                tg = h.get("tg")
                tg_label = _tg_label_map.get(str(tg) or "", "")
                _display = tg_label or (f"TG {tg}" if tg else "")
                _ts = float(h.get("ts") or 0.0)
                items.append({
                    "band": "digital",
                    "source": "digital",
                    "type": "digital",
                    "ts": _ts,
                    "time": _fmt_hit_time(_ts),
                    "duration": 0,
                    "label": _display,
                    "label_full": _display,
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




# Constants for the HP -> chirp/op25 sync.
_HP_SYNC_MTRTRS_CCS = [856.4875, 856.7125, 857.0375, 857.4875]
_HP_SYNC_TACN_CCS = [852.9875, 853.7375]
_HP_SYNC_KNOWN_TRUNKED = {
    "7078": {"name": "MTRTRS", "ccs": _HP_SYNC_MTRTRS_CCS,
             "tuner": "RSPduo Tuner 1 SER#1809063632"},
    "6355": {"name": "TACN",   "ccs": _HP_SYNC_TACN_CCS,
             "tuner": "RSPduo Tuner 2 SER#1809063632"},
}


def _hp_extract_pool(state_obj):
    """Split HPState custom_favorites into airband / ground / digital groups."""
    airs, grounds = [], []
    digital_by_system = {}
    for cf in state_obj.get("custom_favorites", []) or []:
        kind = cf.get("kind")
        if kind == "trunked":
            sid = str(cf.get("system_id") or "")
            entry = digital_by_system.setdefault(sid, {"name": cf.get("system_name",""), "tgs": [], "ccs": []})
            # Capture control_channels from the first fav that has them.
            _ccs = cf.get("control_channels") or []
            if _ccs and not entry.get("ccs"):
                try:
                    entry["ccs"] = [float(f) for f in _ccs if f]
                except Exception:
                    pass
            try:
                dec = int(cf.get("talkgroup"))
            except Exception:
                continue
            entry["tgs"].append({
                "dec": dec,
                "alpha": cf.get("alpha_tag", ""),
                "description": cf.get("department_name", ""),
            })
        elif kind == "conventional":
            try:
                freq_mhz = float(cf.get("frequency") or 0)
            except Exception:
                continue
            label = cf.get("alpha_tag", "")
            if 108 <= freq_mhz <= 137:
                airs.append({"freq_mhz": freq_mhz, "label": label})
            elif 137 < freq_mhz <= 175 or 400 <= freq_mhz <= 470:
                grounds.append({"freq_mhz": freq_mhz, "label": label})
    return airs, grounds, digital_by_system


def _hp_push_chirp(port, freqs, mode):
    """Remove all non-keepalive/vfo channels then add fresh N4-derived ones."""
    import socket as _s, json as _j
    _hosts = {7400: "100.114.219.115", 7401: "127.0.0.1"}
    _host = _hosts.get(port, "127.0.0.1")
    def send(cmd, args, timeout=3.0):
        sk = _s.socket(_s.AF_INET, _s.SOCK_DGRAM); sk.settimeout(timeout)
        try:
            sk.sendto(_j.dumps({"v": 1, "id": cmd, "cmd": cmd, "args": args}).encode(),
                      (_host, port))
            data, _ = sk.recvfrom(65535)
            return _j.loads(data)
        finally:
            sk.close()
    try:
        st = send("get_status", {})
    except Exception as exc:
        return {"ok": False, "error": "chirp %s:%d unreachable: %r" % (_host, port, exc)}
    chans = st.get("data", {}).get("channels", [])
    cur_ids = list(chans.keys()) if isinstance(chans, dict) else [c.get("id") for c in chans if c.get("id")]
    removed = 0
    for cid in cur_ids:
        cid_str = str(cid or "")
        if "keepalive" in cid_str.lower() or cid_str == "vfo":
            continue
        try:
            send("remove_channel", {"id": cid_str})
            removed += 1
        except Exception:
            pass
    if not freqs:
        return {"ok": True, "removed": removed, "added": 0}
    ch_list = []
    for i, f in enumerate(freqs):
        safe = str(f.get("label", "")).lower().replace(" ", "-").replace("/", "-")[:30]
        ch_list.append({
            "id": "n4-%d-%s" % (i, safe),
            "freq_mhz": round(float(f.get("freq_mhz")), 6),
            "mode": mode,
            "squelch_dbfs": -40.0 if mode == "am" else -55.0,
            "gain_db": 3.0,
        })
    # Chirp ground caps at max_channels=16 (minus 1 for vfo/keepalive), so any
    # add_channel batch beyond that is a silent drop. Trim to fit.
    ch_list = ch_list[:15]
    r = send("add_channel", {"channels": ch_list})
    return {"ok": r.get("status") == "ok", "removed": removed, "added": len(ch_list), "chirp": r}


def _hp_push_op25(digital_by_system):
    """POST a digital profile blob to Venus op25-log-server /apply-profile."""
    import json as _j, urllib.request as _ur, os as _os
    blob = {"name": "hp-auto", "systems": [], "talkgroups": [], "op25_overrides": {},
            "dongle_assignments": [], "activate": True}
    for sid, info in (digital_by_system or {}).items():
        known = _HP_SYNC_KNOWN_TRUNKED.get(sid)
        if known:
            sysname = known["name"]
            ccs = known["ccs"]
            tuner = known.get("tuner")
        else:
            sysname = (info.get("name") or f"sys-{sid}").strip() or f"sys-{sid}"
            ccs = info.get("ccs") or []
            # Default the tuner for unknown systems to Tuner 1 of the digital
            # RSPduo (same tuner MTRTRS uses when home). Op25 process planner
            # requires every system to have a dongle assignment.
            tuner = "RSPduo Tuner 1 SER#1809063632"
            if not ccs:
                # No control channels available for this unknown system;
                # can't build a P25 profile entry for it, skip.
                continue
        blob["systems"].append({"name": sysname, "control_channels_mhz": ccs})
        blob["op25_overrides"][sysname] = {"gains": "IFGR:20,RFGR:0"}
        if tuner:
            blob["dongle_assignments"].append({"system_name": sysname, "preferred_tuner_serial": tuner})
        for tg in info["tgs"]:
            blob["talkgroups"].append({
                "dec": tg["dec"], "hex": format(tg["dec"], "X"), "mode": "D",
                "alpha": tg["alpha"], "description": tg["description"],
            })
    if not blob["systems"]:
        return {"ok": True, "skipped": "no known digital systems in favorites"}
    target = _os.environ.get("SB3_OP25_APPLY_URL", "http://100.114.219.115:9200").rstrip("/")
    req = _ur.Request(target + "/apply-profile", data=_j.dumps(blob).encode(),
                      method="POST", headers={"Content-Type": "application/json"})
    try:
        with _ur.urlopen(req, timeout=45) as resp:
            return _j.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": "op25 push failed: %r" % (exc,)}


def _hp_auto_sync(state_obj):
    """Given the HPState dict, push its favorites out to chirp + op25."""
    airs, grounds, digital = _hp_extract_pool(state_obj)
    return {
        "airband": _hp_push_chirp(7400, airs, "am"),
        "ground":  _hp_push_chirp(7401, grounds, "nfm"),
        "digital": _hp_push_op25(digital),
    }


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

    # Auto-sync: push the just-saved favorites into chirp (airband+ground)
    # and op25 (digital via Venus). Best-effort — sync failure does not
    # fail the save. Replaces the legacy sync_scan_pool_to_runtime path
    # which targeted /usr/local/etc rtl-airband configs that no longer
    # exist on this stack.
    sync_result = None
    try:
        sync_result = _hp_auto_sync(state.to_dict())
    except Exception as exc:
        sync_result = {"ok": False, "error": repr(exc)}

    resp = {"ok": True, "state": state.to_dict()}
    if sync_result is not None:
        resp["auto_sync"] = sync_result
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

_LAST_APPLIED_GAIN = {}  # {port: db} module state for gain UI reconciliation


_CHIRP_PORT_BY_BAND = {"airband": 7400, "ground": 7401}
# Chirp cmd hosts per port: airband lives on Venus, ground stays local.
_CHIRP_HOSTS_BY_PORT = {7400: "100.114.219.115", 7401: "127.0.0.1"}

def _chirp_host(port):
    return _CHIRP_HOSTS_BY_PORT.get(int(port), "127.0.0.1")


def _chirp_probe(port: int) -> bool:
    import socket as _s, json as _j
    try:
        sk = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        sk.settimeout(0.5)
        sk.sendto((_j.dumps({"v": 1, "id": "p", "cmd": "get_status", "args": {}}) + "\n").encode(),
                  (_chirp_host(port), port))
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
                  (_chirp_host(port), port))
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


def build_subsystems():
    """/api/subsystems - probe the chirp+op25 stack and return per-service
    {state,detail,note} entries the sitrep cards paint from.

    state = "good" | "bad" | "unknown"
    """
    import json as _json, socket as _sock, time as _time, urllib.request as _ur, urllib.error as _ue
    out = {}

    def _card(s, detail="", note=""):
        return {"state": s, "detail": detail, "note": note}

    def _udp_status(port):
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
        try:
            s.settimeout(1.0)
            s.sendto((_json.dumps({"v":1,"id":"p","cmd":"get_status","args":{}}) + "\n").encode(),
                     (_chirp_host(port), port))
            data, _ = s.recvfrom(65535)
            return _json.loads(data.decode("utf-8", errors="ignore"))
        except Exception:
            return None
        finally:
            s.close()

    # sb3-ui (self) is up if we're serving this endpoint.
    out["sb3ui"] = _card("good", "serving")

    for band, port, key in (("airband", 7400, "chirp_airband"), ("ground", 7401, "chirp_ground")):
        st = _udp_status(port)
        if not st or st.get("status") != "ok":
            out[key] = _card("bad", "no cmd port")
            continue
        data = st.get("data", {}) or {}
        chans = data.get("channels", [])
        n = len(chans) if isinstance(chans, list) else len(chans.keys())
        lo = data.get("lo_scheduler", {}) or {}
        plan_fail = lo.get("plan_failed_reason")
        clusters = lo.get("n_clusters")
        hold = lo.get("scan_hold_state")
        detail = f"{n} ch / {clusters or 0} clusters" + (f" / {hold}" if hold else "")
        note = f"plan failed: {plan_fail}" if plan_fail else ""
        out[key] = _card("bad" if plan_fail else "good", detail, note)

    # Op25 via Venus log-server /health + /hits recency.
    remote = os.environ.get("SB3_OP25_REMOTE_URL", "").rstrip("/")
    if remote:
        try:
            with _ur.urlopen(f"{remote}/health", timeout=2) as resp:
                h = _json.loads(resp.read())
            with _ur.urlopen(f"{remote}/hits?limit=1", timeout=2) as resp:
                hits = _json.loads(resp.read()).get("items", [])
            active = hits[0].get("ts") if hits else 0
            age = _time.time() - float(active or 0)
            active_str = f"last hit {int(age)}s ago" if hits else "no recent hits"
            out["op25"] = _card("good", active_str, f"active profile: {h.get('active_profile','')[-32:]}")
            out["op25_audio"] = _card("good", "publishing (venus)")
        except Exception as exc:
            out["op25"] = _card("bad", f"log-server unreachable: {exc!r}")
            out["op25_audio"] = _card("bad", "log-server unreachable")
    else:
        out["op25"] = _card("unknown", "SB3_OP25_REMOTE_URL not set")
        out["op25_audio"] = _card("unknown", "SB3_OP25_REMOTE_URL not set")

    # Icecast mounts.
    try:
        with _ur.urlopen("http://127.0.0.1:8000/status-json.xsl", timeout=2) as resp:
            d = _json.loads(resp.read())
        s = d.get("icestats", {}).get("source", [])
        s = [s] if isinstance(s, dict) else s
        names = [str(x.get("listenurl","")).rsplit("/",1)[-1] for x in s]
        want = ["neptune-analog.mp3", "neptune-ground.mp3", "venus-digital.mp3"]
        missing = [w for w in want if w not in names]
        detail = f"{len(names)} mounts"
        note = "missing: " + ", ".join(missing) if missing else ""
        out["icecast"] = _card("bad" if missing else "good", detail, note)
    except Exception as exc:
        out["icecast"] = _card("bad", f"unreachable: {exc!r}")

    # ACARS + VDL2 via launchctl. Simple check on process existence.
    import subprocess
    def _launchd_loaded(label):
        try:
            r = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=3)
            return label in r.stdout
        except Exception:
            return None

    for name, label in (("acars", "com.scannerproject.acarsdec"),
                        ("vdl2",  "com.scannerproject.dumpvdl2")):
        loaded = _launchd_loaded(label)
        if loaded is None:
            out[name] = _card("unknown", "launchctl unavailable")
        elif loaded:
            out[name] = _card("good", "running")
        else:
            out[name] = _card("bad", "not loaded")

    # Disco on Venus (dashboard at :8092 on Venus).
    disco_url = os.environ.get("SB3_DISCO_URL", "http://100.114.219.115:8092")
    try:
        with _ur.urlopen(disco_url, timeout=2) as resp:
            code = getattr(resp, "status", 200)
        out["disco"] = _card("good" if 200 <= code < 400 else "bad", f"HTTP {code}")
    except Exception as exc:
        out["disco"] = _card("bad", f"unreachable: {exc!r}")

    # ADS-B panel - placeholder until wired.
    out["adsb"] = _card("unknown", "not probed")

    return {"ok": True, "subsystems": out}

# ---------------------------------------------------------------------------
# WX / ACARS-VDL2 pane endpoints.
# ---------------------------------------------------------------------------

from pathlib import Path as _WxPath  # noqa: E402
_WX_ACARS_DIR = _WxPath("/Users/willminkoff/Library/Logs/acars")
_WX_VDL2_DIR = _WxPath("/Users/willminkoff/Library/Logs/vdl2")


def _wx_newest_jsonl(dir_path: _WxPath, limit_files: int = 2):
    """Return the newest N non-empty *.jsonl files in a log dir, sorted newest last."""
    try:
        files = [p for p in dir_path.glob("*.jsonl") if p.is_file() and p.stat().st_size > 0]
    except OSError:
        return []
    files.sort(key=lambda p: p.stat().st_mtime)
    return files[-limit_files:]


def _wx_tail_jsonl(paths, tail_bytes: int = 500_000):
    """Read the tail of the given files and return a list of parsed JSON objs."""
    import json as _json
    out = []
    for p in paths:
        try:
            with open(p, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - tail_bytes))
                data = fh.read().decode("utf-8", errors="ignore")
        except OSError:
            continue
        for ln in data.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(_json.loads(ln))
            except Exception:
                continue
    return out


def _wx_is_daemon_running(pattern: str) -> bool:
    """True if any process command line contains the given substring."""
    import subprocess as _sp
    try:
        r = _sp.run(["/bin/ps", "-eo", "command"], capture_output=True, text=True, timeout=2)
        return pattern in r.stdout
    except Exception:
        return False


def _wx_msg_is_met(obj: dict) -> bool:
    """Heuristic: does this ACARS/VDL2 payload carry meteorological data?

    AMDAR reports include altitude+wind+temp fields; BUFR blobs carry a marker.
    Keep this cheap; the sidecar shows the flag next to each row.
    """
    if not isinstance(obj, dict):
        return False
    for k in ("temp_c", "temperature_c", "wind_speed_kt", "wind_dir_deg",
              "altitude_ft", "pressure_hpa", "amdar", "bufr"):
        if k in obj:
            return True
    text = str(obj.get("text") or obj.get("message") or "")
    return "AMDAR" in text or "BUFR" in text


# __WX_SOUNDING_WIRED__ 2026-09-22
# Wire ui/wxdata.py MetStore + acars_reader_worker into sb3-ui so
# /api/wx/sounding returns real AMDAR-derived vertical profiles.
_MET_STORE = None
_MET_READER_THREAD = None
_MET_STOP_EVENT = None


def _wx_get_store():
    """Lazy-init MetStore + reader thread on first call. False on failure."""
    global _MET_STORE, _MET_READER_THREAD, _MET_STOP_EVENT
    if _MET_STORE is not None:
        return _MET_STORE if _MET_STORE is not False else None
    try:
        _PATH = "/Users/willminkoff/Library/Logs/acars/acars.jsonl"
        import sys as _sys
        _pp = "/Users/willminkoff/scannerproject"
        if _pp not in _sys.path:
            _sys.path.insert(0, _pp)
        # Overwrite the constant AFTER import to defeat env-var timing races.
        from ui import config as _uicfg
        _uicfg.ACARS_OUTPUT_PATH = _PATH
        from ui import wxdata as _wx
        _wx.ACARS_OUTPUT_PATH = _PATH  # wxdata copied it at import time
        import threading as _th

        _MET_STORE = _wx.MetStore(max_messages=2000, max_met=500)
        _MET_STORE.collecting = True
        _MET_STORE.active_decoder = "acars"

        # __WX_STORE_PRIME__ — seed the store with observations from the
        # existing acars.jsonl so the sidecar has data immediately rather
        # than waiting 30 min for the tail-from-end reader to catch a rare
        # AMDAR match. Parser hit-rate on real Nashville traffic is around
        # 5-10 percent, so this typically primes 20-50 observations from
        # a day of decoded messages.
        try:
            import json as _json
            import os as _osp
            _prime_count = 0
            _prime_msgs = 0
            if _osp.path.exists(_PATH):
                with open(_PATH, "r", encoding="utf-8", errors="ignore") as _fh:
                    _lines = _fh.readlines()
                # Prime from the last N lines to avoid multi-MB replays.
                for _ln in _lines[-2000:]:
                    _ln = _ln.strip()
                    if not _ln:
                        continue
                    try:
                        _msg = _json.loads(_ln)
                    except Exception:
                        continue
                    try:
                        _raw, _obs_list = _wx.parse_acars_message(_msg)
                    except Exception:
                        continue
                    _MET_STORE.add_message(_raw)
                    _prime_msgs += 1
                    for _o in _obs_list:
                        if _MET_STORE.add_observation(_o):
                            _prime_count += 1
            import sys as _sysp
            print(
                f"[wx] primed store: {_prime_msgs} msgs, {_prime_count} obs "
                f"from {_PATH}",
                file=_sysp.stderr,
            )
        except Exception as _pe:
            import traceback as _tbp
            _tbp.print_exc()

        _MET_STOP_EVENT = _th.Event()
        _MET_READER_THREAD = _th.Thread(
            target=_wx.acars_reader_worker,
            args=(_MET_STORE, _MET_STOP_EVENT),
            daemon=True,
            name="wx-acars-reader",
        )
        _MET_READER_THREAD.start()
        return _MET_STORE
    except Exception as _exc:
        import traceback as _tb
        _tb.print_exc()
        _MET_STORE = False
        return None


def wx_status(state: State) -> Dict:
    """/api/wx/status — daemon health + running message counts."""
    acars_up = _wx_is_daemon_running("acarsdec")
    vdl2_up = _wx_is_daemon_running("dumpvdl2")
    # Cheap count: tail 200 KB of the newest file per stream.
    acars_msgs = _wx_tail_jsonl(_wx_newest_jsonl(_WX_ACARS_DIR, 1), tail_bytes=200_000)
    vdl2_msgs = _wx_tail_jsonl(_wx_newest_jsonl(_WX_VDL2_DIR, 1), tail_bytes=200_000)
    total = len(acars_msgs) + len(vdl2_msgs)
    met = sum(1 for m in acars_msgs if _wx_msg_is_met(m)) + sum(
        1 for m in vdl2_msgs if _wx_msg_is_met(m)
    )
    # active_decoder is presentational: acars if either daemon is up.
    active = "acars" if (acars_up or vdl2_up) else None
    return {
        "ok": True,
        "active_decoder": active,
        "collecting": bool(acars_up or vdl2_up),
        "acars_running": acars_up,
        "vdl2_running": vdl2_up,
        "message_count": total,
        "met_count": met,
        "filtered_count": 0,
        "spatial_filter": False,
    }


def wx_messages(state: State, limit: int = 100) -> Dict:
    """/api/wx/messages — merged tail of the newest ACARS + VDL2 jsonl files."""
    try:
        limit = int(limit)
    except Exception:
        limit = 100
    limit = max(1, min(500, limit))

    acars = _wx_tail_jsonl(_wx_newest_jsonl(_WX_ACARS_DIR, 48))
    vdl2 = _wx_tail_jsonl(_wx_newest_jsonl(_WX_VDL2_DIR, 48))

    rows = []
    for m in acars:
        ts = m.get("timestamp") or m.get("ts") or 0
        try:
            ts = float(ts)
        except Exception:
            ts = 0
        rows.append({
            "timestamp": ts,
            "source": "acars",
            "source_id": m.get("flight") or m.get("tail") or m.get("reg") or "",
            "is_met": _wx_msg_is_met(m),
            "text": str(m.get("text") or m.get("message") or m.get("depa", "")
                        + ("->" + m.get("dsta", "") if m.get("dsta") else ""))[:200],
        })
    for m in vdl2:
        # dumpvdl2 nests payload under vdl2 → avlc → acars/xid
        ts = m.get("vdl2", {}).get("t", {}).get("sec") or m.get("timestamp") or 0
        try:
            ts = float(ts)
        except Exception:
            ts = 0
        payload = m.get("vdl2", {}).get("avlc", {}).get("acars", {}) or {}
        sid = payload.get("flight") or payload.get("reg") or ""
        txt = (payload.get("msg_text") or "").replace(chr(10), " ")[:200]
        rows.append({
            "timestamp": ts,
            "source": "vdl2",
            "source_id": sid,
            "is_met": _wx_msg_is_met(payload) or _wx_msg_is_met(m),
            "text": txt,
        })

    # Sort by ts ascending (UI reads newest-last), keep last N.
    rows.sort(key=lambda r: r["timestamp"])
    return {"ok": True, "messages": rows[-limit:]}


def wx_filter(form: Dict, state: State) -> Dict:
    """/api/wx/filter — enable/disable spatial cylinder filter on the sounding.

    Body params:
      enabled=true|false  (required)
      lat, lon            (decimal degrees; required when enabled=true)
      radius_nm           (default 100 nm)
      ceiling_ft          (default 50000 ft)
    """
    store = _wx_get_store()
    if not store:
        return {"ok": False, "error": "sounding store not initialized"}
    enabled = str(form.get("enabled", "")).strip().lower() in ("1", "true", "yes", "on")
    if not enabled:
        try:
            store.clear_spatial_filter()
            return {"ok": True, "enabled": False}
        except Exception as exc:
            return {"ok": False, "error": f"clear failed: {exc!r}"}
    try:
        lat = float(form.get("lat", ""))
        lon = float(form.get("lon", ""))
    except (TypeError, ValueError):
        return {"ok": False, "error": "lat/lon required and must be numeric"}
    try:
        radius_nm = float(form.get("radius_nm", 100.0))
    except (TypeError, ValueError):
        radius_nm = 100.0
    try:
        ceiling_ft = float(form.get("ceiling_ft", 50000.0))
    except (TypeError, ValueError):
        ceiling_ft = 50000.0
    try:
        store.set_spatial_filter(lat, lon, radius_nm=radius_nm,
                                 ceiling_ft=ceiling_ft, user_set=True)
        return {"ok": True, "enabled": True, "lat": lat, "lon": lon,
                "radius_nm": radius_nm, "ceiling_ft": ceiling_ft}
    except Exception as exc:
        return {"ok": False, "error": f"filter set failed: {exc!r}"}


def wx_sounding(state: State) -> Dict:
    """/api/wx/sounding — vertical profile from AMDAR ACARS messages.

    Applies (a) an age cutoff so only recent observations show up (upper-
    level winds change hour-by-hour, so old data is misleading) and
    (b) the spatial filter if the operator has one enabled.
    """
    store = _wx_get_store()
    if not store:
        return {"ok": True, "levels": []}
    try:
        import os as _os_age, time as _t_age
        try:
            _max_age_h = float(_os_age.environ.get("WX_SOUNDING_MAX_AGE_HOURS", "3"))
        except (TypeError, ValueError):
            _max_age_h = 3.0
        _cutoff_ts = _t_age.time() - (_max_age_h * 3600.0)
        data = store.get_sounding_data()
        # Age filter first
        _pre = len(data.get("levels", []))
        data["levels"] = [
            lvl for lvl in data.get("levels", [])
            if lvl.get("timestamp", 0) >= _cutoff_ts
        ]
        data["observations"] = len(data["levels"])
        data["max_age_hours"] = _max_age_h
        data["filtered_by_age"] = _pre - data["observations"]
        try:
            if getattr(store, "_filter_enabled", False):
                from ui.wxdata import haversine_nm as _hav
                flat = store._filter_lat
                flon = store._filter_lon
                fr = store._filter_radius_nm
                fc = store._filter_ceiling_ft
                kept = []
                for lvl in data.get("levels", []):
                    if lvl.get("altitude_ft", 0) > fc:
                        continue
                    d = _hav(flat, flon, lvl.get("lat", 0.0), lvl.get("lon", 0.0))
                    if d <= fr:
                        kept.append(lvl)
                data["levels"] = kept
                data["observations"] = len(kept)
                data["spatial_filter"] = True
                data["filter_center"] = {"lat": flat, "lon": flon,
                                          "radius_nm": fr, "ceiling_ft": fc}
        except Exception:
            pass
        return {"ok": True, **data}
    except Exception as exc:
        return {"ok": False, "error": f"sounding error: {exc!r}", "levels": []}


def wx_decoder(form: Dict, state: State) -> Dict:
    """/api/wx/decoder — start/stop is a no-op in the current stack.

    acarsdec + dumpvdl2 are launchd-managed and always running; the button
    exists only to open the sidecar. Report the same status wx_status does.
    """
    action = str((form or {}).get("action", "")).lower()
    st = wx_status(state)
    st["accepted"] = action in ("start", "stop", "")
    st["note"] = "acarsdec + dumpvdl2 are always-on launchd services; no start/stop needed"
    return st

# ---------------------------------------------------------------------------
# Dongle power + auto-schedule endpoints.
# ---------------------------------------------------------------------------

_POWER_STATE_PATH = _WxPath("/Users/willminkoff/scannerproject/data/power_state.json")
_POWER_SCHEDULE_PATH = _WxPath("/Users/willminkoff/scannerproject/data/power_schedule.json")
_VENUS_POWER_URL = "http://100.114.219.115:9200"


_NEPTUNE_POWER_OFF_LABELS = [
    "com.scannerproject.dumpvdl2",
    "com.scannerproject.acarsdec",
    "com.scannerproject.chirp-ground",
]
_NEPTUNE_POWER_ON_ORDER = [
    "com.scannerproject.chirp-ground",
    "com.scannerproject.acarsdec",
    "com.scannerproject.dumpvdl2",
]


def _uid():
    import os as _os
    return _os.getuid()


def _lc_run(argv, timeout=10):
    import subprocess as _sp
    try:
        r = _sp.run(argv, capture_output=True, text=True, timeout=timeout)
        return {"cmd": " ".join(argv), "rc": r.returncode, "out": (r.stdout or "")[-200:], "err": (r.stderr or "")[-200:]}
    except Exception as exc:
        return {"cmd": " ".join(argv), "rc": -1, "err": repr(exc)}


def _neptune_power_off():
    lines = []
    for label in _NEPTUNE_POWER_OFF_LABELS:
        lines.append(_lc_run(["launchctl", "bootout", f"gui/{_uid()}/{label}"]))
    return lines


def _neptune_power_on():
    lines = []
    import time as _t
    for label in _NEPTUNE_POWER_ON_ORDER:
        plist = f"/Users/willminkoff/Library/LaunchAgents/{label}.plist"
        lines.append(_lc_run(["launchctl", "bootstrap", f"gui/{_uid()}", plist]))
        _t.sleep(3)
    return lines


def _venus_power(action):
    import urllib.request as _ur, json as _j
    url = f"{_VENUS_POWER_URL}/power/{action}"
    try:
        req = _ur.Request(url, data=b"", method="POST")
        with _ur.urlopen(req, timeout=45) as resp:
            return _j.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": f"venus /power/{action} failed: {exc!r}"}


def _power_state_load():
    import json as _j
    try:
        return _j.loads(_POWER_STATE_PATH.read_text())
    except Exception:
        return {"state": "on"}


def _power_state_save(state):
    import json as _j
    _POWER_STATE_PATH.write_text(_j.dumps({"state": state}))


def _power_schedule_load():
    import json as _j
    try:
        d = _j.loads(_POWER_SCHEDULE_PATH.read_text())
        return {
            "enabled": bool(d.get("enabled", False)),
            "auto_off": str(d.get("auto_off", "")),
            "auto_on": str(d.get("auto_on", "")),
        }
    except Exception:
        return {"enabled": False, "auto_off": "", "auto_on": ""}


def _power_schedule_save(sched):
    import json as _j
    _POWER_SCHEDULE_PATH.write_text(_j.dumps(sched, indent=2))


def dongles_power(form, state):
    """POST /api/dongles/power  action=off|on|status."""
    action = str((form or {}).get("action", "status")).lower()
    if action == "status":
        st = _power_state_load()
        st["ok"] = True
        st["schedule"] = _power_schedule_load()
        return st
    if action not in ("off", "on"):
        return {"ok": False, "error": f"unknown action: {action!r}"}
    # Order matters: on Off, quit Neptune consumers first, then Venus tail.
    # On On, wake Venus (RSPduo firmware needs settle) before Neptune probes it.
    lines = []
    if action == "off":
        lines += _neptune_power_off()
        vr = _venus_power("off")
        lines.append({"cmd": "venus /power/off", "rc": 0 if vr.get("ok") else -1, "err": vr.get("error", "")})
    else:
        vr = _venus_power("on")
        lines.append({"cmd": "venus /power/on", "rc": 0 if vr.get("ok") else -1, "err": vr.get("error", "")})
        lines += _neptune_power_on()
    _power_state_save(action)
    return {"ok": True, "state": action, "lines": lines, "schedule": _power_schedule_load()}


def dongles_power_schedule(form, state):
    """POST /api/dongles/power/schedule  enabled=1&auto_off=HH:MM&auto_on=HH:MM."""
    enabled_raw = str((form or {}).get("enabled", "0")).lower()
    enabled = enabled_raw in ("1", "true", "on", "yes")
    auto_off = str((form or {}).get("auto_off", "")).strip()
    auto_on = str((form or {}).get("auto_on", "")).strip()
    def _valid(t):
        if not t:
            return True
        parts = t.split(":")
        if len(parts) != 2:
            return False
        try:
            h, m = int(parts[0]), int(parts[1])
            return 0 <= h < 24 and 0 <= m < 60
        except Exception:
            return False
    if not _valid(auto_off) or not _valid(auto_on):
        return {"ok": False, "error": "auto_off/auto_on must be HH:MM"}
    sched = {"enabled": enabled, "auto_off": auto_off, "auto_on": auto_on}
    _power_schedule_save(sched)
    _ensure_power_schedule_thread()
    return {"ok": True, "schedule": sched}


_POWER_SCHEDULE_THREAD_STARTED = False


def _ensure_power_schedule_thread():
    """Idempotently start the background schedule enforcer."""
    global _POWER_SCHEDULE_THREAD_STARTED
    if _POWER_SCHEDULE_THREAD_STARTED:
        return
    import threading, time as _t
    def _loop():
        last_fired_minute = None
        while True:
            try:
                sched = _power_schedule_load()
                if sched.get("enabled"):
                    import datetime as _dt
                    now = _dt.datetime.now()
                    cur = now.strftime("%H:%M")
                    if cur != last_fired_minute:
                        current_state = _power_state_load().get("state", "on")
                        if sched.get("auto_off") == cur and current_state != "off":
                            dongles_power({"action": "off"}, None)
                            last_fired_minute = cur
                        elif sched.get("auto_on") == cur and current_state != "on":
                            dongles_power({"action": "on"}, None)
                            last_fired_minute = cur
            except Exception:
                pass
            _t.sleep(30)
    t = threading.Thread(target=_loop, daemon=True, name="power-schedule")
    t.start()
    _POWER_SCHEDULE_THREAD_STARTED = True

# ---------------------------------------------------------------------------
# Band-scan direct-to-chirp handler (bypasses the profile registry, which
# has no create/save endpoints wired up in sb3-ui 3.1). See UAT bug: Rail
# button snapped back to VFO because ensureBandScanProfile's create/save
# calls both 501'd, so /api/profile then couldn't find the profile.
# ---------------------------------------------------------------------------

def bandscan_apply(form, state):
    """POST /api/bandscan/apply — push a band-scan preset straight to chirp.

    form fields:
        target        "air" / "airband" / "ground"
        freqs_text    comma- or newline-separated MHz values
        modulation    "am" | "nfm"  (default: am for airband, nfm for ground)
        bandwidth     kHz (informational; chirp uses its own cluster defaults)
        preset_id     optional label prefix for the created channels
    """
    import socket as _s
    import json as _j

    target_raw = str((form or {}).get("target", "")).strip().lower()
    if target_raw in ("air", "airband"):
        target = "airband"
        port = 7400
        host = "100.114.219.115"  # venus (chirp-airband lives here now)
        default_mode = "am"
    elif target_raw == "ground":
        target = "ground"
        port = 7401
        host = "127.0.0.1"
        default_mode = "nfm"
    else:
        return {"ok": False, "error": f"unknown target: {target_raw!r}"}

    mode = str((form or {}).get("modulation", default_mode)).strip().lower() or default_mode
    preset_id = str((form or {}).get("preset_id", "bandscan")).strip() or "bandscan"

    freqs_raw = str((form or {}).get("freqs_text", "")).strip()
    freqs = []
    for tok in freqs_raw.replace("\n", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            freqs.append(float(tok))
        except Exception:
            continue
    if not freqs:
        return {"ok": False, "error": "no frequencies parsed from freqs_text"}
    freqs = freqs[:15]  # chirp caps channels at 16; leave one slot for VFO

    def _send(cmd, args, timeout=3.0):
        sk = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        sk.settimeout(timeout)
        try:
            sk.sendto(_j.dumps({"v": 1, "id": cmd, "cmd": cmd, "args": args}).encode(),
                      (host, port))
            data, _ = sk.recvfrom(65535)
            return _j.loads(data)
        finally:
            sk.close()

    try:
        st = _send("get_status", {})
    except Exception as exc:
        return {"ok": False, "error": f"chirp {host}:{port} unreachable: {exc!r}"}

    chans = st.get("data", {}).get("channels", [])
    if isinstance(chans, dict):
        cur_ids = list(chans.keys())
    else:
        cur_ids = [c.get("id") for c in chans if isinstance(c, dict) and c.get("id")]
    removed = 0
    for cid in cur_ids:
        cid_s = str(cid or "")
        if not cid_s:
            continue
        if "keepalive" in cid_s.lower() or cid_s.lower() == "vfo":
            continue
        try:
            _send("remove_channel", {"id": cid_s})
            removed += 1
        except Exception:
            pass

    safe_label = "".join(c for c in preset_id if c.isalnum() or c == "-")[:20] or "bs"
    ch_list = []
    for i, f in enumerate(freqs):
        ch_list.append({
            "id": f"bs-{safe_label}-{i}",
            "freq_mhz": round(float(f), 6),
            "mode": mode,
            "squelch_dbfs": -55.0 if mode == "am" else -75.0,
            "gain_db": 3.0,
        })
    try:
        r = _send("add_channel", {"channels": ch_list})
    except Exception as exc:
        return {"ok": False, "error": f"add_channel failed: {exc!r}", "removed": removed}
    return {
        "ok": r.get("status") == "ok",
        "target": target,
        "added": len(ch_list),
        "removed": removed,
        "chirp": r,
    }


def profile_create_stub(form, state):
    """POST /api/profile/create — no-op (profile registry is unimplemented).

    Legacy client code posts here before /api/profile-editor/analog/save.
    The band-scan flow now bypasses the whole chain (see bandscan_apply),
    but returning 200 keeps any straggler callers happy instead of 501.
    """
    return {"ok": True, "stub": True}


def profile_editor_analog_save_stub(form, state):
    """POST /api/profile-editor/analog/save — no-op (see profile_create_stub)."""
    return {"ok": True, "stub": True}

