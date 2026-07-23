"""sb3.ui.wizard — data for the Favorites Builder wizard (location → systems → channels).

The wizard in sb3.html walks five stages; stages 2–4 (Location, System Pick,
Channel Pick) fetch their options from the backend.  In the inherited
ScannerBox6 UI these came from a RadioReference/HomePatrol SQLite dump
(``ui/hpdb.db``, ~hundreds of MB, gitignored).  SB3's slimmer server never wired
those endpoints, so every one fell through to the ``not-implemented`` catch-all
and the State dropdown came up empty — the bug Will hit.

This module serves them on a **two-tier** rule, matching the project's
"real-if-easy, clear-stub-otherwise" convention:

  * Countries + US states are STATIC data — 51 rows that never change — so they
    are always served, with no database at all.  That alone un-empties the
    Location stage.
  * Counties, systems and channels require the HomePatrol dump.  When it is
    present we serve it for real (reusing the already-tested queries in
    ``ui.hp_favorites_wizard``); when it is not we return an empty list plus a
    ``note`` the UI shows, rather than a raw error.  No lookup is invented and
    nothing external is contacted.

WHERE THE DUMP COMES FROM (this cost a search once — write it down):
the .db itself is gitignored, but it is fully REPRODUCIBLE from the repo.  The
68 per-state ``.hpd`` source files in ``assets/HPDB/`` (~42 MB) ARE tracked, and
``scripts/hpdb_builder.py`` + ``scripts/homepatrol_db.py`` build them into
``data/homepatrol.db`` (~52 MB)::

    python3 scripts/hpdb_builder.py          # → data/homepatrol.db
    cp data/homepatrol.db <deploy>/ui/hpdb.db

Deployed on Neptune 2026-07-23 at ``~/sb3/ui/hpdb.db``.  It is gitignored, so
``sb3-ctl update`` leaves it alone; a fresh clone would need the copy re-done.
No restart is needed after dropping it in — every call below re-resolves the
path and opens its own connection, so the data goes live on the next request.

⚠️  The dump's ``state_id`` numbering is its OWN, not RadioReference's public
one (Tennessee is 47 here, 43 there).  That is safe because when the dump is
present the state list is served FROM it, so counties/systems key off the same
scheme; the static table below is only ever used when there is no dump at all.

The static state IDs use RadioReference's own ``stateId`` numbering (Tennessee =
43, DC = 9), so if Will later drops the dump onto a host, the IDs already line
up and county/system queries key straight off them.  When the dump IS present,
states come from it instead, so the static list is only ever a fallback.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# static tier — always available, no database
# ---------------------------------------------------------------------------

#: country_id → display name (mirrors ui.hp_favorites_wizard._COUNTRY_NAMES).
COUNTRY_NAMES = {1: "USA", 2: "Canada"}

#: (state_id, abbr, name) using RadioReference's canonical stateId numbering.
#: DC is 9, which shifts every state after it — that offset is part of the real
#: scheme, not a mistake. Kept in ID order here; the endpoint sorts by name.
US_STATES = [
    (1, "AL", "Alabama"), (2, "AK", "Alaska"), (3, "AZ", "Arizona"),
    (4, "AR", "Arkansas"), (5, "CA", "California"), (6, "CO", "Colorado"),
    (7, "CT", "Connecticut"), (8, "DE", "Delaware"),
    (9, "DC", "District of Columbia"), (10, "FL", "Florida"),
    (11, "GA", "Georgia"), (12, "HI", "Hawaii"), (13, "ID", "Idaho"),
    (14, "IL", "Illinois"), (15, "IN", "Indiana"), (16, "IA", "Iowa"),
    (17, "KS", "Kansas"), (18, "KY", "Kentucky"), (19, "LA", "Louisiana"),
    (20, "ME", "Maine"), (21, "MD", "Maryland"), (22, "MA", "Massachusetts"),
    (23, "MI", "Michigan"), (24, "MN", "Minnesota"), (25, "MS", "Mississippi"),
    (26, "MO", "Missouri"), (27, "MT", "Montana"), (28, "NE", "Nebraska"),
    (29, "NV", "Nevada"), (30, "NH", "New Hampshire"), (31, "NJ", "New Jersey"),
    (32, "NM", "New Mexico"), (33, "NY", "New York"),
    (34, "NC", "North Carolina"), (35, "ND", "North Dakota"), (36, "OH", "Ohio"),
    (37, "OK", "Oklahoma"), (38, "OR", "Oregon"), (39, "PA", "Pennsylvania"),
    (40, "RI", "Rhode Island"), (41, "SC", "South Carolina"),
    (42, "SD", "South Dakota"), (43, "TN", "Tennessee"), (44, "TX", "Texas"),
    (45, "UT", "Utah"), (46, "VT", "Vermont"), (47, "VA", "Virginia"),
    (48, "WA", "Washington"), (49, "WV", "West Virginia"),
    (50, "WI", "Wisconsin"), (51, "WY", "Wyoming"),
]

#: Shown to the user whenever a stage needs the dump this host doesn't have.
#: It names the fix, because a note that only says "missing" sends whoever reads
#: it on the same search this module's docstring exists to prevent.
NO_DB_NOTE = ("HomePatrol database not present on this host — counties, systems "
              "and channels need it. Country/state selection work without it. "
              "Rebuild with `python3 scripts/hpdb_builder.py` (sources are "
              "tracked in assets/HPDB/) and copy the result to ui/hpdb.db.")


# ---------------------------------------------------------------------------
# real tier — the HomePatrol dump, if this host has it
# ---------------------------------------------------------------------------

def _resolve_db_path() -> Optional[Path]:
    """First readable, non-empty HomePatrol DB among the known locations, or None.

    Order: the HPDB_DB_PATH env override, then the in-repo path, then the legacy
    Micro path. A zero-byte file (the gitignored placeholder in this checkout)
    is treated as absent — it is a placeholder, not a database.
    """
    candidates = []
    env = os.getenv("HPDB_DB_PATH", "").strip()
    if env:
        candidates.append(Path(env))
    repo = Path(__file__).resolve().parent.parent.parent   # <repo>/
    candidates.append(repo / "ui" / "hpdb.db")
    candidates.append(Path("~/scanner-db/homepatrol.db").expanduser())
    for p in candidates:
        try:
            if p.is_file() and p.stat().st_size > 0:
                return p
        except OSError:
            continue
    return None


def _wizard():
    """An ``HPFavoritesWizard`` bound to a real DB, or None if none is usable.

    Reused rather than reimplemented: those queries already exist and are
    covered by the legacy test suite. Imported lazily and defensively so a
    missing/!importable ``ui`` package degrades to the static tier instead of
    500-ing the whole endpoint.
    """
    db = _resolve_db_path()
    if db is None:
        return None
    try:
        from ui.hp_favorites_wizard import HPFavoritesWizard
        wiz = HPFavoritesWizard(str(db))
        # Prove the schema is really there before promising real data.
        wiz.get_countries()
        return wiz
    except Exception:
        return None


# ---------------------------------------------------------------------------
# endpoint payloads — each returns a graceful {ok:true, ...} shape
# ---------------------------------------------------------------------------

def countries() -> Dict:
    """/api/scan/favorites-wizard/countries — USA always; others only if the DB
    actually has them (Canada needs the dump for its provinces)."""
    wiz = _wizard()
    if wiz is not None:
        try:
            rows = wiz.get_countries()
            if rows:
                return {"ok": True, "countries": rows}
        except Exception:
            pass
    return {"ok": True, "countries": [{"country_id": 1, "name": "USA"}]}


def states(country_id: int) -> Dict:
    """/api/scan/favorites-wizard/states — real if the dump is present, else the
    static US-50+DC list (country 1 only)."""
    wiz = _wizard()
    if wiz is not None:
        try:
            rows = wiz.get_states(country_id)
            if rows:
                return {"ok": True, "states": rows}
        except Exception:
            pass
    if int(country_id) != 1:
        # No static list for non-US countries; say so rather than pretend.
        return {"ok": True, "states": [],
                "note": f"State list for country {country_id} needs the "
                        f"HomePatrol database."}
    rows = [{"state_id": sid, "abbr": abbr, "name": name}
            for sid, abbr, name in US_STATES]
    rows.sort(key=lambda r: r["name"])
    return {"ok": True, "states": rows, "source": "static"}


def counties(state_id: int) -> Dict:
    """/api/scan/favorites-wizard/counties — real, or an empty list + note."""
    wiz = _wizard()
    if wiz is not None:
        try:
            return {"ok": True, "counties": wiz.get_counties(state_id)}
        except Exception:
            pass
    # 'All Counties' keeps the dropdown selectable even with no dump.
    return {"ok": True, "counties": [{"county_id": 0, "name": "All Counties"}],
            "note": NO_DB_NOTE}


def systems(state_id: int, county_id: int, system_type: str, scope: str) -> Dict:
    """/api/scan/favorites-wizard/systems — real, or an empty list + note."""
    wiz = _wizard()
    if wiz is not None:
        try:
            rows = wiz.get_systems(state_id=state_id, county_id=county_id,
                                   system_type=system_type, scope=scope)
            return {"ok": True, "systems": rows}
        except Exception:
            pass
    return {"ok": True, "systems": [], "note": NO_DB_NOTE}


#: Why a system with rows in the database can still yield nothing selectable.
#: LTR/legacy systems store talkgroup ids in Area-LCN-Talkgroup form
#: ("0-01-035"), and the channel query keeps only numeric ids — so every LTR
#: talkgroup is dropped and the picker comes up empty. That is a real limit, not
#: a fault, but it has to SAY so: an empty list with no explanation is
#: indistinguishable from a broken wizard, which is exactly how this looked.
EMPTY_SYSTEM_NOTE = ("No selectable channels for this system. LTR/legacy "
                     "systems store non-numeric talkgroup ids (e.g. 0-01-035) "
                     "which cannot be used as talkgroups, so they are skipped. "
                     "Try a P25/Motorola system.")


def channels(system_type: str, system_id: str, limit: int = 5000) -> Dict:
    """/api/scan/favorites-wizard/channels — digital talkgroups OR analog freqs.

    Dispatches through the legacy ``get_channels``, which routes on system_type:
    digital keys off an integer trunk_id, analog off the string system_key
    ("CountyId:2446"). Handling only the digital shape — as this did first —
    meant every analog system returned empty *and* claimed the database was
    missing, which was wrong on both counts.
    """
    wiz = _wizard()
    if wiz is None:
        return {"ok": True, "channels": [], "note": NO_DB_NOTE}
    try:
        _name, rows = wiz.get_channels(str(system_type), str(system_id))
    except (TypeError, ValueError):
        # digital path casts system_id to int; a mismatched type lands here.
        return {"ok": True, "channels": [],
                "note": f"system_id {system_id!r} is not valid for "
                        f"system_type {system_type!r}."}
    except Exception:
        return {"ok": True, "channels": [], "note": NO_DB_NOTE}
    out = rows[:int(limit)]
    if not out:
        # The database IS present and answered — so say why it is empty rather
        # than leaving the picker blank.
        return {"ok": True, "channels": [], "note": EMPTY_SYSTEM_NOTE}
    return {"ok": True, "channels": out}


def scan_state(state) -> Dict:
    """/api/scan/state — the wizard's own favorites state (lists + entries).

    SB3 has no favorites-persistence store yet, so return an empty state. The
    UI's applyHpFavoritesState() reads an empty payload as "one default list
    named 'My Favorites'", so the wizard loads and browses cleanly; saving
    picked channels is the piece still to be wired (see the routes note).
    """
    return {"ok": True, "state": {}, "backend": "sb3", "persisted": False}
