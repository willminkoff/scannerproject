"""Scanner activity monitoring and hit tracking."""
import logging
import os
import subprocess
import time
import json
import re
import datetime
import threading
from typing import Optional

try:
    from .config import (
        RE_ACTIVITY, RE_ACTIVITY_TS, HIT_GAP_RESET_SECONDS,
        AIRBAND_MIN_MHZ, AIRBAND_MAX_MHZ, LAST_HIT_AIRBAND_PATH,
        LAST_HIT_GROUND_PATH, ICECAST_HIT_LOG_PATH, ICECAST_HIT_LOG_LIMIT,
        ICECAST_HIT_MIN_DURATION, UNITS, GROUND_CONFIG_PATH,
        ANALOG_AUTO_SQUELCH_STATS_PATH,
    )
    from .profile_config import parse_freqs_labels, read_active_config_path
    from .systemd import unit_active
except ImportError:
    from ui.config import (
        RE_ACTIVITY, RE_ACTIVITY_TS, HIT_GAP_RESET_SECONDS,
        AIRBAND_MIN_MHZ, AIRBAND_MAX_MHZ, LAST_HIT_AIRBAND_PATH,
        LAST_HIT_GROUND_PATH, ICECAST_HIT_LOG_PATH, ICECAST_HIT_LOG_LIMIT,
        ICECAST_HIT_MIN_DURATION, UNITS, GROUND_CONFIG_PATH,
        ANALOG_AUTO_SQUELCH_STATS_PATH,
    )
    from ui.profile_config import parse_freqs_labels, read_active_config_path
    from ui.systemd import unit_active

logger = logging.getLogger(__name__)


_ANALOG_HIT_CUTOFF_LOCK = threading.Lock()
_ANALOG_HIT_CUTOFF_TS = {"airband": 0.0, "ground": 0.0}
_LIVE_ANALOG_HIT_LOCK = threading.Lock()
_LIVE_ANALOG_HIT_STATE = {
    "entries": [],
    "current": {"airband": None, "ground": None},
    "files": {
        "airband": {"path": "", "mtime_ns": 0, "value": ""},
        "ground": {"path": "", "mtime_ns": 0, "value": ""},
    },
    "allowlists": {
        "airband": {"path": "", "mtime_ns": 0, "freqs": set()},
        "ground": {"path": "", "mtime_ns": 0, "freqs": set()},
    },
    "stats": {"path": "", "mtime_ns": 0, "counters": {}, "initialized": False},
    "scan_health": {
        "samples": {"airband": [], "ground": []},
        "last_update_ts": 0.0,
    },
}
_LIVE_ANALOG_HIT_ENTRY_LIMIT = max(200, int(ICECAST_HIT_LOG_LIMIT or 200))
_RE_STATS_ACTIVITY_COUNTER = re.compile(
    r'^channel_activity_counter\{freq="([0-9.]+)"[^}]*\}\s+([0-9.]+)\s*$'
)
_STATS_HIT_GAP_SECONDS = max(float(HIT_GAP_RESET_SECONDS), 20.0)
_SCAN_HEALTH_WINDOW_SECONDS = 30.0
_SCAN_HEALTH_KEEP_SECONDS = max(_SCAN_HEALTH_WINDOW_SECONDS * 2.0, 60.0)
_SCAN_HEALTH_MONOPOLY_RATIO = 0.85
_SCAN_HEALTH_MONOPOLY_MIN_ACTIVITY = 50


def mark_analog_hit_cutoff(target: str, at_ts: Optional[float] = None) -> None:
    """Advance analog hit cutoff for a target profile switch."""
    key = str(target or "").strip().lower()
    if key not in ("airband", "ground"):
        return
    ts = float(at_ts or time.time())
    with _ANALOG_HIT_CUTOFF_LOCK:
        current = float(_ANALOG_HIT_CUTOFF_TS.get(key) or 0.0)
        if ts > current:
            _ANALOG_HIT_CUTOFF_TS[key] = ts
    with _LIVE_ANALOG_HIT_LOCK:
        _LIVE_ANALOG_HIT_STATE["current"][key] = None
        _LIVE_ANALOG_HIT_STATE["entries"] = [
            dict(item)
            for item in (_LIVE_ANALOG_HIT_STATE.get("entries") or [])
            if str(item.get("source") or "").strip().lower() != key or float(item.get("ts") or 0.0) >= ts
        ]
    # Force cache miss so callers see post-switch hits immediately.
    read_hit_list_cached._cache = {"value": [], "ts": 0.0}
    read_last_hit_from_journal_cached._cache = {"value": "", "ts": 0.0}


def _analog_hit_cutoff_snapshot() -> dict:
    with _ANALOG_HIT_CUTOFF_LOCK:
        return {
            "airband": float(_ANALOG_HIT_CUTOFF_TS.get("airband") or 0.0),
            "ground": float(_ANALOG_HIT_CUTOFF_TS.get("ground") or 0.0),
        }


def _reset_live_analog_hit_state() -> None:
    with _ANALOG_HIT_CUTOFF_LOCK:
        _ANALOG_HIT_CUTOFF_TS["airband"] = 0.0
        _ANALOG_HIT_CUTOFF_TS["ground"] = 0.0
    with _LIVE_ANALOG_HIT_LOCK:
        _LIVE_ANALOG_HIT_STATE["entries"] = []
        _LIVE_ANALOG_HIT_STATE["current"] = {"airband": None, "ground": None}
        _LIVE_ANALOG_HIT_STATE["files"] = {
            "airband": {"path": "", "mtime_ns": 0, "value": ""},
            "ground": {"path": "", "mtime_ns": 0, "value": ""},
        }
        _LIVE_ANALOG_HIT_STATE["allowlists"] = {
            "airband": {"path": "", "mtime_ns": 0, "freqs": set()},
            "ground": {"path": "", "mtime_ns": 0, "freqs": set()},
        }
        _LIVE_ANALOG_HIT_STATE["stats"] = {"path": "", "mtime_ns": 0, "counters": {}, "initialized": False}
        _LIVE_ANALOG_HIT_STATE["scan_health"] = {
            "samples": {"airband": [], "ground": []},
            "last_update_ts": 0.0,
        }
    read_hit_list_cached._cache = {"value": [], "ts": 0.0}
    read_last_hit_from_journal_cached._cache = {"value": "", "ts": 0.0}


def _normalize_freq_text(value: str) -> str:
    text = str(value or "").strip()
    if not text or text == "-":
        return ""
    match = re.search(r"[0-9]+(?:\.[0-9]+)?", text)
    if not match:
        return ""
    try:
        return f"{float(match.group(0)):.4f}"
    except ValueError:
        return ""


def _active_config_path_for_target(target: str) -> str:
    if target == "ground":
        return os.path.realpath(str(GROUND_CONFIG_PATH or "").strip())
    return os.path.realpath(str(read_active_config_path() or "").strip())


def _allowed_freq_keys_for_target(target: str) -> set[str]:
    key = "ground" if str(target or "").strip().lower() == "ground" else "airband"
    path = _active_config_path_for_target(key)
    if not path or not os.path.isfile(path):
        return set()
    try:
        stat = os.stat(path)
    except OSError:
        return set()
    mtime_ns = int(getattr(stat, "st_mtime_ns", int(float(stat.st_mtime) * 1_000_000_000)))
    with _LIVE_ANALOG_HIT_LOCK:
        cache = dict((_LIVE_ANALOG_HIT_STATE.get("allowlists") or {}).get(key) or {})
    if cache.get("path") == path and int(cache.get("mtime_ns") or 0) == mtime_ns:
        cached_freqs = cache.get("freqs")
        if isinstance(cached_freqs, set):
            return set(cached_freqs)
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            freqs, _labels = parse_freqs_labels(handle.read())
    except Exception:
        logger.debug("scanner: failed to load active profile allowlist from %s", path, exc_info=True)
        freqs = []
    keys = set()
    for freq in freqs or []:
        try:
            keys.add(f"{float(freq):.4f}")
        except Exception:
            continue
    with _LIVE_ANALOG_HIT_LOCK:
        allowlists = _LIVE_ANALOG_HIT_STATE.get("allowlists") or {}
        allowlists[key] = {"path": path, "mtime_ns": mtime_ns, "freqs": set(keys)}
        _LIVE_ANALOG_HIT_STATE["allowlists"] = allowlists
    return keys


def _analog_hit_allowed(source: str, freq_text: str) -> bool:
    normalized_source = "ground" if str(source or "").strip().lower() == "ground" else "airband"
    normalized_freq = _normalize_freq_text(freq_text)
    if not normalized_freq:
        return False
    allowed = _allowed_freq_keys_for_target(normalized_source)
    return normalized_freq in allowed if allowed else False


def _append_live_analog_hit_entry_locked(source: str, current: dict) -> None:
    if not isinstance(current, dict):
        return
    freq = _normalize_freq_text(current.get("freq"))
    if not freq:
        return
    last_ts = float(current.get("last_ts") or 0.0)
    start_ts = float(current.get("start_ts") or last_ts or 0.0)
    if last_ts <= 0:
        return
    entry = {
        "time": time.strftime("%H:%M:%S", time.localtime(last_ts)),
        "freq": freq,
        "duration": max(0, int(last_ts - start_ts)),
        "ts": last_ts,
        "source": source,
        "type": source,
    }
    entries = list(_LIVE_ANALOG_HIT_STATE.get("entries") or [])
    entries.append(entry)
    if len(entries) > _LIVE_ANALOG_HIT_ENTRY_LIMIT:
        entries = entries[-_LIVE_ANALOG_HIT_ENTRY_LIMIT:]
    _LIVE_ANALOG_HIT_STATE["entries"] = entries
    # Persist each finalized analog hit to the on-disk hit log exactly once.
    # The Mar-25 live-hit refactor (commit 0eec825) replaced the old
    # icecast-title poller — which used to drive update_icecast_hit_log() —
    # with refresh_analog_hit_state(), but dropped disk persistence, leaving
    # ICECAST_HIT_LOG_PATH unwritten. This restores it at the single point
    # where a hit is finalized, so no de-dup bookkeeping is needed.
    _append_icecast_hit_entry(dict(entry))


def _finalize_stale_live_analog_hits_locked(now_ts: float) -> None:
    for source in ("airband", "ground"):
        current = (_LIVE_ANALOG_HIT_STATE.get("current") or {}).get(source)
        if not isinstance(current, dict):
            continue
        last_ts = float(current.get("last_ts") or 0.0)
        if last_ts <= 0:
            (_LIVE_ANALOG_HIT_STATE.get("current") or {})[source] = None
            continue
        gap_reset_sec = max(
            float(HIT_GAP_RESET_SECONDS),
            float(current.get("gap_reset_sec") or 0.0),
        )
        if (now_ts - last_ts) > gap_reset_sec:
            _append_live_analog_hit_entry_locked(source, current)
            (_LIVE_ANALOG_HIT_STATE.get("current") or {})[source] = None


def _record_live_analog_hit(
    source: str,
    freq_text: str,
    ts: float,
    *,
    gap_reset_sec: Optional[float] = None,
) -> None:
    normalized_source = "ground" if str(source or "").strip().lower() == "ground" else "airband"
    freq = _normalize_freq_text(freq_text)
    if not freq:
        return
    effective_gap_reset = max(float(HIT_GAP_RESET_SECONDS), float(gap_reset_sec or 0.0))
    cutoffs = _analog_hit_cutoff_snapshot()
    if ts < float(cutoffs.get(normalized_source) or 0.0):
        return
    if not _analog_hit_allowed(normalized_source, freq):
        return
    with _LIVE_ANALOG_HIT_LOCK:
        _finalize_stale_live_analog_hits_locked(ts)
        current_map = _LIVE_ANALOG_HIT_STATE.get("current") or {}
        current = current_map.get(normalized_source)
        if isinstance(current, dict):
            last_ts = float(current.get("last_ts") or 0.0)
            gap = ts - last_ts if last_ts > 0 else None
            current_gap_reset = max(
                float(HIT_GAP_RESET_SECONDS),
                float(current.get("gap_reset_sec") or 0.0),
            )
            if current.get("freq") == freq and (gap is None or gap <= max(current_gap_reset, effective_gap_reset)):
                current["last_ts"] = max(ts, last_ts)
                current["gap_reset_sec"] = max(current_gap_reset, effective_gap_reset)
                return
            _append_live_analog_hit_entry_locked(normalized_source, current)
        current_map[normalized_source] = {
            "freq": freq,
            "start_ts": ts,
            "last_ts": ts,
            "gap_reset_sec": effective_gap_reset,
        }
        _LIVE_ANALOG_HIT_STATE["current"] = current_map


def _read_activity_counter_stats(path: str) -> dict[str, int]:
    counters: dict[str, int] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            for raw in handle:
                match = _RE_STATS_ACTIVITY_COUNTER.match(raw.strip())
                if not match:
                    continue
                freq = _normalize_freq_text(match.group(1))
                if not freq:
                    continue
                try:
                    counters[freq] = int(float(match.group(2)))
                except Exception:
                    continue
    except Exception:
        logger.debug("scanner: failed to read activity counters from %s", path, exc_info=True)
        return {}
    return counters


def _prune_scan_health_samples_locked(now_ts: float) -> None:
    scan_health = dict(_LIVE_ANALOG_HIT_STATE.get("scan_health") or {})
    samples = dict(scan_health.get("samples") or {})
    cutoff = float(now_ts) - float(_SCAN_HEALTH_KEEP_SECONDS)
    for source in ("airband", "ground"):
        bucket = []
        for item in samples.get(source) or []:
            if float((item or {}).get("ts") or 0.0) >= cutoff:
                bucket.append(dict(item or {}))
        samples[source] = bucket
    scan_health["samples"] = samples
    scan_health["last_update_ts"] = max(float(scan_health.get("last_update_ts") or 0.0), float(now_ts))
    _LIVE_ANALOG_HIT_STATE["scan_health"] = scan_health


def _record_scan_health_sample(source: str, deltas: dict[str, int], ts: float) -> None:
    if not deltas:
        return
    normalized_source = "ground" if str(source or "").strip().lower() == "ground" else "airband"
    with _LIVE_ANALOG_HIT_LOCK:
        scan_health = dict(_LIVE_ANALOG_HIT_STATE.get("scan_health") or {})
        samples = dict(scan_health.get("samples") or {})
        bucket = [dict(item or {}) for item in (samples.get(normalized_source) or [])]
        bucket.append(
            {
                "ts": float(ts),
                "deltas": {
                    _normalize_freq_text(freq): int(delta)
                    for freq, delta in (deltas or {}).items()
                    if _normalize_freq_text(freq) and int(delta) > 0
                },
            }
        )
        samples[normalized_source] = bucket
        scan_health["samples"] = samples
        scan_health["last_update_ts"] = max(float(scan_health.get("last_update_ts") or 0.0), float(ts))
        _LIVE_ANALOG_HIT_STATE["scan_health"] = scan_health
        _prune_scan_health_samples_locked(ts)


def get_analog_scan_health(now: Optional[float] = None) -> dict:
    refresh_analog_hit_state(now=now)
    now_ts = float(now if now is not None else time.time())
    with _LIVE_ANALOG_HIT_LOCK:
        stats_state = dict(_LIVE_ANALOG_HIT_STATE.get("stats") or {})
        scan_health = dict(_LIVE_ANALOG_HIT_STATE.get("scan_health") or {})
        samples_map = dict(scan_health.get("samples") or {})
    stats_age_ms = 0
    if int(stats_state.get("mtime_ns") or 0) > 0:
        stats_age_ms = max(
            0,
            int(round(now_ts * 1000.0)) - int(int(stats_state.get("mtime_ns") or 0) / 1_000_000),
        )
    payload: dict[str, dict] = {}
    window_start = now_ts - float(_SCAN_HEALTH_WINDOW_SECONDS)
    for source in ("airband", "ground"):
        allowed = sorted(_allowed_freq_keys_for_target(source), key=float)
        activity = {freq: 0 for freq in allowed}
        for sample in samples_map.get(source) or []:
            sample_ts = float((sample or {}).get("ts") or 0.0)
            if sample_ts < window_start:
                continue
            deltas = dict((sample or {}).get("deltas") or {})
            for freq, delta in deltas.items():
                normalized = _normalize_freq_text(freq)
                if normalized not in activity:
                    continue
                activity[normalized] = int(activity.get(normalized) or 0) + max(0, int(delta))
        active_pairs = [(freq, count) for freq, count in activity.items() if int(count) > 0]
        total_activity = sum(count for _freq, count in active_pairs)
        dominant_frequency = ""
        dominant_activity = 0
        dominant_ratio = 0.0
        if active_pairs and total_activity > 0:
            dominant_frequency, dominant_activity = max(active_pairs, key=lambda item: item[1])
            dominant_ratio = float(dominant_activity) / float(total_activity)
        monopolized = (
            len(allowed) > 1
            and total_activity >= int(_SCAN_HEALTH_MONOPOLY_MIN_ACTIVITY)
            and dominant_ratio >= float(_SCAN_HEALTH_MONOPOLY_RATIO)
        )
        payload[source] = {
            "target": source,
            "window_sec": float(_SCAN_HEALTH_WINDOW_SECONDS),
            "profile_frequency_count": len(allowed),
            "profile_frequencies": list(allowed),
            "active_frequency_count": len(active_pairs),
            "activity_total": int(total_activity),
            "activity_by_frequency": [
                {"freq": freq, "activity": int(activity.get(freq) or 0)}
                for freq in allowed
            ],
            "dominant_frequency": dominant_frequency,
            "dominant_activity": int(dominant_activity),
            "dominant_ratio": round(float(dominant_ratio), 4),
            "monopolized": bool(monopolized),
            "stats_age_ms": int(stats_age_ms),
            "stats_fresh": bool(int(stats_state.get("mtime_ns") or 0) > 0 and stats_age_ms <= 5000),
            "stats_path": str(stats_state.get("path") or ""),
        }
    return payload


def refresh_analog_hit_state(now: Optional[float] = None) -> None:
    now_ts = float(now if now is not None else time.time())
    with _LIVE_ANALOG_HIT_LOCK:
        _finalize_stale_live_analog_hits_locked(now_ts)
        _prune_scan_health_samples_locked(now_ts)
    file_specs = {
        "airband": str(LAST_HIT_AIRBAND_PATH or "").strip(),
        "ground": str(LAST_HIT_GROUND_PATH or "").strip(),
    }
    for source, path in file_specs.items():
        if not path:
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        mtime_ns = int(getattr(stat, "st_mtime_ns", int(float(stat.st_mtime) * 1_000_000_000)))
        mtime_ts = float(stat.st_mtime or now_ts)
        with _LIVE_ANALOG_HIT_LOCK:
            file_state = dict((_LIVE_ANALOG_HIT_STATE.get("files") or {}).get(source) or {})
        if file_state.get("path") == path and int(file_state.get("mtime_ns") or 0) == mtime_ns:
            continue
        value = read_last_hit_file(path)
        with _LIVE_ANALOG_HIT_LOCK:
            files = _LIVE_ANALOG_HIT_STATE.get("files") or {}
            files[source] = {"path": path, "mtime_ns": mtime_ns, "value": value}
            _LIVE_ANALOG_HIT_STATE["files"] = files
        if value:
            _record_live_analog_hit(source, value, mtime_ts)
    stats_path = str(ANALOG_AUTO_SQUELCH_STATS_PATH or "").strip()
    if stats_path:
        try:
            stat = os.stat(stats_path)
        except OSError:
            stat = None
        if stat is not None:
            mtime_ns = int(getattr(stat, "st_mtime_ns", int(float(stat.st_mtime) * 1_000_000_000)))
            mtime_ts = float(stat.st_mtime or now_ts)
            with _LIVE_ANALOG_HIT_LOCK:
                stats_state = dict(_LIVE_ANALOG_HIT_STATE.get("stats") or {})
            if stats_state.get("path") != stats_path or int(stats_state.get("mtime_ns") or 0) != mtime_ns:
                counters = _read_activity_counter_stats(stats_path)
                previous_counters = dict(stats_state.get("counters") or {})
                initialized = bool(stats_state.get("initialized"))
                with _LIVE_ANALOG_HIT_LOCK:
                    _LIVE_ANALOG_HIT_STATE["stats"] = {
                        "path": stats_path,
                        "mtime_ns": mtime_ns,
                        "counters": dict(counters),
                        "initialized": True,
                    }
                if initialized:
                    delta_by_source = {"airband": {}, "ground": {}}
                    for freq, count in counters.items():
                        prev = previous_counters.get(freq)
                        if prev is None or count <= int(prev):
                            continue
                        delta = int(count) - int(prev)
                        if delta <= 0:
                            continue
                        try:
                            freq_value = float(freq)
                        except Exception:
                            continue
                        source = "airband" if _freq_in_airband(freq_value) else "ground"
                        delta_by_source[source][freq] = int(delta_by_source[source].get(freq) or 0) + int(delta)
                        _record_live_analog_hit(
                            source,
                            freq,
                            mtime_ts,
                            gap_reset_sec=_STATS_HIT_GAP_SECONDS,
                        )
                    for source in ("airband", "ground"):
                        _record_scan_health_sample(source, delta_by_source[source], mtime_ts)
    with _LIVE_ANALOG_HIT_LOCK:
        _finalize_stale_live_analog_hits_locked(now_ts)


def _live_hit_items_snapshot(limit: int = 20) -> list:
    now_ts = time.time()
    with _LIVE_ANALOG_HIT_LOCK:
        entries = [dict(item) for item in (_LIVE_ANALOG_HIT_STATE.get("entries") or [])]
        current_map = dict(_LIVE_ANALOG_HIT_STATE.get("current") or {})
    for source in ("airband", "ground"):
        current = current_map.get(source)
        if not isinstance(current, dict):
            continue
        last_ts = float(current.get("last_ts") or 0.0)
        start_ts = float(current.get("start_ts") or last_ts or 0.0)
        gap_reset_sec = max(
            float(HIT_GAP_RESET_SECONDS),
            float(current.get("gap_reset_sec") or 0.0),
        )
        if last_ts <= 0 or (now_ts - last_ts) > gap_reset_sec:
            continue
        freq = _normalize_freq_text(current.get("freq"))
        if not freq:
            continue
        entries.append({
            "time": time.strftime("%H:%M:%S", time.localtime(last_ts)),
            "freq": freq,
            "duration": max(0, int(now_ts - start_ts)),
            "ts": last_ts,
            "source": source,
            "type": source,
        })
    entries.sort(key=lambda item: float(item.get("ts") or 0.0))
    entries = entries[-max(1, int(limit or 20)):]
    entries.reverse()
    return entries


def _latest_live_hit_freq(source: str) -> str:
    refresh_analog_hit_state()
    source_key = "ground" if str(source or "").strip().lower() == "ground" else "airband"
    items = _live_hit_items_snapshot(limit=_LIVE_ANALOG_HIT_ENTRY_LIMIT)
    for item in items:
        if str(item.get("source") or "").strip().lower() == source_key:
            freq = _normalize_freq_text(item.get("freq"))
            if freq:
                return freq
    return ""


def _filter_legacy_hits_to_active_pool(entries: list) -> list:
    out = []
    for item in entries or []:
        row = dict(item or {})
        freq = _normalize_freq_text(row.get("freq"))
        if not freq:
            continue
        source = "airband" if _freq_in_airband(float(freq)) else "ground"
        if not _analog_hit_allowed(source, freq):
            continue
        row["freq"] = freq
        row["source"] = source
        row["type"] = source
        out.append(row)
    return out


def read_last_hit_file(path: str) -> str:
    """Read the last hit from a file."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [line.strip() for line in f.read().splitlines() if line.strip()]
            if not lines:
                raise ValueError("empty last-hit file")
            value = lines[-1]
            if value and value != "-":
                return value
    except FileNotFoundError:
        logger.debug("scanner: last-hit file missing at %s", path)
    except Exception:
        logger.debug("scanner: failed to read last-hit file %s", path, exc_info=True)
    return ""


def read_last_hit_from_journal_unit(unit: str) -> str:
    """Read the last activity from journalctl for a unit."""
    try:
        result = subprocess.run(
            ["journalctl", "-u", unit, "-n", "200", "-o", "cat", "--no-pager"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception:
        logger.debug("scanner: journal lookup failed for unit %s", unit, exc_info=True)
        return ""
    matches = RE_ACTIVITY.findall(result.stdout or "")
    if not matches:
        return ""
    return matches[-1]


def _freq_in_airband(freq: float) -> bool:
    """Check if a frequency is in the airband range."""
    return AIRBAND_MIN_MHZ <= freq <= AIRBAND_MAX_MHZ


def parse_activity_timestamp(date_part: str, time_part: str, tz_part: Optional[str]) -> datetime.datetime:
    """Parse activity timestamp from journal."""
    del tz_part
    return datetime.datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H:%M:%S")


def read_last_hit_for_range(unit: str, in_airband: bool, scan_lines: int = 400) -> str:
    """Read the last hit in a specific frequency range."""
    try:
        result = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(scan_lines), "-o", "short-iso", "--no-pager"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception:
        logger.debug("scanner: journal range lookup failed for unit %s", unit, exc_info=True)
        return ""

    latest_ts = None
    latest_freq = None
    for line in (result.stdout or "").splitlines():
        match = RE_ACTIVITY_TS.search(line)
        if not match:
            continue
        try:
            freq_value = float(match.group("freq"))
        except ValueError:
            continue
        if _freq_in_airband(freq_value) != in_airband:
            continue
        ts = parse_activity_timestamp(match.group("date"), match.group("time"), None)
        if latest_ts is None or ts > latest_ts:
            latest_ts = ts
            latest_freq = freq_value

    if latest_freq is None:
        return ""
    return f"{latest_freq:.4f}"


def read_last_hit_airband() -> str:
    """Read the last airband hit."""
    value = _latest_live_hit_freq("airband")
    if value:
        return value
    value = read_last_hit_file(LAST_HIT_AIRBAND_PATH)
    if value and _analog_hit_allowed("airband", value):
        return value
    value = read_last_hit_for_range(UNITS["rtl"], True)
    if value and _analog_hit_allowed("airband", value):
        return value
    value = read_last_hit_from_journal_unit(UNITS["rtl"])
    try:
        freq_value = float(value)
    except (TypeError, ValueError):
        return ""
    return value if _freq_in_airband(freq_value) and _analog_hit_allowed("airband", value) else ""


def read_last_hit_ground() -> str:
    """Read the last ground hit."""
    value = _latest_live_hit_freq("ground")
    if value:
        return value
    value = read_last_hit_file(LAST_HIT_GROUND_PATH)
    if value and _analog_hit_allowed("ground", value):
        return value
    value = read_last_hit_for_range(UNITS["rtl"], False)
    if value and _analog_hit_allowed("ground", value):
        return value
    value = read_last_hit_for_range(UNITS["ground"], False)
    if value and _analog_hit_allowed("ground", value):
        return value
    value = read_last_hit_from_journal_unit(UNITS["ground"])
    return value if value and _analog_hit_allowed("ground", value) else ""


def read_last_hit_from_journal_cached() -> str:
    """Read last hit with caching."""
    now = time.time()
    cache = getattr(read_last_hit_from_journal_cached, "_cache", {"value": "", "ts": 0.0})
    if now - cache["ts"] < 2.0:
        return cache["value"]
    value = read_last_hit_from_journal_unit(UNITS["rtl"])
    cache = {"value": value, "ts": now}
    read_last_hit_from_journal_cached._cache = cache
    return value


def read_last_hit() -> str:
    """Read the last hit from any source."""
    items = read_hit_list_cached()
    if items:
        return items[0].get("freq", "") or ""

    value = read_last_hit_airband()
    if value:
        return value

    return read_last_hit_from_journal_cached()


def read_hit_list_for_unit(unit: str, limit: int = 20, scan_lines: int = 200) -> list:
    """Read hit list from a specific unit."""
    try:
        result = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(scan_lines), "-o", "short-iso", "--no-pager"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception:
        logger.debug("scanner: failed to read hit list for unit %s", unit, exc_info=True)
        return []

    cutoffs = _analog_hit_cutoff_snapshot()
    hits = []
    for line in (result.stdout or "").splitlines():
        match = RE_ACTIVITY_TS.search(line)
        if not match:
            continue
        ts = parse_activity_timestamp(match.group("date"), match.group("time"), None)
        freq = match.group("freq")
        try:
            freq_value = float(freq)
        except Exception:
            freq_value = None
        source = "airband" if (freq_value is not None and _freq_in_airband(freq_value)) else "ground"
        if ts.timestamp() < float(cutoffs.get(source) or 0.0):
            continue
        hits.append((ts, freq))

    if not hits:
        return []

    entries = []
    current_freq = None
    start_ts = None
    last_ts = None
    for ts, freq in hits:
        if current_freq is None:
            current_freq = freq
            start_ts = ts
            last_ts = ts
            continue
        gap = (ts - last_ts).total_seconds() if last_ts else None
        if freq != current_freq or (gap is not None and gap > HIT_GAP_RESET_SECONDS):
            duration = int((last_ts - start_ts).total_seconds()) if start_ts else 0
            try:
                freq_text = f"{float(current_freq):.4f}"
            except ValueError:
                freq_text = current_freq
            entries.append({
                "time": last_ts.strftime("%H:%M:%S"),
                "freq": freq_text,
                "duration": duration,
                "ts": last_ts.timestamp(),
            })
            current_freq = freq
            start_ts = ts
        last_ts = ts

    if current_freq is not None and start_ts is not None and last_ts is not None:
        duration = int((last_ts - start_ts).total_seconds())
        try:
            freq_text = f"{float(current_freq):.4f}"
        except ValueError:
            freq_text = current_freq
        entries.append({
            "time": last_ts.strftime("%H:%M:%S"),
            "freq": freq_text,
            "duration": duration,
            "ts": last_ts.timestamp(),
        })

    entries = entries[-limit:]
    entries.reverse()
    return entries


def read_hit_list(limit: int = 20, scan_lines: int = 200) -> list:
    """Read combined hit list from all units."""
    refresh_analog_hit_state()
    live_entries = _live_hit_items_snapshot(limit=limit)
    if live_entries:
        return live_entries
    entries = []
    entries.extend(read_hit_list_for_unit(UNITS["rtl"], limit=limit, scan_lines=scan_lines))
    entries.extend(read_hit_list_for_unit(UNITS["ground"], limit=limit, scan_lines=scan_lines))
    entries = _filter_legacy_hits_to_active_pool(entries)
    if not entries:
        return []
    entries.sort(key=lambda item: item.get("ts", 0))
    entries = entries[-limit:]
    entries.reverse()
    return entries


def read_hit_list_cached(limit: int = 20) -> list:
    """Read hit list with caching."""
    now = time.time()
    cache = getattr(read_hit_list_cached, "_cache", {"value": [], "ts": 0.0})
    if now - cache["ts"] < 0.5 and len(cache["value"]) >= limit:
        return cache["value"][:limit]
    value = read_hit_list(limit=limit)
    cache = {"value": value, "ts": now}
    read_hit_list_cached._cache = cache
    return value


def _load_icecast_hit_log():
    """Load Icecast hit log from file."""
    cache = getattr(_load_icecast_hit_log, "_cache", None)
    if cache is not None:
        return cache
    cache = {
        "entries": [],
        "current": None,
    }
    try:
        with open(ICECAST_HIT_LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    cache["entries"].append(entry)
    except FileNotFoundError:
        logger.debug("scanner: hit log missing at %s", ICECAST_HIT_LOG_PATH)
    if len(cache["entries"]) > ICECAST_HIT_LOG_LIMIT:
        cache["entries"] = cache["entries"][-ICECAST_HIT_LOG_LIMIT:]
    _load_icecast_hit_log._cache = cache
    return cache


def _append_icecast_hit_entry(entry: dict) -> None:
    """Append an entry to the Icecast hit log."""
    cache = _load_icecast_hit_log()
    cache["entries"].append(entry)
    if len(cache["entries"]) > ICECAST_HIT_LOG_LIMIT:
        cache["entries"] = cache["entries"][-ICECAST_HIT_LOG_LIMIT:]
    try:
        with open(ICECAST_HIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception:
        logger.debug("scanner: failed to append Icecast hit entry to %s", ICECAST_HIT_LOG_PATH, exc_info=True)


def update_icecast_hit_log(title: str) -> None:
    """Update Icecast hit log with new title."""
    cache = _load_icecast_hit_log()
    now = time.time()
    normalized = (title or "").strip()
    current = cache.get("current")

    if not normalized:
        if current is not None:
            duration = max(0, int(now - current["start_ts"]))
            if duration >= ICECAST_HIT_MIN_DURATION:
                entry = {
                    "time": time.strftime("%H:%M:%S", time.localtime(now)),
                    "freq": current["title"],
                    "duration": duration,
                }
                _append_icecast_hit_entry(entry)
            cache["current"] = None
        return

    if current is None:
        cache["current"] = {"title": normalized, "start_ts": now}
        return

    if normalized == current["title"]:
        return

    duration = max(0, int(now - current["start_ts"]))
    if duration >= ICECAST_HIT_MIN_DURATION:
        entry = {
            "time": time.strftime("%H:%M:%S", time.localtime(now)),
            "freq": current["title"],
            "duration": duration,
        }
        _append_icecast_hit_entry(entry)
    cache["current"] = {"title": normalized, "start_ts": now}


def read_icecast_hit_list(limit: int = 20) -> list:
    """Read hit list from Icecast."""
    cache = _load_icecast_hit_log()
    items = list(cache.get("entries", []))
    current = cache.get("current")
    if current is not None:
        now = time.time()
        duration = max(0, int(now - current["start_ts"]))
        if duration >= ICECAST_HIT_MIN_DURATION:
            items.append({
                "time": time.strftime("%H:%M:%S", time.localtime(now)),
                "freq": current["title"],
                "duration": duration,
            })
    if not items:
        return []
    items = items[-limit:]
    items.reverse()
    return items


def parse_last_hit_freq(target: str) -> Optional[float]:
    """Parse the last hit frequency for a target."""
    if target == "ground":
        value = read_last_hit_ground()
    else:
        value = read_last_hit_airband() or read_last_hit()
    if not value:
        return None
    m = re.search(r'[0-9]+(?:\.[0-9]+)?', value)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None
