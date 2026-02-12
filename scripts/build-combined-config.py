#!/usr/bin/env python3
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from combined_config import build_combined_config

CONFIG_SYMLINK = os.getenv("CONFIG_SYMLINK", "/usr/local/etc/rtl_airband.conf")
GROUND_CONFIG_PATH = os.getenv("GROUND_CONFIG_PATH", "/usr/local/etc/rtl_airband_ground.conf")
COMBINED_CONFIG_PATH = os.getenv("COMBINED_CONFIG_PATH", "/usr/local/etc/rtl_airband_combined.conf")
MIXER_NAME = os.getenv("MIXER_NAME", "combined")
DIGITAL_MIXER_ENABLED = os.getenv("DIGITAL_MIXER_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
DIGITAL_MIXER_AIRBAND_MOUNT = os.getenv("DIGITAL_MIXER_AIRBAND_MOUNT", "GND-air.mp3").strip()
AIRBAND_FALLBACK_PROFILE_PATH = os.getenv(
    "AIRBAND_FALLBACK_PROFILE_PATH",
    "/usr/local/etc/airband-profiles/rtl_airband_airband.conf",
)
GROUND_FALLBACK_PROFILE_PATH = os.getenv(
    "GROUND_FALLBACK_PROFILE_PATH",
    "/usr/local/etc/airband-profiles/rtl_airband_wx.conf",
)


def read_active_config_path() -> str:
    try:
        return os.path.realpath(CONFIG_SYMLINK)
    except Exception:
        return CONFIG_SYMLINK


def _existing_file(path: str) -> str:
    try:
        if path and os.path.isfile(path):
            return os.path.realpath(path)
    except Exception:
        pass
    return ""


def resolve_config_path(primary: str, fallback: str) -> str:
    candidates = []
    if primary:
        candidates.append(primary)
    if primary:
        try:
            rp = os.path.realpath(primary)
            if rp and rp not in candidates:
                candidates.append(rp)
        except Exception:
            pass
    if fallback and fallback not in candidates:
        candidates.append(fallback)
    for candidate in candidates:
        resolved = _existing_file(candidate)
        if resolved:
            return resolved
    raise FileNotFoundError(
        f"No readable config file found. primary={primary!r} fallback={fallback!r}"
    )


def main() -> None:
    airband_path = resolve_config_path(read_active_config_path(), AIRBAND_FALLBACK_PROFILE_PATH)
    ground_path = resolve_config_path(GROUND_CONFIG_PATH, GROUND_FALLBACK_PROFILE_PATH)
    mount_override = DIGITAL_MIXER_AIRBAND_MOUNT if DIGITAL_MIXER_ENABLED else ""
    combined = build_combined_config(airband_path, ground_path, MIXER_NAME, mount_name=mount_override)
    out_dir = os.path.dirname(COMBINED_CONFIG_PATH)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tmp = COMBINED_CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(combined)
    os.replace(tmp, COMBINED_CONFIG_PATH)


if __name__ == "__main__":
    main()
