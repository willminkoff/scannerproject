"""HTTP request handlers."""
import json
import os
import sys
import time
import threading
import subprocess
from datetime import datetime
import queue
import shutil
from http.server import BaseHTTPRequestHandler
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen
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
    targets = []
    for candidate in (
        DIGITAL_PREFERRED_TUNER,
        DIGITAL_RTL_SERIAL,
        DIGITAL_RTL_SERIAL_SECONDARY,
        DIGITAL_RTL_DEVICE,
    ):
        value = str(candidate or "").strip()
        if value and value not in targets:
            targets.append(value)
    return targets


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
        DIGITAL_PREFERRED_TUNER,
        DIGITAL_RTL_DEVICE,
        DIGITAL_RTL_SERIAL,
        DIGITAL_RTL_SERIAL_SECONDARY,
        DIGITAL_RTL_SERIAL_HINT,
        DIGITAL_STREAM_MOUNT,
        ICECAST_PORT,
        PLAYER_MOUNT,
    )
    from .profile_config import (
        read_active_config_path, parse_controls, split_profiles,
        guess_current_profile, summarize_avoids, parse_filter,
        load_profiles_registry, find_profile, validate_profile_id, safe_profile_path,
        enforce_profile_index, set_profile, save_profiles_registry, write_airband_flag,
        parse_freqs_labels, parse_freqs_text, write_freqs_labels, write_combined_config
    )
    from .combined_status import combined_device_summary, combined_config_stale
    from .scanner import (
        read_last_hit_airband, read_last_hit_ground, read_hit_list_cached
    )
    from .icecast import (
        fetch_local_icecast_status,
        list_icecast_mounts,
        extract_icecast_title_for_mount,
    )
    from .systemd import unit_active, unit_exists, restart_rtl, unit_active_enter_epoch
    from .server_workers import enqueue_action, enqueue_apply
    from .diagnostic import write_diagnostic_log
    from .spectrum import get_spectrum_bins, spectrum_to_json, start_spectrum
    from .system_stats import get_system_stats
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
    from .profile_editor import (
        analog_profile_is_active,
        get_analog_editor_payload,
        get_digital_editor_payload,
        save_analog_editor_payload,
        save_digital_editor_payload,
        validate_analog_editor_payload,
        validate_digital_editor_payload,
    )
    from .profile_loop import get_profile_loop_manager
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
        DIGITAL_PREFERRED_TUNER,
        DIGITAL_RTL_DEVICE,
        DIGITAL_RTL_SERIAL,
        DIGITAL_RTL_SERIAL_SECONDARY,
        DIGITAL_RTL_SERIAL_HINT,
        DIGITAL_STREAM_MOUNT,
        ICECAST_PORT,
        PLAYER_MOUNT,
    )
    from ui.profile_config import (
        read_active_config_path, parse_controls, split_profiles,
        guess_current_profile, summarize_avoids, parse_filter,
        load_profiles_registry, find_profile, validate_profile_id, safe_profile_path,
        enforce_profile_index, set_profile, save_profiles_registry, write_airband_flag,
        parse_freqs_labels, parse_freqs_text, write_freqs_labels, write_combined_config
    )
    from ui.combined_status import combined_device_summary, combined_config_stale
    from ui.scanner import (
        read_last_hit_airband, read_last_hit_ground, read_hit_list_cached
    )
    from ui.icecast import (
        fetch_local_icecast_status,
        list_icecast_mounts,
        extract_icecast_title_for_mount,
    )
    from ui.systemd import unit_active, unit_exists, restart_rtl, unit_active_enter_epoch
    from ui.server_workers import enqueue_action, enqueue_apply
    from ui.diagnostic import write_diagnostic_log
    from ui.spectrum import get_spectrum_bins, spectrum_to_json, start_spectrum
    from ui.system_stats import get_system_stats
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
    from ui.profile_editor import (
        analog_profile_is_active,
        get_analog_editor_payload,
        get_digital_editor_payload,
        save_analog_editor_payload,
        save_digital_editor_payload,
        validate_analog_editor_payload,
        validate_digital_editor_payload,
    )
    from ui.profile_loop import get_profile_loop_manager
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
DIGITAL_HIT_RECENT_SEC = max(5.0, float(os.getenv("DIGITAL_HIT_RECENT_SEC", "180")))
DIGITAL_HITS_MIN_VISIBLE = max(0, int(os.getenv("DIGITAL_HITS_MIN_VISIBLE", "3")))
_DIGITAL_IDLE_TITLES = {"", "-", "idle", "n/a", "scanning", "scanning..."}
_ANALOG_LABEL_CACHE: dict[str, dict] = {}
_LOCAL_PROFILES_DIR = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "profiles"))
_STATUS_CACHE_TTL_SEC = max(0.1, float(os.getenv("STATUS_CACHE_TTL_SEC", "0.75")))
_HITS_CACHE_TTL_SEC = max(0.1, float(os.getenv("HITS_CACHE_TTL_SEC", "1.0")))
_UNIT_ACTIVE_CACHE_TTL_SEC = max(0.1, float(os.getenv("UNIT_ACTIVE_CACHE_TTL_SEC", "1.0")))
_CACHE_LOCK = threading.Lock()
_STATUS_CACHE: dict[str, object] = {"ts": 0.0, "payload": None}
_HITS_CACHE: dict[str, object] = {"ts": 0.0, "payload": None}
_UNIT_ACTIVE_CACHE: dict[str, tuple[float, bool]] = {}


def _short_label(text: str, max_len: int = 48) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    if len(raw) <= max_len:
        return raw
    return raw[: max_len - 1].rstrip() + "…"


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


def _normalize_freq_key(value) -> str:
    try:
        return f"{float(str(value).strip()):.4f}"
    except Exception:
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
        title = extract_icecast_title_for_mount(status_text, f"/{DIGITAL_STREAM_MOUNT}")
        if title.strip().lower() not in _DIGITAL_IDLE_TITLES:
            return True
    return _digital_has_recent_event()


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
    for event in events:
        label = str(event.get("label") or "").strip()
        tgid = str(event.get("tgid") or "").strip()
        if not label and tgid:
            label = f"TG {tgid}"
        if label and label.strip("()").isdigit() and tgid:
            label = f"TG {tgid}"
        if not label:
            continue
        time_ms = int(event.get("timeMs") or 0)
        ts = time_ms / 1000.0 if time_ms else time.time()
        time_str = time.strftime("%H:%M:%S", time.localtime(ts))
        digital_items.append({
            "time": time_str,
            "freq": label,
            "duration": 0,
            "label": _short_label(label, max_len=48),
            "label_full": label,
            "mode": event.get("mode"),
            "tgid": tgid,
            "type": "digital",
            "source": "digital",
            "_ts": ts,
        })
    digital_items = _coalesce_digital_hits(digital_items)

    merged = items + digital_items
    merged = _dedupe_hit_rows(merged, window_sec=2.0)
    merged.sort(key=lambda item: item.get("_ts", 0.0))
    merged = merged[-scan_limit:]
    merged.reverse()
    for item in merged:
        item.pop("_ts", None)
    if len(merged) > limit:
        merged = _ensure_digital_visibility(merged, digital_items, limit)
    else:
        merged = _ensure_digital_visibility(merged, digital_items, limit)
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
        self.wfile.write(body)

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

    def _proxy_icecast_mount(self, mount_name: str, head_only: bool = False, transcode: bool = False):
        mount = self._sanitize_mount_name(mount_name)
        if not mount:
            if head_only:
                return self._send_head(400)
            return self._send(400, "invalid mount", "text/plain; charset=utf-8")
        upstream = f"http://127.0.0.1:{ICECAST_PORT}/{mount}"
        headers_sent = False
        if transcode and not head_only:
            proc = None
            try:
                # Desktop browser compatibility path for low-rate analog streams.
                # Re-encode to a widely-supported MP3 profile.
                cmd = [
                    "ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "mp3",
                    "-i",
                    upstream,
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    "32k",
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
                    chunk = proc.stdout.read(2048)
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
            # Use a generous timeout for long-lived audio streams; short read
            # timeouts can terminate otherwise healthy low/idle bitrate mounts.
            with urlopen(req, timeout=60) as upstream_resp:
                self.send_response(200)
                self.send_header("Content-Type", upstream_resp.headers.get("Content-Type") or "audio/mpeg")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
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
                    # Keep proxy chunks small so low-bitrate streams emit data
                    # frequently enough for embedded browser players.
                    chunk = upstream_resp.read(2048)
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

    def do_HEAD(self):
        """Handle HEAD requests."""
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query or "")
        transcode = str((q.get("transcode") or ["0"])[0]).strip().lower() in ("1", "true", "yes", "on")
        if p == "/stream" or p == "/stream/":
            return self._proxy_icecast_mount("", head_only=True, transcode=transcode)
        if p.startswith("/stream/"):
            return self._proxy_icecast_mount(p[len("/stream/"):], head_only=True, transcode=transcode)
        return self._send_head(404)

    def do_GET(self):
        """Handle GET requests."""
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query or "")
        transcode = str((q.get("transcode") or ["0"])[0]).strip().lower() in ("1", "true", "yes", "on")
        if p == "/":
            return self._send_redirect("/sb3")
        
        # Serve SB3 UI
        if p == "/sb3" or p == "/sb3.html":
            ui_dir = os.path.dirname(os.path.abspath(__file__))
            mockup_path = os.path.join(ui_dir, "sb3.html")
            try:
                with open(mockup_path, "r", encoding="utf-8") as f:
                    return self._send(200, f.read())
            except FileNotFoundError:
                return self._send(404, "SB3 UI not found", "text/plain; charset=utf-8")
        
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
                else:
                    ctype = "application/octet-stream"
                return self._send(200, content, ctype)
            except FileNotFoundError:
                return self._send(404, "Not found", "text/plain; charset=utf-8")

        if p == "/stream" or p == "/stream/":
            return self._proxy_icecast_mount("", transcode=transcode)
        if p.startswith("/stream/"):
            return self._proxy_icecast_mount(p[len("/stream/"):], transcode=transcode)

        if p == "/api/profile-editor/analog":
            q = parse_qs(u.query or "")
            profile_id = (q.get("id") or [""])[0].strip()
            target = (q.get("target") or ["airband"])[0].strip().lower() or "airband"
            ok, err, payload = get_analog_editor_payload(profile_id, target)
            if not ok:
                return self._send(400, json.dumps({"ok": False, "error": err}), "application/json; charset=utf-8")
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/profile-editor/digital":
            q = parse_qs(u.query or "")
            profile_id = (q.get("profileId") or [""])[0].strip()
            ok, err, payload = get_digital_editor_payload(profile_id)
            if not ok:
                return self._send(400, json.dumps({"ok": False, "error": err}), "application/json; charset=utf-8")
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")
        
        if p == "/api/profile":
            q = parse_qs(u.query or "")
            profile_id = (q.get("id") or [""])[0].strip()
            if not profile_id:
                return self._send(400, json.dumps({"ok": False, "error": "missing id"}), "application/json; charset=utf-8")
            profiles = load_profiles_registry()
            prof = find_profile(profiles, profile_id)
            if not prof:
                return self._send(404, json.dumps({"ok": False, "error": "profile not found"}), "application/json; charset=utf-8")
            path = prof.get("path", "")
            exists = bool(path) and os.path.exists(path)
            freqs = []
            labels = []
            if exists:
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        text = f.read()
                    fvals, labs = parse_freqs_labels(text)
                    freqs = [f"{v:.4f}" for v in (fvals or [])]
                    labels = labs or []
                except Exception as e:
                    return self._send(500, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")
            payload = {
                "ok": True,
                "profile": {
                    "id": prof.get("id", ""),
                    "label": prof.get("label", ""),
                    "path": path,
                    "airband": bool(prof.get("airband")),
                    "exists": exists,
                },
                "freqs": freqs,
                "labels": labels,
            }
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/system":
            try:
                payload = get_system_stats()
            except Exception as e:
                payload = {"ok": False, "error": str(e)}
                return self._send(500, json.dumps(payload), "application/json; charset=utf-8")
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/status":
            now_monotonic = time.monotonic()
            with _CACHE_LOCK:
                cached_payload = _STATUS_CACHE.get("payload")
                cached_ts = float(_STATUS_CACHE.get("ts") or 0.0)
            if isinstance(cached_payload, dict) and (now_monotonic - cached_ts) <= _STATUS_CACHE_TTL_SEC:
                payload = dict(cached_payload)
                payload["server_time"] = time.time()
                return self._send(200, json.dumps(payload), "application/json; charset=utf-8")
            conf_path = read_active_config_path()
            ground_conf_path = os.path.realpath(GROUND_CONFIG_PATH)
            combined_conf_path = COMBINED_CONFIG_PATH
            airband_gain, airband_snr, airband_dbfs, airband_mode = parse_controls(conf_path)
            ground_gain, ground_snr, ground_dbfs, ground_mode = parse_controls(GROUND_CONFIG_PATH)
            airband_filter = parse_filter("airband")
            ground_filter = parse_filter("ground")
            rtl_unit_active = _unit_active_cached(UNITS["rtl"])
            ground_unit_active = _unit_active_cached(UNITS["ground"])
            keepalive_unit_active = _unit_active_cached(UNITS["keepalive"])
            combined_info = combined_device_summary()
            airband_device = combined_info.get("airband")
            ground_device = combined_info.get("ground")
            expected_serials = dict(combined_info.get("expected_serials") or {})
            if AIRBAND_RTL_SERIAL:
                expected_serials["airband"] = AIRBAND_RTL_SERIAL
            if GROUND_RTL_SERIAL:
                expected_serials["ground"] = GROUND_RTL_SERIAL
            expected_serials["digital"] = DIGITAL_RTL_SERIAL or ""
            expected_serials["digital_secondary"] = DIGITAL_RTL_SERIAL_SECONDARY or ""
            serial_mismatch_detail = []
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
            rtl_ok = rtl_unit_active
            ground_ok = rtl_ok and ground_present
            ice_ok = _unit_active_cached(UNITS["icecast"])
            icecast_mounts = []
            analog_stream_mount = str(PLAYER_MOUNT or "").strip().lstrip("/")
            if ice_ok:
                try:
                    status_text = fetch_local_icecast_status()
                    icecast_mounts = list_icecast_mounts(status_text)
                    analog_stream_mount = _resolve_analog_stream_mount(status_text)
                except Exception:
                    icecast_mounts = []
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

            payload = {
                "rtl_active": rtl_ok,
                "ground_active": ground_ok,
                "ground_exists": ground_present,
                "rtl_unit_active": rtl_unit_active,
                "ground_unit_active": ground_unit_active,
                "combined_config_stale": combined_stale,
                "combined_devices": len(combined_info.get("devices") or []),
                "combined_devices_detail": combined_info.get("devices") or [],
                "airband_present": airband_present,
                "ground_present": ground_present,
                "icecast_active": ice_ok,
                "icecast_mounts": icecast_mounts,
                "icecast_port": ICECAST_PORT,
                "stream_mount": analog_stream_mount,
                "stream_proxy_enabled": True,
                "digital_stream_mount": DIGITAL_STREAM_MOUNT,
                "icecast_expected_mounts": [f"/{PLAYER_MOUNT}", f"/{DIGITAL_STREAM_MOUNT}"],
                "expected_serials": expected_serials,
                "digital_tuner_targets": _digital_tuner_targets(),
                "serial_mismatch": bool(serial_mismatch_detail),
                "serial_mismatch_detail": serial_mismatch_detail,
                "keepalive_active": keepalive_unit_active,
                "server_time": time.time(),
                "rtl_active_enter": rtl_active_enter,
                "rtl_restart_required": rtl_restart_required,
                "config_paths": {
                    "airband": conf_path,
                    "ground": ground_conf_path,
                    "combined": combined_conf_path,
                },
                "config_mtimes": config_mtimes,
                "profile_airband": profile_airband,
                "profile_ground": profile_ground,
                "profiles_airband": profiles_airband,
                "profiles_ground": profiles_ground,
                "missing_profiles": missing,
                "gain": float(airband_gain),
                "squelch": float(airband_snr),
                "airband_gain": float(airband_gain),
                "airband_squelch": float(airband_snr),
                "airband_squelch_mode": airband_mode,
                "airband_squelch_snr": float(airband_snr),
                "airband_squelch_dbfs": float(airband_dbfs),
                "airband_filter": float(airband_filter),
                "ground_gain": float(ground_gain),
                "ground_squelch": float(ground_snr),
                "ground_squelch_mode": ground_mode,
                "ground_squelch_snr": float(ground_snr),
                "ground_squelch_dbfs": float(ground_dbfs),
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
            digital_stream_active_for_hits = True
            if DIGITAL_HITS_REQUIRE_ACTIVE_STREAM:
                try:
                    digital_stream_active_for_hits = _digital_stream_active_for_hits()
                except Exception:
                    digital_stream_active_for_hits = True
            # Preserve raw digital activity indicators even when stream
            # mount-state is uncertain; expose stream visibility separately.
            digital_payload["digital_stream_active_for_hits"] = bool(digital_stream_active_for_hits)
            payload.update(digital_payload)
            try:
                profile_loop_snapshot = get_profile_loop_manager().snapshot()
                profile_loop_targets = dict(profile_loop_snapshot.get("targets") or {})
            except Exception:
                profile_loop_targets = {}
            payload["profile_loop"] = profile_loop_targets
            digital_loop = profile_loop_targets.get("digital") if isinstance(profile_loop_targets, dict) else {}
            if isinstance(digital_loop, dict):
                payload["digital_profile_loop_enabled"] = bool(digital_loop.get("enabled"))
                payload["digital_profile_loop_current_profile"] = str(digital_loop.get("current_profile") or "")
                payload["digital_profile_loop_active_profile"] = str(digital_loop.get("active_profile") or "")
                payload["digital_profile_loop_next_profile"] = str(digital_loop.get("next_profile") or "")
                payload["digital_profile_loop_switch_reason"] = str(digital_loop.get("switch_reason") or "")
                payload["digital_profile_loop_switch_count"] = int(digital_loop.get("switch_count") or 0)
                payload["digital_profile_loop_last_error"] = str(digital_loop.get("last_error") or "")
            analog_loop_air = profile_loop_targets.get("airband") if isinstance(profile_loop_targets, dict) else {}
            if isinstance(analog_loop_air, dict) and analog_loop_air.get("enabled"):
                analog_active = str(analog_loop_air.get("active_profile") or "").strip()
                if analog_active:
                    payload["profile_airband"] = analog_active
            analog_loop_ground = profile_loop_targets.get("ground") if isinstance(profile_loop_targets, dict) else {}
            if isinstance(analog_loop_ground, dict) and analog_loop_ground.get("enabled"):
                analog_active_ground = str(analog_loop_ground.get("active_profile") or "").strip()
                if analog_active_ground:
                    payload["profile_ground"] = analog_active_ground
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
            digital_serial_configured = bool(DIGITAL_RTL_SERIAL)
            digital_tuner_target_configured = bool(_digital_tuner_targets())
            payload = {
                "ok": True,
                "expected_serials": {
                    "airband": airband_serial,
                    "ground": ground_serial,
                    "digital": DIGITAL_RTL_SERIAL or "",
                    "digital_secondary": DIGITAL_RTL_SERIAL_SECONDARY or "",
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
        if p == "/api/profile-loop":
            try:
                payload = get_profile_loop_manager().snapshot()
                payload = dict(payload or {})
                payload["ok"] = True
                return self._send(200, json.dumps(payload), "application/json; charset=utf-8")
            except Exception as e:
                return self._send(
                    500,
                    json.dumps({"ok": False, "error": str(e)}),
                    "application/json; charset=utf-8",
                )
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
                conf_path = read_active_config_path()
                airband_gain, airband_snr, airband_dbfs, airband_mode = parse_controls(conf_path)
                rtl_unit_active = _unit_active_cached(UNITS["rtl"])
                ground_unit_active = _unit_active_cached(UNITS["ground"])
                combined_info = combined_device_summary()
                ground_present = combined_info.get("ground") is not None
                rtl_active = rtl_unit_active
                ground_active = rtl_active and ground_present
                ice_ok = _unit_active_cached(UNITS["icecast"])
                # Keep SSE hits aligned with the full UI hit list so digital
                # rows are not dropped by top-10 truncation during busy analog traffic.
                hits_payload = _get_hits_payload_cached(limit=50)
                hit_items = hits_payload.get("items") or []
                last_hit = hit_items[0].get("freq") if hit_items else (read_last_hit_airband() or read_last_hit_ground())
                try:
                    profile_loop_targets = dict(get_profile_loop_manager().snapshot().get("targets") or {})
                except Exception:
                    profile_loop_targets = {}
                status_data = {
                    "type": "status",
                    "rtl_active": rtl_active,
                    "ground_active": ground_active,
                    "icecast_active": ice_ok,
                    "ground_unit_active": ground_unit_active,
                    "combined_config_stale": combined_config_stale(),
                    "gain": float(airband_gain),
                    "squelch": float(airband_snr),
                    "squelch_mode": airband_mode,
                    "squelch_snr": float(airband_snr),
                    "squelch_dbfs": float(airband_dbfs),
                    "last_hit": last_hit,
                    "server_time": time.time(),
                    "profile_loop": profile_loop_targets,
                }
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

        p = urlparse(self.path).path
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

        if p == "/api/profile-editor/analog/validate":
            profile_id = get_str("id").strip()
            target = get_str("target", "airband").strip().lower() or "airband"
            freqs_text = get_str("freqs_text").strip()
            modulation = get_str("modulation", "am").strip().lower() or "am"
            bandwidth_raw = get_str("bandwidth", "12000").strip()
            if not freqs_text:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": "missing freqs_text"}),
                    "application/json; charset=utf-8",
                )
            try:
                bandwidth = int(round(float(bandwidth_raw)))
            except Exception:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": "invalid bandwidth"}),
                    "application/json; charset=utf-8",
                )

            ok, err, payload = validate_analog_editor_payload(
                profile_id=profile_id,
                target=target,
                freqs_text=freqs_text,
                modulation=modulation,
                bandwidth=bandwidth,
            )
            if not ok:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": err}),
                    "application/json; charset=utf-8",
                )
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/profile-editor/digital/validate":
            profile_id = get_str("profileId").strip()
            control_channels_text = get_str("control_channels_text").strip()
            talkgroups_text = get_str("talkgroups_text")
            systems_json_text = get_str("systems_json_text")
            ok, err, payload = validate_digital_editor_payload(
                profile_id=profile_id,
                control_channels_text=control_channels_text,
                talkgroups_text=talkgroups_text,
                systems_json_text=systems_json_text,
            )
            if not ok:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": err}),
                    "application/json; charset=utf-8",
                )
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/profile-editor/analog/save":
            profile_id = get_str("id").strip()
            target = get_str("target", "airband").strip().lower() or "airband"
            freqs_text = get_str("freqs_text").strip()
            modulation = get_str("modulation", "am").strip().lower() or "am"
            bandwidth_raw = get_str("bandwidth", "12000").strip()
            if not freqs_text:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": "missing freqs_text"}),
                    "application/json; charset=utf-8",
                )
            try:
                bandwidth = int(round(float(bandwidth_raw)))
            except Exception:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": "invalid bandwidth"}),
                    "application/json; charset=utf-8",
                )

            ok, err, payload = save_analog_editor_payload(
                profile_id=profile_id,
                target=target,
                freqs_text=freqs_text,
                modulation=modulation,
                bandwidth=bandwidth,
            )
            if not ok:
                return self._send(400, json.dumps({"ok": False, "error": err}), "application/json; charset=utf-8")

            changed = bool(payload.get("changed"))
            profile_path = str(((payload.get("profile") or {}).get("path") or "")).strip()
            payload["active"] = bool(analog_profile_is_active(profile_path))
            payload["scanner_restarted"] = False
            payload["combined_changed"] = False

            if changed and payload["active"]:
                try:
                    combined_changed = bool(write_combined_config())
                    payload["combined_changed"] = combined_changed
                    if combined_changed:
                        restart_rtl()
                        payload["scanner_restarted"] = True
                except Exception as e:
                    return self._send(500, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")
            try:
                payload["v3_compile"] = compile_runtime()
            except Exception as e:
                payload["v3_compile_error"] = str(e)

            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/profile-editor/digital/save":
            profile_id = get_str("profileId").strip()
            control_channels_text = get_str("control_channels_text").strip()
            talkgroups_text = get_str("talkgroups_text")
            systems_json_text = get_str("systems_json_text")
            apply_now_raw = str(form.get("apply_now", "true")).strip().lower()
            apply_now = apply_now_raw in ("1", "true", "yes", "on", "")

            ok, err, payload = save_digital_editor_payload(
                profile_id=profile_id,
                control_channels_text=control_channels_text,
                talkgroups_text=talkgroups_text,
                systems_json_text=systems_json_text,
            )
            if not ok:
                return self._send(400, json.dumps({"ok": False, "error": err}), "application/json; charset=utf-8")

            runtime_applied = False
            runtime_error = ""
            runtime_active_profile = ""
            try:
                manager = get_digital_manager()
                runtime_active_profile = str(manager.getProfile() or "")
                if payload.get("changed") and apply_now and runtime_active_profile == profile_id:
                    gate = gate_action("digital_profile", profile_id=profile_id)
                    if not gate.get("ok"):
                        payload["runtime_applied"] = False
                        payload["runtime_error"] = "preflight blocked digital profile apply"
                        payload["preflight"] = gate
                        return self._send(409, json.dumps(payload), "application/json; charset=utf-8")
                    runtime_applied, runtime_error = manager.setProfile(profile_id)
            except Exception as e:
                runtime_error = str(e)

            payload["runtime_active_profile"] = runtime_active_profile
            payload["runtime_applied"] = bool(runtime_applied)
            if runtime_error:
                payload["runtime_error"] = runtime_error
            try:
                payload["v3_compile"] = compile_runtime()
            except Exception as e:
                payload["v3_compile_error"] = str(e)
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

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
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/digital/stop":
            ok, err = get_digital_manager().stop()
            payload = {"ok": bool(ok)}
            if not ok:
                payload["error"] = err or "stop failed"
                return self._send(500, json.dumps(payload), "application/json; charset=utf-8")
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
            scheduler_payload = {}
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
            ):
                if key in form:
                    scheduler_payload[key] = form.get(key)
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

        if p == "/api/profile-loop":
            target = get_str("target").strip().lower()
            if not target:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": "missing target"}),
                    "application/json; charset=utf-8",
                )
            update_payload = {}
            for key in ("enabled", "selected_profiles", "dwell_ms", "hang_ms", "pause_on_hit"):
                if key in form:
                    update_payload[key] = form.get(key)
            ok, err, snapshot = get_profile_loop_manager().set_target_config(target, update_payload)
            if not ok:
                return self._send(
                    400,
                    json.dumps({"ok": False, "error": err, "snapshot": snapshot}),
                    "application/json; charset=utf-8",
                )
            return self._send(
                200,
                json.dumps({"ok": True, "target": target, "snapshot": snapshot}),
                "application/json; charset=utf-8",
            )

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

        if p == "/api/profile/create":
            profile_id = get_str("id").strip()
            label = get_str("label").strip()
            airband_raw = form.get("airband", True)
            if isinstance(airband_raw, bool):
                airband_flag = airband_raw
            else:
                airband_flag = str(airband_raw).lower() in ("1", "true", "yes", "on")
            clone_from = get_str("clone_from_id").strip()
            if not validate_profile_id(profile_id):
                return self._send(400, json.dumps({"ok": False, "error": "invalid id"}), "application/json; charset=utf-8")
            # Allow minimalist create: if label omitted, default to id.
            if not label:
                label = profile_id
            profiles = load_profiles_registry()
            if find_profile(profiles, profile_id):
                return self._send(400, json.dumps({"ok": False, "error": "id already exists"}), "application/json; charset=utf-8")
            new_path = os.path.join(PROFILES_DIR, f"rtl_airband_{profile_id}.conf")
            safe_path = safe_profile_path(new_path)
            if not safe_path:
                return self._send(400, json.dumps({"ok": False, "error": "invalid path"}), "application/json; charset=utf-8")
            if os.path.exists(safe_path):
                return self._send(400, json.dumps({"ok": False, "error": "profile file exists"}), "application/json; charset=utf-8")
            try:
                src = find_profile(profiles, clone_from) if clone_from else None
                if src:
                    shutil.copyfile(src["path"], safe_path)
                else:
                    # Minimal blank template with freqs block so the textarea editor can save immediately.
                    desired_index = 0 if bool(airband_flag) else 1
                    # Avoid creating an empty freqs list: rtl_airband refuses to start if freqs is empty.
                    default_freq = "118.6000" if bool(airband_flag) else "462.6500"
                    default_mod = "am" if bool(airband_flag) else "nfm"
                    default_bw = "12000" if bool(airband_flag) else "12000"
                    template = f"""airband = {'true' if bool(airband_flag) else 'false'};\n\n""" + \
                        "devices:\n" + \
                        "({\n" + \
                        "  type = \"rtlsdr\";\n" + \
                        f"  index = {desired_index};\n" + \
                        "  mode = \"scan\";\n" + \
                        "  gain = 32.800;   # UI_CONTROLLED\n\n" + \
                        "  channels:\n" + \
                        "  (\n" + \
                        "    {\n" + \
                        f"      freqs = ({default_freq});\n\n" + \
                        f"      modulation = \"{default_mod}\";\n" + \
                        f"      bandwidth = {default_bw};\n" + \
                        "      squelch_threshold = -70;  # UI_CONTROLLED\n" + \
                        "      squelch_delay = 0.8;\n\n" + \
                        "      outputs:\n" + \
                        "      (\n" + \
                        "        {\n" + \
                        "          type = \"icecast\";\n" + \
                        "          send_scan_freq_tags = true;\n" + \
                        "          server = \"127.0.0.1\";\n" + \
                        "          port = 8000;\n" + \
                        "          mountpoint = \"scannerbox.mp3\";\n" + \
                        "          username = \"source\";\n" + \
                        "          password = \"062352\";\n" + \
                        "          name = \"SprontPi Radio\";\n" + \
                        "          genre = \"AIRBAND\";\n" + \
                        "          description = \"Custom\";\n" + \
                        "          bitrate = 32;\n" + \
                        "        }\n" + \
                        "      );\n" + \
                        "    }\n" + \
                        "  );\n" + \
                        "});\n"
                    with open(safe_path, "w", encoding="utf-8") as f:
                        f.write(template)

                write_airband_flag(safe_path, bool(airband_flag))
                enforce_profile_index(safe_path)
                freqs_text = get_str("freqs_text").strip()
                if freqs_text:
                    freqs, labels = parse_freqs_text(freqs_text)
                    write_freqs_labels(safe_path, freqs, labels)
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")
            profiles.append({
                "id": profile_id,
                "label": label,
                "path": safe_path,
                "airband": bool(airband_flag),
            })
            save_profiles_registry(profiles)
            payload = {"ok": True, "profile": profiles[-1]}
            try:
                payload["v3_compile"] = upsert_analog_profile(profiles[-1])
            except Exception as e:
                payload["v3_compile_error"] = str(e)
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/profile/update_freqs":
            profile_id = get_str("id").strip()
            if not profile_id:
                return self._send(400, json.dumps({"ok": False, "error": "missing id"}), "application/json; charset=utf-8")
            profiles = load_profiles_registry()
            prof = find_profile(profiles, profile_id)
            if not prof:
                return self._send(404, json.dumps({"ok": False, "error": "profile not found"}), "application/json; charset=utf-8")
            path = prof.get("path", "")
            safe_path = safe_profile_path(path) if path else None
            if not safe_path or not os.path.exists(safe_path):
                return self._send(404, json.dumps({"ok": False, "error": "profile file not found"}), "application/json; charset=utf-8")

            # Prefer freqs_text input (textarea format). Otherwise accept arrays.
            freqs_text = get_str("freqs_text").strip()
            labels_present = "labels" in form
            freqs_present = "freqs" in form
            try:
                if freqs_text:
                    freqs, labels = parse_freqs_text(freqs_text)
                else:
                    raw_freqs = form.get("freqs")
                    if isinstance(raw_freqs, list):
                        freqs = [float(x) for x in raw_freqs]
                    elif isinstance(raw_freqs, str) and raw_freqs.strip():
                        freqs = [float(x) for x in raw_freqs.split(",") if x.strip()]
                    else:
                        return self._send(400, json.dumps({"ok": False, "error": "missing freqs"}), "application/json; charset=utf-8")

                    if labels_present:
                        raw_labels = form.get("labels")
                        if not isinstance(raw_labels, list):
                            return self._send(400, json.dumps({"ok": False, "error": "labels must be an array"}), "application/json; charset=utf-8")
                        labels = [str(x) for x in raw_labels]
                        if len(labels) != len(freqs):
                            return self._send(400, json.dumps({"ok": False, "error": "labels must match freqs length"}), "application/json; charset=utf-8")
                    else:
                        labels = None
            except ValueError as e:
                return self._send(400, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")

            try:
                changed = write_freqs_labels(safe_path, freqs, labels)
                enforce_profile_index(safe_path)
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")

            # If editing the active profile, regenerate combined config and restart rtl only if needed.
            active_airband = os.path.realpath(read_active_config_path())
            active_ground = os.path.realpath(GROUND_CONFIG_PATH)
            is_active = os.path.realpath(safe_path) in (active_airband, active_ground)
            if is_active:
                try:
                    combined_changed = write_combined_config()
                    changed = changed or combined_changed
                    if combined_changed:
                        restart_rtl()
                except Exception as e:
                    return self._send(500, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")

            return self._send(200, json.dumps({"ok": True, "changed": bool(changed)}), "application/json; charset=utf-8")

        if p == "/api/profile/update":
            profile_id = get_str("id").strip()
            label = get_str("label").strip()
            if not profile_id or not label:
                return self._send(400, json.dumps({"ok": False, "error": "missing fields"}), "application/json; charset=utf-8")
            profiles = load_profiles_registry()
            prof = find_profile(profiles, profile_id)
            if not prof:
                return self._send(404, json.dumps({"ok": False, "error": "profile not found"}), "application/json; charset=utf-8")
            prof["label"] = label
            save_profiles_registry(profiles)
            payload = {"ok": True, "profile": prof}
            try:
                payload["v3_compile"] = upsert_analog_profile(prof)
            except Exception as e:
                payload["v3_compile_error"] = str(e)
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/profile/delete":
            profile_id = get_str("id").strip()
            profiles = load_profiles_registry()
            prof = find_profile(profiles, profile_id)
            if not prof:
                return self._send(404, json.dumps({"ok": False, "error": "profile not found"}), "application/json; charset=utf-8")
            airband_conf = read_active_config_path()
            ground_conf = os.path.realpath(GROUND_CONFIG_PATH)
            if os.path.realpath(prof["path"]) in (os.path.realpath(airband_conf), os.path.realpath(ground_conf)):
                return self._send(400, json.dumps({"ok": False, "error": "profile is active"}), "application/json; charset=utf-8")
            safe_path = safe_profile_path(prof["path"])
            if safe_path and os.path.exists(safe_path):
                try:
                    os.remove(safe_path)
                except Exception as e:
                    return self._send(500, json.dumps({"ok": False, "error": str(e)}), "application/json; charset=utf-8")
            profiles = [p for p in profiles if p.get("id") != profile_id]
            save_profiles_registry(profiles)
            payload = {"ok": True}
            try:
                payload["v3_compile"] = delete_analog_profile(profile_id)
            except Exception as e:
                payload["v3_compile_error"] = str(e)
            return self._send(200, json.dumps(payload), "application/json; charset=utf-8")

        if p == "/api/profile":
            pid = form.get("profile", "")
            target = form.get("target", "airband")
            gate = gate_action("profile", target=target)
            if not gate.get("ok"):
                return self._send(
                    409,
                    json.dumps({"ok": False, "error": "preflight blocked", "preflight": gate}),
                    "application/json; charset=utf-8",
                )
            result = enqueue_action({"type": "profile", "profile": pid, "target": target})
            payload = dict(result.get("payload") or {})
            if int(result.get("status") or 500) < 300 and payload.get("ok") and pid:
                try:
                    payload["v3_compile"] = set_active_analog_profile(target, str(pid))
                except Exception as e:
                    payload["v3_compile_error"] = str(e)
            return self._send(result["status"], json.dumps(payload), "application/json; charset=utf-8")

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
            result = enqueue_action({"type": "restart", "target": target})
            return self._send(result["status"], json.dumps(result["payload"]), "application/json; charset=utf-8")

        if p == "/api/avoid":
            target = form.get("target", "airband")
            result = enqueue_action({"type": "avoid", "target": target})
            return self._send(result["status"], json.dumps(result["payload"]), "application/json; charset=utf-8")

        if p == "/api/avoid-clear":
            target = form.get("target", "airband")
            result = enqueue_action({"type": "avoid_clear", "target": target})
            return self._send(result["status"], json.dumps(result["payload"]), "application/json; charset=utf-8")

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

        return self._send(404, json.dumps({"ok": False, "error": "not found"}), "application/json; charset=utf-8")

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass
