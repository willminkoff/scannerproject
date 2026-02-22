"""V3 canonical runtime config and deterministic compiler."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

try:
    from .config import (
        CONFIG_SYMLINK,
        DIGITAL_ACTIVE_PROFILE_LINK,
        DIGITAL_PROFILES_DIR,
        GROUND_CONFIG_PATH,
        V3_CANONICAL_CONFIG_PATH,
        V3_COMPILED_STATE_PATH,
    )
    from .profile_config import (
        enforce_profile_index,
        load_profiles_registry,
        read_active_config_path,
        safe_profile_path,
        save_profiles_registry,
        write_combined_config,
    )
except ImportError:
    from ui.config import (
        CONFIG_SYMLINK,
        DIGITAL_ACTIVE_PROFILE_LINK,
        DIGITAL_PROFILES_DIR,
        GROUND_CONFIG_PATH,
        V3_CANONICAL_CONFIG_PATH,
        V3_COMPILED_STATE_PATH,
    )
    from ui.profile_config import (
        enforce_profile_index,
        load_profiles_registry,
        read_active_config_path,
        safe_profile_path,
        save_profiles_registry,
        write_combined_config,
    )


_CANONICAL_VERSION = 3
_PROFILE_ID_RE = re.compile(r"^[a-z0-9_-]{2,40}$")
_DIGITAL_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$")


@dataclass
class CompileIssue:
    code: str
    severity: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": str(self.code),
            "severity": str(self.severity),
            "message": str(self.message),
        }


def _atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    target = str(path or "").strip()
    if not target:
        raise ValueError("missing path")
    parent = os.path.dirname(target) or "."
    os.makedirs(parent, exist_ok=True)
    tmp = f"{target}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, target)


def _swap_symlink(link_path: str, target_path: str) -> None:
    link = str(link_path or "").strip()
    target = str(target_path or "").strip()
    if not link or not target:
        raise ValueError("missing symlink path")
    parent = os.path.dirname(link) or "."
    os.makedirs(parent, exist_ok=True)
    tmp_link = os.path.join(parent, f".{os.path.basename(link)}.tmp")
    if os.path.lexists(tmp_link):
        os.unlink(tmp_link)
    os.symlink(target, tmp_link)
    os.replace(tmp_link, link)


def _path_real(path: str) -> str:
    try:
        return os.path.realpath(str(path or "").strip())
    except Exception:
        return str(path or "").strip()


def _clean_analog_profiles(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("id") or "").strip()
        label = str(item.get("label") or "").strip()
        path = str(item.get("path") or "").strip()
        airband = bool(item.get("airband"))
        if not _PROFILE_ID_RE.fullmatch(pid):
            continue
        if pid in seen:
            continue
        safe = safe_profile_path(path) if path else None
        if not safe:
            continue
        out.append(
            {
                "id": pid,
                "label": label or pid,
                "path": safe,
                "airband": airband,
            }
        )
        seen.add(pid)
    out.sort(key=lambda row: row["id"])
    return out


def _clean_digital_profiles(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    base = _path_real(DIGITAL_PROFILES_DIR)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("id") or "").strip()
        path = str(item.get("path") or os.path.join(base, pid)).strip()
        if not _DIGITAL_PROFILE_RE.fullmatch(pid):
            continue
        if pid in seen:
            continue
        real = _path_real(path)
        if base and not real.startswith(base + os.sep):
            continue
        out.append({"id": pid, "path": real})
        seen.add(pid)
    out.sort(key=lambda row: row["id"])
    return out


def _list_digital_profiles_from_runtime() -> list[dict[str, str]]:
    base = _path_real(DIGITAL_PROFILES_DIR)
    out: list[dict[str, str]] = []
    if not base or not os.path.isdir(base):
        return out
    for name in sorted(os.listdir(base)):
        pid = str(name or "").strip()
        if not _DIGITAL_PROFILE_RE.fullmatch(pid):
            continue
        path = _path_real(os.path.join(base, pid))
        if os.path.isdir(path):
            out.append({"id": pid, "path": path})
    return out


def _active_digital_profile_from_runtime() -> str:
    link = str(DIGITAL_ACTIVE_PROFILE_LINK or "").strip()
    if not link or not os.path.islink(link):
        return ""
    target = _path_real(link)
    base = _path_real(DIGITAL_PROFILES_DIR)
    if base and target.startswith(base + os.sep):
        return os.path.basename(target)
    return ""


def build_canonical_from_runtime() -> dict[str, Any]:
    profiles = load_profiles_registry()
    analog = _clean_analog_profiles(profiles)
    active_airband = ""
    active_ground = ""

    current_airband = _path_real(read_active_config_path())
    current_ground = _path_real(GROUND_CONFIG_PATH)
    for item in analog:
        path = _path_real(item["path"])
        if path == current_airband:
            active_airband = item["id"]
        if path == current_ground:
            active_ground = item["id"]

    digital_profiles = _list_digital_profiles_from_runtime()
    active_digital = _active_digital_profile_from_runtime()

    payload = {
        "version": _CANONICAL_VERSION,
        "updated_at": int(time.time()),
        "analog": {
            "profiles": analog,
            "active": {
                "airband": active_airband,
                "ground": active_ground,
            },
        },
        "digital": {
            "profiles": digital_profiles,
            "active_profile": active_digital,
        },
    }
    return payload


def _merge_runtime_items(canonical: dict[str, Any]) -> dict[str, Any]:
    merged = dict(canonical or {})
    analog = dict(merged.get("analog") or {})
    digital = dict(merged.get("digital") or {})

    runtime_analog = _clean_analog_profiles(load_profiles_registry())
    canonical_analog = _clean_analog_profiles(analog.get("profiles") or [])
    by_id = {row["id"]: row for row in canonical_analog}
    for row in runtime_analog:
        by_id.setdefault(row["id"], row)
    analog["profiles"] = sorted(by_id.values(), key=lambda row: row["id"])

    active = dict(analog.get("active") or {})
    active_air = str(active.get("airband") or "").strip()
    active_gnd = str(active.get("ground") or "").strip()
    if active_air and active_air not in by_id:
        active_air = ""
    if active_gnd and active_gnd not in by_id:
        active_gnd = ""
    analog["active"] = {"airband": active_air, "ground": active_gnd}

    runtime_digital = _list_digital_profiles_from_runtime()
    canonical_digital = _clean_digital_profiles(digital.get("profiles") or [])
    digital_by_id = {row["id"]: row for row in canonical_digital}
    for row in runtime_digital:
        digital_by_id.setdefault(row["id"], row)
    digital_profiles = sorted(digital_by_id.values(), key=lambda row: row["id"])

    active_dig = str(digital.get("active_profile") or "").strip()
    if active_dig and active_dig not in digital_by_id:
        active_dig = ""

    digital["profiles"] = digital_profiles
    digital["active_profile"] = active_dig

    merged["version"] = _CANONICAL_VERSION
    merged["updated_at"] = int(time.time())
    merged["analog"] = analog
    merged["digital"] = digital
    return merged


def load_canonical_config() -> dict[str, Any]:
    path = str(V3_CANONICAL_CONFIG_PATH or "").strip()
    if not path:
        return build_canonical_from_runtime()

    raw: dict[str, Any]
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                parsed = json.load(f)
            raw = parsed if isinstance(parsed, dict) else {}
        except Exception:
            raw = {}
    else:
        raw = {}

    if not raw:
        raw = build_canonical_from_runtime()

    merged = _merge_runtime_items(raw)
    _atomic_write_json(path, merged)
    return merged


def save_canonical_config(payload: dict[str, Any]) -> dict[str, Any]:
    merged = _merge_runtime_items(payload)
    _atomic_write_json(str(V3_CANONICAL_CONFIG_PATH or "").strip(), merged)
    return merged


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _active_id_or_first(active_id: str, profiles: list[dict[str, Any]], airband: bool | None = None) -> str:
    if active_id and any(p.get("id") == active_id for p in profiles):
        return active_id
    for p in profiles:
        if airband is None or bool(p.get("airband")) == bool(airband):
            return str(p.get("id") or "")
    return ""


def _read_control_channels_count(profile_dir: str) -> int:
    path = os.path.join(str(profile_dir or ""), "control_channels.txt")
    if not os.path.isfile(path):
        return 0
    seen: set[int] = set()
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                raw = line.split("#", 1)[0].strip()
                if not raw:
                    continue
                m = re.search(r"\d+\.\d+", raw)
                if not m:
                    continue
                hz = int(round(float(m.group(0)) * 1_000_000))
                if hz > 0:
                    seen.add(hz)
    except Exception:
        return 0
    return len(seen)


def compile_runtime(canonical: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = _merge_runtime_items(canonical or load_canonical_config())
    issues: list[CompileIssue] = []

    analog = dict(cfg.get("analog") or {})
    analog_profiles = _clean_analog_profiles(analog.get("profiles") or [])
    analog_by_id = {row["id"]: row for row in analog_profiles}

    if not analog_profiles:
        issues.append(CompileIssue("ANALOG_PROFILES_EMPTY", "critical", "No analog profiles available in canonical config"))

    try:
        save_profiles_registry(analog_profiles)
    except Exception as e:
        issues.append(CompileIssue("ANALOG_REGISTRY_WRITE_FAILED", "critical", f"Failed writing profiles registry: {e}"))

    active = dict(analog.get("active") or {})
    active_air = _active_id_or_first(str(active.get("airband") or "").strip(), analog_profiles, airband=True)
    active_gnd = _active_id_or_first(str(active.get("ground") or "").strip(), analog_profiles, airband=False)

    if active_air:
        profile = analog_by_id.get(active_air) or {}
        air_path = str(profile.get("path") or "").strip()
        if not os.path.isfile(air_path):
            issues.append(CompileIssue("ANALOG_AIRBAND_PATH_MISSING", "critical", f"Active airband profile file missing: {air_path}"))
        else:
            try:
                enforce_profile_index(air_path)
                _swap_symlink(CONFIG_SYMLINK, air_path)
            except Exception as e:
                issues.append(CompileIssue("ANALOG_AIRBAND_LINK_FAILED", "critical", f"Failed to set active airband profile: {e}"))
    else:
        issues.append(CompileIssue("ANALOG_AIRBAND_ACTIVE_MISSING", "critical", "No active airband profile selected"))

    if active_gnd:
        profile = analog_by_id.get(active_gnd) or {}
        gnd_path = str(profile.get("path") or "").strip()
        if not os.path.isfile(gnd_path):
            issues.append(CompileIssue("ANALOG_GROUND_PATH_MISSING", "critical", f"Active ground profile file missing: {gnd_path}"))
        else:
            try:
                enforce_profile_index(gnd_path)
                _swap_symlink(GROUND_CONFIG_PATH, gnd_path)
            except Exception as e:
                issues.append(CompileIssue("ANALOG_GROUND_LINK_FAILED", "critical", f"Failed to set active ground profile: {e}"))
    else:
        issues.append(CompileIssue("ANALOG_GROUND_ACTIVE_MISSING", "critical", "No active ground profile selected"))

    combined_changed = False
    try:
        combined_changed = bool(write_combined_config())
    except Exception as e:
        issues.append(CompileIssue("ANALOG_COMBINED_WRITE_FAILED", "critical", f"Failed writing combined config: {e}"))

    digital = dict(cfg.get("digital") or {})
    digital_profiles = _clean_digital_profiles(digital.get("profiles") or [])
    digital_by_id = {row["id"]: row for row in digital_profiles}
    active_dig = _active_id_or_first(str(digital.get("active_profile") or "").strip(), digital_profiles)

    if digital_profiles and active_dig:
        row = digital_by_id.get(active_dig) or {}
        dig_path = str(row.get("path") or "").strip()
        if not os.path.isdir(dig_path):
            issues.append(CompileIssue("DIGITAL_ACTIVE_PATH_MISSING", "critical", f"Active digital profile directory missing: {dig_path}"))
        else:
            try:
                _swap_symlink(DIGITAL_ACTIVE_PROFILE_LINK, dig_path)
            except Exception as e:
                issues.append(CompileIssue("DIGITAL_ACTIVE_LINK_FAILED", "critical", f"Failed setting digital active profile: {e}"))
            cc_count = _read_control_channels_count(dig_path)
            if cc_count <= 0:
                issues.append(
                    CompileIssue(
                        "DIGITAL_CONTROL_CHANNELS_EMPTY",
                        "critical",
                        f"Active digital profile has no control channels: {active_dig}",
                    )
                )
    elif digital_profiles:
        issues.append(CompileIssue("DIGITAL_ACTIVE_MISSING", "critical", "No active digital profile selected"))
    else:
        issues.append(CompileIssue("DIGITAL_PROFILES_EMPTY", "warn", "No digital profiles discovered"))

    cfg["analog"] = {
        "profiles": analog_profiles,
        "active": {"airband": active_air, "ground": active_gnd},
    }
    cfg["digital"] = {
        "profiles": digital_profiles,
        "active_profile": active_dig,
    }
    cfg["version"] = _CANONICAL_VERSION
    cfg["updated_at"] = int(time.time())

    save_canonical_config(cfg)

    critical = [issue for issue in issues if issue.severity == "critical"]
    status = "failed" if critical else ("degraded" if issues else "healthy")
    state_payload = {
        "ok": not critical,
        "status": status,
        "compiled_at": int(time.time()),
        "canonical_hash": _canonical_hash(cfg),
        "combined_changed": bool(combined_changed),
        "issues": [issue.as_dict() for issue in issues],
        "counts": {
            "analog_profiles": len(analog_profiles),
            "digital_profiles": len(digital_profiles),
        },
        "active": {
            "airband": active_air,
            "ground": active_gnd,
            "digital": active_dig,
        },
    }

    state_path = str(V3_COMPILED_STATE_PATH or "").strip()
    if state_path:
        try:
            _atomic_write_json(state_path, state_payload)
        except Exception:
            pass
    return state_payload


def load_compiled_state() -> dict[str, Any]:
    path = str(V3_COMPILED_STATE_PATH or "").strip()
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            payload = json.load(f)
        if isinstance(payload, dict):
            return payload
    except Exception:
        return {}
    return {}


def bootstrap_runtime() -> dict[str, Any]:
    cfg = load_canonical_config()
    return compile_runtime(cfg)


def set_active_analog_profile(target: str, profile_id: str) -> dict[str, Any]:
    cfg = load_canonical_config()
    analog = dict(cfg.get("analog") or {})
    active = dict(analog.get("active") or {})
    key = "ground" if str(target or "").strip().lower() == "ground" else "airband"
    active[key] = str(profile_id or "").strip()
    analog["active"] = active
    cfg["analog"] = analog
    return compile_runtime(cfg)


def set_active_digital_profile(profile_id: str) -> dict[str, Any]:
    cfg = load_canonical_config()
    digital = dict(cfg.get("digital") or {})
    digital["active_profile"] = str(profile_id or "").strip()
    cfg["digital"] = digital
    return compile_runtime(cfg)


def upsert_analog_profile(profile: dict[str, Any]) -> dict[str, Any]:
    cfg = load_canonical_config()
    analog = dict(cfg.get("analog") or {})
    profiles = _clean_analog_profiles(analog.get("profiles") or [])
    pid = str(profile.get("id") or "").strip()
    kept = [row for row in profiles if row.get("id") != pid]
    kept.append(
        {
            "id": pid,
            "label": str(profile.get("label") or pid).strip() or pid,
            "path": str(profile.get("path") or "").strip(),
            "airband": bool(profile.get("airband")),
        }
    )
    analog["profiles"] = kept
    cfg["analog"] = analog
    return compile_runtime(cfg)


def delete_analog_profile(profile_id: str) -> dict[str, Any]:
    cfg = load_canonical_config()
    analog = dict(cfg.get("analog") or {})
    profiles = [
        row
        for row in _clean_analog_profiles(analog.get("profiles") or [])
        if str(row.get("id") or "") != str(profile_id or "").strip()
    ]
    analog["profiles"] = profiles
    active = dict(analog.get("active") or {})
    pid = str(profile_id or "").strip()
    if active.get("airband") == pid:
        active["airband"] = ""
    if active.get("ground") == pid:
        active["ground"] = ""
    analog["active"] = active
    cfg["analog"] = analog
    return compile_runtime(cfg)


def sync_digital_profiles_from_fs() -> dict[str, Any]:
    cfg = load_canonical_config()
    digital = dict(cfg.get("digital") or {})
    digital["profiles"] = _list_digital_profiles_from_runtime()
    cfg["digital"] = digital
    return compile_runtime(cfg)
