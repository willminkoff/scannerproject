#!/usr/bin/env python3
"""Quick look at hp_state favorites."""
import json
d = json.load(open("/home/ubuntu/scannerproject/data/hp_state.json"))
print("top-level keys:", sorted(d.keys())[:30])
print()
print("favorites_name:", d.get("favorites_name"))
print("favorites_name_air:", d.get("favorites_name_air"))
print("favorites_name_ground:", d.get("favorites_name_ground"))
favs = d.get("favorites", []) or []
print(f"\nfavorites count: {len(favs)}")
for f in favs:
    nm = f.get("name") or f.get("label") or "?"
    en = f.get("enabled")
    cf = f.get("custom_favorites")
    if isinstance(cf, list):
        kinds = {}
        for x in cf:
            t = x.get("type") or x.get("kind") or "?"
            kinds[t] = kinds.get(t, 0) + 1
        print(f"  - {nm!r} en={en} cf_list_len={len(cf)} kinds={kinds}")
    elif isinstance(cf, dict):
        cfk = list(cf.keys())
        n_freqs = len(cf.get("frequencies", []) or [])
        n_tgs = len(cf.get("talkgroups", []) or [])
        n_systems = len(cf.get("systems", []) or [])
        print(f"  - {nm!r} en={en} cf_keys={cfk} freqs={n_freqs} tgs={n_tgs} systems={n_systems}")
    else:
        print(f"  - {nm!r} en={en} cf={type(cf).__name__}")
