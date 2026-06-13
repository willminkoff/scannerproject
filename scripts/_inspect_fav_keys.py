#!/usr/bin/env python3
"""What keys does the Nashville favorite actually have?"""
import json

d = json.load(open("/home/ubuntu/scannerproject/data/hp_state.json"))
TARGET = "Nashville - BNA/JWN/MTRTRS/TACN/Radios"
for f in d.get("favorites", []) or []:
    nm = f.get("name") or f.get("label")
    if nm != TARGET:
        continue
    print(f"=== favorite: {nm} ===")
    print("top-level keys:", sorted(f.keys()))
    print(f"  id: {f.get('id')!r}")
    print(f"  name: {f.get('name')!r}")
    print(f"  enabled: {f.get('enabled')!r}")
    print(f"  enabled_air: {f.get('enabled_air')!r}")
    print(f"  enabled_ground: {f.get('enabled_ground')!r}")
    cf = f.get("custom_favorites") or []
    print(f"  custom_favorites: {len(cf)} entries")
    if cf:
        print("  first entry keys:", sorted(cf[0].keys()) if isinstance(cf[0], dict) else "not dict")
        # Bucket by airband vs ground
        airband = [x for x in cf if isinstance(x, dict) and 100 < float(x.get("frequency", 0) or 0) < 137]
        ground = [x for x in cf if isinstance(x, dict) and 137 <= float(x.get("frequency", 0) or 0) < 520]
        digital = [x for x in cf if isinstance(x, dict) and float(x.get("frequency", 0) or 0) > 700]
        trunked = [x for x in cf if isinstance(x, dict) and x.get("type") == "trunked"]
        print(f"  airband entries (100-137 MHz): {len(airband)}")
        for e in airband[:12]:
            print(f"    {e.get('frequency')} {e.get('alpha_tag')!r}")
        print(f"  ground entries (137-520 MHz): {len(ground)}")
        for e in ground[:5]:
            print(f"    {e.get('frequency')} {e.get('alpha_tag')!r}")
        print(f"  trunked entries: {len(trunked)}")
