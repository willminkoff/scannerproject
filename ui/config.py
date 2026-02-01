"""Configuration constants for SprontPi Radio Control UI.

All constants can be overridden via environment variables.
"""
import os
import re

# Server & Port Configuration
UI_PORT = int(os.getenv("UI_PORT", "5050"))

# Paths
CONFIG_SYMLINK = os.getenv("CONFIG_SYMLINK", "/usr/local/etc/rtl_airband.conf")
PROFILES_DIR = os.getenv("PROFILES_DIR", "/usr/local/etc/airband-profiles")
GROUND_CONFIG_PATH = os.getenv("GROUND_CONFIG_PATH", "/usr/local/etc/rtl_airband_ground.conf")
COMBINED_CONFIG_PATH = os.getenv("COMBINED_CONFIG_PATH", "/usr/local/etc/rtl_airband_combined.conf")

# Last Hit Tracking
LAST_HIT_AIRBAND_PATH = os.getenv("LAST_HIT_AIRBAND_PATH", "/run/rtl_airband_last_freq_airband.txt")
LAST_HIT_GROUND_PATH = os.getenv("LAST_HIT_GROUND_PATH", "/run/rtl_airband_last_freq_ground.txt")

# Filter Configuration
FILTER_CONFIG_DIR = os.getenv("FILTER_CONFIG_DIR", "/run")
FILTER_AIRBAND_PATH = os.path.join(FILTER_CONFIG_DIR, "rtl_airband_filter.json")
FILTER_GROUND_PATH = os.path.join(FILTER_CONFIG_DIR, "rtl_airband_ground_filter.json")
FILTER_DEFAULT_CUTOFF = float(os.getenv("FILTER_DEFAULT_CUTOFF", "3500"))  # Hz for low-pass
FILTER_MIN_CUTOFF = float(os.getenv("FILTER_MIN_CUTOFF", "2000"))  # Hz
FILTER_MAX_CUTOFF = float(os.getenv("FILTER_MAX_CUTOFF", "5000"))  # Hz

# Avoids & Logs
AVOIDS_DIR = os.getenv("AVOIDS_DIR", "/home/willminkoff/scannerproject/admin/logs")
DIAGNOSTIC_DIR = AVOIDS_DIR
AVOIDS_PATHS = {
    "airband": os.path.join(AVOIDS_DIR, "airband_avoids.json"),
    "ground": os.path.join(AVOIDS_DIR, "ground_avoids.json"),
}
AVOIDS_SUMMARY_PATHS = {
    "airband": os.path.join(AVOIDS_DIR, "airband_avoids.txt"),
    "ground": os.path.join(AVOIDS_DIR, "ground_avoids.txt"),
}

# Icecast Configuration
ICECAST_PORT = int(os.getenv("ICECAST_PORT", "8000"))
MOUNT_NAME = os.getenv("MOUNT_NAME", "GND.mp3")
ICECAST_STATUS_URL = f"http://127.0.0.1:{ICECAST_PORT}/status-json.xsl"
ICECAST_MOUNT_PATH = f"/{MOUNT_NAME}"
ICECAST_HIT_LOG_PATH = os.getenv("ICECAST_HIT_LOG_PATH", "/run/airband_ui_hitlog.jsonl")
ICECAST_HIT_LOG_LIMIT = int(os.getenv("ICECAST_HIT_LOG_LIMIT", "200"))

# Hold/lock state
HOLD_STATE_PATH = os.getenv("HOLD_STATE_PATH", "/run/airband_ui_hold.json")
TUNE_BACKUP_PATH = os.getenv("TUNE_BACKUP_PATH", "/run/airband_ui_tune_backup.json")

# Systemd Units
UNITS = {
    "rtl": os.getenv("UNIT_RTL", "rtl-airband"),
    "ground": os.getenv("UNIT_GROUND", "rtl-airband-ground"),
    "icecast": os.getenv("UNIT_ICECAST", "icecast2"),
    "keepalive": os.getenv("UNIT_KEEPALIVE", "icecast-keepalive"),
}

# Mixer Configuration
MIXER_NAME = os.getenv("MIXER_NAME", "combined")

# Profile Definitions (id, label, path)
PROFILES = [
    ("airband", "KBNA (Nashville)", os.path.join(PROFILES_DIR, "rtl_airband_airband.conf")),
    ("atl", "KATL (Atlanta)", os.path.join(PROFILES_DIR, "rtl_airband_atl.conf")),
    ("nashville_centers", "Nashville Centers", os.path.join(PROFILES_DIR, "rtl_airband_nashville_centers.conf")),
    ("none_airband", "No Profile", os.path.join(PROFILES_DIR, "rtl_airband_none_airband.conf")),
    ("tower",  "TOWER (118.600)", os.path.join(PROFILES_DIR, "rtl_airband_tower.conf")),
    ("khop",   "KHOP (Campbell)", os.path.join(PROFILES_DIR, "rtl_airband_khop.conf")),
    ("kmqy",   "KMQY (Smyrna)", os.path.join(PROFILES_DIR, "rtl_airband_kmqy.conf")),
    ("tune_atis", "Tune ATIS", os.path.join(PROFILES_DIR, "rtl_airband_tune_atis.conf")),
    ("none_ground", "No Profile", os.path.join(PROFILES_DIR, "rtl_airband_none_ground.conf")),
    ("campbell_ground", "Ft. Campbell", os.path.join(PROFILES_DIR, "rtl_airband_campbell_ground.conf")),
    ("campbell_nfm", "Ft. Campbell NFM", os.path.join(PROFILES_DIR, "rtl_airband_campbell_nfm.conf")),
    ("gmrs",   "GMRS", os.path.join(PROFILES_DIR, "rtl_airband_gmrs.conf")),
    ("mtears", "MTEARS", os.path.join(PROFILES_DIR, "rtl_airband_mtears.conf")),
    ("wx",     "WX (162.550)", os.path.join(PROFILES_DIR, "rtl_airband_wx.conf")),
]

# Regex Patterns
RE_GAIN = re.compile(r'^(\s*gain\s*=\s*)([0-9.]+)(\s*;\s*#\s*UI_CONTROLLED.*)$')
RE_SQL  = re.compile(r'^(\s*squelch_snr_threshold\s*=\s*)(-?[0-9.]+)(\s*;\s*#\s*UI_CONTROLLED.*)$')
RE_SQL_DBFS = re.compile(r'^(\s*squelch_threshold\s*=\s*)\(?\s*(-?[0-9.]+)\s*\)?(\s*;\s*#\s*UI_CONTROLLED.*)$')
RE_AIRBAND = re.compile(r'^\s*airband\s*=\s*(true|false)\s*;\s*$', re.I)
RE_UI_DISABLED = re.compile(r'^\s*ui_disabled\s*=\s*(true|false)\s*;\s*$', re.I)
RE_INDEX = re.compile(r'^(\s*index\s*=\s*)(\d+)(\s*;.*)$')
RE_SERIAL = re.compile(r'^\s*serial\s*=\s*"[^\"]*"\s*;\s*$', re.I)
RE_FREQS_BLOCK = re.compile(r'(^\s*freqs\s*=\s*\()(.*?)(\)\s*;)', re.S | re.M)
RE_LABELS_BLOCK = re.compile(r'(^\s*labels\s*=\s*\()(.*?)(\)\s*;)', re.S | re.M)
RE_ACTIVITY = re.compile(r'Activity on ([0-9]+\.[0-9]+)')
RE_ACTIVITY_TS = re.compile(
    r'^(?P<date>\d{4}-\d{2}-\d{2})[ T](?P<time>\d{2}:\d{2}:\d{2})(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|[A-Z]{2,5})?\s+.*Activity on (?P<freq>[0-9]+\.[0-9]+)'
)

# Control & Calibration
HIT_GAP_RESET_SECONDS = int(os.getenv("HIT_GAP_RESET_SECONDS", "10"))
AIRBAND_MIN_MHZ = float(os.getenv("AIRBAND_MIN_MHZ", "118.0"))
AIRBAND_MAX_MHZ = float(os.getenv("AIRBAND_MAX_MHZ", "136.0"))
GAIN_STEPS = [
    0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7,
    16.6, 19.7, 20.7, 22.9, 25.4, 28.0, 29.7, 32.8, 33.8,
    36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48.0, 49.6,
]

# Action Queueing
APPLY_DEBOUNCE_SEC = float(os.getenv("APPLY_DEBOUNCE_SEC", "0.2"))
