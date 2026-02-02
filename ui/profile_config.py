"""Configuration file management for profiles and controls."""
import os
import json
import time
import re
import sys
from typing import Optional
import logging

logger = logging.getLogger(__name__)

try:
    from .config import (
        CONFIG_SYMLINK, PROFILES_DIR, GROUND_CONFIG_PATH, COMBINED_CONFIG_PATH,
        AVOIDS_DIR, AVOIDS_PATHS, AVOIDS_SUMMARY_PATHS, PROFILES, GAIN_STEPS,
        RE_GAIN, RE_SQL, RE_AIRBAND, RE_INDEX, RE_FREQS_BLOCK, RE_LABELS_BLOCK,
        MIXER_NAME, FILTER_AIRBAND_PATH, FILTER_GROUND_PATH, FILTER_DEFAULT_CUTOFF,
        FILTER_MIN_CUTOFF, FILTER_MAX_CUTOFF
    )
    from .systemd import restart_rtl, stop_rtl, start_rtl
except ImportError:
    from ui.config import (
        CONFIG_SYMLINK, PROFILES_DIR, GROUND_CONFIG_PATH, COMBINED_CONFIG_PATH,
        AVOIDS_DIR, AVOIDS_PATHS, AVOIDS_SUMMARY_PATHS, PROFILES, GAIN_STEPS,
        RE_GAIN, RE_SQL, RE_AIRBAND, RE_INDEX, RE_FREQS_BLOCK, RE_LABELS_BLOCK,
        MIXER_NAME, FILTER_AIRBAND_PATH, FILTER_GROUND_PATH, FILTER_DEFAULT_CUTOFF,
        FILTER_MIN_CUTOFF, FILTER_MAX_CUTOFF
    )
    from ui.systemd import restart_rtl, stop_rtl, start_rtl

# Import combined_config builder from root
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
from combined_config import build_combined_config


def read_active_config_path() -> str:
    """Read the active configuration path symlink."""
    try:
        return os.path.realpath(CONFIG_SYMLINK)
    except Exception:
        return CONFIG_SYMLINK


def read_airband_flag(conf_path: str) -> Optional[bool]:
    """Read whether a config is for airband or ground."""
    try:
        with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                match = RE_AIRBAND.match(line)
                if match:
                    return match.group(1).lower() == "true"
    except FileNotFoundError:
        return None
    return None


def write_combined_config() -> bool:
    """Write the combined configuration."""
    airband_path = read_active_config_path()
    ground_path = os.path.realpath(GROUND_CONFIG_PATH)
    combined = build_combined_config(airband_path, ground_path, MIXER_NAME)
    try:
        with open(COMBINED_CONFIG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            existing = f.read()
    except FileNotFoundError:
        existing = None
    if existing == combined:
        return False
    tmp = COMBINED_CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(combined)
    os.replace(tmp, COMBINED_CONFIG_PATH)
    return True


def split_profiles():
    """Split profiles into airband and ground categories."""
    prof_payload = []
    pid_overrides = {
        "airband": True,
        "tower": True,
        "none_airband": True,
        "gmrs": False,
        "wx": False,
        "none_ground": False,
    }
    for pid, label, path in PROFILES:
        exists = os.path.exists(path)
        airband_flag = pid_overrides.get(pid)
        if airband_flag is None and exists:
            airband_flag = read_airband_flag(path)
        prof_payload.append({
            "id": pid,
            "label": label,
            "path": path,
            "exists": exists,
            "airband": airband_flag,
        })
    profiles_airband = [p for p in prof_payload if p.get("airband") is True]
    profiles_ground = [p for p in prof_payload if p.get("airband") is False]
    return prof_payload, profiles_airband, profiles_ground


def enforce_profile_index(conf_path: str) -> None:
    """Enforce correct profile index based on airband flag."""
    conf_path = os.path.realpath(conf_path)
    try:
        with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return

    airband_value = None
    for line in lines:
        match = RE_AIRBAND.match(line)
        if match:
            airband_value = match.group(1).lower() == "true"
            break
    if airband_value is None:
        return

    desired_index = 0 if airband_value else 1
    out = []
    changed = False
    for line in lines:
        match = RE_INDEX.match(line)
        if match:
            new_line = f"{match.group(1)}{desired_index}{match.group(3)}\n"
            if new_line != line:
                changed = True
            out.append(new_line)
            continue
        out.append(line)

    if not changed:
        return

    tmp = conf_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(out)
    os.replace(tmp, conf_path)


def parse_controls(conf_path: str):
    """Parse gain and squelch from configuration."""
    enforce_profile_index(conf_path)
    gain = 32.8
    squelch = 10.0
    try:
        with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = RE_GAIN.match(line)
                if m:
                    gain = float(m.group(2))
                m = RE_SQL.match(line)
                if m:
                    squelch = max(0.0, float(m.group(2)))
    except FileNotFoundError:
        pass

    # Only emit verbose parse logs when explicitly enabled via env var
    if os.environ.get("AIRBAND_DEBUG"):
        logger.debug(f"parse_controls: {conf_path} gain={gain} squelch={squelch}")
    return gain, squelch


def write_controls(conf_path: str, gain: float, squelch: float) -> bool:
    """Write gain and squelch to configuration."""
    gain_value = float(gain)
    gain = min(GAIN_STEPS, key=lambda g: abs(g - gain_value))
    squelch = max(0.0, min(10.0, float(squelch)))

    conf_path = os.path.realpath(conf_path)

    logger.info(f"write_controls: {conf_path} gain={gain} squelch={squelch}")

    with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    out = []
    changed = False
    for line in lines:
        m = RE_GAIN.match(line)
        if m:
            new_line = f"{m.group(1)}{gain:.3f}{m.group(3)}\n"
            if new_line != line:
                changed = True
            out.append(new_line)
            continue
        m = RE_SQL.match(line)
        if m:
            new_line = f"{m.group(1)}{squelch:.3f}{m.group(3)}\n"
            if new_line != line:
                changed = True
            out.append(new_line)
            continue
        out.append(line)

    if not changed:
        logger.debug("write_controls: no change")
        return False

    tmp = conf_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(out)
    os.replace(tmp, conf_path)

    logger.info("write_controls: updated config")
    return True


def parse_filter(target: str) -> float:
    """Parse filter cutoff frequency for a target (airband or ground)."""
    filter_path = FILTER_GROUND_PATH if target == "ground" else FILTER_AIRBAND_PATH
    try:
        with open(filter_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            cutoff = float(data.get("cutoff_hz", FILTER_DEFAULT_CUTOFF))
            return max(FILTER_MIN_CUTOFF, min(FILTER_MAX_CUTOFF, cutoff))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return FILTER_DEFAULT_CUTOFF


def write_filter(target: str, cutoff_hz: float) -> bool:
    """Write filter configuration for a target."""
    filter_path = FILTER_GROUND_PATH if target == "ground" else FILTER_AIRBAND_PATH
    cutoff = max(FILTER_MIN_CUTOFF, min(FILTER_MAX_CUTOFF, float(cutoff_hz)))
    
    # Read current config
    try:
        with open(filter_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    
    # Check if changed
    old_cutoff = data.get("cutoff_hz", FILTER_DEFAULT_CUTOFF)
    if abs(old_cutoff - cutoff) < 0.01:
        return False
    
    data["cutoff_hz"] = cutoff
    data["updated_at"] = time.time()
    
    # Write atomically
    os.makedirs(os.path.dirname(filter_path), exist_ok=True)
    tmp = filter_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, filter_path)
    return True


def guess_current_profile(conf_realpath: str, profiles):
    """Guess the current profile from config path."""
    for pid, _, path in profiles:
        if os.path.realpath(path) == conf_realpath:
            return pid
    return profiles[0][0] if profiles else ""


def set_profile(profile_id: str, current_conf_path: str, profiles, target_symlink: str):
    """Set a profile and return whether it changed."""
    for pid, _, path in profiles:
        if pid == profile_id:
            if os.path.realpath(path) == os.path.realpath(current_conf_path):
                return True, False
            enforce_profile_index(path)
            import subprocess
            subprocess.run(["ln", "-sf", path, target_symlink], check=False)
            return True, True
    return False, False


def load_avoids(target: str) -> dict:
    """Load avoids data for a target."""
    path = AVOIDS_PATHS.get(target, AVOIDS_PATHS["airband"])
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("profiles", {})
                return data
    except FileNotFoundError:
        pass
    except json.JSONDecodeError:
        pass
    return {"profiles": {}}


def summarize_avoids(conf_path: str, target: str) -> dict:
    """Summarize avoids for a configuration."""
    data = load_avoids(target)
    prof = data.get("profiles", {}).get(conf_path, {})
    avoids = prof.get("avoids", []) or []
    avoids_sorted = sorted(avoids)
    sample = [f"{freq:.4f}" for freq in avoids_sorted[:4]]
    return {"count": len(avoids), "sample": sample}


def save_avoids(target: str, data: dict) -> None:
    """Save avoids data."""
    path = AVOIDS_PATHS.get(target, AVOIDS_PATHS["airband"])
    os.makedirs(AVOIDS_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)
    write_avoids_summary(target, data)


def write_avoids_summary(target: str, data: dict) -> None:
    """Write a summary of avoids."""
    lines = []
    ts = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())
    label = "Ground" if target == "ground" else "Airband"
    lines.append(f"SprontPi {label} Avoids Summary (UTC {ts})")
    lines.append("")

    profiles = data.get("profiles", {})
    if not profiles:
        lines.append("No avoids recorded.")
    else:
        path_to_label = {path: label for _, label, path in PROFILES}
        for conf_path in sorted(profiles.keys()):
            prof = profiles.get(conf_path, {})
            avoids = sorted(prof.get("avoids", []) or [])
            label = path_to_label.get(conf_path, os.path.basename(conf_path))
            lines.append(f"Profile: {label}")
            lines.append(f"Config: {conf_path}")
            if avoids:
                lines.append(f"Avoids ({len(avoids)}): " + ", ".join(f"{f:.4f}" for f in avoids))
            else:
                lines.append("Avoids: none")
            lines.append("")

    path = AVOIDS_SUMMARY_PATHS.get(target, AVOIDS_SUMMARY_PATHS["airband"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    os.replace(tmp, path)


def parse_freqs_labels(text: str):
    """Parse frequencies and labels from config."""
    m = RE_FREQS_BLOCK.search(text)
    if not m:
        raise ValueError("freqs block not found")
    freqs = [float(x) for x in re.findall(r'[0-9]+(?:\.[0-9]+)?', m.group(2))]
    labels = None
    m = RE_LABELS_BLOCK.search(text)
    if m:
        labels = re.findall(r'"([^"]+)"', m.group(2))
    return freqs, labels


def replace_freqs_labels(text: str, freqs, labels):
    """Replace frequencies and labels in config."""
    freqs_text = ", ".join(f"{f:.4f}" for f in freqs)
    text = RE_FREQS_BLOCK.sub(lambda m: f"{m.group(1)}{freqs_text}{m.group(3)}", text, count=1)
    if labels is not None:
        labels_text = ", ".join(f"\"{l}\"" for l in labels)
        text = RE_LABELS_BLOCK.sub(lambda m: f"{m.group(1)}{labels_text}{m.group(3)}", text, count=1)
    return text


def same_freq(a: float, b: float) -> bool:
    """Check if two frequencies are the same within tolerance."""
    return abs(a - b) < 0.0005


def filter_freqs_labels(freqs, labels, avoids):
    """Filter out avoided frequencies."""
    if labels is not None and len(labels) != len(freqs):
        labels = [f"{f:.4f}" for f in freqs]
    kept_freqs = []
    kept_labels = [] if labels is not None else None
    for idx, freq in enumerate(freqs):
        if any(same_freq(freq, avoid) for avoid in avoids):
            continue
        kept_freqs.append(freq)
        if kept_labels is not None:
            kept_labels.append(labels[idx])
    return kept_freqs, kept_labels


def write_freqs_labels(conf_path: str, freqs, labels):
    """Write frequencies and labels to config."""
    with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()
    text = replace_freqs_labels(text, freqs, labels)
    tmp = conf_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, conf_path)


def avoid_current_hit(conf_path: str, target: str):
    """Add the current hit to the avoid list."""
    from scanner import parse_last_hit_freq
    freq = parse_last_hit_freq(target)
    if freq is None:
        return None, "No recent hit to avoid"

    data = load_avoids(target)
    profiles = data.setdefault("profiles", {})
    prof = profiles.get(conf_path)

    if not prof:
        with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
            freqs, labels = parse_freqs_labels(f.read())
        prof = {
            "original_freqs": freqs,
            "original_labels": labels,
            "avoids": [],
        }
        profiles[conf_path] = prof

    avoids = prof.get("avoids", [])
    if not any(same_freq(freq, avoid) for avoid in avoids):
        avoids.append(freq)
        prof["avoids"] = avoids

    base_freqs = prof.get("original_freqs") or []
    base_labels = prof.get("original_labels")
    if not base_freqs:
        return None, "No freqs found to avoid"

    new_freqs, new_labels = filter_freqs_labels(base_freqs, base_labels, avoids)
    if not new_freqs:
        return None, "Avoid would remove all frequencies"

    write_freqs_labels(conf_path, new_freqs, new_labels)
    save_avoids(target, data)
    return freq, None


def clear_avoids(conf_path: str, target: str):
    """Clear avoids for a configuration."""
    data = load_avoids(target)
    profiles = data.get("profiles", {})
    prof = profiles.get(conf_path)
    if not prof:
        return 0, None

    freqs = prof.get("original_freqs") or []
    labels = prof.get("original_labels")
    if not freqs:
        return 0, "No stored freqs to restore"

    write_freqs_labels(conf_path, freqs, labels)
    profiles.pop(conf_path, None)
    save_avoids(target, data)
    return len(freqs), None
