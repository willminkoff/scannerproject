#!/usr/bin/env python3
"""
sdrangel-restore.py — idempotent restore of the Venus analog scanner config into SDRangel.

Travel-stability tool: SDRangel keeps its working config in RAM and reverts to a stale
on-disk plist on crash/restart, so the airband + 70cm scanner (and, critically, the fixed
gain that keeps the noise floor below squelch) vanish after any crash. This script
re-applies them deterministically via the REST API, using the only recipe SDRangel's
SDRplayV3 path tolerates: PATCH a RUNNING device, and add/delete channels ONE AT A TIME
with delays (rapid bulk ops crash it).

Idempotent: if a route already matches the desired device + center + channels + RUNNING
state AND the fixed gain AND the channel volume, it is left alone (only GETs) — safe to run
on a timer / from the copytoudp watchdog. The gain + volume checks matter: a stale-plist
reload can restore the right topology but the old ifAGC=1 gain (noise floor floats up over
squelch -> continuous hiss) or the old volume, so those are verified, not just the topology.

Venus config (2026-07 — devices are swapped vs the older SB7 mapping):
  - AIRBAND -> RSPduo SDRplayV3 serial 1809063632, 118.925 MHz, 2 Msps.
      Fixed gain ifAGC=0 / ifGain=-40 / lnaIndex=3 (floor ~-67 dB, well below squelch -40).
      4x AMDemod: 118.400 / 118.600 / 119.350 / 119.450, 8 kHz, squelch -40, VOLUME 4.5.
  - NFM     -> RTL-SDR serial 45469635, 444 MHz, 1.024 Msps, fixed gain 28 dB (gain=280), agc off.
      1x NFMDemod at 443.975 (offset -25 kHz), 12.5 kHz, squelch -60, volume 2.0.
  Every channel outputs to "System default device" so the audio mixes into the idx-0
  copyToUDP tap that macos/bin/venus-audio-bridge.sh streams to icecast (the tap itself is
  re-armed independently by macos/bin/copytoudp-watchdog.sh). Digital (SDRTrunk) is separate.
"""
import json, os, sys, time, urllib.request, urllib.error

BASE = os.environ.get("SDRANGEL_REST", "http://127.0.0.1:8091/sdrangel")
AUDIO = {"audioDeviceName": "System default device", "audioMute": 0}

AIRBAND = dict(
    name="airband", serial="1809063632", hw="SDRplayV3", center=118_925_000, prefer_ds=0,
    dev={"deviceHwType": "SDRplayV3", "direction": 0,
         "sdrPlayV3Settings": {"tuner": 0, "centerFrequency": 118_925_000, "devSampleRate": 2_000_000,
                               "bandwidthIndex": 3, "ifAGC": 0, "ifGain": -40, "lnaIndex": 3,
                               "log2Decim": 0}},
    gain_check={"ifAGC": 0, "ifGain": -40, "lnaIndex": 3},
    ch_type="AMDemod", ch_key="AMDemodSettings", ch_volume=4.5,
    channels=[(-525_000, "118.400 KBNA App/Dep East"), (-325_000, "118.600 KBNA Tower"),
              (425_000, "119.350 KBNA App/Dep West"), (525_000, "119.450 KJWN Tower")],
    ch_settings=lambda off, title: {"inputFrequencyOffset": off, "rfBandwidth": 8000,
                                    "squelch": -40, "volume": 4.5, "title": title, **AUDIO},
)
NFM = dict(
    name="nfm", serial="45469635", hw="RTLSDR", center=444_000_000, prefer_ds=1,
    dev={"deviceHwType": "RTLSDR", "direction": 0,
         "rtlSdrSettings": {"centerFrequency": 444_000_000, "devSampleRate": 1_024_000,
                            "gain": 280, "agc": 0, "log2Decim": 0}},
    gain_check={"gain": 280, "agc": 0},
    ch_type="NFMDemod", ch_key="NFMDemodSettings", ch_volume=2.0,
    channels=[(-25_000, "443.975 NFM")],
    ch_settings=lambda off, title: {"inputFrequencyOffset": off, "rfBandwidth": 12500,
                                    "afBandwidth": 3000, "squelch": -60, "volume": 2.0,
                                    "title": title, **AUDIO},
)
ROUTES = [AIRBAND, NFM]


def log(m): print(m, flush=True)


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
    s, _ = req("GET", "", timeout=10)
    return s == 200


def wait_rest(secs=120):
    for _ in range(secs // 2):
        if alive():
            return True
        time.sleep(2)
    return False


def device_available(serial):
    s, d = req("GET", "/devices?direction=0", timeout=10)
    return s == 200 and any(serial in (dev.get("serial") or "") for dev in (d.get("devices") or []))


def wait_device(serial, secs=40):
    for _ in range(secs // 2):
        if device_available(serial):
            return True
        time.sleep(2)
    return False


def devicesets():
    _, d = req("GET", "")
    return (d.get("devicesetlist", {}) or {}).get("deviceSets", [])


def ds_serial(ds): return (ds.get("samplingDevice", {}) or {}).get("serial")
def ds_center(ds): return (ds.get("samplingDevice", {}) or {}).get("centerFrequency") or 0
def ds_state(ds):  return (ds.get("samplingDevice", {}) or {}).get("state")


def dev_block(ds_idx):
    _, s = req("GET", f"/deviceset/{ds_idx}/device/settings")
    for key in ("sdrPlayV3Settings", "rtlSdrSettings"):
        if isinstance(s, dict) and key in s:
            return s[key] or {}
    return {}


def device_center(ds_idx):
    return dev_block(ds_idx).get("centerFrequency")


def ch_volume(ds_idx, ch_key, ch_idx=0):
    _, d = req("GET", f"/deviceset/{ds_idx}/channel/{ch_idx}/settings")
    return (d.get(ch_key, {}) or {}).get("volume")


def find_or_assign(serial, hw, prefer_ds):
    sets = devicesets()
    for i, ds in enumerate(sets):
        if ds_serial(ds) == serial:
            return i
    if not wait_device(serial):
        log(f"    device {serial} not enumerated by SDRangel yet — skipping (retry next cycle)")
        return None
    candidates = [prefer_ds] + [i for i in range(len(sets)) if i != prefer_ds]
    for idx in candidates:
        if idx >= len(sets):
            continue
        log(f"    assigning {hw} {serial} -> ds{idx}")
        req("DELETE", f"/deviceset/{idx}/device/run")
        time.sleep(1)
        s, _ = req("PUT", f"/deviceset/{idx}/device", {"hwType": hw, "serial": serial, "direction": 0})
        time.sleep(3)
        if s in (200, 202) and alive():
            return idx
        log(f"    PUT failed (HTTP {s}) / SDRangel unhealthy; trying next ds")
    return None


def clear_channels(ds_idx):
    _, ds = req("GET", f"/deviceset/{ds_idx}")
    for i in range(len(ds.get("channels", [])) - 1, -1, -1):
        if not alive():
            return False
        req("DELETE", f"/deviceset/{ds_idx}/channel/{i}")
        time.sleep(0.4)
    return True


def add_channel(ds_idx, ctype, key, settings):
    if not alive():
        return False
    req("POST", f"/deviceset/{ds_idx}/channel", {"channelType": ctype, "direction": 0})
    time.sleep(0.4)
    _, ds = req("GET", f"/deviceset/{ds_idx}")
    idx = len(ds.get("channels", [])) - 1
    req("PATCH", f"/deviceset/{ds_idx}/channel/{idx}/settings",
        {"channelType": ctype, "direction": 0, key: settings})
    time.sleep(0.4)
    return True


def apply_device_settings(ds_idx, spec):
    """PATCH device settings (gain + center) to the RUNNING device; verify the center sticks.
    A pre-run PATCH on a freshly-reloaded device is silently ignored, so start it first."""
    for _ in range(3):
        if not alive():
            return False
        req("PATCH", f"/deviceset/{ds_idx}/device/settings", spec["dev"])
        time.sleep(2)
        c = device_center(ds_idx)
        if c is not None and abs(c - spec["center"]) < 5000:
            return True
    return False


def route_healthy(ds_idx, spec):
    """Fully idempotent: topology AND fixed gain AND channel volume must all match desired."""
    sets = devicesets()
    if ds_idx is None or ds_idx >= len(sets):
        return False
    ds = sets[ds_idx]
    if not (ds_serial(ds) == spec["serial"] and abs(ds_center(ds) - spec["center"]) < 5000
            and len(ds.get("channels", [])) == len(spec["channels"]) and ds_state(ds) == "running"):
        return False
    blk = dev_block(ds_idx)
    if any(blk.get(k) != v for k, v in spec["gain_check"].items()):
        return False  # stale plist reloaded the old gain -> re-apply
    v = ch_volume(ds_idx, spec["ch_key"])
    if v is None or abs(float(v) - spec["ch_volume"]) > 0.01:
        return False  # volume drifted
    return True


def restore_route(spec):
    name = spec["name"]
    log(f"  [{name}] {len(spec['channels'])} channel(s), device {spec['serial']}")
    ds = find_or_assign(spec["serial"], spec["hw"], spec["prefer_ds"])
    if ds is None:
        log(f"  [{name}] FAILED to assign device — skipping")
        return None
    if route_healthy(ds, spec):
        log(f"  [{name}] already correct on ds{ds} (topology + gain + volume) — leaving it alone")
        return ds
    log(f"  [{name}] drift detected on ds{ds} — restoring")
    req("POST", f"/deviceset/{ds}/device/run")
    time.sleep(3)
    if not apply_device_settings(ds, spec):
        log(f"  [{name}] WARNING: center did not converge to {spec['center']/1e6:.3f} MHz")
    clear_channels(ds)
    for off, title in spec["channels"]:
        if not add_channel(ds, spec["ch_type"], spec["ch_key"], spec["ch_settings"](off, title)):
            log(f"  [{name}] SDRangel went down mid-restore — aborting route")
            return None
    req("POST", f"/deviceset/{ds}/device/run")
    ok = False
    for _ in range(4):
        time.sleep(2)
        if route_healthy(ds, spec):
            ok = True
            break
    log(f"  [{name}] {'restored OK' if ok else 'restored (verify warned)'} on ds{ds}")
    return ds


def main():
    if not wait_rest():
        log("SDRangel REST not reachable — is SDRangel running? Aborting.")
        sys.exit(1)
    log("Restoring Venus analog scanner config into SDRangel (airband + 443.975 NFM)...")
    for spec in ROUTES:
        restore_route(spec)
    log("Done.")


if __name__ == "__main__":
    main()
