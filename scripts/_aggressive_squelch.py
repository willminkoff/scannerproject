#!/usr/bin/env python3
"""Drop airband squelch to -85 dBFS — if NOTHING opens at -85, it's
definitely RF/gain, not threshold."""
import sys, time
sys.path.insert(0, "/home/ubuntu/scannerproject")
from ui.chirp_adapter import _chirp_client_for

client = _chirp_client_for("airband")
target_sq = -85.0
status = client.get_status()
chs = (status.get("data") or status).get("channels", []) or []
for c in chs:
    cid = c.get("id")
    if not cid:
        continue
    r = client.set_squelch(cid, target_sq)
    print(f"  set_squelch({cid}, {target_sq}): {r}")
print(f"\nSet all {len(chs)} airband channels to {target_sq} dBFS")
print("Wait 20s and check hits...")
time.sleep(20)
# Count hits
import subprocess
out = subprocess.run(
    ["sudo", "-n", "tail", "-200", "/var/log/chirp/hits.jsonl"],
    capture_output=True, text=True, timeout=5,
).stdout
import json
now_ms = int(time.time() * 1000)
cutoff = now_ms - 30_000
recent = []
for line in out.splitlines():
    try:
        h = json.loads(line)
        if h.get("start_ts_ms", 0) >= cutoff:
            recent.append(h)
    except Exception:
        pass
print(f"\nHits in last 30s: {len(recent)}")
for h in recent[:10]:
    print(f"  {h.get('freq_mhz')} MHz peak={h.get('peak_dbfs')} dur={h.get('duration_s')}s")
