"""Apply HP/SB3 scan-pool conventional channels to analog runtime profiles."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from typing import Any

try:
    from .config import (
        AIRBAND_MAX_MHZ,
        AIRBAND_MIN_MHZ,
        CONFIG_SYMLINK,
        GROUND_CONFIG_PATH,
        PROFILES_DIR,
    )
    from .profile_config import (
        enforce_profile_index,
        find_profile,
        guess_current_profile,
        load_profiles_registry,
        read_active_config_path,
        safe_profile_path,
        save_profiles_registry,
        set_profile,
        split_profiles,
        write_airband_flag,
        write_combined_config,
        write_freqs_labels,
    )
    from .scan_mode_controller import get_scan_mode_controller
    from .scanner import mark_analog_hit_cutoff
    from .systemd import restart_rtl
    from .v3_runtime import set_active_analog_profile, upsert_analog_profile
except ImportError:
    from ui.config import (
        AIRBAND_MAX_MHZ,
        AIRBAND_MIN_MHZ,
        CONFIG_SYMLINK,
        GROUND_CONFIG_PATH,
        PROFILES_DIR,
    )
    from ui.profile_config import (
        enforce_profile_index,
        find_profile,
        guess_current_profile,
        load_profiles_registry,
        read_active_config_path,
        safe_profile_path,
        save_profiles_registry,
        set_profile,
        split_profiles,
        write_airband_flag,
        write_combined_config,
        write_freqs_labels,
    )
    from ui.scan_mode_controller import get_scan_mode_controller
    from ui.scanner import mark_analog_hit_cutoff
    from ui.systemd import restart_rtl
    from ui.v3_runtime import set_active_analog_profile, upsert_analog_profile


_MANAGED_AIR_ID = "hp3_favorites_airband"
_MANAGED_GROUND_ID = "hp3_favorites_ground"
_MANAGED_AIR_LABEL = "HP3 Favorites Airband"
_MANAGED_GROUND_LABEL = "HP3 Favorites Ground"
_MAX_FREQS_PER_BAND = 256
_SYNC_LOCK = threading.Lock()
_LAST_SIGNATURE = ""
_LAST_RESULT: dict[str, Any] = {"ok": True, "changed": False}


def _profile_path_for(profile_id: str) -> str:
    filename = f"rtl_airband_{profile_id}.conf"
    return os.path.join(str(PROFILES_DIR), filename)


def _coerce_float(value: Any) -> float | None:
    try:
        parsed = float(str(value).strip())
    except Exception:
        return None
    if not (parsed > 0):
        return None
    return parsed


def _normalize_label(entry: dict[str, Any], fallback: str) -> str:
    alpha = str(entry.get("alpha_tag") or entry.get("channel_name") or "").strip()
    if alpha:
        return alpha
    system_name = str(entry.get("system_name") or "").strip()
    if system_name:
        return system_name
    return fallback


def _is_airband_frequency(freq_mhz: float) -> bool:
    return float(AIRBAND_MIN_MHZ) <= freq_mhz <= float(AIRBAND_MAX_MHZ)


def _minimal_profile_template(airband: bool) -> str:
    default_freq = "118.6000" if airband else "462.6500"
    default_mod = "am" if airband else "nfm"
    desired_index = 0 if airband else 1
    return (
        f"airband = {'true' if airband else 'false'};\n\n"
        "devices:\n"
        "({\n"
        "  type = \"rtlsdr\";\n"
        f"  index = {desired_index};\n"
        "  mode = \"scan\";\n"
        "  gain = 32.800;   # UI_CONTROLLED\n\n"
        "  channels:\n"
        "  (\n"
        "    {\n"
        f"      freqs = ({default_freq});\n\n"
        f"      modulation = \"{default_mod}\";\n"
        "      bandwidth = 12000;\n"
        "      squelch_threshold = -70;  # UI_CONTROLLED\n"
        "      squelch_delay = 0.8;\n\n"
        "      outputs:\n"
        "      (\n"
        "        {\n"
        "          type = \"icecast\";\n"
        "          send_scan_freq_tags = true;\n"
        "          server = \"127.0.0.1\";\n"
        "          port = 8000;\n"
        "          mountpoint = \"scannerbox.mp3\";\n"
        "          username = \"source\";\n"
        "          password = \"062352\";\n"
        "          name = \"SprontPi Radio\";\n"
        "          genre = \"AIRBAND\";\n"
        "          description = \"HP3 Favorites\";\n"
        "          bitrate = 32;\n"
        "        }\n"
        "      );\n"
        "    }\n"
        "  );\n"
        "});\n"
    )


def _profile_has_freqs_block(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            text = handle.read()
    except Exception:
        return False
    return "freqs" in text and "=" in text


def _template_profile_path(
    profiles: list[dict[str, Any]],
    airband: bool,
    *,
    exclude_paths: set[str] | None = None,
) -> str:
    exclude = {os.path.realpath(p) for p in (exclude_paths or set()) if str(p).strip()}
    preferred_id = "none_airband" if airband else "none_ground"
    preferred = find_profile(profiles, preferred_id)
    if preferred:
        candidate = str(preferred.get("path") or "").strip()
        candidate_real = os.path.realpath(candidate) if candidate else ""
        if (
            candidate
            and os.path.isfile(candidate)
            and candidate_real not in exclude
            and _profile_has_freqs_block(candidate)
        ):
            return candidate

    for row in profiles:
        if bool(row.get("airband")) != bool(airband):
            continue
        candidate = str(row.get("path") or "").strip()
        candidate_real = os.path.realpath(candidate) if candidate else ""
        if (
            candidate
            and os.path.isfile(candidate)
            and candidate_real not in exclude
            and _profile_has_freqs_block(candidate)
        ):
            return candidate
    return ""


def _ensure_managed_profile(
    profiles: list[dict[str, Any]],
    *,
    profile_id: str,
    label: str,
    airband: bool,
) -> tuple[dict[str, Any], bool]:
    changed = False
    profile = find_profile(profiles, profile_id)
    desired_path = _profile_path_for(profile_id)
    safe_path = safe_profile_path(desired_path)
    if not safe_path:
        raise RuntimeError(f"invalid managed profile path for {profile_id}")

    needs_seed = (not os.path.isfile(safe_path)) or (not _profile_has_freqs_block(safe_path))
    if needs_seed:
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        template_path = _template_profile_path(
            profiles,
            airband=airband,
            exclude_paths={safe_path},
        )
        if template_path and os.path.isfile(template_path):
            shutil.copyfile(template_path, safe_path)
        else:
            with open(safe_path, "w", encoding="utf-8") as handle:
                handle.write(_minimal_profile_template(airband=airband))
        changed = True

    write_airband_flag(safe_path, bool(airband))
    enforce_profile_index(safe_path)

    if profile is None:
        profile = {
            "id": profile_id,
            "label": label,
            "path": safe_path,
            "airband": bool(airband),
        }
        profiles.append(profile)
        changed = True
    else:
        expected = {
            "id": profile_id,
            "label": label,
            "path": safe_path,
            "airband": bool(airband),
        }
        for key, value in expected.items():
            if profile.get(key) != value:
                profile[key] = value
                changed = True

    try:
        upsert_analog_profile(profile)
    except Exception:
        # Runtime compile persistence is best-effort here.
        pass
    return profile, changed


def _select_fallback_profile(profiles: list[dict[str, Any]], target: str) -> str:
    fallback_id = "none_ground" if target == "ground" else "none_airband"
    row = find_profile(profiles, fallback_id)
    if row and os.path.isfile(str(row.get("path") or "")):
        return fallback_id
    return _MANAGED_GROUND_ID if target == "ground" else _MANAGED_AIR_ID


def _normalize_conventional_pool(pool: dict[str, Any]) -> tuple[list[float], list[str], list[float], list[str]]:
    rows = pool.get("conventional")
    if not isinstance(rows, list):
        rows = []

    air_labels_by_freq: dict[float, str] = {}
    ground_labels_by_freq: dict[float, str] = {}

    for item in rows:
        if not isinstance(item, dict):
            continue
        freq = _coerce_float(item.get("frequency"))
        if freq is None:
            continue
        mhz = round(freq, 6)
        label = _normalize_label(item, f"{mhz:.4f}")
        if _is_airband_frequency(mhz):
            air_labels_by_freq.setdefault(mhz, label)
        else:
            ground_labels_by_freq.setdefault(mhz, label)

    air_freqs = sorted(air_labels_by_freq.keys())[:_MAX_FREQS_PER_BAND]
    ground_freqs = sorted(ground_labels_by_freq.keys())[:_MAX_FREQS_PER_BAND]
    air_labels = [air_labels_by_freq[freq] for freq in air_freqs]
    ground_labels = [ground_labels_by_freq[freq] for freq in ground_freqs]
    return air_freqs, air_labels, ground_freqs, ground_labels


def _profiles_for_target(target: str) -> tuple[str, list[tuple[str, str, str]], str]:
    profile_payload, profiles_airband, profiles_ground = split_profiles()
    del profile_payload
    if target == "ground":
        tuples = [
            (
                str(row.get("id") or ""),
                str(row.get("label") or ""),
                str(row.get("path") or ""),
            )
            for row in profiles_ground
            if str(row.get("id") or "").strip() and bool(row.get("exists"))
        ]
        conf_path = os.path.realpath(str(GROUND_CONFIG_PATH))
        symlink_path = str(GROUND_CONFIG_PATH)
    else:
        tuples = [
            (
                str(row.get("id") or ""),
                str(row.get("label") or ""),
                str(row.get("path") or ""),
            )
            for row in profiles_airband
            if str(row.get("id") or "").strip() and bool(row.get("exists"))
        ]
        conf_path = os.path.realpath(read_active_config_path())
        symlink_path = str(CONFIG_SYMLINK)
    return conf_path, tuples, symlink_path


def _switch_profile_if_needed(target: str, desired_profile_id: str) -> tuple[bool, str]:
    conf_path, profiles, symlink_path = _profiles_for_target(target)
    if not profiles:
        return False, f"no {target} profiles available"
    current_real = os.path.realpath(str(conf_path or ""))
    current_id = ""
    for pid, _, path in profiles:
        if os.path.realpath(str(path or "")) == current_real:
            current_id = str(pid or "").strip()
            break
    if not current_id and os.path.exists(current_real):
        current_id = str(guess_current_profile(conf_path, profiles) or "").strip()
    if current_id == desired_profile_id:
        return False, ""
    ok, changed = set_profile(desired_profile_id, conf_path, profiles, symlink_path)
    if not ok:
        return False, f"unknown {target} profile: {desired_profile_id}"
    if not changed:
        return False, ""
    try:
        set_active_analog_profile(target, desired_profile_id)
    except Exception:
        pass
    mark_analog_hit_cutoff(target, time.time())
    return True, ""


def _mode_token(value: Any) -> str:
    token = str(value or "").strip().lower()
    if token in {"hp", "hp3"}:
        return "hp"
    if token in {"expert", "sb3", "legacy", "profile"}:
        return "expert"
    return "expert"


def sync_scan_pool_to_analog_runtime(force: bool = False) -> dict[str, Any]:
    """Apply active scan-pool conventional channels to managed analog profiles."""
    global _LAST_SIGNATURE
    global _LAST_RESULT

    with _SYNC_LOCK:
        controller = get_scan_mode_controller()
        mode = _mode_token(controller.get_mode())
        pool = controller.get_scan_pool() if mode in {"hp", "expert"} else {"conventional": []}
        air_freqs, air_labels, ground_freqs, ground_labels = _normalize_conventional_pool(pool)
        signature_payload = {
            "mode": mode,
            "air": air_freqs,
            "ground": ground_freqs,
        }
        signature = json.dumps(signature_payload, sort_keys=True, separators=(",", ":"))
        if not force and signature == _LAST_SIGNATURE:
            return dict(_LAST_RESULT)

        changed = False
        errors: list[str] = []
        switched = {"airband": False, "ground": False}
        profile_write_changed = {"airband": False, "ground": False}
        selected_profiles = {"airband": "", "ground": ""}

        profiles = load_profiles_registry()
        _, reg_changed_air = _ensure_managed_profile(
            profiles,
            profile_id=_MANAGED_AIR_ID,
            label=_MANAGED_AIR_LABEL,
            airband=True,
        )
        _, reg_changed_ground = _ensure_managed_profile(
            profiles,
            profile_id=_MANAGED_GROUND_ID,
            label=_MANAGED_GROUND_LABEL,
            airband=False,
        )
        if reg_changed_air or reg_changed_ground:
            save_profiles_registry(profiles)
            changed = True

        air_profile = find_profile(profiles, _MANAGED_AIR_ID) or {}
        ground_profile = find_profile(profiles, _MANAGED_GROUND_ID) or {}
        air_path = str(air_profile.get("path") or "").strip()
        ground_path = str(ground_profile.get("path") or "").strip()

        if air_path and air_freqs:
            try:
                profile_write_changed["airband"] = bool(write_freqs_labels(air_path, air_freqs, air_labels))
                changed = changed or profile_write_changed["airband"]
            except Exception as exc:
                errors.append(f"failed writing airband favorites profile: {exc}")

        if ground_path and ground_freqs:
            try:
                profile_write_changed["ground"] = bool(write_freqs_labels(ground_path, ground_freqs, ground_labels))
                changed = changed or profile_write_changed["ground"]
            except Exception as exc:
                errors.append(f"failed writing ground favorites profile: {exc}")

        desired_air_profile = _MANAGED_AIR_ID if air_freqs else _select_fallback_profile(profiles, "airband")
        desired_ground_profile = _MANAGED_GROUND_ID if ground_freqs else _select_fallback_profile(profiles, "ground")
        selected_profiles["airband"] = desired_air_profile
        selected_profiles["ground"] = desired_ground_profile

        switched_air, err_air = _switch_profile_if_needed("airband", desired_air_profile)
        if err_air:
            errors.append(err_air)
        switched["airband"] = switched_air
        changed = changed or switched_air

        switched_ground, err_ground = _switch_profile_if_needed("ground", desired_ground_profile)
        if err_ground:
            errors.append(err_ground)
        switched["ground"] = switched_ground
        changed = changed or switched_ground

        restart_ok = True
        restart_error = ""
        combined_changed = False
        if changed:
            try:
                combined_changed = bool(write_combined_config())
            except Exception as exc:
                errors.append(f"failed updating combined config: {exc}")
            if combined_changed or switched_air or switched_ground:
                restart_ok, restart_error = restart_rtl()
                if not restart_ok and restart_error:
                    errors.append(f"rtl restart failed: {restart_error}")

        result = {
            "ok": len(errors) == 0,
            "changed": bool(changed),
            "mode": mode,
            "airband_frequency_count": len(air_freqs),
            "ground_frequency_count": len(ground_freqs),
            "selected_profiles": selected_profiles,
            "profile_write_changed": profile_write_changed,
            "profile_switched": switched,
            "combined_changed": bool(combined_changed),
            "restart_ok": bool(restart_ok),
            "restart_error": str(restart_error or ""),
            "errors": errors,
        }
        _LAST_SIGNATURE = signature
        _LAST_RESULT = dict(result)
        return result


def get_last_favorites_runtime_sync() -> dict[str, Any]:
    with _SYNC_LOCK:
        return dict(_LAST_RESULT)
