#!/usr/bin/env python3
"""Find TACN trunked system + nearby sites in HPDB."""
import sqlite3, math, json
DB = "/home/ubuntu/scannerproject/data/homepatrol.db"
HOME_LAT, HOME_LON = 36.1627, -86.7816


def hav(lat1, lon1, lat2, lon2):
    R = 3958.8
    dlat = math.radians(lat2 - lat1); dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


con = sqlite3.connect(DB); cur = con.cursor()
print("=== trunk_systems columns ===")
cur.execute("PRAGMA table_info(trunk_systems)")
ts_cols = [c[1] for c in cur.fetchall()]
print(ts_cols)

print("=== TACN / Tennessee Advanced match ===")
cur.execute(
    "SELECT * FROM trunk_systems WHERE system_name LIKE '%TACN%' "
    "OR system_name LIKE '%Tennessee Advanced%' OR system_name LIKE '%TN Advanced%' "
    "OR system_name LIKE '%TN STARS%' OR system_name LIKE '%Tennessee STARS%'")
matches = cur.fetchall()
for r in matches:
    print(dict(zip(ts_cols, r)))

print("=== trunk_sites columns ===")
cur.execute("PRAGMA table_info(trunk_sites)")
sites_cols = [c[1] for c in cur.fetchall()]
print(sites_cols)

print("=== trunk_freqs columns ===")
cur.execute("PRAGMA table_info(trunk_freqs)")
tf_cols = [c[1] for c in cur.fetchall()]
print(tf_cols)

# For each match, list nearby sites
for r in matches:
    trunk_id = r[0]
    sys_name = r[3]
    print(f"\n--- sites for {sys_name} (trunk_id={trunk_id}) ---")
    cur.execute(f"SELECT * FROM trunk_sites WHERE trunk_id=?", (trunk_id,))
    sites = cur.fetchall()
    rows = []
    for s in sites:
        sd = dict(zip(sites_cols, s))
        lat = sd.get("latitude") or sd.get("lat")
        lon = sd.get("longitude") or sd.get("lon")
        if lat and lon:
            d = hav(HOME_LAT, HOME_LON, lat, lon)
        else:
            d = 9999
        rows.append((d, sd))
    rows.sort(key=lambda x: x[0])
    for d, sd in rows[:8]:
        print(f"   {d:7.1f} mi  site_id={sd.get('site_id')} name={sd.get('site_name')} "
              f"site_number={sd.get('site_number')}")
        # control freqs
        site_id = sd.get('site_id')
        cur.execute(
            "SELECT * FROM trunk_freqs WHERE site_id=? "
            "ORDER BY freq_hz", (site_id,))
        for f in cur.fetchall():
            fd = dict(zip(tf_cols, f))
            mark = "*" if (fd.get("freq_type") in ("c", "C", "control", "Control", "Primary Control", "Alt Control")) else " "
            print(f"        {mark} {fd.get('freq_hz')} type={fd.get('freq_type')}")

con.close()
