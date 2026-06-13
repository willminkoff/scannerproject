#!/usr/bin/env python3
"""Compare scheduler's intended LO vs the live SDR LO.

Disagreement = sdrplay_api silently rejected retunes; explains every
"alive but useless" symptom on airband + ground today.
"""
import sys, time, json
sys.path.insert(0, "/home/ubuntu/scannerproject")
from ui.chirp_adapter import _chirp_client_for

for band in ("airband", "ground"):
    print(f"\n=== {band} ===")
    c = _chirp_client_for(band)
    for tick in range(6):
        d = (c.get_status().get("data") or {})
        src = d.get("source", {}) or {}
        sched_lo = src.get("sdr_center_freq_hz")
        live_lo = src.get("live_center_freq_hz")
        chs = d.get("channels", []) or []
        live_ch = [ch for ch in chs if not ch.get("is_parked")]
        if live_ch:
            first = live_ch[0]
            f_mhz = first.get("freq_mhz")
            offset = first.get("center_freq_offset_hz")
            expected = (f_mhz * 1e6 - (live_lo or 0)) if (f_mhz is not None and live_lo is not None) else None
            print(f"  t={tick} sched_lo={sched_lo} live_lo={live_lo} "
                  f"first_live_ch={f_mhz}MHz offset={offset} expected_offset={expected}")
        else:
            print(f"  t={tick} sched_lo={sched_lo} live_lo={live_lo} (no live channels)")
        time.sleep(3)
