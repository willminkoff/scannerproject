"""Scanner activity monitoring and hit tracking."""
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
        ICECAST_HIT_MIN_DURATION, UNITS
    )
    from .systemd import unit_active
except ImportError:
    from ui.config import (
        RE_ACTIVITY, RE_ACTIVITY_TS, HIT_GAP_RESET_SECONDS,
        AIRBAND_MIN_MHZ, AIRBAND_MAX_MHZ, LAST_HIT_AIRBAND_PATH,
        LAST_HIT_GROUND_PATH, ICECAST_HIT_LOG_PATH, ICECAST_HIT_LOG_LIMIT,
        ICECAST_HIT_MIN_DURATION, UNITS
    )
    from ui.systemd import unit_active


_ANALOG_HIT_CUTOFF_LOCK = threading.Lock()
_ANALOG_HIT_CUTOFF_TS = {"airband": 0.0, "ground": 0.0}


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
    # Force cache miss so callers see post-switch hits immediately.
    read_hit_list_cached._cache = {"value": [], "ts": 0.0}


def _analog_hit_cutoff_snapshot() -> dict:
    with _ANALOG_HIT_CUTOFF_LOCK:
        return {
            "airband": float(_ANALOG_HIT_CUTOFF_TS.get("airband") or 0.0),
            "ground": float(_ANALOG_HIT_CUTOFF_TS.get("ground") or 0.0),
        }


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
        pass
    except Exception:
        pass
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
    # Try journalctl first with frequency filtering
    value = read_last_hit_for_range(UNITS["rtl"], True)
    if value:
        return value
    # Fall back to last-hit file if nothing in journal
    value = read_last_hit_file(LAST_HIT_AIRBAND_PATH)
    if value:
        return value
    # Final fallback: only accept journal value if it is in airband range
    value = read_last_hit_from_journal_unit(UNITS["rtl"])
    try:
        freq_value = float(value)
    except (TypeError, ValueError):
        return ""
    return value if _freq_in_airband(freq_value) else ""


def read_last_hit_ground() -> str:
    """Read the last ground hit."""
    value = read_last_hit_file(LAST_HIT_GROUND_PATH)
    if value:
        return value
    value = read_last_hit_for_range(UNITS["rtl"], False)
    if value:
        return value
    value = read_last_hit_for_range(UNITS["ground"], False)
    if value:
        return value
    return read_last_hit_from_journal_unit(UNITS["ground"])


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
    value = read_last_hit_airband()
    if value:
        return value

    items = read_hit_list_cached()
    if items:
        return items[0].get("freq", "") or ""

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
    entries = []
    entries.extend(read_hit_list_for_unit(UNITS["rtl"], limit=limit, scan_lines=scan_lines))
    entries.extend(read_hit_list_for_unit(UNITS["ground"], limit=limit, scan_lines=scan_lines))
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
        pass
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
        pass


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
