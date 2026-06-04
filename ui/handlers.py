"""HTTP request handlers."""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import threading
import random
import re
import subprocess
import ssl
from datetime import datetime
import queue
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from http.server import BaseHTTPRequestHandler

try:
    from . import ws_spectrum
except ImportError:
    from ui import ws_spectrum

logger = logging.getLogger(__name__)
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

def _resolve_server_timezone() -> str:
    # systemd boxes: /etc/localtime is a symlink to /usr/share/zoneinfo/<IANA>.
    # `timedatectl set-timezone` updates this symlink but does NOT touch
    # /etc/timezone, so the realpath is the canonical source of truth.
    try:
        link = os.path.realpath("/etc/localtime")
        prefix = "/usr/share/zoneinfo/"
        if link.startswith(prefix):
            return link[len(prefix):]
    except OSError:
        pass
    # Fallback for older systems that still maintain /etc/timezone.
    try:
        tz = Path("/etc/timezone").read_text().strip()
        if tz:
            return tz
    except OSError:
        pass
    return "UTC"


_RESOLVED_SERVER_TIMEZONE = _resolve_server_timezone()


def combined_num_devices(conf_path=None) -> int:
    """Count devices declared in the combined rtl_airband config.

    More stable than probing USB at runtime (devices may be busy/in-use).
    """
    try:
        if not conf_path:
            conf_path = COMBINED_CONFIG_PATH
        with open(conf_path, "r") as f:
            txt = f.read()
        return txt.count('serial = "')
    except Exception:
        return 0


def _digital_tuner_targets() -> list[str]:
    assignments_payload = load_dongle_assignments()
    if isinstance(assignments_payload, dict):
        assigned_targets = assigned_digital_tuner_ids(assignments_payload)
        if assigned_targets:
            return assigned_targets

    targets = []
    for candidate in (
        DIGITAL_PREFERRED_TUNER,
        DIGITAL_RTL_SERIAL,
        DIGITAL_RTL_SERIAL_SECONDARY,
        DIGITAL_RTL_SERIAL_TERTIARY,
        DIGITAL_RTL_DEVICE,
    ):
        value = str(candidate or "").strip()
        if value and value not in targets:
            targets.append(value)
    return targets


def _configured_digital_serials() -> list[str]:
    serials = []
    for candidate in (
        DIGITAL_RTL_SERIAL,
        DIGITAL_RTL_SERIAL_SECONDARY,
        DIGITAL_RTL_SERIAL_TERTIARY,
    ):
        value = str(candidate or "").strip()
        if value and value not in serials:
            serials.append(value)
    return serials


def _effective_digital_rtl_serials() -> list[str]:
    assignments_payload = load_dongle_assignments()
    if isinstance(assignments_payload, dict):
        assigned_targets = assigned_digital_tuner_ids(assignments_payload)
        if assigned_targets:
            return assigned_digital_rtl_serials(assignments_payload)
    return _configured_digital_serials()


def _expected_icecast_mounts(
    *,
    analog_active: bool,
    keepalive_active: bool,
    digital_active: bool,
) -> list[str]:
    mounts = []
    analog_mount = "/" + str(PLAYER_MOUNT or "").strip().lstrip("/")
    digital_mount = "/" + str(DIGITAL_STREAM_MOUNT or "").strip().lstrip("/")
    if analog_mount != "/" and (analog_active or keepalive_active):
        mounts.append(analog_mount)
    if digital_mount != "/" and digital_active:
        mounts.append(digital_mount)
    return mounts


try:
    from .config import (
        CONFIG_SYMLINK,
        GROUND_CONFIG_PATH,
        PROFILES_DIR,
        UI_PORT,
        UNITS,
        COMBINED_CONFIG_PATH,
        AIRBAND_RTL_SERIAL,
        GROUND_RTL_SERIAL,
        DIGITAL_BACKEND,
        DIGITAL_PREFERRED_TUNER,
        DIGITAL_RTL_DEVICE,
        DIGITAL_RTL_SERIAL,
        DIGITAL_RTL_SERIAL_SECONDARY,
        DIGITAL_RTL_SERIAL_TERTIARY,
        DIGITAL_RTL_SERIAL_HINT,
        DIGITAL_STREAM_MOUNT,
        DIGITAL_PLAYLIST_PATH,
        DIGITAL_SDRTRUNK_STREAM_NAME,
        DIGITAL_MIXER_ENABLED,
        HEALTH_SCHEDULER_STALE_MS,
        HP_LOCATION_PUSH_LOG_PATH,
        ICECAST_PORT,
        PLAYER_MOUNT,
        RTL_AIRBAND_STATS_PATH,
        RTL_AIRBAND_AIRBAND_STATS_PATH,
        RTL_AIRBAND_GROUND_STATS_PATH,
        RTL_AIRBAND_STATS_STALE_SEC,
        SB3_CONNECTED_STATUS_REFRESH_SEC,
        SB3_CONNECTED_SYSTEM_REFRESH_SEC,
        SB3_CONNECTED_PROFILES_REFRESH_SEC,
        SB3_DEDICATED_DIGITAL_FETCH_ENABLED,
        STREAM_PROXY_TRANSCODE_ANALOG_DEFAULT,
    )
    from .sample_flow import rtl_airband_sample_flow_state, mount_publishing
    from .profile_config import (
        read_active_config_path, parse_controls, split_profiles,
        resolve_controls_path, write_controls,
        guess_current_profile, summarize_avoids, parse_filter,
        load_profiles_registry, find_profile, validate_profile_id, safe_profile_path,
        enforce_profile_index, set_profile, save_profiles_registry, write_airband_flag,
        parse_freqs_labels, parse_freqs_text, write_freqs_labels, write_combined_config
    )
    from .managed_analog_controls import (
        recommended_managed_controls,
        get_band_squelch_auto as _get_band_squelch_auto,
        set_band_squelch_auto as _set_band_squelch_auto,
    )
    from .squelch_tracker import get_tracker_status as _tracker_status
    from .squelch_preset import (
        apply_preset as squelch_apply_preset,
        compute_preset_plan as squelch_compute_preset_plan,
        normalize_preset as squelch_normalize_preset,
        margin_for as squelch_margin_for,
        VALID_PRESETS as SQUELCH_VALID_PRESETS,
        DEFAULT_PRESET as SQUELCH_DEFAULT_PRESET,
    )
    # Phase 4c — chirp UDP JSON client + feature-flag adapter.  Both
    # modules are stdlib-only at import time and dormant when the flag
    # (SB5_USE_GR_DEMOD) is off.  Production state is untouched until
    # the operator flips the flag.
    from .chirp_client import (
        use_gr_demod as _chirp_use_gr_demod,
        get_airband_client as _chirp_airband_client,
        get_ground_client as _chirp_ground_client,
        ChirpClientError as _ChirpClientError,
        ChirpRejected as _ChirpRejected,
        ChirpDaemonDown as _ChirpDaemonDown,
    )
    from . import chirp_adapter as _chirp_adapter
    from .combined_status import combined_device_summary, combined_config_stale
    from .scanner import (
        get_analog_scan_health, read_last_hit_airband, read_last_hit_ground, read_hit_list_cached
    )
    from .icecast import (
        fetch_local_icecast_status,
        list_icecast_mounts,
        extract_icecast_title_for_mount,
    )
    from .systemd import (
        unit_active,
        unit_exists,
        restart_rtl,
        unit_active_enter_epoch,
        set_bt_heal_auto_recovery,
        reboot_host,
        digital_restart_state,
        rtl_restart_state,
        restart_rtl_airband,
        restart_rtl_ground,
        rtl_airband_restart_state,
        rtl_ground_restart_state,
    )
    from .server_workers import enqueue_action, enqueue_apply, get_met_store
    from .diagnostic import write_diagnostic_log
    from .spectrum import get_spectrum_bins, spectrum_to_json, start_spectrum
    from .system_stats import get_system_stats, read_bt_audio_heal_status
    from .zip_lookup import nearest_zip as _nearest_zip
    from .vlc import start_vlc, stop_vlc, vlc_running, vlc_status
    from .digital import (
        get_digital_manager,
        validate_digital_profile_id,
        create_digital_profile_dir,
        delete_digital_profile_dir,
        inspect_digital_profile,
        read_digital_talkgroups,
        write_digital_listen,
    )
    from .dongle_allocator import (
        assigned_digital_rtl_serials,
        assigned_digital_tuner_ids,
        load_assignments as load_dongle_assignments,
    )
    from .dongle_power import get_power_state, power_off, power_on, load_schedule, save_schedule
    from .profile_editor import (
        analog_profile_is_active,
        get_analog_editor_payload,
        get_digital_editor_payload,
        save_analog_editor_payload,
        save_digital_editor_payload,
        validate_analog_editor_payload,
        validate_digital_editor_payload,
    )
    from .hp_state import HPState
    from .hp_favorites_wizard import HPFavoritesWizard
    from .favorites_runtime import (
        get_last_favorites_runtime_sync,
        get_last_runtime_scan_pool,
        sync_scan_pool_to_runtime,
    )
    from .service_types import get_all_service_types, get_default_enabled_service_types
    from .zip_lookup import resolve_postal_to_lat_lon
    from .scan_mode_controller import get_scan_mode_controller
    from .v3_preflight import (
        evaluate_analog_preflight,
        evaluate_digital_preflight,
        gate_action,
    )
    from .v3_runtime import (
        compile_runtime,
        load_compiled_state,
        set_active_analog_profile,
        set_active_digital_profile,
        sync_digital_profiles_from_fs,
        upsert_analog_profile,
        delete_analog_profile,
    )
except ImportError:
    from ui.config import (
        CONFIG_SYMLINK,
        GROUND_CONFIG_PATH,
        PROFILES_DIR,
        UI_PORT,
        UNITS,
        COMBINED_CONFIG_PATH,
        AIRBAND_RTL_SERIAL,
        GROUND_RTL_SERIAL,
        DIGITAL_BACKEND,
        DIGITAL_PREFERRED_TUNER,
        DIGITAL_RTL_DEVICE,
        DIGITAL_RTL_SERIAL,
        DIGITAL_RTL_SERIAL_SECONDARY,
        DIGITAL_RTL_SERIAL_TERTIARY,
        DIGITAL_RTL_SERIAL_HINT,
        DIGITAL_STREAM_MOUNT,
        DIGITAL_PLAYLIST_PATH,
        DIGITAL_SDRTRUNK_STREAM_NAME,
        DIGITAL_MIXER_ENABLED,
        HEALTH_SCHEDULER_STALE_MS,
        HP_LOCATION_PUSH_LOG_PATH,
        ICECAST_PORT,
        PLAYER_MOUNT,
        RTL_AIRBAND_STATS_PATH,
        RTL_AIRBAND_AIRBAND_STATS_PATH,
        RTL_AIRBAND_GROUND_STATS_PATH,
        RTL_AIRBAND_STATS_STALE_SEC,
        SB3_CONNECTED_STATUS_REFRESH_SEC,
        SB3_CONNECTED_SYSTEM_REFRESH_SEC,
        SB3_CONNECTED_PROFILES_REFRESH_SEC,
        SB3_DEDICATED_DIGITAL_FETCH_ENABLED,
        STREAM_PROXY_TRANSCODE_ANALOG_DEFAULT,
    )
    from ui.sample_flow import rtl_airband_sample_flow_state, mount_publishing
    from ui.profile_config import (
        read_active_config_path, parse_controls, split_profiles,
        resolve_controls_path, write_controls,
        guess_current_profile, summarize_avoids, parse_filter,
        load_profiles_registry, find_profile, validate_profile_id, safe_profile_path,
        enforce_profile_index, set_profile, save_profiles_registry, write_airband_flag,
        parse_freqs_labels, parse_freqs_text, write_freqs_labels, write_combined_config
    )
    from ui.managed_analog_controls import (
        recommended_managed_controls,
        get_band_squelch_auto as _get_band_squelch_auto,
        set_band_squelch_auto as _set_band_squelch_auto,
    )
    from ui.squelch_tracker import get_tracker_status as _tracker_status
    from ui.squelch_preset import (
        apply_preset as squelch_apply_preset,
        compute_preset_plan as squelch_compute_preset_plan,
        normalize_preset as squelch_normalize_preset,
        margin_for as squelch_margin_for,
        VALID_PRESETS as SQUELCH_VALID_PRESETS,
        DEFAULT_PRESET as SQUELCH_DEFAULT_PRESET,
    )
    # Phase 4c — chirp UDP JSON client + feature-flag adapter.  Both
    # modules are stdlib-only at import time and dormant when the flag
    # (SB5_USE_GR_DEMOD) is off.  Production state is untouched until
    # the operator flips the flag.
    from ui.chirp_client import (
        use_gr_demod as _chirp_use_gr_demod,
        get_airband_client as _chirp_airband_client,
        get_ground_client as _chirp_ground_client,
        ChirpClientError as _ChirpClientError,
        ChirpRejected as _ChirpRejected,
        ChirpDaemonDown as _ChirpDaemonDown,
    )
    from ui import chirp_adapter as _chirp_adapter
    from ui.combined_status import combined_device_summary, combined_config_stale
    from ui.scanner import (
        get_analog_scan_health, read_last_hit_airband, read_last_hit_ground, read_hit_list_cached
    )
    from ui.icecast import (
        fetch_local_icecast_status,
        list_icecast_mounts,
        extract_icecast_title_for_mount,
    )
    from ui.systemd import (
        unit_active,
        unit_exists,
        restart_rtl,
        unit_active_enter_epoch,
        set_bt_heal_auto_recovery,
        reboot_host,
        digital_restart_state,
        rtl_restart_state,
        restart_rtl_airband,
        restart_rtl_ground,
        rtl_airband_restart_state,
        rtl_ground_restart_state,
    )
    from ui.server_workers import enqueue_action, enqueue_apply, get_met_store
    from ui.diagnostic import write_diagnostic_log
    from ui.spectrum import get_spectrum_bins, spectrum_to_json, start_spectrum
    from ui.system_stats import get_system_stats, read_bt_audio_heal_status
    from ui.vlc import start_vlc, stop_vlc, vlc_running, vlc_status
    from ui.digital import (
        get_digital_manager,
        validate_digital_profile_id,
        create_digital_profile_dir,
        delete_digital_profile_dir,
        inspect_digital_profile,
        read_digital_talkgroups,
        write_digital_listen,
    )
    from ui.dongle_allocator import (
        assigned_digital_rtl_serials,
        assigned_digital_tuner_ids,
        load_assignments as load_dongle_assignments,
    )
    from ui.dongle_power import get_power_state, power_off, power_on, load_schedule, save_schedule
    from ui.profile_editor import (
        analog_profile_is_active,
        get_analog_editor_payload,
        get_digital_editor_payload,
        save_analog_editor_payload,
        save_digital_editor_payload,
        validate_analog_editor_payload,
        validate_digital_editor_payload,
    )
    from ui.hp_state import HPState
    from ui.hp_favorites_wizard import HPFavoritesWizard
    from ui.favorites_runtime import (
        get_last_favorites_runtime_sync,
        get_last_runtime_scan_pool,
        sync_scan_pool_to_runtime,
    )
    from ui.service_types import get_all_service_types, get_default_enabled_service_types
    from ui.zip_lookup import resolve_postal_to_lat_lon
    from ui.scan_mode_controller import get_scan_mode_controller
    from ui.v3_preflight import (
        evaluate_analog_preflight,
        evaluate_digital_preflight,
        gate_action,
    )
    from ui.v3_runtime import (
        compile_runtime,
        load_compiled_state,
        set_active_analog_profile,
        set_active_digital_profile,
        sync_digital_profiles_from_fs,
        upsert_analog_profile,
        delete_analog_profile,
    )


# Digital call-event logs can emit rapid "grant/continue" updates for the same talkgroup.
# Use a wider default coalesce window to align UI hits with perceived audible traffic.
DIGITAL_HIT_COALESCE_SEC = max(0.0, float(os.getenv("DIGITAL_HIT_COALESCE_SEC", "8")))
DIGITAL_HITS_REQUIRE_ACTIVE_STREAM = os.getenv(
    "DIGITAL_HITS_REQUIRE_ACTIVE_STREAM",
    "1",
).strip().lower() in ("1", "true", "yes", "on")
DIGITAL_HITS_REQUIRE_AUDIO_EVENT = os.getenv(
    "DIGITAL_HITS_REQUIRE_AUDIO_EVENT",
    "1",
).strip().lower() in ("1", "true", "yes", "on")
DIGITAL_HITS_REQUIRE_STREAM_ROUTE = os.getenv(
    "DIGITAL_HITS_REQUIRE_STREAM_ROUTE",
    "1",
).strip().lower() in ("1", "true", "yes", "on")
DIGITAL_HIT_RECENT_SEC = max(5.0, float(os.getenv("DIGITAL_HIT_RECENT_SEC", "180")))
DIGITAL_HITS_MIN_VISIBLE = max(0, int(os.getenv("DIGITAL_HITS_MIN_VISIBLE", "3")))
_DIGITAL_IDLE_TITLES = {"", "-", "idle", "n/a", "scanning", "scanning..."}
_DIGITAL_HIT_TGID_RE = re.compile(
    r"\b(?:tgid|talkgroup|tg)\s*[:=#-]?\s*\(?\s*(\d{1,8})\s*\)?",
    re.I,
)
_DIGITAL_STREAM_ROUTE_CACHE: dict[str, object] = {
    "path": "",
    "mtime": 0.0,
    "ts": 0.0,
    "tgids": set(),
}
_ANALOG_LABEL_CACHE: dict[str, dict] = {}
_LOCAL_PROFILES_DIR = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "profiles"))
_NOAA_LABELS_BY_FREQ = {
    "162.5500": "NOAA 1",
    "162.4000": "NOAA 2",
    "162.4750": "NOAA 3",
    "162.4250": "NOAA 4",
    "162.4500": "NOAA 5",
    "162.5000": "NOAA 6",
    "162.5250": "NOAA 7",
}
_NOAA_LABEL_TOLERANCE_MHZ = 0.003
_STATUS_CACHE_TTL_SEC = max(0.1, float(os.getenv("STATUS_CACHE_TTL_SEC", "0.75")))
_HITS_CACHE_TTL_SEC = max(0.1, float(os.getenv("HITS_CACHE_TTL_SEC", "1.0")))
_UNIT_ACTIVE_CACHE_TTL_SEC = max(0.1, float(os.getenv("UNIT_ACTIVE_CACHE_TTL_SEC", "1.0")))
_UNIT_EXISTS_CACHE_TTL_SEC = max(2.0, float(os.getenv("UNIT_EXISTS_CACHE_TTL_SEC", "30")))
# H2 (2026-06-03): cache for `systemctl is-enabled` state so the heartbeat
# can distinguish "intentionally disabled" (ok) from "should be running but
# isn't" (warn). Refresh window matches _UNIT_ACTIVE_CACHE_TTL_SEC since
# the underlying truth changes at roughly the same cadence.
_UNIT_ENABLED_CACHE_TTL_SEC = max(2.0, float(os.getenv("UNIT_ENABLED_CACHE_TTL_SEC", "30")))
HIT_LIST_MAX_AGE_SEC = max(60, int(os.getenv("HIT_LIST_MAX_AGE_SEC", "1800")))
STREAM_PROXY_READ_TIMEOUT_SEC = max(120.0, float(os.getenv("STREAM_PROXY_READ_TIMEOUT_SEC", "600")))
HP_STATE_SYNC_WAIT_SEC = max(0.0, float(os.getenv("HP_STATE_SYNC_WAIT_SEC", "3.0")))
_HP_STATE_SYNC_COND = threading.Condition()
_HP_STATE_SYNC_THREAD: threading.Thread | None = None
_HP_STATE_SYNC_REQUESTED = 0
_HP_STATE_SYNC_COMPLETED = 0
_HP_STATE_SYNC_LAST_PAYLOAD: dict[str, Any] = {"ok": True, "changed": False, "errors": []}
STREAM_PROXY_CHUNK_BYTES = max(128, int(os.getenv("STREAM_PROXY_CHUNK_BYTES", "256")))
try:
    STREAM_PROXY_TRANSCODE_BITRATE_KBPS = int(os.getenv("STREAM_PROXY_TRANSCODE_BITRATE_KBPS", "96"))
except Exception:
    STREAM_PROXY_TRANSCODE_BITRATE_KBPS = 48
STREAM_PROXY_TRANSCODE_BITRATE_KBPS = max(16, min(192, STREAM_PROXY_TRANSCODE_BITRATE_KBPS))
try:
    STREAM_PROXY_TRANSCODE_SAMPLE_RATE_HZ = int(os.getenv("STREAM_PROXY_TRANSCODE_SAMPLE_RATE_HZ", "44100"))
except Exception:
    STREAM_PROXY_TRANSCODE_SAMPLE_RATE_HZ = 22050
STREAM_PROXY_TRANSCODE_SAMPLE_RATE_HZ = max(8000, min(48000, STREAM_PROXY_TRANSCODE_SAMPLE_RATE_HZ))
LATENCY_TONE_DEFAULT_MOUNT = (
    os.getenv("LATENCY_TONE_DEFAULT_MOUNT", "latency-tone.mp3").strip().lstrip("/") or "latency-tone.mp3"
)
LATENCY_TONE_DEFAULT_TARGET = os.getenv("LATENCY_TONE_DEFAULT_TARGET", "analog").strip().lower() or "analog"
LATENCY_TONE_DEFAULT_FREQ_HZ = max(120, int(os.getenv("LATENCY_TONE_DEFAULT_FREQ_HZ", "1000")))
LATENCY_TONE_DEFAULT_DURATION_MS = max(500, int(os.getenv("LATENCY_TONE_DEFAULT_DURATION_MS", "6000")))
LATENCY_TONE_DEFAULT_PREROLL_MS = max(0, int(os.getenv("LATENCY_TONE_DEFAULT_PREROLL_MS", "800")))
LATENCY_TONE_DEFAULT_BITRATE_KBPS = max(8, int(os.getenv("LATENCY_TONE_DEFAULT_BITRATE_KBPS", "32")))
LATENCY_TONE_DEFAULT_SAMPLE_RATE = max(8000, int(os.getenv("LATENCY_TONE_DEFAULT_SAMPLE_RATE", "16000")))
_CACHE_LOCK = threading.Lock()
_STATUS_CACHE: dict[str, object] = {"ts": 0.0, "payload": None}
_HITS_CACHE: dict[str, object] = {"ts": 0.0, "payload": None}
_UNIT_ACTIVE_CACHE: dict[str, tuple[float, bool]] = {}
_UNIT_EXISTS_CACHE: dict[str, tuple[float, bool]] = {}
_UNIT_ENABLED_CACHE: dict[str, tuple[float, str]] = {}

# Sliding-window samples of (NRestarts, monotonic_ts) per unit, used by
# _unit_restart_loop_state() to detect crash-loops behind the
# happens-to-be-active glitch.  Tunables exposed via env vars below.
_UNIT_RESTART_SAMPLES: dict[str, list[tuple[int, float]]] = {}
RTL_RESTART_LOOP_WINDOW_SEC = max(15, int(os.getenv("RTL_RESTART_LOOP_WINDOW_SEC", "60")))
RTL_RESTART_LOOP_THRESHOLD = max(2, int(os.getenv("RTL_RESTART_LOOP_THRESHOLD", "3")))
RTL_RESTART_SAMPLE_RETAIN_SEC = max(
    RTL_RESTART_LOOP_WINDOW_SEC + 30,
    int(os.getenv("RTL_RESTART_SAMPLE_RETAIN_SEC", "180")),
)
_LATENCY_TONE_LOCK = threading.Lock()
_LATENCY_TONE_PROC: subprocess.Popen | None = None
_LATENCY_TONE_STATE: dict[str, Any] = {
    "active": False,
    "pid": 0,
    "mount": LATENCY_TONE_DEFAULT_MOUNT,
    "target": LATENCY_TONE_DEFAULT_TARGET,
    "frequency_hz": LATENCY_TONE_DEFAULT_FREQ_HZ,
    "duration_ms": LATENCY_TONE_DEFAULT_DURATION_MS,
    "pre_roll_ms": LATENCY_TONE_DEFAULT_PREROLL_MS,
    "bitrate_kbps": LATENCY_TONE_DEFAULT_BITRATE_KBPS,
    "sample_rate_hz": LATENCY_TONE_DEFAULT_SAMPLE_RATE,
    "started_at_ms": 0,
    "estimated_tone_start_ms": 0,
    "ended_at_ms": 0,
    "last_error": "",
    "stop_reason": "",
}
_HP_GEOLOOKUP_USER_AGENT = "scannerproject-hp3/1.0"
_HP_GEOLOOKUP_SSL_NO_VERIFY = ssl._create_unverified_context()
_HP_IP_GEOLOOKUP_PROVIDERS = (
    "https://ipapi.co/json/",
    "https://ipwho.is/",
)
_HP_REVERSE_GEOLOOKUP_URL = "https://api.bigdatacloud.net/data/reverse-geocode-client"
try:
    _HP_GEOLOOKUP_TIMEOUT_SEC = max(1.0, float(os.getenv("HP_GEOLOOKUP_TIMEOUT_SEC", "4.0")))
except Exception:
    _HP_GEOLOOKUP_TIMEOUT_SEC = 4.0


def _invalidate_runtime_caches(*names: str) -> None:
    wanted = {str(name or "").strip().lower() for name in names if str(name or "").strip()}
    if not wanted:
        wanted = {"status", "hits"}
    with _CACHE_LOCK:
        if "status" in wanted:
            _STATUS_CACHE["ts"] = 0.0
            _STATUS_CACHE["payload"] = None
        if "hits" in wanted:
            _HITS_CACHE["ts"] = 0.0
            _HITS_CACHE["payload"] = None


def _should_resolve_zip(resolve_zip: bool, use_location: bool) -> bool:
    """Resolve ZIP only when explicitly requested and location scanning is enabled."""
    return bool(resolve_zip) and bool(use_location)


def _parse_bool_value(raw_value, *, field: str) -> bool:
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, (int, float)):
        return bool(raw_value)
    token = str(raw_value or "").strip().lower()
    if token in ("1", "true", "yes", "on"):
        return True
    if token in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"invalid {field}")


def _parse_float_value(raw_value, *, field: str) -> float:
    try:
        return float(str(raw_value).strip())
    except Exception as exc:
        raise ValueError(f"invalid {field}") from exc


def _parse_json_like_list(raw_value) -> list:
    if isinstance(raw_value, list):
        return raw_value
    if isinstance(raw_value, str):
        text = raw_value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return parsed
            return []
        except Exception:
            return [chunk.strip() for chunk in text.split(",") if chunk.strip()]
    return []


def _fetch_json_url_with_tls_fallback(url: str, timeout_sec: float = _HP_GEOLOOKUP_TIMEOUT_SEC) -> dict[str, Any]:
    req = Request(
        str(url),
        headers={
            "User-Agent": _HP_GEOLOOKUP_USER_AGENT,
            "Accept": "application/json",
        },
    )

    def _load(*, context=None) -> dict[str, Any]:
        with urlopen(req, timeout=float(timeout_sec), context=context) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        payload = json.loads(body or "{}")
        if not isinstance(payload, dict):
            raise ValueError("provider returned non-object JSON payload")
        return payload

    try:
        return _load()
    except URLError as exc:
        text = str(exc)
        if "CERTIFICATE_VERIFY_FAILED" in text or "self-signed certificate" in text:
            return _load(context=_HP_GEOLOOKUP_SSL_NO_VERIFY)
        raise


def _parse_geo_float(value) -> float | None:
    try:
        parsed = float(str(value).strip())
    except Exception:
        return None
    if not parsed == parsed:
        return None
    return parsed


def _normalize_ip_geolookup_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("provider returned invalid payload")
    if payload.get("success") is False:
        reason = str(payload.get("message") or "provider reported failure").strip()
        raise ValueError(reason or "provider reported failure")

    lat = _parse_geo_float(payload.get("latitude"))
    if lat is None:
        lat = _parse_geo_float(payload.get("lat"))
    lon = _parse_geo_float(payload.get("longitude"))
    if lon is None:
        lon = _parse_geo_float(payload.get("lon"))
    if lat is None or lon is None or not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        raise ValueError("provider response missing valid coordinates")

    zip_code = str(
        payload.get("postal")
        or payload.get("postcode")
        or payload.get("zip")
        or ""
    ).strip()
    county = str(
        payload.get("county")
        or payload.get("district")
        or payload.get("region")
        or payload.get("region_name")
        or ""
    ).strip()
    return {
        "lat": float(lat),
        "lon": float(lon),
        "zip": zip_code,
        "county": county,
    }


def _normalize_reverse_geolookup_payload(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("reverse-geocode provider returned invalid payload")
    zip_code = str(
        payload.get("postcode")
        or payload.get("postal_code")
        or payload.get("postal")
        or payload.get("zip")
        or ""
    ).strip()
    county = str(payload.get("county") or "").strip()
    if not county:
        locality_info = payload.get("localityInfo") if isinstance(payload.get("localityInfo"), dict) else {}
        administrative = locality_info.get("administrative") if isinstance(locality_info, dict) else []
        if not isinstance(administrative, list):
            administrative = []
        for row in administrative:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            description = str(row.get("description") or "").strip().lower()
            if description.endswith("county") or "county" in description or name.lower().endswith(" county"):
                county = name
                break
    return {
        "zip": zip_code,
        "county": county,
    }


def _resolve_ip_geolocation(timeout_sec: float = _HP_GEOLOOKUP_TIMEOUT_SEC) -> dict[str, Any]:
    errors: list[str] = []
    for provider in _HP_IP_GEOLOOKUP_PROVIDERS:
        try:
            payload = _fetch_json_url_with_tls_fallback(provider, timeout_sec=timeout_sec)
            result = _normalize_ip_geolookup_payload(payload)
            result["provider"] = str(provider)
            return result
        except Exception as exc:
            errors.append(f"{provider}: {exc}")
    summary = "; ".join(errors[:3]) if errors else "no provider configured"
    raise RuntimeError(f"IP geolocation failed ({summary})")


def _resolve_reverse_geolocation(lat: float, lon: float, timeout_sec: float = _HP_GEOLOOKUP_TIMEOUT_SEC) -> dict[str, Any]:
    request_url = (
        f"{_HP_REVERSE_GEOLOOKUP_URL}"
        f"?latitude={quote(f'{float(lat):.8f}')}"
        f"&longitude={quote(f'{float(lon):.8f}')}"
        f"&localityLanguage=en"
    )
    payload = _fetch_json_url_with_tls_fallback(request_url, timeout_sec=timeout_sec)
    result = _normalize_reverse_geolookup_payload(payload)
    result["provider"] = _HP_REVERSE_GEOLOOKUP_URL
    return result


def _extract_scheduler_payload(form: dict[str, Any]) -> dict:
    payload = {}
    for key in (
        "mode",
        "digital_scan_mode",
        "system_dwell_ms",
        "digital_system_dwell_ms",
        "system_hang_ms",
        "digital_system_hang_ms",
        "pause_on_hit",
        "digital_pause_on_hit",
        "system_order",
        "digital_system_order",
        "performance_profile",
        "digital_perf_profile",
        "digital_allocation_perf_profile",
    ):
        if key in form:
            payload[key] = form.get(key)
    return payload


def parse_service_tags(raw_value) -> list[int]:
    candidates: list[Any]
    if isinstance(raw_value, list):
        candidates = raw_value
    elif isinstance(raw_value, (int, float)):
        candidates = [raw_value]
    elif isinstance(raw_value, str):
        text = raw_value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = [chunk.strip() for chunk in text.split(",") if chunk.strip()]
        if isinstance(parsed, list):
            candidates = parsed
        elif parsed is None:
            return []
        else:
            candidates = [parsed]
    else:
        return []
    out: list[int] = []
    seen: set[int] = set()
    for item in candidates:
        try:
            value = int(str(item).strip())
        except Exception:
            continue
        if value <= 0 or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _apply_hp_state_form(
    state: "HPState",
    form: dict[str, Any],
    *,
    resolve_postal_lookup=resolve_postal_to_lat_lon,
    default_service_tags_resolver=get_default_enabled_service_types,
) -> None:
    """Apply incoming hp/state form data to a state object.

    Raises:
      ValueError: when a provided field is invalid.
    """
    if "mode" in form:
        mode = str(form.get("mode") or "").strip().lower()
        if mode not in ("full_database", "favorites"):
            raise ValueError("invalid mode")
        state.mode = mode

    if "use_location" in form:
        state.use_location = _parse_bool_value(form.get("use_location"), field="use_location")

    if "strict_location" in form:
        state.strict_location = _parse_bool_value(
            form.get("strict_location"),
            field="strict_location",
        )

    if "zip" in form or "postal_code" in form:
        state.zip = str(form.get("zip") or form.get("postal_code") or "").strip()

    if "lat" in form:
        state.lat = _parse_float_value(form.get("lat"), field="lat")

    if "lon" in form:
        state.lon = _parse_float_value(form.get("lon"), field="lon")

    if "resolve_zip" in form:
        resolve_zip = _parse_bool_value(form.get("resolve_zip"), field="resolve_zip")
        if _should_resolve_zip(resolve_zip, state.use_location):
            if not str(state.zip or "").strip():
                raise ValueError("missing zip")
            resolved = resolve_postal_lookup(str(state.zip), "US")
            if not resolved:
                raise ValueError("unable to resolve zip")
            state.lat = float(resolved[0])
            state.lon = float(resolved[1])

    if "range_miles" in form:
        state.range_miles = max(
            0.0,
            _parse_float_value(form.get("range_miles"), field="range_miles"),
        )

    if "nationwide_systems" in form:
        state.nationwide_systems = _parse_bool_value(
            form.get("nationwide_systems"),
            field="nationwide_systems",
        )

    if "enabled_service_tags" in form:
        state.enabled_service_tags = parse_service_tags(form.get("enabled_service_tags"))

    if "favorites" in form:
        incoming_favorites = _parse_json_like_list(form.get("favorites"))
        state.favorites = merge_favorites_preserving_custom(state.favorites, incoming_favorites)

    if "favorites_name" in form:
        incoming_name = str(form.get("favorites_name") or "").strip()
        if incoming_name:
            # Only accept a favorites_name that names an *enabled* tile.  Stale
            # browser sessions, multi-tab races, and accidental refreshes have
            # been observed to POST the label of a disabled tile, which then
            # caused the favorites_runtime sync to write that disabled tile's
            # custom_favorites into the active rtl-airband / OP25 profiles.
            # Layer 2 of the defense lives in scan_mode_controller's
            # _resolve_active_favorites_entries.
            enabled_labels = {
                str(f.get("label") or "").strip().lower()
                for f in (state.favorites or [])
                if isinstance(f, dict) and bool(f.get("enabled"))
            }
            if incoming_name.lower() in enabled_labels:
                state.favorites_name = incoming_name
            else:
                logger.warning(
                    "rejecting favorites_name=%r: not an enabled tile (enabled=%s)",
                    incoming_name,
                    sorted(enabled_labels),
                )
                # leave state.favorites_name untouched so the previously-active
                # tile remains the source of truth
        else:
            state.favorites_name = "My Favorites"

    if "custom_favorites" in form:
        state.custom_favorites = _parse_json_like_list(form.get("custom_favorites"))

    if "avoid_list" in form:
        state.avoid_list = _parse_json_like_list(form.get("avoid_list"))

    if not state.enabled_service_tags:
        try:
            state.enabled_service_tags = list(default_service_tags_resolver())
        except Exception:
            state.enabled_service_tags = [2, 3, 4]


def _save_hp_state_with_sync(state: "HPState") -> dict[str, Any]:
    """Persist HP state and run runtime sync, preserving sync error details."""
    state.save()
    request_id = _enqueue_favorites_runtime_sync()
    _wait_for_favorites_runtime_sync(request_id, HP_STATE_SYNC_WAIT_SEC)
    sync_payload = _snapshot_favorites_runtime_sync(request_id)
    return {
        "ok": True,
        "state": state.to_dict(),
        "favorites_runtime_sync": sync_payload,
    }


_TRAVEL_PUSH_ZIP_RE = re.compile(r"^\d{5}$")


def _apply_travel_push(state: "HPState", payload: dict[str, Any]) -> None:
    """Mutate only zip/lat/lon on state from a travel push payload.

    Does NOT touch use_location, strict_location, range_miles, favorites, or service tags.
    Raises ValueError when the payload is malformed.
    """
    zip_raw = payload.get("zip")
    if zip_raw is None:
        raise ValueError("missing zip")
    zip_text = str(zip_raw).strip()
    if not _TRAVEL_PUSH_ZIP_RE.match(zip_text):
        raise ValueError("invalid zip")

    lat_present = "lat" in payload and payload.get("lat") is not None
    lon_present = "lon" in payload and payload.get("lon") is not None
    if lat_present != lon_present:
        raise ValueError("lat and lon must be provided together")

    if lat_present:
        lat = _parse_float_value(payload.get("lat"), field="lat")
        lon = _parse_float_value(payload.get("lon"), field="lon")
        if not (-90.0 <= lat <= 90.0):
            raise ValueError("invalid lat")
        if not (-180.0 <= lon <= 180.0):
            raise ValueError("invalid lon")
        state.lat = lat
        state.lon = lon

    state.zip = zip_text


def _last_travel_push_receipt() -> dict[str, Any] | None:
    """Return the most recent push receipt parsed from the JSONL log, or None."""
    log_path = (HP_LOCATION_PUSH_LOG_PATH or "").strip()
    if not log_path:
        return None
    try:
        path = Path(log_path).expanduser()
        if not path.is_file():
            return None
        last_line = ""
        with path.open("rb") as handle:
            for raw in handle:
                stripped = raw.strip()
                if stripped:
                    last_line = stripped.decode("utf-8", errors="replace")
        if not last_line:
            return None
        record = json.loads(last_line)
        if not isinstance(record, dict):
            return None
        return record
    except Exception:
        logger.debug("travel push log read failed", exc_info=True)
        return None


# PR #35 — Owntracks adapter counters. In-process; surfaced via /api/status.
# A simple dict (no lock) is acceptable here — increments are integer adds,
# and the loss tolerance for a status counter is high.
_OWNTRACKS_STATS: dict[str, Any] = {
    "invocations_total": 0,
    "pushes_accepted_total": 0,
    "pushes_rejected_total": 0,
    "last_push_ts": 0.0,
    "last_lat": None,
    "last_lon": None,
    "last_battery_pct": None,
}


def _log_travel_push(record: dict[str, Any]) -> None:
    """Append a travel-push receipt to the log file and INFO log it.

    The record may include an `accepted` boolean; rejected pushes (travel mode
    off) are logged so Will can see his iPhone is trying to push even when the
    system isn't accepting. Log errors are swallowed: logging must not break
    the request path.
    """
    try:
        logger.info(
            "Travel mode push: accepted=%s zip=%s lat=%s lon=%s source=%s reason=%s",
            record.get("accepted", True),
            record.get("zip"),
            record.get("lat"),
            record.get("lon"),
            record.get("source") or "",
            record.get("reason") or "",
        )
    except Exception:
        pass
    log_path = (HP_LOCATION_PUSH_LOG_PATH or "").strip()
    if not log_path:
        return
    try:
        path = Path(log_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception:
        logger.debug("travel push log write failed for %s", log_path, exc_info=True)


def _normalize_runtime_sync_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        normalized = dict(payload)
    else:
        normalized = {
            "ok": False,
            "changed": False,
            "errors": [f"unexpected runtime sync payload type: {type(payload).__name__}"],
        }
    normalized["ok"] = bool(normalized.get("ok", True))
    normalized["changed"] = bool(normalized.get("changed", False))
    errors = normalized.get("errors")
    if isinstance(errors, list):
        normalized["errors"] = [str(err) for err in errors if str(err).strip()]
    elif errors:
        normalized["errors"] = [str(errors)]
    else:
        normalized["errors"] = []
    return normalized


def _favorites_runtime_sync_worker() -> None:
    global _HP_STATE_SYNC_THREAD
    global _HP_STATE_SYNC_COMPLETED
    global _HP_STATE_SYNC_LAST_PAYLOAD
    while True:
        with _HP_STATE_SYNC_COND:
            if _HP_STATE_SYNC_COMPLETED >= _HP_STATE_SYNC_REQUESTED:
                _HP_STATE_SYNC_THREAD = None
                _HP_STATE_SYNC_COND.notify_all()
                return
            target_request_id = _HP_STATE_SYNC_REQUESTED
        try:
            payload = _normalize_runtime_sync_payload(sync_scan_pool_to_runtime(force=True))
        except Exception as sync_exc:
            payload = {
                "ok": False,
                "changed": False,
                "errors": [str(sync_exc)],
            }
        with _HP_STATE_SYNC_COND:
            _HP_STATE_SYNC_LAST_PAYLOAD = payload
            _HP_STATE_SYNC_COMPLETED = max(_HP_STATE_SYNC_COMPLETED, target_request_id)
            _HP_STATE_SYNC_COND.notify_all()


def _enqueue_favorites_runtime_sync() -> int:
    global _HP_STATE_SYNC_THREAD
    global _HP_STATE_SYNC_REQUESTED
    with _HP_STATE_SYNC_COND:
        _HP_STATE_SYNC_REQUESTED += 1
        request_id = _HP_STATE_SYNC_REQUESTED
        thread = _HP_STATE_SYNC_THREAD
        if thread is None or not thread.is_alive():
            thread = threading.Thread(
                target=_favorites_runtime_sync_worker,
                name="favorites-runtime-sync",
                daemon=True,
            )
            _HP_STATE_SYNC_THREAD = thread
            thread.start()
        _HP_STATE_SYNC_COND.notify_all()
        return request_id


def _wait_for_favorites_runtime_sync(request_id: int, timeout_sec: float) -> bool:
    if timeout_sec <= 0:
        return False
    deadline = time.monotonic() + float(timeout_sec)
    with _HP_STATE_SYNC_COND:
        while _HP_STATE_SYNC_COMPLETED < request_id:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _HP_STATE_SYNC_COND.wait(timeout=remaining)
        return True


def _snapshot_favorites_runtime_sync(request_id: int) -> dict[str, Any]:
    with _HP_STATE_SYNC_COND:
        payload = _normalize_runtime_sync_payload(_HP_STATE_SYNC_LAST_PAYLOAD)
        completed_for_request = _HP_STATE_SYNC_COMPLETED >= request_id
        backlog = max(0, _HP_STATE_SYNC_REQUESTED - _HP_STATE_SYNC_COMPLETED)
    payload.update(
        {
            "request_id": int(request_id),
            "request_complete": bool(completed_for_request),
            "pending": bool(not completed_for_request),
            "backlog": int(backlog),
        }
    )
    return payload


def merge_favorites_preserving_custom(existing_rows, incoming_rows) -> list:
    """Merge incoming favorites while retaining existing custom_favorites metadata."""
    existing_by_id: dict[str, dict] = {}
    existing_by_label: dict[str, dict] = {}
    for item in existing_rows if isinstance(existing_rows, list) else []:
        if not isinstance(item, dict):
            continue
        row_id = str(item.get("id") or "").strip().lower()
        if row_id and row_id not in existing_by_id:
            existing_by_id[row_id] = item
        row_label = str(item.get("label") or item.get("name") or "").strip().lower()
        if row_label and row_label not in existing_by_label:
            existing_by_label[row_label] = item

    merged: list = []
    for item in incoming_rows if isinstance(incoming_rows, list) else []:
        if not isinstance(item, dict):
            merged.append(item)
            continue
        row = dict(item)
        if "custom_favorites" not in row:
            row_id = str(row.get("id") or "").strip().lower()
            row_label = str(row.get("label") or row.get("name") or "").strip().lower()
            existing = existing_by_id.get(row_id) or existing_by_label.get(row_label)
            if isinstance(existing, dict) and "custom_favorites" in existing:
                row["custom_favorites"] = existing.get("custom_favorites")
        merged.append(row)
    return merged


def _read_effective_analog_controls() -> dict[str, Any]:
    """Read analog controls from effective runtime source profiles."""
    controls_airband_path = resolve_controls_path("airband")
    controls_ground_path = resolve_controls_path("ground")
    airband_gain, _airband_snr, airband_dbfs, airband_mode = parse_controls(controls_airband_path)
    ground_gain, _ground_snr, ground_dbfs, ground_mode = parse_controls(controls_ground_path)
    return {
        "controls_airband_path": controls_airband_path,
        "controls_ground_path": controls_ground_path,
        "airband_gain": airband_gain,
        "airband_dbfs": airband_dbfs,
        "airband_mode": airband_mode,
        "ground_gain": ground_gain,
        "ground_dbfs": ground_dbfs,
        "ground_mode": ground_mode,
    }


def _short_label(text: str, max_len: int = 48) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    if len(raw) <= max_len:
        return raw
    return raw[: max_len - 1].rstrip() + "…"


def _safe_int(value) -> int | None:
    try:
        return int(str(value).strip())
    except Exception:
        return None


def _safe_float(value) -> float | None:
    try:
        parsed = float(str(value).strip())
    except Exception:
        return None
    if not (parsed == parsed) or parsed in (float("inf"), float("-inf")):
        return None
    return parsed


def _bounded_int(raw_value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(float(str(raw_value).strip()))
    except Exception:
        parsed = int(default)
    if parsed < minimum:
        return int(minimum)
    if parsed > maximum:
        return int(maximum)
    return int(parsed)


def _sanitize_simple_mount_name(raw_name: str) -> str:
    mount = unquote(str(raw_name or "")).strip().lstrip("/")
    if not mount:
        return ""
    if "/" in mount or "\\" in mount:
        return ""
    for ch in mount:
        if not (ch.isalnum() or ch in "._-"):
            return ""
    return mount


def _latency_tone_reap_locked(now_ms: int) -> None:
    global _LATENCY_TONE_PROC
    proc = _LATENCY_TONE_PROC
    if proc is None:
        return
    if proc.poll() is None:
        return
    _LATENCY_TONE_PROC = None
    _LATENCY_TONE_STATE["active"] = False
    _LATENCY_TONE_STATE["pid"] = 0
    if not int(_LATENCY_TONE_STATE.get("ended_at_ms") or 0):
        _LATENCY_TONE_STATE["ended_at_ms"] = int(now_ms)


def _latency_tone_status_payload_locked(now_ms: int | None = None) -> dict[str, Any]:
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    _latency_tone_reap_locked(int(now_ms))
    payload = {
        "active": bool(_LATENCY_TONE_STATE.get("active")),
        "pid": int(_LATENCY_TONE_STATE.get("pid") or 0),
        "mount": str(_LATENCY_TONE_STATE.get("mount") or ""),
        "target": str(_LATENCY_TONE_STATE.get("target") or "analog"),
        "frequency_hz": int(_LATENCY_TONE_STATE.get("frequency_hz") or 0),
        "duration_ms": int(_LATENCY_TONE_STATE.get("duration_ms") or 0),
        "pre_roll_ms": int(_LATENCY_TONE_STATE.get("pre_roll_ms") or 0),
        "bitrate_kbps": int(_LATENCY_TONE_STATE.get("bitrate_kbps") or 0),
        "sample_rate_hz": int(_LATENCY_TONE_STATE.get("sample_rate_hz") or 0),
        "started_at_ms": int(_LATENCY_TONE_STATE.get("started_at_ms") or 0),
        "estimated_tone_start_ms": int(_LATENCY_TONE_STATE.get("estimated_tone_start_ms") or 0),
        "ended_at_ms": int(_LATENCY_TONE_STATE.get("ended_at_ms") or 0),
        "last_error": str(_LATENCY_TONE_STATE.get("last_error") or ""),
        "stop_reason": str(_LATENCY_TONE_STATE.get("stop_reason") or ""),
    }
    mount = str(payload.get("mount") or "").strip().lstrip("/")
    payload["stream_path"] = f"/stream/{mount}" if mount else "/stream/"
    return payload


def _latency_tone_status_payload() -> dict[str, Any]:
    with _LATENCY_TONE_LOCK:
        return _latency_tone_status_payload_locked()


def _latency_tone_stop_locked(reason: str = "manual_stop") -> dict[str, Any]:
    global _LATENCY_TONE_PROC
    now_ms = int(time.time() * 1000)
    proc = _LATENCY_TONE_PROC
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()
    _LATENCY_TONE_PROC = None
    _LATENCY_TONE_STATE["active"] = False
    _LATENCY_TONE_STATE["pid"] = 0
    _LATENCY_TONE_STATE["ended_at_ms"] = int(now_ms)
    _LATENCY_TONE_STATE["stop_reason"] = str(reason or "manual_stop")
    return _latency_tone_status_payload_locked(now_ms)


def _start_latency_tone_injection(
    *,
    target: str,
    mount: str,
    frequency_hz: int,
    duration_ms: int,
    pre_roll_ms: int,
    bitrate_kbps: int,
    sample_rate_hz: int,
) -> tuple[bool, str, dict[str, Any]]:
    global _LATENCY_TONE_PROC

    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        with _LATENCY_TONE_LOCK:
            _LATENCY_TONE_STATE["last_error"] = "ffmpeg not found in PATH"
            status = _latency_tone_status_payload_locked()
        return False, "ffmpeg not found in PATH", status

    safe_mount = _sanitize_simple_mount_name(mount) or _sanitize_simple_mount_name(LATENCY_TONE_DEFAULT_MOUNT)
    if not safe_mount:
        safe_mount = "latency-tone.mp3"
    safe_target = "digital" if str(target or "").strip().lower() == "digital" else "analog"
    freq_hz = _bounded_int(frequency_hz, LATENCY_TONE_DEFAULT_FREQ_HZ, 120, 5000)
    total_ms = _bounded_int(duration_ms, LATENCY_TONE_DEFAULT_DURATION_MS, 500, 30000)
    lead_ms = _bounded_int(pre_roll_ms, LATENCY_TONE_DEFAULT_PREROLL_MS, 0, min(5000, max(0, total_ms - 100)))
    kbps = _bounded_int(bitrate_kbps, LATENCY_TONE_DEFAULT_BITRATE_KBPS, 8, 128)
    rate_hz = _bounded_int(sample_rate_hz, LATENCY_TONE_DEFAULT_SAMPLE_RATE, 8000, 48000)
    total_sec = total_ms / 1000.0
    lead_sec = lead_ms / 1000.0

    icecast_user = quote(str(os.getenv("ICECAST_SOURCE_USER", "source") or "source"), safe="")
    icecast_password = quote(str(os.getenv("ICECAST_SOURCE_PASSWORD", "062352") or "062352"), safe="")
    source_url = f"icecast://{icecast_user}:{icecast_password}@127.0.0.1:{ICECAST_PORT}/{safe_mount}"

    # Emit silence for pre-roll, then tone, so UI can measure from a known onset.
    gate_expr = f"volume='if(lt(t,{lead_sec:.3f}),0,0.92)'"
    cmd = [
        ffmpeg_bin,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-re",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={freq_hz}:sample_rate={rate_hz}",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(rate_hz),
        "-af",
        gate_expr,
        "-t",
        f"{total_sec:.3f}",
        "-c:a",
        "libmp3lame",
        "-b:a",
        f"{kbps}k",
        "-write_xing",
        "0",
        "-flush_packets",
        "1",
        "-content_type",
        "audio/mpeg",
        "-legacy_icecast",
        "1",
        "-f",
        "mp3",
        source_url,
    ]

    with _LATENCY_TONE_LOCK:
        now_ms = int(time.time() * 1000)
        _latency_tone_reap_locked(now_ms)
        if _LATENCY_TONE_PROC is not None and _LATENCY_TONE_PROC.poll() is None:
            _latency_tone_stop_locked("replaced_by_new_injection")
        try:
            _LATENCY_TONE_PROC = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            _LATENCY_TONE_STATE["last_error"] = str(exc)
            status = _latency_tone_status_payload_locked()
            return False, str(exc), status

        started_ms = int(time.time() * 1000)
        _LATENCY_TONE_STATE.update(
            {
                "active": True,
                "pid": int(_LATENCY_TONE_PROC.pid or 0),
                "mount": safe_mount,
                "target": safe_target,
                "frequency_hz": freq_hz,
                "duration_ms": total_ms,
                "pre_roll_ms": lead_ms,
                "bitrate_kbps": kbps,
                "sample_rate_hz": rate_hz,
                "started_at_ms": started_ms,
                "estimated_tone_start_ms": started_ms + lead_ms,
                "ended_at_ms": 0,
                "last_error": "",
                "stop_reason": "",
            }
        )
        if _LATENCY_TONE_PROC.poll() is not None:
            rc = int(_LATENCY_TONE_PROC.returncode or 0)
            _LATENCY_TONE_PROC = None
            _LATENCY_TONE_STATE["active"] = False
            _LATENCY_TONE_STATE["pid"] = 0
            _LATENCY_TONE_STATE["ended_at_ms"] = int(time.time() * 1000)
            _LATENCY_TONE_STATE["last_error"] = f"ffmpeg exited rc={rc}"
            status = _latency_tone_status_payload_locked()
            return False, _LATENCY_TONE_STATE["last_error"], status
        status = _latency_tone_status_payload_locked(started_ms)
    return True, "", status


def _flatten_hp_scan_pool_for_preview(pool: dict[str, Any], *, limit: int = 4000) -> dict[str, Any]:
    payload = pool if isinstance(pool, dict) else {}
    trunked_sites = payload.get("trunked_sites") if isinstance(payload.get("trunked_sites"), list) else []
    conventional = payload.get("conventional") if isinstance(payload.get("conventional"), list) else []

    trunked_entries: list[dict[str, Any]] = []
    conventional_entries: list[dict[str, Any]] = []
    seen_trunked: set[str] = set()
    seen_conventional: set[str] = set()
    trunked_talkgroups = 0
    conventional_channels = 0

    for site in trunked_sites:
        if not isinstance(site, dict):
            continue
        system_id = _safe_int(site.get("system_id")) or 0
        system_name = str(site.get("system_name") or "").strip()
        site_name = str(site.get("site_name") or "").strip()
        default_department = str(site.get("department_name") or "").strip() or site_name or system_name
        talkgroups = site.get("talkgroups") if isinstance(site.get("talkgroups"), list) else []
        labels_map = site.get("talkgroup_labels") if isinstance(site.get("talkgroup_labels"), dict) else {}
        groups_map = site.get("talkgroup_groups") if isinstance(site.get("talkgroup_groups"), dict) else {}
        for raw_tgid in talkgroups:
            tgid = _safe_int(raw_tgid)
            if tgid is None or tgid <= 0:
                continue
            key_base = str(system_id) if system_id > 0 else (system_name.lower() or site_name.lower() or "unknown")
            dedupe_key = f"{key_base}:{tgid}"
            if dedupe_key in seen_trunked:
                continue
            seen_trunked.add(dedupe_key)
            trunked_talkgroups += 1
            tgid_text = str(tgid)
            alpha_tag = str(labels_map.get(tgid_text) or "").strip()
            department_name = str(groups_map.get(tgid_text) or "").strip() or default_department
            trunked_entries.append(
                {
                    "id": f"fulldb-trunked-{key_base}-{tgid}",
                    "kind": "trunked",
                    "system_id": int(system_id),
                    "system_key": "",
                    "system_name": system_name,
                    "department_name": department_name,
                    "alpha_tag": alpha_tag or f"TG {tgid}",
                    "service_tag": 0,
                    "talkgroup": tgid_text,
                    "control_channels": [],
                    "frequency": 0.0,
                }
            )

    for row in conventional:
        if not isinstance(row, dict):
            continue
        frequency = _safe_float(row.get("frequency"))
        if frequency is None or frequency <= 0:
            continue
        rounded_frequency = round(float(frequency), 6)
        alpha_tag = str(row.get("alpha_tag") or "").strip()
        service_tag = _safe_int(row.get("service_tag")) or 0
        system_name = str(row.get("system_name") or "").strip()
        system_key = str(row.get("system_key") or "").strip()
        dedupe_key = f"{rounded_frequency:.6f}:{service_tag}:{alpha_tag.lower()}:{system_name.lower()}:{system_key.lower()}"
        if dedupe_key in seen_conventional:
            continue
        seen_conventional.add(dedupe_key)
        conventional_channels += 1
        conventional_entries.append(
            {
                "id": f"fulldb-conv-{rounded_frequency:.6f}-{service_tag}-{len(conventional_entries) + 1}",
                "kind": "conventional",
                "system_id": 0,
                "system_key": system_key,
                "system_name": system_name,
                "department_name": "",
                "alpha_tag": alpha_tag,
                "service_tag": int(service_tag),
                "talkgroup": "",
                "control_channels": [],
                "frequency": rounded_frequency,
            }
        )

    trunked_entries.sort(
        key=lambda item: (
            str(item.get("system_name") or "").lower(),
            str(item.get("department_name") or "").lower(),
            _safe_int(item.get("talkgroup")) or 0,
        )
    )
    conventional_entries.sort(
        key=lambda item: (
            float(item.get("frequency") or 0.0),
            int(item.get("service_tag") or 0),
            str(item.get("alpha_tag") or "").lower(),
            str(item.get("system_name") or "").lower(),
        )
    )

    combined_entries = [*trunked_entries, *conventional_entries]
    total_entries = len(combined_entries)
    safe_limit = max(100, min(int(limit or 4000), 20000))
    truncated = total_entries > safe_limit
    if truncated:
        combined_entries = combined_entries[:safe_limit]

    return {
        "entries": combined_entries,
        "total_entries": total_entries,
        "trunked_sites": len([row for row in trunked_sites if isinstance(row, dict)]),
        "trunked_talkgroups": trunked_talkgroups,
        "conventional_channels": conventional_channels,
        "truncated": truncated,
    }


def _icecast_sources(status_text: str) -> list[dict]:
    try:
        data = json.loads(status_text)
    except Exception:
        return []
    sources = data.get("icestats", {}).get("source")
    if not sources:
        return []
    if not isinstance(sources, list):
        sources = [sources]
    out = []
    for source in sources:
        listenurl = str(source.get("listenurl") or "").strip()
        mount = ""
        if listenurl:
            mount = listenurl.rsplit("/", 1)[-1].strip()
        if not mount:
            mount = str(source.get("mount") or "").strip().lstrip("/")
        out.append({
            "mount": mount,
            "audio_info": str(source.get("audio_info") or "").strip(),
            "server_type": str(source.get("server_type") or "").strip(),
            "stream_start": str(source.get("stream_start") or "").strip(),
            "server_name": str(source.get("server_name") or "").strip(),
        })
    return out


def _is_live_analog_source(source: dict) -> bool:
    if not source:
        return False
    mount = str(source.get("mount") or "").strip().lower()
    if not mount:
        return False
    if "digital" in mount or "keepalive" in mount:
        return False
    return _source_has_audio_metadata(source)


def _source_has_audio_metadata(source: dict) -> bool:
    if not source:
        return False
    if str(source.get("audio_info") or "").strip():
        return True
    if str(source.get("server_type") or "").strip():
        return True
    if str(source.get("stream_start") or "").strip():
        return True
    if str(source.get("server_name") or "").strip():
        return True
    return False


def _resolve_analog_stream_mount(status_text: str) -> str:
    configured = str(PLAYER_MOUNT or "").strip().lstrip("/")
    sources = _icecast_sources(status_text)
    if not sources:
        return configured
    by_mount = {
        str(row.get("mount") or "").strip(): row
        for row in sources
        if str(row.get("mount") or "").strip()
    }
    configured_row = by_mount.get(configured)
    if _is_live_analog_source(configured_row):
        return configured
    for row in sources:
        if _is_live_analog_source(row):
            mount = str(row.get("mount") or "").strip()
            if mount:
                return mount
    return configured


def _resolve_digital_stream_mount(status_text: str) -> str:
    configured = str(DIGITAL_STREAM_MOUNT or "").strip().lstrip("/")
    sources = _icecast_sources(status_text)
    if not sources:
        return configured
    by_mount = {
        str(row.get("mount") or "").strip(): row
        for row in sources
        if str(row.get("mount") or "").strip()
    }
    if configured and configured in by_mount:
        return configured
    for row in sources:
        mount = str(row.get("mount") or "").strip()
        if not mount:
            continue
        mount_l = mount.lower()
        if "digital" not in mount_l:
            continue
        if _source_has_audio_metadata(row):
            return mount
    for row in sources:
        mount = str(row.get("mount") or "").strip()
        if not mount:
            continue
        if "digital" in mount.lower():
            return mount
    return configured


def _normalize_freq_key(value) -> str:
    try:
        return f"{float(str(value).strip()):.4f}"
    except Exception:
        return ""


def _fallback_noaa_label(freq_text: str) -> str:
    key = _normalize_freq_key(freq_text)
    if key and key in _NOAA_LABELS_BY_FREQ:
        return _NOAA_LABELS_BY_FREQ[key]
    try:
        freq_num = float(str(freq_text or "").strip())
    except Exception:
        return ""
    for freq_key, label in _NOAA_LABELS_BY_FREQ.items():
        try:
            known = float(freq_key)
        except Exception:
            continue
        if abs(freq_num - known) <= _NOAA_LABEL_TOLERANCE_MHZ:
            return label
    return ""


def _load_profile_label_map(conf_path: str) -> dict[str, str]:
    path = os.path.realpath(str(conf_path or ""))
    if not path or not os.path.isfile(path):
        return {}
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        return {}

    cached = _ANALOG_LABEL_CACHE.get(path)
    if cached and cached.get("mtime") == mtime:
        return dict(cached.get("map") or {})

    mapping: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        freqs, labels = parse_freqs_labels(text)
    except Exception:
        freqs, labels = [], None

    if labels and len(labels) == len(freqs):
        for freq, label in zip(freqs, labels):
            key = _normalize_freq_key(freq)
            clean = str(label or "").strip()
            if key and clean:
                mapping[key] = clean

    _ANALOG_LABEL_CACHE[path] = {"mtime": mtime, "map": mapping}
    return dict(mapping)


def _resolve_analog_label_map(conf_path: str, profile_id: str, profile_rows: list[dict]) -> dict[str, str]:
    mapping = _load_profile_label_map(conf_path)
    if mapping:
        return mapping
    basename = os.path.basename(str(conf_path or "").strip())
    if basename:
        candidate = os.path.realpath(os.path.join(PROFILES_DIR, basename))
        fallback = _load_profile_label_map(candidate)
        if fallback:
            return fallback
        local_candidate = os.path.realpath(os.path.join(_LOCAL_PROFILES_DIR, basename))
        fallback = _load_profile_label_map(local_candidate)
        if fallback:
            return fallback
    pid = str(profile_id or "").strip()
    if not pid:
        return mapping
    for row in profile_rows or []:
        if str(row.get("id") or "").strip() != pid:
            continue
        path = str(row.get("path") or "").strip()
        if not path:
            continue
        fallback = _load_profile_label_map(path)
        if fallback:
            return fallback
    # Active profile can be a minimalist "none_*" config (no labels). In that
    # case, recover labels by frequency from the full profile catalog.
    merged: dict[str, str] = {}
    for row in profile_rows or []:
        path = str((row or {}).get("path") or "").strip()
        if not path:
            continue
        row_map = _load_profile_label_map(path)
        for key, value in row_map.items():
            if key and value and key not in merged:
                merged[key] = value
    if merged:
        return merged
    return mapping


def _infer_analog_source(freq_text: str) -> str:
    try:
        num = float(str(freq_text or "").strip())
    except Exception:
        return "analog"
    if 118.0 <= num <= 136.991:
        return "airband"
    return "ground"


def _lookup_analog_label(
    freq_text: str,
    source: str,
    airband_labels: dict[str, str],
    ground_labels: dict[str, str],
) -> str:
    key = _normalize_freq_key(freq_text)
    if not key:
        return ""

    if source == "airband":
        label = airband_labels.get(key, "")
    elif source == "ground":
        label = ground_labels.get(key, "")
    else:
        label = ""

    if not label:
        label = airband_labels.get(key, "") or ground_labels.get(key, "")
    if not label:
        label = _fallback_noaa_label(freq_text)
    return str(label or "").strip()


def _annotate_analog_hits(items: list[dict], airband_labels: dict[str, str], ground_labels: dict[str, str]) -> list[dict]:
    out = []
    for item in items or []:
        row = dict(item or {})
        source = _infer_analog_source(row.get("freq"))
        row["source"] = source
        row["type"] = source
        label_full = _lookup_analog_label(row.get("freq"), source, airband_labels, ground_labels)
        if label_full:
            row["label_full"] = label_full
            row["label"] = _short_label(label_full, max_len=48)
        out.append(row)
    return out


def _latest_hit_item(items: list[dict], source: str) -> dict[str, Any]:
    normalized = str(source or "").strip().lower()
    for item in items or []:
        row = dict(item or {})
        item_source = str(row.get("source") or row.get("type") or "").strip().lower()
        if item_source == normalized:
            return row
    return {}


def _digital_status_with_hit_aliases(payload: dict[str, Any], hit_items: list[dict]) -> dict[str, Any]:
    out = dict(payload or {})
    latest = _latest_hit_item(hit_items, "digital")
    latest_label = str(
        latest.get("label_full")
        or latest.get("label")
        or latest.get("freq")
        or out.get("digital_last_label")
        or ""
    ).strip()
    latest_tgid = str(latest.get("tgid") or out.get("digital_last_tgid") or "").strip()
    latest_ts = 0.0
    try:
        latest_ts = float(latest.get("ts") or 0.0)
    except Exception:
        latest_ts = 0.0
    latest_time_ms = int(round(latest_ts * 1000.0)) if latest_ts > 0 else int(out.get("digital_last_time") or 0)
    if latest_label:
        out["digital_last_label"] = latest_label
    if latest_tgid:
        out["digital_last_tgid"] = latest_tgid
    out["digital_last_time"] = int(latest_time_ms or 0)
    out["last_hit_digital"] = latest_tgid or latest_label
    out["last_hit_digital_label"] = _short_label(latest_label, max_len=48) if latest_label else ""
    out["last_hit_digital_time"] = int(latest_time_ms or 0)
    return out


def _digital_has_recent_event(max_age_sec: float = DIGITAL_HIT_RECENT_SEC) -> bool:
    """Fallback activity signal when Icecast title stays idle."""
    try:
        event = get_digital_manager().getLastEvent() or {}
        time_ms = int(event.get("timeMs") or 0)
    except Exception:
        return False
    if time_ms <= 0:
        return False
    return (int(time.time() * 1000) - time_ms) <= int(max_age_sec * 1000)


def _digital_stream_active_for_hits() -> bool:
    """Treat digital events as active via mount title, with recent-event fallback."""
    if not DIGITAL_STREAM_MOUNT:
        return True
    status_text = fetch_local_icecast_status()
    if status_text and not status_text.startswith("ERROR:"):
        mount = _resolve_digital_stream_mount(status_text) or str(DIGITAL_STREAM_MOUNT or "").strip().lstrip("/")
        title = extract_icecast_title_for_mount(status_text, f"/{mount}")
        if title.strip().lower() not in _DIGITAL_IDLE_TITLES:
            return True
    return _digital_has_recent_event()


def _digital_event_is_audible_hit(event: dict) -> bool:
    if not isinstance(event, dict):
        return False
    if bool(event.get("muted")):
        return False
    label = str(event.get("label") or "").strip()
    tgid = str(event.get("tgid") or "").strip()
    if not label and not tgid:
        return False

    duration_ms = _safe_int(event.get("durationMs")) or 0
    if duration_ms > 0:
        return True

    mode = str(event.get("mode") or "").strip()
    frequency = str(event.get("frequency") or "").strip()
    if mode and (tgid or frequency):
        return True
    return False


def _digital_stream_routed_tgids_for_hits() -> set[str]:
    if not DIGITAL_HITS_REQUIRE_STREAM_ROUTE:
        return set()
    playlist_path = str(DIGITAL_PLAYLIST_PATH or "").strip()
    if not playlist_path or not os.path.isfile(playlist_path):
        return set()

    try:
        mtime = float(os.path.getmtime(playlist_path))
    except Exception:
        return set()
    now_mono = time.monotonic()
    with _CACHE_LOCK:
        cached_path = str(_DIGITAL_STREAM_ROUTE_CACHE.get("path") or "")
        cached_mtime = float(_DIGITAL_STREAM_ROUTE_CACHE.get("mtime") or 0.0)
        cached_ts = float(_DIGITAL_STREAM_ROUTE_CACHE.get("ts") or 0.0)
        if (
            cached_path == playlist_path
            and cached_mtime == mtime
            and (now_mono - cached_ts) <= 2.0
        ):
            cached = _DIGITAL_STREAM_ROUTE_CACHE.get("tgids")
            if isinstance(cached, set):
                return set(cached)

    try:
        root = ET.parse(playlist_path).getroot()
    except Exception:
        return set()

    stream_name = str(DIGITAL_SDRTRUNK_STREAM_NAME or "").strip()
    if not stream_name:
        mount_name = "/" + (str(DIGITAL_STREAM_MOUNT or "").strip().lstrip("/") or "DIGITAL.mp3")
        for stream_node in root.findall("stream"):
            if str(stream_node.get("mount_point") or "").strip() == mount_name:
                stream_name = str(stream_node.get("name") or "").strip()
                if stream_name:
                    break
    if not stream_name:
        return set()

    tgid_keys = ("value", "talkgroup", "tgid", "id")
    tgid_types = {"talkgroup", "talkgroupid", "p25fullyqualifiedtalkgroup"}
    routed_tgids: set[str] = set()
    for alias in root.findall("alias"):
        has_stream_binding = False
        for alias_id in alias.findall("id"):
            alias_type = str(alias_id.get("type") or "").strip().lower()
            channel = str(alias_id.get("channel") or "").strip()
            if alias_type == "broadcastchannel" and channel == stream_name:
                has_stream_binding = True
                break
        if not has_stream_binding:
            continue
        for alias_id in alias.findall("id"):
            alias_type = str(alias_id.get("type") or "").strip().lower()
            if alias_type not in tgid_types:
                continue
            for key in tgid_keys:
                token = str(alias_id.get(key) or "").strip()
                if token.isdigit():
                    routed_tgids.add(token)
                    break

    with _CACHE_LOCK:
        _DIGITAL_STREAM_ROUTE_CACHE["path"] = playlist_path
        _DIGITAL_STREAM_ROUTE_CACHE["mtime"] = mtime
        _DIGITAL_STREAM_ROUTE_CACHE["ts"] = now_mono
        _DIGITAL_STREAM_ROUTE_CACHE["tgids"] = set(routed_tgids)
    return routed_tgids


def _digital_event_routes_to_stream(event: dict, routed_tgids: set[str]) -> bool:
    if not routed_tgids:
        return True
    tgid = _digital_event_tgid_for_route(event)
    if not tgid:
        return False
    return tgid in routed_tgids


def _digital_event_tgid_for_route(event: dict) -> str:
    if not isinstance(event, dict):
        return ""
    token = str(event.get("tgid") or "").strip()
    if token.isdigit():
        return token
    for key in ("label", "raw", "freq", "channel"):
        text = str(event.get(key) or "").strip()
        if not text:
            continue
        m = _DIGITAL_HIT_TGID_RE.search(text)
        if m:
            return str(m.group(1) or "").strip()
        compact = text.strip().strip("()").strip()
        if compact.isdigit():
            return compact
    return ""


def _coalesce_digital_hits(items: list[dict], window_sec: float = DIGITAL_HIT_COALESCE_SEC) -> list[dict]:
    """Collapse repeated digital updates for the same talkgroup/label within a short window."""
    if not items or window_sec <= 0:
        return items
    kept = []
    last_by_key: dict[str, tuple[float, int]] = {}
    for item in sorted(items, key=lambda row: float(row.get("_ts", 0.0))):
        ts = float(item.get("_ts", 0.0))
        tgid = str(item.get("tgid") or "").strip()
        label = str(item.get("label") or item.get("freq") or "").strip().lower()
        if tgid:
            key = f"tgid:{tgid}"
        elif label:
            key = f"label:{label}"
        else:
            continue
        prev = last_by_key.get(key)
        if prev is not None:
            prev_ts, prev_idx = prev
            if (ts - prev_ts) < window_sec:
                # Keep the newest event in-window so hit-list labels stay aligned
                # with the latest mapped digital label shown in status cards.
                kept[prev_idx] = item
                last_by_key[key] = (ts, prev_idx)
                continue
        kept.append(item)
        last_by_key[key] = (ts, len(kept) - 1)
    return kept


def _hit_row_key(item: dict) -> tuple:
    return (
        str(item.get("source") or ""),
        str(item.get("tgid") or ""),
        str(item.get("label_full") or item.get("label") or item.get("freq") or ""),
        str(item.get("time") or ""),
    )


def _dedupe_hit_rows(items: list[dict], window_sec: float = 2.0) -> list[dict]:
    """Dedupe near-identical hits across analog+digital ingestion windows."""
    if not items:
        return []
    if window_sec <= 0:
        return list(items)

    out: list[dict] = []
    last_seen: dict[tuple, float] = {}
    for row in sorted(items, key=lambda item: float(item.get("_ts", 0.0)), reverse=True):
        src = str(row.get("source") or row.get("type") or "").strip().lower()
        tgid = str(row.get("tgid") or "").strip()
        label = str(row.get("label_full") or row.get("label") or row.get("freq") or "").strip().lower()
        if src == "digital":
            key = ("digital", tgid or label)
        else:
            freq_key = _normalize_freq_key(row.get("freq"))
            key = (src or "analog", freq_key or label)
        ts = float(row.get("_ts", 0.0))
        prev = last_seen.get(key)
        if prev is not None and (prev - ts) <= window_sec:
            continue
        last_seen[key] = ts
        out.append(dict(row))
    out.sort(key=lambda item: float(item.get("_ts", 0.0)), reverse=True)
    return out


def _ensure_digital_visibility(merged: list[dict], digital_items: list[dict], limit: int) -> list[dict]:
    """Keep at least N digital rows visible in the hit list when digital hits exist."""
    limit = max(1, int(limit or 1))
    if not merged:
        return merged
    if DIGITAL_HITS_MIN_VISIBLE <= 0 or not digital_items:
        return merged[:limit]

    top = list(merged[:limit])
    min_visible = min(DIGITAL_HITS_MIN_VISIBLE, limit, len(digital_items))
    visible = sum(1 for row in top if str(row.get("source") or "") == "digital")
    if visible >= min_visible:
        return top

    need = min_visible - visible
    existing_keys = {_hit_row_key(row) for row in top}
    inject: list[dict] = []
    for row in sorted(digital_items, key=lambda item: float(item.get("_ts", 0.0)), reverse=True):
        key = _hit_row_key(row)
        if key in existing_keys:
            continue
        inject.append(dict(row))
        existing_keys.add(key)
        if len(inject) >= need:
            break
    if not inject:
        return top

    # Drop oldest non-digital rows to make room for injected digital rows.
    out = list(top)
    remaining = len(inject)
    for idx in range(len(out) - 1, -1, -1):
        if remaining <= 0:
            break
        if str(out[idx].get("source") or "") != "digital":
            out.pop(idx)
            remaining -= 1

    out = inject + out
    deduped: list[dict] = []
    seen: set[tuple] = set()
    for row in out:
        key = _hit_row_key(row)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
        if len(deduped) >= limit:
            break
    return deduped


def _unit_active_cached(unit: str) -> bool:
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _UNIT_ACTIVE_CACHE.get(unit)
        if entry and (now - float(entry[0])) <= _UNIT_ACTIVE_CACHE_TTL_SEC:
            return bool(entry[1])
    value = bool(unit_active(unit))
    with _CACHE_LOCK:
        _UNIT_ACTIVE_CACHE[unit] = (now, value)
    return value


def _unit_exists_cached(unit: str) -> bool:
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _UNIT_EXISTS_CACHE.get(unit)
        if entry and (now - float(entry[0])) <= _UNIT_EXISTS_CACHE_TTL_SEC:
            return bool(entry[1])
    value = bool(unit_exists(unit))
    with _CACHE_LOCK:
        _UNIT_EXISTS_CACHE[unit] = (now, value)
    return value


# H2 (2026-06-03): the heartbeat used to flip ANY inactive service to warn,
# which made acarsdec / radiosonde-auto-rx / scanner-vlc-vfo show permanent
# yellow flags even though they are intentionally `systemctl disable`d. This
# helper exposes the systemctl is-enabled state string so callers can
# distinguish "should be running but isn't" from "intentionally off".
#
# Returns one of: "enabled", "enabled-runtime", "disabled", "masked",
# "static", "linked", "alias", "indirect", "generated", "transient",
# "not-found", or "unknown" (any unexpected output / subprocess failure).
def _unit_enabled_state_cached(unit: str) -> str:
    import subprocess
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _UNIT_ENABLED_CACHE.get(unit)
        if entry and (now - float(entry[0])) <= _UNIT_ENABLED_CACHE_TTL_SEC:
            return str(entry[1])
    state = "unknown"
    try:
        # `is-enabled` writes the state to stdout and exits 0 for enabled,
        # 1 for disabled/masked/static/etc, 4 for not-found. We trust the
        # stdout token regardless of returncode.
        proc = subprocess.run(
            ["systemctl", "is-enabled", unit],
            capture_output=True, text=True, timeout=5.0,
        )
        token = (proc.stdout or proc.stderr or "").strip().lower()
        if token:
            # `systemctl` sometimes prepends a warning line; take the last
            # non-empty token, which is the actual state.
            for line in reversed(token.splitlines()):
                line = line.strip()
                if line and not line.startswith("warning:"):
                    state = line
                    break
    except Exception:
        state = "unknown"
    with _CACHE_LOCK:
        _UNIT_ENABLED_CACHE[unit] = (now, state)
    return state


# H2 helper: a unit is "intentionally off" (operator deliberately disabled
# it) when the is-enabled state is one of these. Heartbeat / sitrep should
# surface those as ok rather than warn so the dashboard stays meaningful.
_UNIT_INTENTIONALLY_OFF_STATES = frozenset({"disabled", "masked"})


def _unit_restart_loop_state(unit: str) -> dict[str, Any]:
    """Sample systemd's NRestarts for *unit* and detect crash-loops.

    Maintains a per-unit ring of (count, timestamp) samples; on each
    call adds a fresh sample and prunes any entry older than
    ``RTL_RESTART_SAMPLE_RETAIN_SEC``.  The "loop detected" flag fires
    when the delta between the newest and the oldest in-window sample
    is at or above ``RTL_RESTART_LOOP_THRESHOLD`` restarts within
    ``RTL_RESTART_LOOP_WINDOW_SEC`` seconds.

    Returns a dict shaped for direct inclusion in /api/status:
      - ``count``            : current NRestarts (int or None)
      - ``window_sec``       : configured window
      - ``threshold``        : configured restart threshold
      - ``window_restarts``  : restarts observed within window
      - ``loop_detected``    : bool
    """
    try:
        from .systemd import unit_restart_count
    except ImportError:  # pragma: no cover - test/dev fallback
        from ui.systemd import unit_restart_count  # type: ignore[no-redef]

    current = unit_restart_count(unit)
    now = time.monotonic()
    window_sec = float(RTL_RESTART_LOOP_WINDOW_SEC)
    retain_sec = float(RTL_RESTART_SAMPLE_RETAIN_SEC)
    threshold = int(RTL_RESTART_LOOP_THRESHOLD)
    window_restarts = 0
    loop_detected = False

    if current is not None:
        with _CACHE_LOCK:
            samples = _UNIT_RESTART_SAMPLES.setdefault(unit, [])
            samples.append((int(current), now))
            cutoff = now - retain_sec
            # Drop samples outside the retain window (keep a little more
            # than the detection window so brand-new samples can still
            # find an older baseline to subtract from).
            _UNIT_RESTART_SAMPLES[unit] = [
                (c, t) for (c, t) in samples if t >= cutoff
            ]
            in_window = [
                (c, t) for (c, t) in _UNIT_RESTART_SAMPLES[unit]
                if t >= (now - window_sec)
            ]
        if in_window:
            oldest_in_window = min(in_window, key=lambda s: s[1])
            window_restarts = max(0, int(current) - int(oldest_in_window[0]))
            if window_restarts >= threshold:
                loop_detected = True

    return {
        "count": current,
        "window_sec": int(window_sec),
        "threshold": threshold,
        "window_restarts": int(window_restarts),
        "loop_detected": bool(loop_detected),
    }


def _digital_mixer_runtime_state() -> tuple[bool, bool]:
    unit = str((UNITS or {}).get("digital_mixer") or "").strip()
    mixer_unit_exists = bool(unit and _unit_exists_cached(unit))
    mixer_enabled = bool(DIGITAL_MIXER_ENABLED)
    mixer_active = bool(unit and mixer_unit_exists and _unit_active_cached(unit))
    return mixer_enabled, mixer_active


def _health_state_rank(state: str) -> int:
    token = str(state or "").strip().lower()
    if token in ("failed", "critical", "bad", "offline"):
        return 3
    if token in ("degraded", "warn", "warning"):
        return 2
    if token in ("unknown",):
        return 1
    return 0


def _health_worst_state(states: list[str]) -> str:
    if not states:
        return "healthy"
    worst = max(states, key=_health_state_rank)
    norm = str(worst or "").strip().lower()
    if norm in ("critical", "bad", "offline"):
        return "failed"
    if norm in ("warn", "warning"):
        return "degraded"
    if norm in ("unknown",):
        return "unknown"
    return "healthy" if norm in ("healthy", "ok", "good") else norm


def _build_health_payload(
    *,
    status_payload: dict,
    system_stats: dict,
    analog_air_preflight: dict,
    analog_ground_preflight: dict,
    digital_preflight: dict,
    compile_state: dict,
) -> dict:
    subsystems: dict[str, dict] = {}

    dongles = ((system_stats or {}).get("dongles") or {})
    dongle_status = str(dongles.get("status") or "").strip().lower() or "unknown"
    if dongle_status == "critical":
        dongle_state = "failed"
    elif dongle_status == "degraded":
        dongle_state = "degraded"
    elif dongle_status == "ideal":
        dongle_state = "healthy"
    else:
        dongle_state = "unknown"
    dongle_reasons = []
    for serial in (dongles.get("missing_expected_serials") or []):
        dongle_reasons.append(
            {
                "code": "DONGLE_MISSING",
                "severity": "critical",
                "message": f"Missing expected serial {serial}",
            }
        )
    for serial in (dongles.get("slow_expected_serials") or []):
        dongle_reasons.append(
            {
                "code": "DONGLE_UNDERSPEED",
                "severity": "critical",
                "message": f"Under-speed serial {serial}",
            }
        )
    subsystems["dongles"] = {
        "state": dongle_state,
        "reasons": dongle_reasons,
    }

    analog_air_state = str((analog_air_preflight or {}).get("state") or "unknown")
    analog_air_reasons = list((analog_air_preflight or {}).get("reasons") or [])
    subsystems["airband"] = {"state": analog_air_state, "reasons": analog_air_reasons}

    analog_ground_state = str((analog_ground_preflight or {}).get("state") or "unknown")
    analog_ground_reasons = list((analog_ground_preflight or {}).get("reasons") or [])
    subsystems["ground"] = {"state": analog_ground_state, "reasons": analog_ground_reasons}

    analog_scan_health = dict(status_payload.get("analog_scan_health") or {})
    analog_scan_reasons = []
    analog_scan_state = "healthy"
    for target in ("airband", "ground"):
        snapshot = dict(analog_scan_health.get(target) or {})
        if not bool(snapshot.get("monopolized")):
            continue
        analog_scan_state = _health_worst_state([analog_scan_state, "degraded"])
        dominant_frequency = str(snapshot.get("dominant_frequency") or "").strip()
        dominant_ratio = float(snapshot.get("dominant_ratio") or 0.0)
        profile_count = int(snapshot.get("profile_frequency_count") or 0)
        analog_scan_reasons.append(
            {
                "code": "ANALOG_SCAN_MONOPOLIZED",
                "severity": "warn",
                "message": (
                    f"{target} scan is dominated by {dominant_frequency or 'one frequency'} "
                    f"({dominant_ratio:.0%} of activity across {profile_count} configured channels)"
                ),
            }
        )
    subsystems["analog_scan"] = {"state": analog_scan_state, "reasons": analog_scan_reasons}

    digital_state = str((digital_preflight or {}).get("state") or "unknown")
    digital_reasons = list((digital_preflight or {}).get("reasons") or [])
    if not bool(status_payload.get("digital_active")):
        digital_state = _health_worst_state([digital_state, "failed"])
        digital_reasons.append(
            {
                "code": "DIGITAL_SERVICE_OFFLINE",
                "severity": "critical",
                "message": "Digital decoder service is stopped",
            }
        )
    subsystems["digital"] = {"state": digital_state, "reasons": digital_reasons}

    missing_digital_tuners = [
        str(token or "").strip()
        for token in (status_payload.get("digital_tuner_missing_serials") or [])
        if str(token or "").strip()
    ]
    slow_digital_tuners = [
        str(token or "").strip()
        for token in (status_payload.get("digital_tuner_slow_serials") or [])
        if str(token or "").strip()
    ]
    sdrtrunk_state = "healthy" if bool(status_payload.get("digital_active")) else "failed"
    sdrtrunk_reasons = []
    if sdrtrunk_state != "healthy":
        sdrtrunk_reasons.append(
            {
                "code": "SDRTRUNK_INACTIVE",
                "severity": "critical",
                "message": "scanner-digital.service is not active",
            }
        )
    elif missing_digital_tuners:
        sdrtrunk_state = "failed"
        sdrtrunk_reasons.append(
            {
                "code": "SDRTRUNK_TUNER_MISSING",
                "severity": "critical",
                "message": (
                    "Configured digital tuner serial(s) missing: "
                    + ", ".join(missing_digital_tuners)
                ),
            }
        )
    elif slow_digital_tuners:
        sdrtrunk_state = "failed"
        sdrtrunk_reasons.append(
            {
                "code": "SDRTRUNK_TUNER_UNDERSPEED",
                "severity": "critical",
                "message": (
                    "Configured digital tuner serial(s) under USB speed threshold: "
                    + ", ".join(slow_digital_tuners)
                ),
            }
        )
    subsystems["sdrtrunk"] = {"state": sdrtrunk_state, "reasons": sdrtrunk_reasons}

    mixer_enabled = bool(status_payload.get("digital_mixer_enabled"))
    mixer_active = bool(status_payload.get("digital_mixer_active"))
    if not mixer_enabled:
        mixer_state = "healthy"
        mixer_reasons = [
            {
                "code": "MIXER_DISABLED_BY_DESIGN",
                "severity": "info",
                "message": "Digital mixer is intentionally disabled",
            }
        ]
    elif mixer_active:
        mixer_state = "healthy"
        mixer_reasons = []
    else:
        mixer_state = "failed"
        mixer_reasons = [
            {
                "code": "MIXER_ENABLED_BUT_INACTIVE",
                "severity": "critical",
                "message": "Digital mixer is enabled but not active",
            }
        ]
    subsystems["mixer"] = {"state": mixer_state, "reasons": mixer_reasons}

    scheduler_age_ms = int(status_payload.get("digital_allocation_snapshot_age_ms") or 0)
    scheduler_stale_ms = max(1000, int(HEALTH_SCHEDULER_STALE_MS or 3000))
    scheduler_error = str(status_payload.get("digital_last_apply_error") or "").strip()
    scheduler_state = "healthy"
    scheduler_reasons = []
    if not bool(status_payload.get("digital_active")):
        scheduler_state = "failed"
        scheduler_reasons.append(
            {
                "code": "ALLOCATION_OFFLINE",
                "severity": "critical",
                "message": "Allocation is offline because digital decoder is stopped",
            }
        )
    elif scheduler_age_ms > (scheduler_stale_ms * 2):
        scheduler_state = "failed"
        scheduler_reasons.append(
            {
                "code": "ALLOCATION_STALE",
                "severity": "critical",
                "message": (
                    f"Allocation snapshot stale ({scheduler_age_ms}ms > "
                    f"{scheduler_stale_ms * 2}ms)"
                ),
            }
        )
    elif scheduler_age_ms > scheduler_stale_ms:
        scheduler_state = "degraded"
        scheduler_reasons.append(
            {
                "code": "ALLOCATION_STALE",
                "severity": "warn",
                "message": f"Allocation snapshot stale ({scheduler_age_ms}ms > {scheduler_stale_ms}ms)",
            }
        )
    if scheduler_error:
        scheduler_state = _health_worst_state([scheduler_state, "degraded"])
        scheduler_reasons.append(
            {
                "code": "ALLOCATION_APPLY_ERROR",
                "severity": "warn",
                "message": scheduler_error,
            }
        )
    subsystems["digital_allocation"] = {"state": scheduler_state, "reasons": scheduler_reasons}

    mounts = list(status_payload.get("icecast_mounts") or [])
    expected_mounts = list(status_payload.get("icecast_expected_mounts") or [])
    stream_ok = bool(status_payload.get("icecast_active")) and (
        not expected_mounts or all(m in mounts for m in expected_mounts)
    )
    subsystems["stream"] = {
        "state": "healthy" if stream_ok else "failed",
        "reasons": [] if stream_ok else [
            {
                "code": "STREAM_OFFLINE",
                "severity": "critical",
                "message": "Icecast stream not serving all expected mounts",
            }
        ],
    }

    config_reasons = []
    config_states = []
    if bool(status_payload.get("combined_config_stale")):
        config_reasons.append(
            {
                "code": "CONFIG_STALE",
                "severity": "warn",
                "message": "Combined runtime config is stale",
            }
        )
        config_states.append("degraded")
    if bool(status_payload.get("rtl_restart_required")):
        config_reasons.append(
            {
                "code": "CONFIG_RESTART_REQUIRED",
                "severity": "warn",
                "message": "Runtime restart required to apply config",
            }
        )
        config_states.append("degraded")
    compile_status = str((compile_state or {}).get("status") or "").strip().lower()
    if compile_status in ("failed", "degraded"):
        config_states.append(compile_status)
        for issue in (compile_state.get("issues") or []):
            if isinstance(issue, dict):
                config_reasons.append(issue)
    subsystems["config"] = {
        "state": _health_worst_state(config_states or ["healthy"]),
        "reasons": config_reasons,
    }

    overall_state = _health_worst_state([row.get("state") or "unknown" for row in subsystems.values()])
    overall_codes = []
    for row in subsystems.values():
        for reason in (row.get("reasons") or []):
            code = str((reason or {}).get("code") or "").strip()
            if code and code not in overall_codes:
                overall_codes.append(code)
    return {
        "overall": {
            "state": overall_state,
            "reason_codes": overall_codes[:64],
        },
        "subsystems": subsystems,
    }


def _parse_time_ts(value: str) -> float:
    if not value:
        return 0.0
    try:
        dt = datetime.strptime(value, "%H:%M:%S")
        now = datetime.now()
        dt = dt.replace(year=now.year, month=now.month, day=now.day)
        return dt.timestamp()
    except Exception:
        return 0.0


def _canonical_scan_api_path(path: str) -> str:
    """Normalize preferred `/api/scan/*` routes to current handler paths."""
    token = str(path or "").strip()
    if not token:
        return ""
    if token == "/api/scan/mode":
        return "/api/mode"
    if token.startswith("/api/scan/favorites-wizard/"):
        return "/api/hp/" + token[len("/api/scan/") :]
    scan_aliases = {
        "/api/scan/state": "/api/hp/state",
        "/api/scan/pool-preview": "/api/hp/scan-pool-preview",
        "/api/scan/service-types": "/api/hp/service-types",
        "/api/scan/avoids": "/api/hp/avoids",
        "/api/scan/location/ip": "/api/hp/location/ip",
        "/api/scan/location/reverse": "/api/hp/location/reverse",
        "/api/scan/favorites-sync": "/api/hp/favorites-sync",
        "/api/scan/hold": "/api/hp/hold",
        "/api/scan/next": "/api/hp/next",
        "/api/scan/avoid": "/api/hp/avoid",
    }
    return scan_aliases.get(token, token)


def _clone_hit_items(items: list[dict]) -> list[dict]:
    return [dict(item or {}) for item in (items or [])]


def _build_hits_payload(limit: int = 50) -> dict:
    limit = max(1, int(limit or 50))
    scan_limit = max(50, limit)

    airband_conf = read_active_config_path()
    ground_conf = os.path.realpath(GROUND_CONFIG_PATH)
    _, profiles_airband, profiles_ground = split_profiles()
    profile_airband = guess_current_profile(
        airband_conf,
        [(p["id"], p["label"], p["path"]) for p in profiles_airband],
    )
    profile_ground = guess_current_profile(
        ground_conf,
        [(p["id"], p["label"], p["path"]) for p in profiles_ground],
    )
    airband_labels = _resolve_analog_label_map(airband_conf, profile_airband, profiles_airband)
    ground_labels = _resolve_analog_label_map(ground_conf, profile_ground, profiles_ground)
    items = _annotate_analog_hits(
        read_hit_list_cached(limit=scan_limit),
        airband_labels,
        ground_labels,
    )
    for item in items:
        ts_val = 0.0
        try:
            ts_val = float(item.get("ts") or 0.0)
        except Exception:
            ts_val = 0.0
        item["_ts"] = ts_val if ts_val > 0 else _parse_time_ts(item.get("time"))
        item.pop("ts", None)

    digital_items = []
    include_digital_events = True
    if DIGITAL_HITS_REQUIRE_ACTIVE_STREAM:
        # Never hide real digital traffic from the hit list solely based on
        # stream mount heuristics. Keep events visible whenever decoder is up.
        try:
            include_digital_events = bool(_digital_stream_active_for_hits())
        except Exception:
            include_digital_events = True
        if not include_digital_events:
            try:
                include_digital_events = bool(get_digital_manager().isActive())
            except Exception:
                include_digital_events = True
    if include_digital_events:
        try:
            events = get_digital_manager().getRecentEvents(limit=scan_limit)
        except Exception:
            events = []
    else:
        events = []
    # OP25 backend has no SDRTrunk XML playlist, so the route-to-stream
    # filter is meaningless — skip it to avoid dropping valid OP25 events.
    if DIGITAL_BACKEND == "op25":
        routed_tgids: set[str] = set()
    else:
        routed_tgids = _digital_stream_routed_tgids_for_hits() if DIGITAL_HITS_REQUIRE_AUDIO_EVENT else set()
    for event in events:
        if DIGITAL_HITS_REQUIRE_AUDIO_EVENT and not _digital_event_is_audible_hit(event):
            continue
        if DIGITAL_HITS_REQUIRE_AUDIO_EVENT and not _digital_event_routes_to_stream(event, routed_tgids):
            continue
        label = str(event.get("label") or "").strip()
        tgid = _digital_event_tgid_for_route(event)
        agency = str(event.get("agency") or "").strip()
        department = str(event.get("department") or "").strip()
        if not label and tgid:
            label = f"TG {tgid}"
        if label and label.strip("()").isdigit() and tgid:
            label = f"TG {tgid}"
        if not label:
            continue
        duration_ms = max(0, _safe_int(event.get("durationMs")) or 0)
        time_ms = int(event.get("timeMs") or 0)
        ts = time_ms / 1000.0 if time_ms else time.time()
        time_str = time.strftime("%H:%M:%S", time.localtime(ts))
        digital_items.append({
            "time": time_str,
            "freq": label,
            "duration": int((duration_ms + 999) // 1000) if duration_ms > 0 else 0,
            "label": _short_label(label, max_len=48),
            "label_full": label,
            "mode": event.get("mode"),
            "tgid": tgid,
            "agency": agency,
            "department": department,
            "type": "digital",
            "source": "digital",
            "_ts": ts,
        })
    digital_items = _coalesce_digital_hits(digital_items)

    merged = items + digital_items
    merged = _dedupe_hit_rows(merged, window_sec=2.0)
    now_ts = time.time()
    min_ts = now_ts - float(HIT_LIST_MAX_AGE_SEC)
    merged = [
        item for item in merged
        if float(item.get("_ts") or 0.0) >= min_ts
    ]
    merged.sort(key=lambda item: item.get("_ts", 0.0))
    merged = merged[-scan_limit:]
    merged.reverse()
    if len(merged) > limit:
        merged = _ensure_digital_visibility(merged, digital_items, limit)
    else:
        merged = _ensure_digital_visibility(merged, digital_items, limit)
    for item in merged:
        try:
            ts_val = float(item.get("_ts") or 0.0)
        except Exception:
            ts_val = 0.0
        if ts_val > 0:
            item["ts"] = ts_val
        item.pop("_ts", None)
    return {"items": merged}


def _get_hits_payload_cached(limit: int = 50) -> dict:
    limit = max(1, int(limit or 50))
    now = time.monotonic()
    with _CACHE_LOCK:
        cached_payload = _HITS_CACHE.get("payload")
        cached_ts = float(_HITS_CACHE.get("ts") or 0.0)
        if isinstance(cached_payload, dict) and (now - cached_ts) <= _HITS_CACHE_TTL_SEC:
            items = _clone_hit_items(cached_payload.get("items") or [])
            if len(items) > limit:
                items = items[:limit]
            return {"items": items}
    payload = _build_hits_payload(limit=max(50, limit))
    with _CACHE_LOCK:
        _HITS_CACHE["ts"] = now
        _HITS_CACHE["payload"] = {"items": _clone_hit_items(payload.get("items") or [])}
    items = _clone_hit_items(payload.get("items") or [])
    if len(items) > limit:
        items = items[:limit]
    return {"items": items}


def _compute_sounding_params(levels):
    """Compute severe weather parameters from sounding levels using SHARPpy.

    Levels must be dicts with pressure_hpa, altitude_ft, temp_c, dewpoint_c,
    wind_dir_deg, wind_speed_kt — already filtered and sorted pressure desc.
    Returns a dict of parameter values (floats or None for missing).
    """
    try:
        import numpy as np
        import sharppy.sharptab.profile as sp_profile
        import sharppy.sharptab.params as sp_params
        import sharppy.sharptab.winds as sp_winds
        import sharppy.sharptab.interp as sp_interp
        import sharppy.sharptab.utils as sp_utils
    except ImportError as e:
        return {"error": f"SHARPpy not available: {e}"}

    def _f(v):
        if v is None:
            return None
        try:
            f = float(v)
            return None if (f != f) else f  # NaN check
        except Exception:
            return None

    def _ma(v):
        """Convert masked array scalar to float or None."""
        try:
            import numpy as np
            if np.ma.is_masked(v):
                return None
            return _f(v)
        except Exception:
            return None

    pres = np.array([o["pressure_hpa"] for o in levels], dtype=float)
    hght = np.array([o["altitude_ft"] * 0.3048 for o in levels], dtype=float)
    tmpc = np.array([o["temp_c"] for o in levels], dtype=float)
    dwpc = np.array([o["dewpoint_c"] for o in levels], dtype=float)
    wdir = np.array([o["wind_dir_deg"] for o in levels], dtype=float)
    wspd = np.array([o["wind_speed_kt"] for o in levels], dtype=float)

    try:
        prof = sp_profile.create_profile(
            profile="default",
            pres=pres, hght=hght, tmpc=tmpc, dwpc=dwpc,
            wdir=wdir, wspd=wspd,
        )

        sb = sp_params.parcelx(prof, flag=1)
        mu = sp_params.parcelx(prof, flag=4)
        ml = sp_params.parcelx(prof, flag=3)

        p_sfc = prof.pres[prof.sfc]
        try:
            p_6km = sp_interp.pres(prof, sp_interp.to_msl(prof, 6000))
            shr_u, shr_v = sp_winds.wind_shear(prof, pbot=p_sfc, ptop=p_6km)
            shear_06 = _ma(sp_utils.mag(shr_u, shr_v))
        except Exception:
            shear_06 = None

        try:
            srh1 = sp_winds.helicity(prof, 0, 1000)
            srh1_val = _ma(srh1[0]) if srh1 else None
        except Exception:
            srh1_val = None

        try:
            srh3 = sp_winds.helicity(prof, 0, 3000)
            srh3_val = _ma(srh3[0]) if srh3 else None
        except Exception:
            srh3_val = None

        return {
            "sb_cape": _ma(sb.bplus),
            "sb_cin": _ma(sb.bminus),
            "mu_cape": _ma(mu.bplus),
            "mu_cin": _ma(mu.bminus),
            "ml_cape": _ma(ml.bplus),
            "ml_cin": _ma(ml.bminus),
            "lcl_hpa": _ma(sb.lclpres),
            "lfc_hpa": _ma(sb.lfcpres),
            "el_hpa": _ma(sb.elpres),
            "shear_06km_kt": shear_06,
            "srh_1km": srh1_val,
            "srh_3km": srh3_val,
        }
    except Exception as e:
        return {"error": str(e)}


def _generate_skewt_png(levels, params_dict):
    """Render a skew-T/log-P diagram with hodograph; return PNG bytes."""
    try:
        import io
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.ticker as ticker
        import numpy as np
    except ImportError as e:
        raise RuntimeError(f"matplotlib/numpy not available: {e}")

    # Sort surface→top (pressure descending)
    levels = sorted(levels, key=lambda o: o["pressure_hpa"], reverse=True)

    pres = np.array([o["pressure_hpa"] for o in levels], dtype=float)
    tmpc = np.array([o["temp_c"] for o in levels], dtype=float)
    dwpc = np.array([o["dewpoint_c"] for o in levels], dtype=float)
    wspd = np.array([o.get("wind_speed_kt", 0) or 0 for o in levels], dtype=float)
    wdir = np.array([o.get("wind_dir_deg", 0) or 0 for o in levels], dtype=float)

    SKEW = 45.0  # °C per log10-pressure decade

    def sx(t, p, p_ref=1000.0):
        return t + SKEW * np.log10(p_ref / p)

    def py(p):
        return np.log10(p)

    BG = "#0d1117"
    fig = plt.figure(figsize=(11, 9), facecolor=BG)
    ax = fig.add_axes([0.08, 0.06, 0.58, 0.88], facecolor=BG)
    ax_h = fig.add_axes([0.72, 0.52, 0.26, 0.42], facecolor=BG)
    ax_p = fig.add_axes([0.72, 0.06, 0.26, 0.40], facecolor=BG)

    ISOBARS = [1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100]
    T_MIN, T_MAX = -50, 45
    P_MIN, P_MAX = 100, 1050

    # --- Background lines ---
    for t in range(-100, 55, 10):
        pa = np.array([P_MAX, P_MIN], dtype=float)
        clr = "#2a4a2a" if t % 20 == 0 else "#1a2e1a"
        ax.plot(sx(np.full(2, float(t)), pa), py(pa), color=clr, lw=0.5, zorder=1)
        xt = sx(float(t), P_MAX)
        if sx(T_MIN, P_MAX) <= xt <= sx(T_MAX, P_MIN) + 5:
            ax.text(xt, py(P_MAX) - 0.008, str(t), color="#3a5a3a",
                    fontsize=6.5, ha="center", va="top")

    for p in ISOBARS:
        ax.axhline(py(p), color="#1a2a3a", lw=0.6, zorder=1)
        ax.text(sx(T_MIN, p) - 0.5, py(p), str(p), color="#4a7a9a",
                fontsize=6.5, ha="right", va="center")

    # Dry adiabats
    KAPPA = 0.2854
    for theta_c in range(-30, 110, 10):
        pa = np.linspace(P_MAX, P_MIN, 80)
        ta = (theta_c + 273.15) * (pa / 1000.0) ** KAPPA - 273.15
        mask = ta >= -75
        if mask.sum() > 1:
            ax.plot(sx(ta[mask], pa[mask]), py(pa[mask]),
                    color="#2a2a0a", lw=0.45, zorder=1)

    # Moist adiabats
    def _moist_lapse(t_c, p_hpa):
        t_k = t_c + 273.15
        es = 6.112 * np.exp(17.67 * t_c / (t_c + 243.5))
        rs = 0.622 * es / max(p_hpa - es, 0.01)
        numer = (287.04 * t_k + 2501000.0 * rs) / (p_hpa * 100.0)
        denom = 1004.0 + (2501000.0 ** 2 * rs) / (461.5 * t_k ** 2)
        return numer / denom

    for t_sfc in range(-20, 35, 5):
        pa = np.linspace(P_MAX, P_MIN, 100)
        ta = [float(t_sfc)]
        for i in range(1, len(pa)):
            dp = (pa[i] - pa[i - 1]) * 100.0
            ta.append(ta[-1] + _moist_lapse(ta[-1], pa[i - 1]) * dp)
        ta = np.array(ta)
        mask = ta >= -75
        if mask.sum() > 1:
            ax.plot(sx(ta[mask], pa[mask]), py(pa[mask]),
                    color="#0a2a2a", lw=0.45, zorder=1)

    # --- Temperature / dewpoint traces ---
    ax.plot(sx(tmpc, pres), py(pres), "-", color="#e74c3c", lw=2.0, zorder=5, label="T (°C)")
    ax.plot(sx(tmpc, pres), py(pres), "o", color="#e74c3c", ms=2.5, zorder=6)
    ax.plot(sx(dwpc, pres), py(pres), "-", color="#2ecc71", lw=2.0, zorder=5, label="Td (°C)")
    ax.plot(sx(dwpc, pres), py(pres), "o", color="#2ecc71", ms=2.5, zorder=6)

    # Wind barbs (right margin)
    u = -wspd * np.sin(np.radians(wdir))
    v = -wspd * np.cos(np.radians(wdir))
    x_barb = np.full(len(pres), sx(T_MAX, P_MIN) + 6)
    ax.barbs(x_barb, py(pres), u, v,
             barbcolor="#9090b0", flagcolor="#9090b0",
             length=5, linewidth=0.8, zorder=7)

    # Axes limits / labels
    ax.set_xlim(sx(T_MIN, P_MAX) - 2, sx(T_MAX, P_MIN) + 12)
    ax.set_ylim(py(P_MAX) + 0.02, py(P_MIN) - 0.02)
    ax.set_yticks([py(p) for p in ISOBARS])
    ax.set_yticklabels([str(p) for p in ISOBARS], fontsize=7, color="#8090a0")
    ax.set_ylabel("Pressure (hPa)", color="#8090a0", fontsize=9)
    ax.set_xlabel("Temperature (°C)  [isotherms skewed]", color="#8090a0", fontsize=8)
    ax.tick_params(axis="x", colors="#606070")
    ax.set_title("Skew-T / Log-P", color="#d0d0e0", fontsize=11, pad=6)
    for sp in ax.spines.values():
        sp.set_edgecolor("#303040")
    ax.legend(loc="upper right", fontsize=8, facecolor="#1a1a2a",
              edgecolor="#404050", labelcolor="#d0d0e0")

    # --- Hodograph ---
    sort_idx = np.argsort(pres)[::-1]
    u_s, v_s = u[sort_idx], v[sort_idx]
    for r in [20, 40, 60, 80]:
        th = np.linspace(0, 2 * np.pi, 120)
        ax_h.plot(r * np.cos(th), r * np.sin(th), color="#252535", lw=0.6)
        ax_h.text(r + 0.5, 0.5, str(r), color="#404050", fontsize=5.5)
    ax_h.axhline(0, color="#303040", lw=0.5)
    ax_h.axvline(0, color="#303040", lw=0.5)
    ax_h.plot(u_s, v_s, "-", color="#a060e0", lw=1.8, zorder=5)
    ax_h.plot(u_s[:1], v_s[:1], "o", color="#60a0ff", ms=5, zorder=6)
    ax_h.set_aspect("equal")
    ax_h.set_title("Hodograph (kt)", color="#c0c0d0", fontsize=8, pad=3)
    ax_h.tick_params(colors="#606070", labelsize=6)
    for sp in ax_h.spines.values():
        sp.set_edgecolor("#303040")

    # --- Parameters panel ---
    ax_p.axis("off")
    ax_p.set_title("Parameters", color="#c0c0d0", fontsize=8, pad=3)

    def _fv(v, fmt=".0f", unit=""):
        if v is None:
            return "--"
        try:
            return ("{:" + fmt + "}{}").format(float(v), unit)
        except Exception:
            return "--"

    if params_dict and "error" not in params_dict:
        rows = [
            ("SBCAPE", _fv(params_dict.get("sb_cape"), unit=" J/kg")),
            ("SBCIN", _fv(params_dict.get("sb_cin"), unit=" J/kg")),
            ("MUCAPE", _fv(params_dict.get("mu_cape"), unit=" J/kg")),
            ("MLCAPE", _fv(params_dict.get("ml_cape"), unit=" J/kg")),
            ("LCL", _fv(params_dict.get("lcl_hpa"), unit=" hPa")),
            ("LFC", _fv(params_dict.get("lfc_hpa"), unit=" hPa")),
            ("EL", _fv(params_dict.get("el_hpa"), unit=" hPa")),
            ("Shr 0-6km", _fv(params_dict.get("shear_06km_kt"), unit=" kt")),
            ("SRH 0-1km", _fv(params_dict.get("srh_1km"), unit=" m²/s²")),
            ("SRH 0-3km", _fv(params_dict.get("srh_3km"), unit=" m²/s²")),
        ]
    else:
        err = params_dict.get("error", "unavailable") if params_dict else "unavailable"
        rows = [("Params", err[:30])]

    for i, (label, val) in enumerate(rows):
        y = 0.96 - i * 0.095
        ax_p.text(0.02, y, label + ":", color="#8090a0", fontsize=7.5,
                  transform=ax_p.transAxes, va="top", fontfamily="monospace")
        ax_p.text(0.98, y, val, color="#d0e0d0", fontsize=7.5,
                  transform=ax_p.transAxes, va="top", ha="right",
                  fontfamily="monospace")

    for sp in ax_h.spines.values():
        sp.set_edgecolor("#303040")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _filter_sounding_levels(levels, max_age_sec=5400):
    """Filter levels to recent observations with valid met data."""
    cutoff = time.time() - max_age_sec
    out = []
    for o in levels:
        if o.get("timestamp", 0) < cutoff:
            continue
        p = o.get("pressure_hpa")
        t = o.get("temp_c")
        td = o.get("dewpoint_c")
        if not p or p <= 0:
            continue
        if t is None or t < -9000:
            continue
        if td is None or td < -9000:
            continue
        if o.get("altitude_ft") is None:
            continue
        out.append(o)
    out.sort(key=lambda o: o["pressure_hpa"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Heartbeat: dashboard-grade answer to "what is the radio doing right now?"
# ---------------------------------------------------------------------------
# Phase 1b — V1 ships QUIET vs WEDGED only. RF_DEGRADED (noise-floor and
# sentinel-channel heuristics) is intentionally deferred to a follow-up so
# this endpoint stays cheap, deterministic, and read-only.
#
# All probes here MUST be read-only — no service restarts, no config writes.
# Total compute budget is bounded by `_HEARTBEAT_CACHE_TTL_SEC` (the
# foreground sample is wall-time capped via `_HEARTBEAT_MP3_SAMPLE_*`).
_HEARTBEAT_CACHE_TTL_SEC = max(2.0, float(os.getenv("HEARTBEAT_CACHE_TTL_SEC", "5.0")))
_HEARTBEAT_STATS_STALE_SEC = max(5.0, float(os.getenv("HEARTBEAT_STATS_STALE_SEC", "60.0")))
_HEARTBEAT_MP3_SAMPLE_DURATION_SEC = max(0.3, float(os.getenv("HEARTBEAT_MP3_SAMPLE_DURATION_SEC", "3.0")))
_HEARTBEAT_MP3_SAMPLE_TIMEOUT_SEC = max(0.5, float(os.getenv("HEARTBEAT_MP3_SAMPLE_TIMEOUT_SEC", "7.0")))
_HEARTBEAT_MP3_CONNECT_TIMEOUT_SEC = max(0.3, float(os.getenv("HEARTBEAT_MP3_CONNECT_TIMEOUT_SEC", "5.0")))
_HEARTBEAT_LOCK = threading.Lock()
_HEARTBEAT_CACHE: dict[str, Any] = {
    "ts": 0.0,
    "payload": None,
    "since_state": None,
    "since_ts": 0.0,
}


def _heartbeat_format_age(age_sec) -> str:
    """Compact human-readable age for evidence rows."""
    if age_sec is None:
        return "unknown"
    age_sec = float(age_sec)
    if age_sec < 60:
        return f"{age_sec:.0f}s"
    if age_sec < 3600:
        return f"{age_sec/60:.0f}m"
    if age_sec < 86400:
        return f"{age_sec/3600:.0f}h"
    return f"{age_sec/86400:.0f}d"


# ---- Heartbeat mp3 byte-delta probe (Option B hybrid, 2026-06-03) ----------
#
# Why no streaming probe any more: icecast 2.4.4 propagates its source-side
# `total_bytes_read` counter in libshout flush boundaries (~5 s chunks),
# not smoothly. Diagnostic evidence: ANALOG_GROUND at +2s = 0 bytes read,
# at +5s = 11,690 bytes — same healthy mount, two adjacent windows. A short
# fixed sample window therefore reports 0 bytes on healthy low-bitrate
# mounts whenever the call lands between flushes. The H1 patch (extending
# the per-read socket timeout) helped high-bitrate mounts but still
# false-warned on 8 kbps ANALOG / ANALOG_GROUND because those mounts'
# flush cadence frequently exceeded any reasonable in-call wait.
#
# New approach: read icecast's own `total_bytes_read` once per heartbeat
# call (via /admin/stats — `total_bytes_read` is NOT exposed in the public
# /status-json.xsl). Cache the value + timestamp module-level. Subsequent
# calls compute a delta over the heartbeat-call cadence (~10–30 s), which
# is large enough to span at least one flush boundary and yield a
# deterministic byte-rate signal at ~100 ms wall-time per call.
#
# Bootstrap (first call after airband-ui restart): no prior sample, so
# fall back to a presence check on `stream_start` + a positive
# `total_bytes_read`.

# Module-level cache. Keyed by canonical mount name (no leading slash).
# Each entry: {"bytes": int, "ts": float (wall time)}.
_HEARTBEAT_BYTE_CACHE: dict[str, dict] = {}
# Last-known status per mount so the "between flushes" branch
# (5 ≤ interval < 10 s, delta < 500 B) can hold steady rather than
# bouncing between ok and warm-up.
_HEARTBEAT_BYTE_LAST_STATUS: dict[str, dict] = {}
_HEARTBEAT_BYTE_CACHE_LOCK = threading.Lock()

# Operator-facing mounts. Keepalive-* mounts always flush smoothly because
# rtl-airband-keepalive-* services publish a continuous silence stream;
# they are intentionally NOT probed (not operator-relevant).
_HEARTBEAT_PROBE_MOUNTS: tuple[str, ...] = (
    "ANALOG.mp3",
    "ANALOG_GROUND.mp3",
    "DIGITAL.mp3",
    "VFO.mp3",
)

# Healthy threshold: ≥500 B over ≥5 s = ≥800 bps. Way above counter noise,
# well below the ~5040-byte size of a single libshout flush, so a single
# flush in the window is enough to clear the bar.
_HEARTBEAT_BYTE_DELTA_MIN: int = 500
_HEARTBEAT_BYTE_INTERVAL_MIN: float = 5.0
_HEARTBEAT_BYTE_INTERVAL_WARN: float = 10.0
# Presence-fallback window: stream_start within the last 5 minutes is
# accepted as "ok present" when no prior cached sample exists.
_HEARTBEAT_BYTE_PRESENCE_MAX_AGE_SEC: float = 300.0
# Wall-time budgets for the admin/stats fetch.
_HEARTBEAT_ADMIN_TIMEOUT_SEC: float = 2.5
_HEARTBEAT_ADMIN_RETRY_BACKOFF_SEC: float = 0.5


def _heartbeat_parse_admin_stats(xml_text: str) -> dict[str, dict]:
    """Parse icecast `/admin/stats` XML into per-mount metadata.

    Returns dict keyed by mount name (no leading slash). Each entry:
      * ``bytes_read``: int (0 if missing)
      * ``stream_start``: str (ISO8601 or RFC-2822, empty if missing)
      * ``has_source``: bool — True iff a publisher appears connected
        (proxy: non-empty stream_start OR bytes_read > 0).
    """
    out: dict[str, dict] = {}
    if not xml_text:
        return out
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for src in root.findall("source"):
        mount_attr = (src.get("mount") or "").strip().lstrip("/")
        if not mount_attr:
            continue
        bytes_node = src.find("total_bytes_read")
        start_node = src.find("stream_start_iso8601")
        if start_node is None:
            start_node = src.find("stream_start")
        bytes_read = 0
        if bytes_node is not None:
            try:
                bytes_read = int((bytes_node.text or "0").strip())
            except (ValueError, AttributeError):
                bytes_read = 0
        stream_start = ""
        if start_node is not None and start_node.text:
            stream_start = start_node.text.strip()
        out[mount_attr] = {
            "bytes_read": bytes_read,
            "stream_start": stream_start,
            "has_source": bool(stream_start) or bytes_read > 0,
        }
    return out


def _heartbeat_fetch_admin_stats() -> dict[str, dict] | None:
    """One `/admin/stats` fetch + parse. One retry with backoff on failure.

    Returns the parsed-by-mount dict, or None if both attempts failed.
    Wall-time budget: ≤ ~5.5 s in the worst case (2 × 2.5 s timeout +
    500 ms backoff), but typical-case ~50 ms.
    """
    from base64 import b64encode
    user = (
        os.getenv("ICECAST_ADMIN_USER")
        or os.getenv("ICECAST_SOURCE_USER")
        or "source"
    )
    password = (
        os.getenv("ICECAST_ADMIN_PASSWORD")
        or os.getenv("ICECAST_SOURCE_PASSWORD")
        or "062352"
    )
    url = f"http://127.0.0.1:{ICECAST_PORT}/admin/stats"
    creds = b64encode(f"{user}:{password}".encode()).decode()
    headers = {
        "Authorization": f"Basic {creds}",
        "User-Agent": "sb3-heartbeat/1.1",
    }
    for attempt in range(2):
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=_HEARTBEAT_ADMIN_TIMEOUT_SEC) as resp:
                text = resp.read().decode("utf-8", errors="ignore")
            return _heartbeat_parse_admin_stats(text)
        except Exception:
            if attempt == 0:
                time.sleep(_HEARTBEAT_ADMIN_RETRY_BACKOFF_SEC)
                continue
    return None


def _heartbeat_parse_stream_start_age(value: str, now_wall: float) -> float | None:
    """Seconds since `stream_start`, or None if unparseable.

    Accepts both ISO8601 (icecast 2.4 emits `stream_start_iso8601`) and
    RFC-2822 (`stream_start`).
    """
    if not value:
        return None
    s = value.strip()
    # ISO8601 — handle a trailing offset without a colon, which Python
    # <3.11 doesn't accept (icecast emits `-0400`, not `-04:00`).
    try:
        candidate = s
        if "T" in candidate and len(candidate) >= 5:
            tail = candidate[-5:]
            if tail[0] in "+-" and ":" not in tail:
                candidate = candidate[:-2] + ":" + candidate[-2:]
        dt = datetime.fromisoformat(candidate)
        return max(0.0, now_wall - dt.timestamp())
    except Exception:
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt is not None:
            return max(0.0, now_wall - dt.timestamp())
    except Exception:
        pass
    return None


def _heartbeat_check_mount_bytes(mount_name: str,
                                 admin_stats: dict[str, dict] | None,
                                 now_wall: float) -> dict:
    """Hybrid byte-delta probe with presence fallback for one mount.

    See the module-level explanation above for the bursty-counter rationale.
    Returns ``{"status": str, "value": str}``.

    Cache semantics:
      * On every call we read the current `total_bytes_read` + timestamp.
      * If the elapsed interval since the last cached sample is < 5 s,
        we do NOT overwrite the cache — we keep the older sample so the
        next call still has a usable baseline (delta needs ≥ 5 s to span
        a flush boundary deterministically).
      * Otherwise we overwrite the cache with the fresh sample.
    """
    mount_key = str(mount_name or "").strip().lstrip("/")
    if not mount_key:
        return {"status": "warn", "value": "invalid mount"}

    if not admin_stats:
        return {"status": "warn", "value": "icecast admin unreachable"}

    info = admin_stats.get(mount_key)
    if info is None or not info.get("has_source"):
        return {"status": "warn", "value": "no source connected"}

    current_bytes = int(info.get("bytes_read", 0))
    current_start = str(info.get("stream_start") or "")

    with _HEARTBEAT_BYTE_CACHE_LOCK:
        cached = _HEARTBEAT_BYTE_CACHE.get(mount_key)

        # Bootstrap / presence fallback — no prior sample.
        if cached is None:
            stream_age = _heartbeat_parse_stream_start_age(current_start, now_wall)
            if current_bytes > 0 and stream_age is not None \
                    and stream_age <= _HEARTBEAT_BYTE_PRESENCE_MAX_AGE_SEC:
                status = {"status": "ok", "value": "present"}
            else:
                status = {"status": "warn", "value": "no recent stream"}
            _HEARTBEAT_BYTE_CACHE[mount_key] = {"bytes": current_bytes, "ts": now_wall}
            _HEARTBEAT_BYTE_LAST_STATUS[mount_key] = status
            return status

        prior_bytes = int(cached.get("bytes", 0))
        prior_ts = float(cached.get("ts", 0.0))
        interval = now_wall - prior_ts

        if interval < _HEARTBEAT_BYTE_INTERVAL_MIN:
            # Too soon since the last sample; preserve the cached baseline
            # so the next call has ≥5 s to compare against.
            last = _HEARTBEAT_BYTE_LAST_STATUS.get(mount_key)
            if last and last.get("status") == "ok":
                return last
            return {"status": "ok", "value": "warming up"}

        # Counter resets (source reconnected, mount restart) can make the
        # current value smaller than the cached one — clamp at zero.
        delta = max(0, current_bytes - prior_bytes)

        if delta >= _HEARTBEAT_BYTE_DELTA_MIN:
            status = {"status": "ok", "value": f"{delta} B in {interval:.1f}s"}
        elif interval >= _HEARTBEAT_BYTE_INTERVAL_WARN:
            status = {"status": "warn", "value": "no source data"}
        else:
            # 5 ≤ interval < 10 with delta < 500: could legitimately be
            # the gap between libshout flushes. Hold the last-known-good
            # status rather than flagging prematurely.
            last = _HEARTBEAT_BYTE_LAST_STATUS.get(mount_key)
            if last and last.get("status") == "ok":
                status = last
            else:
                status = {"status": "ok", "value": f"{delta} B in {interval:.1f}s"}

        _HEARTBEAT_BYTE_CACHE[mount_key] = {"bytes": current_bytes, "ts": now_wall}
        _HEARTBEAT_BYTE_LAST_STATUS[mount_key] = status
        return status


# Evidence-row status → severity. The overall heartbeat state is the worst
# severity across ALL evidence rows (see `_heartbeat_rollup_state`).
_HEARTBEAT_STATUS_SEVERITY = {"bad": 2, "warn": 1, "warning": 1, "ok": 0, "info": 0}


def _heartbeat_row_severity(row: dict) -> int:
    """Severity of one evidence row (unknown statuses are treated as healthy)."""
    if not isinstance(row, dict):
        return 0
    return _HEARTBEAT_STATUS_SEVERITY.get(str(row.get("status") or "").strip().lower(), 0)


def _heartbeat_summarize_row(row: dict | None) -> str:
    """`label: value` one-liner for an evidence row (empty when row is falsy)."""
    if not isinstance(row, dict):
        return ""
    label = str(row.get("label") or "component").strip()
    value = str(row.get("value") or "").strip()
    return f"{label}: {value}" if value else label


def _heartbeat_rollup_state(evidence: list[dict], wedged_reasons: list[str]) -> tuple[str, dict | None]:
    """Roll evidence rows + core wedge reasons up into one overall state.

    Phase R1 fix: the rollup must reflect EVERY evidence row, not just the
    core-pipeline `wedged_reasons`. Previously dongle rows (waterfall A/B,
    VFO, disco) and warn rows (e.g. "/ANALOG.mp3 byte rate 0 B") were appended
    to `evidence` but never influenced `state`, so the badge could read
    "All systems healthy" while a row literally said "dongle A DOWN".

    Severity → state:  any ``bad`` row (or a core wedged_reason) ⇒ ``wedged``;
    else any ``warn`` row ⇒ ``degraded`` (RF_DEGRADED); else ``quiet``.
    Returns ``(state, worst_row)`` where ``worst_row`` is the highest-severity
    evidence row (ties broken toward the earliest / most core row), or ``None``.
    """
    worst_severity = max((_heartbeat_row_severity(r) for r in evidence), default=0)
    # A core pipeline failure is authoritatively `bad` even if no row carries
    # the severity — `wedged_reasons` is the core signal of last resort.
    if wedged_reasons:
        worst_severity = 2

    worst_row = None
    worst_row_sev = -1
    for row in evidence:
        sev = _heartbeat_row_severity(row)
        if sev > worst_row_sev:
            worst_row_sev = sev
            worst_row = row

    if worst_severity >= 2:
        return "wedged", worst_row
    if worst_severity == 1:
        return "degraded", worst_row
    return "quiet", worst_row


def _compute_heartbeat_payload() -> dict:
    """Compute the heartbeat payload. Cached via `_HEARTBEAT_CACHE_TTL_SEC`.

    V1 state space: ``quiet`` | ``wedged``.

    Decision rule:
      WEDGED ⇐ any service in {airband-ui, rtl-airband-airband, icecast2,
               scanner-vlc-digital, rtl-airband-ground} is inactive
            OR rtl_airband_airband stats file is missing / >threshold stale
            OR ANALOG.mp3 mount is publishing-but-empty (0 bytes/window)
      QUIET ⇐ everything healthy.

    All probes read-only. Designed to be safe to poll at 5s intervals.
    """
    now_wall = time.time()
    now_mono = time.monotonic()
    with _HEARTBEAT_LOCK:
        cached_ts = float(_HEARTBEAT_CACHE.get("ts") or 0.0)
        cached_payload = _HEARTBEAT_CACHE.get("payload")
    if isinstance(cached_payload, dict) and (now_mono - cached_ts) <= _HEARTBEAT_CACHE_TTL_SEC:
        out = dict(cached_payload)
        out["server_time"] = now_wall
        out["cached"] = True
        return out

    evidence: list[dict] = []
    wedged_reasons: list[str] = []
    recovery: str | None = None

    # Phase 4d (2026-06-04): when chirp is the analog demod, rtl-airband
    # is intentionally stopped and its stats file is intentionally stale.
    # Skip the rtl-airband-side checks under the flag and let the chirp
    # heartbeat rows (added later in this function) carry the contract.
    try:
        _chirp_on = bool(_chirp_use_gr_demod())
    except Exception:
        _chirp_on = False

    if not _chirp_on:
        # 1) rtl-airband stats file freshness — the contractual heartbeat
        #    of the RF→audio sample path. Stale ⇒ pipeline stuck.
        try:
            stats = rtl_airband_sample_flow_state(
                RTL_AIRBAND_AIRBAND_STATS_PATH,
                _HEARTBEAT_STATS_STALE_SEC,
            )
        except Exception as exc:
            stats = {
                "sample_flow_ok": False,
                "stats_age_sec": None,
                "reason": f"probe error: {exc}",
            }
        stats_age = stats.get("stats_age_sec")
        if stats_age is None:
            evidence.append({"label": "stats file", "value": "missing", "status": "bad"})
            wedged_reasons.append("rtl-airband stats file missing")
            recovery = recovery or f"systemctl restart {UNITS.get('rtl_airband','rtl-airband-airband')}"
        else:
            ok_flag = bool(stats.get("sample_flow_ok"))
            evidence.append({
                "label": "stats file",
                "value": (f"fresh ({_heartbeat_format_age(stats_age)})" if ok_flag
                          else f"stale ({_heartbeat_format_age(stats_age)})"),
                "status": "ok" if ok_flag else "bad",
            })
            if not ok_flag:
                wedged_reasons.append(stats.get("reason") or "rtl-airband stats stale")
                recovery = recovery or f"systemctl restart {UNITS.get('rtl_airband','rtl-airband-airband')}"

    # 2) Service active state for the 5 core units.
    if _chirp_on:
        # Phase 4d: chirp gr-demod replaces rtl-airband.  The 5-core
        # block becomes airband-ui + gr-demod@airband + gr-demod@ground
        # + icecast2 + scanner-vlc-digital so the operator sees the
        # actual analog pipeline.
        service_units = [
            ("airband-ui",          UNITS.get("ui", "airband-ui")),
            ("gr-demod@airband",    "gr-demod@airband.service"),
            ("gr-demod@ground",     "gr-demod@ground.service"),
            ("icecast2",            UNITS.get("icecast", "icecast2")),
            ("scanner-vlc-digital", "scanner-vlc-digital.service"),
        ]
    else:
        service_units = [
            ("airband-ui",          UNITS.get("ui", "airband-ui")),
            ("rtl-airband-airband", UNITS.get("rtl_airband", "rtl-airband-airband")),
            ("rtl-airband-ground",  UNITS.get("rtl_ground", "rtl-airband-ground")),
            ("icecast2",            UNITS.get("icecast", "icecast2")),
            ("scanner-vlc-digital", "scanner-vlc-digital.service"),
        ]
    for label, unit in service_units:
        try:
            active = _unit_active_cached(unit)
        except Exception:
            active = False
        evidence.append({
            "label": label,
            "value": "active" if active else "inactive",
            "status": "ok" if active else "bad",
        })
        if not active:
            wedged_reasons.append(f"{label} inactive")
            if not recovery:
                recovery = f"systemctl restart {unit}"

    # 2b) Extended service surface — decoders, VLC bridges, op25/VFO/broker.
    #     These are not part of the core "wedge" decision (the 5-unit block
    #     above is authoritative for that) but their silent failure has been
    #     burning operator time, so surface them as evidence rows.
    #     Severity: `warn` on inactive (RF_DEGRADED in the rollup) rather
    #     than `bad`, so a stopped decoder doesn't catastrophize the badge.
    #     Units not installed on this host → status `ok`, value `not
    #     configured` (skipped cleanly, no failure).
    extended_service_units = [
        ("scanner-digital-op25",        "scanner-digital-op25.service"),
        ("scanner-digital-op25-audio",  "scanner-digital-op25-audio.service"),
        ("scanner-vfo",                 "scanner-vfo.service"),
        ("scanner-tuner-broker",        "scanner-tuner-broker.service"),
        ("dumpvdl2",                    "dumpvdl2.service"),
        ("acarsdec",                    "acarsdec.service"),
        ("radiosonde-auto-rx",          "radiosonde-auto-rx.service"),
        ("scanner-vlc-analog",          "scanner-vlc-analog.service"),
        ("scanner-vlc-ground",          "scanner-vlc-ground.service"),
        ("scanner-vlc-vfo",             "scanner-vlc-vfo.service"),
    ]
    for label, unit in extended_service_units:
        try:
            exists = _unit_exists_cached(unit)
        except Exception:
            exists = True  # err on the side of probing; worst case we surface inactive
        if not exists:
            evidence.append({
                "label": label,
                "value": "not configured",
                "status": "ok",
            })
            continue
        try:
            active = _unit_active_cached(unit)
        except Exception:
            active = False
        if active:
            evidence.append({
                "label": label,
                "value": "active",
                "status": "ok",
            })
            continue
        # H2 (2026-06-03): inactive is not necessarily a problem. Some
        # services are intentionally `systemctl disable`d (acarsdec,
        # radiosonde-auto-rx, scanner-vlc-vfo on this host) and were
        # showing permanent warn yellows that trained the operator to
        # ignore the row. Cross-check is-enabled before flipping warn.
        try:
            enabled_state = _unit_enabled_state_cached(unit)
        except Exception:
            enabled_state = "unknown"
        if enabled_state in _UNIT_INTENTIONALLY_OFF_STATES:
            evidence.append({
                "label": label,
                "value": "intentionally off",
                "status": "ok",
            })
        else:
            evidence.append({
                "label": label,
                "value": "inactive",
                "status": "warn",
            })

    # 3) Icecast ANALOG.mp3 mount-publishing state (cheap — icecast JSON).
    analog_mount_publishing = False
    icecast_status_text = ""
    try:
        icecast_status_text = fetch_local_icecast_status()
        analog_mount_publishing = mount_publishing(
            icecast_status_text, PLAYER_MOUNT or "ANALOG.mp3"
        )
    except Exception:
        evidence.append({
            "label": "/ANALOG.mp3 mount",
            "value": "icecast status unreachable",
            "status": "bad",
        })
        wedged_reasons.append("icecast status unreachable")
    else:
        evidence.append({
            "label": "/ANALOG.mp3 mount",
            "value": "publishing" if analog_mount_publishing else "no source",
            "status": "ok" if analog_mount_publishing else "warn",
        })
        if not analog_mount_publishing:
            # `no source` is not always wedged (the upstream may be in a
            # legitimate retune), but flag for follow-on byte probe.
            wedged_reasons.append("/ANALOG.mp3 has no source")

    # 4) Operator-facing mp3 mounts — Option B hybrid byte-delta probe.
    #
    #    One read of icecast /admin/stats (~50 ms typical) yields the
    #    per-mount `total_bytes_read` counter. We diff it against a
    #    module-level cache to derive a bytes-per-interval rate over the
    #    heartbeat-call cadence (≈10–30 s), instead of a fixed 2–3 s
    #    in-call sample window.  Replaces the streaming probe that
    #    false-warned on healthy 8 kbps mounts because icecast 2.4.4
    #    flushes `total_bytes_read` in ~5 s libshout chunks; an in-call
    #    window often landed entirely between flushes (0 bytes) on a
    #    perfectly healthy mount. See `_heartbeat_check_mount_bytes`.
    #
    #    Keepalive-* mounts are intentionally NOT probed here — they are
    #    rtl-airband's continuous silence-fallback streams and are not
    #    operator-relevant.
    _hb_now = time.time()
    _hb_admin = _heartbeat_fetch_admin_stats()
    for _hb_mount in _HEARTBEAT_PROBE_MOUNTS:
        _hb_result = _heartbeat_check_mount_bytes(_hb_mount, _hb_admin, _hb_now)
        evidence.append({
            "label": f"/{_hb_mount} byte rate",
            "value": _hb_result.get("value", ""),
            "status": _hb_result.get("status", "warn"),
        })

    # ----- Phase 4c — chirp daemon awareness (ONLY when flag on).
    # When SB5_USE_GR_DEMOD=false (the default) these rows are absent
    # so the current heartbeat schema is unchanged.  When the flag is
    # on, three rows are appended:
    #   chirp-airband : ok if daemon answers get_status, fail otherwise
    #   chirp-ground  : same probe on port 7401
    #   chirp icecast : surfaces the daemon's own icecast_state field
    # ``fail`` here counts as a wedged_reason so a downed chirp daemon
    # rolls the badge up to WEDGED, which matches operator expectation
    # under the chirp regime.
    try:
        _chirp_on = bool(_chirp_use_gr_demod())
    except Exception:
        _chirp_on = False
    if _chirp_on:
        try:
            _chirp_air = _chirp_airband_client()
            _chirp_gnd = _chirp_ground_client()
        except Exception:
            _chirp_air = None
            _chirp_gnd = None
        for _label, _client in (("chirp-airband", _chirp_air),
                                ("chirp-ground", _chirp_gnd)):
            if _client is None:
                evidence.append({
                    "label": _label,
                    "value": "client init failed",
                    "status": "bad",
                })
                wedged_reasons.append(f"{_label} client init failed")
                continue
            try:
                _snap = _client.get_status()
                evidence.append({
                    "label": _label,
                    "value": (
                        f"active "
                        f"({len(_snap.get('channels') or [])} chan, "
                        f"{_snap.get('pool_free', '?')} free)"
                    ),
                    "status": "ok",
                })
                # Surface the daemon's view of its own icecast publisher.
                _ic_state = _snap.get("icecast_state")
                if _ic_state:
                    _ic_drops = int(_snap.get("icecast_drop_count") or 0)
                    _ic_bytes = int(_snap.get("icecast_bytes_sent") or 0)
                    _ic_status = "ok"
                    if _ic_state == "not_configured":
                        _ic_status = "ok"
                    elif _ic_state == "disconnected":
                        _ic_status = "warn"
                    elif _ic_state == "failed":
                        _ic_status = "bad"
                    evidence.append({
                        "label": f"{_label} icecast",
                        "value": (
                            f"{_ic_state} "
                            f"(bytes={_ic_bytes}, drops={_ic_drops})"
                        ),
                        "status": _ic_status,
                    })
            except _ChirpDaemonDown as _exc:
                evidence.append({
                    "label": _label,
                    "value": f"daemon down ({_exc})",
                    "status": "bad",
                })
                wedged_reasons.append(f"{_label} daemon down")
                if not recovery:
                    recovery = (
                        f"systemctl restart chirp-{_label.split('-', 1)[1]}.service"
                    )
            except _ChirpClientError as _exc:
                evidence.append({
                    "label": _label,
                    "value": f"daemon error ({_exc})",
                    "status": "warn",
                })
            except Exception as _exc:  # noqa: BLE001
                evidence.append({
                    "label": _label,
                    "value": f"probe exception ({_exc})",
                    "status": "warn",
                })

    # Phase 6a — live evidence rows for the two waterfall RTL-SDRs.
    # Backed by /run/scannerproject/waterfall/state.json which the
    # scanner-waterfall.service writes once a frame is in hand.
    for row in _waterfall_dongle_evidence_rows():
        evidence.append(row)
    # Phase 6b — live VFO row.
    evidence.append(_vfo_dongle_evidence_row())
    # Phase 6c — per-Disco-dongle live evidence rows backed by
    # /run/scannerproject/disco/coord_state.json.
    # Phase 6d — overridden by broker ownership when sounding is on:
    # a dongle loaned to ACARS / VDL2 shows the consumer's evidence
    # instead of the (stale) disco coordinator view.
    for row in _broker_aware_dongle_rows():
        evidence.append(row)

    # Decide state. The rollup reflects EVERY evidence row (see
    # `_heartbeat_rollup_state` for the severity rule and the bug it fixes).
    state, worst_row = _heartbeat_rollup_state(evidence, wedged_reasons)

    if state == "wedged":
        if wedged_reasons:
            headline = "A pipeline component is wedged."
            # Show up to four reasons to avoid pathological growth.
            explanation = "; ".join(wedged_reasons[:4]) + (
                "" if len(wedged_reasons) <= 4 else f" (+{len(wedged_reasons) - 4} more)"
            )
        else:
            # No core wedge, but a `bad` evidence row (e.g. a downed dongle).
            headline = f"Component failure — {_heartbeat_summarize_row(worst_row)}."
            explanation = (
                "Core audio pipeline is alive, but a monitored component "
                "reports a hard failure. See evidence rows below."
            )
    elif state == "degraded":
        headline = f"RF degraded — {_heartbeat_summarize_row(worst_row)}."
        explanation = (
            "Core services are active, but at least one component is "
            "publishing a warning. The audio path is up; a secondary "
            "subsystem needs attention."
        )
    else:
        headline = "All systems healthy. No traffic on selected channels."
        explanation = (
            "RF chain alive, mounts publishing, services active. The radio "
            "is simply quiet right now."
        )
        recovery = None

    # One-line summary naming the worst-offending component (empty when quiet).
    summary = "" if state == "quiet" else _heartbeat_summarize_row(worst_row)

    # `since` — wall-time of last state transition. Stays stable while the
    # state is unchanged so the UI can show "wedged for 4m".
    with _HEARTBEAT_LOCK:
        prev_state = _HEARTBEAT_CACHE.get("since_state")
        since_ts = float(_HEARTBEAT_CACHE.get("since_ts") or 0.0)
        if prev_state != state or since_ts <= 0.0:
            since_ts = now_wall
            _HEARTBEAT_CACHE["since_state"] = state
            _HEARTBEAT_CACHE["since_ts"] = since_ts

    since_iso = (
        datetime.utcfromtimestamp(since_ts).replace(microsecond=0).isoformat() + "Z"
    )

    # Phase 4d — expose the chirp-on flag to the frontend.  sb5.html's
    # auto-apply countdown (which POSTs /api/sitrep/action reset_radios
    # under the rtl-airband world) needs to know to NO-OP when chirp is
    # the analog backend, because in the chirp world that POST just
    # wipes the channel pool (and reset_radios_via_chirp's
    # repopulate-by-default is belt for the suspenders).  Probed
    # defensively — any failure becomes False so the legacy countdown
    # behaviour is preserved on probe error.
    try:
        _use_gr_demod_flag = bool(_chirp_use_gr_demod())
    except Exception:
        _use_gr_demod_flag = False

    payload = {
        "state": state,
        "since": since_iso,
        "headline": headline,
        "explanation": explanation,
        "summary": summary,
        "recovery": recovery,
        "evidence": evidence,
        "server_time": now_wall,
        "cached": False,
        "use_gr_demod": _use_gr_demod_flag,
    }

    with _HEARTBEAT_LOCK:
        _HEARTBEAT_CACHE["ts"] = now_mono
        _HEARTBEAT_CACHE["payload"] = payload

    return dict(payload)




# =====================================================================
# Phase 6a — waterfall state plumbing.
# scripts/waterfall.py writes state.json atomically; /api/waterfall is
# a file-backed pass-through, /api/heartbeat surfaces per-dongle status
# via _waterfall_dongle_evidence_rows().
# =====================================================================

WATERFALL_STATE_DIR = "/run/scannerproject/waterfall"
WATERFALL_STATE_PATH = os.path.join(WATERFALL_STATE_DIR, "state.json")
WATERFALL_CONFIG_PATH = os.path.join(WATERFALL_STATE_DIR, "config.json")
WATERFALL_STALE_SEC = 5.0   # state.json older than this -> treat as down


def _waterfall_read_state() -> dict | None:
    """Best-effort read of waterfall state.json.

    Returns the parsed dict on success, or None if the file is missing /
    unreadable / older than WATERFALL_STALE_SEC.
    """
    try:
        st = os.stat(WATERFALL_STATE_PATH)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if (time.time() - st.st_mtime) > WATERFALL_STALE_SEC:
        return None
    try:
        with open(WATERFALL_STATE_PATH) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _waterfall_dongle_evidence_rows() -> list[dict]:
    """Build the per-dongle heartbeat evidence rows (A, B).

    On dropout we surface "DOWN since Nm ago - reconnecting in Ks" with
    status=bad.  Reconnect countdown is best-effort (we don't have the
    backoff timer exposed, so we compute it from the dongle.state and the
    age of state.json).
    """
    state = _waterfall_read_state()
    out: list[dict] = []
    labels = ["A", "B"]
    if state is None:
        # Service down or stale.  Show both as bad with a hint that the
        # service isn't writing state.
        for lbl in labels:
            out.append({
                "label": f"Waterfall dongle {lbl}",
                "value": "DOWN - waterfall service not running",
                "status": "bad",
            })
        return out
    dongles = state.get("dongles") if isinstance(state.get("dongles"), list) else []
    by_label = {d.get("label"): d for d in dongles if isinstance(d, dict)}
    for lbl in labels:
        d = by_label.get(lbl) or {}
        serial = str(d.get("serial") or "?")
        d_state = str(d.get("state") or "down")
        age_ms = d.get("last_frame_age_ms")
        if d_state == "ok" and isinstance(age_ms, (int, float)) and age_ms >= 0:
            out.append({
                "label": f"Waterfall dongle {lbl}",
                "value": f"live · {serial} · {int(age_ms)}ms",
                "status": "ok",
            })
        else:
            # Compute a coarse "down since" from the most recent state.json
            # write minus the last_frame_age_ms (if present).
            since_phrase = "recently"
            try:
                wf_mtime = os.stat(WATERFALL_STATE_PATH).st_mtime
                # When the dongle is down the watchdog stops updating the
                # frame age, so the most useful "since" is the difference
                # between state.json updates and the bad-read window.
                down_for_sec = max(0.0, time.time() - wf_mtime + 5.0)
                if down_for_sec < 60:
                    since_phrase = f"{int(down_for_sec)}s ago"
                elif down_for_sec < 3600:
                    since_phrase = f"{int(down_for_sec / 60)}m ago"
                else:
                    since_phrase = f"{int(down_for_sec / 3600)}h ago"
            except OSError:
                pass
            out.append({
                "label": f"Waterfall dongle {lbl}",
                "value": f"DOWN since {since_phrase} · reconnecting",
                "status": "bad",
            })
    return out


def _waterfall_pass_through_payload() -> dict:
    """GET /api/waterfall body — file-backed pass-through.

    If state.json is missing or stale (> WATERFALL_STALE_SEC), return a
    minimal "down" payload so the client doesn't see a 500.
    """
    state = _waterfall_read_state()
    if state is None:
        return {
            "state": "down",
            "reason": "service not running",
            "bins": [],
            "dongles": [],
        }
    return state


def _waterfall_write_config(
    center_mhz: float, bw_mhz: float | None = None
) -> tuple[bool, str]:
    """POST /api/waterfall body handler — atomically write config.json.

    Returns (ok, message).  Validates the requested center freq against
    the RTL-SDR tuning range (24 MHz - 1.7 GHz).  An optional bw_mhz is
    passed through (the waterfall service clamps it to its supported
    stitched range); a missing bw_mhz preserves the current bandwidth.
    """
    if not (24.0 <= center_mhz <= 1700.0):
        return False, f"center_mhz {center_mhz} out of range (24-1700)"
    try:
        os.makedirs(WATERFALL_STATE_DIR, exist_ok=True)
    except OSError as e:
        return False, f"mkdir failed: {e}"
    payload = {"center_mhz": float(center_mhz)}
    if bw_mhz is not None:
        if not (0.1 <= bw_mhz <= 60.0):
            return False, f"bw_mhz {bw_mhz} out of sane range (0.1-60)"
        payload["bw_mhz"] = float(bw_mhz)
    tmp = WATERFALL_CONFIG_PATH + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, WATERFALL_CONFIG_PATH)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False, f"write failed: {e}"
    return True, "ok"


# =====================================================================
# Phase 6b — VFO (single tunable RTL-SDR) file-backed pass-through.
#
# Mirrors the waterfall pattern: scripts/vfo.py owns the dongle and
# writes state.json once per loop; this module reads it.  POST
# /api/vfo merges into config.json which the script picks up via
# mtime-poll.  If state.json is missing or stale we surface a "down"
# stub rather than 500ing.
# =====================================================================

VFO_STATE_DIR = "/run/scannerproject/vfo"
VFO_STATE_PATH = os.path.join(VFO_STATE_DIR, "state.json")
VFO_CONFIG_PATH = os.path.join(VFO_STATE_DIR, "config.json")
VFO_STALE_SEC = 5.0
VFO_FREQ_MIN_MHZ = 24.0
VFO_FREQ_MAX_MHZ = 1700.0
VFO_VALID_MODS = ("am", "nfm", "wfm", "usb", "lsb")
# Phase 6b.2 — squelch + gain ranges (mirror scripts/vfo.py).
VFO_SQUELCH_MIN_DBFS = -80.0
VFO_SQUELCH_MAX_DBFS = 0.0
VFO_GAIN_MIN_DB = 0.0
VFO_GAIN_MAX_DB = 49.0

# Phase 6b.3 — BT routing.  Flipping the "Route to BT speaker" toggle ON
# triggers a bluetoothctl connect to VFO_BT_SPEAKER_MAC followed by
# starting scanner-vlc-vfo.service (the icecast→bluez VLC bridge).
# Flipping OFF stops the service but leaves the BT connection intact —
# other targets may be using it.
VFO_BT_SPEAKER_MAC = os.getenv("VFO_BT_SPEAKER_MAC", "C0:28:8D:34:6E:67").strip()
VFO_BT_CONNECT_TIMEOUT_SEC = 8.0
VFO_BT_POST_START_WAIT_SEC = 1.2


def _vfo_read_state() -> dict | None:
    """Best-effort read of /run/scannerproject/vfo/state.json.

    Returns the parsed dict, or None if missing/unreadable/older than
    VFO_STALE_SEC.  Mirrors _waterfall_read_state.
    """
    try:
        st = os.stat(VFO_STATE_PATH)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if (time.time() - st.st_mtime) > VFO_STALE_SEC:
        return None
    try:
        with open(VFO_STATE_PATH) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _vfo_read_config() -> dict | None:
    """Best-effort read of config.json so POST can merge into existing."""
    try:
        with open(VFO_CONFIG_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _vfo_dongle_evidence_row() -> dict:
    """Build the single VFO dongle heartbeat row.

    On healthy: "live . 80000003 . 22ms . 127.700 AM"
    On dropout: "DOWN since 4m ago . reconnecting in 8s"

    The reconnect countdown is best-effort — scripts/vfo.py doesn't
    surface its current backoff, so we round-estimate from how long
    state.json has been stale (capped at 60s like the script's backoff
    ceiling).
    """
    state = _vfo_read_state()
    if state is None:
        return {
            "label": "VFO dongle",
            "value": "DOWN - vfo service not running",
            "status": "bad",
        }
    dongle = state.get("dongle") if isinstance(state.get("dongle"), dict) else {}
    serial = str(dongle.get("serial") or state.get("dongle_serial") or "?")
    d_state = str(dongle.get("state") or state.get("state") or "down")
    age_ms = dongle.get("last_frame_age_ms")
    if age_ms is None:
        age_ms = state.get("last_frame_age_ms")
    freq_mhz = state.get("freq_mhz")
    mod = str(state.get("mod") or "?").upper()
    top_state = str(state.get("state") or "down").lower()

    if d_state == "ok" and top_state in ("ok", "degraded") and isinstance(age_ms, (int, float)) and age_ms >= 0:
        try:
            f_str = f"{float(freq_mhz):.3f}"
        except (TypeError, ValueError):
            f_str = "?"
        return {
            "label": "VFO dongle",
            "value": f"live . {serial} . {int(age_ms)}ms . {f_str} {mod}",
            "status": "ok",
        }
    # Down/degraded path.  Best-effort time-since estimate from
    # state.json mtime (which the script touches every ~250ms when
    # healthy, every loop when degraded).
    try:
        wf_mtime = os.stat(VFO_STATE_PATH).st_mtime
        down_secs = max(0.0, time.time() - wf_mtime)
    except OSError:
        down_secs = 0.0
    # Reconnect cadence: 5 -> 10 -> 20 -> 40 -> 60 s; the time until
    # the next attempt is best surfaced as min(60, down_secs) since
    # the user really just wants to know "is it still trying".
    cd = min(60, max(5, int(down_secs)))
    return {
        "label": "VFO dongle",
        "value": f"DOWN since {int(down_secs)}s ago . reconnecting in {cd}s",
        "status": "bad",
    }


def _vfo_pass_through_payload() -> dict:
    """GET /api/vfo body — file-backed pass-through, "down" stub on miss."""
    state = _vfo_read_state()
    if state is None:
        return {
            "state": "down",
            "reason": "service not running",
            "bins": [],
        }
    return state


def _vfo_bt_connect(mac: str = VFO_BT_SPEAKER_MAC) -> tuple[bool, str]:
    """Connect to the configured BT speaker via ``bluetoothctl connect``.

    Returns (ok, err).  ``bluetoothctl`` runs as the ``ubuntu`` user
    without sudo on this host (the user is in the bluetooth-capable
    groups).  Calling connect on an already-connected device is a
    no-op — bluetoothctl returns success quickly.  We additionally
    verify ``Connected: yes`` via ``bluetoothctl info`` because some
    bluetoothctl versions return rc 0 on a transient connect failure.
    """
    if not mac:
        return False, "no MAC configured (VFO_BT_SPEAKER_MAC)"
    try:
        res = subprocess.run(
            ["bluetoothctl", "connect", mac],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=VFO_BT_CONNECT_TIMEOUT_SEC, check=False,
        )
    except subprocess.TimeoutExpired:
        return False, (
            f"connect timed out after {VFO_BT_CONNECT_TIMEOUT_SEC:.0f}s "
            f"(speaker off?)"
        )
    except FileNotFoundError:
        return False, "bluetoothctl not installed"
    except Exception as exc:  # noqa: BLE001
        return False, f"connect exec error: {exc}"
    out = (res.stdout or "").strip()
    try:
        chk = subprocess.run(
            ["bluetoothctl", "info", mac],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=3.0, check=False,
        )
        if "Connected: yes" in (chk.stdout or ""):
            return True, ""
    except Exception:  # noqa: BLE001
        pass
    snippet = out.splitlines()[-1] if out else f"rc={res.returncode}"
    return False, snippet[:200]


def _vfo_write_config(patch: dict) -> tuple[bool, str, dict]:
    """POST /api/vfo body handler — merge patch into config.json atomically.

    Accepts any subset of {freq_mhz, mod, muted, bt_routed}.  Validates
    freq range + mod choice.  USB/LSB are accepted as mod values but
    the worker stubs them (Phase 6b.1).  Returns (ok, msg, applied).
    """
    existing = _vfo_read_config() or {
        "freq_mhz": 127.700, "mod": "am", "muted": False, "bt_routed": False,
        "squelch_dbfs": -60.0, "squelch_auto": False, "gain_db": 40.0,
    }

    merged = dict(existing)
    if "freq_mhz" in patch:
        try:
            f = float(patch["freq_mhz"])
        except (TypeError, ValueError) as e:
            return False, f"invalid freq_mhz: {e}", {}
        if not (VFO_FREQ_MIN_MHZ <= f <= VFO_FREQ_MAX_MHZ):
            return False, (
                f"freq_mhz {f} out of range ({VFO_FREQ_MIN_MHZ}-{VFO_FREQ_MAX_MHZ})"
            ), {}
        merged["freq_mhz"] = f
    if "mod" in patch:
        m = str(patch["mod"]).lower()
        if m not in VFO_VALID_MODS:
            return False, f"invalid mod {m!r}: must be one of {VFO_VALID_MODS}", {}
        merged["mod"] = m
    if "muted" in patch:
        merged["muted"] = bool(patch["muted"])
    if "bt_routed" in patch:
        merged["bt_routed"] = bool(patch["bt_routed"])
    # Phase 6b.2 — squelch + gain.  Range-validated; out-of-range
    # values are 400'd rather than silently clamped, so the UI can
    # surface the mistake to the operator.
    if "squelch_dbfs" in patch:
        try:
            v = float(patch["squelch_dbfs"])
        except (TypeError, ValueError) as e:
            return False, f"invalid squelch_dbfs: {e}", {}
        if not (VFO_SQUELCH_MIN_DBFS <= v <= VFO_SQUELCH_MAX_DBFS):
            return False, (
                f"squelch_dbfs {v} out of range ({VFO_SQUELCH_MIN_DBFS}-{VFO_SQUELCH_MAX_DBFS})"
            ), {}
        merged["squelch_dbfs"] = v
    if "squelch_auto" in patch:
        merged["squelch_auto"] = bool(patch["squelch_auto"])
    if "gain_db" in patch:
        try:
            v = float(patch["gain_db"])
        except (TypeError, ValueError) as e:
            return False, f"invalid gain_db: {e}", {}
        if not (VFO_GAIN_MIN_DB <= v <= VFO_GAIN_MAX_DB):
            return False, (
                f"gain_db {v} out of range ({VFO_GAIN_MIN_DB}-{VFO_GAIN_MAX_DB})"
            ), {}
        merged["gain_db"] = v

    # Phase 6b.3 — if bt_routed flipped, do the BT + VLC bridge side
    # effects BEFORE persisting config.  Going ON requires both
    # bluetoothctl-connect AND scanner-vlc-vfo.service-start to succeed;
    # if either fails we return an error WITHOUT writing config so the
    # UI doesn't lie about bt_routed=True.  Going OFF stops the bridge
    # (best-effort) but leaves the BT connection intact — other targets
    # may still be using the speaker.
    bt_was = bool(existing.get("bt_routed"))
    bt_now = bool(merged.get("bt_routed"))
    bt_flipped = ("bt_routed" in patch) and (bt_was != bt_now)
    if bt_flipped and bt_now:
        bt_ok, bt_err = _vfo_bt_connect()
        if not bt_ok:
            return False, f"bluetooth connect failed: {bt_err}", {}
        try:
            from ui import vlc as _vlc_mod
            unit = _vlc_mod._VLC_SYSTEMD_SERVICES.get(
                "vfo", "scanner-vlc-vfo.service",
            )
            svc_ok, svc_err = _vlc_mod._systemd_service_ctl(unit, "start")
            if not svc_ok:
                return False, f"vlc-vfo service start failed: {svc_err}", {}
        except Exception as exc:  # noqa: BLE001
            return False, f"vlc-vfo wire-up error: {exc}", {}
        # Give the bridge a beat to attach to the bluez sink before we
        # acknowledge success — gives the UI a stable handover.
        time.sleep(VFO_BT_POST_START_WAIT_SEC)
    elif bt_flipped and not bt_now:
        try:
            from ui import vlc as _vlc_mod
            unit = _vlc_mod._VLC_SYSTEMD_SERVICES.get(
                "vfo", "scanner-vlc-vfo.service",
            )
            svc_ok, svc_err = _vlc_mod._systemd_service_ctl(unit, "stop")
            if not svc_ok:
                logger.warning("scanner-vlc-vfo stop failed: %s", svc_err)
        except Exception as exc:  # noqa: BLE001
            logger.warning("scanner-vlc-vfo stop error: %s", exc)

    try:
        os.makedirs(VFO_STATE_DIR, exist_ok=True)
    except OSError as e:
        return False, f"mkdir failed: {e}", {}

    tmp = VFO_CONFIG_PATH + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "w") as f:
            json.dump(merged, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, VFO_CONFIG_PATH)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False, f"write failed: {e}", {}

    return True, "ok", merged



# =====================================================================
# Phase 6c — Disco (N-dongle unified sweep) file-backed pass-through.
#
# scripts/disco_coordinator.py owns coord_state.json (composite bins +
# per-dongle status + classified detections) and reads coord_config.json
# (user-set frequency range + dongle serial list).  GET /api/disco is a
# pass-through; POST /api/disco/range validates + atomic-writes the
# range portion of the config which the coordinator picks up via re-read
# on its next tick.
# =====================================================================

DISCO_STATE_DIR = "/run/scannerproject/disco"
DISCO_STATE_PATH = os.path.join(DISCO_STATE_DIR, "coord_state.json")
DISCO_CONFIG_PATH = os.path.join(DISCO_STATE_DIR, "coord_config.json")
DISCO_STALE_SEC = 5.0
DISCO_FREQ_MIN_MHZ = 24.0
DISCO_FREQ_MAX_MHZ = 1700.0
DISCO_MIN_SPAN_MHZ = 1.0


def _disco_read_state() -> dict | None:
    """Best-effort read of coord_state.json. Returns None if missing,
    unreadable, or older than DISCO_STALE_SEC. Mirrors waterfall/VFO."""
    try:
        st = os.stat(DISCO_STATE_PATH)
    except (FileNotFoundError, OSError):
        return None
    if (time.time() - st.st_mtime) > DISCO_STALE_SEC:
        return None
    try:
        with open(DISCO_STATE_PATH) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _disco_read_config() -> dict | None:
    """Read coord_config.json so POST can merge into existing."""
    try:
        with open(DISCO_CONFIG_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _disco_pass_through_payload() -> dict:
    """GET /api/disco body — coord_state.json or 'down' stub."""
    state = _disco_read_state()
    if state is None:
        return {
            "state": "down",
            "reason": "service not running",
            "bins": [],
            "dongles": [],
            "detections": [],
            "range": {"start_mhz": 117.0, "end_mhz": 470.0},
        }
    return state


# ---------------------------------------------------------------------
# GET /api/disco/recent — operator hits-history feed.
#
# Backs the sb5 Discovery card's "Fullscreen" mode (table of the last
# N unique hits).  Reads disco-coordinator's SQLite DB directly in
# read-only mode and returns either:
#   - unique=true (default): deduped per 25-kHz bin within a 24h
#     window, sorted newest-first; each row carries occurrence count
#     + the most-recent classified label.
#   - unique=false: raw rows, newest first, capped at limit.
#
# Query params (validated, all optional):
#   limit  int 1..500   default 100
#   unique bool          default true
# Backend is fully decoupled from the active sweep range so the table
# shows everything the classifier has seen, not just what's in view.
# ---------------------------------------------------------------------
DISCO_DB_PATH = os.environ.get(
    "DISCO_DB", "/home/ubuntu/scannerproject/disco/state/disco.sqlite",
)
DISCO_RECENT_WINDOW_S = 24 * 3600.0     # 24 hours of history
DISCO_RECENT_LIMIT_MAX = 500
DISCO_RECENT_BIN_HZ = 25_000            # dedupe granularity


def _disco_recent_hits(limit: int = 100, unique: bool = True) -> dict:
    """Return recent disco detections from the SQLite DB.

    Schema (see scripts/disco_coordinator.py / disco/src/*):
      detections(id, ts, tuner_id, freq_hz, bandwidth_hz, power_dbfs,
                 snr_db, modulation_class, modulation_confidence,
                 protocol_tag, slice_path, classified_ts, interpretation,
                 ...)

    Returns dict with keys: rows (list of normalised hit dicts),
    window_s, server_time, source ("db"|"down"), error (optional).
    """
    import sqlite3
    limit = max(1, min(int(limit or 100), DISCO_RECENT_LIMIT_MAX))
    now = time.time()
    cutoff = now - DISCO_RECENT_WINDOW_S
    out: dict = {
        "rows": [],
        "window_s": DISCO_RECENT_WINDOW_S,
        "server_time": now,
        "source": "db",
        "unique": bool(unique),
        "limit": limit,
    }
    try:
        conn = sqlite3.connect(
            f"file:{DISCO_DB_PATH}?mode=ro", uri=True, timeout=2.0,
        )
    except Exception as exc:
        out["source"] = "down"
        out["error"] = f"db open failed: {exc}"
        return out
    try:
        # Pull a larger working set so dedupe doesn't starve narrower
        # neighbours (an FM broadcast row spans many bins otherwise).
        fetch_limit = limit * 12 if unique else limit
        try:
            rows = conn.execute(
                "SELECT ts, freq_hz, bandwidth_hz, power_dbfs, snr_db, "
                "modulation_class, modulation_confidence, protocol_tag, "
                "interpretation, classified_ts "
                "FROM detections "
                "WHERE ts >= ? "
                "ORDER BY ts DESC LIMIT ?",
                (cutoff, fetch_limit),
            ).fetchall()
        except Exception as exc:
            out["source"] = "down"
            out["error"] = f"query failed: {exc}"
            return out
    finally:
        try: conn.close()
        except Exception: pass

    def _row_to_label(mod_cls, conf, proto, interp, has_class):
        if not has_class:
            return "pending"
        bits = [str(mod_cls)]
        if proto and proto != mod_cls:
            bits.append(f"[{proto}]")
        s = " ".join(bits)
        if interp:
            s = f"{s} - {str(interp)[:80]}"
        return s

    if not unique:
        for r in rows[:limit]:
            (ts, freq_hz, bw_hz, p_db, snr, mod_cls, conf,
             proto, interp, classified_ts) = r
            has_class = (classified_ts is not None and mod_cls
                         and mod_cls != "unclassified")
            out["rows"].append({
                "ts": float(ts or 0.0),
                "freq_mhz": round(float(freq_hz or 0.0) / 1e6, 4),
                "classification": _row_to_label(
                    mod_cls, conf, proto, interp, has_class),
                "classified": bool(has_class),
                "confidence": round(float(conf or 0.0), 3) if has_class else 0.0,
                "snr_db": round(float(snr or 0.0), 1),
                "power_dbfs": round(float(p_db or 0.0), 1),
                "bandwidth_khz": round(float(bw_hz or 0.0) / 1e3, 2),
                "last_seen_s_ago": int(max(0.0, now - float(ts or now))),
                "count": 1,
            })
        return out

    # Dedupe by 25-kHz bin while keeping most-recent ts, best SNR, and
    # the first classified sighting we see (rows are DESC ts so that's
    # the most-recent classified label).
    by_bin: dict = {}
    order: list = []
    for r in rows:
        (ts, freq_hz, bw_hz, p_db, snr, mod_cls, conf,
         proto, interp, classified_ts) = r
        if freq_hz is None or ts is None:
            continue
        key = int(float(freq_hz) // DISCO_RECENT_BIN_HZ)
        slot = by_bin.get(key)
        has_class = (classified_ts is not None and mod_cls
                     and mod_cls != "unclassified")
        if slot is None:
            slot = {
                "ts": float(ts),
                "freq_hz": float(freq_hz),
                "bandwidth_hz": float(bw_hz or 0.0),
                "power_dbfs": float(p_db or 0.0),
                "snr_db": float(snr or 0.0),
                "mod_cls": mod_cls if has_class else None,
                "confidence": float(conf or 0.0) if has_class else 0.0,
                "proto": proto if has_class else None,
                "interp": str(interp)[:80] if interp else None,
                "count": 1,
            }
            by_bin[key] = slot
            order.append(key)
            continue
        slot["count"] += 1
        if float(ts) > slot["ts"]:
            slot["ts"] = float(ts)
        s = float(snr or 0.0)
        if s > slot["snr_db"]:
            slot["snr_db"] = s
            slot["power_dbfs"] = float(p_db or slot["power_dbfs"])
            slot["bandwidth_hz"] = float(bw_hz or slot["bandwidth_hz"])
        if has_class and slot["mod_cls"] is None:
            slot["mod_cls"] = mod_cls
            slot["confidence"] = float(conf or 0.0)
            slot["proto"] = proto
            if interp:
                slot["interp"] = str(interp)[:80]

    uniq_rows = []
    for slot in by_bin.values():
        is_classified = slot["mod_cls"] is not None
        uniq_rows.append({
            "ts": slot["ts"],
            "freq_mhz": round(slot["freq_hz"] / 1e6, 4),
            "classification": _row_to_label(
                slot["mod_cls"], slot["confidence"], slot["proto"],
                slot["interp"], is_classified),
            "classified": bool(is_classified),
            "confidence": round(slot["confidence"], 3),
            "snr_db": round(slot["snr_db"], 1),
            "power_dbfs": round(slot["power_dbfs"], 1),
            "bandwidth_khz": round(slot["bandwidth_hz"] / 1e3, 2),
            "last_seen_s_ago": int(max(0.0, now - slot["ts"])),
            "count": int(slot["count"]),
        })
    uniq_rows.sort(key=lambda d: d["last_seen_s_ago"])
    out["rows"] = uniq_rows[:limit]
    return out



def _disco_write_range(start_mhz: float, end_mhz: float) -> tuple[bool, str]:
    """POST /api/disco/range — validate and atomically merge into config."""
    if not (DISCO_FREQ_MIN_MHZ <= start_mhz <= DISCO_FREQ_MAX_MHZ):
        return False, f"start_mhz {start_mhz} out of range ({DISCO_FREQ_MIN_MHZ}-{DISCO_FREQ_MAX_MHZ})"
    if not (DISCO_FREQ_MIN_MHZ <= end_mhz <= DISCO_FREQ_MAX_MHZ):
        return False, f"end_mhz {end_mhz} out of range ({DISCO_FREQ_MIN_MHZ}-{DISCO_FREQ_MAX_MHZ})"
    if end_mhz <= start_mhz:
        return False, "end_mhz must be > start_mhz"
    if (end_mhz - start_mhz) < DISCO_MIN_SPAN_MHZ:
        return False, f"span must be >= {DISCO_MIN_SPAN_MHZ} MHz"
    existing = _disco_read_config() or {}
    serials = existing.get("dongle_serials") or ["45469635", "61108285"]
    merged = {
        "range": {"start_mhz": float(start_mhz), "end_mhz": float(end_mhz)},
        "dongle_serials": list(serials),
    }
    try:
        os.makedirs(DISCO_STATE_DIR, exist_ok=True)
    except OSError as e:
        return False, f"mkdir failed: {e}"
    tmp = DISCO_CONFIG_PATH + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "w") as f:
            json.dump(merged, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, DISCO_CONFIG_PATH)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False, f"write failed: {e}"
    return True, "ok"


def _disco_dongle_evidence_rows() -> list[dict]:
    """Build per-Disco-dongle heartbeat rows from coord_state.json.

    Healthy: "live . 45469635 . 117-293 MHz . 22ms"
    Dropped: "DOWN since 4m ago . reconnecting in 16s"

    Separator matches the Phase 6b VFO row style (ASCII '.').
    """
    state = _disco_read_state()
    out: list[dict] = []
    if state is None:
        # Service down or stale — surface two bad rows so the user sees
        # the coordinator isn't writing state.
        for i in (1, 2):
            out.append({
                "label": f"Disco dongle {i}",
                "value": "DOWN - disco coordinator not running",
                "status": "bad",
            })
        return out
    dongles = state.get("dongles") if isinstance(state.get("dongles"), list) else []
    # Render up to len(dongles); if fewer than two, still render 2 rows
    # so the heartbeat shape is predictable.
    n = max(2, len(dongles))
    for i in range(n):
        d = dongles[i] if i < len(dongles) and isinstance(dongles[i], dict) else None
        label = f"Disco dongle {i + 1}"
        if d is None:
            out.append({
                "label": label,
                "value": "not configured",
                "status": "info",
            })
            continue
        serial = str(d.get("serial") or "?")
        d_state = str(d.get("state") or "down")
        age_ms = d.get("last_frame_age_ms")
        sub = d.get("sub_range_mhz") or []
        try:
            sub_lo = int(round(float(sub[0]))) if len(sub) > 0 else 0
            sub_hi = int(round(float(sub[1]))) if len(sub) > 1 else 0
            sub_str = f"{sub_lo}-{sub_hi} MHz"
        except (TypeError, ValueError):
            sub_str = "?-? MHz"
        if d_state == "ok" and isinstance(age_ms, (int, float)) and age_ms >= 0:
            out.append({
                "label": label,
                "value": f"live . {serial} . {sub_str} . {int(age_ms)}ms",
                "status": "ok",
            })
        else:
            # Best-effort "DOWN since" from coord_state mtime.
            try:
                st = os.stat(DISCO_STATE_PATH)
                down_secs = max(0.0, time.time() - st.st_mtime)
            except OSError:
                down_secs = 0.0
            if down_secs < 60:
                since = f"{int(down_secs)}s ago"
            elif down_secs < 3600:
                since = f"{int(down_secs / 60)}m ago"
            else:
                since = f"{int(down_secs / 3600)}h ago"
            cd = min(60, max(5, int(down_secs)))
            out.append({
                "label": label,
                "value": f"DOWN since {since} . reconnecting in {cd}s . {serial}",
                "status": "bad",
            })
    return out


# =====================================================================
# Phase 6d — tuner broker (Disco <-> ACARS/VDL2 ownership swap).
#
# scripts/tuner_broker.py owns /run/scannerproject/broker/state.json and
# reads /run/scannerproject/broker/mode.json.  The UI exposes:
#   GET  /api/sounding -> broker state (current ownership)
#   POST /api/sounding -> writes mode.json; broker swaps within ~500ms
# Heartbeat surfaces per-dongle role via _broker_aware_dongle_rows()
# which replaces _disco_dongle_evidence_rows() so a swapped dongle
# reads e.g. "Sounding (ACARS) . 61108285 . 131.550 MHz . 12ms" rather
# than the disco coordinator's stale "DOWN" view.
# =====================================================================

BROKER_STATE_DIR = "/run/scannerproject/broker"
BROKER_MODE_PATH = os.path.join(BROKER_STATE_DIR, "mode.json")
BROKER_STATE_PATH = os.path.join(BROKER_STATE_DIR, "state.json")
BROKER_STALE_SEC = 5.0


def _broker_read_state() -> dict | None:
    """Best-effort read of broker state.json. Returns None if missing,
    unreadable, or older than BROKER_STALE_SEC."""
    try:
        st = os.stat(BROKER_STATE_PATH)
    except (FileNotFoundError, OSError):
        return None
    if (time.time() - st.st_mtime) > BROKER_STALE_SEC:
        return None
    try:
        with open(BROKER_STATE_PATH) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _broker_pass_through_payload() -> dict:
    """GET /api/sounding body — broker state or 'down' stub on miss."""
    state = _broker_read_state()
    if state is None:
        return {
            "sounding": False,
            "state": "down",
            "reason": "broker not running",
            "dongles": [],
        }
    out = {
        "sounding": bool(state.get("sounding", False)),
        "state": "ok",
        "dongles": state.get("dongles") or [],
        "updated_ts": state.get("updated_ts"),
        "last_transition_ts": state.get("last_transition_ts"),
    }
    if state.get("last_error"):
        out["last_error"] = state["last_error"]
    return out


def _broker_write_mode(sounding: bool) -> tuple[bool, str]:
    """POST /api/sounding body handler — atomically write mode.json.
    Broker (running as root) picks up the change on its next mtime poll
    (within ~500ms)."""
    try:
        os.makedirs(BROKER_STATE_DIR, exist_ok=True)
    except OSError as e:
        return False, f"mkdir failed: {e}"
    payload = {"sounding": bool(sounding)}
    tmp = BROKER_MODE_PATH + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, BROKER_MODE_PATH)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False, f"write failed: {e}"
    return True, "ok"


# ---------------------------------------------------------------------
# Sounding-mode evidence sources — when a Disco dongle is loaned out to
# ACARS or VDL2, we read live evidence from the consumer's output file
# rather than the (now-stale) disco coord_state.json.  Fallback values
# are sensible defaults when the file is missing.
# ---------------------------------------------------------------------
_ACARS_OUTPUT_PATH = "/run/acars_output.json"
_VDL2_OUTPUT_PATH = "/run/vdl2_output.json"


def _file_age_ms(path: str) -> int | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return int(max(0.0, time.time() - st.st_mtime) * 1000)


def _sounding_evidence_for_role(role: str, serial: str) -> tuple[str, str]:
    """Return (value_string, status) for a dongle in a sounding role.
    Reads the consumer output file for a freshness signal.  The serial
    is *not* repeated in the value because the row label already
    contains it ("Dongle <serial>")."""
    role = (role or "").lower()
    if role == "acars":
        age = _file_age_ms(_ACARS_OUTPUT_PATH)
        if age is None:
            return ("Sounding (ACARS) . 131.550 MHz . waiting", "warn")
        # acarsdec writes one JSON line per message; output file mtime
        # advances on each message.  A quiet channel can be many minutes
        # between messages — we surface the age in seconds rather than
        # ms once it exceeds 5s so the row doesn't look broken.
        if age < 5000:
            age_str = f"{age}ms"
        elif age < 60_000:
            age_str = f"{age // 1000}s"
        else:
            age_str = f"{age // 60_000}m"
        return (f"Sounding (ACARS) . 131.550 MHz . {age_str}", "ok")
    if role == "vdl2":
        age = _file_age_ms(_VDL2_OUTPUT_PATH)
        if age is None:
            return ("Sounding (VDL2) . 136.975 MHz . waiting", "warn")
        if age < 5000:
            age_str = f"{age}ms"
        elif age < 60_000:
            age_str = f"{age // 1000}s"
        else:
            age_str = f"{age // 60_000}m"
        return (f"Sounding (VDL2) . 136.975 MHz . {age_str}", "ok")
    return (f"Sounding ({role})", "info")


def _broker_aware_dongle_rows() -> list[dict]:
    """Heartbeat rows that reflect live broker ownership.

    When sounding is OFF, each dongle uses the Disco evidence row.  When
    sounding is ON and a dongle has been loaned to ACARS/VDL2, we replace
    that dongle's row with a sounding-consumer-evidence row.  This avoids
    surfacing the disco-coordinator's "DOWN" view for a dongle that's
    intentionally not feeding the coordinator.
    """
    broker = _broker_read_state()
    disco_rows = _disco_dongle_evidence_rows()
    if broker is None:
        # Broker not running — fall back to the raw disco rows so we
        # don't lose the heartbeat entirely on broker outage.
        return disco_rows
    broker_dongles = broker.get("dongles") or []
    if not broker_dongles:
        return disco_rows
    # Map disco row index -> broker serial via positional alignment is
    # fragile; instead, parse the serial out of each disco row's value
    # string ("live . <serial> . ..." or "DOWN ... <serial>") and
    # rebuild rows.  When in doubt, keep the disco row.
    out: list[dict] = []
    # We need parallel ordering: build rows keyed by broker policy
    # serial order so a swapped dongle appears alongside the unswapped.
    # We assume broker_dongles ordering is stable (broker emits them in
    # policy order).
    for bd in broker_dongles:
        serial = str(bd.get("serial") or "")
        role = str(bd.get("current_role") or "").lower()
        # Phase 6d — label is "Dongle <serial>" so the heartbeat shows
        # the live owner per-dongle.  Replaces the Phase 6c "Disco
        # dongle N" rows entirely.
        label = f"Dongle {serial}" if serial else "Dongle ?"
        if role == "disco":
            # Use the disco coordinator's row for this serial if present.
            match = None
            for r in disco_rows:
                val = str(r.get("value") or "")
                if serial and serial in val:
                    match = r
                    break
            if match is not None:
                # Strip the leading "live . <serial> . " redundancy so
                # the row reads "Disco · 64-241 MHz · 283ms".
                v = str(match.get("value") or "")
                prefix = f"live . {serial} . "
                if v.startswith(prefix):
                    v = "Disco . " + v[len(prefix):]
                out.append({"label": label, "value": v, "status": match.get("status", "ok")})
            else:
                out.append({
                    "label": label,
                    "value": f"Disco . (no coord data)",
                    "status": "warn",
                })
        else:
            # Sounding consumer owns this dongle.
            value, status = _sounding_evidence_for_role(role, serial)
            out.append({"label": label, "value": value, "status": status})
    if not out:
        return disco_rows
    return out


# =====================================================================
# Phase 7a — /api/sitrep: aggregated operator situation report.
#
# The Sitrep button in the /sb5 topbar opens a modal that needs more
# context than the raw heartbeat evidence rows.  This endpoint
# aggregates the data the modal renders so the client only does one
# fetch and a single render pass.  All sub-sources are read-only and
# already cached upstream, so the cost is bounded.
# =====================================================================

# Services the operator cares about for "is the scanner alive" — these
# are the units whose state lights up the service-health grid in the
# Sitrep modal.  Order matters: it's the painting order in the modal.
# Static "always-listed" Sitrep services.  The analog demod rows (rtl-
# airband vs gr-demod) are computed at request time by
# ``_sitrep_active_service_units`` below so the SB5_USE_GR_DEMOD flag
# can swap them in/out without an app restart.
_SITREP_SERVICE_UNITS_STATIC_HEAD: tuple[tuple[str, str], ...] = (
    ("airband-ui",                  "airband-ui.service"),
)
_SITREP_SERVICE_UNITS_STATIC_TAIL: tuple[tuple[str, str], ...] = (
    ("scanner-digital-op25",        "scanner-digital-op25.service"),
    ("scanner-digital-op25-audio",  "scanner-digital-op25-audio.service"),
    ("scanner-waterfall",           "scanner-waterfall.service"),
    ("scanner-vfo",                 "scanner-vfo.service"),
    ("scanner-tuner-broker",        "scanner-tuner-broker.service"),
    ("disco-coordinator",           "disco-coordinator.service"),
    ("dumpvdl2",                    "dumpvdl2.service"),
    ("acarsdec",                    "acarsdec.service"),
    ("radiosonde-auto-rx",          "radiosonde-auto-rx.service"),
    ("scanner-vlc-digital",         "scanner-vlc-digital.service"),
    ("scanner-vlc-analog",          "scanner-vlc-analog.service"),
    ("scanner-vlc-ground",          "scanner-vlc-ground.service"),
    ("scanner-vlc-vfo",             "scanner-vlc-vfo.service"),
    ("icecast2",                    "icecast2.service"),
)


def _sitrep_active_service_units() -> tuple[tuple[str, str], ...]:
    """Return the per-request Sitrep service list.

    Phase 4d: under ``SB5_USE_GR_DEMOD=true`` we swap the rtl-airband
    rows for the chirp ``gr-demod@{airband,ground}`` rows.  When the
    flag is off (the legacy production path), the list is identical to
    the pre-Phase-4d static tuple so nothing changes for that audience.
    """
    try:
        chirp_on = bool(_chirp_use_gr_demod())
    except Exception:
        chirp_on = False
    if chirp_on:
        analog_rows = (
            ("gr-demod@airband", "gr-demod@airband.service"),
            ("gr-demod@ground",  "gr-demod@ground.service"),
        )
    else:
        analog_rows = (
            ("rtl-airband-airband", "rtl-airband-airband.service"),
            ("rtl-airband-ground",  "rtl-airband-ground.service"),
        )
    return (
        _SITREP_SERVICE_UNITS_STATIC_HEAD
        + analog_rows
        + _SITREP_SERVICE_UNITS_STATIC_TAIL
    )


# Back-compat alias preserved for any callers (tests, other ui modules)
# that import the constant directly.  Snapshot of the *flag-off* layout
# — matches the pre-Phase-4d ordering exactly.
_SITREP_SERVICE_UNITS: tuple[tuple[str, str], ...] = (
    _SITREP_SERVICE_UNITS_STATIC_HEAD
    + (
        ("rtl-airband-airband", "rtl-airband-airband.service"),
        ("rtl-airband-ground",  "rtl-airband-ground.service"),
    )
    + _SITREP_SERVICE_UNITS_STATIC_TAIL
)


def _sitrep_services() -> list[dict]:
    """Active-state for each operator-relevant service.

    Units not installed on this host are surfaced as status `ok` with
    ``active=False`` and ``installed=False`` so the modal can render them
    as "not configured" rather than red-flagging the grid.
    """
    rows = []
    for label, unit in _sitrep_active_service_units():
        try:
            exists = _unit_exists_cached(unit)
        except Exception:
            exists = True
        if not exists:
            rows.append({
                "label": label,
                "unit": unit,
                "active": False,
                "installed": False,
                "status": "ok",
            })
            continue
        try:
            active = _unit_active_cached(unit)
        except Exception:
            active = False
        if active:
            rows.append({
                "label": label,
                "unit": unit,
                "active": True,
                "installed": True,
                "status": "ok",
            })
            continue
        # H2 (2026-06-03): mirror the heartbeat's is-enabled logic so the
        # sitrep modal also marks intentionally-disabled units as ok rather
        # than bad. Otherwise the modal would still red-flag units that the
        # main heartbeat now considers healthy.
        try:
            enabled_state = _unit_enabled_state_cached(unit)
        except Exception:
            enabled_state = "unknown"
        intentionally_off = enabled_state in _UNIT_INTENTIONALLY_OFF_STATES
        rows.append({
            "label": label,
            "unit": unit,
            "active": False,
            "installed": True,
            "status": "ok" if intentionally_off else "bad",
            "enabled_state": enabled_state,
            "intentionally_off": intentionally_off,
        })
    return rows


def _sitrep_dongles() -> list[dict]:
    """Per-dongle serial + role + live state.

    Sources: tuner broker (Disco/Sounding owners), waterfall state,
    VFO state, and rtl-airband combined-config (analog + ground)."""
    rows: list[dict] = []
    seen: set[str] = set()

    def _push(serial: str, role: str, status: str, detail: str = "") -> None:
        s = str(serial or "").strip()
        if not s or s in seen:
            return
        seen.add(s)
        rows.append({
            "serial": s,
            "role": role,
            "status": status,
            "detail": detail,
        })

    # Broker — disco/sounding owners.
    broker = _broker_read_state()
    if isinstance(broker, dict):
        for bd in (broker.get("dongles") or []):
            if not isinstance(bd, dict):
                continue
            serial = str(bd.get("serial") or "")
            role = str(bd.get("current_role") or "").lower() or "disco"
            _push(serial, role, "ok", str(bd.get("current_service") or ""))

    # Waterfall A / B and VFO via the broker-aware/heartbeat rows.
    wf_rows = _waterfall_dongle_evidence_rows()
    for wr in wf_rows:
        val = str(wr.get("value") or "")
        # Format: "live · <serial> · ..." or "DOWN since ..."
        serial = ""
        for tok in val.replace("·", ".").split("."):
            tok = tok.strip()
            if tok.isdigit() and len(tok) >= 6:
                serial = tok
                break
        role = "waterfall-a" if "A" in str(wr.get("label") or "") else "waterfall-b"
        _push(serial, role, str(wr.get("status") or "ok"), val)

    vfo_row = _vfo_dongle_evidence_row()
    if isinstance(vfo_row, dict):
        val = str(vfo_row.get("value") or "")
        serial = ""
        for tok in val.replace("·", ".").split("."):
            tok = tok.strip()
            if tok.isdigit() and len(tok) >= 6:
                serial = tok
                break
        _push(serial, "vfo", str(vfo_row.get("status") or "ok"), val)

    # Analog + ground RSPduo dongle status.  Under SB5_USE_GR_DEMOD=true
    # the source of truth is the chirp daemons' own ``get_status`` (one
    # row per band, both sharing the same physical RSPduo serial).
    # Under the flag-off legacy path, read from the rtl-airband combined
    # config — unchanged behaviour.
    try:
        _chirp_on = bool(_chirp_use_gr_demod())
    except Exception:
        _chirp_on = False
    if _chirp_on:
        # Hit each chirp daemon's UDP get_status and emit rows keyed on
        # the SDR serial advertised by the daemon (sdr_device_args
        # contains 'serial=<n>').  Active iff the daemon's icecast_state
        # is connected.
        for _label, _factory in (
            ("airband", _chirp_airband_client),
            ("ground",  _chirp_ground_client),
        ):
            try:
                _client = _factory()
                _snap = _client.get_status() if _client is not None else {}
            except Exception:
                _snap = {}
            # Extract serial from sdr_device_args; fall back to "?".
            _serial = ""
            _args = str(_snap.get("sdr_device_args") or "")
            for _tok in _args.split(","):
                _tok = _tok.strip()
                if _tok.startswith("serial="):
                    _serial = _tok[len("serial="):]
                    break
            _ic_ok = (str(_snap.get("icecast_state") or "")
                      == "connected")
            _push(_serial, _label,
                  "ok" if _ic_ok else "bad",
                  f"chirp gr-demod@{_label}")
    else:
        # Legacy rtl-airband path.  Untouched behaviour.
        try:
            combined = combined_device_summary()
        except Exception:
            combined = {}
        airband = combined.get("airband") if isinstance(combined, dict) else None
        ground = combined.get("ground") if isinstance(combined, dict) else None
        if isinstance(airband, dict):
            _push(str(airband.get("serial") or ""), "airband",
                  "ok" if airband.get("active") else "bad", "rtl-airband-airband")
        if isinstance(ground, dict):
            _push(str(ground.get("serial") or ""), "ground",
                  "ok" if ground.get("active") else "bad", "rtl-airband-ground")

    # VDL2 dedicated dongle from env (does not flow through broker).
    vdl2_serial = ""
    try:
        with open("/etc/airband-ui.conf", "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("VDL2_RTL_SERIAL="):
                    vdl2_serial = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    except OSError:
        vdl2_serial = ""
    if vdl2_serial:
        try:
            vdl2_active = _unit_active_cached("dumpvdl2.service")
        except Exception:
            vdl2_active = False
        _push(vdl2_serial, "vdl2",
              "ok" if vdl2_active else "bad",
              "dumpvdl2.service")
    return rows


def _tail_jsonl_line(path: str, max_bytes: int = 8192) -> dict | None:
    """Return the last JSON object written to ``path`` (one-line-per-record).

    dumpvdl2 and acarsdec both append one JSON object per decoded message.
    To stay bounded on long-running files we seek to ``max_bytes`` from end
    and parse the final line.  Returns None on any error / empty file.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    if st.st_size <= 0:
        return None
    try:
        with open(path, "rb") as fh:
            read_from = max(0, st.st_size - int(max_bytes))
            fh.seek(read_from)
            tail = fh.read()
    except OSError:
        return None
    # Drop a partial first line if we started mid-record.
    lines = tail.splitlines()
    while lines and lines[-1].strip() == b"":
        lines.pop()
    if not lines:
        return None
    raw = lines[-1].decode("utf-8", errors="replace").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _count_jsonl_recent(path: str, window_sec: float) -> int:
    """Count lines in ``path`` whose mtime/atime falls within ``window_sec``.

    For acarsdec / dumpvdl2 we use line count over the recent tail as a
    proxy for decode rate — exact per-message timestamps require parsing
    each row which is too expensive for the UI poll.  The tail size is
    capped at 64KB so heavy decoders don't stall the request.
    """
    try:
        st = os.stat(path)
    except OSError:
        return 0
    if st.st_size <= 0:
        return 0
    age = max(0.0, time.time() - st.st_mtime)
    if age > float(window_sec):
        return 0
    try:
        with open(path, "rb") as fh:
            tail_size = min(65536, st.st_size)
            fh.seek(max(0, st.st_size - tail_size))
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0


def _vdl2_summarize_message(msg: dict | None) -> str:
    """Produce a one-line operator-readable summary of a dumpvdl2 record."""
    if not isinstance(msg, dict):
        return ""
    vdl2 = msg.get("vdl2") if isinstance(msg.get("vdl2"), dict) else msg
    avlc = vdl2.get("avlc") if isinstance(vdl2.get("avlc"), dict) else {}
    src = (avlc.get("src") or {}) if isinstance(avlc, dict) else {}
    dst = (avlc.get("dst") or {}) if isinstance(avlc, dict) else {}
    parts = []
    if src.get("addr"):
        parts.append(f"src {src.get('addr')}")
    if dst.get("addr"):
        parts.append(f"dst {dst.get('addr')}")
    acars = avlc.get("acars") if isinstance(avlc.get("acars"), dict) else None
    if isinstance(acars, dict):
        if acars.get("reg"):
            parts.append(f"reg {acars.get('reg')}")
        if acars.get("flight"):
            parts.append(f"flt {acars.get('flight')}")
        text = acars.get("msg_text") or acars.get("text")
        if text:
            parts.append(f"text {str(text)[:60]}")
    return " · ".join(parts) if parts else "VDL2 frame"


def _acars_summarize_message(msg: dict | None) -> str:
    """Produce a one-line operator-readable summary of an acarsdec record."""
    if not isinstance(msg, dict):
        return ""
    parts = []
    if msg.get("freq"):
        parts.append(f"{msg.get('freq')} MHz")
    if msg.get("regno") or msg.get("tail"):
        parts.append(f"reg {msg.get('regno') or msg.get('tail')}")
    if msg.get("flight"):
        parts.append(f"flt {msg.get('flight')}")
    text = msg.get("text") or msg.get("message")
    if text:
        parts.append(f"text {str(text)[:60]}")
    return " · ".join(parts) if parts else "ACARS frame"


def _compute_sounding_detail_payload() -> dict:
    """Live state for the Sounding pane: VDL2 + ACARS decoders.

    Returns active/inactive, recent decode count, last decoded message
    summary, and dedicated-vs-shared dongle role.  Cheap to compute —
    bounded tail read + cached unit-active probe.
    """
    broker = _broker_read_state() or {}
    sounding_on = bool(broker.get("sounding", False))

    try:
        vdl2_active = _unit_active_cached("dumpvdl2.service")
    except Exception:
        vdl2_active = False
    try:
        acars_active = _unit_active_cached("acarsdec.service")
    except Exception:
        acars_active = False

    vdl2_last = _tail_jsonl_line(_VDL2_OUTPUT_PATH)
    acars_last = _tail_jsonl_line(_ACARS_OUTPUT_PATH)
    vdl2_age_ms = _file_age_ms(_VDL2_OUTPUT_PATH)
    acars_age_ms = _file_age_ms(_ACARS_OUTPUT_PATH)

    return {
        "sounding_on": sounding_on,
        "vdl2": {
            "active": bool(vdl2_active),
            "dongle": "dedicated",
            "recent_count": _count_jsonl_recent(_VDL2_OUTPUT_PATH, window_sec=60.0),
            "last_age_ms": vdl2_age_ms,
            "last_summary": _vdl2_summarize_message(vdl2_last),
            "last_raw": vdl2_last if isinstance(vdl2_last, dict) else None,
        },
        "acars": {
            "active": bool(acars_active),
            "dongle": "shared via broker",
            "recent_count": _count_jsonl_recent(_ACARS_OUTPUT_PATH, window_sec=60.0),
            "last_age_ms": acars_age_ms,
            "last_summary": _acars_summarize_message(acars_last),
            "last_raw": acars_last if isinstance(acars_last, dict) else None,
        },
        "server_time": time.time(),
    }


def _compute_sitrep_payload() -> dict:
    """Aggregate everything the Sitrep modal renders into one payload.

    Active-favorite counts, recent-hits, and Discovery summaries were
    removed when the Sitrep modal grew the Controls section — that
    data is surfaced in the dedicated panes already and the modal
    only needs heartbeat headline, service health, dongle assignments,
    and the evidence rows.
    """
    hb = _compute_heartbeat_payload()
    return {
        "state": hb.get("state"),
        "headline": hb.get("headline"),
        "explanation": hb.get("explanation"),
        "evidence": hb.get("evidence") or [],
        "since": hb.get("since"),
        "services": _sitrep_services(),
        "dongles": _sitrep_dongles(),
        "server_time": time.time(),
    }


# Sitrep Controls — one-button-per-action recovery levers exposed in the
# Sitrep modal.  Each action maps to a single fully-qualified command
# vector; we deliberately do not accept arbitrary args from the client.
# The matching NOPASSWD sudoers entries live in
# /etc/sudoers.d/scanner-controls (see commit message for the line).
_SITREP_ACTIONS: dict[str, dict] = {
    "reboot": {
        "label": "Reboot Micro",
        "cmd": ["sudo", "-n", "/sbin/reboot"],
    },
    "reset_radios": {
        "label": "Reset Radios",
        # NOTE: this raw cmd vector is NOT executed for ``reset_radios``
        # anymore — see _run_sitrep_action's special-case below, which
        # routes through ``safe_restart_rtl_airband`` to get SDRplay
        # daemon recovery + idempotency.  The vector is retained as
        # documentation of the underlying sudoers grant.
        "cmd": [
            "sudo", "-n", "/bin/systemctl", "restart",
            "rtl-airband-airband.service",
            "rtl-airband-ground.service",
            "scanner-digital-op25.service",
            "scanner-vfo.service",
        ],
    },
    "reset_live_iq": {
        "label": "Reset Live IQ",
        "cmd": ["sudo", "-n", "/bin/systemctl", "restart",
                "scanner-waterfall.service"],
    },
    "reset_disco": {
        "label": "Reset Discovery",
        "cmd": ["sudo", "-n", "/bin/systemctl", "restart",
                "disco-coordinator.service"],
    },
    "reset_ui": {
        "label": "Reset UI",
        "cmd": ["sudo", "-n", "/bin/systemctl", "restart",
                "airband-ui.service"],
    },
}

_SITREP_ACTION_SUDOERS_HINT = (
    "sudo NOPASSWD entries missing — add via "
    "`sudo visudo -f /etc/sudoers.d/scanner-controls`:\n"
    "ubuntu ALL=(ALL) NOPASSWD: /sbin/reboot, "
    "/bin/systemctl restart rtl-airband-airband.service, "
    "/bin/systemctl restart rtl-airband-ground.service, "
    "/bin/systemctl restart scanner-digital-op25.service, "
    "/bin/systemctl restart scanner-vfo.service, "
    "/bin/systemctl restart scanner-waterfall.service, "
    "/bin/systemctl restart disco-coordinator.service, "
    "/bin/systemctl restart airband-ui.service"
)


def _run_sitrep_action(action: str) -> tuple[bool, str, str]:
    """Execute the command vector mapped to ``action`` and return
    (ok, message, error).  Sudo-password failures are surfaced with a
    visudo hint so the operator can self-serve the fix.

    Special case: ``reset_radios`` does NOT run the raw 4-service
    ``systemctl restart`` command vector anymore.  That path was the
    direct cause of the SDRplay-wedge cascade Will hit repeatedly on
    2026-06-03 — a bare ``systemctl restart rtl-airband-*`` lets the
    stop-sigterm timeout escalate to SIGKILL mid-SDRplay-teardown,
    corrupts ``/dev/shm/Glbl*sdrSrv*`` semaphores, and the next
    ``sdrplay_api_Open`` hangs indefinitely with no recovery.
    Instead we delegate to ``safe_restart_rtl_airband`` which wraps
    the existing sequenced ``restart_rtl_airband`` /
    ``restart_rtl_ground`` helpers with a module-level idempotency
    lock + automatic sdrplay-daemon recovery on probe failure.
    """
    spec = _SITREP_ACTIONS.get(action)
    if not spec:
        return False, "", f"unknown action: {action}"
    label = spec["label"]

    # ------------------------------------------------------------------
    # Phase 4c — chirp feature-flag branch (single, top of reset_radios).
    # When SB5_USE_GR_DEMOD=true, reset both chirp daemons (sub-second
    # op, no SDR restart cascade) instead of bouncing rtl-airband.
    # ------------------------------------------------------------------
    if action == "reset_radios":
        try:
            try:
                from .chirp_client import use_gr_demod as _flag_use_gr_demod
                from . import chirp_adapter as _flag_chirp_adapter
            except ImportError:
                from ui.chirp_client import use_gr_demod as _flag_use_gr_demod  # type: ignore
                from ui import chirp_adapter as _flag_chirp_adapter  # type: ignore
        except Exception as exc:
            return False, "", f"{label}: chirp module import failed: {exc}"
        if _flag_use_gr_demod():
            try:
                return _flag_chirp_adapter.reset_radios_via_chirp()
            except Exception as exc:
                return False, "", f"{label}: chirp reset raised: {exc}"
    # ------------------------------------------------------------------
    # Safe path: reset_radios routes through the safe wrapper.
    # ------------------------------------------------------------------
    if action == "reset_radios":
        try:
            try:
                from .airband_restart import safe_restart_rtl_airband
            except ImportError:
                from ui.airband_restart import safe_restart_rtl_airband  # type: ignore
        except Exception as exc:
            return False, "", f"{label}: safe_restart import failed: {exc}"
        try:
            result = safe_restart_rtl_airband(
                bands=("airband", "ground"),
                reason="sitrep_reset_radios",
                also_restart_op25=True,
                also_restart_vfo=True,
            )
        except Exception as exc:
            return False, "", f"{label}: safe_restart raised: {exc}"
        status = str(result.get("status") or "")
        elapsed = result.get("elapsed_s", 0.0)
        if status == "ok":
            recovered = " (sdrplay recovered)" if result.get("restarted_sdrplay") else ""
            return True, f"{label} triggered{recovered} in {elapsed}s", ""
        if status == "in_flight_skipped":
            # A prior restart is still running — surface as success
            # (the in-flight one will complete on its own) so the UI
            # doesn't show a false error to the operator who clicked
            # twice or who clicked while squelch tracker was applying.
            return True, f"{label}: already in progress, skipped duplicate", ""
        per_band = result.get("results") or {}
        details = "; ".join(
            f"{b}: {('ok' if r.get('ok') else (r.get('error') or 'fail'))}"
            for b, r in per_band.items()
        ) or "(no per-band detail)"
        return False, "", f"{label}: {status} after {elapsed}s — {details}"

    # ------------------------------------------------------------------
    # Default path: raw systemctl command for the other sitrep actions
    # (reboot, reset_live_iq, reset_disco, reset_ui).  These don't
    # touch the SDRplay master/slave handle so the wedge cascade
    # doesn't apply.
    # ------------------------------------------------------------------
    cmd = list(spec["cmd"])
    try:
        # 20s is enough for `systemctl restart` of any of these units;
        # reboot returns immediately (the actual reboot is async).
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return False, "", f"{label}: command timed out after 20s"
    except FileNotFoundError as exc:
        return False, "", f"{label}: binary not found ({exc})"
    if proc.returncode == 0:
        return True, f"{label} triggered", ""
    stderr = (proc.stderr or "").strip() or (proc.stdout or "").strip()
    # `sudo -n` with no NOPASSWD entry exits 1 and writes "a password
    # is required" or "sorry" to stderr.  Surface the visudo hint so
    # Will can paste the fix without leaving the modal.
    sudo_blocked = (
        "password is required" in stderr.lower()
        or "sorry" in stderr.lower()
        or proc.returncode in (1, 100, 126, 127)
        and "sudo:" in stderr.lower()
    )
    if sudo_blocked:
        return False, "", f"{label}: {stderr}\n\n{_SITREP_ACTION_SUDOERS_HINT}"
    return False, "", f"{label}: rc={proc.returncode} {stderr}"



# =====================================================================
# Phase 5a — mock-data builders for the new dongle panes.
#
# These return realistic-looking JSON for /api/waterfall, /api/vfo,
# /api/disco.  Bins use a noise floor of -95..-85 dBFS with bursts at
# real airband / public-safety / FM-broadcast / NOAA-wx frequencies
# so the UI doesn't look like lorem-ipsum.  Slight per-call jitter
# keeps the display from looking frozen.
#
# Real RF-backed implementations land per-pane as the 6 RTL-SDRs
# (2 waterfall stitched, 1 VFO, 3 disco unified) come online.
# =====================================================================

def _mock_bin_floor(n: int, floor_lo: float = -95.0, floor_hi: float = -85.0) -> list[float]:
    """A baseline noise floor with mild per-bin jitter."""
    span = floor_hi - floor_lo
    return [floor_lo + random.random() * span for _ in range(n)]


def _mock_add_burst(bins: list[float], f_min: float, f_max: float,
                    freq_mhz: float, peak_dbfs: float, width_bins: int) -> None:
    """Add a Gaussian-ish bump at `freq_mhz` into `bins`."""
    n = len(bins)
    if n <= 0 or f_max <= f_min:
        return
    if not (f_min <= freq_mhz <= f_max):
        return
    center = int(round((freq_mhz - f_min) / (f_max - f_min) * (n - 1)))
    width = max(1, int(width_bins))
    floor_amp = -90.0  # the floor we add over (approx)
    for i in range(max(0, center - 4 * width), min(n, center + 4 * width + 1)):
        d = i - center
        g = pow(2.718281828, -(d * d) / (2.0 * width * width))
        # jitter the burst peak a touch
        jitter = (random.random() - 0.5) * 1.5
        val = floor_amp + (peak_dbfs - floor_amp) * g + jitter
        if val > bins[i]:
            bins[i] = val


def _mock_waterfall_payload() -> dict:
    """Mock GET /api/waterfall payload — 2 RTL-SDRs stitched, ~5 MHz around 127.5 MHz."""
    center_mhz = 127.5
    bw_mhz = 5.0
    f_min = center_mhz - bw_mhz / 2
    f_max = center_mhz + bw_mhz / 2
    n = 1024
    bins = _mock_bin_floor(n)
    # plausible airband bursts inside the window
    air_chans = [
        (125.450, -42.0, 5),  # busy approach
        (127.700, -36.0, 6),  # tower
        (126.150, -58.0, 4),  # ground
        (124.600, -52.0, 5),
        (128.825, -64.0, 3),
        (129.300, -48.0, 5),
        (125.900, -70.0, 3),
    ]
    for f, peak, w in air_chans:
        _mock_add_burst(bins, f_min, f_max, f, peak + (random.random() - 0.5) * 4.0, w)
    return {
        "state": "ok",
        "center_mhz": center_mhz,
        "bw_mhz": bw_mhz,
        "bins": bins,
        "last_frame_age_ms": random.randint(8, 28),
        "dongle_serials": ["00000001", "00000002"],
    }


def _mock_vfo_payload() -> dict:
    """Mock GET /api/vfo payload — 1 RTL-SDR, 2.4 MHz around the tuned freq."""
    freq_mhz = 127.700
    bw_mhz = 2.4
    f_min = freq_mhz - bw_mhz / 2
    f_max = freq_mhz + bw_mhz / 2
    n = 256
    bins = _mock_bin_floor(n, -94.0, -84.0)
    # local airband activity around the tuned freq
    _mock_add_burst(bins, f_min, f_max, 127.700, -34.0, 3)
    _mock_add_burst(bins, f_min, f_max, 127.450, -62.0, 2)
    _mock_add_burst(bins, f_min, f_max, 128.150, -55.0, 2)
    _mock_add_burst(bins, f_min, f_max, 126.950, -68.0, 2)
    return {
        "state": "ok",
        "freq_mhz": freq_mhz,
        "mod": "am",
        "muted": False,
        "bt_routed": False,
        "bins": bins,
        "dongle_serial": "00000003",
    }


def _mock_disco_payload() -> dict:
    """Mock GET /api/disco payload — 3 RTL-SDRs unified, 30..1700 MHz wide sweep."""
    start_mhz = 30.0
    end_mhz = 1700.0
    n = 1024
    bins = _mock_bin_floor(n, -94.0, -82.0)
    # Sprinkle bursts across the wide sweep at plausible real-world freqs.
    sweep_bursts = [
        ( 88.5, -48.0, 4),    # FM broadcast
        ( 92.9, -38.0, 4),    # FM broadcast (strong local)
        ( 99.7, -50.0, 4),    # FM broadcast
        (104.5, -45.0, 4),    # FM broadcast
        (121.5, -55.0, 3),    # aviation guard
        (125.450, -42.0, 3),  # airband approach
        (127.700, -38.0, 3),  # airband tower
        (155.160, -52.0, 3),  # public-safety VHF
        (159.480, -58.0, 3),  # public-safety VHF
        (162.400, -48.0, 3),  # NOAA wx
        (162.550, -46.0, 3),  # NOAA wx
        (462.6125, -60.0, 2), # GMRS
        (851.4625, -58.0, 2), # public-safety 800
        (855.7375, -55.0, 2), # public-safety 800
        (1090.0, -62.0, 2),   # ADS-B
        (1575.42, -70.0, 2),  # GPS L1 leak (very faint)
    ]
    for f, peak, w in sweep_bursts:
        _mock_add_burst(bins, start_mhz, end_mhz, f,
                        peak + (random.random() - 0.5) * 4.0, w)
    detections = [
        {"freq_mhz": 121.500, "classification": "AM voice (emergency guard)", "confidence": 0.91},
        {"freq_mhz": 127.700, "classification": "AM voice (airband tower)",   "confidence": 0.88},
        {"freq_mhz": 162.550, "classification": "NFM weather broadcast",      "confidence": 0.96},
        {"freq_mhz":  92.900, "classification": "WFM broadcast",              "confidence": 0.99},
        {"freq_mhz": 155.160, "classification": "NFM public-safety (TN VHF)", "confidence": 0.74},
        {"freq_mhz": 851.4625,"classification": "P25 control channel",        "confidence": 0.82},
        {"freq_mhz":1090.000, "classification": "ADS-B Mode-S",               "confidence": 0.67},
    ]
    return {
        "state": "ok",
        "range": {"start_mhz": start_mhz, "end_mhz": end_mhz},
        "bins": bins,
        "dongle_serials": ["00000004", "00000005", "00000006"],
        "detections": detections,
    }



class Handler(BaseHTTPRequestHandler):
    """HTTP request handler for the UI."""

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        """Send an HTTP response."""
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError) as exc:
            # Client hung up mid-write (closed tab, refresh, dropped socket).
            # Benign — log at debug instead of letting it bubble into a
            # noisy traceback / 500 in the worker thread.
            logger.debug("client disconnected during _send write: %s", exc)

    def _send_redirect(self, location: str, code: int = 302):
        """Send a redirect response."""
        self.send_response(code)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _send_head(self, code: int, ctype: str = "text/plain; charset=utf-8", content_length: int | None = None):
        """Send headers-only response for HEAD requests."""
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        if content_length is not None:
            self.send_header("Content-Length", str(int(content_length)))
        self.end_headers()

    def _sanitize_mount_name(self, mount_name: str) -> str:
        mount = unquote(str(mount_name or "")).strip().lstrip("/")
        if not mount:
            mount = str(PLAYER_MOUNT or "").strip().lstrip("/")
        if not mount:
            return ""
        if "/" in mount or "\\" in mount:
            return ""
        for ch in mount:
            if not (ch.isalnum() or ch in "._-"):
                return ""
        return mount

    @staticmethod
    def _parse_optional_bool_query(qs: dict[str, list[str]], key: str) -> bool | None:
        if key not in qs:
            return None
        raw = ((qs.get(key) or [""])[0] or "").strip().lower()
        if raw in ("1", "true", "yes", "on"):
            return True
        if raw in ("", "0", "false", "no", "off"):
            return False
        return None

    def _proxy_icecast_mount(self, mount_name: str, head_only: bool = False, transcode: bool | None = None):
        mount = self._sanitize_mount_name(mount_name)
        if not mount:
            if head_only:
                return self._send_head(400)
            return self._send(400, "invalid mount", "text/plain; charset=utf-8")
        transcode_enabled = False
        if not head_only:
            if transcode is None:
                # All mounts go through ffmpeg re-encode so browser audio
                # elements get clean, streaming-friendly MP3 framing.
                transcode_enabled = True
            else:
                transcode_enabled = bool(transcode)
        upstream = f"http://127.0.0.1:{ICECAST_PORT}/{mount}"
        headers_sent = False
        if transcode_enabled:
            proc = None
            try:
                cmd = [
                    "ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-fflags",
                    "+nobuffer+discardcorrupt",
                    "-probesize",
                    "131072",
                    "-analyzeduration",
                    "500000",
                    "-f",
                    "mp3",
                    "-i",
                    upstream,
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    str(STREAM_PROXY_TRANSCODE_SAMPLE_RATE_HZ),
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    f"{STREAM_PROXY_TRANSCODE_BITRATE_KBPS}k",
                    "-write_xing",
                    "0",
                    "-flush_packets",
                    "1",
                    "-f",
                    "mp3",
                    "pipe:1",
                ]
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Connection", "close")
                self.end_headers()
                headers_sent = True
                if not proc.stdout:
                    return
                while True:
                    chunk = proc.stdout.read(STREAM_PROXY_CHUNK_BYTES)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                return
            except FileNotFoundError:
                if headers_sent:
                    return
                return self._send(500, "ffmpeg not found", "text/plain; charset=utf-8")
            except (BrokenPipeError, ConnectionResetError):
                return
            finally:
                if proc and proc.poll() is None:
                    proc.terminate()
        req = Request(
            upstream,
            headers={
                "User-Agent": "airband-ui/stream-proxy",
                "Connection": "close",
            },
            # Icecast can reject HEAD on mounts; use GET for both and suppress body on HEAD.
            method="GET",
        )
        try:
            # Use a long read timeout for low-traffic mounts so mobile clients
            # do not see frequent stream teardowns during quiet periods.
            with urlopen(req, timeout=STREAM_PROXY_READ_TIMEOUT_SEC) as upstream_resp:
                self.send_response(200)
                self.send_header("Content-Type", upstream_resp.headers.get("Content-Type") or "audio/mpeg")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Accept-Ranges", "none")
                self.send_header("Connection", "close")
                for header in (
                    "icy-name",
                    "icy-genre",
                    "icy-description",
                    "icy-br",
                    "icy-metaint",
                    "ice-audio-info",
                ):
                    value = upstream_resp.headers.get(header)
                    if value:
                        self.send_header(header, value)
                self.end_headers()
                headers_sent = True
                if head_only:
                    return
                while True:
                    # Keep proxy chunks small so low-bitrate streams flush
                    # frequently enough for embedded browser players.
                    chunk = upstream_resp.read(STREAM_PROXY_CHUNK_BYTES)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except HTTPError as e:
            status = int(e.code or 502)
            if headers_sent:
                return
            if head_only:
                return self._send_head(status)
            return self._send(status, f"upstream error: {e.reason}", "text/plain; charset=utf-8")
        except (URLError, TimeoutError) as e:
            if headers_sent:
                return
            if head_only:
                return self._send_head(502)
            return self._send(502, f"upstream unavailable: {e}", "text/plain; charset=utf-8")
        except (BrokenPipeError, ConnectionResetError):
            return

    _HLS_BASE = Path("/run/scannerproject/hls")
    _HLS_CONTENT_TYPES = {
        ".m3u8": "application/vnd.apple.mpegurl",
        ".ts": "video/MP2T",
        ".m4s": "video/iso.segment",
        ".mp4": "video/mp4",
    }

    def _serve_hls_file(self, url_path: str):
        """Serve HLS playlist or segment files."""
        # Sanitize: only allow safe filenames under the HLS directory.
        rel = url_path[len("/hls/"):].strip("/")
        if not rel or ".." in rel:
            return self._send(400, "invalid path", "text/plain; charset=utf-8")
        fpath = self._HLS_BASE / rel
        if not fpath.is_file():
            return self._send(404, "not found", "text/plain; charset=utf-8")
        ext = fpath.suffix.lower()
        ctype = self._HLS_CONTENT_TYPES.get(ext, "application/octet-stream")
        try:
            data = fpath.read_bytes()
        except Exception:
            return self._send(500, "read error", "text/plain; charset=utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, no-store" if ext == ".m3u8" else "max-age=10")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):
        """Handle HEAD requests."""
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query or "")
        transcode = self._parse_optional_bool_query(q, "transcode")
        if p == "/stream" or p == "/stream/":
            return self._proxy_icecast_mount("", head_only=True, transcode=transcode)
        if p.startswith("/stream/"):
            return self._proxy_icecast_mount(p[len("/stream/"):], head_only=True, transcode=transcode)
        return self._send_head(404)

    def do_GET(self):
        """Handle GET requests."""
        u = urlparse(self.path)
        p = _canonical_scan_api_path(u.path)
        q = parse_qs(u.query or "")
        transcode = self._parse_optional_bool_query(q, "transcode")
        # WebSocket push for FFT spectrum bins.  Hand-rolled RFC 6455
        # server inside the existing http.server handler; on a successful
        # upgrade the call blocks until the client disconnects and we
        # must NOT fall through to any further HTTP processing on this
        # connection.
        if p == "/ws/spectrum":
            if ws_spectrum.handle_spectrum_upgrade(self):
                return
            return self._send(400, "Expected WebSocket upgrade", "text/plain; charset=utf-8")
        if p == "/":
            return self._send_redirect("/sb3")

        if p in ("/hp3", "/hp3/", "/hp3.html"):
            return self._send_redirect("/static/hp3-react.html")

        if p in ("/hp", "/hp/", "/hp.html"):
            return self._send_redirect("/hp3")
        
        # Serve SB3 UI
        #
        # SB3 is the previous-generation UI, kept alive alongside the SB5
        # production UI because SB3 still hosts features SB5 has not yet
        # absorbed:
        #   * Favorites wizard (hp-wizard-*) — multi-stage create / edit
        #     flow that drives /api/profile/create, /api/profile/delete,
        #     /api/profile-editor/analog/{save,validate}, and
        #     /api/profile-editor/digital/{save,validate}.  SB5's Phase 7b
        #     "Favorite picker" modal only SWITCHES the active favorite;
        #     it does not create or edit one.
        #   * Band Scan tile — the airband/marine/CB/mil-air/rail
        #     quick-scan shortcuts that POST scan ranges (#band-scan, the
        #     btn-bandscan-* controls).  No SB5 equivalent.
        # When SB5 grows a real profile editor + band-scan tile, this
        # route can be retired and ui/sb3.html moved to archive/.
        if p == "/sb3" or p == "/sb3.html":
            ui_dir = os.path.dirname(os.path.abspath(__file__))
            mockup_path = os.path.join(ui_dir, "sb3.html")
            try:
                with open(mockup_path, "r", encoding="utf-8") as f:
                    return self._send(200, f.read())
            except FileNotFoundError:
                return self._send(404, "SB3 UI not found", "text/plain; charset=utf-8")

        # Serve SB5 UI (Phase 4 -- iOS-native consumer design)
        if p == "/sb5" or p == "/sb5.html":
            ui_dir = os.path.dirname(os.path.abspath(__file__))
            mockup_path = os.path.join(ui_dir, "sb5.html")
            try:
                with open(mockup_path, "r", encoding="utf-8") as f:
                    return self._send(200, f.read())
            except FileNotFoundError:
                return self._send(404, "SB5 UI not found", "text/plain; charset=utf-8")

        # Captive portal detection (iOS, Android, Windows)
        if p in ("/hotspot-detect.html", "/library/test/success.html",
                 "/generate_204", "/connecttest.txt", "/ncsi.txt",
                 "/redirect", "/canonical.html"):
            return self._send_redirect("/sb3")

        if p in ("/decode", "/decode/", "/decode.html"):
            ui_dir = os.path.dirname(os.path.abspath(__file__))
            decode_path = os.path.join(ui_dir, "acars-decode.html")
            try:
                with open(decode_path, "r", encoding="utf-8") as f:
                    return self._send(200, f.read())
            except FileNotFoundError:
                return self._send(404, "ACARS decode view not found", "text/plain; charset=utf-8")

        # Serve static files
        if p.startswith("/static/"):
            ui_dir = os.path.dirname(os.path.abspath(__file__))
            static_dir = os.path.realpath(os.path.join(ui_dir, "static"))
            file_path = os.path.realpath(os.path.join(ui_dir, p.lstrip("/")))
            if not (file_path == static_dir or file_path.startswith(static_dir + os.sep)):
                return self._send(403, "Forbidden", "text/plain; charset=utf-8")
            try:
                with open(file_path, "rb") as f:
                    content = f.read()
                # Determine content type
                if file_path.endswith(".css"):
                    ctype = "text/css; charset=utf-8"
                elif file_path.endswith(".js"):
                    ctype = "application/javascript; charset=utf-8"
                elif file_path.endswith(".mjs"):
                    ctype = "application/javascript; charset=utf-8"
                elif file_path.endswith(".html"):
                    ctype = "text/html; charset=utf-8"
                elif file_path.endswith(".json"):
                    ctype = "application/json; charset=utf-8"
                else:
                    ctype = "application/octet-stream"
                return self._send(200, content, ctype)
            except FileNotFoundError:
                return self._send(404, "Not found", "text/plain; charset=utf-8")

        if p == "/stream" or p == "/stream/":
            return self._proxy_icecast_mount("", transcode=transcode)
        if p.startswith("/stream/"):
            return self._proxy_icecast_mount(p[len("/stream/"):], transcode=transcode)

        # HLS segment/playlist serving for live audio players.
        if p.startswith("/hls/"):
            return self._serve_hls_file(p)

        if p == "/api/system":
            try:
                payload = get_system_stats()
            except Exception as e:
                payload = {"ok": False, "error": str(e)}
                return self._send(500, json.dumps(payload), "application/json; charset=utf-8")
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/heartbeat":
            # Phase 1b — read-only state inspection: QUIET vs WEDGED.
            # See `_compute_heartbeat_payload` for the decision rule.
            try:
                payload = _compute_heartbeat_payload()
            except Exception as e:
                logger.exception("/api/heartbeat probe failed")
                fallback = {
                    "state": "wedged",
                    "since": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
                    "headline": "Heartbeat probe failed.",
                    "explanation": f"Heartbeat computation raised: {e}",
                    "recovery": None,
                    "evidence": [],
                    "server_time": time.time(),
                    "cached": False,
                }
                return self._send(500, json.dumps(fallback), "application/json; charset=utf-8")
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/sitrep":
            # Phase 7a — aggregated situation report.  Combines heartbeat
            # evidence with active-favorite metadata, recent hits, Disco
            # detection counts, service health, and per-dongle role/state
            # so the Sitrep modal renders from a single fetch.
            try:
                payload = _compute_sitrep_payload()
            except Exception as exc:
                logger.exception("/api/sitrep failed")
                payload = {
                    "state": "wedged",
                    "headline": "Sitrep computation failed",
                    "explanation": str(exc),
                    "evidence": [],
                    "services": [],
                    "dongles": [],
                    "server_time": time.time(),
                }
                return self._send(500, json.dumps(payload), "application/json; charset=utf-8")
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/hp/location/ip":
            try:
                resolved = _resolve_ip_geolocation()
                payload = {
                    "ok": True,
                    "lat": float(resolved.get("lat")),
                    "lon": float(resolved.get("lon")),
                    "latitude": float(resolved.get("lat")),
                    "longitude": float(resolved.get("lon")),
                    "postal": str(resolved.get("zip") or "").strip(),
                    "zip": str(resolved.get("zip") or "").strip(),
                    "county": str(resolved.get("county") or "").strip(),
                    "provider": str(resolved.get("provider") or ""),
                }
            except Exception as e:
                return self._send(
                    502,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/hp/location/reverse":
            try:
                raw_lat = (q.get("lat") or q.get("latitude") or [""])[0]
                raw_lon = (q.get("lon") or q.get("longitude") or [""])[0]
                lat = _parse_float_value(raw_lat, field="lat")
                lon = _parse_float_value(raw_lon, field="lon")
                if not (-90.0 <= lat <= 90.0):
                    raise ValueError("invalid lat")
                if not (-180.0 <= lon <= 180.0):
                    raise ValueError("invalid lon")
                resolved = _resolve_reverse_geolocation(lat, lon)
                zip_code = str(resolved.get("zip") or "").strip()
                county = str(resolved.get("county") or "").strip()
                payload = {
                    "ok": True,
                    "postcode": zip_code,
                    "zip": zip_code,
                    "county": county,
                    "provider": str(resolved.get("provider") or ""),
                }
            except ValueError as e:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            except Exception as e:
                return self._send(
                    502,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/hp/state":
            try:
                state = HPState.load()
                controller = get_scan_mode_controller()
                payload = {
                    "ok": True,
                    "mode": controller.get_mode(),
                    "state": state.to_dict(),
                    "travel_mode_last_push": _last_travel_push_receipt(),
                    "favorites_runtime_sync": get_last_favorites_runtime_sync(),
                }
            except Exception as e:
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/hp/scan-pool-preview":
            try:
                state = HPState.load()
                controller = get_scan_mode_controller()
                limit_raw = (q.get("limit") or ["4000"])[0]
                try:
                    limit = int(str(limit_raw).strip())
                except Exception:
                    limit = 4000
                source_raw = (q.get("source") or ["runtime_applied"])[0]
                source = str(source_raw or "").strip().lower()
                preview_source = "computed_state"
                snapshot_signature = ""
                snapshot_applied_at_ms = 0
                snapshot_ready = False
                pool = None
                if source in {"", "runtime", "runtime_applied", "active", "applied", "scanned"}:
                    runtime_snapshot = get_last_runtime_scan_pool()
                    if isinstance(runtime_snapshot, dict) and bool(runtime_snapshot.get("snapshot_ready")):
                        candidate_pool = runtime_snapshot.get("pool")
                        if isinstance(candidate_pool, dict):
                            pool = candidate_pool
                            preview_source = "runtime_applied"
                            snapshot_signature = str(runtime_snapshot.get("signature") or "")
                            snapshot_applied_at_ms = int(runtime_snapshot.get("applied_at_ms") or 0)
                            snapshot_ready = True
                if not isinstance(pool, dict):
                    pool = controller.get_scan_pool()
                    preview_source = "computed_state"
                preview = _flatten_hp_scan_pool_for_preview(pool, limit=limit)
                payload = {
                    "ok": True,
                    "mode": str(getattr(state, "mode", "") or "").strip().lower(),
                    "pool_source": preview_source,
                    "pool_snapshot_ready": bool(snapshot_ready),
                    "pool_signature": snapshot_signature,
                    "pool_applied_at_ms": snapshot_applied_at_ms,
                    **preview,
                }
            except Exception as e:
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p.startswith("/api/hp/favorites-wizard/"):
            def _query_int(name: str, default: int | None = None, required: bool = False) -> int | None:
                raw = (q.get(name) or [None])[0]
                if raw is None or str(raw).strip() == "":
                    if required:
                        raise ValueError(f"missing {name}")
                    return default
                try:
                    return int(str(raw).strip())
                except Exception as exc:
                    raise ValueError(f"invalid {name}") from exc

            text_filter = str((q.get("q") or [""])[0] or "").strip()
            try:
                wizard = HPFavoritesWizard()
                if p == "/api/hp/favorites-wizard/countries":
                    payload = {
                        "ok": True,
                        "countries": wizard.get_countries(),
                    }
                    return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

                if p == "/api/hp/favorites-wizard/states":
                    country_id = _query_int("country_id", default=1, required=False)
                    payload = {
                        "ok": True,
                        "states": wizard.get_states(country_id=int(country_id or 1)),
                    }
                    return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

                if p == "/api/hp/favorites-wizard/counties":
                    state_id = _query_int("state_id", required=True)
                    payload = {
                        "ok": True,
                        "counties": wizard.get_counties(state_id=int(state_id or 0)),
                    }
                    return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

                if p == "/api/hp/favorites-wizard/systems":
                    state_id = _query_int("state_id", required=True)
                    county_id = _query_int("county_id", default=0, required=False)
                    system_type = str((q.get("system_type") or ["digital"])[0] or "").strip().lower()
                    default_scope = "county" if int(county_id or 0) > 0 else "statewide"
                    scope = str((q.get("scope") or [default_scope])[0] or "").strip().lower()
                    if system_type not in {"digital", "analog"}:
                        return self._send(
                            400,
                            json.dumps({"ok": False, "error": "invalid system_type"}),
                            "application/json; charset=utf-8",
                        )
                    if scope not in {"nationwide", "statewide", "county"}:
                        return self._send(
                            400,
                            json.dumps({"ok": False, "error": "invalid scope"}),
                            "application/json; charset=utf-8",
                        )
                    payload = {
                        "ok": True,
                        "systems": wizard.get_systems(
                            state_id=int(state_id or 0),
                            county_id=int(county_id or 0),
                            system_type=system_type,
                            scope=scope,
                            text_filter=text_filter,
                        ),
                    }
                    return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

                if p == "/api/hp/favorites-wizard/channels":
                    system_type = str((q.get("system_type") or ["digital"])[0] or "").strip().lower()
                    if system_type not in {"digital", "analog"}:
                        return self._send(
                            400,
                            json.dumps({"ok": False, "error": "invalid system_type"}),
                            "application/json; charset=utf-8",
                        )
                    system_id = str((q.get("system_id") or [""])[0] or "").strip()
                    if not system_id:
                        return self._send(
                            400,
                            json.dumps({"ok": False, "error": "missing system_id"}),
                            "application/json; charset=utf-8",
                        )
                    limit = _query_int("limit", default=500, required=False)
                    limit = max(1, min(int(limit or 500), 5000))
                    system_name, channels = wizard.get_channels(
                        system_type=system_type,
                        system_id=system_id,
                        text_filter=text_filter,
                    )
                    payload = {
                        "ok": True,
                        "system_name": system_name,
                        "channels": list(channels[:limit]),
                        "total_channels": len(channels),
                        "truncated": len(channels) > limit,
                    }
                    return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

                return self._send(
                    404,
                    json.dumps({"ok": False, "error": "not found"}),
                    "application/json; charset=utf-8",
                )
            except ValueError as e:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            except Exception as e:
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )

        if p == "/api/hp/service-types":
            try:
                service_types = get_all_service_types()
                defaults = get_default_enabled_service_types()
                payload = {
                    "ok": True,
                    "service_types": service_types,
                    "default_enabled_service_tags": defaults,
                }
            except Exception as e:
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/hp/avoids":
            try:
                controller = get_scan_mode_controller()
                payload = {
                    "ok": True,
                    "avoids": controller.get_hp_avoids(),
                }
            except Exception as e:
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/hp/favorites-sync":
            return self._send(
                200,
                json.dumps(
                    {
                        "ok": False,
                        "available": False,
                        "in_sync": True,
                        "reason": "favorites sync retired; favorites route directly to runtime",
                    }
                ),
                "application/json; charset=utf-8",
            )

        if p == "/api/latency/tone":
            payload = _latency_tone_status_payload()
            return self._send(
                200,
                json.dumps({"ok": True, "tone": payload}),
                "application/json; charset=utf-8",
            )

        if p == "/api/status":
            now_monotonic = time.monotonic()
            with _CACHE_LOCK:
                cached_payload = _STATUS_CACHE.get("payload")
                cached_ts = float(_STATUS_CACHE.get("ts") or 0.0)
            if isinstance(cached_payload, dict) and (now_monotonic - cached_ts) <= _STATUS_CACHE_TTL_SEC:
                payload = dict(cached_payload)
                payload["server_time"] = time.time()
                payload["server_timezone"] = _RESOLVED_SERVER_TIMEZONE
                return self._send(200, json.dumps(payload), "application/json; charset=utf-8")
            conf_path = read_active_config_path()
            ground_conf_path = os.path.realpath(GROUND_CONFIG_PATH)
            combined_conf_path = COMBINED_CONFIG_PATH
            controls_snapshot = _read_effective_analog_controls()
            controls_airband_path = controls_snapshot["controls_airband_path"]
            controls_ground_path = controls_snapshot["controls_ground_path"]
            airband_gain = controls_snapshot["airband_gain"]
            airband_dbfs = controls_snapshot["airband_dbfs"]
            airband_mode = controls_snapshot["airband_mode"]
            ground_gain = controls_snapshot["ground_gain"]
            ground_dbfs = controls_snapshot["ground_dbfs"]
            ground_mode = controls_snapshot["ground_mode"]
            airband_filter = parse_filter("airband")
            ground_filter = parse_filter("ground")
            rtl_unit_active = _unit_active_cached(UNITS["rtl"])
            ground_unit_active = _unit_active_cached(UNITS["ground"])
            keepalive_unit_active = _unit_active_cached(UNITS["keepalive"])
            digital_audio_unit_active = _unit_active_cached(UNITS["digital_audio"])
            combined_info = combined_device_summary()
            airband_device = combined_info.get("airband")
            ground_device = combined_info.get("ground")
            expected_serials = dict(combined_info.get("expected_serials") or {})
            expected_indices = dict(combined_info.get("expected_indices") or {})
            effective_digital_serials = _effective_digital_rtl_serials()
            if AIRBAND_RTL_SERIAL:
                expected_serials["airband"] = AIRBAND_RTL_SERIAL
            if GROUND_RTL_SERIAL:
                expected_serials["ground"] = GROUND_RTL_SERIAL
            expected_serials["digital"] = effective_digital_serials[0] if len(effective_digital_serials) > 0 else ""
            expected_serials["digital_secondary"] = effective_digital_serials[1] if len(effective_digital_serials) > 1 else ""
            expected_serials["digital_tertiary"] = effective_digital_serials[2] if len(effective_digital_serials) > 2 else ""
            serial_mismatch_detail = []
            index_mismatch_detail = list(combined_info.get("index_mismatch_detail") or [])
            if AIRBAND_RTL_SERIAL:
                actual = airband_device.get("serial") if airband_device else ""
                if not actual:
                    serial_mismatch_detail.append({
                        "device": "airband",
                        "expected": AIRBAND_RTL_SERIAL,
                        "actual": "",
                        "reason": "airband device not found in combined config",
                    })
                elif actual != AIRBAND_RTL_SERIAL:
                    serial_mismatch_detail.append({
                        "device": "airband",
                        "expected": AIRBAND_RTL_SERIAL,
                        "actual": actual,
                        "reason": "airband serial mismatch",
                    })
            if GROUND_RTL_SERIAL:
                actual = ground_device.get("serial") if ground_device else ""
                if not actual:
                    serial_mismatch_detail.append({
                        "device": "ground",
                        "expected": GROUND_RTL_SERIAL,
                        "actual": "",
                        "reason": "ground device not found in combined config",
                    })
                elif actual != GROUND_RTL_SERIAL:
                    serial_mismatch_detail.append({
                        "device": "ground",
                        "expected": GROUND_RTL_SERIAL,
                        "actual": actual,
                        "reason": "ground serial mismatch",
                    })
            airband_present = airband_device is not None
            ground_present = ground_device is not None
            # Sample-flow liveness for rtl-airband: systemd "active" stays
            # true through zombie states where the SoapySDR readStream is
            # stuck and the channel pipeline writes no samples.  The
            # stats file mtime is the contractual heartbeat — rtl_airband
            # rewrites it every output_thread cycle.  Stale mtime ⇒ the
            # pipeline is dead regardless of unit state.
            #
            # PRE-SPLIT (legacy): one rtl-airband process, one stats file.
            sample_flow = rtl_airband_sample_flow_state(
                RTL_AIRBAND_STATS_PATH,
                RTL_AIRBAND_STATS_STALE_SEC,
            )
            sample_flow_ok = bool(sample_flow.get("sample_flow_ok"))
            # POST-SPLIT: two rtl-airband processes, two stats files.
            # During the transition both blocks are evaluated; the
            # legacy ``rtl_ok`` stays bound to the legacy unit while
            # the new ``rtl_airband_ok`` / ``rtl_ground_ok`` track the
            # split services.  ``_unit_active_cached`` returns False
            # for nonexistent units, so during pre-cutover the new
            # signals report unhealthy without breaking anything.
            rtl_airband_unit_active = _unit_active_cached(UNITS.get("rtl_airband", ""))
            rtl_ground_unit_active = _unit_active_cached(UNITS.get("rtl_ground", ""))
            sample_flow_airband = rtl_airband_sample_flow_state(
                RTL_AIRBAND_AIRBAND_STATS_PATH,
                RTL_AIRBAND_STATS_STALE_SEC,
            )
            sample_flow_ground = rtl_airband_sample_flow_state(
                RTL_AIRBAND_GROUND_STATS_PATH,
                RTL_AIRBAND_STATS_STALE_SEC,
            )
            rtl_airband_sample_flow_ok = bool(sample_flow_airband.get("sample_flow_ok"))
            rtl_ground_sample_flow_ok = bool(sample_flow_ground.get("sample_flow_ok"))
            rtl_airband_ok = rtl_airband_unit_active and rtl_airband_sample_flow_ok
            rtl_ground_ok = rtl_ground_unit_active and rtl_ground_sample_flow_ok

            rtl_ok = rtl_unit_active and sample_flow_ok
            ground_ok = rtl_ok and ground_present
            ice_unit_active = _unit_active_cached(UNITS["icecast"])
            icecast_mounts = []
            icecast_status_text = ""
            analog_stream_mount = str(PLAYER_MOUNT or "").strip().lstrip("/")
            digital_stream_mount = str(DIGITAL_STREAM_MOUNT or "").strip().lstrip("/")
            if ice_unit_active:
                try:
                    icecast_status_text = fetch_local_icecast_status()
                    icecast_mounts = list_icecast_mounts(icecast_status_text)
                    analog_stream_mount = _resolve_analog_stream_mount(icecast_status_text)
                    digital_stream_mount = _resolve_digital_stream_mount(icecast_status_text)
                except Exception:
                    icecast_mounts = []
                    icecast_status_text = ""
            # Mount-publishing liveness: icecast's systemd wrapper can
            # report "active (exited)" after the daemon dies (SysV init
            # script wrapper artifact), and mount entries persist after
            # source disconnects.  Truth is whether a source is actively
            # publishing to the expected mounts right now.
            mount_analog = mount_publishing(icecast_status_text, PLAYER_MOUNT or "ANALOG.mp3")
            mount_digital = mount_publishing(
                icecast_status_text, DIGITAL_STREAM_MOUNT or "DIGITAL.mp3"
            )
            # MA/SL split adds a sister mount for the ground service;
            # before cutover this returns False (no source publishing
            # there), which is the truthful answer.
            mount_analog_ground = mount_publishing(
                icecast_status_text, "ANALOG_GROUND.mp3"
            )
            # icecast_active retains its is-active semantic for backwards
            # compatibility; callers that need real liveness consume the
            # mount-publishing booleans.
            ice_ok = ice_unit_active
            combined_stale = combined_config_stale()

            prof_payload, profiles_airband, profiles_ground = split_profiles()
            missing = [p["path"] for p in prof_payload if not p.get("exists")]
            profile_airband = guess_current_profile(conf_path, [(p["id"], p["label"], p["path"]) for p in profiles_airband])
            profile_ground = guess_current_profile(ground_conf_path, [(p["id"], p["label"], p["path"]) for p in profiles_ground])
            last_hit_airband = read_last_hit_airband()
            last_hit_ground = read_last_hit_ground()
            airband_labels = _resolve_analog_label_map(conf_path, profile_airband, profiles_airband)
            ground_labels = _resolve_analog_label_map(ground_conf_path, profile_ground, profiles_ground)
            hit_items = _annotate_analog_hits(
                read_hit_list_cached(limit=20),
                airband_labels,
                ground_labels,
            )
            full_hits_payload = _get_hits_payload_cached(limit=20)
            full_hit_items = full_hits_payload.get("items") or []
            analog_scan_health = get_analog_scan_health()
            latest_hit = hit_items[0].get("freq") if hit_items else ""
            last_hit_airband_label = ""
            last_hit_ground_label = ""
            for item in hit_items:
                src = str(item.get("source") or "").strip().lower()
                if src == "airband" and not last_hit_airband_label:
                    last_hit_airband_label = str(item.get("label_full") or item.get("label") or "").strip()
                if src == "ground" and not last_hit_ground_label:
                    last_hit_ground_label = str(item.get("label_full") or item.get("label") or "").strip()
                if last_hit_airband_label and last_hit_ground_label:
                    break
            if not last_hit_airband_label:
                last_hit_airband_label = _lookup_analog_label(
                    last_hit_airband,
                    "airband",
                    airband_labels,
                    ground_labels,
                )
            if not last_hit_ground_label:
                last_hit_ground_label = _lookup_analog_label(
                    last_hit_ground,
                    "ground",
                    airband_labels,
                    ground_labels,
                )
            config_mtimes = {}
            for key, path in (("airband", conf_path), ("ground", ground_conf_path), ("combined", combined_conf_path)):
                try:
                    config_mtimes[key] = os.path.getmtime(path)
                except Exception:
                    config_mtimes[key] = None
            rtl_active_enter = unit_active_enter_epoch(UNITS["rtl"])
            rtl_restart_required = False
            if rtl_active_enter and config_mtimes.get("combined"):
                rtl_restart_required = config_mtimes["combined"] > rtl_active_enter
            # Crash-loop detection: rtl_active above is just is-active, which
            # stays true through the brief active window of each restart
            # cycle.  Sample NRestarts on a sliding window so the API can
            # actually tell when the unit is thrashing.
            rtl_restart_loop = _unit_restart_loop_state(UNITS["rtl"])
            if rtl_restart_loop.get("loop_detected"):
                # Effective health drops even though is-active is true.
                rtl_ok = False
                ground_ok = False
            try:
                favorites_runtime_sync = get_last_favorites_runtime_sync()
            except Exception as e:
                favorites_runtime_sync = {
                    "ok": False,
                    "changed": False,
                    "errors": [str(e)],
                }

            # Heartbeat truth: post-MA/SL split, the legacy "rtl-airband"
            # combined unit is masked, so rtl_ok/ground_ok would always
            # read False even though analog is healthy on RSPduo.  Expose
            # the per-service truth and prefer it for downstream
            # heartbeats / SDR-active dots.  Trust unit-active alone
            # because rtl_airband's stats file isn't updated frequently
            # enough to pass the 15s freshness gate on RSPduo backends —
            # the unit being active is sufficient liveness for the
            # dashboard heartbeats.
            airband_active_truth = (
                bool(rtl_airband_unit_active)
                if rtl_airband_unit_active
                else bool(rtl_ok)
            )
            ground_active_truth = (
                bool(rtl_ground_unit_active)
                if rtl_ground_unit_active
                else bool(ground_ok)
            )
            payload = {
                "rtl_active": rtl_ok,
                "airband_active": airband_active_truth,
                "ground_active": ground_active_truth,
                "ground_exists": ground_present,
                "rtl_unit_active": rtl_unit_active,
                "ground_unit_active": ground_unit_active,
                "rtl_airband_sample_flow_ok": sample_flow_ok,
                "rtl_airband_stats_age_sec": sample_flow.get("stats_age_sec"),
                "rtl_airband_stats_stale_threshold_sec": sample_flow.get(
                    "stale_threshold_sec"
                ),
                "rtl_airband_sample_flow_reason": sample_flow.get("reason") or "",
                # MA/SL split-process truth fields.  Pre-cutover the
                # underlying units don't exist yet so these report
                # ``False`` honestly.  Post-cutover these become the
                # primary signals the dashboard heartbeats consume.
                "rtl_airband_service_active": rtl_airband_ok,
                "rtl_airband_unit_active": rtl_airband_unit_active,
                "rtl_airband_service_sample_flow_ok": rtl_airband_sample_flow_ok,
                "rtl_airband_service_stats_age_sec": sample_flow_airband.get("stats_age_sec"),
                "rtl_airband_service_sample_flow_reason": sample_flow_airband.get("reason") or "",
                "rtl_ground_service_active": rtl_ground_ok,
                "rtl_ground_unit_active": rtl_ground_unit_active,
                "rtl_ground_service_sample_flow_ok": rtl_ground_sample_flow_ok,
                "rtl_ground_service_stats_age_sec": sample_flow_ground.get("stats_age_sec"),
                "rtl_ground_service_sample_flow_reason": sample_flow_ground.get("reason") or "",
                "rtl_restart_loop": rtl_restart_loop,
                "rtl_restart_count": rtl_restart_loop.get("count"),
                "rtl_restart_loop_detected": bool(rtl_restart_loop.get("loop_detected")),
                "combined_config_stale": combined_stale,
                "combined_devices": len(combined_info.get("devices") or []),
                "combined_devices_detail": combined_info.get("devices") or [],
                "airband_present": airband_present,
                "ground_present": ground_present,
                "icecast_active": ice_ok,
                "icecast_unit_active": ice_unit_active,
                "icecast_mount_analog_alive": bool(mount_analog),
                "icecast_mount_digital_alive": bool(mount_digital),
                "icecast_mount_analog_ground_alive": bool(mount_analog_ground),
                "icecast_mounts": icecast_mounts,
                "icecast_port": ICECAST_PORT,
                "stream_mount": analog_stream_mount,
                "stream_proxy_enabled": True,
                "digital_stream_mount": digital_stream_mount,
                "ground_stream_mount": "ANALOG_GROUND.mp3",
                "icecast_expected_mounts": [],
                "expected_serials": expected_serials,
                "expected_indices": expected_indices,
                "digital_tuner_targets": _digital_tuner_targets(),
                "serial_mismatch": bool(serial_mismatch_detail),
                "serial_mismatch_detail": serial_mismatch_detail,
                "index_mismatch": bool(index_mismatch_detail),
                "index_mismatch_detail": index_mismatch_detail,
                "keepalive_active": keepalive_unit_active,
                "server_time": time.time(),
                "server_timezone": _RESOLVED_SERVER_TIMEZONE,
                "rtl_active_enter": rtl_active_enter,
                "rtl_restart_required": rtl_restart_required,
                "config_paths": {
                    "airband": conf_path,
                    "ground": ground_conf_path,
                    "combined": combined_conf_path,
                },
                "config_paths_controls": {
                    "airband": controls_airband_path,
                    "ground": controls_ground_path,
                },
                "config_mtimes": config_mtimes,
                "profile_airband": profile_airband,
                "profile_ground": profile_ground,
                "profiles_airband": profiles_airband,
                "profiles_ground": profiles_ground,
                "missing_profiles": missing,
                "gain": float(airband_gain),
                "airband_gain": float(airband_gain),
                "airband_squelch_mode": airband_mode,
                "airband_squelch_dbfs": float(airband_dbfs),
                "airband_squelch_preset": (
                    (recommended_managed_controls("airband", controls_airband_path) or {}).get("squelch_preset")
                    or SQUELCH_DEFAULT_PRESET
                ),
                "airband_squelch_margin_db": (
                    (recommended_managed_controls("airband", controls_airband_path) or {}).get("squelch_preset_margin_db")
                    or squelch_margin_for(SQUELCH_DEFAULT_PRESET)
                ),
                "airband_squelch_noise_floor_dbfs": (
                    (recommended_managed_controls("airband", controls_airband_path) or {}).get("squelch_preset_noise_floor_dbfs")
                ),
                # SB5 Phase 2: AUTO/MANUAL toggle + tracker bookkeeping.
                # The UI uses these to paint the AUTO pill state and
                # render the "Auto · last sync Xs ago" timestamp under
                # the chip row.
                "airband_squelch_auto": bool(_get_band_squelch_auto("airband")),
                "airband_squelch_tracker_applied_at_ms": (
                    (recommended_managed_controls("airband", controls_airband_path) or {}).get("squelch_tracker_applied_at_ms")
                ),
                "airband_squelch_tracker_last_cycle_ms": int(
                    _tracker_status("airband").get("last_cycle_ms") or 0
                ),
                "airband_filter": float(airband_filter),
                "ground_gain": float(ground_gain),
                "ground_squelch_mode": ground_mode,
                "ground_squelch_dbfs": float(ground_dbfs),
                "ground_squelch_preset": (
                    (recommended_managed_controls("ground", controls_ground_path) or {}).get("squelch_preset")
                    or SQUELCH_DEFAULT_PRESET
                ),
                "ground_squelch_margin_db": (
                    (recommended_managed_controls("ground", controls_ground_path) or {}).get("squelch_preset_margin_db")
                    or squelch_margin_for(SQUELCH_DEFAULT_PRESET)
                ),
                "ground_squelch_noise_floor_dbfs": (
                    (recommended_managed_controls("ground", controls_ground_path) or {}).get("squelch_preset_noise_floor_dbfs")
                ),
                "ground_squelch_auto": bool(_get_band_squelch_auto("ground")),
                "ground_squelch_tracker_applied_at_ms": (
                    (recommended_managed_controls("ground", controls_ground_path) or {}).get("squelch_tracker_applied_at_ms")
                ),
                "ground_squelch_tracker_last_cycle_ms": int(
                    _tracker_status("ground").get("last_cycle_ms") or 0
                ),
                "ground_filter": float(ground_filter),
                "airband_applied_gain": airband_device.get("gain") if airband_device else None,
                "airband_applied_squelch_dbfs": airband_device.get("squelch_dbfs") if airband_device else None,
                "ground_applied_gain": ground_device.get("gain") if ground_device else None,
                "ground_applied_squelch_dbfs": ground_device.get("squelch_dbfs") if ground_device else None,
                "last_hit": latest_hit or last_hit_airband or last_hit_ground or "",
                "last_hit_airband": last_hit_airband,
                "last_hit_ground": last_hit_ground,
                "last_hit_airband_label": _short_label(last_hit_airband_label, max_len=48),
                "last_hit_ground_label": _short_label(last_hit_ground_label, max_len=48),
                "avoids_airband": summarize_avoids(conf_path, "airband"),
                "avoids_ground": summarize_avoids(os.path.realpath(GROUND_CONFIG_PATH), "ground"),
                "hp_avoids": get_scan_mode_controller().get_hp_avoids(),
                "stripped_custom_favorites": {
                    str(tag_id): bucket
                    for tag_id, bucket in (get_scan_mode_controller().get_last_stripped_custom_favorites() or {}).items()
                },
                "favorites_runtime_sync": favorites_runtime_sync,
                "analog_scan_health": analog_scan_health,
            }
            digital_payload = {
                "digital_active": False,
                "digital_backend": "",
                "digital_profile": "",
                "digital_muted": False,
                "digital_last_label": "",
                "digital_last_time": 0,
                "digital_last_warning": "",
            }
            try:
                digital_payload = get_digital_manager().status_payload()
            except Exception as e:
                digital_payload["digital_last_error"] = str(e)
            mixer_enabled, mixer_active = _digital_mixer_runtime_state()
            digital_payload["digital_audio_active"] = bool(digital_audio_unit_active)
            digital_payload["digital_mixer_enabled"] = bool(mixer_enabled)
            digital_payload["digital_mixer_active"] = bool(mixer_active)
            digital_stream_active_for_hits = True
            if DIGITAL_HITS_REQUIRE_ACTIVE_STREAM:
                try:
                    digital_stream_active_for_hits = _digital_stream_active_for_hits()
                except Exception:
                    digital_stream_active_for_hits = True
            # Preserve raw digital activity indicators even when stream
            # mount-state is uncertain; expose stream visibility separately.
            digital_payload["digital_stream_active_for_hits"] = bool(digital_stream_active_for_hits)
            digital_payload = _digital_status_with_hit_aliases(digital_payload, full_hit_items)
            try:
                restart_state = digital_restart_state()
                digital_payload["digital_restart_attempts"] = int(
                    restart_state.get("attempts_total") or 0
                )
                digital_payload["digital_last_restart_reason"] = str(
                    restart_state.get("last_attempt_reason") or ""
                )
                digital_payload["digital_health_probe_result"] = str(
                    restart_state.get("last_health_probe_result") or ""
                )
                digital_payload["digital_health_probe_detail"] = str(
                    restart_state.get("last_health_probe_detail") or ""
                )
                digital_payload["digital_wedge_recovery_total"] = int(
                    restart_state.get("wedge_recovery_total") or 0
                )
                digital_payload["digital_last_wedge_recovery_ts"] = float(
                    restart_state.get("last_wedge_recovery_ts") or 0.0
                )
            except Exception:
                pass
            # Symmetric rtl-airband restart telemetry — same keys as the
            # digital block above, just rtl_-prefixed.  Useful for the
            # sitrep and any external watchdog correlating wedge events
            # across the two SoapySDR-consuming pipelines.
            try:
                rtl_state = rtl_restart_state()
                payload["rtl_restart_attempts"] = int(
                    rtl_state.get("attempts_total") or 0
                )
                payload["rtl_last_restart_reason"] = str(
                    rtl_state.get("last_attempt_reason") or ""
                )
                payload["rtl_health_probe_result"] = str(
                    rtl_state.get("last_health_probe_result") or ""
                )
                payload["rtl_health_probe_detail"] = str(
                    rtl_state.get("last_health_probe_detail") or ""
                )
                payload["rtl_wedge_recovery_total"] = int(
                    rtl_state.get("wedge_recovery_total") or 0
                )
                payload["rtl_last_wedge_recovery_ts"] = float(
                    rtl_state.get("last_wedge_recovery_ts") or 0.0
                )
            except Exception:
                pass
            # MA/SL split-process restart telemetry.  Each band's
            # restart machinery has its own state dict (mirrors the
            # legacy ``_RTL_RESTART_STATE`` shape); surface both so
            # the dashboard can show independent wedge counters per
            # band and any watchdog correlating events across bands
            # has both signals available.  Empty/zero before the
            # cutover when the new functions haven't been called yet.
            for prefix, state_fn in (
                ("rtl_airband", rtl_airband_restart_state),
                ("rtl_ground", rtl_ground_restart_state),
            ):
                try:
                    svc_state = state_fn()
                    payload[f"{prefix}_service_restart_attempts"] = int(
                        svc_state.get("attempts_total") or 0
                    )
                    payload[f"{prefix}_service_last_restart_reason"] = str(
                        svc_state.get("last_attempt_reason") or ""
                    )
                    payload[f"{prefix}_service_health_probe_result"] = str(
                        svc_state.get("last_health_probe_result") or ""
                    )
                    payload[f"{prefix}_service_health_probe_detail"] = str(
                        svc_state.get("last_health_probe_detail") or ""
                    )
                    payload[f"{prefix}_service_wedge_recovery_total"] = int(
                        svc_state.get("wedge_recovery_total") or 0
                    )
                    payload[f"{prefix}_service_last_wedge_recovery_ts"] = float(
                        svc_state.get("last_wedge_recovery_ts") or 0.0
                    )
                except Exception:
                    pass
            payload.update(digital_payload)
            payload["icecast_expected_mounts"] = _expected_icecast_mounts(
                analog_active=bool(rtl_unit_active or ground_unit_active),
                keepalive_active=bool(keepalive_unit_active),
                digital_active=bool(payload.get("digital_active")),
            )
            payload["sb3_connected_status_refresh_sec"] = int(SB3_CONNECTED_STATUS_REFRESH_SEC)
            payload["sb3_connected_system_refresh_sec"] = int(SB3_CONNECTED_SYSTEM_REFRESH_SEC)
            payload["sb3_connected_profiles_refresh_sec"] = int(SB3_CONNECTED_PROFILES_REFRESH_SEC)
            payload["sb3_dedicated_digital_fetch_enabled"] = bool(SB3_DEDICATED_DIGITAL_FETCH_ENABLED)
            try:
                compile_state = load_compiled_state() or {}
            except Exception:
                compile_state = {}
            try:
                system_stats = get_system_stats()
            except Exception:
                system_stats = {"ok": False}
            dongle_snapshot = (system_stats or {}).get("dongles") or None
            analog_air_preflight = evaluate_analog_preflight(
                "airband",
                strict=False,
                dongles=dongle_snapshot,
                compile_state=compile_state,
            )
            analog_ground_preflight = evaluate_analog_preflight(
                "ground",
                strict=False,
                dongles=dongle_snapshot,
                compile_state=compile_state,
            )
            digital_preflight = evaluate_digital_preflight(
                profile_id=str(digital_payload.get("digital_profile") or ""),
                strict=False,
                dongles=dongle_snapshot,
                compile_state=compile_state,
                manager_preflight=digital_payload.get("digital_preflight"),
            )
            payload["v3_compile"] = compile_state
            payload["preflight"] = {
                "airband": analog_air_preflight,
                "ground": analog_ground_preflight,
                "digital": digital_preflight,
            }
            payload["health"] = _build_health_payload(
                status_payload=payload,
                system_stats=system_stats,
                analog_air_preflight=analog_air_preflight,
                analog_ground_preflight=analog_ground_preflight,
                digital_preflight=digital_preflight,
                compile_state=compile_state,
            )
            wx_store = get_met_store()
            payload["wx_decoder_active"] = wx_store.active_decoder
            payload["wx_collecting"] = wx_store.collecting
            payload["wx_met_count"] = wx_store.get_status().get("met_count", 0)
            try:
                payload["dongle_power"] = get_power_state()
                payload["dongle_schedule"] = load_schedule()
            except Exception:
                payload["dongle_power"] = "unknown"
                payload["dongle_schedule"] = {}
            # PR #35 — Owntracks adapter counters.
            payload["owntracks_invocations_total"] = int(_OWNTRACKS_STATS["invocations_total"])
            payload["owntracks_pushes_accepted_total"] = int(_OWNTRACKS_STATS["pushes_accepted_total"])
            payload["owntracks_pushes_rejected_total"] = int(_OWNTRACKS_STATS["pushes_rejected_total"])
            payload["owntracks_last_push_ts"] = float(_OWNTRACKS_STATS["last_push_ts"] or 0.0)
            payload["owntracks_last_lat"] = _OWNTRACKS_STATS["last_lat"]
            payload["owntracks_last_lon"] = _OWNTRACKS_STATS["last_lon"]
            payload["owntracks_last_battery_pct"] = _OWNTRACKS_STATS["last_battery_pct"]
            with _CACHE_LOCK:
                _STATUS_CACHE["ts"] = now_monotonic
                _STATUS_CACHE["payload"] = dict(payload)
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")
        if p == "/api/profiles":
            profiles = load_profiles_registry()
            prof_payload, profiles_airband, profiles_ground = split_profiles()
            airband_conf = read_active_config_path()
            ground_conf = os.path.realpath(GROUND_CONFIG_PATH)
            active_airband_id = ""
            active_ground_id = ""
            for pitem in profiles:
                path = pitem.get("path")
                if path and os.path.realpath(path) == os.path.realpath(airband_conf):
                    active_airband_id = pitem.get("id", "")
                if path and os.path.realpath(path) == os.path.realpath(ground_conf):
                    active_ground_id = pitem.get("id", "")
            payload = {
                "ok": True,
                "profiles": prof_payload,
                "profiles_airband": profiles_airband,
                "profiles_ground": profiles_ground,
                "active_airband_id": active_airband_id,
                "active_ground_id": active_ground_id,
            }
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")
        if p == "/api/digital/profiles":
            try:
                manager = get_digital_manager()
                payload = {
                    "ok": True,
                    "profiles": manager.listProfiles(),
                    "active": manager.getProfile(),
                }
            except Exception as e:
                payload = {"ok": False, "error": str(e)}
                return self._send(500, json.dumps(payload), "application/json; charset=utf-8")
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")
        if p == "/api/digital/preflight":
            try:
                manager = get_digital_manager()
                preflight = manager.preflight() or {}
            except Exception as e:
                preflight = {"tuner_busy": False, "tuner_busy_lines": [], "error": str(e)}
            combined_info = combined_device_summary()
            airband_serial = AIRBAND_RTL_SERIAL or (combined_info.get("airband") or {}).get("serial")
            ground_serial = GROUND_RTL_SERIAL or (combined_info.get("ground") or {}).get("serial")
            effective_digital_serials = _effective_digital_rtl_serials()
            digital_serial_configured = bool(effective_digital_serials)
            digital_tuner_target_configured = bool(_digital_tuner_targets())
            payload = {
                "ok": True,
                "expected_serials": {
                    "airband": airband_serial,
                    "ground": ground_serial,
                    "digital": effective_digital_serials[0] if len(effective_digital_serials) > 0 else "",
                    "digital_secondary": effective_digital_serials[1] if len(effective_digital_serials) > 1 else "",
                    "digital_tertiary": effective_digital_serials[2] if len(effective_digital_serials) > 2 else "",
                },
                "digital_serial_configured": digital_serial_configured,
                "digital_tuner_target_configured": digital_tuner_target_configured,
                "digital_tuner_targets": _digital_tuner_targets(),
                "tuner_busy": bool(preflight.get("tuner_busy")),
                "tuner_busy_lines": preflight.get("tuner_busy_lines") or [],
                "tuner_busy_count": int(preflight.get("tuner_busy_count") or 0),
                "tuner_busy_last_time_ms": int(preflight.get("tuner_busy_last_time_ms") or 0),
                "playlist_source_ok": bool(preflight.get("playlist_source_ok")),
                "playlist_source_type": preflight.get("playlist_source_type") or "",
                "playlist_source_config_type": preflight.get("playlist_source_config_type") or "",
                "playlist_frequency_count": int(preflight.get("playlist_frequency_count") or 0),
                "playlist_frequency_hz": preflight.get("playlist_frequency_hz") or [],
                "playlist_preferred_tuner": preflight.get("playlist_preferred_tuner") or "",
                "playlist_source_error": preflight.get("playlist_source_error") or "",
                "listen_filter_ok": bool(preflight.get("listen_filter_ok")),
                "listen_filter_blocking": bool(preflight.get("listen_filter_blocking")),
                "listen_filter_error": preflight.get("listen_filter_error") or "",
                "listen_talkgroup_count": int(preflight.get("listen_talkgroup_count") or 0),
                "listen_enabled_count": int(preflight.get("listen_enabled_count") or 0),
                "listen_default": bool(preflight.get("listen_default")),
                "listen_map_entries": int(preflight.get("listen_map_entries") or 0),
                "rtl_devices": [],
                "rtl_devices_note": "not implemented",
                "device_holders": {"ok": False, "error": "not implemented"},
            }
            if not digital_tuner_target_configured:
                payload["digital_serial_hint"] = DIGITAL_RTL_SERIAL_HINT
                payload["digital_serial_help"] = "Set DIGITAL_RTL_SERIAL or DIGITAL_PREFERRED_TUNER in your EnvironmentFile and restart airband-ui."
            if preflight.get("error"):
                payload["error"] = preflight.get("error")
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")
        if p == "/api/digital/scheduler":
            try:
                manager = get_digital_manager()
                payload = manager.getScheduler() if hasattr(manager, "getScheduler") else {}
                payload = dict(payload or {})
                payload["ok"] = True
                return self._send(200, json.dumps(payload), "application/json; charset=utf-8")
            except Exception as e:
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
        if p == "/api/digital/dongle-assignments":
            try:
                assignments = load_dongle_assignments()
                payload = dict(assignments) if assignments else {}
                payload["ok"] = True
                return self._send(200, json.dumps(payload), "application/json; charset=utf-8")
            except Exception as e:
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
        if p == "/api/dongles/power":
            try:
                state = get_power_state()
                schedule = load_schedule()
                return self._send(
                    200,
                    json.dumps({"ok": True, "state": state, "schedule": schedule}),
                    "application/json; charset=utf-8",
                )
            except Exception as e:
                logger.exception("GET /api/dongles/power failed")
                return self._send(500, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")
        if p == "/api/preflight":
            q = parse_qs(u.query or "")
            action = (q.get("action") or [""])[0].strip()
            target = (q.get("target") or [""])[0].strip()
            profile_id = (q.get("profileId") or [""])[0].strip()
            payload = gate_action(
                action,
                target=target,
                profile_id=profile_id,
                strict=False,
            )
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")
        if p == "/api/v3/compile-state":
            payload = {"ok": True, "state": load_compiled_state()}
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")
        if p == "/api/digital/talkgroups":
            q = parse_qs(u.query or "")
            profile_id = (q.get("profileId") or [""])[0].strip()
            if not profile_id:
                return self._send(400, json.dumps({"ok": False, "error": "missing profileId"}), "application/json; charset=utf-8")
            ok, payload = read_digital_talkgroups(profile_id)
            if not ok:
                return self._send(400, json.dumps({"ok": False, "error": payload}), "application/json; charset=utf-8")
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")
        # ---- Weather sounding (ACARS / Radiosonde) endpoints ----

        if p == "/api/wx/status":
            store = get_met_store()
            status = store.get_status()
            _acars = "/usr/local/bin/acarsdec"
            status["acars_installed"] = bool(shutil.which("acarsdec") or os.path.isfile(_acars))
            _vdl2 = "/usr/local/bin/dumpvdl2"
            status["vdl2_installed"] = bool(shutil.which("dumpvdl2") or os.path.isfile(_vdl2))
            _autorx = "/opt/radiosonde_auto_rx/auto_rx.py"
            status["radiosonde_installed"] = bool(shutil.which("auto_rx.py") or os.path.isfile(_autorx))
            return self._send(200, json.dumps(status), "application/json; charset=utf-8")

        if p == "/api/wx/messages":
            q = parse_qs(u.query or "")
            limit = int((q.get("limit") or ["50"])[0])
            source = (q.get("source") or [None])[0]
            store = get_met_store()
            msgs = store.get_messages(limit=limit, source=source)
            return self._send(200, json.dumps({"ok": True, "messages": msgs}), "application/json; charset=utf-8")

        if p == "/api/wx/messages/raw":
            q = parse_qs(u.query or "")
            limit = int((q.get("limit") or ["200"])[0])
            source = (q.get("source") or [None])[0]
            store = get_met_store()
            msgs = store.get_messages(limit=limit, source=source)
            logger.debug(
                "/api/wx/messages/raw: limit=%d source=%r returning %d messages",
                limit, source, len(msgs),
            )
            return self._send(200, json.dumps({"ok": True, "messages": msgs}), "application/json; charset=utf-8")

        if p == "/api/wx/sounding":
            store = get_met_store()
            data = store.get_sounding_data()
            return self._send(200, json.dumps({"ok": True, **data}), "application/json; charset=utf-8")

        if p == "/api/wx/sounding/parameters":
            store = get_met_store()
            data = store.get_sounding_data()
            levels = _filter_sounding_levels(data.get("levels", []))
            if len(levels) < 5:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": f"Not enough data: {len(levels)} levels (need ≥5 from last 90 min)"}),
                    "application/json; charset=utf-8",
                )
            result = _compute_sounding_params(levels)
            result["levels_used"] = len(levels)
            return self._send(200, json.dumps({"ok": True, **result}), "application/json; charset=utf-8")

        if p == "/api/wx/sounding/skewt":
            store = get_met_store()
            data = store.get_sounding_data()
            levels = _filter_sounding_levels(data.get("levels", []))
            if len(levels) < 5:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": f"Not enough data: {len(levels)} levels (need ≥5 from last 90 min)"}),
                    "application/json; charset=utf-8",
                )
            try:
                params_dict = _compute_sounding_params(levels)
                png_bytes = _generate_skewt_png(levels, params_dict)
            except Exception as exc:
                logging.exception("skewt generation failed")
                return self._send(500, json.dumps({"ok": False, "error": str(exc)}), "application/json; charset=utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png_bytes)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(png_bytes)
            return

        if p == "/api/wx/export":
            q = parse_qs(u.query or "")
            fmt = (q.get("format") or ["json"])[0]
            store = get_met_store()
            if fmt == "spc":
                body = store.get_sounding_spc()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Disposition", "attachment; filename=sounding.txt")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
                return
            elif fmt == "csv":
                data = store.get_sounding_data()
                cols = ["timestamp", "source", "source_id", "altitude_ft", "pressure_hpa",
                        "temp_c", "dewpoint_c", "wind_dir_deg", "wind_speed_kt", "humidity_pct", "lat", "lon"]
                lines = [",".join(cols)]
                for obs in data.get("levels", []):
                    lines.append(",".join(str(obs.get(c, "")) for c in cols))
                body = "\n".join(lines)
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", "attachment; filename=sounding.csv")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
                return
            else:
                data = store.get_sounding_data()
                body = json.dumps(data)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition", "attachment; filename=sounding.json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
                return

        if p == "/api/hits":
            payload = _get_hits_payload_cached(limit=50)
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")
        
        if p == "/api/spectrum":
            # One-shot spectrum data
            band = parse_qs(urlparse(self.path).query).get("band", ["airband"])[0]
            return self._send(200, spectrum_to_json(band), "application/json; charset=utf-8")
        
        if p == "/api/stream":
            # Server-Sent Events stream for real-time updates
            return self._handle_sse_stream()

        # ============================================================
        # Phase 5a — mock dongle-pane endpoints.  Real RF-backed
        # implementations land per-pane as the 6 RTL-SDRs come online.
        # All three GETs return realistic mock JSON; the URL-hash
        # dongle-lost overlay is purely client-side, so the GETs do
        # not return "dongle-lost" in normal operation.
        # ============================================================
        if p == "/api/waterfall":
            # Phase 6a: file-backed pass-through.  Returns state.json
            # written by scanner-waterfall.service, or a "down" stub if
            # the service isn't running / state is stale.
            payload = _waterfall_pass_through_payload()
            return self._send(
                200,
                json.dumps(payload),
                "application/json; charset=utf-8",
            )

        if p == "/api/vfo":
            # Phase 6b: file-backed pass-through.  Returns state.json
            # written by scanner-vfo.service, or a "down" stub if the
            # service isn't running / state is stale.
            payload = _vfo_pass_through_payload()
            return self._send(
                200,
                json.dumps(payload),
                "application/json; charset=utf-8",
            )

        if p == "/api/disco":
            # Phase 6c: file-backed pass-through. Returns coord_state.json
            # written by disco-coordinator.service, or a 'down' stub if
            # the service isn't running / state is stale.
            payload = _disco_pass_through_payload()
            return self._send(
                200,
                json.dumps(payload),
                "application/json; charset=utf-8",
            )

        if p == "/api/disco/recent":
            # Operator hits-history feed for the Discovery fullscreen
            # table. Reads disco-coordinator's SQLite DB directly. Query
            # params: limit (1..500, default 100), unique (default true).
            try:
                qs = parse_qs(urlparse(self.path).query)
                try:
                    limit = int((qs.get("limit") or ["100"])[0])
                except (TypeError, ValueError):
                    limit = 100
                uval = (qs.get("unique") or ["true"])[0].lower()
                unique = uval not in ("0", "false", "no")
                payload = _disco_recent_hits(limit=limit, unique=unique)
            except Exception as exc:
                logger.exception("/api/disco/recent failed")
                payload = {"rows": [], "error": str(exc),
                           "source": "down", "server_time": time.time()}
                return self._send(500, json.dumps(payload),
                                  "application/json; charset=utf-8")
            return self._send(
                200, json.dumps(payload),
                "application/json; charset=utf-8",
            )


        if p == "/api/sounding":
            # Phase 6d: file-backed pass-through. Returns broker state.json
            # written by scanner-tuner-broker.service, or a 'down' stub if
            # the broker isn't running / state is stale.
            payload = _broker_pass_through_payload()
            return self._send(
                200,
                json.dumps(payload),
                "application/json; charset=utf-8",
            )

        if p == "/api/sounding/detail":
            # Phase 7d — live state for the /sb5 Sounding pane: VDL2 +
            # ACARS active/inactive, recent decode counts, last decoded
            # message summary.  Reads dumpvdl2 / acarsdec output files.
            try:
                payload = _compute_sounding_detail_payload()
            except Exception as exc:
                logger.exception("/api/sounding/detail failed")
                payload = {
                    "sounding_on": False,
                    "vdl2": {"active": False, "error": str(exc),
                             "recent_count": 0, "last_age_ms": None,
                             "last_summary": "", "last_raw": None,
                             "dongle": "dedicated"},
                    "acars": {"active": False, "error": str(exc),
                              "recent_count": 0, "last_age_ms": None,
                              "last_summary": "", "last_raw": None,
                              "dongle": "shared via broker"},
                    "server_time": time.time(),
                }
                return self._send(500, json.dumps(payload), "application/json; charset=utf-8")
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        return self._send(404, "Not found", "text/plain; charset=utf-8")

    def _handle_sse_stream(self):
        """Handle SSE stream for real-time data."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            while True:
                controls_snapshot = _read_effective_analog_controls()
                airband_gain = controls_snapshot["airband_gain"]
                airband_dbfs = controls_snapshot["airband_dbfs"]
                airband_mode = controls_snapshot["airband_mode"]
                rtl_unit_active = _unit_active_cached(UNITS["rtl"])
                ground_unit_active = _unit_active_cached(UNITS["ground"])
                combined_info = combined_device_summary()
                ground_present = combined_info.get("ground") is not None
                # Sample-flow gate: see /api/status path above for rationale.
                # Both code paths must agree or the SSE-driven sitrep dots
                # will disagree with the polled /api/status view.
                sample_flow = rtl_airband_sample_flow_state(
                    RTL_AIRBAND_STATS_PATH,
                    RTL_AIRBAND_STATS_STALE_SEC,
                )
                sample_flow_ok = bool(sample_flow.get("sample_flow_ok"))
                rtl_active = rtl_unit_active and sample_flow_ok
                # MA/SL split signals (post-cutover analog runs on RSPduo
                # in two services; the legacy "rtl-airband" unit is
                # masked).  Use these to drive airband_active /
                # ground_active so heartbeat dots strobe correctly.
                rtl_airband_unit_active = _unit_active_cached(UNITS.get("rtl_airband", ""))
                rtl_ground_unit_active = _unit_active_cached(UNITS.get("rtl_ground", ""))
                rtl_airband_split_flow = rtl_airband_sample_flow_state(
                    RTL_AIRBAND_AIRBAND_STATS_PATH,
                    RTL_AIRBAND_STATS_STALE_SEC,
                )
                rtl_ground_split_flow = rtl_airband_sample_flow_state(
                    RTL_AIRBAND_GROUND_STATS_PATH,
                    RTL_AIRBAND_STATS_STALE_SEC,
                )
                rtl_airband_split_ok = (
                    rtl_airband_unit_active
                    and bool(rtl_airband_split_flow.get("sample_flow_ok"))
                )
                rtl_ground_split_ok = (
                    rtl_ground_unit_active
                    and bool(rtl_ground_split_flow.get("sample_flow_ok"))
                )
                # See /api/status above: trust unit-active alone because
                # the rtl-airband stats file isn't updated frequently
                # enough to pass the 15s freshness gate on RSPduo.
                airband_active = (
                    bool(rtl_airband_unit_active)
                    if rtl_airband_unit_active
                    else rtl_active
                )
                ground_active = (
                    bool(rtl_ground_unit_active)
                    if rtl_ground_unit_active
                    else (rtl_active and ground_present)
                )
                ice_unit_active = _unit_active_cached(UNITS["icecast"])
                ice_ok = ice_unit_active
                icecast_status_text = ""
                analog_stream_mount = str(PLAYER_MOUNT or "").strip().lstrip("/")
                digital_stream_mount = str(DIGITAL_STREAM_MOUNT or "").strip().lstrip("/")
                if ice_unit_active:
                    try:
                        icecast_status_text = fetch_local_icecast_status()
                        analog_stream_mount = _resolve_analog_stream_mount(icecast_status_text)
                        digital_stream_mount = _resolve_digital_stream_mount(icecast_status_text)
                    except Exception:
                        icecast_status_text = ""
                        analog_stream_mount = str(PLAYER_MOUNT or "").strip().lstrip("/")
                        digital_stream_mount = str(DIGITAL_STREAM_MOUNT or "").strip().lstrip("/")
                mount_analog = mount_publishing(icecast_status_text, PLAYER_MOUNT or "ANALOG.mp3")
                mount_digital = mount_publishing(
                    icecast_status_text, DIGITAL_STREAM_MOUNT or "DIGITAL.mp3"
                )
                # MA/SL split publishes ground audio on its own mount.
                # Hard-coded here because there's no separate ground stream
                # mount env var today; the rtl-airband-ground service config
                # always uses ANALOG_GROUND.mp3.
                ground_stream_mount = "ANALOG_GROUND.mp3"
                mount_analog_ground = mount_publishing(
                    icecast_status_text, ground_stream_mount
                )
                try:
                    from .dongle_power import get_power_state as _sse_get_dongle_power
                except ImportError:
                    from ui.dongle_power import get_power_state as _sse_get_dongle_power
                try:
                    sse_dongle_power = _sse_get_dongle_power()
                except Exception:
                    sse_dongle_power = "unknown"
                # Keep SSE hits aligned with the full UI hit list so digital
                # rows are not dropped by top-10 truncation during busy analog traffic.
                hits_payload = _get_hits_payload_cached(limit=50)
                hit_items = hits_payload.get("items") or []
                analog_scan_health = get_analog_scan_health()
                last_hit = hit_items[0].get("freq") if hit_items else (read_last_hit_airband() or read_last_hit_ground())
                last_hit_airband_label = ""
                last_hit_ground_label = ""
                for item in hit_items:
                    src = str(item.get("source") or "").strip().lower()
                    label = str(item.get("label_full") or item.get("label") or "").strip()
                    if src == "airband" and label and not last_hit_airband_label:
                        last_hit_airband_label = label
                    if src == "ground" and label and not last_hit_ground_label:
                        last_hit_ground_label = label
                    if last_hit_airband_label and last_hit_ground_label:
                        break
                digital_payload = {
                    "digital_active": False,
                    "digital_profile": "",
                    "digital_last_label": "",
                    "digital_last_time": 0,
                }
                try:
                    digital_payload = dict(get_digital_manager().status_payload() or {})
                except Exception:
                    digital_payload = {
                        "digital_active": False,
                        "digital_profile": "",
                        "digital_last_label": "",
                        "digital_last_time": 0,
                    }
                mixer_enabled, mixer_active = _digital_mixer_runtime_state()
                digital_payload["digital_mixer_enabled"] = bool(mixer_enabled)
                digital_payload["digital_mixer_active"] = bool(mixer_active)
                digital_payload = _digital_status_with_hit_aliases(digital_payload, hit_items)
                status_data = {
                    "type": "status",
                    "rtl_active": rtl_active,
                    "airband_active": airband_active,
                    "ground_active": ground_active,
                    "icecast_active": ice_ok,
                    "icecast_unit_active": ice_unit_active,
                    "icecast_mount_analog_alive": bool(mount_analog),
                    "icecast_mount_digital_alive": bool(mount_digital),
                    "icecast_mount_analog_ground_alive": bool(mount_analog_ground),
                    "dongle_power": sse_dongle_power,
                    "rtl_airband_sample_flow_ok": sample_flow_ok,
                    "rtl_airband_stats_age_sec": sample_flow.get("stats_age_sec"),
                    "rtl_airband_stats_stale_threshold_sec": sample_flow.get(
                        "stale_threshold_sec"
                    ),
                    "rtl_airband_sample_flow_reason": sample_flow.get("reason") or "",
                    "rtl_unit_active": rtl_unit_active,
                    "ground_unit_active": ground_unit_active,
                    "combined_config_stale": combined_config_stale(),
                    "gain": float(airband_gain),
                    "squelch_mode": airband_mode,
                    "squelch_dbfs": float(airband_dbfs),
                    "airband_squelch_dbfs": float(airband_dbfs),
                    "airband_squelch_mode": airband_mode,
                    "airband_squelch_preset": (
                        (recommended_managed_controls("airband", resolve_controls_path("airband")) or {}).get("squelch_preset")
                        or SQUELCH_DEFAULT_PRESET
                    ),
                    "airband_squelch_margin_db": (
                        (recommended_managed_controls("airband", resolve_controls_path("airband")) or {}).get("squelch_preset_margin_db")
                        or squelch_margin_for(SQUELCH_DEFAULT_PRESET)
                    ),
                    "airband_squelch_noise_floor_dbfs": (
                        (recommended_managed_controls("airband", resolve_controls_path("airband")) or {}).get("squelch_preset_noise_floor_dbfs")
                    ),
                    "airband_squelch_auto": bool(_get_band_squelch_auto("airband")),
                    "airband_squelch_tracker_applied_at_ms": (
                        (recommended_managed_controls("airband", resolve_controls_path("airband")) or {}).get("squelch_tracker_applied_at_ms")
                    ),
                    "airband_squelch_tracker_last_cycle_ms": int(
                        _tracker_status("airband").get("last_cycle_ms") or 0
                    ),
                    "ground_gain": float(ground_gain),
                    "ground_squelch_mode": ground_mode,
                    "ground_squelch_dbfs": float(ground_dbfs),
                    "ground_squelch_preset": (
                        (recommended_managed_controls("ground", resolve_controls_path("ground")) or {}).get("squelch_preset")
                        or SQUELCH_DEFAULT_PRESET
                    ),
                    "ground_squelch_margin_db": (
                        (recommended_managed_controls("ground", resolve_controls_path("ground")) or {}).get("squelch_preset_margin_db")
                        or squelch_margin_for(SQUELCH_DEFAULT_PRESET)
                    ),
                    "ground_squelch_noise_floor_dbfs": (
                        (recommended_managed_controls("ground", resolve_controls_path("ground")) or {}).get("squelch_preset_noise_floor_dbfs")
                    ),
                    "ground_squelch_auto": bool(_get_band_squelch_auto("ground")),
                    "ground_squelch_tracker_applied_at_ms": (
                        (recommended_managed_controls("ground", resolve_controls_path("ground")) or {}).get("squelch_tracker_applied_at_ms")
                    ),
                    "ground_squelch_tracker_last_cycle_ms": int(
                        _tracker_status("ground").get("last_cycle_ms") or 0
                    ),
                    "last_hit": last_hit,
                    "last_hit_airband_label": _short_label(last_hit_airband_label, max_len=48),
                    "last_hit_ground_label": _short_label(last_hit_ground_label, max_len=48),
                    "stream_mount": analog_stream_mount,
                    "digital_stream_mount": digital_stream_mount,
                    "ground_stream_mount": ground_stream_mount,
                    "server_time": time.time(),
                    "server_timezone": _RESOLVED_SERVER_TIMEZONE,
                    "hp_avoids": get_scan_mode_controller().get_hp_avoids(),
                    "stripped_custom_favorites": {
                        str(tag_id): bucket
                        for tag_id, bucket in (get_scan_mode_controller().get_last_stripped_custom_favorites() or {}).items()
                    },
                    "analog_scan_health": analog_scan_health,
                    "sb3_connected_status_refresh_sec": int(SB3_CONNECTED_STATUS_REFRESH_SEC),
                    "sb3_connected_system_refresh_sec": int(SB3_CONNECTED_SYSTEM_REFRESH_SEC),
                    "sb3_connected_profiles_refresh_sec": int(SB3_CONNECTED_PROFILES_REFRESH_SEC),
                    "sb3_dedicated_digital_fetch_enabled": bool(SB3_DEDICATED_DIGITAL_FETCH_ENABLED),
                }
                status_data.update(digital_payload)
                self.wfile.write(f"event: status\ndata: {json.dumps(status_data)}\n\n".encode())
                spectrum_data = {
                    "type": "spectrum",
                    "bins": [],
                    "timestamp": time.time(),
                    "note": "stats_filepath not supported in rtl_airband v5.1.1"
                }
                self.wfile.write(f"event: spectrum\ndata: {json.dumps(spectrum_data)}\n\n".encode())
                hits_data = {
                    "type": "hits",
                    "items": hit_items,
                }
                self.wfile.write(f"event: hits\ndata: {json.dumps(hits_data)}\n\n".encode())
                self.wfile.flush()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        """Handle POST requests."""

        p = _canonical_scan_api_path(urlparse(self.path).path)
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8", errors="ignore")
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "application/json" in ctype:
            try:
                data = json.loads(raw) if raw.strip() else {}
                form = data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                form = {}
        else:
            parsed_form = parse_qs(raw, keep_blank_values=True)
            form = {}
            for key, values in parsed_form.items():
                if not values:
                    form[key] = ""
                    continue
                if key == "selected_profiles":
                    form[key] = ",".join(str(item or "").strip() for item in values)
                    continue
                form[key] = values[0]

        def get_str(key: str, default: str = "") -> str:
            v = form.get(key, default)
            if v is None:
                return default
            return str(v)

        def parse_bool_value(raw_value, *, field: str) -> bool:
            return _parse_bool_value(raw_value, field=field)

        def parse_float_value(raw_value, *, field: str) -> float:
            return _parse_float_value(raw_value, field=field)

        def parse_json_like_list(raw_value) -> list:
            return _parse_json_like_list(raw_value)

        if p == "/api/sitrep/action":
            # Sitrep Controls — Reboot Micro + targeted service restarts.
            # The client confirms with the operator and POSTs
            # {"action": "<key>"}; we map the key to a fixed argv and run
            # it.  Arbitrary commands are NOT accepted.
            action = get_str("action", "").strip().lower()
            if action not in _SITREP_ACTIONS:
                return self._send(
                    400,
                    json.dumps({
                        "ok": False,
                        "error": f"unknown action: {action!r}",
                        "actions": list(_SITREP_ACTIONS.keys()),
                    }),
                    "application/json; charset=utf-8",
                )
            ok, msg, err = _run_sitrep_action(action)
            body = {"ok": bool(ok)}
            if ok:
                body["message"] = msg
            else:
                body["error"] = err
            return self._send(
                200 if ok else 500,
                json.dumps(body),
                "application/json; charset=utf-8",
            )

        if p == "/api/latency/tone":
            action = get_str("action", "status").strip().lower() or "status"
            if action in ("status", "get"):
                payload = _latency_tone_status_payload()
                return self._send(
                    200,
                    json.dumps({"ok": True, "tone": payload}),
                    "application/json; charset=utf-8",
                )
            if action in ("stop", "cancel"):
                with _LATENCY_TONE_LOCK:
                    payload = _latency_tone_stop_locked("api_stop")
                return self._send(
                    200,
                    json.dumps({"ok": True, "tone": payload}),
                    "application/json; charset=utf-8",
                )
            if action in ("start", "inject"):
                requested_target = get_str("target", LATENCY_TONE_DEFAULT_TARGET).strip().lower()
                target = "digital" if requested_target == "digital" else "analog"
                default_mount = f"latency-{target}.mp3"
                requested_mount = get_str("mount", "").strip()
                mount = requested_mount or default_mount
                frequency_hz = _bounded_int(
                    form.get("frequency_hz", LATENCY_TONE_DEFAULT_FREQ_HZ),
                    LATENCY_TONE_DEFAULT_FREQ_HZ,
                    120,
                    5000,
                )
                duration_ms = _bounded_int(
                    form.get("duration_ms", LATENCY_TONE_DEFAULT_DURATION_MS),
                    LATENCY_TONE_DEFAULT_DURATION_MS,
                    500,
                    30000,
                )
                pre_roll_ms = _bounded_int(
                    form.get("pre_roll_ms", LATENCY_TONE_DEFAULT_PREROLL_MS),
                    LATENCY_TONE_DEFAULT_PREROLL_MS,
                    0,
                    min(5000, max(0, duration_ms - 100)),
                )
                bitrate_kbps = _bounded_int(
                    form.get("bitrate_kbps", LATENCY_TONE_DEFAULT_BITRATE_KBPS),
                    LATENCY_TONE_DEFAULT_BITRATE_KBPS,
                    8,
                    128,
                )
                sample_rate_hz = _bounded_int(
                    form.get("sample_rate_hz", LATENCY_TONE_DEFAULT_SAMPLE_RATE),
                    LATENCY_TONE_DEFAULT_SAMPLE_RATE,
                    8000,
                    48000,
                )
                ok, err, tone_status = _start_latency_tone_injection(
                    target=target,
                    mount=mount,
                    frequency_hz=frequency_hz,
                    duration_ms=duration_ms,
                    pre_roll_ms=pre_roll_ms,
                    bitrate_kbps=bitrate_kbps,
                    sample_rate_hz=sample_rate_hz,
                )
                status_code = 200 if ok else 500
                body = {
                    "ok": bool(ok),
                    "tone": tone_status,
                }
                if not ok:
                    body["error"] = str(err or "unable to start latency tone injection")
                return self._send(
                    status_code,
                    json.dumps(body),
                    "application/json; charset=utf-8",
                )
            return self._send(
                400,
                json.dumps({"ok": False, "error": "invalid action"}),
                "application/json; charset=utf-8",
            )

        if p == "/api/mode":
            controller = get_scan_mode_controller()
            mode = get_str("mode").strip().lower()
            if not mode:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": "missing mode"}),
                    "application/json; charset=utf-8",
                )
            try:
                controller.set_mode(mode)
            except ValueError as e:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )

            sync_payload: dict[str, Any] = {"ok": True, "changed": False}
            try:
                sync_payload = sync_scan_pool_to_runtime(force=True)
            except Exception as exc:
                sync_payload = {"ok": False, "changed": False, "errors": [str(exc)]}

            return self._send(
                200,
                json.dumps(
                    {
                        "ok": True,
                        "mode": controller.get_mode(),
                        "favorites_runtime_sync": sync_payload,
                    }
                ),
                "application/json; charset=utf-8",
            )

        if p == "/api/hp/state/activate":
            # Per-band favorite activation (Phase 7g).
            # POST body: fav_id=<id>, band=air|ground|digital|all
            #
            # Sets enabled_air / enabled_ground / enabled_digital to True
            # for the named favorite and False for ALL others (mutex
            # enforcement, one fav per band).  band='all' touches all
            # three flags; 'both' is kept as a backward-compat alias for
            # 'all' (the AIR card's Sync-to-Ground button still posts
            # 'both' and gets the same 3-flag behavior).  Legacy
            # `enabled = enabled_air OR enabled_ground OR enabled_digital`
            # is kept consistent so tools that don't know about per-band
            # still behave.  Persists state + enqueues runtime sync, but
            # does NOT restart rtl-airband / op25 (those happen via
            # Sitrep -> Reset Radios so Will controls when the radio
            # bounces).
            try:
                fav_id = str((form.get("fav_id") or form.get("id") or "")).strip()
                band = str((form.get("band") or "")).strip().lower()
                if not fav_id:
                    raise ValueError("missing fav_id")
                if band not in ("air", "ground", "digital", "both", "all"):
                    raise ValueError(
                        "band must be 'air', 'ground', 'digital', or 'all'"
                    )
            except ValueError as e:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            try:
                state = HPState.load()
            except Exception as e:
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            favorites = list(state.favorites or [])
            target_found = False
            for f in favorites:
                if not isinstance(f, dict):
                    continue
                is_target = (str(f.get("id") or "").strip() == fav_id)
                if is_target:
                    target_found = True
                if band in ("air", "both", "all"):
                    f["enabled_air"] = bool(is_target)
                if band in ("ground", "both", "all"):
                    f["enabled_ground"] = bool(is_target)
                if band in ("digital", "both", "all"):
                    f["enabled_digital"] = bool(is_target)
                # Keep legacy `enabled` consistent.
                f["enabled"] = bool(
                    f.get("enabled_air")
                    or f.get("enabled_ground")
                    or f.get("enabled_digital")
                )
            if not target_found:
                return self._send(
                    404,
                    json.dumps({"ok": False, "error": f"favorite not found: {fav_id}"}),
                    "application/json; charset=utf-8",
                )
            state.favorites = favorites
            # Keep favorites_name pointing at the AIR favorite when AIR
            # was just switched (or to the new target on band=both) — that
            # keeps the legacy display-name field aligned with the user's
            # most-recent explicit pick.
            try:
                if band in ("air", "both", "all"):
                    for f in favorites:
                        if isinstance(f, dict) and str(f.get("id") or "").strip() == fav_id:
                            label = str(f.get("label") or "").strip()
                            if label:
                                state.favorites_name = label
                            break
            except Exception:
                logger.debug("activate: favorites_name update failed", exc_info=True)
            try:
                payload = _save_hp_state_with_sync(state)
            except Exception as e:
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            payload["activated"] = {"fav_id": fav_id, "band": band}
            # ----- Phase 4c feature-flag branch.
            # When the chirp path is on, push the favorite's channel
            # list into the relevant chirp daemon(s) so they reflect
            # the new activation immediately.  This REPLACES the
            # implicit "next reset_radios will pick it up" delay
            # baked into the rtl-airband path.  Non-fatal on failure:
            # the HPState persistence above is the source of truth,
            # and the operator can re-trigger via /api/sitrep/action
            # reset_radios.
            use_chirp = False
            try:
                use_chirp = bool(_chirp_use_gr_demod())
            except Exception:
                logger.debug("hp/state/activate: use_gr_demod probe failed", exc_info=True)
            if use_chirp:
                bands_to_push: list[str] = []
                if band in ("air", "both", "all"):
                    bands_to_push.append("airband")
                if band in ("ground", "both", "all"):
                    bands_to_push.append("ground")
                chirp_results: dict[str, dict] = {}
                for b in bands_to_push:
                    try:
                        chirp_results[b] = _chirp_adapter.activate_favorite_via_chirp(
                            b, fav_id
                        )
                    except Exception as exc:
                        logger.exception(
                            "hp/state/activate: chirp push failed band=%s", b
                        )
                        chirp_results[b] = {"ok": False, "error": str(exc)}
                payload["chirp"] = chirp_results
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/hp/state":
            try:
                state = HPState.load()
            except Exception as e:
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )

            try:
                _apply_hp_state_form(state, form)
            except ValueError as e:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )

            try:
                payload = _save_hp_state_with_sync(state)
            except Exception as e:
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/hp/location/push":
            # TAILNET-ONLY-TRUSTED. This endpoint has NO authentication. It is
            # safe only because the UI listens on a Tailscale-only interface
            # (no Funnel, no public reverse proxy, no port forward). The
            # iPhone Shortcut reaches it over the tailnet via Tailscale's iOS
            # app — that's the access control.
            #
            # >>> DO NOT expose port 5050 publicly without re-adding auth. <<<
            #
            # If you ever enable `tailscale funnel`, an nginx/Caddy reverse
            # proxy, an ngrok tunnel, or a router port-forward to :5050,
            # ANYONE on the internet can move the scanner's ZIP. Re-add a
            # shared-secret check (see git history at 61864b5 for the
            # hmac.compare_digest pattern) before doing any of that.
            #
            # Pushes are also gated on HPState.travel_mode_enabled — the SB3
            # UI Travel Mode toggle is the user-facing on/off switch. iPhone
            # pushes when off are rejected with 409 and logged with
            # accepted=false so Will can see them in the receipt log.
            try:
                state = HPState.load()
            except Exception as e:
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )

            if not state.travel_mode_enabled:
                rejected_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                attempted_zip = str(form.get("zip") or "").strip()
                _log_travel_push(
                    {
                        "ts": rejected_at,
                        "accepted": False,
                        "reason": "travel_mode_disabled",
                        "zip": attempted_zip,
                        "lat": form.get("lat"),
                        "lon": form.get("lon"),
                        "source": str(form.get("source") or "").strip(),
                    }
                )
                return self._send(
                    409,
                    json.dumps({"ok": False, "reason": "travel_mode_disabled"}),
                    "application/json; charset=utf-8",
                )

            try:
                _apply_travel_push(state, form)
            except ValueError as e:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )

            try:
                save_payload = _save_hp_state_with_sync(state)
            except Exception as e:
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )

            updated_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            _log_travel_push(
                {
                    "ts": updated_at,
                    "accepted": True,
                    "zip": state.zip,
                    "lat": state.lat,
                    "lon": state.lon,
                    "source": str(form.get("source") or "").strip(),
                }
            )
            return self._send(
                200,
                json.dumps(
                    {
                        "ok": True,
                        "zip": state.zip,
                        "lat": state.lat,
                        "lon": state.lon,
                        "updated_at": updated_at,
                        "favorites_runtime_sync": save_payload.get("favorites_runtime_sync"),
                    }
                ),
                "application/json; charset=utf-8",
            )

        if p == "/api/hp/owntracks":
            # PR #35 — Owntracks iOS app adapter. Same TAILNET-ONLY-TRUSTED
            # security posture as /api/hp/location/push: no auth, safe only
            # because the UI binds to a Tailscale-only interface. >>> DO NOT
            # expose port 5050 publicly without re-adding auth. <<<
            #
            # Owntracks publishes multiple message types over HTTP/MQTT:
            #   _type=location   — periodic GPS push (the one we route)
            #   _type=lwt        — last-will/ping (no action)
            #   _type=transition — geofence entry/exit (no action)
            #   _type=waypoint   — user-defined POI (no action)
            #   _type=…          — anything else (no action)
            # We ack non-location types 200 so the iOS app doesn't retry.
            _OWNTRACKS_STATS["invocations_total"] += 1

            mtype = str(form.get("_type") or "").strip().lower()
            if mtype != "location":
                logger.info("Owntracks: ignored _type=%s tid=%s",
                            mtype or "(none)", form.get("tid"))
                return self._send(
                    200,
                    json.dumps({"ok": True, "ignored": mtype or "no _type"}),
                    "application/json; charset=utf-8",
                )

            lat_raw = form.get("lat")
            lon_raw = form.get("lon")
            if lat_raw is None or lon_raw is None:
                _OWNTRACKS_STATS["pushes_rejected_total"] += 1
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": "missing lat/lon"}),
                    "application/json; charset=utf-8",
                )
            try:
                lat_f = float(lat_raw)
                lon_f = float(lon_raw)
            except (TypeError, ValueError):
                _OWNTRACKS_STATS["pushes_rejected_total"] += 1
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": "invalid lat/lon"}),
                    "application/json; charset=utf-8",
                )
            if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
                _OWNTRACKS_STATS["pushes_rejected_total"] += 1
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": "lat/lon out of range"}),
                    "application/json; charset=utf-8",
                )

            try:
                zip_resolved = _nearest_zip(lat_f, lon_f)
            except Exception:
                zip_resolved = ""
            if not zip_resolved:
                _OWNTRACKS_STATS["pushes_rejected_total"] += 1
                return self._send(
                    400,
                    json.dumps(
                        {"ok": False,
                         "error": "could not resolve nearest US ZIP "
                                  "(outside US coverage or index missing)"}
                    ),
                    "application/json; charset=utf-8",
                )

            try:
                state = HPState.load()
            except Exception as e:
                _OWNTRACKS_STATS["pushes_rejected_total"] += 1
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )

            # Gate on the same Travel Mode flag as /api/hp/location/push.
            if not state.travel_mode_enabled:
                _OWNTRACKS_STATS["pushes_rejected_total"] += 1
                rejected_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                _log_travel_push(
                    {
                        "ts": rejected_at,
                        "accepted": False,
                        "reason": "travel_mode_disabled",
                        "zip": zip_resolved,
                        "lat": lat_f,
                        "lon": lon_f,
                        "source": "owntracks",
                    }
                )
                return self._send(
                    409,
                    json.dumps({"ok": False, "reason": "travel_mode_disabled"}),
                    "application/json; charset=utf-8",
                )

            # Build the internal payload and route through the shared push
            # helpers. Reuses _apply_travel_push / _save_hp_state_with_sync
            # exactly — no special-case for Owntracks beyond the reverse
            # geocode and source tag.
            internal = {
                "zip": zip_resolved,
                "lat": lat_f,
                "lon": lon_f,
                "source": "owntracks",
            }
            try:
                _apply_travel_push(state, internal)
            except ValueError as e:
                _OWNTRACKS_STATS["pushes_rejected_total"] += 1
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )

            try:
                save_payload = _save_hp_state_with_sync(state)
            except Exception as e:
                _OWNTRACKS_STATS["pushes_rejected_total"] += 1
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )

            now_ts = time.time()
            updated_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            _OWNTRACKS_STATS["pushes_accepted_total"] += 1
            _OWNTRACKS_STATS["last_push_ts"] = now_ts
            _OWNTRACKS_STATS["last_lat"] = lat_f
            _OWNTRACKS_STATS["last_lon"] = lon_f
            batt_raw = form.get("batt")
            try:
                _OWNTRACKS_STATS["last_battery_pct"] = (
                    int(batt_raw) if batt_raw is not None else None
                )
            except (TypeError, ValueError):
                _OWNTRACKS_STATS["last_battery_pct"] = None

            _log_travel_push(
                {
                    "ts": updated_at,
                    "accepted": True,
                    "zip": state.zip,
                    "lat": state.lat,
                    "lon": state.lon,
                    "source": "owntracks",
                    "owntracks_tid": str(form.get("tid") or "").strip(),
                    "owntracks_acc_m": form.get("acc"),
                    "owntracks_vel_kmh": form.get("vel"),
                    "owntracks_battery_pct": _OWNTRACKS_STATS["last_battery_pct"],
                }
            )
            return self._send(
                200,
                json.dumps(
                    {
                        "ok": True,
                        "zip": state.zip,
                        "lat": state.lat,
                        "lon": state.lon,
                        "updated_at": updated_at,
                        "favorites_runtime_sync": save_payload.get("favorites_runtime_sync"),
                    }
                ),
                "application/json; charset=utf-8",
            )

        if p == "/api/hp/travel_mode/toggle":
            # User-facing on/off switch for travel mode. Mutates only
            # HPState.travel_mode_enabled — never touches zip/lat/lon or any
            # other field. The flag is a pure gate over /api/hp/location/push;
            # manual sidecar ZIP entry remains the way to set baseline.
            if "enabled" not in form:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": "missing enabled"}),
                    "application/json; charset=utf-8",
                )
            raw_enabled = form.get("enabled")
            if not isinstance(raw_enabled, bool):
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": "enabled must be a boolean"}),
                    "application/json; charset=utf-8",
                )

            try:
                state = HPState.load()
            except Exception as e:
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )

            previous = bool(state.travel_mode_enabled)
            state.travel_mode_enabled = bool(raw_enabled)

            try:
                save_payload = _save_hp_state_with_sync(state)
            except Exception as e:
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )

            logger.info(
                "Travel mode toggle: %s -> %s",
                previous,
                state.travel_mode_enabled,
            )
            return self._send(
                200,
                json.dumps(
                    {
                        "ok": True,
                        "travel_mode_enabled": state.travel_mode_enabled,
                        "zip": state.zip,
                        "lat": state.lat,
                        "lon": state.lon,
                        "favorites_runtime_sync": save_payload.get("favorites_runtime_sync"),
                    }
                ),
                "application/json; charset=utf-8",
            )

        if p == "/api/hp/avoids":
            controller = get_scan_mode_controller()
            action = str(form.get("action") or "").strip().lower()
            if action == "clear":
                controller.clear_hp_avoids()
                return self._send(
                    200,
                    json.dumps({"ok": True, "avoids": controller.get_hp_avoids()}),
                    "application/json; charset=utf-8",
                )
            if action == "add":
                system_token = str(form.get("system") or "").strip()
                if not system_token:
                    return self._send(
                        400,
                        json.dumps({"ok": False, "error": "missing system"}),
                        "application/json; charset=utf-8",
                    )
                added = controller.add_hp_avoid_system(system_token)
                if not added:
                    return self._send(
                        400,
                        json.dumps({"ok": False, "error": "invalid system"}),
                        "application/json; charset=utf-8",
                    )
                return self._send(
                    200,
                    json.dumps({"ok": True, "avoids": controller.get_hp_avoids()}),
                    "application/json; charset=utf-8",
                )
            if action == "remove":
                system_token = str(form.get("system") or "").strip()
                if not system_token:
                    return self._send(
                        400,
                        json.dumps({"ok": False, "error": "missing system"}),
                        "application/json; charset=utf-8",
                    )
                removed = controller.remove_hp_avoid_system(system_token)
                if not removed:
                    return self._send(
                        404,
                        json.dumps({"ok": False, "error": "system not in avoid list"}),
                        "application/json; charset=utf-8",
                    )
                return self._send(
                    200,
                    json.dumps({"ok": True, "avoids": controller.get_hp_avoids()}),
                    "application/json; charset=utf-8",
                )
            return self._send(
                400,
                json.dumps({"ok": False, "error": "invalid action"}),
                "application/json; charset=utf-8",
            )

        if p == "/api/hp/favorites-sync":
            return self._send(
                200,
                json.dumps(
                    {
                        "ok": False,
                        "available": False,
                        "in_sync": True,
                        "reason": "favorites sync retired; favorites route directly to runtime",
                    }
                ),
                "application/json; charset=utf-8",
            )

        if p in ("/api/hp/hold", "/api/hp/next", "/api/hp/avoid"):
            action = p.rsplit("/", 1)[-1]
            controller = get_scan_mode_controller()
            manager = get_digital_manager()
            if not hasattr(manager, "getScheduler") or not hasattr(manager, "setScheduler"):
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": "digital allocation not supported"}),
                    "application/json; charset=utf-8",
                )

            scheduler = dict(manager.getScheduler() or {})
            systems = [
                str(item).strip()
                for item in (scheduler.get("digital_allocation_systems") or [])
                if str(item).strip()
            ]
            active = str(scheduler.get("digital_active_system") or "").strip()
            mode = str(scheduler.get("digital_scan_mode") or "").strip().lower()
            order = [
                str(item).strip()
                for item in (
                    scheduler.get("digital_system_order")
                    or scheduler.get("digital_allocation_systems")
                    or []
                )
                if str(item).strip()
            ]

            if action == "hold":
                if mode == "single_system" and len(order) == 1:
                    ok, err, snapshot = manager.setScheduler(
                        {
                            "mode": "timeslice_multi_system",
                            "system_order": [],
                        }
                    )
                    if not ok:
                        return self._send(
                            500,
                            json.dumps({"ok": False, "error": err or "hold release failed"}),
                            "application/json; charset=utf-8",
                        )
                    payload = {
                        "ok": True,
                        "action": "hold",
                        "runtime_changed": True,
                        "released": True,
                        "digital_allocation": snapshot,
                    }
                    return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

                target = active if active in systems else (systems[0] if systems else "")
                if not target:
                    return self._send(
                        409,
                        json.dumps({"ok": False, "error": "no schedulable systems"}),
                        "application/json; charset=utf-8",
                    )
                ok, err, snapshot = manager.setScheduler(
                    {
                        "mode": "single_system",
                        "system_order": [target],
                    }
                )
                if not ok:
                    return self._send(
                        500,
                        json.dumps({"ok": False, "error": err or "hold failed"}),
                        "application/json; charset=utf-8",
                    )
                payload = {
                    "ok": True,
                    "action": "hold",
                    "runtime_changed": True,
                    "active_system": target,
                    "digital_allocation": snapshot,
                }
                return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

            if action == "next":
                if not systems:
                    return self._send(
                        409,
                        json.dumps({"ok": False, "error": "no schedulable systems"}),
                        "application/json; charset=utf-8",
                    )
                if active in systems:
                    idx = systems.index(active)
                    target = systems[(idx + 1) % len(systems)]
                else:
                    target = systems[0]
                ok, err, snapshot = manager.setScheduler(
                    {
                        "mode": "single_system",
                        "system_order": [target],
                    }
                )
                if not ok:
                    return self._send(
                        500,
                        json.dumps({"ok": False, "error": err or "next failed"}),
                        "application/json; charset=utf-8",
                    )
                payload = {
                    "ok": True,
                    "action": "next",
                    "runtime_changed": True,
                    "active_system": target,
                    "digital_allocation": snapshot,
                }
                return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

            if not active:
                return self._send(
                    409,
                    json.dumps({"ok": False, "error": "no active system to avoid"}),
                    "application/json; charset=utf-8",
                )
            if not controller.add_hp_avoid_system(active):
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": "invalid active system"}),
                    "application/json; charset=utf-8",
                )
            ok, err, snapshot = manager.setScheduler(
                {
                    "mode": "timeslice_multi_system",
                    "system_order": [],
                }
            )
            if not ok:
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": err or "avoid failed"}),
                    "application/json; charset=utf-8",
                )
            payload = {
                "ok": True,
                "action": "avoid",
                "runtime_changed": True,
                "avoided_system": active,
                "avoids": controller.get_hp_avoids(),
                "digital_allocation": snapshot,
            }
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/wx/filter":
            store = get_met_store()
            enabled = get_str("enabled", "")
            if enabled.lower() in ("false", "0", "off"):
                store.clear_spatial_filter()
                return self._send(200, json.dumps({"ok": True, "spatial_filter": False}), "application/json; charset=utf-8")
            # Apply or update filter — HPState is already imported at module level;
            # a local import here would shadow it for the entire do_POST method
            # and cause UnboundLocalError in earlier code paths.
            try:
                state = HPState.load()
                lat = float(get_str("lat", "") or state.lat)
                lon = float(get_str("lon", "") or state.lon)
            except Exception:
                lat = float(get_str("lat", "0"))
                lon = float(get_str("lon", "0"))
            radius_nm = float(get_str("radius_nm", "") or store.get_status().get("filter_radius_nm", 10.0))
            ceiling_ft = float(get_str("ceiling_ft", "") or store.get_status().get("filter_ceiling_ft", 40000.0))
            store.set_spatial_filter(lat=lat, lon=lon, radius_nm=radius_nm, ceiling_ft=ceiling_ft, user_set=True)
            return self._send(200, json.dumps({
                "ok": True, "spatial_filter": True,
                "filter_lat": lat, "filter_lon": lon,
                "filter_radius_nm": radius_nm, "filter_ceiling_ft": ceiling_ft,
            }), "application/json; charset=utf-8")
        if p == "/api/wx/clear":
            store = get_met_store()
            source = get_str("source") or None
            store.clear(source=source)
            return self._send(200, json.dumps({"ok": True}), "application/json; charset=utf-8")

        if p == "/api/wx/sounding/delete":
            keys = form.get("keys") if isinstance(form.get("keys"), list) else []
            store = get_met_store()
            deleted = store.delete_observations(keys)
            return self._send(200, json.dumps({"ok": True, "deleted": deleted}), "application/json; charset=utf-8")

        if p == "/api/wx/decoder":
            # Start or stop ACARS / radiosonde decoders.
            # These use the ground SDR dongle.  Starting a WX decoder
            # must stop *both* other WX decoders first (not just the
            # "other" one) to guarantee the dongle is free.  Stopping
            # checks whether the digital decoder (OP25) crashed during
            # the WX session and restarts it if needed.
            try:
                from .actions import _WX_START, _WX_STOP, _start_wx_reader, _stop_wx_reader
            except ImportError:
                from ui.actions import _WX_START, _WX_STOP, _start_wx_reader, _stop_wx_reader
            try:
                from .systemd import unit_active, restart_digital, restart_digital_audio, stop_ground, start_ground, UNITS
            except ImportError:
                from ui.systemd import unit_active, restart_digital, restart_digital_audio, stop_ground, start_ground, UNITS

            def _heal_digital():
                """Restart OP25 if it's in a failed/inactive state."""
                try:
                    digital_unit = UNITS.get("digital", "")
                    if digital_unit and not unit_active(digital_unit):
                        logger.info("WX decoder stop: digital decoder not active, restarting")
                        restart_digital(reason="wx_decoder_stop_heal")
                except Exception:
                    logger.exception("WX decoder stop: failed to heal digital decoder")

            # Sentinel file: presence tells ensure-op25-runtime.py that VDL2 owns
            # its dongle so it must not be added as sdr_traffic2 in OP25.
            import os as _os
            # Default sentinel path is in /run/user/1000/ (ubuntu's runtime dir):
            #   - writable by ubuntu (the UI process and service user)
            #   - cleared on reboot (correct: VDL2 won't be running after reboot)
            #   - survives OP25 service restarts (unlike OP25_RUNTIME_DIR which
            #     RuntimeDirectory= wipes before ExecStartPre runs)
            _VDL2_SENTINEL = _os.environ.get(
                "OP25_VDL2_SENTINEL",
                "/run/user/1000/vdl2_dongle_reserved",
            )
            _vdl2_share = _os.environ.get("OP25_VDL2_TRAFFIC_SHARE", "1").strip() != "0"

            def _vdl2_reclaim_dongle():
                """VDL2 stopped — remove sentinel and restart OP25 to add sdr_traffic2."""
                if not _vdl2_share:
                    return
                try:
                    _os.unlink(_VDL2_SENTINEL)
                    logger.info(
                        "VDL2 dongle sharing: removed sentinel %s, restarting OP25 to add sdr_traffic2",
                        _VDL2_SENTINEL,
                    )
                except FileNotFoundError:
                    pass  # already gone
                except Exception:
                    logger.exception("VDL2 dongle sharing: failed to remove sentinel %s", _VDL2_SENTINEL)
                try:
                    ok, err = restart_digital(reason="vdl2_reclaim_dongle")
                    if ok:
                        logger.info("VDL2 dongle sharing: OP25 restarted successfully")
                    else:
                        logger.warning("VDL2 dongle sharing: OP25 restart failed: %s", err)
                except Exception:
                    logger.exception("VDL2 dongle sharing: exception restarting OP25")
                # Audio bridge binds UDP ports at startup — restart to pick up new port.
                try:
                    ok, err = restart_digital_audio()
                    if ok:
                        logger.info("VDL2 dongle sharing: audio bridge restarted successfully")
                    else:
                        logger.warning("VDL2 dongle sharing: audio bridge restart failed: %s", err)
                except Exception:
                    logger.exception("VDL2 dongle sharing: exception restarting audio bridge")

            def _vdl2_reserve_dongle():
                """VDL2 starting — touch sentinel and restart OP25 to drop sdr_traffic2."""
                if not _vdl2_share:
                    return
                try:
                    _os.makedirs(_os.path.dirname(_VDL2_SENTINEL), exist_ok=True)
                    with open(_VDL2_SENTINEL, "w") as _f:
                        _f.write("")
                    logger.info(
                        "VDL2 dongle sharing: touched sentinel %s, restarting OP25 to drop sdr_traffic2",
                        _VDL2_SENTINEL,
                    )
                except Exception:
                    logger.exception("VDL2 dongle sharing: failed to touch sentinel %s", _VDL2_SENTINEL)
                    return  # don't restart OP25 if we couldn't set the sentinel
                try:
                    ok, err = restart_digital(reason="vdl2_reserve_dongle")
                    if ok:
                        logger.info("VDL2 dongle sharing: OP25 restarted successfully (sdr_traffic2 removed)")
                    else:
                        logger.warning("VDL2 dongle sharing: OP25 restart failed: %s", err)
                except Exception:
                    logger.exception("VDL2 dongle sharing: exception restarting OP25")
                try:
                    ok, err = restart_digital_audio()
                    if ok:
                        logger.info("VDL2 dongle sharing: audio bridge restarted successfully")
                    else:
                        logger.warning("VDL2 dongle sharing: audio bridge restart failed: %s", err)
                except Exception:
                    logger.exception("VDL2 dongle sharing: exception restarting audio bridge")

            action = get_str("action", "").lower()
            decoder = get_str("decoder", "").lower()
            if action == "stop":
                # Stop whatever is running
                for name, stop_fn in _WX_STOP.items():
                    try:
                        stop_fn()
                    except Exception:
                        pass
                _stop_wx_reader()
                # Ground shares the WX dongle — restart it now that the dongle is free
                start_ground()
                # Heal digital decoder if it crashed while WX was active
                _heal_digital()
                # VDL2 stopped — reclaim its dongle as sdr_traffic2 in OP25
                _vdl2_reclaim_dongle()
                return self._send(200, json.dumps({"ok": True, "active_decoder": None}), "application/json; charset=utf-8")
            if action != "start" or decoder not in ("acars", "radiosonde"):
                return self._send(400, json.dumps({"ok": False, "error": "action must be start|stop, decoder must be acars|radiosonde"}), "application/json; charset=utf-8")
            # Stop ALL WX decoders first to guarantee the ground dongle is free
            for name, stop_fn in _WX_STOP.items():
                try:
                    stop_fn()
                except Exception:
                    pass
            _stop_wx_reader()
            # Ground shares the WX dongle — stop it to release the dongle
            stop_ground()
            time.sleep(0.5)  # let USB device release
            # Start the requested decoder(s)
            if decoder == "acars":
                # ACARS mode starts both acarsdec and dumpvdl2 for combined data.
                # Reserve VDL2 dongle in OP25 before starting dumpvdl2.
                _vdl2_reserve_dongle()
                ok1, err1 = _WX_START["acars"]()
                ok2, err2 = _WX_START.get("vdl2", lambda: (True, ""))()
                if not ok1 and not ok2:
                    return self._send(500, json.dumps({"ok": False, "error": f"acars: {err1}; vdl2: {err2}"}), "application/json; charset=utf-8")
            else:
                ok, err = _WX_START[decoder]()
                if not ok:
                    return self._send(500, json.dumps({"ok": False, "error": err}), "application/json; charset=utf-8")
            _start_wx_reader(decoder)
            return self._send(200, json.dumps({"ok": True, "active_decoder": decoder}), "application/json; charset=utf-8")

        if p == "/api/digital/start":
            gate = gate_action("digital_start")
            if not gate.get("ok"):
                return self._send(
                    409,
                    json.dumps({"ok": False, "error": "preflight blocked", "preflight": gate}),
                    "application/json; charset=utf-8",
                )
            ok, err = get_digital_manager().start()
            payload = {"ok": bool(ok)}
            if not ok:
                payload["error"] = err or "start failed"
                return self._send(500, json.dumps(payload), "application/json; charset=utf-8")
            _invalidate_runtime_caches("status", "hits")
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/digital/stop":
            ok, err = get_digital_manager().stop()
            payload = {"ok": bool(ok)}
            if not ok:
                payload["error"] = err or "stop failed"
                return self._send(500, json.dumps(payload), "application/json; charset=utf-8")
            _invalidate_runtime_caches("status", "hits")
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/digital/restart":
            gate = gate_action("digital_restart")
            if not gate.get("ok"):
                return self._send(
                    409,
                    json.dumps({"ok": False, "error": "preflight blocked", "preflight": gate}),
                    "application/json; charset=utf-8",
                )
            ok, err = get_digital_manager().restart()
            payload = {"ok": bool(ok)}
            if not ok:
                payload["error"] = err or "restart failed"
                return self._send(500, json.dumps(payload), "application/json; charset=utf-8")
            _invalidate_runtime_caches("status", "hits")
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/digital/profile":
            profile_id = get_str("profileId").strip()
            if not profile_id:
                return self._send(400, json.dumps({"ok": False, "error": "missing profileId"}), "application/json; charset=utf-8")
            if not validate_digital_profile_id(profile_id):
                return self._send(400, json.dumps({"ok": False, "error": "invalid profileId"}), "application/json; charset=utf-8")
            gate = gate_action("digital_profile", profile_id=profile_id)
            if not gate.get("ok"):
                return self._send(
                    409,
                    json.dumps({"ok": False, "error": "preflight blocked", "preflight": gate}),
                    "application/json; charset=utf-8",
                )
            ok, err = get_digital_manager().setProfile(profile_id)
            payload = {"ok": bool(ok)}
            if not ok:
                payload["error"] = err or "set profile failed"
                status = 400 if err in ("invalid profileId", "unknown profileId") else 500
                return self._send(status, json.dumps(payload), "application/json; charset=utf-8")
            _invalidate_runtime_caches("status", "hits")
            try:
                payload["v3_compile"] = set_active_digital_profile(profile_id)
            except Exception as e:
                payload["v3_compile_error"] = str(e)
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/digital/scheduler":
            manager = get_digital_manager()
            if not hasattr(manager, "setScheduler"):
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": "scheduler not supported"}),
                    "application/json; charset=utf-8",
                )
            scheduler_payload = _extract_scheduler_payload(form)
            ok, err, payload = manager.setScheduler(scheduler_payload)
            if not ok:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": err or "invalid scheduler payload"}),
                    "application/json; charset=utf-8",
                )
            response = {"ok": True}
            response.update(payload or {})
            return self._send(200, json.dumps(response), "application/json; charset=utf-8")

        if p == "/api/digital/mute":
            raw_muted = form.get("muted")
            if raw_muted is None:
                return self._send(400, json.dumps({"ok": False, "error": "missing muted"}), "application/json; charset=utf-8")
            muted = None
            if isinstance(raw_muted, bool):
                muted = raw_muted
            elif isinstance(raw_muted, (int, float)):
                muted = bool(raw_muted)
            else:
                sval = str(raw_muted).strip().lower()
                if sval in ("1", "true", "yes", "on"):
                    muted = True
                elif sval in ("0", "false", "no", "off"):
                    muted = False
            if muted is None:
                return self._send(400, json.dumps({"ok": False, "error": "invalid muted"}), "application/json; charset=utf-8")
            get_digital_manager().setMuted(muted)
            return self._send(200, json.dumps({"ok": True}), "application/json; charset=utf-8")

        if p == "/api/digital/profile/create":
            profile_id = get_str("profileId").strip()
            ok, err = create_digital_profile_dir(profile_id)
            if not ok:
                status = 400 if err in ("invalid profileId", "profile already exists") else 500
                return self._send(status, json.dumps({"ok": False, "error": err}), "application/json; charset=utf-8")
            payload = {"ok": True}
            try:
                payload["v3_compile"] = sync_digital_profiles_from_fs()
            except Exception as e:
                payload["v3_compile_error"] = str(e)
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/digital/profile/delete":
            profile_id = get_str("profileId").strip()
            ok, err = delete_digital_profile_dir(profile_id)
            if not ok:
                status = 400 if err in ("invalid profileId", "profile is active", "profile not found", "profile path is a symlink") else 500
                return self._send(status, json.dumps({"ok": False, "error": err}), "application/json; charset=utf-8")
            payload = {"ok": True}
            try:
                payload["v3_compile"] = sync_digital_profiles_from_fs()
            except Exception as e:
                payload["v3_compile_error"] = str(e)
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/digital/profile/inspect":
            profile_id = get_str("profileId").strip()
            ok, payload = inspect_digital_profile(profile_id)
            if not ok:
                status = 400 if payload in ("invalid profileId", "profile not found") else 500
                return self._send(status, json.dumps({"ok": False, "error": payload}), "application/json; charset=utf-8")
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/digital/talkgroups/listen":
            profile_id = get_str("profileId").strip()
            if not profile_id:
                return self._send(400, json.dumps({"ok": False, "error": "missing profileId"}), "application/json; charset=utf-8")
            items = form.get("items")
            if isinstance(items, str):
                try:
                    items = json.loads(items)
                except json.JSONDecodeError:
                    items = []
            if not isinstance(items, list):
                items = []
            ok, err = write_digital_listen(profile_id, items)
            if not ok:
                return self._send(400, json.dumps({"ok": False, "error": err}), "application/json; charset=utf-8")
            return self._send(200, json.dumps({"ok": True}), "application/json; charset=utf-8")

        if p == "/api/v3/compile":
            try:
                state = compile_runtime()
                return self._send(200, json.dumps({"ok": True, "state": state}), "application/json; charset=utf-8")
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")

        if p == "/api/profile":
            logger.info("POST /api/profile hit: form=%s", {k: form.get(k) for k in ("profile", "target")})
            pid = form.get("profile", "")
            target = form.get("target", "airband")
            gate = gate_action("profile", target=target)
            if not gate.get("ok"):
                return self._send(
                    409,
                    json.dumps({"ok": False, "error": "preflight blocked", "preflight": gate}),
                    "application/json; charset=utf-8",
                )
            # Snapshot VLC state before profile change may kill Icecast mount
            vlc_analog_was = vlc_running(target="analog")
            vlc_digital_was = vlc_running(target="digital")
            logger.info("Profile change %s->%s: vlc_analog=%s vlc_digital=%s", target, pid, vlc_analog_was, vlc_digital_was)
            result = enqueue_action({"type": "profile", "profile": pid, "target": target})
            payload = dict(result.get("payload") or {})
            result_ok = int(result.get("status") or 500) < 300 and payload.get("ok") and pid
            logger.info("Profile change result: status=%s ok=%s pid=%r result_ok=%s", result.get("status"), payload.get("ok"), pid, result_ok)
            if result_ok:
                _invalidate_runtime_caches("status", "hits")
                try:
                    payload["v3_compile"] = set_active_analog_profile(target, str(pid))
                except Exception as e:
                    payload["v3_compile_error"] = str(e)
                # Restart VLC if it was playing before the profile change
                if target in ("airband", "ground") and vlc_analog_was:
                    try:
                        stop_vlc(target="analog")
                        start_vlc(target="analog")
                        logger.info("VLC analog restarted after %s profile change", target)
                    except Exception:
                        logger.exception("Failed to restart VLC analog after profile change")
                if target == "digital" and vlc_digital_was:
                    try:
                        stop_vlc(target="digital")
                        start_vlc(target="digital")
                        logger.info("VLC digital restarted after digital profile change")
                    except Exception:
                        logger.exception("Failed to restart VLC digital after profile change")
            return self._send(result["status"], json.dumps(payload), "application/json; charset=utf-8")

        # ============ /api/airband/{squelch,gain} — Phase 7f ============
        # Lightweight per-band knobs powering the new AIR + GROUND cards
        # on sb5. Each slider commit POSTs one of:
        #   POST /api/airband/squelch  {band:'air'|'ground', threshold_dbfs, auto}
        #   POST /api/airband/gain     {band:'air'|'ground', gain_db}
        # We persist the new value via write_controls + the managed
        # controls override store, but DO NOT auto-restart rtl-airband
        # (TimeoutStopSec=5; risky to bounce on every slider drag, and
        # the SDRplay daemon can wedge on SIGKILL). The change applies
        # on the next manual restart via Sitrep → Reset Radios; we
        # signal that with `pending_restart: true` in the response so
        # the UI can render a "pending" hint. The existing /api/apply
        # path remains the way to commit-and-restart in one shot.
        if p in ("/api/airband/squelch", "/api/airband/gain"):
            band_raw = str(form.get("band", "")).strip().lower()
            band_map = {"air": "airband", "airband": "airband", "ground": "ground", "gnd": "ground"}
            target = band_map.get(band_raw)
            if not target:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": "unknown band (expected 'air' or 'ground')"}),
                    "application/json; charset=utf-8",
                )
            try:
                conf_path = resolve_controls_path(target)
                cur_gain, cur_snr, cur_dbfs, cur_mode = parse_controls(conf_path)
            except Exception as e:
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": f"controls read failed: {e}"}),
                    "application/json; charset=utf-8",
                )

            new_gain = float(cur_gain)
            new_snr = float(cur_snr)
            new_dbfs = float(cur_dbfs)
            new_mode = str(cur_mode or "dbfs").lower()

            try:
                if p == "/api/airband/squelch":
                    # Auto mode flips to rtl-airband's SNR floor detector.
                    if "auto" in form:
                        auto_flag = _parse_bool_value(form.get("auto"), field="auto")
                        new_mode = "snr" if auto_flag else "dbfs"
                    if "threshold_dbfs" in form:
                        new_dbfs = float(form.get("threshold_dbfs"))
                        # Clamp to UI range so a typo can't push a wild value.
                        if new_dbfs < -80.0: new_dbfs = -80.0
                        if new_dbfs > 0.0:   new_dbfs = 0.0
                else:  # /api/airband/gain
                    if "gain_db" in form:
                        new_gain = float(form.get("gain_db"))
                        if new_gain < 0.0:  new_gain = 0.0
                        if new_gain > 60.0: new_gain = 60.0
            except (TypeError, ValueError):
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": "bad value"}),
                    "application/json; charset=utf-8",
                )

            try:
                changed = write_controls(conf_path, new_gain, new_mode, new_snr, new_dbfs)
                # Persist as a managed-controls override so the value
                # survives across favorites-runtime regenerations.
                try:
                    try:
                        from .managed_analog_controls import persist_managed_controls_override
                    except ImportError:
                        from ui.managed_analog_controls import persist_managed_controls_override
                    persist_managed_controls_override(
                        target,
                        conf_path,
                        gain=new_gain,
                        squelch_mode=new_mode,
                        squelch_snr=new_snr,
                        squelch_dbfs=new_dbfs,
                    )
                except Exception:
                    logger.debug("managed override persist skipped", exc_info=True)
            except Exception as e:
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": f"write_controls failed: {e}"}),
                    "application/json; charset=utf-8",
                )

            return self._send(
                200,
                json.dumps({
                    "ok": True,
                    "band": "air" if target == "airband" else "ground",
                    "target": target,
                    "gain_db": float(new_gain),
                    "threshold_dbfs": float(new_dbfs),
                    "auto": new_mode == "snr",
                    "changed": bool(changed),
                    # Hot reload of rtl-airband is intentionally NOT done
                    # here. The operator restarts manually via Sitrep.
                    "pending_restart": bool(changed),
                }),
                "application/json; charset=utf-8",
            )

        # ============ /api/airband/squelch_preset — Phase 1 SB5 ============
        # Replaces the legacy per-band squelch dBFS slider with a 3-state
        # preset (Sensitive / Balanced / Selective).  See ui/squelch_preset.py
        # for the rationale: the legacy scalar threshold landed many dB above
        # the per-channel noise floor, so channel_squelch_counter never
        # incremented.  This endpoint reads the live noise floor from
        # /run/rtl_airband_<svc>_stats.txt, computes per-channel
        # threshold = noise + margin, and writes a list form into the
        # resolved controls profile.  rtl-airband does NOT hot-reload; the
        # operator (or the 6s sb5 auto-apply countdown) restarts via
        # /api/sitrep/action reset_radios.  We respond with
        # pending_restart=true so the countdown fires the same way the
        # legacy slider commit did.
        if p == "/api/airband/squelch_preset":
            band_raw = str(form.get("band", "")).strip().lower()
            band_map = {"air": "airband", "airband": "airband", "ground": "ground", "gnd": "ground"}
            target = band_map.get(band_raw)
            if not target:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": "unknown band (expected 'air' or 'ground')"}),
                    "application/json; charset=utf-8",
                )
            preset_in = str(form.get("preset", "")).strip().lower()
            if preset_in and preset_in not in SQUELCH_VALID_PRESETS:
                return self._send(
                    400,
                    json.dumps({
                        "ok": False,
                        "error": f"unknown preset '{preset_in}' (expected one of {list(SQUELCH_VALID_PRESETS)})",
                    }),
                    "application/json; charset=utf-8",
                )
            preset = squelch_normalize_preset(preset_in)
            try:
                conf_path = resolve_controls_path(target)
            except Exception as e:
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": f"controls path failed: {e}"}),
                    "application/json; charset=utf-8",
                )
            # ----- Phase 4c feature-flag branch (single, top of handler).
            # When SB5_USE_GR_DEMOD=true, route the apply through the chirp
            # daemon's set_squelch path instead of writing rtl-airband
            # config + restarting.  The plan dict shape is preserved so
            # the rest of this handler (409 poison rejection, override
            # persistence, response JSON) does not need to know which
            # back end did the work.
            use_chirp = False
            try:
                use_chirp = bool(_chirp_use_gr_demod())
            except Exception:
                logger.debug("squelch_preset: use_gr_demod probe failed", exc_info=True)
            try:
                if use_chirp:
                    plan = _chirp_adapter.apply_squelch_preset_via_chirp(target, preset)
                else:
                    plan = squelch_apply_preset(target, preset, conf_path)
            except Exception as e:
                logger.exception("squelch_preset apply failed (use_chirp=%s)", use_chirp)
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": f"apply_preset failed: {e}"}),
                    "application/json; charset=utf-8",
                )
            if plan.get("error"):
                # Poison-noise-floor rejection is an EXPECTED transient
                # condition (rtl-airband just restarted; noise estimator
                # still on init constant).  Surface it as 409 with a
                # retry hint so the UI can show a "noise floor warming"
                # toast and the operator knows to wait + retry — distinct
                # from a 500 "something is broken".  Matches the gate
                # squelch_tracker has had since f4e9eb7.
                if plan.get("error") == "noise_floor_not_warm":
                    return self._send(
                        409,
                        json.dumps({
                            "ok": False,
                            "status": "rejected",
                            "error": "noise_floor_not_warm",
                            "reason": plan.get("reason") or "noise floor not warm yet",
                            "retry_after_sec": int(plan.get("retry_after_sec") or 30),
                            "noise_floor_dbfs": plan.get("noise_floor_median"),
                            "poison_ceiling_dbfs": plan.get("poison_ceiling_dbfs"),
                            "band": "air" if plan.get("target") == "airband" else "ground",
                            "target": plan.get("target"),
                            "preset": plan.get("preset"),
                            "margin_db": plan.get("margin_db"),
                            "freqs_count": len(plan.get("freqs") or []),
                        }),
                        "application/json; charset=utf-8",
                    )
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": plan.get("error"), "plan": {
                        "target": plan.get("target"),
                        "preset": plan.get("preset"),
                        "margin_db": plan.get("margin_db"),
                        "stats_available": plan.get("stats_available"),
                        "freqs_count": len(plan.get("freqs") or []),
                    }}),
                    "application/json; charset=utf-8",
                )
            # Persist the preset + computed metadata into the managed
            # override store.  We also update squelch_dbfs to the median
            # computed threshold so the SSE airband_squelch_dbfs field
            # carries a useful value (used by the readout fallback).
            try:
                try:
                    from .managed_analog_controls import persist_managed_controls_override
                except ImportError:
                    from ui.managed_analog_controls import persist_managed_controls_override
                # Read current gain/snr to preserve them.
                try:
                    cur_gain, cur_snr, _cur_dbfs, _cur_mode = parse_controls(conf_path)
                except Exception:
                    cur_gain, cur_snr = 32.8, 10.0
                persist_managed_controls_override(
                    target,
                    conf_path,
                    gain=float(cur_gain),
                    squelch_mode="dbfs",
                    squelch_snr=float(cur_snr),
                    squelch_dbfs=float(plan.get("threshold_median") or -60.0),
                    squelch_preset=plan.get("preset"),
                    squelch_preset_margin_db=plan.get("margin_db"),
                    squelch_preset_noise_floor_dbfs=plan.get("noise_floor_median"),
                    squelch_preset_computed_at_ms=plan.get("written_at_ms") or int(time.time() * 1000),
                )
            except Exception:
                logger.debug("managed override persist for preset skipped", exc_info=True)
            return self._send(
                200,
                json.dumps({
                    "ok": True,
                    "band": "air" if target == "airband" else "ground",
                    "target": target,
                    "preset": plan.get("preset"),
                    "margin_db": plan.get("margin_db"),
                    "threshold_median": plan.get("threshold_median"),
                    "noise_floor_median": plan.get("noise_floor_median"),
                    "freqs_count": len(plan.get("freqs") or []),
                    "stats_available": plan.get("stats_available"),
                    "changed": bool(plan.get("changed")),
                    "pending_restart": bool(plan.get("changed")),
                }),
                "application/json; charset=utf-8",
            )

        # ============ /api/airband/squelch_auto — Phase 2 SB5 ============
        # Per-band AUTO/MANUAL toggle for the continuous noise-floor
        # tracker (ui/squelch_tracker.py).  When AUTO is off, the
        # tracker leaves that band's thresholds frozen at whatever the
        # last preset apply wrote.  No auth — solo-user phase.
        if p == "/api/airband/squelch_auto":
            band_raw = str(form.get("band", "")).strip().lower()
            band_map = {"air": "airband", "airband": "airband",
                        "ground": "ground", "gnd": "ground"}
            target = band_map.get(band_raw)
            if not target:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": "unknown band (expected 'air' or 'ground')"}),
                    "application/json; charset=utf-8",
                )
            enabled_raw = form.get("enabled")
            if isinstance(enabled_raw, bool):
                enabled = enabled_raw
            else:
                token = str(enabled_raw or "").strip().lower()
                if token in ("1", "true", "on", "yes", "auto"):
                    enabled = True
                elif token in ("0", "false", "off", "no", "manual"):
                    enabled = False
                else:
                    return self._send(
                        400,
                        json.dumps({"ok": False, "error": "missing/unparseable 'enabled' (expected bool)"}),
                        "application/json; charset=utf-8",
                    )
            # ----- Phase 4c feature-flag branch (single, top of handler).
            # When the chirp path is on, route through the adapter so the
            # audit log captures the toggle on the chirp side too.  The
            # adapter delegates to the SAME persistence helper, so the
            # behavior is identical — the difference is that the
            # squelch_tracker (Task 5) reads the flag and pushes
            # set_squelch via the chirp client instead of writing
            # rtl_airband.conf when the flag is on.
            use_chirp = False
            try:
                use_chirp = bool(_chirp_use_gr_demod())
            except Exception:
                logger.debug("squelch_auto: use_gr_demod probe failed", exc_info=True)
            try:
                if use_chirp:
                    changed = _chirp_adapter.set_squelch_auto_via_chirp(target, enabled)
                else:
                    changed = _set_band_squelch_auto(target, enabled)
            except Exception as exc:
                logger.exception("squelch_auto toggle failed (use_chirp=%s)", use_chirp)
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": f"persist failed: {exc}"}),
                    "application/json; charset=utf-8",
                )
            return self._send(
                200,
                json.dumps({
                    "ok": True,
                    "band": "air" if target == "airband" else "ground",
                    "target": target,
                    "enabled": bool(enabled),
                    "changed": bool(changed),
                }),
                "application/json; charset=utf-8",
            )

        if p == "/api/apply":
            target = form.get("target", "airband")
            if target not in ("airband", "ground"):
                return self._send(400, json.dumps({"ok": False, "error": "unknown target"}), "application/json; charset=utf-8")
            gate = gate_action("apply", target=target)
            if not gate.get("ok"):
                return self._send(
                    409,
                    json.dumps({"ok": False, "error": "preflight blocked", "preflight": gate}),
                    "application/json; charset=utf-8",
                )
            try:
                gain = float(form.get("gain", "32.8"))
                squelch_mode = (form.get("squelch_mode") or "dbfs").lower()
                squelch_snr = form.get("squelch_snr", form.get("squelch", "10.0"))
                squelch_dbfs = form.get("squelch_dbfs", form.get("squelch", "0"))
                squelch_snr = float(squelch_snr)
                squelch_dbfs = float(squelch_dbfs)
            except ValueError:
                return self._send(400, json.dumps({"ok": False, "error": "bad values"}), "application/json; charset=utf-8")
            result = enqueue_apply(target, gain, squelch_mode, squelch_snr, squelch_dbfs)
            return self._send(result["status"], json.dumps(result["payload"]), "application/json; charset=utf-8")

        if p in ("/api/auto-squelch", "/api/analog/auto-squelch"):
            raw_targets = form.get("targets")
            parsed_targets: list[str] = []
            if isinstance(raw_targets, list):
                for item in raw_targets:
                    value = str(item or "").strip().lower()
                    if value:
                        parsed_targets.append(value)
            elif isinstance(raw_targets, str):
                tokenized = [tok.strip().lower() for tok in raw_targets.replace(";", ",").split(",")]
                parsed_targets.extend(tok for tok in tokenized if tok)
            fallback_target = str(form.get("target", "") or "").strip().lower()
            if fallback_target and not parsed_targets:
                parsed_targets.append(fallback_target)
            if not parsed_targets:
                parsed_targets = ["airband", "ground"]

            ordered_targets: list[str] = []
            for candidate in parsed_targets:
                if candidate not in ("airband", "ground"):
                    continue
                if candidate in ordered_targets:
                    continue
                ordered_targets.append(candidate)
            if not ordered_targets:
                return self._send(400, json.dumps({"ok": False, "error": "unknown target"}), "application/json; charset=utf-8")

            blocked: dict[str, Any] = {}
            for target in ordered_targets:
                gate = gate_action("apply", target=target)
                if not gate.get("ok"):
                    blocked[target] = gate
            if blocked:
                return self._send(
                    409,
                    json.dumps({"ok": False, "error": "preflight blocked", "preflight": blocked}),
                    "application/json; charset=utf-8",
                )

            result = enqueue_action({"type": "auto_squelch", "targets": ordered_targets})
            return self._send(result["status"], json.dumps(result["payload"]), "application/json; charset=utf-8")

        if p == "/api/apply-batch":
            target = form.get("target", "airband")
            if target not in ("airband", "ground"):
                return self._send(400, json.dumps({"ok": False, "error": "unknown target"}), "application/json; charset=utf-8")
            gate = gate_action("apply_batch", target=target)
            if not gate.get("ok"):
                return self._send(
                    409,
                    json.dumps({"ok": False, "error": "preflight blocked", "preflight": gate}),
                    "application/json; charset=utf-8",
                )
            try:
                gain = float(form.get("gain", "32.8"))
                squelch_mode = (form.get("squelch_mode") or "dbfs").lower()
                squelch_snr = form.get("squelch_snr", form.get("squelch", "10.0"))
                squelch_dbfs = form.get("squelch_dbfs", form.get("squelch", "0"))
                cutoff_hz = float(form.get("cutoff_hz", "3500"))
                squelch_snr = float(squelch_snr)
                squelch_dbfs = float(squelch_dbfs)
            except ValueError:
                return self._send(400, json.dumps({"ok": False, "error": "bad values"}), "application/json; charset=utf-8")
            result = enqueue_action({
                "type": "apply_batch",
                "target": target,
                "gain": gain,
                "squelch_mode": squelch_mode,
                "squelch_snr": squelch_snr,
                "squelch_dbfs": squelch_dbfs,
                "cutoff_hz": cutoff_hz,
            })
            return self._send(result["status"], json.dumps(result["payload"]), "application/json; charset=utf-8")

        if p == "/api/filter":
            target = form.get("target", "airband")
            if target not in ("airband", "ground"):
                return self._send(400, json.dumps({"ok": False, "error": "unknown target"}), "application/json; charset=utf-8")
            gate = gate_action("filter", target=target)
            if not gate.get("ok"):
                return self._send(
                    409,
                    json.dumps({"ok": False, "error": "preflight blocked", "preflight": gate}),
                    "application/json; charset=utf-8",
                )
            try:
                cutoff_hz = float(form.get("cutoff_hz", "3500"))
            except ValueError:
                return self._send(400, json.dumps({"ok": False, "error": "bad values"}), "application/json; charset=utf-8")
            result = enqueue_action({"type": "filter", "target": target, "cutoff_hz": cutoff_hz})
            return self._send(result["status"], json.dumps(result["payload"]), "application/json; charset=utf-8")

        if p == "/api/restart":
            target = form.get("target", "airband")
            vlc_analog_was = vlc_running(target="analog")
            vlc_digital_was = vlc_running(target="digital")
            result = enqueue_action({"type": "restart", "target": target})
            # Restart VLC if it was playing before the service restart
            try:
                if target in ("airband", "ground") and vlc_analog_was:
                    stop_vlc(target="analog")
                    start_vlc(target="analog")
                    logger.info("VLC analog restarted after %s restart", target)
                elif target == "digital" and vlc_digital_was:
                    stop_vlc(target="digital")
                    start_vlc(target="digital")
                    logger.info("VLC digital restarted after digital restart")
                elif target == "icecast":
                    if vlc_analog_was:
                        stop_vlc(target="analog")
                        start_vlc(target="analog")
                    if vlc_digital_was:
                        stop_vlc(target="digital")
                        start_vlc(target="digital")
                    if vlc_analog_was or vlc_digital_was:
                        logger.info("VLC restarted after icecast restart")
            except Exception:
                logger.exception("Failed to restart VLC after %s restart", target)
            return self._send(result["status"], json.dumps(result["payload"]), "application/json; charset=utf-8")

        if p in ("/api/dongles/power", "/api/dongles/power/schedule"):
            if p == "/api/dongles/power/schedule":
                auto_off = get_str("auto_off", "").strip()
                auto_on = get_str("auto_on", "").strip()
                enabled_raw = get_str("enabled", "0").strip().lower()
                enabled = enabled_raw in ("1", "true", "yes", "on")
                logger.info(
                    "POST /api/dongles/power/schedule enabled=%s auto_off=%s auto_on=%s",
                    enabled, auto_off, auto_on,
                )
                try:
                    ok, err = save_schedule(auto_off, auto_on, enabled)
                    schedule = load_schedule()
                    payload = {"ok": ok, "schedule": schedule}
                    if not ok:
                        payload["error"] = err
                    return self._send(200 if ok else 500, json.dumps(payload), "application/json; charset=utf-8")
                except Exception as e:
                    logger.exception("POST /api/dongles/power/schedule failed")
                    return self._send(500, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")

            # POST /api/dongles/power
            action = get_str("action", "").strip().lower()
            if action not in ("on", "off"):
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": "action must be 'on' or 'off'"}),
                    "application/json; charset=utf-8",
                )
            logger.info("POST /api/dongles/power action=%s", action)
            try:
                if action == "off":
                    ok, lines = power_off(set_by="api")
                else:
                    ok, lines = power_on(set_by="api")
                _invalidate_runtime_caches()
                state = get_power_state()
                schedule = load_schedule()
                payload = {"ok": ok, "state": state, "schedule": schedule, "lines": lines}
                status_code = 200 if ok else 500
                return self._send(status_code, json.dumps(payload), "application/json; charset=utf-8")
            except Exception as e:
                logger.exception("POST /api/dongles/power action=%s failed", action)
                return self._send(500, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")

        if p == "/api/bt-heal":
            action = get_str("action", "status").strip().lower() or "status"
            status_payload = read_bt_audio_heal_status()
            if action in ("status", "get"):
                return self._send(
                    200,
                    json.dumps(
                        {
                            "ok": True,
                            "enabled": bool(status_payload.get("auto_recovery_enabled")),
                            "bt_heal": status_payload,
                        }
                    ),
                    "application/json; charset=utf-8",
                )
            desired_enabled: bool | None = None
            if action in ("enable", "on", "start"):
                desired_enabled = True
            elif action in ("disable", "off", "stop"):
                desired_enabled = False
            elif action == "toggle":
                desired_enabled = not bool(status_payload.get("auto_recovery_enabled"))
            elif action in ("set", "update"):
                desired_enabled = parse_bool_value(form.get("enabled", "0"), field="enabled")
            elif action == "connect":
                proc = subprocess.run(
                    ["sudo", "systemctl", "start", "scanner-bt-audio-heal"],
                    capture_output=True, text=True, check=False,
                )
                ok = proc.returncode == 0
                err = (proc.stderr or proc.stdout or "").strip()
                payload = {"ok": bool(ok), "action": "connect"}
                if err:
                    payload["error"] = str(err)
                return self._send(
                    200 if ok else 500,
                    json.dumps(payload),
                    "application/json; charset=utf-8",
                )
            if desired_enabled is None:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": "unknown action"}),
                    "application/json; charset=utf-8",
                )
            ok, err = set_bt_heal_auto_recovery(bool(desired_enabled))
            refreshed = read_bt_audio_heal_status()
            payload = {
                "ok": bool(ok),
                "requested_enabled": bool(desired_enabled),
                "enabled": bool(refreshed.get("auto_recovery_enabled")),
                "bt_heal": refreshed,
            }
            if err:
                payload["error"] = str(err)
            return self._send(
                200 if ok else 500,
                json.dumps(payload),
                "application/json; charset=utf-8",
            )

        if p == "/api/usb-hub-reset":
            result = enqueue_action({"type": "usb_hub_reset"})
            return self._send(result["status"], json.dumps(result["payload"]), "application/json; charset=utf-8")

        if p == "/api/reboot-host":
            ok, err = reboot_host()
            payload = {
                "ok": bool(ok),
                "message": "host reboot requested" if ok else "host reboot failed",
            }
            if err:
                payload["error"] = str(err)
            return self._send(
                200 if ok else 500,
                json.dumps(payload),
                "application/json; charset=utf-8",
            )

        if p == "/api/avoid":
            target = form.get("target", "airband")
            result = enqueue_action({"type": "avoid", "target": target})
            return self._send(result["status"], json.dumps(result["payload"]), "application/json; charset=utf-8")

        if p == "/api/avoid-clear":
            target = form.get("target", "airband")
            result = enqueue_action({"type": "avoid_clear", "target": target})
            return self._send(result["status"], json.dumps(result["payload"]), "application/json; charset=utf-8")

        if p == "/api/volume":
            action = get_str("action", "get").strip().lower()
            sink_name = get_str("sink", "alsa_output.pci-0000_00_1f.3.analog-stereo").strip()
            _vol_env = os.environ.copy()
            _vol_env["XDG_RUNTIME_DIR"] = "/run/user/1000"
            # Resolve node name -> numeric ID (wpctl only accepts numeric IDs)
            sink = sink_name
            try:
                _pw = subprocess.run(["pw-cli", "ls", "Node"], env=_vol_env, timeout=3, capture_output=True, text=True)
                _parts = re.split(r"(?=^\tid \d+, type)", _pw.stdout, flags=re.MULTILINE)
                for _blk in _parts:
                    if sink_name in _blk:
                        _m = re.match(r"^\tid (\d+)", _blk)
                        if _m:
                            sink = _m.group(1)
                            break
            except Exception:
                pass
            if action == "set":
                raw_level = get_str("level", "").strip()
                try:
                    level = max(0.0, min(1.5, float(raw_level) / 100.0))
                except (ValueError, TypeError):
                    return self._send(400, json.dumps({"ok": False, "error": "bad level"}), "application/json; charset=utf-8")
                try:
                    subprocess.run(["wpctl", "set-volume", sink, str(round(level, 2))],
                                   env=_vol_env, timeout=2, check=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception as exc:
                    return self._send(500, json.dumps({"ok": False, "error": str(exc)}), "application/json; charset=utf-8")
            try:
                res = subprocess.run(["wpctl", "get-volume", sink],
                                     env=_vol_env, timeout=2, capture_output=True, text=True, check=True)
                raw = res.stdout.strip()
                vol_frac = float(raw.split(":")[1].strip().split()[0])
                vol_pct = round(vol_frac * 100)
            except Exception:
                vol_pct = -1
            return self._send(200, json.dumps({"ok": True, "volume": vol_pct, "sink": sink}), "application/json; charset=utf-8")

        if p == "/api/vlc":
            action = get_str("action", "start").strip().lower()
            target = get_str("target", "").strip().lower()
            mount = get_str("mount", "").strip()
            valid_targets = ("analog", "digital")
            if target and target not in valid_targets:
                return self._send(400, json.dumps({"ok": False, "error": "unknown target"}), "application/json; charset=utf-8")
            if action == "status":
                targets = vlc_status()
                if target:
                    running = bool(targets.get(target))
                    return self._send(200, json.dumps({
                        "ok": True,
                        "target": target,
                        "running": running,
                        "targets": targets,
                    }), "application/json; charset=utf-8")
                return self._send(200, json.dumps({
                    "ok": True,
                    "running": bool(vlc_running()),
                    "targets": targets,
                }), "application/json; charset=utf-8")
            if not target:
                target = "analog"
            if action == "start":
                ok, err = start_vlc(target=target, mount=mount)
            elif action == "stop":
                ok, err = stop_vlc(target=target)
            elif action == "restart":
                stop_vlc(target=target)
                ok, err = start_vlc(target=target, mount=mount)
            else:
                return self._send(400, json.dumps({"ok": False, "error": "unknown action"}), "application/json; charset=utf-8")
            targets = vlc_status()
            payload = {
                "ok": ok,
                "target": target,
                "running": bool(targets.get(target)),
                "targets": targets,
            }
            if not ok:
                payload["error"] = err or "command failed"
                return self._send(500, json.dumps(payload), "application/json; charset=utf-8")
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/tune":
            target = form.get("target", "airband")
            freq = form.get("freq")
            result = enqueue_action({"type": "tune", "target": target, "freq": freq})
            return self._send(result["status"], json.dumps(result["payload"]), "application/json; charset=utf-8")

        if p == "/api/tune-restore":
            target = form.get("target", "airband")
            result = enqueue_action({"type": "tune_restore", "target": target})
            return self._send(result["status"], json.dumps(result["payload"]), "application/json; charset=utf-8")

        if p == "/api/hold":
            target = form.get("target", "airband")
            mode = form.get("action", "start")
            freq = form.get("freq")
            action = {"type": "hold", "target": target, "mode": mode}
            if mode != "stop":
                action["freq"] = freq
            result = enqueue_action(action)
            return self._send(result["status"], json.dumps(result["payload"]), "application/json; charset=utf-8")

        if p == "/api/diagnostic":
            try:
                path = write_diagnostic_log()
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")
            return self._send(200, json.dumps({"ok": True, "path": path}), "application/json; charset=utf-8")

        if p == "/api/ask-claude":
            try:
                try:
                    from .claude_ask import ask as claude_ask
                except ImportError:
                    from ui.claude_ask import ask as claude_ask
            except Exception as e:
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": f"claude_ask import failed: {e}"}),
                    "application/json; charset=utf-8",
                )
            question = str(form.get("question") or "").strip()
            if not question:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": "missing question"}),
                    "application/json; charset=utf-8",
                )
            session_id = str(form.get("session_id") or "").strip() or None
            include_status_raw = form.get("include_status")
            include_status = True
            if include_status_raw is not None:
                include_status = str(include_status_raw).strip().lower() not in (
                    "0",
                    "false",
                    "no",
                    "off",
                )
            result = claude_ask(
                question,
                session_id=session_id,
                include_status=include_status,
            )
            status_code = 200 if result.get("ok") else 500
            return self._send(
                status_code,
                json.dumps(result),
                "application/json; charset=utf-8",
            )

        if p == "/api/ask-claude/reset":
            try:
                try:
                    from .claude_ask import reset_session
                except ImportError:
                    from ui.claude_ask import reset_session
            except Exception as e:
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": f"claude_ask import failed: {e}"}),
                    "application/json; charset=utf-8",
                )
            session_id = str(form.get("session_id") or "").strip()
            if not session_id:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": "missing session_id"}),
                    "application/json; charset=utf-8",
                )
            removed = reset_session(session_id)
            return self._send(
                200,
                json.dumps({"ok": True, "removed": bool(removed)}),
                "application/json; charset=utf-8",
            )

        # ============================================================
        # Phase 6a — POST /api/waterfall: writes /run/scannerproject/
        # waterfall/config.json, which scanner-waterfall.service polls
        # by mtime and uses to retune both dongles around `center_mhz`.
        # ============================================================
        if p == "/api/waterfall":
            try:
                payload_in = form if isinstance(form, dict) else {}
                center_mhz = float(payload_in.get("center_mhz"))
            except (TypeError, ValueError) as exc:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": f"invalid center_mhz: {exc}"}),
                    "application/json; charset=utf-8",
                )
            bw_mhz = None
            if payload_in.get("bw_mhz") is not None:
                try:
                    bw_mhz = float(payload_in.get("bw_mhz"))
                except (TypeError, ValueError) as exc:
                    return self._send(
                        400,
                        json.dumps({"ok": False, "error": f"invalid bw_mhz: {exc}"}),
                        "application/json; charset=utf-8",
                    )
            ok, msg = _waterfall_write_config(center_mhz, bw_mhz)
            if not ok:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": msg}),
                    "application/json; charset=utf-8",
                )
            resp = {"ok": True, "center_mhz": center_mhz}
            if bw_mhz is not None:
                resp["bw_mhz"] = bw_mhz
            return self._send(
                200,
                json.dumps(resp),
                "application/json; charset=utf-8",
            )

        # ============================================================
        # Phase 6b — VFO POST: file-backed config merge.  Accepts any
        # subset of {freq_mhz, mod, muted, bt_routed}, validates, and
        # atomically writes /run/scannerproject/vfo/config.json which
        # scripts/vfo.py picks up via mtime-poll.
        # ============================================================
        if p == "/api/vfo":
            payload_in = form if isinstance(form, dict) else {}
            ok, msg, applied = _vfo_write_config(payload_in)
            if not ok:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": msg}),
                    "application/json; charset=utf-8",
                )
            return self._send(
                200,
                json.dumps({"ok": True, "applied": applied}),
                "application/json; charset=utf-8",
            )

        # ============================================================
        # Phase 6b.2 — VFO per-knob endpoints powering the new sliders
        # on the VFO card.  Each commit POSTs one of:
        #   POST /api/vfo/squelch  {threshold_dbfs, auto}
        #   POST /api/vfo/gain     {gain_db}
        # Both merge into /run/scannerproject/vfo/config.json which
        # scripts/vfo.py picks up via mtime-poll within ~250ms.
        # Unlike rtl-airband, the VFO is a single-process demod loop
        # — changes apply live, no service restart required.
        # ============================================================
        if p == "/api/vfo/squelch":
            payload_in = form if isinstance(form, dict) else {}
            patch = {}
            if "threshold_dbfs" in payload_in:
                patch["squelch_dbfs"] = payload_in["threshold_dbfs"]
            if "auto" in payload_in:
                patch["squelch_auto"] = bool(payload_in["auto"])
            if not patch:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": "need threshold_dbfs and/or auto"}),
                    "application/json; charset=utf-8",
                )
            ok, msg, applied = _vfo_write_config(patch)
            if not ok:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": msg}),
                    "application/json; charset=utf-8",
                )
            return self._send(
                200,
                json.dumps({
                    "ok": True,
                    "threshold_dbfs": applied.get("squelch_dbfs"),
                    "auto": applied.get("squelch_auto"),
                    "live_apply": True,
                }),
                "application/json; charset=utf-8",
            )

        if p == "/api/vfo/gain":
            payload_in = form if isinstance(form, dict) else {}
            if "gain_db" not in payload_in:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": "need gain_db"}),
                    "application/json; charset=utf-8",
                )
            ok, msg, applied = _vfo_write_config({"gain_db": payload_in["gain_db"]})
            if not ok:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": msg}),
                    "application/json; charset=utf-8",
                )
            return self._send(
                200,
                json.dumps({
                    "ok": True,
                    "gain_db": applied.get("gain_db"),
                    "live_apply": True,
                }),
                "application/json; charset=utf-8",
            )

        # Phase 6c — POST /api/disco/range: validates + atomically
        # writes /run/scannerproject/disco/coord_config.json, which
        # disco-coordinator.service picks up next tick and propagates
        # to per-tuner sweep_config_<serial>.json.
        if p == "/api/disco/range":
            try:
                payload_in = form if isinstance(form, dict) else {}
                start_mhz = float(payload_in.get("start_mhz"))
                end_mhz = float(payload_in.get("end_mhz"))
            except (TypeError, ValueError) as exc:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": f"invalid disco range: {exc}"}),
                    "application/json; charset=utf-8",
                )
            ok, msg = _disco_write_range(start_mhz, end_mhz)
            if not ok:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": msg}),
                    "application/json; charset=utf-8",
                )
            return self._send(
                200,
                json.dumps({"ok": True, "start_mhz": start_mhz, "end_mhz": end_mhz}),
                "application/json; charset=utf-8",
            )

        # Phase 6d — POST /api/sounding: writes /run/scannerproject/
        # broker/mode.json which scanner-tuner-broker.service picks up
        # via mtime poll (within ~500ms) and uses to swap dongle
        # ownership between Disco and the sounding consumers.
        if p == "/api/sounding":
            payload_in = form if isinstance(form, dict) else {}
            sounding = payload_in.get("sounding")
            if not isinstance(sounding, bool):
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": "sounding must be a boolean"}),
                    "application/json; charset=utf-8",
                )
            ok, msg = _broker_write_mode(sounding)
            if not ok:
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": msg}),
                    "application/json; charset=utf-8",
                )
            return self._send(
                200,
                json.dumps({"ok": True, "sounding": sounding}),
                "application/json; charset=utf-8",
            )

        return self._send(404, json.dumps({"ok": False, "error": "not found"}), "application/json; charset=utf-8")

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass
