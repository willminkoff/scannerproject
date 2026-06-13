#!/usr/bin/env python3
"""Inspect each airband channel's xlating offset vs. its target freq + the
current LO position.  If offset != (freq - LO), the freq_xlating isn't
shifting to the right place and we'd see noise-floor only.
"""
import sys, json
sys.path.insert(0, "/home/ubuntu/scannerproject")
from ui.chirp_adapter import _chirp_client_for

for band in ("airband",):
    c = _chirp_client_for(band)
    d = (c.get_status().get("data") or {})
    src = d.get("source", {}) or {}
    lo_hz = src.get("sdr_center_freq_hz")
    print(f"=== {band} ===")
    print(f"LO sdr_center_freq_hz: {lo_hz}")
    print(f"source samp_rate: {src.get('samp_rate')}")
    chs = d.get("channels", []) or []
    # Per-channel from get_status (uses _Channel.snapshot())
    for ch in chs:
        # The snapshot key in channel.py is 'center_freq_offset_hz'.
        # Look at all keys to be safe.
        sn = ch
        print(f"  freq_mhz={ch.get('freq_mhz')} keys={sorted(ch.keys())}")
        break
    print("---")
    for ch in chs:
        freq_mhz = ch.get("freq_mhz")
        # The 'snapshot' nested dict, if present.
        snap_offset = ch.get("center_freq_offset_hz")
        parked = ch.get("is_parked")
        lvl = ch.get("signal_level_dbfs")
        # Compute expected offset assuming LO at the current value.
        try:
            expected = float(freq_mhz) * 1e6 - float(lo_hz or 0)
        except Exception:
            expected = None
        print(f"  {freq_mhz:>9.4f} parked={parked} lvl={lvl:.2f}  "
              f"snap_offset={snap_offset} expected={expected}")
