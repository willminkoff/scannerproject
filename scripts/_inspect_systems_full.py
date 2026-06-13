#!/usr/bin/env python3
"""Show what control channels are in each system + site in active systems.json."""
import json
with open("/etc/scannerproject/digital/active/systems.json") as f:
    d = json.load(f)
for s in d.get("systems", []):
    name = s.get("name")
    print(f"\n{name}:")
    for site in s.get("sites", []):
        ccs = site.get("control_channels_hz", [])
        cc_mhz = sorted(set(int(c) for c in ccs))
        bands = set()
        for c in cc_mhz:
            if 760_000_000 <= c <= 776_000_000:
                bands.add("700")
            elif 800_000_000 <= c <= 870_000_000:
                bands.add("800")
            elif 150_000_000 <= c <= 174_000_000:
                bands.add("VHF")
            elif 400_000_000 <= c <= 520_000_000:
                bands.add("UHF")
        flag = "ON " if site.get("enabled") else "off"
        print(f"   {flag} site={site.get('site_id')} {site.get('site_name')!r}  bands={','.join(sorted(bands)) or '?'}  ccs={len(ccs)}")
        for c in cc_mhz[:3]:
            print(f"        {c/1e6:.5f} MHz")
        if len(cc_mhz) > 3:
            print(f"        ... +{len(cc_mhz)-3} more")
