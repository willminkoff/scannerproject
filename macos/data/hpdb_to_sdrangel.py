#!/usr/bin/env python3
"""hpdb_to_sdrangel.py — HomePatrol SQLite (analog) → SDRangel Frequency-Scanner CSV.

Reads conventional_freqs from homepatrol.db and emits a CSV importable by the
SDRangel Frequency Scanner channel, matching etc/mac/sdrangel/scan-38380.csv:

    Freq (Hz),Enable,Notes,Channel,Ch BW (Hz),TH (dB),Sq (dB)

Mapping (from the verified DB schema):
  conventional_freqs: alpha_tag, freq_hz, mode (AM/FM/...), tone, service_tag
    mode AM  -> Channel R0:0, BW 8000   (airband)
    mode FM  -> Channel R0:1, BW 12500  (ground/NFM)
  Location filter: conventional_groups has lat/lon/radius; hp_state.json has
  lat/lon/range_miles + enabled_service_tags. Default filters to the band range
  you ask for + (optionally) the favorites' location radius.

Usage:
  python3 hpdb_to_sdrangel.py --db homepatrol.db --band airband        > airband.csv
  python3 hpdb_to_sdrangel.py --db homepatrol.db --band ground         > ground.csv
  python3 hpdb_to_sdrangel.py --db homepatrol.db --min-mhz 118 --max-mhz 137
  python3 hpdb_to_sdrangel.py --db homepatrol.db --state hp_state.json --near  # location-filtered
"""
from __future__ import annotations
import argparse, csv, json, math, sqlite3, sys

BANDS = {  # convenience presets (MHz)
    "airband": (118.0, 137.0),
    "ground":  (144.0, 174.0),
    "uhf":     (400.0, 470.0),
}
TH_DEFAULT, SQ_DEFAULT = -55.0, -55.0


def _haversine_mi(a_lat, a_lon, b_lat, b_lon):
    R = 3959.0
    dlat, dlon = math.radians(b_lat - a_lat), math.radians(b_lon - a_lon)
    h = math.sin(dlat/2)**2 + math.cos(math.radians(a_lat))*math.cos(math.radians(b_lat))*math.sin(dlon/2)**2
    return 2*R*math.asin(math.sqrt(h))


def rows(db, min_hz, max_hz, state=None, near=False):
    con = sqlite3.connect(db); con.row_factory = sqlite3.Row
    # join freq -> group for lat/lon when location-filtering
    q = ("SELECT f.alpha_tag, f.freq_hz, f.mode, f.service_tag, "
         "g.latitude AS glat, g.longitude AS glon "
         "FROM conventional_freqs f LEFT JOIN conventional_groups g ON f.cgroup_id=g.cgroup_id "
         "WHERE f.freq_hz BETWEEN ? AND ?")
    params = [int(min_hz), int(max_hz)]
    svc = None
    if state:
        st = json.load(open(state))
        svc = set(str(s) for s in st.get("enabled_service_tags", []))
        clat, clon, rng = st.get("lat"), st.get("lon"), st.get("range_miles", 50)
    for r in con.execute(q, params):
        if svc and str(r["service_tag"]) not in svc:
            continue
        if near and state and r["glat"] and r["glon"]:
            if _haversine_mi(clat, clon, r["glat"], r["glon"]) > rng:
                continue
        yield r
    con.close()


def to_csv(rs, out):
    w = csv.writer(out)
    w.writerow(["Freq (Hz)", "Enable", "Notes", "Channel", "Ch BW (Hz)", "TH (dB)", "Sq (dB)"])
    seen = set(); n = 0
    for r in rs:
        hz = int(r["freq_hz"])
        if hz in seen:
            continue
        seen.add(hz)
        am = (r["mode"] or "").upper().startswith("AM")
        chan, bw = ("R0:0", 8000) if am else ("R0:1", 12500)
        note = (r["alpha_tag"] or "").replace(",", " ")[:60]
        w.writerow([hz, "true", note, chan, bw, TH_DEFAULT, SQ_DEFAULT]); n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--band", choices=BANDS.keys())
    ap.add_argument("--min-mhz", type=float)
    ap.add_argument("--max-mhz", type=float)
    ap.add_argument("--state", help="hp_state.json for service-tag + location filtering")
    ap.add_argument("--near", action="store_true", help="filter to hp_state lat/lon within range_miles")
    ap.add_argument("-o", "--out", default="-")
    a = ap.parse_args()
    if a.band:
        lo, hi = BANDS[a.band]
    elif a.min_mhz and a.max_mhz:
        lo, hi = a.min_mhz, a.max_mhz
    else:
        ap.error("give --band or --min-mhz/--max-mhz")
    out = sys.stdout if a.out == "-" else open(a.out, "w", newline="")
    n = to_csv(rows(a.db, lo*1e6, hi*1e6, a.state, a.near), out)
    if out is not sys.stdout:
        out.close()
    print(f"# wrote {n} channels [{lo}-{hi} MHz]", file=sys.stderr)


if __name__ == "__main__":
    main()
