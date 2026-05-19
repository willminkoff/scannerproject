"""Disco — FCC ULS loader.

Iterates the 4 zip files in disco/uls/raw/, parses pipe-delimited .dat files,
and populates a fresh `uls.sqlite` with a denormalized `licenses_by_freq`
table indexed on freq_hz for fast point-frequency lookup.

Empirical column indices (no header rows; 0-based):

HD (License header, 59 cols):
    [1] unique_system_identifier (USI)  — primary key for license
    [4] callsign
    [5] license_status                   ('A' = active; 'E' = expired; etc.)
    [6] radio_service_code               (e.g. 'PW','GX','YC','YH','HA','GB','RS','GM')

EN (Entity, 30 cols):
    [1] USI
    [7] entity_name

LO (Location, 51 cols):
    [1]  USI
    [8]  location_number
    [19] lat_deg, [20] lat_min, [21] lat_sec, [22] lat_dir ('N'/'S')
    [23] lon_deg, [24] lon_min, [25] lon_sec, [26] lon_dir ('E'/'W')

FR (Frequency, 30 cols):
    [1]  USI
    [6]  location_number
    [7]  antenna_number
    [8]  station_class                   ('FXO','MO','FB2C',...)
    [10] frequency_assigned (MHz, decimal string e.g. '173.26250000')

EM (Emission, 16 cols):
    [1] USI
    [5] location_number
    [6] antenna_number
    [7] frequency_assigned (MHz)
    [9] emission_designator             (e.g. '11K0F3E', '20K0F3E', '100HN0N')

AM (Amateur, 18 cols):
    [1] USI
    [4] callsign
    [6] operator_class                   ('A' / 'G' / 'E' / 'T' / 'N')

Schema strategy:
- One row per (USI, location_number, freq_assigned) joined: licensee + lat/lon
  + emission designator + station class.
- Amateur licenses (no FR/LO/EM) are stored in a sibling `amateur_callsigns`
  table; lookup_uls() falls back to that for amateur-band detections.
"""
import math
import os
import sqlite3
import sys
import time
import zipfile
from typing import Dict, Iterable, Optional, Tuple

ULS_RAW_DIR = "/home/ubuntu/scannerproject/disco/uls/raw"
DB_PATH = "/home/ubuntu/scannerproject/disco/state/uls.sqlite"

# Frequency range cutoffs (MHz). FR rows can include 4 GHz microwave links etc.
# We keep everything because the index handles out-of-range fine, but rows below
# 1 kHz or above 6 GHz are filtered as obvious junk.
MIN_FREQ_HZ = 1e3
MAX_FREQ_HZ = 6e9

# Zip layout: bands (LMpriv / LMcomm / market) follow the (HD,EN,LO,FR,EM)
# pattern. Amateur is special: no FR/LO/EM, just HD+EN+AM.
BAND_ZIPS = [
    ("l_LMpriv.zip",  "LMpriv"),
    ("l_LMcomm.zip",  "LMcomm"),
    ("l_market.zip",  "market"),
]
AMATEUR_ZIP = ("l_amat.zip", "amateur")


def log(msg: str):
    sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    sys.stderr.flush()


def dms_to_dd(deg: str, minute: str, sec: str, direction: str) -> Optional[float]:
    """Convert degrees/minutes/seconds + N/S/E/W → signed decimal degrees."""
    try:
        d = float(deg or 0); m = float(minute or 0); s = float(sec or 0)
    except ValueError:
        return None
    if d == 0 and m == 0 and s == 0:
        return None
    dd = d + m / 60.0 + s / 3600.0
    if direction in ("S", "W"):
        dd = -dd
    return dd


def iter_dat(zf: zipfile.ZipFile, name: str) -> Iterable[list]:
    """Yield split rows from a .dat file inside the zip."""
    try:
        info = zf.getinfo(name)
    except KeyError:
        return
    with zf.open(info) as f:
        for line in f:
            # latin-1 because some entity names contain non-utf8 bytes.
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

        CREATE TABLE licenses_by_freq (
            freq_hz REAL NOT NULL,
            callsign TEXT,
            entity_name TEXT,
            emission_designator TEXT,
            station_class TEXT,
            radio_service_code TEXT,
            license_status TEXT,
            lat_dd REAL,
            lon_dd REAL,
            unique_system_identifier INTEGER,
            source TEXT
        );

        CREATE TABLE amateur_callsigns (
            callsign TEXT PRIMARY KEY,
            entity_name TEXT,
            operator_class TEXT,
            state TEXT,
            zip TEXT,
            city TEXT,
            unique_system_identifier INTEGER
        );

        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    return conn


def load_band_zip(zf_path: str, source_label: str, conn: sqlite3.Connection) -> int:
    """Load LMpriv / LMcomm / market style zip (has HD, EN, LO, FR, EM).

    Returns number of rows inserted into licenses_by_freq.
    """
    log(f"--- {source_label}: opening {os.path.basename(zf_path)} ---")
    t0 = time.time()
    inserted = 0

    with zipfile.ZipFile(zf_path) as zf:
        # Pass 1: HD — keep active licenses only.
        # hd[USI] = (callsign, status, radio_service_code)
        hd: Dict[int, Tuple[str, str, str]] = {}
        n = 0
        for cols in iter_dat(zf, "HD.dat"):
            n += 1
            if len(cols) < 7:
                continue
            try:
                usi = int(cols[1])
            except ValueError:
                continue
            status = cols[5]
            if status != "A":
                continue
            hd[usi] = (cols[4], status, cols[6])
        log(f"  HD: {n} rows scanned, {len(hd)} active kept ({time.time()-t0:.1f}s)")

        # Pass 2: EN — entity_name keyed by USI (active only).
        # en[USI] = entity_name
        t1 = time.time()
        en: Dict[int, str] = {}
        n = 0
        for cols in iter_dat(zf, "EN.dat"):
            n += 1
            if len(cols) < 8:
                continue
            try:
                usi = int(cols[1])
            except ValueError:
                continue
            if usi not in hd:
                continue
            name = cols[7].strip()
            if name:
                en[usi] = name
        log(f"  EN: {n} rows scanned, {len(en)} matched ({time.time()-t1:.1f}s)")

        # Pass 3: LO — lat/lon by (USI, location_number).
        # lo[(USI, loc_num)] = (lat_dd, lon_dd)
        t2 = time.time()
        lo: Dict[Tuple[int, str], Tuple[Optional[float], Optional[float]]] = {}
        n = 0
        for cols in iter_dat(zf, "LO.dat"):
            n += 1
            if len(cols) < 27:
                continue
            try:
                usi = int(cols[1])
            except ValueError:
                continue
            if usi not in hd:
                continue
            loc_num = cols[8]
            lat = dms_to_dd(cols[19], cols[20], cols[21], cols[22])
            lon = dms_to_dd(cols[23], cols[24], cols[25], cols[26])
            if lat is None and lon is None:
                continue
            lo[(usi, loc_num)] = (lat, lon)
        log(f"  LO: {n} rows scanned, {len(lo)} positions kept ({time.time()-t2:.1f}s)")

        # Pass 4: EM — emission designator by (USI, loc_num, ant_num, freq_mhz_rounded).
        # We round freq to 8 decimals to match FR's string but key off USI+freq for
        # robustness (loc/ant numbering is loosely consistent).
        # em[(USI, freq_mhz_int_khz)] = emission_designator
        # Using kHz int keying because freq strings are e.g. '173.26250000' which
        # round-trips fine through float, but compare via int(freq*1000) for safety.
        t3 = time.time()
        em: Dict[Tuple[int, int], str] = {}
        n = 0
        for cols in iter_dat(zf, "EM.dat"):
            n += 1
            if len(cols) < 10:
                continue
            try:
                usi = int(cols[1])
                freq_mhz = float(cols[7] or 0)
            except ValueError:
                continue
            if usi not in hd or freq_mhz <= 0:
                continue
            desig = cols[9].strip()
            if not desig:
                continue
            key = (usi, int(round(freq_mhz * 1000)))  # kHz integer
            # First occurrence wins (rows are usually identical per USI+freq).
            if key not in em:
                em[key] = desig
        log(f"  EM: {n} rows scanned, {len(em)} designators kept ({time.time()-t3:.1f}s)")

        # Pass 5: FR — emit one row per (USI, loc_num, freq_assigned).
        t4 = time.time()
        cur = conn.cursor()
        cur.execute("BEGIN")
        batch = []
        BATCH_SIZE = 50_000
        n = 0
        for cols in iter_dat(zf, "FR.dat"):
            n += 1
            if len(cols) < 11:
                continue
            try:
                usi = int(cols[1])
            except ValueError:
                continue
            if usi not in hd:
                continue
            try:
                freq_mhz = float(cols[10] or 0)
            except ValueError:
                continue
            if freq_mhz <= 0:
                continue
            freq_hz = freq_mhz * 1e6
            if freq_hz < MIN_FREQ_HZ or freq_hz > MAX_FREQ_HZ:
                continue
            loc_num = cols[6]
            station_class = cols[8] or None
            callsign, status, rsc = hd[usi]
            entity_name = en.get(usi)
            lat, lon = lo.get((usi, loc_num), (None, None))
            desig = em.get((usi, int(round(freq_mhz * 1000))))
            batch.append((
                freq_hz, callsign, entity_name, desig, station_class,
                rsc, status, lat, lon, usi, source_label,
            ))
            if len(batch) >= BATCH_SIZE:
                cur.executemany(
                    "INSERT INTO licenses_by_freq "
                    "(freq_hz, callsign, entity_name, emission_designator, station_class, "
                    "radio_service_code, license_status, lat_dd, lon_dd, "
                    "unique_system_identifier, source) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    batch
                )
                inserted += len(batch)
                if inserted % 500_000 < BATCH_SIZE:
                    log(f"    {inserted:>9,d} rows inserted "
                        f"({inserted / max(time.time()-t4, 1e-3):.0f} rows/s)")
                batch.clear()
        if batch:
            cur.executemany(
                "INSERT INTO licenses_by_freq "
                "(freq_hz, callsign, entity_name, emission_designator, station_class, "
                "radio_service_code, license_status, lat_dd, lon_dd, "
                "unique_system_identifier, source) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                batch
            )
            inserted += len(batch)
            batch.clear()
        conn.commit()
        log(f"  FR: {n} rows scanned, {inserted} inserted ({time.time()-t4:.1f}s)")

    log(f"--- {source_label}: done in {time.time()-t0:.1f}s, {inserted} rows ---")
    return inserted


def load_amateur_zip(zf_path: str, conn: sqlite3.Connection) -> int:
    """Amateur licenses: HD + EN + AM (no FR/LO/EM).

    We dump amateur callsigns into both tables: a sibling `amateur_callsigns`
    keyed on callsign for direct callsign lookups, and licenses_by_freq with
    NULL freq_hz so they don't pollute frequency lookups but are still queryable
    by USI/callsign.
    """
    log(f"--- amateur: opening {os.path.basename(zf_path)} ---")
    t0 = time.time()

    with zipfile.ZipFile(zf_path) as zf:
        hd: Dict[int, Tuple[str, str, str]] = {}
        n = 0
        for cols in iter_dat(zf, "HD.dat"):
            n += 1
            if len(cols) < 7:
                continue
            try:
                usi = int(cols[1])
            except ValueError:
                continue
            if cols[5] != "A":
                continue
            hd[usi] = (cols[4], cols[5], cols[6])
        log(f"  HD: {n} rows, {len(hd)} active amateurs")

        en: Dict[int, Tuple[str, str, str, str]] = {}
        n = 0
        for cols in iter_dat(zf, "EN.dat"):
            n += 1
            if len(cols) < 19:
                continue
            try:
                usi = int(cols[1])
            except ValueError:
                continue
            if usi not in hd:
                continue
            name = cols[7].strip()
            city = cols[16].strip() if len(cols) > 16 else ""
            state = cols[17].strip() if len(cols) > 17 else ""
            zip_ = cols[18].strip() if len(cols) > 18 else ""
            en[usi] = (name, city, state, zip_)
        log(f"  EN: {n} rows, {len(en)} matched")

        am: Dict[int, str] = {}  # USI -> operator_class
        n = 0
        for cols in iter_dat(zf, "AM.dat"):
            n += 1
            if len(cols) < 7:
                continue
            try:
                usi = int(cols[1])
            except ValueError:
                continue
            if usi not in hd:
                continue
            am[usi] = cols[6]
        log(f"  AM: {n} rows, {len(am)} matched")

        cur = conn.cursor()
        cur.execute("BEGIN")
        am_rows = []
        for usi, (callsign, status, rsc) in hd.items():
            name, city, state, zip_ = en.get(usi, ("", "", "", ""))
            op_class = am.get(usi, "")
            am_rows.append((callsign, name, op_class, state, zip_, city, usi))
        # Amateur callsigns live ONLY in `amateur_callsigns`; the lookup_uls
        # band-fallback path queries that table directly. Inserting NULL freq_hz
        # rows into licenses_by_freq adds no value (and trips the NOT NULL).
        cur.executemany(
            "INSERT OR REPLACE INTO amateur_callsigns "
            "(callsign, entity_name, operator_class, state, zip, city, unique_system_identifier) "
            "VALUES (?,?,?,?,?,?,?)",
            am_rows
        )
        conn.commit()
        log(f"--- amateur: done in {time.time()-t0:.1f}s, {len(am_rows)} amateur rows ---")
        return 0


def build_indexes(conn: sqlite3.Connection):
    log("building indexes...")
    t0 = time.time()
    conn.executescript("""
        CREATE INDEX idx_licenses_freq ON licenses_by_freq(freq_hz);
        CREATE INDEX idx_licenses_freq_loc ON licenses_by_freq(freq_hz, lat_dd, lon_dd);
        CREATE INDEX idx_licenses_callsign ON licenses_by_freq(callsign);
        CREATE INDEX idx_licenses_usi ON licenses_by_freq(unique_system_identifier);
        CREATE INDEX idx_amateur_state ON amateur_callsigns(state);
    """)
    conn.commit()
    log(f"indexes built in {time.time()-t0:.1f}s")


def write_meta(conn: sqlite3.Connection):
    cur = conn.cursor()
    total = cur.execute("SELECT COUNT(*) FROM licenses_by_freq").fetchone()[0]
    am_total = cur.execute("SELECT COUNT(*) FROM amateur_callsigns").fetchone()[0]
    pos_total = cur.execute(
        "SELECT COUNT(*) FROM licenses_by_freq WHERE lat_dd IS NOT NULL"
    ).fetchone()[0]
    em_total = cur.execute(
        "SELECT COUNT(*) FROM licenses_by_freq WHERE emission_designator IS NOT NULL"
    ).fetchone()[0]
    cur.execute("INSERT INTO meta(key, value) VALUES (?, ?)", ("loaded_ts", str(time.time())))
    cur.execute("INSERT INTO meta(key, value) VALUES (?, ?)", ("total_rows", str(total)))
    cur.execute("INSERT INTO meta(key, value) VALUES (?, ?)", ("rows_with_position", str(pos_total)))
    cur.execute("INSERT INTO meta(key, value) VALUES (?, ?)", ("rows_with_emission", str(em_total)))
    cur.execute("INSERT INTO meta(key, value) VALUES (?, ?)", ("amateur_rows", str(am_total)))
    conn.commit()
    log(f"meta: total={total:,} with_pos={pos_total:,} with_emission={em_total:,} amateur={am_total:,}")


def main():
    log(f"starting ULS load → {DB_PATH}")
    t_overall = time.time()
    conn = init_db(DB_PATH)
    total = 0
    for fname, label in BAND_ZIPS:
        path = os.path.join(ULS_RAW_DIR, fname)
        if not os.path.exists(path):
            log(f"skipping missing {path}")
            continue
        total += load_band_zip(path, label, conn)
    am_path = os.path.join(ULS_RAW_DIR, AMATEUR_ZIP[0])
    if os.path.exists(am_path):
        total += load_amateur_zip(am_path, conn)
    build_indexes(conn)
    write_meta(conn)
    conn.execute("PRAGMA optimize")
    conn.close()
    log(f"DONE in {time.time()-t_overall:.1f}s, total rows={total:,}")


if __name__ == "__main__":
    main()
