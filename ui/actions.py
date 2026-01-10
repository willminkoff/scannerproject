"""Business logic for scanner actions."""
import os

try:
    from .config import CONFIG_SYMLINK, GROUND_CONFIG_PATH, COMBINED_CONFIG_PATH
    from .systemd import (
        unit_active, stop_rtl, start_rtl, restart_rtl, stop_ground
    )
    from .profile_config import (
        split_profiles, guess_current_profile, set_profile, write_controls,
        write_combined_config, read_active_config_path, avoid_current_hit,
        clear_avoids, write_filter
    )
except ImportError:
    from ui.config import CONFIG_SYMLINK, GROUND_CONFIG_PATH, COMBINED_CONFIG_PATH
    from ui.systemd import (
        unit_active, stop_rtl, start_rtl, restart_rtl, stop_ground
    )
    from ui.profile_config import (
        split_profiles, guess_current_profile, set_profile, write_controls,
        write_combined_config, read_active_config_path, avoid_current_hit,
        clear_avoids, write_filter
    )


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
    return {"status": 400, "payload": {"ok": False, "error": "unknown action"}}
