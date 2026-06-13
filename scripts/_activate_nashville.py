#!/usr/bin/env python3
"""Activate the Nashville favorite and trigger digital runtime sync.

Sets `favorites_name = 'Nashville - BNA/JWN/MTRTRS/TACN/Radios'`,
flips that entry's `enabled` flag to True and every other entry's
to False, persists the state, then drives sync via the
ui.favorites_runtime API the same way airband_ui does.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Set repo on path
sys.path.insert(0, "/home/ubuntu/scannerproject")

from ui.hp_state import HPState
from ui import favorites_runtime as fr

TARGET = "Nashville - BNA/JWN/MTRTRS/TACN/Radios"
STATE_PATH = "/home/ubuntu/scannerproject/data/hp_state.json"

state = HPState.load(STATE_PATH)

favs = list(state.favorites or [])
found = False
for entry in favs:
    nm = entry.get("name") or entry.get("label")
    if nm == TARGET:
        entry["enabled"] = True
        found = True
    else:
        entry["enabled"] = False

if not found:
    print(f"ERROR: favorite {TARGET!r} not found among {[f.get('name') for f in favs]}")
    sys.exit(2)

state.favorites = favs
state.favorites_name = TARGET
# Clear per-band overrides so the global selection takes
if hasattr(state, "favorites_name_air"):
    state.favorites_name_air = None
if hasattr(state, "favorites_name_ground"):
    state.favorites_name_ground = None
state.mode = "favorites"
state.save(STATE_PATH)
print(f"set favorites_name={TARGET!r} and enabled the entry; saved {STATE_PATH}")

# Now trigger the digital sync
print("calling sync_scan_pool_to_digital_runtime(force=True)...")
res = fr.sync_scan_pool_to_digital_runtime(force=True)
print("sync result:")
try:
    print(json.dumps(res, indent=2, default=str)[:3000])
except Exception as e:
    print(repr(res)[:3000], f"(format err: {e})")
