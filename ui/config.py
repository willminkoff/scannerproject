"""Configuration constants for SprontPi Radio Control UI.

All constants can be overridden via environment variables.
"""
import os
import re

# Server & Port Configuration
UI_PORT = int(os.getenv("UI_PORT", "5050"))

# Paths
CONFIG_DIR = os.getenv("CONFIG_DIR", "/usr/local/etc")
CONFIG_SYMLINK = os.getenv("CONFIG_SYMLINK", os.path.join(CONFIG_DIR, "rtl_airband.conf"))
PROFILES_DIR = os.getenv("PROFILES_DIR", "/usr/local/etc/airband-profiles")
PROFILES_REGISTRY_PATH = os.path.join(PROFILES_DIR, "profiles.json")
GROUND_CONFIG_PATH = os.getenv("GROUND_CONFIG_PATH", os.path.join(CONFIG_DIR, "rtl_airband_ground.conf"))
COMBINED_CONFIG_PATH = os.getenv("COMBINED_CONFIG_PATH", os.path.join(CONFIG_DIR, "rtl_airband_combined.conf"))

# HomePatrol DB Builder paths
HPDB_ZIP_PATH = os.getenv("HPDB_ZIP_PATH", "/home/willminkoff/Desktop/HPCOPY.zip")
HPDB_EXTRACT_DIR = os.getenv("HPDB_EXTRACT_DIR", "/home/willminkoff/scanner-db/source")
HPDB_ROOT_PATH = os.getenv("HPDB_ROOT_PATH", os.path.join(HPDB_EXTRACT_DIR, "HPCOPY", "HPDB"))
HPDB_DB_PATH = os.getenv("HPDB_DB_PATH", "/home/willminkoff/scanner-db/homepatrol.db")

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
ICECAST_HOST = os.getenv("ICECAST_HOST", "127.0.0.1").strip() or "127.0.0.1"
MOUNT_NAME = os.getenv("MOUNT_NAME", "GND.mp3").strip().lstrip("/")
ICECAST_STATUS_URL = f"http://{ICECAST_HOST}:{ICECAST_PORT}/status-json.xsl"
ICECAST_HIT_LOG_PATH = os.getenv("ICECAST_HIT_LOG_PATH", "/run/airband_ui_hitlog.jsonl")
ICECAST_HIT_LOG_LIMIT = int(os.getenv("ICECAST_HIT_LOG_LIMIT", "200"))
ICECAST_HIT_MIN_DURATION = int(os.getenv("ICECAST_HIT_MIN_DURATION", "2"))

# Hold/lock state
HOLD_STATE_PATH = os.getenv("HOLD_STATE_PATH", "/run/airband_ui_hold.json")
TUNE_BACKUP_PATH = os.getenv("TUNE_BACKUP_PATH", "/run/airband_ui_tune_backup.json")

# Digital backend configuration
DIGITAL_BACKEND = os.getenv("DIGITAL_BACKEND", "sdrtrunk").strip().lower()
DIGITAL_SERVICE_NAME = os.getenv("DIGITAL_SERVICE_NAME", os.getenv("UNIT_DIGITAL", "scanner-digital"))
DIGITAL_PROFILES_DIR = os.getenv("DIGITAL_PROFILES_DIR", "/etc/scannerproject/digital/profiles")
DIGITAL_ACTIVE_PROFILE_LINK = os.getenv("DIGITAL_ACTIVE_PROFILE_LINK", "/etc/scannerproject/digital/active")
DIGITAL_LOG_PATH = os.getenv("DIGITAL_LOG_PATH", "/var/log/sdrtrunk/sdrtrunk.log")
DIGITAL_PLAYLIST_PATH = os.getenv(
    "DIGITAL_PLAYLIST_PATH",
    os.path.join(os.path.expanduser("~"), "SDRTrunk", "playlist", "default.xml"),
)
DIGITAL_EVENT_LOG_DIR = os.getenv(
    "DIGITAL_EVENT_LOG_DIR",
    os.path.join(os.path.expanduser("~"), "SDRTrunk", "event_logs"),
)
DIGITAL_EVENT_LOG_MODE = os.getenv("DIGITAL_EVENT_LOG_MODE", "auto").strip().lower()
DIGITAL_EVENT_LOG_TAIL_LINES = int(os.getenv("DIGITAL_EVENT_LOG_TAIL_LINES", "500"))
DIGITAL_SCHEDULER_STATE_PATH = os.getenv(
    "DIGITAL_SCHEDULER_STATE_PATH",
    "/run/airband_ui_digital_scheduler.json",
).strip()
AIRBAND_RTL_SERIAL = os.getenv("AIRBAND_RTL_SERIAL", "").strip()
GROUND_RTL_SERIAL = os.getenv("GROUND_RTL_SERIAL", "").strip()
DIGITAL_RTL_DEVICE = os.getenv("DIGITAL_RTL_DEVICE", "").strip()
DIGITAL_RTL_SERIAL = os.getenv("DIGITAL_RTL_SERIAL", "").strip()
DIGITAL_RTL_SERIAL_SECONDARY = os.getenv(
    "DIGITAL_RTL_SERIAL_SECONDARY",
    os.getenv("DIGITAL_RTL_SERIAL_2", ""),
).strip()
DIGITAL_PREFERRED_TUNER = os.getenv("DIGITAL_PREFERRED_TUNER", "").strip()
DIGITAL_FORCE_PREFERRED_TUNER = os.getenv(
    "DIGITAL_FORCE_PREFERRED_TUNER",
    "0",
).strip().lower() in ("1", "true", "yes", "on")
DIGITAL_USE_MULTI_FREQ_SOURCE = os.getenv(
    "DIGITAL_USE_MULTI_FREQ_SOURCE",
    "1",
).strip().lower() in ("1", "true", "yes", "on")
DIGITAL_SOURCE_ROTATION_DELAY_MS = int(os.getenv("DIGITAL_SOURCE_ROTATION_DELAY_MS", "500"))
DIGITAL_SDRTRUNK_STREAM_NAME = os.getenv("DIGITAL_SDRTRUNK_STREAM_NAME", "DIGITAL").strip()
DIGITAL_ATTACH_BROADCAST_CHANNEL = os.getenv(
    "DIGITAL_ATTACH_BROADCAST_CHANNEL",
    "1",
).strip().lower() in ("1", "true", "yes", "on")
DIGITAL_IGNORE_DATA_CALLS = os.getenv(
    "DIGITAL_IGNORE_DATA_CALLS",
    "1",
).strip().lower() in ("1", "true", "yes", "on")
_DIGITAL_SCAN_MODE_RAW = os.getenv("DIGITAL_SCAN_MODE", "single_system").strip().lower()
DIGITAL_SCAN_MODE = (
    _DIGITAL_SCAN_MODE_RAW
    if _DIGITAL_SCAN_MODE_RAW in ("single_system", "timeslice_multi_system")
    else "single_system"
)
DIGITAL_SYSTEM_DWELL_MS = max(1000, int(os.getenv("DIGITAL_SYSTEM_DWELL_MS", "15000")))
DIGITAL_SYSTEM_HANG_MS = max(0, int(os.getenv("DIGITAL_SYSTEM_HANG_MS", "4000")))
DIGITAL_SYSTEM_ORDER = [
    token.strip()
    for token in os.getenv("DIGITAL_SYSTEM_ORDER", "").replace(";", ",").split(",")
    if token.strip()
]
DIGITAL_PAUSE_ON_HIT = os.getenv(
    "DIGITAL_PAUSE_ON_HIT",
    "1",
).strip().lower() in ("1", "true", "yes", "on")
DIGITAL_SUPER_PROFILE_MODE = os.getenv(
    "DIGITAL_SUPER_PROFILE_MODE",
    "1",
).strip().lower() in ("1", "true", "yes", "on")
DIGITAL_RTL_SERIAL_HINT = "DIGITAL_RTL_SERIAL not set; set it to your digital dongle serial"
# Multi-profile loop scheduler
PROFILE_LOOP_STATE_PATH = os.getenv(
    "PROFILE_LOOP_STATE_PATH",
    "/run/airband_ui_profile_loop.json",
).strip()
PROFILE_LOOP_TICK_SEC = max(0.5, float(os.getenv("PROFILE_LOOP_TICK_SEC", "1.0")))
# Legacy mixer envs are intentionally ignored by runtime paths. Keep these
# variables for backward compatibility with older env files.
DIGITAL_MIXER_ENABLED = os.getenv("DIGITAL_MIXER_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
DIGITAL_MIXER_AIRBAND_MOUNT = os.getenv("DIGITAL_MIXER_AIRBAND_MOUNT", "GND-air.mp3").strip().lstrip("/")
DIGITAL_MIXER_DIGITAL_MOUNT = os.getenv("DIGITAL_MIXER_DIGITAL_MOUNT", "DIGITAL.mp3").strip().lstrip("/")
DIGITAL_MIXER_OUTPUT_MOUNT = os.getenv("DIGITAL_MIXER_OUTPUT_MOUNT", "scannerbox.mp3").strip().lstrip("/")
ANALOG_STREAM_MOUNT = MOUNT_NAME
DIGITAL_STREAM_MOUNT = os.getenv(
    "DIGITAL_STREAM_MOUNT",
    DIGITAL_MIXER_DIGITAL_MOUNT or "DIGITAL.mp3",
).strip().lstrip("/") or "DIGITAL.mp3"
PLAYER_MOUNT = os.getenv("PLAYER_MOUNT", "").strip().lstrip("/")
if not PLAYER_MOUNT:
    PLAYER_MOUNT = ANALOG_STREAM_MOUNT
ICECAST_MOUNT_PATH = f"/{PLAYER_MOUNT}"

# V3 Runtime + Preflight
V3_CANONICAL_CONFIG_PATH = os.getenv(
    "V3_CANONICAL_CONFIG_PATH",
    "/etc/scannerproject/v3/canonical_config.json",
).strip()
V3_COMPILED_STATE_PATH = os.getenv(
    "V3_COMPILED_STATE_PATH",
    "/run/airband_ui_v3_compiled_state.json",
).strip()
V3_STRICT_PREFLIGHT = os.getenv(
    "V3_STRICT_PREFLIGHT",
    "1",
).strip().lower() in ("1", "true", "yes", "on")
RTL_MIN_USB_SPEED_MBPS = max(1, int(os.getenv("RTL_MIN_USB_SPEED_MBPS", "480")))

# Systemd Units
UNITS = {
    "rtl": os.getenv("UNIT_RTL", "rtl-airband"),
    "ground": os.getenv("UNIT_GROUND", "rtl-airband-ground"),
    "icecast": os.getenv("UNIT_ICECAST", "icecast2"),
    "keepalive": os.getenv("UNIT_KEEPALIVE", "icecast-keepalive"),
    "ui": os.getenv("UNIT_UI", "airband-ui"),
    "digital": DIGITAL_SERVICE_NAME,
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
ACTION_WAIT_TIMEOUT_SEC = max(1.0, float(os.getenv("ACTION_WAIT_TIMEOUT_SEC", "45")))
