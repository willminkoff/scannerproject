"""HP3 favorites bridge for analog and digital profiles."""
from __future__ import annotations

import os
from typing import Any

try:
    from .config import GROUND_CONFIG_PATH
    from .digital import get_digital_manager
    from .profile_config import read_active_config_path, split_profiles, guess_current_profile
    from .server_workers import enqueue_action
    from .v3_preflight import gate_action
    from .v3_runtime import set_active_analog_profile, set_active_digital_profile
except ImportError:
    from ui.config import GROUND_CONFIG_PATH
    from ui.digital import get_digital_manager
    from ui.profile_config import read_active_config_path, split_profiles, guess_current_profile
    from ui.server_workers import enqueue_action
    from ui.v3_preflight import gate_action
    from ui.v3_runtime import set_active_analog_profile, set_active_digital_profile


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    token = str(value or "").strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return default


def _group_key(entry: dict[str, Any]) -> str:
    entry_type = str(entry.get("type") or "").strip().lower()
    if entry_type == "digital":
        return "digital"
    target = str(entry.get("target") or "").strip().lower()
    return f"analog:{target}"


def normalize_profile_favorites(raw: Any) -> list[dict[str, Any]]:
    """Normalize raw favorites payload into canonical favorite entries."""
    if not isinstance(raw, list):
        return []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue

        entry_type = str(item.get("type") or item.get("kind") or "").strip().lower()
        target = str(item.get("target") or "").strip().lower()
        profile_id = str(
            item.get("profile_id")
            or item.get("profileId")
            or item.get("profile")
            or ""
        ).strip()
        token = str(item.get("id") or "").strip()

        parts = token.split(":") if token else []
        if not profile_id and parts:
            if parts[0].lower() == "digital" and len(parts) >= 2:
                entry_type = "digital"
                profile_id = ":".join(parts[1:]).strip()
            elif parts[0].lower() == "analog" and len(parts) >= 3:
                entry_type = "analog"
                target = str(parts[1] or "").strip().lower()
                profile_id = ":".join(parts[2:]).strip()

        if entry_type == "digital":
            target = ""
        elif entry_type == "analog":
            if target not in {"airband", "ground"}:
                continue
        else:
            continue

        if not profile_id:
            continue

        label = str(item.get("label") or item.get("name") or profile_id).strip() or profile_id
        entry_id = f"digital:{profile_id}" if entry_type == "digital" else f"analog:{target}:{profile_id}"
        if entry_id in seen:
            continue
        seen.add(entry_id)

        out.append(
            {
                "id": entry_id,
                "type": entry_type,
                "target": target,
                "profile_id": profile_id,
                "label": label,
                "enabled": _coerce_bool(item.get("enabled"), default=False),
            }
        )

    return out


def _profile_catalog() -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Build profile catalog and active profile snapshot."""
    try:
        _, profiles_airband, profiles_ground = split_profiles()
    except Exception:
        profiles_airband = []
        profiles_ground = []
    airband_tuples = [
        (str(row.get("id") or ""), str(row.get("label") or ""), str(row.get("path") or ""))
        for row in profiles_airband
        if str(row.get("id") or "").strip() and bool(row.get("exists"))
    ]
    ground_tuples = [
        (str(row.get("id") or ""), str(row.get("label") or ""), str(row.get("path") or ""))
        for row in profiles_ground
        if str(row.get("id") or "").strip() and bool(row.get("exists"))
    ]

    try:
        airband_conf = read_active_config_path()
        ground_conf = os.path.realpath(GROUND_CONFIG_PATH)
        active_airband = str(guess_current_profile(airband_conf, airband_tuples) or "").strip()
        active_ground = str(guess_current_profile(ground_conf, ground_tuples) or "").strip()
    except Exception:
        active_airband = ""
        active_ground = ""

    try:
        manager = get_digital_manager()
        digital_profiles = [str(pid or "").strip() for pid in (manager.listProfiles() or []) if str(pid or "").strip()]
        active_digital = str(manager.getProfile() or "").strip()
    except Exception:
        digital_profiles = []
        active_digital = ""

    catalog: list[dict[str, Any]] = []
    for pid, label, _ in airband_tuples:
        catalog.append(
            {
                "id": f"analog:airband:{pid}",
                "type": "analog",
                "target": "airband",
                "profile_id": pid,
                "label": str(label or pid),
            }
        )
    for pid, label, _ in ground_tuples:
        catalog.append(
            {
                "id": f"analog:ground:{pid}",
                "type": "analog",
                "target": "ground",
                "profile_id": pid,
                "label": str(label or pid),
            }
        )
    for pid in digital_profiles:
        catalog.append(
            {
                "id": f"digital:{pid}",
                "type": "digital",
                "target": "",
                "profile_id": pid,
                "label": pid,
            }
        )

    active = {
        "analog_airband": active_airband,
        "analog_ground": active_ground,
        "digital": active_digital,
    }
    return catalog, active


def _collapse_group(entries: list[dict[str, Any]], group: str, preferred_profile_id: str = "") -> None:
    group_entries = [entry for entry in entries if _group_key(entry) == group]
    if not group_entries:
        return

    enabled_entries = [entry for entry in group_entries if bool(entry.get("enabled"))]
    if len(enabled_entries) > 1:
        keep = enabled_entries[0]
        for entry in enabled_entries[1:]:
            entry["enabled"] = False
        enabled_entries = [keep]

    if enabled_entries:
        return

    preferred = None
    if preferred_profile_id:
        preferred = next(
            (
                entry
                for entry in group_entries
                if str(entry.get("profile_id") or "").strip() == preferred_profile_id
            ),
            None,
        )
    if preferred is None:
        preferred = group_entries[0]
    preferred["enabled"] = True


def build_profile_favorites(saved_favorites: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build runtime-safe favorites list from catalog + saved preferences."""
    catalog, active = _profile_catalog()
    normalized_saved = normalize_profile_favorites(saved_favorites)
    enabled_by_id = {
        str(entry.get("id") or "").strip(): bool(entry.get("enabled"))
        for entry in normalized_saved
        if str(entry.get("id") or "").strip()
    }

    favorites: list[dict[str, Any]] = []
    for entry in catalog:
        entry_id = str(entry.get("id") or "").strip()
        row = dict(entry)
        row["enabled"] = bool(enabled_by_id.get(entry_id, False))
        favorites.append(row)

    if not favorites and normalized_saved:
        favorites = [dict(entry) for entry in normalized_saved]

    _collapse_group(favorites, "analog:airband", preferred_profile_id=str(active.get("analog_airband") or ""))
    _collapse_group(favorites, "analog:ground", preferred_profile_id=str(active.get("analog_ground") or ""))
    _collapse_group(favorites, "digital", preferred_profile_id=str(active.get("digital") or ""))

    selected = {
        "analog_airband": "",
        "analog_ground": "",
        "digital": "",
    }
    for entry in favorites:
        if not bool(entry.get("enabled")):
            continue
        group = _group_key(entry)
        profile_id = str(entry.get("profile_id") or "").strip()
        if group == "analog:airband":
            selected["analog_airband"] = profile_id
        elif group == "analog:ground":
            selected["analog_ground"] = profile_id
        elif group == "digital":
            selected["digital"] = profile_id

    metadata = {
        "active": active,
        "selected": selected,
        "counts": {
            "analog_airband": len([entry for entry in favorites if _group_key(entry) == "analog:airband"]),
            "analog_ground": len([entry for entry in favorites if _group_key(entry) == "analog:ground"]),
            "digital": len([entry for entry in favorites if _group_key(entry) == "digital"]),
        },
    }
    return favorites, metadata


def apply_profile_favorites(favorites: Any) -> dict[str, Any]:
    """Apply selected favorites to active analog/digital runtime profiles."""
    normalized, metadata = build_profile_favorites(favorites)
    selected = dict(metadata.get("selected") or {})
    active = dict(metadata.get("active") or {})

    payload: dict[str, Any] = {
        "ok": True,
        "selected": selected,
        "active_before": active,
        "applied": {"analog_airband": False, "analog_ground": False, "digital": False},
        "errors": [],
    }

    for target_key, target_name in (("analog_airband", "airband"), ("analog_ground", "ground")):
        desired = str(selected.get(target_key) or "").strip()
        current = str(active.get(target_key) or "").strip()
        if not desired or desired == current:
            continue
        result = enqueue_action({"type": "profile", "profile": desired, "target": target_name})
        status = int(result.get("status") or 500)
        body = dict(result.get("payload") or {})
        if status >= 300 or not body.get("ok"):
            err = str(body.get("error") or f"{target_name} profile switch failed ({status})").strip()
            payload["errors"].append(err)
            continue
        try:
            set_active_analog_profile(target_name, desired)
        except Exception as exc:
            payload["errors"].append(f"{target_name} profile persisted with warning: {exc}")
        payload["applied"][target_key] = True

    desired_digital = str(selected.get("digital") or "").strip()
    current_digital = str(active.get("digital") or "").strip()
    if desired_digital and desired_digital != current_digital:
        gate = gate_action("digital_profile", profile_id=desired_digital)
        if not bool((gate or {}).get("ok")):
            payload["errors"].append("digital profile preflight blocked")
        else:
            try:
                manager = get_digital_manager()
                ok, err = manager.setProfile(desired_digital)
            except Exception as exc:
                ok, err = False, str(exc)
            if not ok:
                payload["errors"].append(str(err or "digital profile switch failed"))
            else:
                try:
                    set_active_digital_profile(desired_digital)
                except Exception as exc:
                    payload["errors"].append(f"digital profile persisted with warning: {exc}")
                payload["applied"]["digital"] = True

    payload["ok"] = len(payload["errors"]) == 0
    payload["favorites"] = normalized
    return payload
