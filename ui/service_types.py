"""Service type helpers for HomePatrol scan filtering."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import HPDB_DB_PATH

_DEFAULT_DB_PATH = str(Path(HPDB_DB_PATH).expanduser().resolve())

# (display_order, name, service_tag, is_custom, enabled_by_default)
# These IDs are HPDB-native service tags used on talkgroups/conventional rows.
_HP2_SERVICE_TYPES: list[tuple[int, str, int, int, int]] = [
    (1, "Multi-Dispatch", 1, 0, 0),
    (2, "Law Dispatch", 2, 0, 1),
    (3, "Fire Dispatch", 3, 0, 1),
    (4, "EMS Dispatch", 4, 0, 1),
    (5, "Service Tag 5", 5, 0, 0),
    (6, "Service Tag 6", 6, 0, 0),
    (7, "Law Tac", 7, 0, 0),
    (8, "Fire Tac", 8, 0, 0),
    (9, "EMS Tac", 9, 0, 0),
    (10, "Service Tag 10", 10, 0, 0),
    (11, "Interop", 11, 0, 0),
    (12, "Hospital", 12, 0, 0),
    (13, "Ham", 13, 0, 0),
    (14, "Public Works", 14, 0, 0),
    (15, "Aircraft", 15, 0, 0),
    (16, "Service Tag 16", 16, 0, 0),
    (17, "Business", 17, 0, 0),
    (18, "Service Tag 18", 18, 0, 0),
    (19, "Service Tag 19", 19, 0, 0),
    (20, "Railroad", 20, 0, 0),
    (21, "Service Tag 21", 21, 0, 0),
    (22, "Service Tag 22", 22, 0, 0),
    (23, "Law Talk", 23, 0, 0),
    (24, "Fire Talk", 24, 0, 0),
    (25, "EMS Talk", 25, 0, 0),
    (26, "Transportation", 26, 0, 0),
    (27, "Service Tag 27", 27, 0, 0),
    (28, "Service Tag 28", 28, 0, 0),
    (29, "Emergency Ops", 29, 0, 0),
    (30, "Military", 30, 0, 0),
    (31, "Media", 31, 0, 0),
    (32, "Schools", 32, 0, 0),
    (33, "Security", 33, 0, 0),
    (34, "Utilities", 34, 0, 0),
    (35, "Custom 1", 35, 1, 0),
    (36, "Custom 2", 36, 1, 0),
    (37, "Corrections", 37, 0, 0),
    (38, "Custom 3", 38, 1, 0),
    (39, "Custom 4", 39, 1, 0),
]

_ORDER_BY_TAG = {service_tag: order for order, _, service_tag, _, _ in _HP2_SERVICE_TYPES}
_CATALOG_BY_TAG = {
    service_tag: (name, int(is_custom), int(enabled_by_default))
    for _, name, service_tag, is_custom, enabled_by_default in _HP2_SERVICE_TYPES
}


def _connect(db_path: str) -> sqlite3.Connection:
    path = str(Path(db_path).expanduser().resolve())
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def load_distinct_service_tags(db_path: str = _DEFAULT_DB_PATH) -> list[int]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT service_tag
            FROM (
                SELECT DISTINCT service_tag
                FROM talkgroups
                WHERE service_tag IS NOT NULL
                UNION
                SELECT DISTINCT service_tag
                FROM conventional_freqs
                WHERE service_tag IS NOT NULL
            )
            ORDER BY service_tag
            """
        ).fetchall()
        tags: list[int] = []
        for row in rows:
            try:
                value = int(row["service_tag"])
            except Exception:
                continue
            tags.append(value)
        return tags
    finally:
        conn.close()


def _ensure_service_types_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS service_types (
            service_tag INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            enabled_by_default INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    cols = {
        str(row["name"]): str(row["type"])
        for row in conn.execute("PRAGMA table_info(service_types)").fetchall()
    }
    if "is_custom" not in cols:
        conn.execute("ALTER TABLE service_types ADD COLUMN is_custom INTEGER NOT NULL DEFAULT 0")


def _distinct_tags(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        """
        SELECT service_tag
        FROM (
            SELECT DISTINCT service_tag
            FROM talkgroups
            WHERE service_tag IS NOT NULL
            UNION
            SELECT DISTINCT service_tag
            FROM conventional_freqs
            WHERE service_tag IS NOT NULL
        )
        ORDER BY service_tag
        """
    ).fetchall()
    out: list[int] = []
    for row in rows:
        try:
            out.append(int(row["service_tag"]))
        except Exception:
            continue
    return out


def _upsert_catalog_rows(conn: sqlite3.Connection) -> None:
    for _, name, service_tag, is_custom, enabled_by_default in _HP2_SERVICE_TYPES:
        conn.execute(
            """
            INSERT OR IGNORE INTO service_types(service_tag, name, enabled_by_default, is_custom)
            VALUES (?, ?, ?, ?)
            """,
            (service_tag, name, int(enabled_by_default), int(is_custom)),
        )
        conn.execute(
            """
            UPDATE service_types
            SET name = ?, enabled_by_default = ?, is_custom = ?
            WHERE service_tag = ?
            """,
            (name, int(enabled_by_default), int(is_custom), service_tag),
        )


def _upsert_unknown_rows(conn: sqlite3.Connection, tags: list[int]) -> None:
    known = set(_CATALOG_BY_TAG.keys())
    for tag in tags:
        if tag in known:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO service_types(service_tag, name, enabled_by_default, is_custom)
            VALUES (?, ?, 0, 0)
            """,
            (int(tag), f"Service Tag {int(tag)}"),
        )


def _ensure_populated(conn: sqlite3.Connection) -> None:
    _ensure_service_types_table(conn)
    tags = _distinct_tags(conn)
    _upsert_catalog_rows(conn)
    _upsert_unknown_rows(conn, tags)


def _sort_key(service_tag: int) -> tuple[int, int]:
    order = _ORDER_BY_TAG.get(service_tag)
    if order is not None:
        return (0, int(order))
    return (1, int(service_tag))


def get_all_service_types(db_path: str = _DEFAULT_DB_PATH) -> list[dict]:
    conn = _connect(db_path)
    try:
        _ensure_populated(conn)
        conn.commit()
        rows = conn.execute(
            """
            SELECT service_tag, name, enabled_by_default, is_custom
            FROM service_types
            """
        ).fetchall()
        out = []
        for row in rows:
            try:
                tag = int(row["service_tag"])
            except Exception:
                continue
            out.append(
                {
                    "service_tag": tag,
                    "name": str(row["name"] or ""),
                    "enabled_by_default": bool(int(row["enabled_by_default"] or 0)),
                    "is_custom": bool(int(row["is_custom"] or 0)),
                }
            )
        out.sort(key=lambda item: _sort_key(int(item["service_tag"])))
        return out
    finally:
        conn.close()


def get_default_enabled_service_types(db_path: str = _DEFAULT_DB_PATH) -> list[int]:
    rows = get_all_service_types(db_path=db_path)
    return [int(row["service_tag"]) for row in rows if bool(row.get("enabled_by_default"))]
