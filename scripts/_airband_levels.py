#!/usr/bin/env python3
"""Read live signal_level_dbfs from each airband channel."""
import sys, time
sys.path.insert(0, "/home/ubuntu/scannerproject")
from ui.chirp_adapter import _chirp_client_for

client = _chirp_client_for("airband")

# Sample 8x over 16s (covers >1 full LO cluster cycle of ~15s)
samples = []
for i in range(8):
    status = client.get_status()
    data = status.get("data") or status
    chs = data.get("channels", []) or []
    src = data.get("source", {}) or {}
    print(f"\n=== sample {i} (gain_db={src.get('sdr_gain_db')}) ===")
    for c in chs:
        lvl = c.get("signal_level_dbfs")
        parked = c.get("is_parked")
        cid = c.get("id", "?")
        print(f"  {c.get('freq_mhz'):>9.4f} parked={parked} lvl={lvl}")
    time.sleep(2)
