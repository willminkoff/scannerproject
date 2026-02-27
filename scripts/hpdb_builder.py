#!/usr/bin/env python3
"""Build a normalized HomePatrol SQLite database from local HPDB assets."""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

from homepatrol_db import _connect
from homepatrol_db import _create_schema
from homepatrol_db import _import_config
from homepatrol_db import _import_hpd_file


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HPDB_ROOT = REPO_ROOT / "assets" / "HPDB"
DEFAULT_DB_PATH = REPO_ROOT / "data" / "homepatrol.db"


TOTAL_KEYS = (
    "conventional_systems",
    "conventional_groups",
    "conventional_freqs",
    "trunk_systems",
    "trunk_sites",
    "trunk_freqs",
    "trunk_groups",
    "talkgroups",
    "entity_areas",
)


def _build_database(hpdb_root: Path, db_path: Path, reset: bool, verbose: bool) -> int:
    hpdb_root = hpdb_root.expanduser().resolve()
    db_path = db_path.expanduser().resolve()

    if not hpdb_root.is_dir():
        print(f"error: HPDB path not found: {hpdb_root}")
        return 2

    hpd_files = sorted(hpdb_root.glob("*.hpd"), key=lambda p: p.name.lower())
    if not hpd_files:
        print(f"error: no .hpd files found in {hpdb_root}")
        return 2

    conn = _connect(db_path)
    try:
        _create_schema(conn, reset=reset)
        now = dt.datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            ("last_import_started", now),
        )
        cfg_counts = _import_config(conn, hpdb_root / "HPDB.config")
        totals = {key: 0 for key in TOTAL_KEYS}

        for hpd_file in hpd_files:
            counts = _import_hpd_file(conn, hpd_file)
            for key in TOTAL_KEYS:
                totals[key] += counts.get(key, 0)
            if verbose:
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
    print(f"HPD files: {len(hpd_files)}")
    print(f"States: {cfg_counts['states']}, Counties: {cfg_counts['counties']}")
    for key in TOTAL_KEYS:
        print(f"{key}: {totals[key]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build data/homepatrol.db from assets/HPDB .hpd files",
    )
    parser.add_argument(
        "--hpdb-root",
        default=str(DEFAULT_HPDB_ROOT),
        help=f"HPDB directory to scan (default: {DEFAULT_HPDB_ROOT})",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help=f"SQLite database output path (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="Do not drop/recreate tables before import",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print each imported .hpd filename",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return _build_database(
        hpdb_root=Path(args.hpdb_root),
        db_path=Path(args.db),
        reset=not args.no_reset,
        verbose=bool(args.verbose),
    )


if __name__ == "__main__":
    raise SystemExit(main())
