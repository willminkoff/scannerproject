"""Business logic for scanner actions."""
import os
import json
import time
import re

try:
    from .config import CONFIG_SYMLINK, GROUND_CONFIG_PATH, COMBINED_CONFIG_PATH, HOLD_STATE_PATH, TUNE_BACKUP_PATH
    from .systemd import (
        unit_active, stop_rtl, start_rtl, restart_rtl, stop_ground
    )
    from .profile_config import (
        split_profiles, guess_current_profile, set_profile, write_controls,
        write_combined_config, read_active_config_path, avoid_current_hit,
        clear_avoids, write_filter, parse_freqs_labels, replace_freqs_labels
    )
except ImportError:
    from ui.config import CONFIG_SYMLINK, GROUND_CONFIG_PATH, COMBINED_CONFIG_PATH, HOLD_STATE_PATH, TUNE_BACKUP_PATH
    from ui.systemd import (
        unit_active, stop_rtl, start_rtl, restart_rtl, stop_ground
    )
    from ui.profile_config import (
        split_profiles, guess_current_profile, set_profile, write_controls,
        write_combined_config, read_active_config_path, avoid_current_hit,
        clear_avoids, write_filter, parse_freqs_labels, replace_freqs_labels
    )


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


def action_set_profile(profile_id: str, target: str) -> dict:
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
        ok, changed = set_profile(profile_id, conf_path, profiles, target_symlink)
        if not ok:
            return {"status": 400, "payload": {"ok": False, "error": "unknown profile"}}
        
        try:
            combined_changed = write_combined_config()
        except Exception as e:
            return {"status": 500, "payload": {"ok": False, "error": f"combine failed: {e}"}}
        
        # Only restart if combined config actually changed
        # This avoids unnecessary restarts when frequency lists are identical
        if combined_changed:
            unit_restart()
        
        return {"status": 200, "payload": {"ok": True, "changed": changed or combined_changed}}
    
    # No profile change requested
    return {"status": 200, "payload": {"ok": True, "changed": False}}


def action_apply_controls(target: str, gain: float, squelch: float) -> dict:
    """Action: Apply gain/squelch controls."""
    if target == "ground":
        conf_path = GROUND_CONFIG_PATH
    elif target == "airband":
        conf_path = read_active_config_path()
    else:
        return {"status": 400, "payload": {"ok": False, "error": "unknown target"}}
    try:
        changed = write_controls(conf_path, gain, squelch)
        combined_changed = write_combined_config()
        changed = changed or combined_changed
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": str(e)}}
    if changed:
        restart_rtl()
    return {"status": 200, "payload": {"ok": True, "changed": changed}}


def action_apply_filter(target: str, cutoff_hz: float) -> dict:
    """Action: Apply noise filter."""
    if target not in ("airband", "ground"):
        return {"status": 400, "payload": {"ok": False, "error": "unknown target"}}
    try:
        changed = write_filter(target, cutoff_hz)
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": str(e)}}
    if changed:
        restart_rtl()
    return {"status": 200, "payload": {"ok": True, "changed": changed}}


def action_restart(target: str) -> dict:
    """Action: Restart a scanner."""
    if target == "ground":
        restart_rtl()
    elif target == "airband":
        restart_rtl()
    else:
        return {"status": 400, "payload": {"ok": False, "error": "unknown target"}}
    return {"status": 200, "payload": {"ok": True}}


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
    restart_rtl()
    return {"status": 200, "payload": {"ok": True, "freq": f"{freq:.4f}"}}


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
    restart_rtl()
    return {"status": 200, "payload": {"ok": True}}


def _write_text(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


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

    conf_path = os.path.realpath(GROUND_CONFIG_PATH) if target == "ground" else read_active_config_path()
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

    try:
        _write_text(conf_path, new_text)
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": f"write failed: {e}"}}

    hold_state = {
        "active": True,
        "target": target,
        "freq": f"{freq_val:.4f}",
        "conf_path": conf_path,
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
            _write_text(conf_path, original)
            state.pop(target, None)
            _save_or_clear_hold_state(state)
        except Exception:
            pass
        return {"status": 500, "payload": {"ok": False, "error": f"combine failed: {e}"}}

    restart_rtl()
    return {"status": 200, "payload": {"ok": True, "freq": f"{freq_val:.4f}", "target": target}}


def action_hold_stop(target: str) -> dict:
    """Action: Exit hold and restore original config."""
    state = _load_hold_state() or {}
    target_state = state.get(target) or {}
    if not target_state.get("active"):
        return {"status": 200, "payload": {"ok": True, "restored": False}}

    conf_path = target_state.get("conf_path") or (os.path.realpath(GROUND_CONFIG_PATH) if target == "ground" else read_active_config_path())
    original = target_state.get("original_text")
    if not conf_path or original is None:
        state.pop(target, None)
        _save_or_clear_hold_state(state)
        return {"status": 400, "payload": {"ok": False, "error": "hold state incomplete"}}

    try:
        _write_text(conf_path, original)
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": f"restore failed: {e}"}}

    _clear_hold_state()

    try:
        write_combined_config()
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": f"combine failed: {e}"}}

    restart_rtl()
    return {"status": 200, "payload": {"ok": True, "restored": True}}


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

    conf_path = os.path.realpath(GROUND_CONFIG_PATH) if target == "ground" else read_active_config_path()
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
        _write_text(conf_path, new_text)
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": f"write failed: {e}"}}

    try:
        write_combined_config()
    except Exception as e:
        return {"status": 500, "payload": {"ok": False, "error": f"combine failed: {e}"}}

    restart_rtl()
    return {"status": 200, "payload": {"ok": True, "freq": f"{freq_val:.4f}", "target": target}}


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
    if not conf_path or original is None:
        return {"status": 400, "payload": {"ok": False, "error": "invalid backup"}}
    try:
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
    restart_rtl()
    return {"status": 200, "payload": {"ok": True}}


def execute_action(action: dict) -> dict:
    """Execute an action based on its type."""
    action_type = action.get("type")
    if action_type == "apply":
        return action_apply_controls(action.get("target"), action.get("gain"), action.get("squelch"))
    if action_type == "filter":
        return action_apply_filter(action.get("target"), action.get("cutoff_hz"))
    if action_type == "profile":
        return action_set_profile(action.get("profile"), action.get("target"))
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
