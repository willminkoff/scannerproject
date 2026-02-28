"""Service type helpers for HomePatrol scan filtering."""
from __future__ import annotations

import sqlite3
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_DB_PATH = str((_REPO_ROOT / "data" / "homepatrol.db").resolve())

# (display_order, name, service_tag, is_custom, enabled_by_default)
_HP2_SERVICE_TYPES: list[tuple[int, str, int, int, int]] = [
    (1, "Aircraft", 1, 0, 0),
    (2, "Business", 2, 0, 0),
    (3, "Corrections", 3, 0, 0),
    (4, "Emergency Ops", 4, 0, 0),
    (5, "EMS Dispatch", 5, 0, 1),
    (6, "EMS Tac", 6, 0, 0),
    (7, "EMS Talk", 7, 0, 0),
    (8, "Federal", 8, 0, 0),
    (9, "Fire Dispatch", 9, 0, 1),
    (10, "Fire Tac", 10, 0, 0),
    (11, "Fire Talk", 11, 0, 0),
    (12, "Ham", 12, 0, 0),
    (13, "Hospital", 13, 0, 0),
    (14, "Interop", 14, 0, 0),
    (15, "Law Dispatch", 15, 0, 1),
    (16, "Law Tac", 16, 0, 0),
    (17, "Law Talk", 17, 0, 0),
    (18, "Media", 18, 0, 0),
    (19, "Military", 19, 0, 0),
    (20, "Multi-Dispatch", 20, 0, 0),
    (21, "Multi-Tac", 21, 0, 0),
    (22, "Multi-Talk", 22, 0, 0),
    (23, "Other", 23, 0, 0),
    (24, "Public Works", 24, 0, 0),
    (25, "Railroad", 25, 0, 0),
    (26, "Racing Officials", 26, 0, 0),
    (27, "Racing Teams", 27, 0, 0),
    (28, "Schools", 28, 0, 0),
    (29, "Security", 29, 0, 0),
    (30, "Transportation", 30, 0, 0),
    (31, "Utilities", 31, 0, 0),
    (32, "Custom 1", 32, 1, 0),
    (33, "Custom 2", 33, 1, 0),
    (34, "Custom 3", 34, 1, 0),
    (35, "Custom 4", 35, 1, 0),
    (36, "Custom 5", 36, 1, 0),
    (37, "Custom 6", 37, 1, 0),
    (38, "Custom 7", 38, 1, 0),
    (39, "Custom 8", 39, 1, 0),
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
