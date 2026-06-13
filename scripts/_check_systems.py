#!/usr/bin/env python3
"""Quick scan: what systems and sites are in the active digital config."""
import json
with open("/etc/scannerproject/digital/active/systems.json") as f:
    d = json.load(f)
for s in d.get("systems", []):
    name = s.get("name")
    sites = s.get("sites", [])
    en = [x for x in sites if x.get("enabled")]
    print(f"  {name}: {len(en)} enabled / {len(sites)} sites")
    for site in sites:
        flag = "ON " if site.get("enabled") else "off"
        print(f"     {flag} {site.get('site_id')} {site.get('site_name')}")
