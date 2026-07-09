#!/usr/bin/env python3
"""
sdrangel-restore.py — idempotent restore of the analog scanner config into SDRangel.

Travel-stability tool: SDRangel keeps its working config in RAM and reverts to a
stale on-disk plist on crash, so airband + 70cm vanish after any crash/reboot.
This script re-applies them deterministically via the REST API, using the only
recipe SDRangel's SDRplayV3 path tolerates: change sample rate GRADUALLY and
add/delete channels ONE AT A TIME with delays (rapid bulk ops crash it).

Idempotent: if a route is already correctly configured and running, it is left
alone (only GETs) — safe to run on a timer as a watchdog.

Routes (see macos/data/analog_scanlists.json — channel freqs are read from there):
  - Airband -> RTL-SDR serial 83241970 ("970 dongle"), 118.925 MHz, 2.4 Msps, AM
  - 70cm    -> RSPduo  serial 1809063632 (RSP-B), 446.1 MHz, 8 Msps (IF 8 MHz), NFM
Digital (MTRTRS/TACN) is SDRTrunk's job and already persists via its playlist.
"""
import json, os, sys, time, urllib.request, urllib.error

BASE = os.environ.get("SDRANGEL_REST", "http://127.0.0.1:8091/sdrangel")
HERE = os.path.dirname(os.path.abspath(__file__))
SCANLISTS = os.path.join(HERE, "..", "data", "analog_scanlists.json")
AUDIO = {"audioDeviceName": "System default device", "audioMute": 0, "volume": 2}

# Deployment constants (the device-level params; channel freqs come from the JSON).
AIRBAND = dict(serial="83241970", hw="RTLSDR", center=118_925_000, prefer_ds=3,
               dev={"deviceHwType": "RTLSDR", "direction": 0,
                    "rtlSdrSettings": {"centerFrequency": 118_925_000, "devSampleRate": 2_400_000,
                                       "gain": 300, "agc": 1, "log2Decim": 0}})
CM70 = dict(serial="1809063632", hw="SDRplayV3", center=444_500_000, prefer_ds=1,
            # 4 MHz @ 444.5 covers 442.5-446.5: repeaters 442.75/442.80 + 446 simplex.
            # (8 MHz pegged ~420% CPU and overloaded the box -> trunk died. 4 MHz ~halves it.)
            # bandwidthIndex 4 = 5 MHz IF (covers the 4 MHz window). Ramp gradually to avoid the crash.
            rate_ramp=[2_000_000, 4_000_000],
            dev=lambda rate: {"deviceHwType": "SDRplayV3", "direction": 0,
                              "sdrPlayV3Settings": {"tuner": 0, "centerFrequency": 444_500_000,
                                                    "devSampleRate": rate, "bandwidthIndex": 4,
                                                    "lnaIndex": 0, "ifAGC": 1, "log2Decim": 0}})

def req(method, path, body=None, timeout=8):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as x:
            raw = x.read()
            return x.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:
        return None, {"error": str(e)}

def alive():
    s, _ = req("GET", "", timeout=10)  # generous: post-boot load can make REST slow
    return s == 200

def device_available(serial):
    """True if SDRangel has enumerated a device whose serial contains `serial`."""
    s, d = req("GET", "/devices?direction=0", timeout=10)
    if s != 200:
        return False
    return any(serial in ((dev.get("serial") or "")) for dev in (d.get("devices") or []))

def wait_device(serial, secs=40):
    for _ in range(secs // 2):
        if device_available(serial):
            return True
        time.sleep(2)
    return False

def log(m): print(m, flush=True)

def wait_rest(secs=120):
    for _ in range(secs // 2):
        if alive(): return True
        time.sleep(2)
    return False

def devicesets():
    _, d = req("GET", "")
    return (d.get("devicesetlist", {}) or {}).get("deviceSets", [])

def ds_serial(ds): return (ds.get("samplingDevice", {}) or {}).get("serial")
def ds_center(ds): return (ds.get("samplingDevice", {}) or {}).get("centerFrequency") or 0
def ds_state(ds):  return (ds.get("samplingDevice", {}) or {}).get("state")

def find_or_assign(serial, hw, prefer_ds, avoid_ds):
    """Return the deviceset index hosting `serial`, assigning a deviceset if needed."""
    sets = devicesets()
    for i, ds in enumerate(sets):
        if ds_serial(ds) == serial:
            return i
    # not assigned anywhere: make sure SDRangel has actually enumerated the device first
    # (a fresh post-boot SDRangel hasn't yet -> PUT 404s and can crash it)
    if not wait_device(serial):
        log(f"    device {serial} not enumerated by SDRangel yet — skipping (retry next cycle)")
        return None
    # pick a target deviceset and PUT the device onto it
    candidates = [prefer_ds] + [i for i in range(len(sets)) if i != prefer_ds]
    for idx in candidates:
        if idx == avoid_ds or idx >= len(sets):
            continue
        log(f"    assigning {hw} {serial} -> ds{idx}")
        req("DELETE", f"/deviceset/{idx}/device/run")  # stop whatever's there
        time.sleep(1)
        s, _ = req("PUT", f"/deviceset/{idx}/device",
                   {"hwType": hw, "serial": serial, "direction": 0})
        time.sleep(3)
        if s in (200, 202) and alive():
            return idx
        log(f"    PUT failed (HTTP {s}) / SDRangel unhealthy; trying next ds")
    return None

def clear_channels(ds_idx):
    _, ds = req("GET", f"/deviceset/{ds_idx}")
    n = len(ds.get("channels", []))
    for i in range(n - 1, -1, -1):
        if not alive(): return False
        req("DELETE", f"/deviceset/{ds_idx}/channel/{i}")
        time.sleep(0.4)
    return True

def add_channel(ds_idx, ctype, settings_key, settings):
    if not alive(): return False
    req("POST", f"/deviceset/{ds_idx}/channel", {"channelType": ctype, "direction": 0})
    time.sleep(0.4)
    _, ds = req("GET", f"/deviceset/{ds_idx}")
    idx = len(ds.get("channels", [])) - 1
    req("PATCH", f"/deviceset/{ds_idx}/channel/{idx}/settings",
        {"channelType": ctype, "direction": 0, settings_key: settings})
    time.sleep(0.4)
    return True

def healthy(ds_idx, serial, center, want_chans):
    sets = devicesets()
    if ds_idx is None or ds_idx >= len(sets): return False
    ds = sets[ds_idx]
    return (ds_serial(ds) == serial and abs(ds_center(ds) - center) < 5000
            and len(ds.get("channels", [])) == want_chans and ds_state(ds) == "running")

def device_center(ds_idx):
    _, s = req("GET", f"/deviceset/{ds_idx}/device/settings")
    for key in ("rtlSdrSettings", "sdrPlayV3Settings"):
        if isinstance(s, dict) and key in s:
            return (s[key] or {}).get("centerFrequency")
    return None

def apply_device_settings(ds_idx, spec, ramp):
    """Apply device settings to the RUNNING device, verifying the center sticks (retry 3x).
    A pre-run PATCH on a freshly-reloaded device is silently ignored, so the caller must
    start the device first."""
    for _ in range(3):
        if not alive(): return False
        if ramp:
            for rate in ramp:
                req("PATCH", f"/deviceset/{ds_idx}/device/settings", spec["dev"](rate))
                time.sleep(3)
        else:
            req("PATCH", f"/deviceset/{ds_idx}/device/settings", spec["dev"])
            time.sleep(2)
        c = device_center(ds_idx)
        if c is not None and abs(c - spec["center"]) < 5000:
            return True
    return False

def load_scanlists():
    with open(SCANLISTS) as f:
        return json.load(f)

def airband_channels(sl):
    C = AIRBAND["center"]
    out = []
    for ch in sl["airband"]["deployed"]:
        f = int(round(ch["mhz"] * 1e6))
        out.append(("AMDemod", "AMDemodSettings",
                    {"inputFrequencyOffset": f - C, "rfBandwidth": 8000, "squelch": -50,
                     "title": f'{ch["label"]} {ch["mhz"]:.3f}', **AUDIO}))
    return out

def cm70_channels(sl):
    C = CM70["center"]
    out = []
    for mhz in sl["70cm"]["simplex"]:
        f = int(round(mhz * 1e6))
        label = "70cm Call 446.000" if abs(mhz - 446.0) < 1e-6 else f"{mhz:.3f}"
        out.append(("NFMDemod", "NFMDemodSettings",
                    {"inputFrequencyOffset": f - C, "rfBandwidth": 12500, "afBandwidth": 3000,
                     "squelch": -77, "title": label, **AUDIO}))
    for rp in sl["70cm"]["repeaters"]:
        f = int(round(rp["output_mhz"] * 1e6))
        out.append(("NFMDemod", "NFMDemodSettings",
                    {"inputFrequencyOffset": f - C, "rfBandwidth": 12500, "afBandwidth": 3000,
                     "squelch": -77, "title": f'{rp["call"]} {rp["output_mhz"]:.3f} ({rp["pl"]})', **AUDIO}))
    return out

def restore_route(name, spec, channels, ramp=None):
    log(f"  [{name}] {len(channels)} channels")
    ds = find_or_assign(spec["serial"], spec["hw"], spec["prefer_ds"], spec.get("avoid"))
    if ds is None:
        log(f"  [{name}] FAILED to assign device {spec['serial']} — skipping")
        return None
    if healthy(ds, spec["serial"], spec["center"], len(channels)):
        log(f"  [{name}] already restored on ds{ds} — leaving it alone")
        return ds
    # Start the device FIRST: settings (notably RTL centerFrequency) only stick reliably
    # when PATCHed against a RUNNING device; a pre-run PATCH on a freshly-reloaded device
    # is silently ignored. The rate ramp (RSPduo) still avoids the wide-channelizer crash
    # because channels aren't added until afterward.
    req("POST", f"/deviceset/{ds}/device/run")
    time.sleep(3)
    if not apply_device_settings(ds, spec, ramp):
        log(f"  [{name}] WARNING: center did not converge to {spec['center']/1e6:.3f} MHz")
    clear_channels(ds)
    for ctype, key, st in channels:
        if not add_channel(ds, ctype, key, st):
            log(f"  [{name}] SDRangel went down mid-restore — aborting route"); return None
    req("POST", f"/deviceset/{ds}/device/run")
    ok = False
    for _ in range(4):  # device state can lag a beat behind the run command
        time.sleep(2)
        if healthy(ds, spec["serial"], spec["center"], len(channels)):
            ok = True; break
    log(f"  [{name}] {'restored OK' if ok else 'restored (verify warned)'} on ds{ds}")
    return ds

def main():
    if not wait_rest():
        log("SDRangel REST not reachable — is SDRangel running? Aborting."); sys.exit(1)
    sl = load_scanlists()
    # AIRBAND-ONLY MODE (2026-06-28): dedicated to aviation for travel; digital (SDRTrunk) and
    # 70cm are stopped to keep the 2018 mini from overloading. To bring 70cm back, restore the
    # two-route version below.
    log("Restoring airband config into SDRangel (airband-only mode)...")
    restore_route("airband", AIRBAND, airband_channels(sl))
    log("Done.")
    # --- 70cm (disabled in airband-only mode) ---
    # air_ds = restore_route("airband", AIRBAND, airband_channels(sl))
    # CM70["avoid"] = air_ds
    # restore_route("70cm", CM70, cm70_channels(sl), ramp=CM70["rate_ramp"])

if __name__ == "__main__":
    main()
