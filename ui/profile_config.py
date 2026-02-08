"""Configuration file management for profiles and controls."""
import os
import json
import time
import re
import sys
from typing import Optional, List, Dict
import logging

logger = logging.getLogger(__name__)

try:
    from .config import (
        CONFIG_SYMLINK, PROFILES_DIR, PROFILES_REGISTRY_PATH, GROUND_CONFIG_PATH, COMBINED_CONFIG_PATH,
        AVOIDS_DIR, AVOIDS_PATHS, AVOIDS_SUMMARY_PATHS, PROFILES, GAIN_STEPS,
        RE_GAIN, RE_SQL, RE_SQL_DBFS, RE_AIRBAND, RE_INDEX, RE_FREQS_BLOCK, RE_LABELS_BLOCK,
        MIXER_NAME, FILTER_AIRBAND_PATH, FILTER_GROUND_PATH, FILTER_DEFAULT_CUTOFF,
        FILTER_MIN_CUTOFF, FILTER_MAX_CUTOFF
    )
    from .systemd import restart_rtl, stop_rtl, start_rtl
except ImportError:
    from ui.config import (
        CONFIG_SYMLINK, PROFILES_DIR, PROFILES_REGISTRY_PATH, GROUND_CONFIG_PATH, COMBINED_CONFIG_PATH,
        AVOIDS_DIR, AVOIDS_PATHS, AVOIDS_SUMMARY_PATHS, PROFILES, GAIN_STEPS,
        RE_GAIN, RE_SQL, RE_SQL_DBFS, RE_AIRBAND, RE_INDEX, RE_FREQS_BLOCK, RE_LABELS_BLOCK,
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


def write_airband_flag(conf_path: str, airband: bool) -> None:
    """Ensure the config contains an explicit airband=true/false line."""
    conf_path = os.path.realpath(conf_path)
    try:
        with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return

    out = []
    changed = False
    wrote = False
    for line in lines:
        match = RE_AIRBAND.match(line)
        if match:
            indent_match = re.match(r'^(\s*)', line)
            indent = indent_match.group(1) if indent_match else ""
            new_line = f"{indent}airband = {'true' if airband else 'false'};\n"
            out.append(new_line)
            changed = changed or (new_line != line)
            wrote = True
            continue
        out.append(line)

    if not wrote:
        out.insert(0, f"airband = {'true' if airband else 'false'};\n\n")
        changed = True

    if not changed:
        return

    tmp = conf_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(out)
    os.replace(tmp, conf_path)


def _infer_airband_flag(profile_id: str, path: str) -> Optional[bool]:
    """Infer airband flag using overrides or file contents."""
    pid_overrides = {
        "airband": True,
        "tower": True,
        "none_airband": True,
        "gmrs": False,
        "wx": False,
        "none_ground": False,
        "dmr_nashville": False,
    }
    if profile_id in pid_overrides:
        return pid_overrides[profile_id]
    if "ground" in profile_id:
        return False
    if "airband" in profile_id:
        return True
    if os.path.exists(path):
        return read_airband_flag(path)
    return None


def validate_profile_id(profile_id: str) -> bool:
    return bool(re.match(r'^[a-z0-9_-]{2,40}$', profile_id or ""))


def safe_profile_path(path: str) -> Optional[str]:
    root = os.path.realpath(PROFILES_DIR)
    real = os.path.realpath(path)
    if real == root:
        return None
    if not real.startswith(root + os.sep):
        return None
    return real


def _registry_payload_from_profiles(profiles) -> List[Dict]:
    payload = []
    for pid, label, path in profiles:
        airband_flag = _infer_airband_flag(pid, path)
        payload.append({
            "id": pid,
            "label": label,
            "path": path,
            "airband": airband_flag if airband_flag is not None else True,
        })
    return payload


def save_profiles_registry(profiles: List[Dict]) -> None:
    os.makedirs(PROFILES_DIR, exist_ok=True)
    tmp = PROFILES_REGISTRY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"profiles": profiles}, f, indent=2)
        f.write("\n")
    os.replace(tmp, PROFILES_REGISTRY_PATH)


def load_profiles_registry() -> List[Dict]:
    if not os.path.exists(PROFILES_REGISTRY_PATH):
        profiles = _registry_payload_from_profiles(PROFILES)
        save_profiles_registry(profiles)
        return profiles
    try:
        with open(PROFILES_REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        profiles = data.get("profiles", [])
        if isinstance(profiles, list):
            cleaned = []
            for p in profiles:
                if not isinstance(p, dict):
                    continue
                pid = p.get("id")
                label = p.get("label")
                path = p.get("path")
                airband = p.get("airband")
                if not pid or not label or not path:
                    continue
                cleaned.append({
                    "id": pid,
                    "label": label,
                    "path": path,
                    "airband": bool(airband),
                })
            if cleaned:
                defaults = _registry_payload_from_profiles(PROFILES)
                default_ids = {p.get("id"): p for p in defaults if p.get("id")}
                changed = False
                existing_ids = {p.get("id") for p in cleaned}
                for pid, prof in default_ids.items():
                    if pid not in existing_ids:
                        cleaned.append(prof)
                        changed = True
                if changed:
                    save_profiles_registry(cleaned)
                return cleaned
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    profiles = _registry_payload_from_profiles(PROFILES)
    save_profiles_registry(profiles)
    return profiles


def find_profile(profiles: List[Dict], profile_id: str) -> Optional[Dict]:
    for p in profiles:
        if p.get("id") == profile_id:
            return p
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
    for p in load_profiles_registry():
        path = p.get("path", "")
        exists = os.path.exists(path)
        prof_payload.append({
            "id": p.get("id", ""),
            "label": p.get("label", ""),
            "path": path,
            "exists": exists,
            "airband": p.get("airband") is True,
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
    squelch_snr = 10.0
    squelch_dbfs = 0.0
    has_dbfs = False
    try:
        with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = RE_GAIN.match(line)
                if m:
                    gain = float(m.group(2))
                m = RE_SQL.match(line)
                if m:
                    squelch_snr = max(0.0, float(m.group(2)))
                m = RE_SQL_DBFS.match(line)
                if m:
                    squelch_dbfs = float(m.group(2))
                    has_dbfs = True
    except FileNotFoundError:
        pass

    mode = "dbfs" if has_dbfs else "dbfs"

    # Only emit verbose parse logs when explicitly enabled via env var
    if os.environ.get("AIRBAND_DEBUG"):
        logger.debug(
            f"parse_controls: {conf_path} gain={gain} snr={squelch_snr} dbfs={squelch_dbfs} mode={mode}"
        )
    return gain, squelch_snr, squelch_dbfs, mode


def write_controls(conf_path: str, gain: float, squelch_mode: str, squelch_snr: float, squelch_dbfs: float) -> bool:
    """Write gain and squelch to configuration."""
    gain_value = float(gain)
    gain = min(GAIN_STEPS, key=lambda g: abs(g - gain_value))
    squelch_mode = (squelch_mode or "dbfs").lower()
    squelch_snr = max(0.0, min(10.0, float(squelch_snr)))
    squelch_dbfs = float(squelch_dbfs)
    # rtl_airband treats 0 dBFS as effectively open; clamp to -1 for "closed"
    if squelch_dbfs > -1.0:
        squelch_dbfs = -1.0

    conf_path = os.path.realpath(conf_path)

    logger.info(
        f"write_controls: {conf_path} gain={gain} mode={squelch_mode} snr={squelch_snr} dbfs={squelch_dbfs}"
    )

    with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    out = []
    changed = False
    saw_snr = False
    saw_dbfs = False
    snr_insert_idx = None
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
            if squelch_mode == "dbfs":
                indent_match = re.match(r'^(\s*)', line)
                indent = indent_match.group(1) if indent_match else ""
                new_line = f"{indent}// squelch_snr_threshold = {squelch_snr:.3f};  # UI_CONTROLLED\n"
            else:
                new_line = f"{m.group(1)}{squelch_snr:.3f}{m.group(3)}\n"
            if new_line != line:
                changed = True
            out.append(new_line)
            saw_snr = True
            snr_insert_idx = len(out)
            continue
        m = RE_SQL_DBFS.match(line)
        if m:
            value = squelch_dbfs if squelch_mode == "dbfs" else 0.0
            value_int = int(round(value))
            new_line = f"{m.group(1)}{value_int}{m.group(3)}\n"
            if new_line != line:
                changed = True
            out.append(new_line)
            saw_dbfs = True
            continue
        out.append(line)

    if not saw_dbfs:
        value = squelch_dbfs if squelch_mode == "dbfs" else 0.0
        value_int = int(round(value))
        indent = "      "
        if snr_insert_idx is not None and snr_insert_idx - 1 < len(out):
            indent_match = re.match(r'^(\s*)', out[snr_insert_idx - 1])
            if indent_match:
                indent = indent_match.group(1)
        new_line = f"{indent}squelch_threshold = {value_int};  # UI_CONTROLLED\n"
        if snr_insert_idx is not None:
            out.insert(snr_insert_idx, new_line)
        else:
            out.append(new_line)
        changed = True

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
        path_to_label = {p["path"]: p["label"] for p in load_profiles_registry()}
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
    """Parse frequencies and labels from config.

    Returns:
      (freqs, labels) where:
        - freqs: list[float]
        - labels: Optional[list[str]] (None when labels block missing)
    """
    m = RE_FREQS_BLOCK.search(text or "")
    if not m:
        raise ValueError("freqs block not found")
    freqs = []
    for tok in re.findall(r'-?\d+(?:\.\d+)?', m.group(2) or ""):
        try:
            freqs.append(float(tok))
        except ValueError:
            continue

    labels = None
    lm = RE_LABELS_BLOCK.search(text or "")
    if lm:
        # Keep labels as raw strings (no unescaping needed for our usage).
        labels = re.findall(r'"([^"]*)"', lm.group(2) or "")
    return freqs, labels


def parse_freqs_text(freqs_text: str):
    """Parse textarea freqs input into (freqs, labels?).

    Format per line:
      - "118.600"
      - "118.600 TOWER"
    If any line has a label, we synthesize labels for all lines (unlabeled lines
    fall back to the frequency string), so lengths always match.
    """
    freqs = []
    raw_labels: List[Optional[str]] = []
    saw_label = False
    for raw_line in (freqs_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        parts = line.split()
        if not parts:
            continue
        try:
            fval = float(parts[0])
        except ValueError as e:
            raise ValueError(f"bad frequency: {parts[0]}") from e
        freqs.append(fval)
        if len(parts) > 1:
            saw_label = True
            raw_labels.append(" ".join(parts[1:]).strip())
        else:
            raw_labels.append(None)

    if not freqs:
        raise ValueError("no frequencies provided")

    if not saw_label:
        return freqs, None

    labels: List[str] = []
    for fval, lab in zip(freqs, raw_labels):
        fstr = f"{fval:.4f}"
        labels.append(lab if lab is not None and lab != "" else fstr)
    return freqs, labels


def _format_list_block(base_indent: str, values: List[str]) -> str:
    if not values:
        return "();"
    # First "(" stays on the same line after "= ".
    lines = ["("]
    for i, v in enumerate(values):
        comma = "," if i < len(values) - 1 else ""
        lines.append(f"{base_indent}  {v}{comma}")
    lines.append(f"{base_indent});")
    return "\n".join(lines)


def replace_freqs_labels(text: str, freqs, labels):
    """Replace freqs/labels blocks in config.

    If labels is None:
      - preserve existing labels only if count matches new freqs count
      - otherwise remove labels block (if present)
    If labels is provided:
      - require len(labels) == len(freqs)
    """
    # Normalize/validate frequencies.
    if not isinstance(freqs, list) or not freqs:
        raise ValueError("freqs must be a non-empty list")
    norm_freqs = []
    for f in freqs:
        try:
            norm_freqs.append(float(f))
        except ValueError as e:
            raise ValueError(f"bad frequency: {f}") from e
    freqs = norm_freqs

    m = RE_FREQS_BLOCK.search(text or "")
    if not m:
        raise ValueError("freqs block not found")
    indent_match = re.match(r'^(\s*)', m.group(1) or "")
    base_indent = indent_match.group(1) if indent_match else ""

    # Determine labels to write.
    existing_labels = None
    lm_existing = RE_LABELS_BLOCK.search(text or "")
    if lm_existing:
        existing_labels = re.findall(r'"([^"]*)"', lm_existing.group(2) or "")
    if labels is None:
        if existing_labels is not None and len(existing_labels) == len(freqs):
            labels_to_write = existing_labels
        else:
            labels_to_write = None
    else:
        if not isinstance(labels, list):
            raise ValueError("labels must be a list")
        if len(labels) != len(freqs):
            raise ValueError("labels must match freqs length")
        labels_to_write = [str(s) for s in labels]

    # Build freqs replacement (one per line).
    freqs_values = [f"{f:.4f}" for f in freqs]
    freqs_block = f"{base_indent}freqs = " + _format_list_block(base_indent, freqs_values)
    out = RE_FREQS_BLOCK.sub(freqs_block, text, count=1)

    # Labels: replace, insert, or remove.
    lm = RE_LABELS_BLOCK.search(out)
    if labels_to_write is None:
        if lm:
            out = RE_LABELS_BLOCK.sub("", out, count=1)
        return out

    labels_values = [json.dumps(s) for s in labels_to_write]  # includes quotes
    labels_block = f"{base_indent}labels = " + _format_list_block(base_indent, labels_values)
    if lm:
        out = RE_LABELS_BLOCK.sub(labels_block, out, count=1)
    else:
        # Insert labels block after freqs block.
        m2 = RE_FREQS_BLOCK.search(out)
        if m2:
            insert_at = m2.end()
            out = out[:insert_at] + "\n\n" + labels_block + out[insert_at:]
        else:
            out = out.rstrip() + "\n\n" + labels_block + "\n"
    return out


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
    """Write frequencies and labels to config atomically; returns True if changed."""
    conf_path = os.path.realpath(conf_path)
    with open(conf_path, "r", encoding="utf-8", errors="ignore") as f:
        original = f.read()
    updated = replace_freqs_labels(original, freqs, labels)
    if updated == original:
        return False
    tmp = conf_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(updated)
    os.replace(tmp, conf_path)
    return True


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
