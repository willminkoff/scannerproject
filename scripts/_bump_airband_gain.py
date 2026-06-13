#!/usr/bin/env python3
"""Push SDR gain higher on airband to see if levels start moving."""
import sys, time
sys.path.insert(0, "/home/ubuntu/scannerproject")
from ui.chirp_client import ChirpClient
from ui.chirp_adapter import _chirp_client_for

client = _chirp_client_for("airband")

# Try set_master_gain or set_sdr_gain to bring up the signal.
# First see what API is available.
for new_gain in (40.0, 50.0, 55.0):
    print(f"\n=== set_master_gain({new_gain}) ===")
    try:
        r = client._send("set_master_gain", {"db": float(new_gain)})
        print(f"  -> {r}")
    except Exception as e:
        print(f"  failed: {e}")
    time.sleep(3)
    status = client.get_status()
    data = status.get("data") or status
    mg = data.get("master_gain_db")
    sd_g = (data.get("source") or {}).get("sdr_gain_db")
    chs = data.get("channels", []) or []
    # Show unparked channels with current level
    live_ch = [c for c in chs if not c.get("is_parked")]
    if live_ch:
        c = live_ch[0]
        print(f"  master_gain={mg} sdr_gain={sd_g} live ch lvl={c.get('signal_level_dbfs')}")
