"""Sync and drift status between HP3 favorites and active digital profile."""
from __future__ import annotations

import re
from typing import Any

try:
    from .digital import get_digital_manager, read_digital_talkgroups, write_digital_listen
    from .hp_state import HPState
except ImportError:
    from ui.digital import get_digital_manager, read_digital_talkgroups, write_digital_listen
    from ui.hp_state import HPState


def _normalize_tgid(value: Any) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    if token.isdigit():
        try:
            parsed = int(token)
        except Exception:
            return ""
        return str(parsed) if parsed > 0 else ""
    match = re.search(r"\b(\d{2,10})\b", token)
    if not match:
        return ""
    try:
        parsed = int(match.group(1))
    except Exception:
        return ""
    return str(parsed) if parsed > 0 else ""


def _resolve_active_favorites_entries(state: HPState) -> tuple[str, list[dict[str, Any]]]:
    favorites = list(getattr(state, "favorites", []) or [])
    active_name = str(getattr(state, "favorites_name", "") or "").strip()
    active_token = active_name.lower()

    selected: dict[str, Any] | None = None
    for item in favorites:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip().lower()
        if label and label == active_token:
            selected = item
            break

    if selected is None:
        for item in favorites:
            if not isinstance(item, dict):
                continue
            if bool(item.get("enabled")):
                selected = item
                break

    if selected is not None:
        selected_name = str(selected.get("label") or active_name or "My Favorites").strip() or "My Favorites"
        selected_custom = selected.get("custom_favorites")
        if isinstance(selected_custom, list):
            return selected_name, [entry for entry in selected_custom if isinstance(entry, dict)]
        return selected_name, []

    fallback_name = active_name or "My Favorites"
    fallback_entries = list(getattr(state, "custom_favorites", []) or [])
    return fallback_name, [entry for entry in fallback_entries if isinstance(entry, dict)]


def _favorites_tgids(entries: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or "").strip().lower()
        if kind != "trunked":
            continue
        tgid = _normalize_tgid(entry.get("talkgroup") or entry.get("tgid"))
        if not tgid:
            continue
        out.add(tgid)
    return out


def _active_digital_profile_id() -> str:
    try:
        manager = get_digital_manager()
        return str(manager.getProfile() or "").strip()
    except Exception:
        return ""


def _read_profile_talkgroups(profile_id: str, max_rows: int = 200000) -> tuple[set[str], set[str], str, str]:
    ok, payload = read_digital_talkgroups(profile_id, max_rows=max_rows)
    if not ok:
        return set(), set(), "", str(payload or "failed to read talkgroups")
    data = payload if isinstance(payload, dict) else {}
    items = data.get("items")
    if not isinstance(items, list):
        items = []
    all_tgids: set[str] = set()
    enabled_tgids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        dec = _normalize_tgid(item.get("dec"))
        if not dec:
            continue
        all_tgids.add(dec)
        if bool(item.get("listen")):
            enabled_tgids.add(dec)
    source = str(data.get("source") or "").strip()
    return all_tgids, enabled_tgids, source, ""


def _build_status(state: HPState | None = None, profile_id: str = "") -> dict[str, Any]:
    loaded_state = state or HPState.load()
    favorites_name, favorite_entries = _resolve_active_favorites_entries(loaded_state)
    favorite_tgids = _favorites_tgids(favorite_entries)

    active_profile_id = str(profile_id or "").strip() or _active_digital_profile_id()
    if not active_profile_id:
        return {
            "ok": True,
            "available": False,
            "in_sync": False,
            "reason": "no active digital profile",
            "active_profile_id": "",
            "favorites_name": favorites_name,
            "favorites_mode": str(getattr(loaded_state, "mode", "") or "").strip().lower(),
            "favorite_tgids_count": int(len(favorite_tgids)),
            "profile_tgids_count": 0,
            "profile_listen_enabled_count": 0,
            "desired_enabled_count": 0,
            "overlap_count": 0,
            "missing_in_profile_count": int(len(favorite_tgids)),
            "missing_in_profile_sample": sorted(favorite_tgids, key=int)[:25],
            "disabled_desired_count": 0,
            "disabled_desired_sample": [],
            "extra_enabled_count": 0,
            "extra_enabled_sample": [],
            "profile_source": "",
        }

    profile_tgids, enabled_tgids, source, read_error = _read_profile_talkgroups(active_profile_id)
    if read_error:
        return {
            "ok": False,
            "available": False,
            "in_sync": False,
            "reason": read_error,
            "error": read_error,
            "active_profile_id": active_profile_id,
            "favorites_name": favorites_name,
            "favorites_mode": str(getattr(loaded_state, "mode", "") or "").strip().lower(),
            "favorite_tgids_count": int(len(favorite_tgids)),
            "profile_tgids_count": 0,
            "profile_listen_enabled_count": 0,
            "desired_enabled_count": 0,
            "overlap_count": 0,
            "missing_in_profile_count": int(len(favorite_tgids)),
            "missing_in_profile_sample": sorted(favorite_tgids, key=int)[:25],
            "disabled_desired_count": 0,
            "disabled_desired_sample": [],
            "extra_enabled_count": 0,
            "extra_enabled_sample": [],
            "profile_source": source,
        }

    desired_enabled = favorite_tgids & profile_tgids
    overlap = desired_enabled
    missing_in_profile = favorite_tgids - profile_tgids
    disabled_desired = desired_enabled - enabled_tgids
    extra_enabled = enabled_tgids - desired_enabled

    favorites_has_tgids = len(favorite_tgids) > 0
    in_sync = bool(
        favorites_has_tgids
        and not missing_in_profile
        and not disabled_desired
        and not extra_enabled
    )
    if not favorites_has_tgids:
        reason = "favorites list has no talkgroups"
    elif missing_in_profile:
        reason = "favorites includes talkgroups absent from active profile"
    elif disabled_desired:
        reason = "desired talkgroups are disabled in active profile listen map"
    elif extra_enabled:
        reason = "active profile has extra enabled talkgroups not in favorites"
    else:
        reason = "favorites and profile are aligned"

    status = {
        "ok": True,
        "available": True,
        "in_sync": bool(in_sync),
        "reason": reason,
        "active_profile_id": active_profile_id,
        "favorites_name": favorites_name,
        "favorites_mode": str(getattr(loaded_state, "mode", "") or "").strip().lower(),
        "favorite_tgids_count": int(len(favorite_tgids)),
        "profile_tgids_count": int(len(profile_tgids)),
        "profile_listen_enabled_count": int(len(enabled_tgids)),
        "desired_enabled_count": int(len(desired_enabled)),
        "overlap_count": int(len(overlap)),
        "missing_in_profile_count": int(len(missing_in_profile)),
        "missing_in_profile_sample": sorted(missing_in_profile, key=int)[:25],
        "disabled_desired_count": int(len(disabled_desired)),
        "disabled_desired_sample": sorted(disabled_desired, key=int)[:25],
        "extra_enabled_count": int(len(extra_enabled)),
        "extra_enabled_sample": sorted(extra_enabled, key=int)[:25],
        "profile_source": source,
    }
    # Internal fields used by sync operation.
    status["_profile_tgids"] = profile_tgids
    status["_desired_enabled"] = desired_enabled
    return status


def _public_status(snapshot: dict[str, Any]) -> dict[str, Any]:
    out = dict(snapshot or {})
    out.pop("_profile_tgids", None)
    out.pop("_desired_enabled", None)
    return out


def get_hp_favorites_sync_status(state: HPState | None = None) -> dict[str, Any]:
    return _public_status(_build_status(state=state))


def sync_hp_favorites_to_profile(state: HPState | None = None) -> dict[str, Any]:
    before = _build_status(state=state)
    before_public = _public_status(before)
    active_profile_id = str(before.get("active_profile_id") or "").strip()
    if not active_profile_id:
        return {
            "ok": False,
            "in_sync": False,
            "error": "no active digital profile",
            "before": before_public,
            "after": before_public,
        }
    if not bool(before.get("ok")):
        return {
            "ok": False,
            "in_sync": False,
            "error": str(before.get("error") or before.get("reason") or "sync precheck failed"),
            "before": before_public,
            "after": before_public,
        }

    profile_tgids = set(before.get("_profile_tgids") or set())
    desired_enabled = set(before.get("_desired_enabled") or set())
    update_items = [
        {"dec": tgid, "listen": bool(tgid in desired_enabled)}
        for tgid in sorted(profile_tgids, key=int)
    ]
    ok, err = write_digital_listen(active_profile_id, update_items)
    if not ok:
        return {
            "ok": False,
            "in_sync": False,
            "error": str(err or "failed to write profile listen map"),
            "before": before_public,
            "after": before_public,
        }

    after = _public_status(_build_status(state=state, profile_id=active_profile_id))
    return {
        "ok": True,
        "in_sync": bool(after.get("in_sync")),
        "active_profile_id": active_profile_id,
        "favorites_name": str(after.get("favorites_name") or ""),
        "updated_talkgroups": int(len(update_items)),
        "before": before_public,
        "after": after,
    }
