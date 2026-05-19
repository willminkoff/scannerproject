"""Disco — FCC ULS lookup.

Given a frequency in Hz, return ranked candidate licensees from `uls.sqlite`.

Module-level cached connection (read-only). Safe to call from the classifier
on the hot path; the freq index makes each lookup ~sub-millisecond.

Amateur fallback: if the frequency falls inside a US amateur band and no
direct freq match is found, return the closest amateur licensees by state
(Tennessee neighbors first); they're tagged with source='amateur'.
"""
import math
import os
import sqlite3
import threading
from typing import Optional

DEFAULT_DB_PATH = "/home/ubuntu/scannerproject/disco/state/uls.sqlite"

# Default observer location (Will, Nashville TN) — overridable per-call.
DEFAULT_LAT_DD = 36.1627
DEFAULT_LON_DD = -86.7816

# US amateur bands (Hz, low/high inclusive). Used by the band fallback.
# Source: ARRL band plan as of 2026; HF bands omitted because Disco scans
# 100 MHz–1 GHz and they wouldn't resolve from a sweep at that range anyway.
AMATEUR_BANDS_HZ = [
    (50.000e6,   54.000e6,  "6m"),
    (144.000e6,  148.000e6, "2m"),
    (219.000e6,  220.000e6, "1.25m (low)"),
    (222.000e6,  225.000e6, "1.25m"),
    (420.000e6,  450.000e6, "70cm"),
    (902.000e6,  928.000e6, "33cm"),
    (1240.000e6, 1300.000e6, "23cm"),
]

# State proximity: when amateur fallback fires we have only entity state
# (no lat/lon), so rank Tennessee + neighbors first. This is rough but ships.
TN_NEIGHBORS = ["TN", "KY", "AL", "GA", "NC", "VA", "AR", "MS", "MO"]

# Cached connection per-thread. sqlite3 connections are not thread-safe by
# default; with detect_types|check_same_thread=False they are if shared
# carefully, but we just open one per thread.
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
    """Great-circle distance in km between two lat/lon pairs (decimal degrees)."""
    R = 6371.0
    p1 = math.radians(lat1); p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1); dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _bbox_for_radius(lat: float, lon: float, radius_km: float):
    """Cheap pre-filter bbox in decimal degrees. Slightly inflates radius."""
    lat_delta = radius_km / 111.0
    lon_delta = radius_km / max(0.1, 111.0 * math.cos(math.radians(lat)))
    return (lat - lat_delta, lat + lat_delta, lon - lon_delta, lon + lon_delta)


def _amateur_band_label(freq_hz: float) -> Optional[str]:
    for lo, hi, label in AMATEUR_BANDS_HZ:
        if lo <= freq_hz <= hi:
            return label
    return None


def lookup_uls(
    freq_hz: float,
    lat_dd: float = DEFAULT_LAT_DD,
    lon_dd: float = DEFAULT_LON_DD,
    radius_km: float = 80.0,
    guard_khz: float = 12.5,
    db_path: str = DEFAULT_DB_PATH,
    limit: int = 25,
) -> list[dict]:
    """Return ranked list of license matches for `freq_hz`.

    Each result dict has keys:
        callsign, entity_name, emission_designator, station_class,
        radio_service_code, freq_hz, freq_match_offset_hz,
        distance_km (None if unknown), source, fallback (bool)

    Sorted: (1) direct point-frequency matches by distance ascending,
    NULL-distance rows last; (2) amateur band fallback if no direct hits.
    """
    if freq_hz is None or freq_hz <= 0:
        return []

    if not os.path.exists(db_path):
        return []

    conn = _conn(db_path)
    guard_hz = guard_khz * 1000.0
    f_lo = freq_hz - guard_hz
    f_hi = freq_hz + guard_hz

    rows = conn.execute(
        "SELECT freq_hz, callsign, entity_name, emission_designator, station_class, "
        "radio_service_code, license_status, lat_dd, lon_dd, "
        "unique_system_identifier, source "
        "FROM licenses_by_freq WHERE freq_hz BETWEEN ? AND ? "
        "LIMIT 5000",
        (f_lo, f_hi),
    ).fetchall()

    in_range = []      # rows with known lat/lon inside radius_km
    unlocated = []     # rows with NULL lat/lon (e.g. station_class='MO' mobile-only)
    seen_keys = set()
    for r in rows:
        # De-dupe near-identical rows (same USI + freq + station class).
        key = (r["unique_system_identifier"], r["freq_hz"], r["station_class"])
        if key in seen_keys:
            continue
        seen_keys.add(key)

        lat = r["lat_dd"]; lon = r["lon_dd"]
        dist = None
        if lat is not None and lon is not None:
            dist = haversine_km(lat_dd, lon_dd, lat, lon)
            if dist > radius_km:
                continue
        item = {
            "callsign": r["callsign"],
            "entity_name": r["entity_name"],
            "emission_designator": r["emission_designator"],
            "station_class": r["station_class"],
            "radio_service_code": r["radio_service_code"],
            "freq_hz": r["freq_hz"],
            "freq_match_offset_hz": (r["freq_hz"] - freq_hz) if r["freq_hz"] else None,
            "distance_km": dist,
            "source": r["source"],
            "fallback": False,
        }
        if dist is not None:
            in_range.append(item)
        else:
            unlocated.append(item)

    in_range.sort(key=lambda d: d["distance_km"])
    out = list(in_range)

    # If we have in-range fixed stations, those are the answer. For unlocated
    # rows (typically station_class='MO' mobile-only or 'MX' itinerant), only
    # promote those whose USI ALSO has a fixed location in our radius — i.e.,
    # the licensee has presence near us. Without this, e.g. a Nashville
    # detection at 854.7 MHz spuriously matches "Los Angeles County Mobile"
    # because LA County has the same channel licensed.
    if unlocated:
        usis_with_local_presence = set()
        if in_range:
            usis_with_local_presence = {d.get("_usi") for d in in_range}
        # Also independently check whether each unlocated row's USI has any
        # local fixed station at any frequency.
        if unlocated:
            unlocated_usis = list({(r["entity_name"], r["callsign"]) for r in unlocated})
            # Lookup unique USIs from the set of unlocated rows
            usi_set = set()
            for r in rows:
                if r["lat_dd"] is None and r["unique_system_identifier"] is not None:
                    usi_set.add(r["unique_system_identifier"])
            if usi_set:
                lat_lo, lat_hi, lon_lo, lon_hi = _bbox_for_radius(lat_dd, lon_dd, radius_km)
                placeholders = ",".join("?" * len(usi_set))
                local_hits = conn.execute(
                    f"SELECT DISTINCT unique_system_identifier FROM licenses_by_freq "
                    f"WHERE unique_system_identifier IN ({placeholders}) "
                    f"AND lat_dd BETWEEN ? AND ? AND lon_dd BETWEEN ? AND ?",
                    (*usi_set, lat_lo, lat_hi, lon_lo, lon_hi),
                ).fetchall()
                local_usi_set = {r[0] for r in local_hits}
                for r in unlocated:
                    # Each unlocated entry was built from a Row; we don't have
                    # the USI in the dict. Re-derive from the original row pass.
                    pass
                # Build a quick callsign->usi map from the original rows
                cs_to_usi = {}
                for r in rows:
                    if r["lat_dd"] is None:
                        cs_to_usi[(r["callsign"], r["freq_hz"])] = r["unique_system_identifier"]
                for item in unlocated:
                    usi = cs_to_usi.get((item["callsign"], item["freq_hz"]))
                    if usi in local_usi_set:
                        # Promote: this licensee has a local fixed location.
                        item["distance_km"] = None  # still unknown — but trustworthy
                        item["_locally_present"] = True
                        out.append(item)

    # Amateur band fallback: only when no direct matches and freq is in an amateur band.
    if not out:
        band = _amateur_band_label(freq_hz)
        if band:
            ham_rows = conn.execute(
                "SELECT callsign, entity_name, operator_class, state, city "
                "FROM amateur_callsigns WHERE state IN ({}) "
                "ORDER BY CASE state {} ELSE 99 END "
                "LIMIT ?".format(
                    ",".join("?" * len(TN_NEIGHBORS)),
                    " ".join(f"WHEN '{s}' THEN {i}" for i, s in enumerate(TN_NEIGHBORS)),
                ),
                (*TN_NEIGHBORS, limit),
            ).fetchall()
            for r in ham_rows:
                out.append({
                    "callsign": r["callsign"],
                    "entity_name": r["entity_name"],
                    "emission_designator": None,
                    "station_class": f"AMATEUR-{r['operator_class']}" if r["operator_class"] else "AMATEUR",
                    "radio_service_code": "HA",
                    "freq_hz": None,
                    "freq_match_offset_hz": None,
                    "distance_km": None,
                    "source": f"amateur ({band})",
                    "fallback": True,
                })

    return out[:limit]


def best_match(*args, **kwargs) -> Optional[dict]:
    """Convenience: return only the top-ranked result, or None."""
    rows = lookup_uls(*args, **kwargs)
    return rows[0] if rows else None


if __name__ == "__main__":
    # Quick smoke test from the CLI: python uls.py 464.625e6
    import json
    import sys
    if len(sys.argv) < 2:
        print("usage: python uls.py <freq_hz> [radius_km] [guard_khz]")
        sys.exit(1)
    f = float(sys.argv[1])
    r = float(sys.argv[2]) if len(sys.argv) > 2 else 80.0
    g = float(sys.argv[3]) if len(sys.argv) > 3 else 12.5
    res = lookup_uls(f, radius_km=r, guard_khz=g, limit=10)
    print(json.dumps(res, indent=2, default=str))
