#!/usr/bin/env python3
"""Push the active Nashville favorite's airband entries into chirp via the
activate_favorite_via_chirp API.  One-shot recovery for the airband state
being stale after a reboot.
"""
import sys
sys.path.insert(0, "/home/ubuntu/scannerproject")

from ui.chirp_adapter import activate_favorite_via_chirp

TARGET = "fav-nashville-bna-radios"  # favorite id, not label

# Activate on both bands so airband + ground are in sync with the favorite.
import json as _json
for band in ("airband", "ground"):
    print(f"\n=== activating {band!r} ===")
    try:
        result = activate_favorite_via_chirp(band, TARGET)
        print(_json.dumps(result, indent=2, default=str)[:2000])
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
