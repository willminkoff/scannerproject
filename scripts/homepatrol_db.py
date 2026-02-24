#!/usr/bin/env python3
"""Import HomePatrol HPDB files into SQLite and generate digital profiles."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import sqlite3
from pathlib import Path
from typing import Iterable, Sequence


DEFAULT_DB_PATH = Path("data/homepatrol.db")


def _field(parts: Sequence[str], idx: int) -> str:
    if idx < 0 or idx >= len(parts):
        return ""
    return (parts[idx] or "").strip()


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _kv_pairs(parts: Sequence[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for token in parts:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key:
            out.append((key, value))
    return out


def _kv_first(pairs: Sequence[tuple[str, str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in pairs:
        if key not in out:
            out[key] = value
    return out


def _detect_parent(
    pairs: Sequence[tuple[str, str]],
    ignore: set[str] | None = None,
) -> tuple[str | None, int | None]:
    ignore = ignore or set()
    for key, value in pairs:
        if key in ignore:
            continue
        ident = _to_int(value)
        if ident is not None:
            return key, ident
    return None, None


def _mhz_from_hz(freq_hz: int) -> str:
    mhz = freq_hz / 1_000_000.0
    return f"{mhz:.6f}".rstrip("0").rstrip(".")


def _slug(text: str) -> str:
    chars: list[str] = []
    prev_dash = False
    for ch in text.strip().lower():
        if ("a" <= ch <= "z") or ("0" <= ch <= "9"):
            chars.append(ch)
            prev_dash = False
            continue
        if ch in {" ", "-", "_", "."} and not prev_dash:
            chars.append("-")
            prev_dash = True
    while chars and chars[-1] == "-":
        chars.pop()
    while chars and chars[0] == "-":
        chars = chars[1:]
    if not chars:
        return "profile"
    return "".join(chars[:64])


def _escape_label(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace('"', '\\"')


def _mode_to_modulation(mode: str) -> str:
    mode_u = (mode or "").strip().upper()
    if mode_u == "AM":
        return "am"
    return "nfm"


def _resolve_conventional_system(
    conn: sqlite3.Connection,
    query: str,
    state: str = "",
    county: str = "",
) -> tuple[str, str] | None:
    filters = []
    params: list[object] = []
    if state:
        filters.append("(lower(st.abbr) = ? OR lower(st.name) = ?)")
        params.extend([state.lower(), state.lower()])
    if county:
        filters.append("lower(co.name) LIKE ?")
        params.append(f"%{county.lower()}%")
    extra = f" AND {' AND '.join(filters)}" if filters else ""

    exact = conn.execute(
        f"""
        SELECT cs.system_key, cs.system_name, COALESCE(st.abbr, ''), COALESCE(co.name, '')
        FROM conventional_systems cs
        LEFT JOIN states st ON st.state_id = cs.state_id
        LEFT JOIN counties co ON co.county_id = cs.county_id
        WHERE lower(cs.system_name) = lower(?){extra}
        ORDER BY cs.system_name, cs.system_key
        """,
        [query, *params],
    ).fetchall()
    if len(exact) == 1:
        return exact[0][0], exact[0][1]
    if len(exact) > 1:
        print("error: conventional system query is ambiguous; exact matches:")
        for key, name, st, co in exact[:25]:
            print(f"  {key}\t{name}\t{st}\t{co}")
        if len(exact) > 25:
            print(f"  ... {len(exact) - 25} more")
        return None

    like = conn.execute(
        f"""
        SELECT cs.system_key, cs.system_name, COALESCE(st.abbr, ''), COALESCE(co.name, '')
        FROM conventional_systems cs
        LEFT JOIN states st ON st.state_id = cs.state_id
        LEFT JOIN counties co ON co.county_id = cs.county_id
        WHERE lower(cs.system_name) LIKE ?{extra}
        ORDER BY cs.system_name, cs.system_key
        """,
        [f"%{query.lower()}%", *params],
    ).fetchall()
    if len(like) == 1:
        return like[0][0], like[0][1]
    if not like:
        return None
    print("error: conventional system query is ambiguous; matches:")
    for key, name, st, co in like[:25]:
        print(f"  {key}\t{name}\t{st}\t{co}")
    if len(like) > 25:
        print(f"  ... {len(like) - 25} more")
    return None


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS states (
    state_id INTEGER PRIMARY KEY,
    country_id INTEGER,
    name TEXT NOT NULL,
    abbr TEXT
);

CREATE TABLE IF NOT EXISTS counties (
    county_id INTEGER PRIMARY KEY,
    state_id INTEGER,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_areas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL,
    record_type TEXT NOT NULL,
    entity_kind TEXT NOT NULL,
    entity_id INTEGER,
    state_id INTEGER,
    county_id INTEGER
);

CREATE TABLE IF NOT EXISTS conventional_systems (
    system_key TEXT PRIMARY KEY,
    source_file TEXT NOT NULL,
    system_name TEXT NOT NULL,
    state_id INTEGER,
    county_id INTEGER,
    agency_id INTEGER,
    category TEXT
);

CREATE TABLE IF NOT EXISTS conventional_groups (
    cgroup_id INTEGER PRIMARY KEY,
    source_file TEXT NOT NULL,
    parent_key TEXT,
    parent_id INTEGER,
    group_name TEXT NOT NULL,
    latitude REAL,
    longitude REAL,
    radius REAL,
    shape TEXT
);

CREATE TABLE IF NOT EXISTS conventional_freqs (
    cfreq_id INTEGER PRIMARY KEY,
    source_file TEXT NOT NULL,
    cgroup_id INTEGER,
    alpha_tag TEXT NOT NULL,
    freq_hz INTEGER,
    mode TEXT,
    tone TEXT,
    service_tag INTEGER
);

CREATE TABLE IF NOT EXISTS trunk_systems (
    trunk_id INTEGER PRIMARY KEY,
    source_file TEXT NOT NULL,
    state_id INTEGER,
    system_name TEXT NOT NULL,
    system_type TEXT,
    protocol TEXT
);

CREATE TABLE IF NOT EXISTS trunk_sites (
    site_id INTEGER PRIMARY KEY,
    source_file TEXT NOT NULL,
    trunk_id INTEGER,
    site_name TEXT NOT NULL,
    latitude REAL,
    longitude REAL,
    radius REAL,
    site_mode TEXT,
    bandplan TEXT,
    width TEXT,
    shape TEXT
);

CREATE TABLE IF NOT EXISTS trunk_freqs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL,
    site_id INTEGER NOT NULL,
    tfreq_id TEXT,
    freq_hz INTEGER,
    lcn TEXT,
    UNIQUE(site_id, freq_hz, lcn)
);

CREATE TABLE IF NOT EXISTS trunk_groups (
    tgroup_id INTEGER PRIMARY KEY,
    source_file TEXT NOT NULL,
    trunk_id INTEGER,
    group_name TEXT NOT NULL,
    latitude REAL,
    longitude REAL,
    radius REAL,
    shape TEXT
);

CREATE TABLE IF NOT EXISTS talkgroups (
    tid INTEGER PRIMARY KEY,
    source_file TEXT NOT NULL,
    tgroup_id INTEGER,
    alpha_tag TEXT NOT NULL,
    dec_tgid TEXT,
    mode TEXT,
    service_tag INTEGER
);

CREATE INDEX IF NOT EXISTS idx_trunk_systems_name ON trunk_systems(system_name);
CREATE INDEX IF NOT EXISTS idx_trunk_sites_trunk ON trunk_sites(trunk_id);
CREATE INDEX IF NOT EXISTS idx_trunk_groups_trunk ON trunk_groups(trunk_id);
CREATE INDEX IF NOT EXISTS idx_talkgroups_tgroup ON talkgroups(tgroup_id);
CREATE INDEX IF NOT EXISTS idx_talkgroups_dec ON talkgroups(dec_tgid);
CREATE INDEX IF NOT EXISTS idx_conv_groups_parent ON conventional_groups(parent_key, parent_id);
"""


DROP_SQL = """
DROP TABLE IF EXISTS talkgroups;
DROP TABLE IF EXISTS trunk_groups;
DROP TABLE IF EXISTS trunk_freqs;
DROP TABLE IF EXISTS trunk_sites;
DROP TABLE IF EXISTS trunk_systems;
DROP TABLE IF EXISTS conventional_freqs;
DROP TABLE IF EXISTS conventional_groups;
DROP TABLE IF EXISTS conventional_systems;
DROP TABLE IF EXISTS entity_areas;
DROP TABLE IF EXISTS counties;
DROP TABLE IF EXISTS states;
DROP TABLE IF EXISTS meta;
"""


def _create_schema(conn: sqlite3.Connection, reset: bool) -> None:
    if reset:
        conn.executescript(DROP_SQL)
    conn.executescript(SCHEMA_SQL)


def _import_config(conn: sqlite3.Connection, config_path: Path) -> dict[str, int]:
    counts = {"states": 0, "counties": 0}
    if not config_path.is_file():
        return counts
    with config_path.open("r", encoding="utf-8", errors="ignore", newline="") as handle:
        for raw in handle:
            line = raw.rstrip("\r\n")
            if not line:
                continue
            parts = line.split("\t")
            rec = _field(parts, 0)
            if rec == "StateInfo":
                pairs = _kv_pairs(parts[1:])
                kv = _kv_first(pairs)
                state_id = _to_int(kv.get("StateId"))
                country_id = _to_int(kv.get("CountryId"))
                name = _field(parts, 3)
                abbr = _field(parts, 4)
                if state_id is None or not name:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO states(state_id, country_id, name, abbr)
                    VALUES (?, ?, ?, ?)
                    """,
                    (state_id, country_id, name, abbr or None),
                )
                counts["states"] += 1
            elif rec == "CountyInfo":
                pairs = _kv_pairs(parts[1:])
                kv = _kv_first(pairs)
                county_id = _to_int(kv.get("CountyId"))
                state_id = _to_int(kv.get("StateId"))
                name = _field(parts, 3)
                if county_id is None or not name:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO counties(county_id, state_id, name)
                    VALUES (?, ?, ?)
                    """,
                    (county_id, state_id, name),
                )
                counts["counties"] += 1
    return counts


def _extract_area_mapping(
    record_type: str,
    pairs: Sequence[tuple[str, str]],
) -> tuple[str | None, int | None, int | None, int | None]:
    entity_kind: str | None = None
    entity_id: int | None = None
    state_id: int | None = None
    county_id: int | None = None

    if record_type == "AreaState":
        for key, value in pairs:
            if key == "StateId":
                state_id = _to_int(value)
                continue
            if entity_kind is None:
                entity_kind = key
                entity_id = _to_int(value)
    elif record_type == "AreaCounty":
        if len(pairs) >= 2 and pairs[0][0] == "CountyId" and pairs[1][0] == "CountyId":
            entity_kind = "CountyId"
            entity_id = _to_int(pairs[0][1])
            county_id = _to_int(pairs[1][1])
            return entity_kind, entity_id, state_id, county_id
        for key, value in pairs:
            if key == "CountyId":
                county_id = _to_int(value)
                continue
            if entity_kind is None:
                entity_kind = key
                entity_id = _to_int(value)
    return entity_kind, entity_id, state_id, county_id


def _import_hpd_file(conn: sqlite3.Connection, hpd_path: Path) -> dict[str, int]:
    source = hpd_path.name
    counts = {
        "conventional_systems": 0,
        "conventional_groups": 0,
        "conventional_freqs": 0,
        "trunk_systems": 0,
        "trunk_sites": 0,
        "trunk_freqs": 0,
        "trunk_groups": 0,
        "talkgroups": 0,
        "entity_areas": 0,
    }
    with hpd_path.open("r", encoding="utf-8", errors="ignore", newline="") as handle:
        for raw in handle:
            line = raw.rstrip("\r\n")
            if not line:
                continue
            parts = line.split("\t")
            rec = _field(parts, 0)
            pairs = _kv_pairs(parts[1:])
            kv = _kv_first(pairs)

            if rec == "Conventional":
                state_id = _to_int(kv.get("StateId"))
                county_id = _to_int(kv.get("CountyId"))
                agency_id = _to_int(kv.get("AgencyId"))
                system_name = _field(parts, 3)
                category = _field(parts, 6)
                parent_kind, parent_id = _detect_parent(
                    pairs,
                    ignore={"StateId"},
                )
                if not system_name:
                    continue
                if parent_kind and parent_id is not None:
                    system_key = f"{parent_kind}:{parent_id}"
                else:
                    system_key = f"{source}:{system_name}"
                conn.execute(
                    """
                    INSERT OR REPLACE INTO conventional_systems(
                        system_key, source_file, system_name, state_id, county_id, agency_id, category
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (system_key, source, system_name, state_id, county_id, agency_id, category or None),
                )
                counts["conventional_systems"] += 1
                continue

            if rec == "C-Group":
                cgroup_id = _to_int(kv.get("CGroupId"))
                if cgroup_id is None:
                    continue
                parent_kind, parent_id = _detect_parent(
                    pairs,
                    ignore={"CGroupId"},
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO conventional_groups(
                        cgroup_id, source_file, parent_key, parent_id, group_name,
                        latitude, longitude, radius, shape
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cgroup_id,
                        source,
                        parent_kind,
                        parent_id,
                        _field(parts, 3),
                        _to_float(_field(parts, 5)),
                        _to_float(_field(parts, 6)),
                        _to_float(_field(parts, 7)),
                        _field(parts, 8) or None,
                    ),
                )
                counts["conventional_groups"] += 1
                continue

            if rec == "C-Freq":
                cfreq_id = _to_int(kv.get("CFreqId"))
                cgroup_id = _to_int(kv.get("CGroupId"))
                if cfreq_id is None:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO conventional_freqs(
                        cfreq_id, source_file, cgroup_id, alpha_tag, freq_hz, mode, tone, service_tag
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cfreq_id,
                        source,
                        cgroup_id,
                        _field(parts, 3),
                        _to_int(_field(parts, 5)),
                        _field(parts, 6) or None,
                        _field(parts, 7) or None,
                        _to_int(_field(parts, 8)),
                    ),
                )
                counts["conventional_freqs"] += 1
                continue

            if rec == "Trunk":
                trunk_id = _to_int(kv.get("TrunkId"))
                if trunk_id is None:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO trunk_systems(
                        trunk_id, source_file, state_id, system_name, system_type, protocol
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trunk_id,
                        source,
                        _to_int(kv.get("StateId")),
                        _field(parts, 3),
                        _field(parts, 5) or None,
                        _field(parts, 6) or None,
                    ),
                )
                counts["trunk_systems"] += 1
                continue

            if rec == "Site":
                site_id = _to_int(kv.get("SiteId"))
                if site_id is None:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO trunk_sites(
                        site_id, source_file, trunk_id, site_name, latitude, longitude, radius,
                        site_mode, bandplan, width, shape
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        site_id,
                        source,
                        _to_int(kv.get("TrunkId")),
                        _field(parts, 3),
                        _to_float(_field(parts, 5)),
                        _to_float(_field(parts, 6)),
                        _to_float(_field(parts, 7)),
                        _field(parts, 8) or None,
                        _field(parts, 9) or None,
                        _field(parts, 10) or None,
                        _field(parts, 11) or None,
                    ),
                )
                counts["trunk_sites"] += 1
                continue

            if rec == "T-Freq":
                site_id = _to_int(kv.get("SiteId"))
                if site_id is None:
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO trunk_freqs(source_file, site_id, tfreq_id, freq_hz, lcn)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        source,
                        site_id,
                        kv.get("TFreqId"),
                        _to_int(_field(parts, 5)),
                        _field(parts, 6) or None,
                    ),
                )
                counts["trunk_freqs"] += 1
                continue

            if rec == "T-Group":
                tgroup_id = _to_int(kv.get("TGroupId"))
                if tgroup_id is None:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO trunk_groups(
                        tgroup_id, source_file, trunk_id, group_name, latitude, longitude, radius, shape
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tgroup_id,
                        source,
                        _to_int(kv.get("TrunkId")),
                        _field(parts, 3),
                        _to_float(_field(parts, 5)),
                        _to_float(_field(parts, 6)),
                        _to_float(_field(parts, 7)),
                        _field(parts, 8) or None,
                    ),
                )
                counts["trunk_groups"] += 1
                continue

            if rec == "TGID":
                tid = _to_int(kv.get("Tid"))
                if tid is None:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO talkgroups(
                        tid, source_file, tgroup_id, alpha_tag, dec_tgid, mode, service_tag
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tid,
                        source,
                        _to_int(kv.get("TGroupId")),
                        _field(parts, 3),
                        _field(parts, 5),
                        _field(parts, 6) or None,
                        _to_int(_field(parts, 7)),
                    ),
                )
                counts["talkgroups"] += 1
                continue

            if rec in {"AreaState", "AreaCounty"}:
                entity_kind, entity_id, state_id, county_id = _extract_area_mapping(rec, pairs)
                if not entity_kind:
                    continue
                conn.execute(
                    """
                    INSERT INTO entity_areas(
                        source_file, record_type, entity_kind, entity_id, state_id, county_id
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (source, rec, entity_kind, entity_id, state_id, county_id),
                )
                counts["entity_areas"] += 1
                continue
    return counts


def cmd_import(args: argparse.Namespace) -> int:
    hpdb_root = Path(args.hpdb_root).expanduser()
    db_path = Path(args.db).expanduser()
    if not hpdb_root.is_dir():
        print(f"error: HPDB path not found: {hpdb_root}")
        return 2

    hpd_files = sorted(hpdb_root.glob("*.hpd"), key=lambda p: p.name.lower())
    if not hpd_files:
        print(f"error: no .hpd files found in {hpdb_root}")
        return 2

    conn = _connect(db_path)
    try:
        _create_schema(conn, reset=not args.no_reset)
        now = dt.datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            ("last_import_started", now),
        )
        cfg_counts = _import_config(conn, hpdb_root / "HPDB.config")
        totals = {
            "conventional_systems": 0,
            "conventional_groups": 0,
            "conventional_freqs": 0,
            "trunk_systems": 0,
            "trunk_sites": 0,
            "trunk_freqs": 0,
            "trunk_groups": 0,
            "talkgroups": 0,
            "entity_areas": 0,
        }
        for hpd_file in hpd_files:
            file_counts = _import_hpd_file(conn, hpd_file)
            for key in totals:
                totals[key] += file_counts.get(key, 0)
            if args.verbose:
                print(f"imported {hpd_file.name}")

        finished = dt.datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            ("last_import_finished", finished),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            ("last_import_source", str(hpdb_root)),
        )
        conn.commit()
    finally:
        conn.close()

    print(f"Database: {db_path}")
    print(f"Imported from: {hpdb_root}")
    print(f"States: {cfg_counts['states']}, Counties: {cfg_counts['counties']}")
    for key in (
        "conventional_systems",
        "conventional_groups",
        "conventional_freqs",
        "trunk_systems",
        "trunk_sites",
        "trunk_freqs",
        "trunk_groups",
        "talkgroups",
        "entity_areas",
    ):
        print(f"{key}: {totals[key]}")
    return 0


def _print_rows(headers: Sequence[str], rows: Iterable[Sequence[object]]) -> None:
    print("\t".join(headers))
    for row in rows:
        values = ["" if value is None else str(value) for value in row]
        print("\t".join(values))


def cmd_find_system(args: argparse.Namespace) -> int:
    conn = _connect(Path(args.db).expanduser())
    try:
        needle = f"%{args.query.lower()}%"
        rows: list[tuple[object, ...]] = []
        if args.kind in {"all", "trunk"}:
            rows.extend(
                conn.execute(
                    """
                    SELECT
                        'trunk' AS kind,
                        ts.trunk_id AS id,
                        ts.system_name AS name,
                        COALESCE(st.abbr, '') AS state,
                        COALESCE(ts.protocol, '') AS extra
                    FROM trunk_systems ts
                    LEFT JOIN states st ON st.state_id = ts.state_id
                    WHERE lower(ts.system_name) LIKE ?
                    ORDER BY ts.system_name
                    LIMIT ?
                    """,
                    (needle, args.limit),
                ).fetchall()
            )
        if args.kind in {"all", "conventional"}:
            rows.extend(
                conn.execute(
                    """
                    SELECT
                        'conventional' AS kind,
                        cs.system_key AS id,
                        cs.system_name AS name,
                        COALESCE(st.abbr, '') AS state,
                        COALESCE(cs.category, '') AS extra
                    FROM conventional_systems cs
                    LEFT JOIN states st ON st.state_id = cs.state_id
                    WHERE lower(cs.system_name) LIKE ?
                    ORDER BY cs.system_name
                    LIMIT ?
                    """,
                    (needle, args.limit),
                ).fetchall()
            )
    finally:
        conn.close()

    _print_rows(("kind", "id", "name", "state", "extra"), rows[: args.limit])
    return 0


def cmd_find_talkgroup(args: argparse.Namespace) -> int:
    conn = _connect(Path(args.db).expanduser())
    try:
        where = """
            (
                lower(t.alpha_tag) LIKE :needle
                OR lower(t.dec_tgid) LIKE :needle
                OR lower(tg.group_name) LIKE :needle
                OR lower(ts.system_name) LIKE :needle
            )
            AND t.dec_tgid GLOB '[0-9]*'
        """
        params: dict[str, object] = {"needle": f"%{args.query.lower()}%", "limit": args.limit}
        if args.system:
            where += " AND lower(ts.system_name) LIKE :system"
            params["system"] = f"%{args.system.lower()}%"
        rows = conn.execute(
            f"""
            SELECT
                ts.system_name,
                tg.group_name,
                t.dec_tgid,
                t.alpha_tag,
                COALESCE(t.mode, ''),
                COALESCE(t.service_tag, '')
            FROM talkgroups t
            JOIN trunk_groups tg ON tg.tgroup_id = t.tgroup_id
            JOIN trunk_systems ts ON ts.trunk_id = tg.trunk_id
            WHERE {where}
            ORDER BY ts.system_name,
                     CAST(t.dec_tgid AS INTEGER),
                     t.alpha_tag
            LIMIT :limit
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    _print_rows(("system", "group", "dec", "alpha", "mode", "service_tag"), rows)
    return 0


def _resolve_system(conn: sqlite3.Connection, query: str) -> tuple[int, str] | None:
    exact = conn.execute(
        """
        SELECT trunk_id, system_name
        FROM trunk_systems
        WHERE lower(system_name) = lower(?)
        ORDER BY trunk_id
        """,
        (query,),
    ).fetchall()
    if len(exact) == 1:
        return exact[0][0], exact[0][1]
    if len(exact) > 1:
        return None

    like = conn.execute(
        """
        SELECT trunk_id, system_name
        FROM trunk_systems
        WHERE lower(system_name) LIKE ?
        ORDER BY system_name
        """,
        (f"%{query.lower()}%",),
    ).fetchall()
    if len(like) == 1:
        return like[0][0], like[0][1]
    if not like:
        return None
    print("error: system query is ambiguous; matches:")
    for trunk_id, name in like[:25]:
        print(f"  {trunk_id}\t{name}")
    if len(like) > 25:
        print(f"  ... {len(like) - 25} more")
    return None


def cmd_site_freqs(args: argparse.Namespace) -> int:
    conn = _connect(Path(args.db).expanduser())
    try:
        resolved = _resolve_system(conn, args.system)
        if not resolved:
            print(f"error: unable to resolve system query: {args.system}")
            return 2
        trunk_id, system_name = resolved
        params: list[object] = [trunk_id]
        where = "s.trunk_id = ?"
        if args.site:
            where += " AND lower(s.site_name) LIKE ?"
            params.append(f"%{args.site.lower()}%")
        rows = conn.execute(
            f"""
            SELECT s.site_name, f.freq_hz, COALESCE(f.lcn, '')
            FROM trunk_sites s
            JOIN trunk_freqs f ON f.site_id = s.site_id
            WHERE {where}
              AND f.freq_hz > 0
            ORDER BY s.site_name,
                     CASE WHEN f.lcn GLOB '[0-9]*' THEN CAST(f.lcn AS INTEGER) ELSE 0 END,
                     f.freq_hz
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    print(f"# system: {system_name}")
    _print_rows(("site", "freq_mhz", "freq_hz", "lcn"), ((r[0], _mhz_from_hz(r[1]), r[1], r[2]) for r in rows))
    return 0


def _write_talkgroup_csv(path: Path, rows: Sequence[tuple[object, ...]], with_group: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if with_group:
            writer.writerow(["Group", "DEC", "HEX", "Mode", "Alpha Tag", "Description", "Tag"])
        else:
            writer.writerow(["DEC", "HEX", "Mode", "Alpha Tag", "Description", "Tag"])
        for row in rows:
            dec = str(row[3] or "").strip()
            if not dec.isdigit():
                continue
            dec_int = int(dec)
            hex_val = f"{dec_int:X}"
            mode = str(row[4] or "")
            alpha = str(row[5] or "")
            description = str(row[2] or "")
            service_tag = str(row[6] or "")
            tag = f"Svc {service_tag}" if service_tag else ""
            if with_group:
                writer.writerow([row[2], dec, hex_val, mode, alpha, description, tag])
            else:
                writer.writerow([dec, hex_val, mode, alpha, description, tag])


def cmd_build_profile(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).expanduser()
    db_path = Path(args.db).expanduser()
    conn = _connect(db_path)
    try:
        resolved = _resolve_system(conn, args.system)
        if not resolved:
            print(f"error: unable to resolve system query: {args.system}")
            return 2
        trunk_id, system_name = resolved

        site_rows = conn.execute(
            """
            SELECT site_id, site_name
            FROM trunk_sites
            WHERE trunk_id = ?
            ORDER BY site_name
            """,
            (trunk_id,),
        ).fetchall()
        if not site_rows:
            print(f"error: no sites found for system: {system_name}")
            return 2

        selected_sites = site_rows
        if args.site:
            wanted = [item.lower() for item in args.site]
            selected_sites = [
                row for row in site_rows if any(part in str(row[1]).lower() for part in wanted)
            ]
            if not selected_sites:
                print("error: no sites matched filter")
                for _, site_name in site_rows:
                    print(f"  {site_name}")
                return 2

        site_ids = [int(row[0]) for row in selected_sites]
        placeholders = ",".join("?" for _ in site_ids)
        freq_rows = conn.execute(
            f"""
            SELECT DISTINCT freq_hz
            FROM trunk_freqs
            WHERE site_id IN ({placeholders})
              AND freq_hz > 0
            ORDER BY freq_hz
            """,
            site_ids,
        ).fetchall()
        if not freq_rows:
            print("error: no frequencies found for selected sites")
            return 2

        where = ["tg.trunk_id = ?", "t.dec_tgid GLOB '[0-9]*'"]
        params: list[object] = [trunk_id]
        if args.group:
            group_filters = [f"%{g.lower()}%" for g in args.group]
            where.append("(" + " OR ".join("lower(tg.group_name) LIKE ?" for _ in group_filters) + ")")
            params.extend(group_filters)
        if args.talkgroup:
            tg_filters = [f"%{g.lower()}%" for g in args.talkgroup]
            where.append(
                "("
                + " OR ".join(
                    "(lower(t.alpha_tag) LIKE ? OR lower(t.dec_tgid) LIKE ?)"
                    for _ in tg_filters
                )
                + ")"
            )
            for item in tg_filters:
                params.extend([item, item])

        tg_rows = conn.execute(
            f"""
            SELECT
                ts.system_name,
                ts.trunk_id,
                tg.group_name,
                t.dec_tgid,
                COALESCE(t.mode, ''),
                COALESCE(t.alpha_tag, ''),
                COALESCE(t.service_tag, '')
            FROM talkgroups t
            JOIN trunk_groups tg ON tg.tgroup_id = t.tgroup_id
            JOIN trunk_systems ts ON ts.trunk_id = tg.trunk_id
            WHERE {" AND ".join(where)}
            ORDER BY CAST(t.dec_tgid AS INTEGER), t.alpha_tag
            """,
            params,
        ).fetchall()
        if not tg_rows:
            print("error: no talkgroups matched filters")
            return 2

        if args.max_talkgroups > 0:
            tg_rows = tg_rows[: args.max_talkgroups]

        profile_name = args.profile_name.strip() if args.profile_name else system_name
        profile_slug = _slug(args.profile_slug) if args.profile_slug else _slug(profile_name)
        profile_dir = out_dir / profile_slug
        profile_dir.mkdir(parents=True, exist_ok=True)

        control_path = profile_dir / "control_channels.txt"
        with control_path.open("w", encoding="utf-8") as handle:
            for (freq_hz,) in freq_rows:
                if not freq_hz:
                    continue
                handle.write(f"{_mhz_from_hz(int(freq_hz))}\n")

        _write_talkgroup_csv(profile_dir / "talkgroups.csv", tg_rows, with_group=False)
        _write_talkgroup_csv(profile_dir / "talkgroups_with_group.csv", tg_rows, with_group=True)

        readme_path = profile_dir / "README.md"
        generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        site_list = ", ".join(str(row[1]) for row in selected_sites)
        readme_path.write_text(
            (
                f"# {profile_name}\n\n"
                f"- Generated: {generated}\n"
                f"- Source DB: `{db_path}`\n"
                f"- Trunk ID: `{trunk_id}`\n"
                f"- Sites: {site_list}\n"
                f"- Control frequencies: `{len(freq_rows)}` entries in `control_channels.txt`\n"
                f"- Talkgroups: `{len(tg_rows)}` rows in `talkgroups.csv`\n"
                f"- Grouped copy: `talkgroups_with_group.csv`\n"
            ),
            encoding="utf-8",
        )
    finally:
        conn.close()

    print(f"Profile created: {profile_dir}")
    print(f"Control channels: {len(freq_rows)}")
    print(f"Talkgroups: {len(tg_rows)}")
    return 0


def _write_analog_profile(
    path: Path,
    rows: Sequence[tuple[int, int, str, str, str]],
    airband_flag: bool,
    device_index: int,
    serial: str,
    gain: float,
    squelch_dbfs: int,
    mountpoint: str,
    stream_name: str,
    genre: str,
    description: str,
) -> None:
    by_mod: dict[str, list[tuple[int, str]]] = {}
    seen: dict[str, set[int]] = {}
    for _cfreq_id, freq_hz, mode, alpha, group_name in rows:
        mod = _mode_to_modulation(mode)
        by_mod.setdefault(mod, [])
        seen.setdefault(mod, set())
        if freq_hz in seen[mod]:
            continue
        seen[mod].add(freq_hz)
        label = alpha.strip() or group_name.strip() or _mhz_from_hz(freq_hz)
        by_mod[mod].append((freq_hz, label))

    order = [mod for mod in ("am", "nfm") if mod in by_mod and by_mod[mod]]
    for mod in sorted(by_mod):
        if mod not in order and by_mod[mod]:
            order.append(mod)

    lines: list[str] = []
    lines.append(f"airband = {'true' if airband_flag else 'false'};")
    lines.append("log_scan_activity = true;")
    lines.append('stats_filepath = "/run/rtl_airband_stats.txt";')
    lines.append("")
    lines.append("devices:")
    lines.append("({")
    lines.append('  type = "rtlsdr";')
    if serial:
        lines.append(f'  serial = "{serial}";')
    lines.append(f"  index = {device_index};")
    lines.append('  mode = "scan";')
    lines.append(f"  gain = {gain:.3f};   # UI_CONTROLLED")
    lines.append("")
    lines.append("  channels:")
    lines.append("  (")
    for idx, mod in enumerate(order):
        pairs = by_mod[mod]
        freq_values = ", ".join(_mhz_from_hz(freq_hz) for freq_hz, _ in pairs)
        label_values = ", ".join(f'"{_escape_label(label)}"' for _, label in pairs)
        lines.append("    {")
        lines.append(f"      freqs = ({freq_values});")
        lines.append("")
        lines.append(f"      labels = ({label_values});")
        lines.append("")
        lines.append(f'      modulation = "{mod}";')
        lines.append("      bandwidth = 12000;")
        lines.append(f"      squelch_threshold = {int(squelch_dbfs)};  # UI_CONTROLLED")
        lines.append("      squelch_delay = 0.8;")
        lines.append("")
        lines.append("      outputs:")
        lines.append("      (")
        lines.append("        {")
        lines.append('          type = "icecast";')
        lines.append("          send_scan_freq_tags = true;")
        lines.append('          server = "127.0.0.1";')
        lines.append("          port = 8000;")
        lines.append(f'          mountpoint = "{mountpoint}";')
        lines.append('          username = "source";')
        lines.append('          password = "062352";')
        lines.append(f'          name = "{_escape_label(stream_name)}";')
        lines.append(f'          genre = "{_escape_label(genre)}";')
        lines.append(f'          description = "{_escape_label(description)}";')
        lines.append("          bitrate = 32;")
        lines.append("        }")
        lines.append("      );")
        lines.append("    }" + ("," if idx < len(order) - 1 else ""))
    lines.append("  );")
    lines.append("});")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def cmd_build_analog_profile(args: argparse.Namespace) -> int:
    db_path = Path(args.db).expanduser()
    conn = _connect(db_path)
    try:
        resolved = _resolve_conventional_system(conn, args.system, state=args.state, county=args.county)
        if not resolved:
            print(f"error: unable to resolve conventional system query: {args.system}")
            return 2
        system_key, system_name = resolved

        where = ["cs.system_key = ?", "cf.freq_hz > 0"]
        params: list[object] = [system_key]
        if args.group:
            vals = [f"%{g.lower()}%" for g in args.group]
            where.append("(" + " OR ".join("lower(cg.group_name) LIKE ?" for _ in vals) + ")")
            params.extend(vals)
        if args.channel:
            vals = [f"%{c.lower()}%" for c in args.channel]
            where.append("(" + " OR ".join("lower(cf.alpha_tag) LIKE ?" for _ in vals) + ")")
            params.extend(vals)
        if args.mode:
            vals = [m.lower() for m in args.mode]
            where.append("(" + " OR ".join("lower(cf.mode) = ?" for _ in vals) + ")")
            params.extend(vals)

        rows_raw = conn.execute(
            f"""
            SELECT
                cf.cfreq_id,
                cf.freq_hz,
                COALESCE(cf.mode, ''),
                COALESCE(cf.alpha_tag, ''),
                COALESCE(cg.group_name, '')
            FROM conventional_freqs cf
            JOIN conventional_groups cg ON cg.cgroup_id = cf.cgroup_id
            LEFT JOIN conventional_systems cs
                ON cs.system_key = (cg.parent_key || ':' || CAST(cg.parent_id AS TEXT))
            WHERE {" AND ".join(where)}
            ORDER BY cf.cfreq_id
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    if not rows_raw:
        print("error: no conventional frequencies matched filters")
        return 2

    filtered: list[tuple[int, int, str, str, str]] = []
    for cfreq_id, freq_hz, mode, alpha, group_name in rows_raw:
        mhz = float(freq_hz) / 1_000_000.0
        in_vhf_airband = 118.0 <= mhz <= 137.0
        include = True
        if args.profile_type == "airband":
            include = in_vhf_airband
            if not include and args.include_military:
                include = (225.0 <= mhz <= 400.0) and (str(mode).upper() == "AM")
        elif args.profile_type == "ground":
            include = not in_vhf_airband
        if include:
            filtered.append((int(cfreq_id), int(freq_hz), str(mode), str(alpha), str(group_name)))

    dedup_key = set()
    filtered_dedup: list[tuple[int, int, str, str, str]] = []
    for row in filtered:
        key = (_mode_to_modulation(row[2]), row[1])
        if key in dedup_key:
            continue
        dedup_key.add(key)
        filtered_dedup.append(row)

    if args.max_freqs > 0:
        filtered_dedup = filtered_dedup[: args.max_freqs]

    if not filtered_dedup:
        print("error: no frequencies left after profile-type filtering")
        return 2

    out_arg = Path(args.out).expanduser()
    profile_id = _slug(args.profile_id) if args.profile_id else _slug(f"{system_name}-{args.profile_type}")
    if out_arg.name.endswith(".conf"):
        out_path = out_arg
    else:
        out_path = out_arg / f"rtl_airband_{profile_id}.conf"

    airband_flag = args.airband_flag == "true"
    if args.airband_flag == "auto":
        airband_flag = args.profile_type == "airband"
    device_index = args.index if args.index is not None else (0 if airband_flag else 1)
    serial = args.serial.strip()
    if not serial:
        serial = "00000002" if airband_flag else "70613472"
    squelch_dbfs = args.squelch_dbfs
    if squelch_dbfs is None:
        squelch_dbfs = -52 if airband_flag else -70
    mountpoint = args.mountpoint.strip().lstrip("/") or "ANALOG.mp3"

    _write_analog_profile(
        path=out_path,
        rows=filtered_dedup,
        airband_flag=airband_flag,
        device_index=device_index,
        serial=serial,
        gain=float(args.gain),
        squelch_dbfs=int(squelch_dbfs),
        mountpoint=mountpoint,
        stream_name=args.stream_name.strip() or "SprontPi Radio",
        genre=args.genre.strip() or "Scanner",
        description=args.description.strip() or f"Generated from {system_name}",
    )

    print(f"Analog profile created: {out_path}")
    print(f"System: {system_name}")
    print(f"Frequencies: {len(filtered_dedup)}")
    print(f"Profile type: {args.profile_type}")
    print(f"airband flag: {'true' if airband_flag else 'false'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="HomePatrol HPDB importer/query tool for scanner profiles",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help=f"SQLite database path (default: {DEFAULT_DB_PATH})",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="Import HPDB files into SQLite")
    p_import.add_argument("--hpdb-root", required=True, help="Path to HPDB directory")
    p_import.add_argument(
        "--no-reset",
        action="store_true",
        help="Do not drop/recreate tables before import",
    )
    p_import.add_argument("--verbose", action="store_true", help="Print each imported file")
    p_import.set_defaults(func=cmd_import)

    p_find = sub.add_parser("find-system", help="Find systems by name")
    p_find.add_argument("query", help="Case-insensitive system search string")
    p_find.add_argument(
        "--kind",
        choices=("all", "trunk", "conventional"),
        default="all",
        help="System family to search",
    )
    p_find.add_argument("--limit", type=int, default=50)
    p_find.set_defaults(func=cmd_find_system)

    p_tg = sub.add_parser("find-talkgroup", help="Find talkgroups by name/TGID")
    p_tg.add_argument("query", help="Case-insensitive search string")
    p_tg.add_argument("--system", default="", help="Optional system name filter")
    p_tg.add_argument("--limit", type=int, default=100)
    p_tg.set_defaults(func=cmd_find_talkgroup)

    p_site = sub.add_parser("site-freqs", help="List frequencies for system sites")
    p_site.add_argument("--system", required=True, help="System name (exact or partial)")
    p_site.add_argument("--site", default="", help="Optional site name filter")
    p_site.set_defaults(func=cmd_site_freqs)

    p_profile = sub.add_parser(
        "build-profile",
        help="Generate control_channels/talkgroups CSV files for a digital profile",
    )
    p_profile.add_argument("--system", required=True, help="System name (exact or partial)")
    p_profile.add_argument("--site", action="append", default=[], help="Site name filter (repeatable)")
    p_profile.add_argument(
        "--group",
        action="append",
        default=[],
        help="Talkgroup category filter on HPDB group name (repeatable)",
    )
    p_profile.add_argument(
        "--talkgroup",
        action="append",
        default=[],
        help="Talkgroup name/TGID filter (repeatable)",
    )
    p_profile.add_argument("--out", required=True, help="Output directory for generated profile")
    p_profile.add_argument("--profile-name", default="", help="Friendly profile name")
    p_profile.add_argument("--profile-slug", default="", help="Folder slug override")
    p_profile.add_argument(
        "--max-talkgroups",
        type=int,
        default=0,
        help="Limit exported talkgroups (0 = no limit)",
    )
    p_profile.set_defaults(func=cmd_build_profile)

    p_analog = sub.add_parser(
        "build-analog-profile",
        help="Generate rtl_airband *.conf from conventional channels in HPDB",
    )
    p_analog.add_argument("--system", required=True, help="Conventional system name (exact or partial)")
    p_analog.add_argument("--state", default="", help="Optional state filter (abbr or full name)")
    p_analog.add_argument("--county", default="", help="Optional county filter")
    p_analog.add_argument("--group", action="append", default=[], help="Group name filter (repeatable)")
    p_analog.add_argument("--channel", action="append", default=[], help="Channel alpha tag filter (repeatable)")
    p_analog.add_argument("--mode", action="append", default=[], help="Mode filter, e.g. AM/NFM/FM (repeatable)")
    p_analog.add_argument(
        "--profile-type",
        choices=("airband", "ground", "all"),
        default="airband",
        help="Frequency class filter applied before config generation",
    )
    p_analog.add_argument(
        "--include-military",
        action="store_true",
        help="For airband profiles, include 225-400 MHz AM channels",
    )
    p_analog.add_argument("--out", required=True, help="Output .conf path or output directory")
    p_analog.add_argument("--profile-id", default="", help="Used when --out points to a directory")
    p_analog.add_argument(
        "--airband-flag",
        choices=("auto", "true", "false"),
        default="auto",
        help="Value for `airband = ...;` in generated profile",
    )
    p_analog.add_argument("--index", type=int, default=None, help="RTL device index override")
    p_analog.add_argument("--serial", default="", help="RTL serial override")
    p_analog.add_argument("--gain", type=float, default=32.8, help="Initial gain value")
    p_analog.add_argument("--squelch-dbfs", type=int, default=None, help="Initial squelch_threshold value")
    p_analog.add_argument("--mountpoint", default="ANALOG.mp3", help="Icecast mountpoint")
    p_analog.add_argument("--stream-name", default="SprontPi Radio", help="Icecast stream name")
    p_analog.add_argument("--genre", default="Scanner", help="Icecast stream genre")
    p_analog.add_argument("--description", default="", help="Icecast stream description")
    p_analog.add_argument("--max-freqs", type=int, default=0, help="Limit exported frequencies (0 = no limit)")
    p_analog.set_defaults(func=cmd_build_analog_profile)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
