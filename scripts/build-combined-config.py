#!/usr/bin/env python3
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from combined_config import build_combined_config

CONFIG_SYMLINK = "/usr/local/etc/rtl_airband.conf"
GROUND_CONFIG_PATH = "/usr/local/etc/rtl_airband_ground.conf"
COMBINED_CONFIG_PATH = "/usr/local/etc/rtl_airband_combined.conf"
MIXER_NAME = "combined"
ICECAST_MOUNT = os.environ.get("AIR_MOUNT", "")


def read_active_config_path() -> str:
    try:
        return os.path.realpath(CONFIG_SYMLINK)
    except Exception:
        return CONFIG_SYMLINK


def main() -> None:
    airband_path = read_active_config_path()
    ground_path = os.path.realpath(GROUND_CONFIG_PATH)
    combined = build_combined_config(airband_path, ground_path, MIXER_NAME, ICECAST_MOUNT)
    tmp = COMBINED_CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(combined)
    os.replace(tmp, COMBINED_CONFIG_PATH)


if __name__ == "__main__":
    main()
