"""Disco — FCC CDBS loader (FM + AM broadcast).

Mirrors the structure of uls_load.py but for the FCC Media Bureau's CDBS
database, which covers broadcast services that ULS doesn't (commercial FM
88-108 MHz and AM 530-1700 kHz).

Data sources (downloaded into disco/cdbs/raw/ as individual zips):
    facility.zip     — facility records: callsign, freq, service, status, comm city/state.
    fm_eng_data.zip  — FM engineering: facility_id → station_class + lat/lon.
    am_eng_data.zip  — AM engineering: facility_id → engineering metadata (no lat/lon).

Empirical column indices for the 2024-snapshot files (no header rows; 0-based):

facility.dat (32 cols):
    [ 0] comm_city          [ 5] fac_callsign
    [ 1] comm_state         [ 6] fac_channel
    [ 7] fac_city           [ 9] fac_frequency  (MHz for FM, kHz for AM)
    [10] fac_service        ('AM','FM','TV','FX','FB','FL',...)
    [11] fac_state          [14] facility_id
    [15] lic_expiration_date
    [16] fac_status         ('LICEN','LICAN','PRCAN','FVOID',...)

fm_eng_data.dat (73 cols):
    [19] station_class      ('A','B1','B','C0','C','C1','C2','C3','D')
    [20] facility_id
    [21] eng_record_type    ('LIC','APP','CP',...)
    [30] lat_deg  [31] lat_dir  [32] lat_min  [33] lat_sec
    [34] lon_deg  [35] lon_dir  [36] lon_min  [37] lon_sec

am_eng_data.dat (17 cols): no clean lat/lon. We index facility_id so
    the loader can mark AM rows with eng presence, but lat/lon comes from
    comm_city/comm_state via facility.dat (no point-lat/lon for AM in v0).

Frequency conversion:
    FM: facility.dat freq is in MHz (e.g. '90.100000') -> Hz = * 1e6
    AM: facility.dat freq is in kHz (e.g. '1380.000000') -> Hz = * 1e3

Entity name: CDBS's facility.dat has no explicit licensee field. For v0 we
    synthesize entity_name as '<callsign> (<comm_city>, <comm_state>)' so
    the dashboard's 'Licensed to' column renders something useful. A future
    pass can join application.dat for the actual licensee LLC name.
"""
import math
import os
import sqlite3
import sys
import time
import zipfile
from typing import Dict, Iterable, Optional, Tuple

CDBS_RAW_DIR = "/home/ubuntu/scannerproject/disco/cdbs/raw"
DB_PATH = "/home/ubuntu/scannerproject/disco/state/cdbs.sqlite"

# fac_status values we accept as 'currently licensed / on the air'.
ACTIVE_STATUSES = ("LICEN", "LICAN")

# fm_eng_data eng_record_type we want.
FM_ENG_LIC = "LIC"


def log(msg: str):
    sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    sys.stderr.flush()


def dms_to_dd(deg: str, minute: str, sec: str, direction: str) -> Optional[float]:
    """Convert DMS + N/S/E/W -> signed decimal degrees. Returns None for null/bad."""
    try:
        d = float(deg or 0); m = float(minute or 0); s = float(sec or 0)
    except ValueError:
        return None
    if d == 0 and m == 0 and s == 0:
        return None
    # CDBS sentinel for 'unknown lat/lon': 90 N 60 60 / 0 W 60 60. Reject.
    if d >= 90 or m >= 60 or s >= 60:
        return None
    dd = d + m / 60.0 + s / 3600.0
    if direction in ("S", "W"):
        dd = -dd
    return dd


def iter_dat(zf: zipfile.ZipFile, name: str) -> Iterable[list]:
    try:
        info = zf.getinfo(name)
    except KeyError:
        return
    with zf.open(info) as f:
        for line in f:
            yield line.decode("latin-1", errors="replace").rstrip("\r\n").split("|")


def init_db(path: str) -> sqlite3.Connection:
    if os.path.exists(path):
        os.remove(path)
    if os.path.exists(path + "-wal"):
        os.remove(path + "-wal")
    if os.path.exists(path + "-shm"):
        os.remove(path + "-shm")
    conn = sqlite3.connect(path)
    conn.executescript("""
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;
        PRAGMA cache_size=-262144;

        CREATE TABLE broadcast_by_freq (
            freq_hz REAL NOT NULL,
            callsign TEXT,
            entity_name TEXT,
            station_class TEXT,
            service TEXT,
            fac_status TEXT,
            lat_dd REAL,
            lon_dd REAL,
            facility_id INTEGER,
            community TEXT,
            cdbs_freq_unit TEXT
        );

        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    return conn


def load_facility(zf_path: str) -> Dict[int, dict]:
    """Load facility.dat; return {facility_id: {callsign, freq, service, status,
    comm_city, comm_state, fac_state}} for AM/FM rows with active status."""
    log(f"--- facility: opening {os.path.basename(zf_path)} ---")
    t0 = time.time()
    out: Dict[int, dict] = {}
    n = 0; kept = 0
    with zipfile.ZipFile(zf_path) as zf:
        for cols in iter_dat(zf, "facility.dat"):
            n += 1
            if len(cols) < 17:
                continue
            service = cols[10]
            if service not in ("AM", "FM"):
                continue
            status = cols[16]
            if status not in ACTIVE_STATUSES:
                continue
            try:
                fid = int(cols[14])
            except ValueError:
                continue
            if fid <= 0:
                continue
            try:
                freq = float(cols[9] or 0)
            except ValueError:
                continue
            if freq <= 0:
                continue
            callsign = (cols[5] or "").strip() or None
            comm_city = (cols[0] or "").strip()
            comm_state = (cols[1] or "").strip()
            out[fid] = {
                "callsign": callsign,
                "freq": freq,           # MHz for FM, kHz for AM
                "service": service,
                "status": status,
                "comm_city": comm_city,
                "comm_state": comm_state,
                "fac_state": (cols[11] or "").strip(),
            }
            kept += 1
    log(f"  facility: {n} scanned, {kept} active AM/FM kept ({time.time()-t0:.1f}s)")
    return out


def load_fm_eng(zf_path: str, facilities: Dict[int, dict]) -> Dict[int, dict]:
    """Load fm_eng_data.dat LIC rows; return {facility_id: {station_class,
    lat_dd, lon_dd}}. Only emits entries for facilities we already have."""
    log(f"--- fm_eng: opening {os.path.basename(zf_path)} ---")
    t0 = time.time()
    out: Dict[int, dict] = {}
    n = 0; kept = 0
    with zipfile.ZipFile(zf_path) as zf:
        for cols in iter_dat(zf, "fm_eng_data.dat"):
            n += 1
            if len(cols) < 38:
                continue
            if cols[21] != FM_ENG_LIC:
                continue
            try:
                fid = int(cols[20])
            except ValueError:
                continue
            if fid not in facilities:
                continue
            station_class = (cols[19] or "").strip() or None
            lat = dms_to_dd(cols[30], cols[32], cols[33], cols[31])
            lon = dms_to_dd(cols[34], cols[36], cols[37], cols[35])
            # First LIC record wins per facility (rare duplicates).
            if fid not in out:
                out[fid] = {"station_class": station_class, "lat_dd": lat, "lon_dd": lon}
                kept += 1
    log(f"  fm_eng: {n} scanned, {kept} eng records kept ({time.time()-t0:.1f}s)")
    return out


def load_am_eng_facility_ids(zf_path: str) -> set:
    """AM has no clean lat/lon in am_eng_data.dat; we just collect the set of
    facility_ids that appear there to prove the engineering record exists."""
    log(f"--- am_eng: opening {os.path.basename(zf_path)} ---")
    t0 = time.time()
    fids = set()
    n = 0
    with zipfile.ZipFile(zf_path) as zf:
        for cols in iter_dat(zf, "am_eng_data.dat"):
            n += 1
            if len(cols) < 5:
                continue
            try:
                fids.add(int(cols[4]))
            except ValueError:
                continue
    log(f"  am_eng: {n} scanned, {len(fids)} unique facility_ids ({time.time()-t0:.1f}s)")
    return fids


def emit_rows(facilities: Dict[int, dict],
              fm_eng: Dict[int, dict],
              conn: sqlite3.Connection) -> Tuple[int, int]:
    """Materialize the broadcast_by_freq table from joined dicts. Returns
    (fm_inserted, am_inserted)."""
    cur = conn.cursor()
    cur.execute("BEGIN")
    fm_n = 0; am_n = 0
    batch = []
    BATCH = 5000
    for fid, fac in facilities.items():
        if fac["service"] == "FM":
            freq_hz = fac["freq"] * 1e6
            unit = "MHz"
            eng = fm_eng.get(fid, {})
            station_class = eng.get("station_class")
            lat = eng.get("lat_dd")
            lon = eng.get("lon_dd")
        else:  # AM
            freq_hz = fac["freq"] * 1e3
            unit = "kHz"
            station_class = None
            lat = None
            lon = None
        if freq_hz <= 0:
            continue
        callsign = fac["callsign"]
        community = ""
        if fac["comm_city"]:
            community = fac["comm_city"]
            if fac["comm_state"]:
                community += f", {fac['comm_state']}"
        # entity_name: synthesize '<CALL> (<city, st>)'. CDBS has no clean
        # licensee field in facility.dat; ship a useful placeholder so the
        # dashboard's 'Licensed to' column renders something. Future work:
        # join application.dat for the legal licensee LLC name.
        if callsign and community:
            entity_name = f"{callsign} ({community})"
        elif callsign:
            entity_name = callsign
        elif community:
            entity_name = community
        else:
            entity_name = None
        batch.append((
            freq_hz, callsign, entity_name, station_class,
            fac["service"], fac["status"], lat, lon,
            fid, community or None, unit,
        ))
        if fac["service"] == "FM": fm_n += 1
        else:                       am_n += 1
        if len(batch) >= BATCH:
            cur.executemany(
                "INSERT INTO broadcast_by_freq "
                "(freq_hz, callsign, entity_name, station_class, service, fac_status, "
                "lat_dd, lon_dd, facility_id, community, cdbs_freq_unit) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                batch
            )
            batch.clear()
    if batch:
        cur.executemany(
            "INSERT INTO broadcast_by_freq "
            "(freq_hz, callsign, entity_name, station_class, service, fac_status, "
            "lat_dd, lon_dd, facility_id, community, cdbs_freq_unit) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            batch
        )
    conn.commit()
    return fm_n, am_n


def build_indexes(conn: sqlite3.Connection):
    log("building indexes...")
    t0 = time.time()
    conn.executescript("""
        CREATE INDEX idx_broadcast_freq ON broadcast_by_freq(freq_hz);
        CREATE INDEX idx_broadcast_freq_loc ON broadcast_by_freq(freq_hz, lat_dd, lon_dd);
        CREATE INDEX idx_broadcast_callsign ON broadcast_by_freq(callsign);
        CREATE INDEX idx_broadcast_facility ON broadcast_by_freq(facility_id);
    """)
    conn.commit()
    log(f"indexes built in {time.time()-t0:.1f}s")


def write_meta(conn: sqlite3.Connection):
    cur = conn.cursor()
    total = cur.execute("SELECT COUNT(*) FROM broadcast_by_freq").fetchone()[0]
    fm = cur.execute("SELECT COUNT(*) FROM broadcast_by_freq WHERE service='FM'").fetchone()[0]
    am = cur.execute("SELECT COUNT(*) FROM broadcast_by_freq WHERE service='AM'").fetchone()[0]
    pos = cur.execute("SELECT COUNT(*) FROM broadcast_by_freq WHERE lat_dd IS NOT NULL").fetchone()[0]
    sc = cur.execute("SELECT COUNT(*) FROM broadcast_by_freq WHERE station_class IS NOT NULL").fetchone()[0]
    cur.execute("INSERT INTO meta(key,value) VALUES (?,?)", ("loaded_ts", str(time.time())))
    cur.execute("INSERT INTO meta(key,value) VALUES (?,?)", ("total_rows", str(total)))
    cur.execute("INSERT INTO meta(key,value) VALUES (?,?)", ("fm_rows", str(fm)))
    cur.execute("INSERT INTO meta(key,value) VALUES (?,?)", ("am_rows", str(am)))
    cur.execute("INSERT INTO meta(key,value) VALUES (?,?)", ("rows_with_position", str(pos)))
    cur.execute("INSERT INTO meta(key,value) VALUES (?,?)", ("rows_with_station_class", str(sc)))
    conn.commit()
    log(f"meta: total={total:,} fm={fm:,} am={am:,} with_pos={pos:,} with_class={sc:,}")


def main():
    log(f"starting CDBS load -> {DB_PATH}")
    t = time.time()
    conn = init_db(DB_PATH)

    facilities = load_facility(os.path.join(CDBS_RAW_DIR, "facility.zip"))
    fm_eng     = load_fm_eng(os.path.join(CDBS_RAW_DIR, "fm_eng_data.zip"), facilities)
    _ = load_am_eng_facility_ids(os.path.join(CDBS_RAW_DIR, "am_eng_data.zip"))

    fm_n, am_n = emit_rows(facilities, fm_eng, conn)
    log(f"emitted {fm_n} FM + {am_n} AM rows")

    build_indexes(conn)
    write_meta(conn)
    conn.execute("PRAGMA optimize")
    conn.close()
    log(f"DONE in {time.time()-t:.1f}s, total rows={fm_n+am_n:,}")


if __name__ == "__main__":
    main()
