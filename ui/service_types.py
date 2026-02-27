"""Service type helpers for HomePatrol scan filtering."""
from __future__ import annotations

import sqlite3
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_DB_PATH = str((_REPO_ROOT / "data" / "homepatrol.db").resolve())

_SERVICE_TYPE_NAME_MAP = {
    1: "Multi-Dispatch",
    2: "Law Dispatch",
    3: "Fire Dispatch",
    4: "EMS Dispatch",
}

_TARGET_DEFAULT_NAMES = {
    "law dispatch",
    "fire dispatch",
    "ems dispatch",
    "multi-dispatch",
    "multi dispatch",
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


def _compute_default_enabled_ids(rows: list[sqlite3.Row]) -> list[int]:
    if not rows:
        return []

    all_ids = sorted(
        {
            int(row["service_tag"])
            for row in rows
            if row["service_tag"] is not None
        }
    )
    name_by_id = {
        int(row["service_tag"]): str(row["name"] or "").strip().lower()
        for row in rows
        if row["service_tag"] is not None
    }

    selected = [
        sid
        for sid in all_ids
        if name_by_id.get(sid, "") in _TARGET_DEFAULT_NAMES
    ]
    if len(selected) >= 4:
        return selected[:4]

    dispatch_like = [
        sid
        for sid in all_ids
        if "dispatch" in name_by_id.get(sid, "")
    ]
    for sid in dispatch_like:
        if sid not in selected:
            selected.append(sid)
        if len(selected) >= 4:
            return selected[:4]

    for sid in all_ids:
        if sid not in selected:
            selected.append(sid)
        if len(selected) >= 4:
            return selected[:4]
    return selected


def _ensure_populated(conn: sqlite3.Connection) -> None:
    _ensure_service_types_table(conn)
    rows = conn.execute("SELECT COUNT(*) AS c FROM service_types").fetchone()
    count = int((rows["c"] if rows else 0) or 0)

    distinct_rows = conn.execute(
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
    tags = []
    for row in distinct_rows:
        try:
            tags.append(int(row["service_tag"]))
        except Exception:
            continue

    if count == 0 and tags:
        for tag in tags:
            conn.execute(
                """
                INSERT OR IGNORE INTO service_types(service_tag, name, enabled_by_default)
                VALUES (?, ?, 0)
                """,
                (tag, _SERVICE_TYPE_NAME_MAP.get(tag, f"Service Tag {tag}")),
            )
    elif tags:
        for tag in tags:
            conn.execute(
                """
                INSERT OR IGNORE INTO service_types(service_tag, name, enabled_by_default)
                VALUES (?, ?, 0)
                """,
                (tag, _SERVICE_TYPE_NAME_MAP.get(tag, f"Service Tag {tag}")),
            )

    existing = conn.execute(
        """
        SELECT service_tag, name
        FROM service_types
        ORDER BY service_tag
        """
    ).fetchall()
    defaults = set(_compute_default_enabled_ids(existing))
    for row in existing:
        tag = int(row["service_tag"])
        enabled = 1 if tag in defaults else 0
        conn.execute(
            "UPDATE service_types SET enabled_by_default = ? WHERE service_tag = ?",
            (enabled, tag),
        )


def get_all_service_types(db_path: str = _DEFAULT_DB_PATH) -> list[dict]:
    conn = _connect(db_path)
    try:
        _ensure_populated(conn)
        conn.commit()
        rows = conn.execute(
            """
            SELECT service_tag, name, enabled_by_default
            FROM service_types
            ORDER BY service_tag
            """
        ).fetchall()
        return [
            {
                "service_tag": int(row["service_tag"]),
                "name": str(row["name"] or ""),
                "enabled_by_default": bool(int(row["enabled_by_default"] or 0)),
            }
            for row in rows
        ]
    finally:
        conn.close()


def get_default_enabled_service_types(db_path: str = _DEFAULT_DB_PATH) -> list[int]:
    rows = get_all_service_types(db_path=db_path)
    return [
        int(row["service_tag"])
        for row in rows
        if bool(row.get("enabled_by_default"))
    ]
