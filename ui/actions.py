"""Business logic for scanner actions."""
import os
import json
import time
import re
from typing import Any

try:
    from .config import CONFIG_SYMLINK, GROUND_CONFIG_PATH, COMBINED_CONFIG_PATH, HOLD_STATE_PATH, TUNE_BACKUP_PATH
    from .systemd import (
        unit_active,
        stop_rtl,
        start_rtl,
        restart_rtl,
        restart_ground,
        restart_icecast,
        restart_keepalive,
        restart_ui,
        restart_digital,
        stop_ground,
    )
    from .profile_config import (
        split_profiles, guess_current_profile, set_profile, write_controls,
        write_combined_config, read_active_config_path, avoid_current_hit,
        clear_avoids, write_filter, parse_freqs_labels, replace_freqs_labels
    )
    from .scanner import mark_analog_hit_cutoff
except ImportError:
    from ui.config import CONFIG_SYMLINK, GROUND_CONFIG_PATH, COMBINED_CONFIG_PATH, HOLD_STATE_PATH, TUNE_BACKUP_PATH
    from ui.systemd import (
        unit_active,
        stop_rtl,
        start_rtl,
        restart_rtl,
        restart_ground,
        restart_icecast,
        restart_keepalive,
        restart_ui,
        restart_digital,
        stop_ground,
    )
    from ui.profile_config import (
        split_profiles, guess_current_profile, set_profile, write_controls,
        write_combined_config, read_active_config_path, avoid_current_hit,
        clear_avoids, write_filter, parse_freqs_labels, replace_freqs_labels
    )
    from ui.scanner import mark_analog_hit_cutoff


_DEFAULT_PROFILE_LOOP_BUNDLE_DIR = os.path.join(
    os.path.dirname(str(COMBINED_CONFIG_PATH or "").strip()) or "/tmp",
    "profile_loop_bundle",
)
_PROFILE_LOOP_BUNDLE_DIR = os.getenv("PROFILE_LOOP_BUNDLE_DIR", _DEFAULT_PROFILE_LOOP_BUNDLE_DIR)
_PROFILE_LOOP_BUNDLE_NAME = {
    "airband": "rtl_airband_profile_loop_airband.conf",
    "ground": "rtl_airband_profile_loop_ground.conf",
}
_PROFILE_LOOP_MAX_SELECTED = 128


def _normalize_hold_state(state):
    if not isinstance(state, dict):
        return {}
    if state.get("active") and state.get("target") in ("airband", "ground"):
        return {state["target"]: state}
    return state


def _load_hold_state():
    """Load persisted hold state."""
    try:
        with open(HOLD_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    return _normalize_hold_state(data)


def _save_hold_state(data: dict) -> None:
    """Persist hold state to disk."""
    try:
        os.makedirs(os.path.dirname(HOLD_STATE_PATH) or ".", exist_ok=True)
    except Exception:
        pass
    tmp = HOLD_STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, HOLD_STATE_PATH)


def _clear_hold_state() -> None:
    """Clear hold state file."""
    try:
        os.remove(HOLD_STATE_PATH)
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _has_active_hold(state: dict) -> bool:
    for entry in (state or {}).values():
        if isinstance(entry, dict) and entry.get("active"):
            return True
    return False


def _save_or_clear_hold_state(state: dict) -> None:
    if _has_active_hold(state):
        _save_hold_state(state)
        return
    _clear_hold_state()


_FREQ_BLOCK_RE = re.compile(r'(^\s*freqs\s*=\s*\()(.*?)(\)\s*;)', re.S | re.M)
_LABEL_BLOCK_RE = re.compile(r'(^\s*labels\s*=\s*\()(.*?)(\)\s*;)', re.S | re.M)


def _rewrite_single_freq(text: str, freq_val: float, label: str) -> str:
    """Rewrite all freq/label blocks to a single frequency."""
    def replace_freq(m):
        return f"{m.group(1)}{freq_val:.4f}{m.group(3)}"

    def replace_label(m):
        return f'{m.group(1)}"{label}"{m.group(3)}'

    text = _FREQ_BLOCK_RE.sub(replace_freq, text)
    text = _LABEL_BLOCK_RE.sub(replace_label, text)
    return text


def _load_tune_backup():
    try:
        with open(TUNE_BACKUP_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _save_tune_backup(data: dict):
    try:
        os.makedirs(os.path.dirname(TUNE_BACKUP_PATH) or ".", exist_ok=True)
    except Exception:
        pass
    tmp = TUNE_BACKUP_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, TUNE_BACKUP_PATH)


def _clear_tune_backup():
    try:
        os.remove(TUNE_BACKUP_PATH)
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _swap_symlink(link_path: str, target_path: str) -> None:
    link_path = str(link_path or "").strip()
    target_path = str(target_path or "").strip()
    if not link_path or not target_path:
        raise ValueError("missing symlink path")
    parent = os.path.dirname(link_path) or "."
    os.makedirs(parent, exist_ok=True)
    tmp_link = os.path.join(parent, f".{os.path.basename(link_path)}.tmp")
    try:
        if os.path.lexists(tmp_link):
            os.unlink(tmp_link)
    except Exception:
        pass
    os.symlink(target_path, tmp_link)
    os.replace(tmp_link, link_path)


def _parse_profile_ids(raw: Any) -> list[str]:
    if isinstance(raw, list):
        incoming = raw
    else:
        text = str(raw or "").strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                incoming = parsed if isinstance(parsed, list) else [text]
            except Exception:
                incoming = text.replace(";", ",").split(",")
        else:
            incoming = text.replace(";", ",").split(",")
    out: list[str] = []
    seen: set[str] = set()
    for item in incoming:
        value = str(item or "").strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
        if len(out) >= _PROFILE_LOOP_MAX_SELECTED:
            break
    return out


def action_apply_profile_loop_bundle(target: str, selected_profiles: Any, active_profile: str = "") -> dict:
    """Build and apply a generated analog loop-bundle config."""
    normalized = "ground" if str(target or "").strip().lower() == "ground" else "airband"
    _, profiles_airband, profiles_ground = split_profiles()
    source = profiles_ground if normalized == "ground" else profiles_airband
    profile_map: dict[str, tuple[str, str]] = {}
    for row in source:
        pid = str(row.get("id") or "").strip()
        path = str(row.get("path") or "").strip()
        if not pid or not path:
            continue
        profile_map[pid] = (str(row.get("label") or pid), path)

    selected = [pid for pid in _parse_profile_ids(selected_profiles) if pid in profile_map]
    if len(selected) < 2:
        return {"status": 400, "payload": {"ok": False, "error": "profile loop requires at least 2 selected profiles"}}

    preferred = str(active_profile or "").strip()
    template_id = preferred if preferred in profile_map else selected[0]
    template_path = profile_map.get(template_id, ("", ""))[1]
    if not template_path or not os.path.isfile(template_path):
        return {"status": 400, "payload": {"ok": False, "error": f"missing profile template: {template_id}"}}

    merged_freqs: list[float] = []
    merged_labels: list[str] = []
    seen_freqs: set[str] = set()
    for pid in selected:
        label, path = profile_map.get(pid, ("", ""))
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                freqs, labels = parse_freqs_labels(f.read())
        except Exception:
            continue
        if not freqs:
            continue
        label_list = list(labels) if isinstance(labels, list) and len(labels) == len(freqs) else [f"{float(x):.4f}" for x in freqs]
        for idx, raw_freq in enumerate(freqs):
            try:
                freq_val = float(raw_freq)
            except Exception:
                continue
            key = f"{freq_val:.4f}"
            if key in seen_freqs:
                continue
            seen_freqs.add(key)
            merged_freqs.append(freq_val)
            src_label = str(label_list[idx] if idx < len(label_list) else key).strip() or key
            merged_labels.append(f"[{pid}] {src_label}")

    if not merged_freqs:
        return {"status": 400, "payload": {"ok": False, "error": "selected profiles have no frequencies"}}

    try:
        with open(template_path, "r", encoding="utf-8", errors="ignore") as f:
            template_text = f.read()
        bundle_text = replace_freqs_labels(template_text, merged_freqs, merged_labels)
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": f"bundle build failed: {e}"}}

    try:
        os.makedirs(_PROFILE_LOOP_BUNDLE_DIR, exist_ok=True)
        bundle_name = _PROFILE_LOOP_BUNDLE_NAME.get(normalized, f"rtl_airband_profile_loop_{normalized}.conf")
        bundle_path = os.path.realpath(os.path.join(_PROFILE_LOOP_BUNDLE_DIR, bundle_name))
        bundle_tmp = f"{bundle_path}.tmp"
        previous_bundle = ""
        if os.path.isfile(bundle_path):
            with open(bundle_path, "r", encoding="utf-8", errors="ignore") as f:
                previous_bundle = f.read()
        with open(bundle_tmp, "w", encoding="utf-8") as f:
            f.write(bundle_text)
        os.replace(bundle_tmp, bundle_path)
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": f"bundle write failed: {e}"}}

    symlink_path = GROUND_CONFIG_PATH if normalized == "ground" else CONFIG_SYMLINK
    try:
        current_target = os.path.realpath(symlink_path)
    except Exception:
        current_target = ""
    try:
        _swap_symlink(symlink_path, bundle_path)
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": f"bundle activate failed: {e}"}}

    try:
        combined_changed = bool(write_combined_config())
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": f"combine failed: {e}"}}

    bundle_changed = bundle_text != previous_bundle
    symlink_changed = current_target != bundle_path
    restart_needed = bool(bundle_changed or symlink_changed or combined_changed)
    restart_ok = True
    restart_error = ""
    if restart_needed:
        restart_ok, restart_error = restart_rtl()

    payload = {
        "ok": True,
        "changed": bool(bundle_changed or symlink_changed or combined_changed),
        "restart_ok": bool(restart_ok),
        "restart_skipped": not restart_needed,
        "target": normalized,
        "bundle_path": bundle_path,
        "profile_count": len(selected),
        "frequency_count": len(merged_freqs),
    }
    if bool(bundle_changed or symlink_changed):
        mark_analog_hit_cutoff(normalized)
    if restart_error:
        payload["restart_error"] = str(restart_error)
    return {"status": 200 if restart_ok else 500, "payload": payload}


def action_set_profile(profile_id: str, target: str, *, restart_service: bool = True) -> dict:
    """Action: Set a profile."""
    _, profiles_airband, profiles_ground = split_profiles()
    if target == "ground":
        conf_path = os.path.realpath(GROUND_CONFIG_PATH)
        profiles = [(p["id"], p["label"], p["path"]) for p in profiles_ground]
        unit_restart = restart_rtl
        target_symlink = GROUND_CONFIG_PATH
    else:
        conf_path = read_active_config_path()
        profiles = [(p["id"], p["label"], p["path"]) for p in profiles_airband]
        unit_restart = restart_rtl
        target_symlink = CONFIG_SYMLINK

    if not profiles:
        return {"status": 400, "payload": {"ok": False, "error": "no profiles available"}}

    current_profile = guess_current_profile(conf_path, profiles)
    
    # Only proceed if profile actually changed
    if profile_id and profile_id != current_profile:
        # Safety: refuse to activate a profile with an empty freqs list.
        # rtl_airband will refuse to start if freqs is empty.
        next_path = None
        for pid, _, path in profiles:
            if pid == profile_id:
                next_path = path
                break
        if next_path and os.path.exists(next_path):
            try:
                with open(next_path, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                freqs, _labels = parse_freqs_labels(text)
                if not freqs:
                    return {"status": 400, "payload": {"ok": False, "error": "profile has no frequencies; add freqs and save first"}}
            except Exception:
                # If parsing fails, continue; rtl_airband will surface config errors.
                pass

        ok, changed = set_profile(profile_id, conf_path, profiles, target_symlink)
        if not ok:
            return {"status": 400, "payload": {"ok": False, "error": "unknown profile"}}
        
        combined_changed = False
        restart_ok = True
        restart_error = ""

        # Loop-mode profile switches can skip restart to keep stream/mount
        # continuity. Manual profile sets keep restart behavior.
        if restart_service:
            try:
                combined_changed = write_combined_config()
            except Exception as e:
                return {"status": 500, "payload": {"ok": False, "error": f"combine failed: {e}"}}

            # Only restart if combined config actually changed.
            # This avoids unnecessary restarts when frequency lists are identical.
            if combined_changed:
                restart_ok, restart_error = unit_restart()

        payload = {"ok": True, "changed": changed or combined_changed}
        if restart_service and combined_changed:
            payload["restart_ok"] = restart_ok
            if not restart_ok and restart_error:
                payload["restart_error"] = restart_error
        if not restart_service:
            payload["restart_skipped"] = True
        if changed:
            mark_analog_hit_cutoff(target)
        return {"status": 200, "payload": payload}
    
    # No profile change requested
    return {"status": 200, "payload": {"ok": True, "changed": False}}


def action_apply_controls(target: str, gain: float, squelch_mode: str, squelch_snr: float, squelch_dbfs: float) -> dict:
    """Action: Apply gain/squelch controls."""
    if target == "ground":
        conf_path = GROUND_CONFIG_PATH
    elif target == "airband":
        conf_path = read_active_config_path()
    else:
        return {"status": 400, "payload": {"ok": False, "error": "unknown target"}}
    try:
        changed = write_controls(conf_path, gain, squelch_mode, squelch_snr, squelch_dbfs)
        combined_changed = write_combined_config()
        changed = changed or combined_changed
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": str(e)}}
    restart_ok = True
    restart_error = ""
    if changed:
        restart_ok, restart_error = restart_rtl()
    payload = {"ok": True, "changed": changed}
    if changed:
        payload["restart_ok"] = restart_ok
        if not restart_ok and restart_error:
            payload["restart_error"] = restart_error
    return {"status": 200, "payload": payload}


def action_apply_batch(target: str, gain: float, squelch_mode: str, squelch_snr: float, squelch_dbfs: float, cutoff_hz: float) -> dict:
    """Apply gain/squelch and filter in a single restart."""
    if target == "ground":
        conf_path = GROUND_CONFIG_PATH
    elif target == "airband":
        conf_path = read_active_config_path()
    else:
        return {"status": 400, "payload": {"ok": False, "error": "unknown target"}}
    try:
        changed_controls = write_controls(conf_path, gain, squelch_mode, squelch_snr, squelch_dbfs)
        changed_filter = write_filter(target, cutoff_hz)
        combined_changed = write_combined_config() if changed_controls else False
        changed = changed_controls or changed_filter or combined_changed
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": str(e)}}
    restart_ok = True
    restart_error = ""
    if changed:
        restart_ok, restart_error = restart_rtl()
    payload = {"ok": True, "changed": changed}
    if changed:
        payload["restart_ok"] = restart_ok
        if not restart_ok and restart_error:
            payload["restart_error"] = restart_error
    return {"status": 200, "payload": payload}


def action_apply_filter(target: str, cutoff_hz: float) -> dict:
    """Action: Apply noise filter."""
    if target not in ("airband", "ground"):
        return {"status": 400, "payload": {"ok": False, "error": "unknown target"}}
    try:
        changed = write_filter(target, cutoff_hz)
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": str(e)}}
    restart_ok = True
    restart_error = ""
    if changed:
        restart_ok, restart_error = restart_rtl()
    payload = {"ok": True, "changed": changed}
    if changed:
        payload["restart_ok"] = restart_ok
        if not restart_ok and restart_error:
            payload["restart_error"] = restart_error
    return {"status": 200, "payload": payload}


def action_restart(target: str) -> dict:
    """Action: Restart a scanner."""
    if target == "ground":
        ok, err = restart_ground()
    elif target == "airband":
        ok, err = restart_rtl()
    elif target == "icecast":
        ok, err = restart_icecast()
    elif target == "keepalive":
        ok, err = restart_keepalive()
    elif target == "ui":
        ok, err = restart_ui()
    elif target == "digital":
        ok, err = restart_digital()
    elif target == "all":
        results = {
            "airband": restart_rtl(),
            "ground": restart_ground(),
            "icecast": restart_icecast(),
            "keepalive": restart_keepalive(),
            "ui": restart_ui(),
            "digital": restart_digital(),
        }
        ok = all(v[0] for v in results.values())
        err = "; ".join(f"{k}:{v[1]}" for k, v in results.items() if v[1])
    else:
        return {"status": 400, "payload": {"ok": False, "error": "unknown target"}}
    payload = {"ok": bool(ok)}
    if err:
        payload["restart_error"] = err
    return {"status": 200 if ok else 500, "payload": payload}


def action_avoid(target: str) -> dict:
    """Action: Avoid the current frequency."""
    if target not in ("airband", "ground"):
        return {"status": 400, "payload": {"ok": False, "error": "unknown target"}}
    conf_path = os.path.realpath(GROUND_CONFIG_PATH) if target == "ground" else read_active_config_path()
    stop_rtl()
    try:
        freq, err = avoid_current_hit(conf_path, target)
    except Exception as e:
        start_rtl()
        return {"status": 500, "payload": {"ok": False, "error": str(e)}}
    if err:
        start_rtl()
        return {"status": 400, "payload": {"ok": False, "error": err}}
    try:
        write_combined_config()
    except Exception as e:
        start_rtl()
        return {"status": 500, "payload": {"ok": False, "error": f"combine failed: {e}"}}
    restart_ok, restart_error = restart_rtl()
    payload = {"ok": True, "freq": f"{freq:.4f}", "restart_ok": restart_ok}
    if not restart_ok and restart_error:
        payload["restart_error"] = restart_error
    return {"status": 200, "payload": payload}


def action_avoid_clear(target: str) -> dict:
    """Action: Clear avoids."""
    if target not in ("airband", "ground"):
        return {"status": 400, "payload": {"ok": False, "error": "unknown target"}}
    conf_path = os.path.realpath(GROUND_CONFIG_PATH) if target == "ground" else read_active_config_path()
    stop_rtl()
    try:
        _, err = clear_avoids(conf_path, target)
    except Exception as e:
        start_rtl()
        return {"status": 500, "payload": {"ok": False, "error": str(e)}}
    if err:
        start_rtl()
        return {"status": 400, "payload": {"ok": False, "error": err}}
    try:
        write_combined_config()
    except Exception as e:
        start_rtl()
        return {"status": 500, "payload": {"ok": False, "error": f"combine failed: {e}"}}
    restart_ok, restart_error = restart_rtl()
    payload = {"ok": True, "restart_ok": restart_ok}
    if not restart_ok and restart_error:
        payload["restart_error"] = restart_error
    return {"status": 200, "payload": payload}


def _write_text(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _swap_symlink(link_path: str, target_path: str) -> None:
    """Atomically repoint a symlink without invoking a shell."""
    link_path = str(link_path)
    parent = os.path.dirname(link_path) or "."
    os.makedirs(parent, exist_ok=True)
    tmp_link = os.path.join(parent, f".{os.path.basename(link_path)}.tmp")
    try:
        if os.path.lexists(tmp_link):
            os.unlink(tmp_link)
    except Exception:
        pass
    os.symlink(str(target_path), tmp_link)
    os.replace(tmp_link, link_path)


def _write_temp_config(target: str, purpose: str, text: str) -> str:
    tmp_dir = "/run"
    os.makedirs(tmp_dir, exist_ok=True)
    path = os.path.join(tmp_dir, f"airband_ui_{purpose}_{target}.conf")
    _write_text(path, text)
    return path


def _get_symlink_target(path: str) -> str:
    try:
        return os.path.realpath(path)
    except Exception:
        return path


def _resolve_config_target(target: str):
    """Resolve config path + symlink info for a target."""
    if target == "airband":
        symlink_path = CONFIG_SYMLINK if os.path.islink(CONFIG_SYMLINK) else None
        original_target = _get_symlink_target(CONFIG_SYMLINK) if symlink_path else None
        conf_path = original_target if symlink_path else read_active_config_path()
        return conf_path, symlink_path, original_target
    if target == "ground":
        symlink_path = GROUND_CONFIG_PATH if os.path.islink(GROUND_CONFIG_PATH) else None
        original_target = _get_symlink_target(GROUND_CONFIG_PATH) if symlink_path else None
        conf_path = original_target if symlink_path else os.path.realpath(GROUND_CONFIG_PATH)
        return conf_path, symlink_path, original_target
    return None, None, None


def action_hold_start(target: str, freq) -> dict:
    """Action: Enter hold by replacing target config with single frequency."""
    if target not in ("airband", "ground"):
        return {"status": 400, "payload": {"ok": False, "error": "unknown target"}}
    try:
        freq_val = float(freq)
    except (TypeError, ValueError):
        return {"status": 400, "payload": {"ok": False, "error": "bad freq"}}

    state = _load_hold_state() or {}
    target_state = state.get(target) or {}
    if target_state.get("active"):
        return {"status": 400, "payload": {"ok": False, "error": f"already holding {target_state.get('freq')}"}}

    conf_path, symlink_path, original_target = _resolve_config_target(target)
    try:
        with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
            original = f.read()
    except FileNotFoundError:
        return {"status": 400, "payload": {"ok": False, "error": "config not found"}}

    label = f"{freq_val:.4f}"
    try:
        new_text = _rewrite_single_freq(original, freq_val, label)
    except Exception as e:
        return {"status": 400, "payload": {"ok": False, "error": f"rewrite failed: {e}"}}

    temp_path = None
    if symlink_path:
        try:
            temp_path = _write_temp_config(target, "hold", new_text)
            _swap_symlink(symlink_path, temp_path)
        except Exception as e:
            return {"status": 500, "payload": {"ok": False, "error": f"write failed: {e}"}}
    else:
        try:
            _write_text(conf_path, new_text)
        except Exception as e:
            return {"status": 500, "payload": {"ok": False, "error": f"write failed: {e}"}}

    hold_state = {
        "active": True,
        "target": target,
        "freq": f"{freq_val:.4f}",
        "conf_path": conf_path,
        "symlink_path": symlink_path,
        "original_target": original_target,
        "temp_path": temp_path,
        "original_text": original,
        "ts": time.time(),
    }
    state[target] = hold_state
    try:
        _save_hold_state(state)
    except Exception:
        pass

    try:
        write_combined_config()
    except Exception as e:
        # rollback to original config if combine fails
        try:
            if symlink_path and state.get(target):
                original_target = state[target].get("original_target")
                if original_target:
                    _swap_symlink(symlink_path, original_target)
            else:
                _write_text(conf_path, original)
            state.pop(target, None)
            _save_or_clear_hold_state(state)
        except Exception:
            pass
        return {"status": 500, "payload": {"ok": False, "error": f"combine failed: {e}"}}

    restart_ok, restart_error = restart_rtl()
    payload = {"ok": True, "freq": f"{freq_val:.4f}", "target": target, "restart_ok": restart_ok}
    if not restart_ok and restart_error:
        payload["restart_error"] = restart_error
    return {"status": 200, "payload": payload}


def action_hold_stop(target: str) -> dict:
    """Action: Exit hold and restore original config."""
    state = _load_hold_state() or {}
    target_state = state.get(target) or {}
    if not target_state.get("active"):
        return {"status": 200, "payload": {"ok": True, "restored": False}}

    conf_path = target_state.get("conf_path") or (os.path.realpath(GROUND_CONFIG_PATH) if target == "ground" else read_active_config_path())
    original = target_state.get("original_text")
    symlink_path = target_state.get("symlink_path")
    original_target = target_state.get("original_target")
    temp_path = target_state.get("temp_path")
    if not conf_path or original is None:
        state.pop(target, None)
        _save_or_clear_hold_state(state)
        return {"status": 400, "payload": {"ok": False, "error": "hold state incomplete"}}

    try:
        if symlink_path and original_target:
            _swap_symlink(symlink_path, original_target)
            if temp_path:
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
        else:
            _write_text(conf_path, original)
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": f"restore failed: {e}"}}

    _clear_hold_state()

    try:
        write_combined_config()
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": f"combine failed: {e}"}}

    restart_ok, restart_error = restart_rtl()
    payload = {"ok": True, "restored": True, "restart_ok": restart_ok}
    if not restart_ok and restart_error:
        payload["restart_error"] = restart_error
    return {"status": 200, "payload": payload}


def action_tune(target: str, freq) -> dict:
    """Action: Directly tune target config to a single frequency (no auto-restore)."""
    if target not in ("airband", "ground"):
        return {"status": 400, "payload": {"ok": False, "error": "unknown target"}}
    state = _load_hold_state() or {}
    if (state.get(target) or {}).get("active"):
        return {"status": 400, "payload": {"ok": False, "error": "cannot tune while hold is active"}}
    try:
        freq_val = float(freq)
    except (TypeError, ValueError):
        return {"status": 400, "payload": {"ok": False, "error": "bad freq"}}

    conf_path, symlink_path, original_target = _resolve_config_target(target)
    try:
        with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
            original = f.read()
    except FileNotFoundError:
        return {"status": 400, "payload": {"ok": False, "error": "config not found"}}

    # Save backup for restore
    backup = _load_tune_backup()
    backup[target] = {
        "conf_path": conf_path,
        "original_text": original,
        "symlink_path": symlink_path,
        "original_target": original_target,
        "temp_path": None,
        "ts": time.time(),
    }
    try:
        _save_tune_backup(backup)
    except Exception:
        pass

    label = f"{freq_val:.4f}"
    try:
        new_text = _rewrite_single_freq(original, freq_val, label)
    except Exception as e:
        return {"status": 400, "payload": {"ok": False, "error": f"rewrite failed: {e}"}}

    try:
        if symlink_path:
            temp_path = _write_temp_config(target, "tune", new_text)
            backup[target]["temp_path"] = temp_path
            _save_tune_backup(backup)
            _swap_symlink(symlink_path, temp_path)
        else:
            _write_text(conf_path, new_text)
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": f"write failed: {e}"}}

    try:
        write_combined_config()
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": f"combine failed: {e}"}}

    restart_ok, restart_error = restart_rtl()
    payload = {"ok": True, "freq": f"{freq_val:.4f}", "target": target, "restart_ok": restart_ok}
    if not restart_ok and restart_error:
        payload["restart_error"] = restart_error
    return {"status": 200, "payload": payload}


def action_tune_restore(target: str) -> dict:
    """Restore config from tune backup."""
    if target not in ("airband", "ground"):
        return {"status": 400, "payload": {"ok": False, "error": "unknown target"}}
    backup = _load_tune_backup()
    entry = backup.get(target)
    if not entry:
        return {"status": 400, "payload": {"ok": False, "error": "no tune backup"}}
    conf_path = entry.get("conf_path")
    original = entry.get("original_text")
    symlink_path = entry.get("symlink_path")
    original_target = entry.get("original_target")
    temp_path = entry.get("temp_path")
    if not conf_path or original is None:
        return {"status": 400, "payload": {"ok": False, "error": "invalid backup"}}
    try:
        if symlink_path and original_target:
            _swap_symlink(symlink_path, original_target)
            if temp_path:
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
        else:
            _write_text(conf_path, original)
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": f"restore failed: {e}"}}
    backup.pop(target, None)
    try:
        _save_tune_backup(backup)
    except Exception:
        pass
    try:
        write_combined_config()
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": f"combine failed: {e}"}}
    restart_ok, restart_error = restart_rtl()
    payload = {"ok": True, "restart_ok": restart_ok}
    if not restart_ok and restart_error:
        payload["restart_error"] = restart_error
    return {"status": 200, "payload": payload}


def execute_action(action: dict) -> dict:
    """Execute an action based on its type."""
    action_type = action.get("type")
    if action_type == "apply":
        return action_apply_controls(
            action.get("target"),
            action.get("gain"),
            action.get("squelch_mode"),
            action.get("squelch_snr"),
            action.get("squelch_dbfs"),
        )
    if action_type == "apply_batch":
        return action_apply_batch(
            action.get("target"),
            action.get("gain"),
            action.get("squelch_mode"),
            action.get("squelch_snr"),
            action.get("squelch_dbfs"),
            action.get("cutoff_hz"),
        )
    if action_type == "filter":
        return action_apply_filter(action.get("target"), action.get("cutoff_hz"))
    if action_type == "profile":
        restart_service = action.get("restart_service", True)
        if isinstance(restart_service, str):
            restart_service = restart_service.strip().lower() not in ("0", "false", "no", "off")
        else:
            restart_service = bool(restart_service)
        return action_set_profile(
            action.get("profile"),
            action.get("target"),
            restart_service=restart_service,
        )
    if action_type == "profile_loop_bundle":
        return action_apply_profile_loop_bundle(
            action.get("target"),
            action.get("selected_profiles"),
            action.get("active_profile"),
        )
    if action_type == "restart":
        return action_restart(action.get("target"))
    if action_type == "avoid":
        return action_avoid(action.get("target"))
    if action_type == "avoid_clear":
        return action_avoid_clear(action.get("target"))
    if action_type == "hold":
        mode = action.get("mode") or "start"
        if mode == "stop":
            return action_hold_stop(action.get("target"))
        return action_hold_start(action.get("target"), action.get("freq"))
    if action_type == "tune":
        return action_tune(action.get("target"), action.get("freq"))
    if action_type == "tune_restore":
        return action_tune_restore(action.get("target"))
    return {"status": 400, "payload": {"ok": False, "error": "unknown action"}}
