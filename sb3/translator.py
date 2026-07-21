"""sb3.translator — a Profile, translated onto SDRangel.

apply() asserts a profile onto the backend; observe() classifies live state
against a profile; unload() tears SB3's assertions back out. SB3 asserts and
leaves — it does not hold the audio state (§4.2).

Design commitments (§ translator invariants in the Phase 2 brief):
  * device binding is preserved if it already matches — no needless rebind;
  * channels apply keepalive-FIRST so the mount comes up before any squelched
    channel can gate it closed;
  * copyToUDP is toggled 0→1 to actually start the sender;
  * audio_device.strategy=dynamic_idx0 resolves at apply-time to the real idx0
    device name and is recorded for observe/unload;
  * apply VERIFIES the mount reached 200 and UNWINDS cleanly on failure — no
    half-applied state left behind;
  * observe never auto-corrects. Divergence is reported as drift; the human is
    right (§4.4). The reconciler is Phase 3+.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from . import backends
from .profile import Profile
from .sdrangel import SDRangelClient
from .state import State

Emit = Callable[[str], None]

# outcome codes (align with killswitch)
OK = 0
INVARIANT_VIOLATED = 1
REFUSED = 3

MOUNT_VERIFY_TRIES = 4
MOUNT_VERIFY_GAP = 3.0


def _channel_settings(prof: Profile, ch, audio_name: str) -> dict:
    s = {
        "inputFrequencyOffset": ch.offset_from(prof.center_freq_hz),
        "rfBandwidth": ch.rf_bw_hz,
        "squelch": ch.squelch_db,
        "volume": ch.volume,
        "title": ch.title,
        "audioDeviceName": audio_name,
        "audioMute": 0,
    }
    # NFM (Ground WX) carries an audio-filter bandwidth AM does not.
    if ch.demod == "NFM":
        s["afBandwidth"] = 3000
    return s


def _device_settings(prof: Profile) -> dict:
    s = {
        "centerFrequency": prof.center_freq_hz,
        "devSampleRate": prof.sample_rate_hz,
        "log2Decim": prof.log2_decim,
        "agc": 1 if prof.agc else 0,
    }
    if prof.hardware_id == "RTLSDR":
        s["gain"] = prof.gain_tenths_db
        s["dcBlock"] = 1 if prof.dc_block else 0
    return s


def verify_mount(prof: Profile, *, execute: bool, emit: Emit,
                 tries: int = MOUNT_VERIFY_TRIES, gap: float = MOUNT_VERIFY_GAP,
                 sleep=None) -> bool:
    """Mount 200 is the end-to-end proof: device → copyToUDP → bridge → icecast.

    Direct UDP byte-counting on :9998 is not available to us — the audio bridge
    (ffmpeg) already owns that socket, so we cannot bind it to sniff. The mount
    reaching 200 is the honest composite proof that samples flowed all the way
    through; icecast only creates the mount once the bridge connects, which only
    happens once real bytes arrive on the UDP tap.
    """
    if not execute:
        emit(f"would: verify mount {prof.mount} → 200 "
             f"(and icecast source connected)")
        return True
    _sleep = sleep if sleep is not None else time.sleep
    for i in range(tries):
        _sleep(gap)
        st = backends.mount_state(prof.mount)
        if st.present:
            live = prof.mount in backends.icecast_mounts()
            emit(f"    mount {prof.mount}: {st.http_status} "
                 f"(icecast source: {'connected' if live else 'unseen'})")
            return True
        emit(f"    mount {prof.mount}: {st.http_status} (waiting… {i+1}/{tries})")
    return False


def apply(prof: Profile, *, execute: bool, emit: Emit,
          state: Optional[State] = None,
          client: Optional[SDRangelClient] = None) -> int:
    """Translate a profile onto SDRangel and verify the mount. Unwinds on failure."""
    state = state or State()
    c = client or SDRangelClient(execute=execute, emit=lambda m: emit(f"    {m}"))
    idx = prof.deviceset_index

    emit(f"apply profile {prof.name!r} → SDRangel ds{idx} "
         f"({prof.hardware_id} {prof.serial})")
    emit("")

    # 1. Baseline + ownership guard (§ pause-marker interaction).
    #
    # Only ds{idx}'s copyToUDP drives this mount's UDP tap, and apply owns ds{idx},
    # so a live mount does NOT mean "something else is fighting us" — it means our
    # own deviceset is currently producing (possibly leftover/un-owned content
    # that this apply will replace). The real thing to refuse is stomping a
    # DIFFERENT profile that is already loaded and live: unload it first.
    marker = state.marker_present()
    mount_now = backends.mount_state(prof.mount)
    # per-role: only a DIFFERENT profile of the SAME role blocks (Air load must
    # not be blocked by a live Ground, and vice versa — different DS, different
    # mount).
    record = state.read_loaded_profile(prof.role)
    emit(f"  baseline: mount {prof.mount} = {mount_now.http_status}, "
         f"pause marker {'present' if marker else 'ABSENT'}, "
         f"loaded profile = {record.get('name') if record else 'none'}")
    if execute and record and record.get("name") != prof.name and mount_now.present:
        emit(f"  ✗ REFUSING: a different profile ({record.get('name')}) is loaded "
             f"and live on {record.get('mount')}.")
        emit("    Unload it first (`sb3-ctl profile unload …`) — apply will not")
        emit("    silently stomp another active profile.")
        return REFUSED
    if execute and mount_now.present:
        emit(f"  note: {prof.mount} is already live (leftover/un-owned content on "
             f"ds{idx}); this apply will reconfigure ds{idx} and take it over.")
    emit("")

    # 2. Device: rebind only if the bound serial differs (§ preserve binding).
    cur_serial = c.ds_serial(idx) if execute else "<dry-run>"
    if execute and cur_serial == prof.serial:
        emit(f"  device already bound: {prof.serial} on ds{idx} — no rebind")
    else:
        if execute:
            emit(f"  device on ds{idx} is {cur_serial!r}, want {prof.serial} — rebinding")
        if not c.rebind_device(idx, prof.hardware_id, prof.serial):
            emit("  ✗ device rebind failed — aborting, nothing else applied")
            return INVARIANT_VIOLATED

    # 3. Start it, then PATCH settings onto the RUNNING device (rule 1).
    c.run(idx)
    if not c.apply_device_settings(idx, prof.device_settings_key,
                                   _device_settings(prof), prof.hardware_id,
                                   prof.center_freq_hz):
        emit("  ✗ device settings did not converge (center never reached target)")
        emit("    → unwinding")
        _unwind(prof, c, state, emit)
        return INVARIANT_VIOLATED
    emit(f"  device settings applied: {prof.center_freq_hz/1e6:.3f} MHz, "
         f"{prof.sample_rate_hz/1e6:.3f} Msps, gain {prof.gain_tenths_db/10:.1f} dB")
    emit("")

    # 4. Channels: clear leftovers, then apply keepalive-FIRST.
    # Resolve the audio tap even in dry-run — it is a harmless read, and it makes
    # the dry-run trace show the REAL device name a reviewer needs to see.
    audio_name, audio_index = backends.resolve_audio_tap(prof.audio_strategy)
    if audio_name is None:
        if execute:
            emit("  ✗ could not resolve audio tap device — aborting")
            _unwind(prof, c, state, emit)
            return INVARIANT_VIOLATED
        audio_name, audio_index = "<audio tap unresolved>", 0
    emit(f"  audio tap resolved: {audio_name!r} (index {audio_index}, "
         f"strategy {prof.audio_strategy})")

    c.clear_channels(idx)
    order = prof.channels_apply_order()
    emit(f"  applying {len(order)} channels (keepalive first):")
    for ch in order:
        tag = " [keepalive]" if ch.keepalive else ""
        emit(f"    + {ch.title} @ {ch.freq_hz/1e6:.4f} MHz "
             f"sq={ch.squelch_db} vol={ch.volume}{tag}")
        if not c.add_channel(idx, prof.channel_type, prof.settings_key,
                             _channel_settings(prof, ch, audio_name)):
            emit("  ✗ SDRangel went unhealthy mid-channel-add — unwinding")
            _unwind(prof, c, state, emit)
            return INVARIANT_VIOLATED
    emit("")

    # 5. copyToUDP on the resolved tap (single sender) + ensure RUNNING.
    emit(f"  copyToUDP → {prof.udp_address}:{prof.udp_port} on audio index "
         f"{audio_index} (toggle 0→1, others disabled)")
    c.set_copy_to_udp(address=prof.udp_address, port=prof.udp_port,
                      audio_index=audio_index)
    if not c.ensure_running(idx, prof.hardware_id, prof.serial):
        emit("  ✗ device would not stay running — unwinding")
        _unwind(prof, c, state, emit)
        return INVARIANT_VIOLATED
    emit("")

    # 6. Verify the mount before removing the marker or recording state.
    if not verify_mount(prof, execute=execute, emit=emit):
        emit("  ✗ mount did not reach 200 — the audio chain is not proven.")
        emit("    → unwinding, leaving the pause marker in place")
        _unwind(prof, c, state, emit)
        return INVARIANT_VIOLATED
    emit("  ✓ mount verified 200 — audio chain live end to end")
    emit("")

    # 7. Marker removed as the FINAL step, then record the loaded profile.
    if execute:
        if marker:
            state.remove_marker()
            emit(f"  ✓ removed pause marker: {state.pause_marker}")
        state.write_loaded_profile({
            "name": prof.name, "role": prof.role, "mode": prof.mode,
            "deviceset_index": idx, "serial": prof.serial,
            "center_freq_hz": prof.center_freq_hz, "mount": prof.mount,
            "audio_device": audio_name,
            "channel_freqs": [ch.freq_hz for ch in prof.channels],
        })
        emit(f"  ✓ recorded loaded profile: {state.loaded_profile_path}")
    else:
        emit(f"  would: remove pause marker {state.pause_marker} (final step)")
        emit(f"  would: record loaded profile to {state.loaded_profile_path}")

    emit("")
    emit(f"  apply complete: {prof.name} live on ds{idx} → {prof.mount}")
    return OK


def _unwind(prof: Profile, c: SDRangelClient, state: State, emit: Emit) -> None:
    """Best-effort teardown after a failed apply — no half-applied state.

    Deliberately does NOT restore the pause marker here (apply never removed it
    on a failure path — it removes it only as its final success step). It clears
    the channels it was adding and stops the copyToUDP sender.
    """
    emit("  unwinding:")
    c.clear_channels(prof.deviceset_index)
    c.set_copy_to_udp(address=prof.udp_address, port=prof.udp_port)  # will re-arm
    # stop the sender by disabling copyToUDP
    c._req("PATCH", "/audio/output/parameters", {"index": 0, "copyToUDP": 0})
    emit("    channels cleared, copyToUDP stopped")


def unload(prof: Profile, *, execute: bool, emit: Emit,
           state: Optional[State] = None,
           client: Optional[SDRangelClient] = None) -> int:
    """Tear down channels and stop copyToUDP. Does NOT restore the pause marker.

    Restoring the marker is Will's separate concern (§ pause-marker
    interaction). unload leaves SDRangel's device bound and running but silent.
    """
    state = state or State()
    c = client or SDRangelClient(execute=execute, emit=lambda m: emit(f"    {m}"))
    idx = prof.deviceset_index

    emit(f"unload profile {prof.name!r} from ds{idx}")
    emit("")
    emit("  clearing channels:")
    c.clear_channels(idx)
    emit(f"  stopping copyToUDP on :{prof.udp_port}")
    c._req("PATCH", "/audio/output/parameters", {"index": 0, "copyToUDP": 0})
    c._wait(1.0) if hasattr(c, "_wait") else None
    if execute:
        state.clear_loaded_profile(prof.role)
        emit(f"  cleared loaded-profile record: {state.loaded_profile_path}")
    else:
        emit(f"  would: clear loaded-profile record {state.loaded_profile_path}")
    emit("")
    emit("  unload complete. Pause marker NOT restored (Will's call).")
    return OK


# ---------------------------------------------------------------------------
# observe — read-only classification (never corrects)
# ---------------------------------------------------------------------------

def observe(prof: Optional[Profile], *, emit: Emit,
            state: Optional[State] = None, role: Optional[str] = None) -> str:
    """Classify live SDRangel state against a loaded profile. Read-only.

    Returns one of: 'no-profile' | 'matches' | 'drifted' | 'phantom'.
    role selects which loaded record to compare against (Air vs Ground). Never
    auto-corrects — the human is right (§4.4).
    """
    state = state or State()
    record = state.read_loaded_profile(role if role is not None
                                       else (prof.role if prof else None))
    if prof is None and record is None:
        emit("  no profile loaded")
        return "no-profile"

    idx = (prof.deviceset_index if prof
           else int(record.get("deviceset_index", 0)))
    want_serial = prof.serial if prof else record.get("serial")
    want_freqs = ([c.freq_hz for c in prof.channels] if prof
                  else record.get("channel_freqs", []))

    dss = backends.sdrangel_devicesets()
    ds = next((d for d in dss if d.index == idx), None)
    if ds is None:
        emit(f"  ds{idx}: not present")
        return "drifted"
    if ds.is_phantom:
        emit(f"  ds{idx}: PHANTOM ({ds.hw_type}, no real device) — profile not live")
        return "phantom"

    live_serial = ds.serial
    live_chs = backends.sdrangel_channels(idx)
    n_live = len(live_chs)
    n_want = len(want_freqs)

    serial_ok = (live_serial == want_serial)
    count_ok = (n_live == n_want)
    emit(f"  ds{idx}: serial={live_serial} (want {want_serial}: "
         f"{'ok' if serial_ok else 'DRIFT'}), "
         f"channels={n_live}/{n_want} ({'ok' if count_ok else 'DRIFT'}), "
         f"state={ds.state}")
    if serial_ok and count_ok and ds.state == "running":
        emit("  → matches profile")
        return "matches"
    emit("  → DRIFTED from profile (read-only: not auto-corrected; the human is right)")
    return "drifted"
