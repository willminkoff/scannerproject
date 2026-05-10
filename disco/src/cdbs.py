"""Disco — FCC CDBS lookup (commercial AM + FM broadcast).

Mirrors uls.lookup_uls. Given a frequency in Hz, return ranked candidate
broadcast stations from cdbs.sqlite, sorted by distance ascending.

Bands handled:
    AM 530 kHz - 1700 kHz   (10 kHz channel spacing in NA)
    FM 88 MHz - 108 MHz     (200 kHz channel spacing)

Module-level cached connection (read-only). Safe to call from the classifier
on the hot path; the freq index makes each lookup ~sub-millisecond.

State-distance fallback: for AM stations (no lat/lon in CDBS am_eng_data),
prefer rows whose comm_state matches Tennessee or its neighbors.
"""
import math
import os
import sqlite3
import threading
from typing import Optional

DEFAULT_DB_PATH = "/home/ubuntu/scannerproject/disco/state/cdbs.sqlite"

# Default observer location (Will, Nashville TN) — overridable per-call.
DEFAULT_LAT_DD = 36.1627
DEFAULT_LON_DD = -86.7816

# State proximity for AM rows that lack lat/lon — sorted by closeness to TN.
TN_NEIGHBORS = ["TN", "KY", "AL", "GA", "NC", "VA", "AR", "MS", "MO"]

# Broadcast band cutoffs (Hz) — used to auto-pick guard bandwidth.
AM_LO_HZ = 530e3
AM_HI_HZ = 1710e3
FM_LO_HZ = 87.9e6
FM_HI_HZ = 108.0e6

_local = threading.local()


def _conn(db_path: str) -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    p = getattr(_local, "path", None)
    if c is not None and p == db_path:
        return c
    if c is not None:
        try: c.close()
        except: pass
    c = sqlite3.connect(db_path, timeout=5.0)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    _local.conn = c
    _local.path = db_path
    return c


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    p1 = math.radians(lat1); p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1); dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _auto_guard_khz(freq_hz: float, default_khz: float) -> float:
    """Pick a sensible guard band based on frequency. AM channels are 10 kHz
    apart in NA, FM 200 kHz. Caller can still override via guard_khz."""
    if AM_LO_HZ <= freq_hz <= AM_HI_HZ:
        return min(default_khz, 10.0)
    return default_khz


def _emission_for_service(service: str) -> Optional[str]:
    """CDBS doesn't carry ULS-style emission designators. Synthesize a
    human-readable label so the dashboard's emission column shows something
    descriptive. Don't fabricate FCC-style designator strings."""
    if service == "FM":
        return "FM stereo"
    if service == "AM":
        return "AM mono"
    return None


def lookup_cdbs(
    freq_hz: float,
    lat_dd: float = DEFAULT_LAT_DD,
    lon_dd: float = DEFAULT_LON_DD,
    radius_km: float = 200.0,
    guard_khz: float = 200.0,
    db_path: str = DEFAULT_DB_PATH,
    limit: int = 25,
) -> list[dict]:
    """Return ranked list of broadcast-station matches for .

    Each result dict has keys:
        callsign, entity_name, emission_designator, station_class,
        distance_km (None if unknown), freq_match_offset_hz, source='cdbs',
        service ('AM' or 'FM'), facility_id, fac_status, community

    Sorted: rows with known lat/lon inside  by distance ascending,
    then unlocated rows (mostly AM) in TN-neighbor-state order, capped at
    cputime         unlimited
filesize        unlimited
datasize        unlimited
stacksize       7MB
coredumpsize    0kB
addressspace    unlimited
memorylocked    unlimited
maxproc         5333
descriptors     1048576.
    """
    if freq_hz is None or freq_hz <= 0:
        return []
    if not os.path.exists(db_path):
        return []

    conn = _conn(db_path)

    # Auto-tighten guard for AM (10 kHz channels).
    eff_guard_khz = _auto_guard_khz(freq_hz, guard_khz)
    guard_hz = eff_guard_khz * 1000.0
    f_lo = freq_hz - guard_hz
    f_hi = freq_hz + guard_hz

    rows = conn.execute(
        "SELECT freq_hz, callsign, entity_name, station_class, service, "
        "fac_status, lat_dd, lon_dd, facility_id, community, cdbs_freq_unit "
        "FROM broadcast_by_freq WHERE freq_hz BETWEEN ? AND ? "
        "LIMIT 5000",
        (f_lo, f_hi),
    ).fetchall()

    in_range = []   # known lat/lon inside radius_km
    unlocated = []  # null lat/lon (mostly AM)
    seen = set()
    for r in rows:
        # De-dupe identical facility_id+freq.
        key = (r["facility_id"], r["freq_hz"])
        if key in seen:
            continue
        seen.add(key)

        lat = r["lat_dd"]; lon = r["lon_dd"]
        dist = None
        if lat is not None and lon is not None:
            dist = haversine_km(lat_dd, lon_dd, lat, lon)
            if dist > radius_km:
                continue
        item = {
            "callsign": r["callsign"],
            "entity_name": r["entity_name"],
            "emission_designator": _emission_for_service(r["service"]),
            "station_class": r["station_class"],
            "radio_service_code": r["service"],
            "freq_hz": r["freq_hz"],
            "freq_match_offset_hz": (r["freq_hz"] - freq_hz) if r["freq_hz"] else None,
            "distance_km": dist,
            "source": "cdbs",
            "service": r["service"],
            "facility_id": r["facility_id"],
            "fac_status": r["fac_status"],
            "community": r["community"],
            "fallback": False,
        }
        if dist is not None:
            in_range.append(item)
        else:
            unlocated.append(item)

    # Sort located by distance ascending.
    in_range.sort(key=lambda d: d["distance_km"])

    # Sort unlocated by TN-neighbor state (parse community 'CITY, ST').
    def _state_rank(item):
        comm = item.get("community") or ""
        # Community format is 'City, ST' from the loader.
        st = comm.rsplit(",", 1)[-1].strip().upper() if "," in comm else ""
        try:
            return TN_NEIGHBORS.index(st)
        except ValueError:
            return len(TN_NEIGHBORS)
    unlocated.sort(key=_state_rank)

    out = in_range + unlocated
    return out[:limit]


def best_match(*args, **kwargs) -> Optional[dict]:
    rows = lookup_cdbs(*args, **kwargs)
    return rows[0] if rows else None


if __name__ == "__main__":
    import json, sys as _sys
    if len(_sys.argv) < 2:
        print("usage: python cdbs.py <freq_hz> [radius_km] [guard_khz]")
        _sys.exit(1)
    f = float(_sys.argv[1])
    r = float(_sys.argv[2]) if len(_sys.argv) > 2 else 200.0
    g = float(_sys.argv[3]) if len(_sys.argv) > 3 else 200.0
    res = lookup_cdbs(f, radius_km=r, guard_khz=g, limit=10)
    print(json.dumps(res, indent=2, default=str))
