#!/usr/bin/env python3
"""Push squelch=-65 dBFS to every airband channel in the current pool."""
import sys
sys.path.insert(0, "/home/ubuntu/scannerproject")

from ui.chirp_adapter import _chirp_client_for

client = _chirp_client_for("airband")
status = client.get_status()
data = status.get("data") or status
chs = data.get("channels", []) or []
print(f"airband pool: {len(chs)} channels")

target_sq = -65.0
results = []
for c in chs:
    cid = c.get("id")
    if not cid:
        continue
    r = client.set_squelch(cid, target_sq)
    ok = r.get("ok", True)  # daemon-side response shape varies
    results.append((cid, c.get("freq_mhz"), ok))
    print(f"  set_squelch({cid!r}, {target_sq}) -> ok={ok}")

print(f"\n{sum(1 for _,_,ok in results if ok)}/{len(results)} squelch updates applied")
