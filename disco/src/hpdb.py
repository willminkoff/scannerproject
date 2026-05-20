"""Disco — HomePatrol DB (HPDB) lookup.

Given a frequency in Hz, return ranked candidate identifications from the
RadioReference-curated HomePatrol database (`data/homepatrol.db`, built by
`scripts/hpdb_builder.py` from HPCOPY.zip).

Where ULS gives FCC licensee names ("CITY OF FRANKLIN POLICE DEPT") and
CDBS gives broadcast station callsigns ("WPLN-FM"), HPDB gives the
human-curated labels Will actually wants ("Williamson County Fire —
Dispatch", "Tennessee Advanced Communications Network"). For trunked
control channels this is the difference between identifying a hit as
"some Motorola system on 851.55" vs "MTRTRS Davidson County Simulcast".

Returns both conventional-channel matches and trunked-control-channel
matches, distinguished by `source_table` ("conventional" / "trunk_control").
Spatial filtering follows SB3's Travel Mode location via the caller —
classifier.py passes lat_dd/lon_dd from `current_location.get_current_location()`.

The HPDB is read-only at runtime: `hpdb_builder.py` rebuilds it from
HPCOPY.zip periodically. No TTL-cache needed at this layer; sqlite3 with
`query_only=ON` is fast enough on the 50 MB file.
"""
import math
import os
import sqlite3
import threading
from typing import Optional

DEFAULT_DB_PATH = "/home/ubuntu/scannerproject/data/homepatrol.db"

# Default observer location (Will, Nashville TN) — overridable per-call.
# Matches the uls.py / cdbs.py constants so behavior is consistent across
# all three FCC-data-style modules when called without an explicit location.
DEFAULT_LAT_DD = 36.1627
DEFAULT_LON_DD = -86.7816

# Thread-local cached connection (sqlite3 connections are not thread-safe by
# default). Pattern mirrors uls.py / cdbs.py.
_local = threading.local()


def _conn(db_path: str) -> Optional[sqlite3.Connection]:
    c = getattr(_local, "conn", None)
    p = getattr(_local, "path", None)
    if c is not None and p == db_path:
        return c
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
        _local.conn = None
        _local.path = None
    if not os.path.exists(db_path):
        return None
    c = sqlite3.connect(db_path, timeout=5.0)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA query_only=ON")
    except Exception:
        pass
    _local.conn = c
    _local.path = db_path
    return c


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two lat/lon pairs (decimal degrees)."""
    R = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _conventional_rows(conn, freq_hz: float, guard_hz: float, lat_dd: float,
                       lon_dd: float, radius_km: float) -> list[dict]:
    """conventional_freqs JOIN conventional_groups, filtered by distance.

    Conventional channels carry the alpha_tag directly (e.g. "Police
    Dispatch"). The group_name provides the agency / area context
    ("Williamson County Sheriff"). Service type ("Law Dispatch") comes
    from service_types.
    """
    f_lo = freq_hz - guard_hz
    f_hi = freq_hz + guard_hz
    rows = conn.execute(
        """
        SELECT cf.alpha_tag, cf.freq_hz, cf.mode, cf.tone, cf.service_tag,
               cg.group_name, cg.latitude, cg.longitude, cg.radius,
               st.name AS service_name
        FROM conventional_freqs cf
        JOIN conventional_groups cg ON cf.cgroup_id = cg.cgroup_id
        LEFT JOIN service_types st ON cf.service_tag = st.service_tag
        WHERE cf.freq_hz BETWEEN ? AND ?
          AND cg.latitude IS NOT NULL AND cg.longitude IS NOT NULL
        LIMIT 5000
        """,
        (f_lo, f_hi),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        dist = haversine_km(lat_dd, lon_dd, r["latitude"], r["longitude"])
        if dist > radius_km:
            continue
        out.append({
            "alpha_tag": r["alpha_tag"],
            "group_name": r["group_name"],
            "system_name": None,
            "service_type": r["service_name"],
            "freq_hz": r["freq_hz"],
            "mode": r["mode"],
            "tone": r["tone"],
            "distance_km": dist,
            "site_radius_km": r["radius"],
            "source_table": "conventional",
        })
    return out


def _trunk_control_rows(conn, freq_hz: float, guard_hz: float, lat_dd: float,
                        lon_dd: float, radius_km: float) -> list[dict]:
    """trunk_freqs JOIN trunk_sites JOIN trunk_systems, filtered by distance.

    Trunked control channels carry the system identity through the join:
    site_name (geographic site), system_name (overall trunked system),
    protocol (P25Standard / P25X2_TDMA / Motorola / etc.). The
    talkgroups table holds per-TGID labels, but Disco only sees the
    control channel signal, not decoded TGIDs — so this lookup
    identifies the SYSTEM, not specific calls.
    """
    f_lo = freq_hz - guard_hz
    f_hi = freq_hz + guard_hz
    rows = conn.execute(
        """
        SELECT tf.freq_hz, tf.lcn,
               ts.site_name, ts.latitude, ts.longitude, ts.radius, ts.site_mode,
               tsy.system_name, tsy.protocol, tsy.system_type
        FROM trunk_freqs tf
        JOIN trunk_sites ts ON tf.site_id = ts.site_id
        JOIN trunk_systems tsy ON ts.trunk_id = tsy.trunk_id
        WHERE tf.freq_hz BETWEEN ? AND ?
          AND ts.latitude IS NOT NULL AND ts.longitude IS NOT NULL
        LIMIT 5000
        """,
        (f_lo, f_hi),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        dist = haversine_km(lat_dd, lon_dd, r["latitude"], r["longitude"])
        if dist > radius_km:
            continue
        # alpha_tag for trunked = "system_name — site_name" so the caller
        # gets a self-contained label without needing to format both fields.
        sys_name = r["system_name"] or "(unnamed system)"
        site_name = r["site_name"] or "(unnamed site)"
        alpha = f"{sys_name} — {site_name}"
        out.append({
            "alpha_tag": alpha,
            "group_name": site_name,
            "system_name": sys_name,
            "service_type": r["protocol"],
            "freq_hz": r["freq_hz"],
            "mode": None,
            "tone": None,
            "distance_km": dist,
            "site_radius_km": r["radius"],
            "source_table": "trunk_control",
            "lcn": r["lcn"],
            "site_mode": r["site_mode"],
            "system_type": r["system_type"],
        })
    return out


def lookup_hpdb(
    freq_hz: float,
    lat_dd: float = DEFAULT_LAT_DD,
    lon_dd: float = DEFAULT_LON_DD,
    radius_km: float = 80.0,
    guard_khz: float = 2.5,
    db_path: Optional[str] = None,
    limit: int = 3,
) -> list[dict]:
    """Return ranked HPDB matches for `freq_hz`.

    Combines conventional_freqs and trunk_freqs results into a single list,
    sorted by distance ascending. Each result has keys:

        alpha_tag        — human label (e.g. "Police Dispatch" or
                           "Tennessee Advanced Communications Network — West Nashville")
        group_name       — agency/area for conventional, site_name for trunked
        system_name      — trunked system name (None for conventional)
        service_type     — "Law Dispatch" / "Fire" / "P25Standard" / etc.
        freq_hz, mode, tone, lcn, site_mode, system_type — auxiliary fields
        distance_km      — distance from (lat_dd, lon_dd) in km
        site_radius_km   — HPDB's per-site advertised radius (if any)
        source_table     — "conventional" or "trunk_control"

    Gracefully returns [] when:
      - freq_hz is None or <= 0
      - HPDB file is missing (warn-and-degrade pattern, mirrors uls.py)
      - any SQL error inside the lookup (the classifier loop must not crash
        because HPDB had a hiccup)
    """
    if freq_hz is None or freq_hz <= 0:
        return []

    path = db_path or os.getenv("DISCO_HPDB_PATH", DEFAULT_DB_PATH)
    conn = _conn(path)
    if conn is None:
        return []

    guard_hz = max(0.0, guard_khz * 1000.0)
    try:
        conv = _conventional_rows(conn, freq_hz, guard_hz, lat_dd, lon_dd, radius_km)
    except Exception:
        conv = []
    try:
        trunk = _trunk_control_rows(conn, freq_hz, guard_hz, lat_dd, lon_dd, radius_km)
    except Exception:
        trunk = []

    combined = conv + trunk
    combined.sort(key=lambda d: d.get("distance_km") if d.get("distance_km") is not None else 1e9)
    return combined[:limit]


def best_match(*args, **kwargs) -> Optional[dict]:
    """Convenience: return only the top-ranked HPDB result, or None."""
    rows = lookup_hpdb(*args, **kwargs)
    return rows[0] if rows else None


if __name__ == "__main__":
    # Smoke test: python hpdb.py <freq_hz> [radius_km]
    import json
    import sys
    if len(sys.argv) < 2:
        print("usage: python hpdb.py <freq_hz> [radius_km]")
        sys.exit(1)
    f = float(sys.argv[1])
    r = float(sys.argv[2]) if len(sys.argv) > 2 else 80.0
    res = lookup_hpdb(f, radius_km=r, limit=10)
    print(json.dumps(res, indent=2, default=str))
