"""Digital backend integration (live-only, in-memory metadata)."""
from __future__ import annotations

import json
import csv
import os
import re
import subprocess
import time
from datetime import datetime

try:
    from .config import (
        AIRBAND_RTL_SERIAL,
        GROUND_RTL_SERIAL,
        DIGITAL_ACTIVE_PROFILE_LINK,
        DIGITAL_BACKEND,
        DIGITAL_EVENT_LOG_DIR,
        DIGITAL_EVENT_LOG_MODE,
        DIGITAL_EVENT_LOG_TAIL_LINES,
        DIGITAL_LOG_PATH,
        DIGITAL_PROFILES_DIR,
        DIGITAL_RTL_SERIAL,
<<<<<<< ours
<<<<<<< ours
        DIGITAL_RTL_SERIAL_HINT,
=======
        DIGITAL_RTL_DEVICE,
>>>>>>> theirs
=======
>>>>>>> theirs
        DIGITAL_SERVICE_NAME,
    )
    from .systemd import unit_active
except ImportError:
    from ui.config import (
        AIRBAND_RTL_SERIAL,
        GROUND_RTL_SERIAL,
        DIGITAL_ACTIVE_PROFILE_LINK,
        DIGITAL_BACKEND,
        DIGITAL_EVENT_LOG_DIR,
        DIGITAL_EVENT_LOG_MODE,
        DIGITAL_EVENT_LOG_TAIL_LINES,
        DIGITAL_LOG_PATH,
        DIGITAL_PROFILES_DIR,
        DIGITAL_RTL_SERIAL,
<<<<<<< ours
<<<<<<< ours
        DIGITAL_RTL_SERIAL_HINT,
=======
        DIGITAL_RTL_DEVICE,
>>>>>>> theirs
=======
>>>>>>> theirs
        DIGITAL_SERVICE_NAME,
    )
    from ui.systemd import unit_active


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$")
_MODE_RE = re.compile(r"\b(P25|P25P1|P25P2|DMR|NXDN|D-STAR|TETRA|YSF|EDACS|LTR)\b", re.I)
_PHASE1_RE = re.compile(r"\bP25\s*Phase\s*1\b", re.I)
_PHASE2_RE = re.compile(r"\bP25\s*Phase\s*2\b", re.I)
_LABEL_RE = re.compile(
    r"\b(label|alias|alpha\s*tag|talkgroup|tgid|channel|channel\s*name|alias\s*name|group)[=:]\s*([^|,]+)",
    re.I,
)
_TGID_RE = re.compile(r"\b(?:tgid|talkgroup|tg)\b[=: ]+(\d+)\b", re.I)
_EVENT_HINT_RE = re.compile(r"(call|voice|traffic|talkgroup|tgid|alias|alpha\s*tag|channel\s*event|from:|to:)", re.I)
_TS_RE = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})[ T](?P<time>\d{2}:\d{2}:\d{2})")
_TS_COMPACT_RE = re.compile(r"(?P<date>\d{8})\s+(?P<time>\d{6})(?:\.\d+)?")
_LOG_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+")
_LOG_PREFIX_COMPACT_RE = re.compile(r"^\d{8}\s+\d{6}(?:\.\d+)?\s+")
_LOG_LEVEL_RE = re.compile(r"^(INFO|WARN|ERROR|DEBUG|TRACE)\s+", re.I)
_KEY_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
_NON_FATAL_ERROR_RE = re.compile(
    r"(no audio playback devices available|couldn't obtain master gain|usb.*in-use|device is busy|unable to set usb configuration)",
    re.I,
)
_TUNER_BUSY_RE = re.compile(
    r"(in[- ]use by another application|device is busy|usb_claim_interface error|"
    r"unable to set usb configuration|failed to open rtlsdr device)",
    re.I,
)
_IGNORE_EVENT_RE = re.compile(
    r"(auto-start failed|no tuner available|mountpoint in use|unable to connect|audiooutput|playbackpreference|"
    r"audio streaming broadcaster|status: connected|starting main application|loading playlist|discovering tuners)",
    re.I,
)
_MUTE_STATE_PATH = "/run/airband_ui_digital_mute.json"
_DIGITAL_MUTED = False
_DEFAULT_PROFILE_NOTE = (
    "This is a placeholder SDRTrunk profile directory.\n"
    "Export or copy your SDRTrunk configuration into this folder.\n"
    "Then set this profile active from the UI or by updating the active symlink.\n"
)
_LISTEN_FILENAME = "talkgroups_listen.json"
_EVENT_HEADER_KEYS = (
    "timestamp",
    "time",
    "date",
    "talkgroup",
    "tgid",
    "alias",
    "alpha",
    "system",
    "site",
    "frequency",
    "freq",
)
_EVENT_LABEL_KEYS = (
    "alias",
    "alpha tag",
    "alpha",
    "talkgroup name",
    "group",
    "description",
    "name",
    "channel name",
)
_EVENT_TGID_KEYS = (
    "tgid",
    "talkgroup",
    "talkgroup id",
    "dec",
    "decimal",
    "tg",
)
_EVENT_MODE_KEYS = (
    "mode",
    "protocol",
    "type",
    "system type",
    "decoder",
)
_EVENT_TIME_KEYS = (
    "timestamp",
    "time",
    "start time",
    "event time",
    "date time",
    "start",
    "received",
)
_EVENT_DATE_KEYS = (
    "date",
    "event date",
)
_EVENT_TIME_ONLY_KEYS = (
    "time",
    "start time",
    "event time",
)
_EVENT_FREQ_KEYS = (
    "frequency",
    "freq",
    "control channel",
    "control frequency",
)
_EVENT_SITE_KEYS = (
    "site",
    "system",
    "site name",
    "system name",
)


def validate_digital_profile_id(profile_id: str) -> bool:
    """Strict validation for digital profile IDs."""
    if not profile_id:
        return False
    return bool(_NAME_RE.match(profile_id))


def validate_digital_service_name(service_name: str) -> bool:
    """Strict validation for digital service name."""
    if not service_name:
        return False
    return bool(_NAME_RE.match(service_name))


def _normalize_name(value: str) -> str:
    if not value:
        return ""
    return value.strip()


def _norm_key(value: str) -> str:
    return _KEY_NORMALIZE_RE.sub(" ", str(value or "").lower()).strip()


def _safe_realpath(path: str) -> str:
    try:
        return os.path.realpath(path)
    except Exception:
        return path


def _read_tail_lines(path: str, max_bytes: int = 8192, max_lines: int = 120):
    try:
        size = os.path.getsize(path)
    except Exception:
        return []
    start = max(size - max_bytes, 0)
    try:
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read(max_bytes)
    except Exception:
        return []
    text = data.decode("utf-8", errors="ignore")
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return lines


def _parse_time_ms(line: str, fallback_ms: int) -> int:
    m = _TS_RE.search(line or "")
    if m:
        try:
            dt = datetime.strptime(f"{m.group('date')} {m.group('time')}", "%Y-%m-%d %H:%M:%S")
            return int(time.mktime(dt.timetuple()) * 1000)
        except Exception:
            return fallback_ms
    m2 = _TS_COMPACT_RE.search(line or "")
    if m2:
        try:
            dt = datetime.strptime(f"{m2.group('date')} {m2.group('time')}", "%Y%m%d %H%M%S")
            return int(time.mktime(dt.timetuple()) * 1000)
        except Exception:
            return fallback_ms
    return fallback_ms


def _parse_time_value(value: str, fallback_ms: int) -> int:
    raw = str(value or "").strip()
    if not raw:
        return fallback_ms
    if raw.isdigit():
        if len(raw) >= 13:
            try:
                return int(raw[:13])
            except Exception:
                return fallback_ms
        if len(raw) == 10:
            try:
                return int(raw) * 1000
            except Exception:
                return fallback_ms
    return _parse_time_ms(raw, fallback_ms)


def _strip_log_prefix(line: str) -> str:
    line = (line or "").strip()
    if not line:
        return ""
    line = _LOG_PREFIX_RE.sub("", line)
    line = _LOG_PREFIX_COMPACT_RE.sub("", line)
    line = re.sub(r"^\[[^\]]+\]\s+", "", line)
    line = _LOG_LEVEL_RE.sub("", line)
    if " - " in line:
        left, right = line.split(" - ", 1)
        if "." in left or left.isupper():
            line = right
    return line.strip()


def _extract_label_mode(line: str):
    line = _strip_log_prefix(line or "")
    if not line:
        return "", "", False
    label = ""
    mode = ""
    label_from_field = False
    m_label = _LABEL_RE.search(line)
    if m_label:
        label = m_label.group(2).strip().strip('"')
        label_from_field = True
    m_tg = _TGID_RE.search(line)
    if not label and m_tg:
        label = f"TG {m_tg.group(1)}"
        label_from_field = True
    if _PHASE2_RE.search(line):
        mode = "P25P2"
    elif _PHASE1_RE.search(line):
        mode = "P25P1"
    else:
        m_mode = _MODE_RE.search(line)
        if m_mode:
            mode = m_mode.group(1).upper()
    if not label:
        label = line
    return label, mode, label_from_field


def _coerce_mode(value: str) -> str:
    if not value:
        return ""
    _, mode, _ = _extract_label_mode(str(value))
    return mode or ""


def _row_value(row: dict, keys: tuple) -> str:
    for key in keys:
        val = row.get(key)
        if val:
            return str(val).strip()
    return ""


def _row_to_event(row: dict, raw_line: str, fallback_ms: int) -> dict | None:
    label = _row_value(row, _EVENT_LABEL_KEYS)
    tgid = _row_value(row, _EVENT_TGID_KEYS)
    mode_val = _row_value(row, _EVENT_MODE_KEYS)
    time_val = _row_value(row, _EVENT_TIME_KEYS)
    date_val = _row_value(row, _EVENT_DATE_KEYS)
    time_only = _row_value(row, _EVENT_TIME_ONLY_KEYS)
    freq = _row_value(row, _EVENT_FREQ_KEYS)
    site = _row_value(row, _EVENT_SITE_KEYS)
    to_val = _row_value(row, ("to", "from"))
    if to_val:
        if not label:
            label = to_val
        if not tgid:
            m = re.search(r"\((\d+)\)", to_val)
            if not m:
                m = re.search(r"\b(\d{3,})\b", to_val)
            if m:
                tgid = m.group(1)

    time_ms = _parse_time_value(time_val, fallback_ms)
    if not time_val and (date_val or time_only):
        time_ms = _parse_time_ms(f"{date_val} {time_only}".strip(), fallback_ms)

    if not label and tgid:
        label = f"TG {tgid}"

    if not label and not tgid:
        return None

    mode = _coerce_mode(mode_val)
    if not mode:
        mode = _coerce_mode(raw_line)

    event = {
        "type": "digital",
        "label": label,
        "timeMs": int(time_ms or fallback_ms),
        "raw": raw_line,
    }
    if mode:
        event["mode"] = mode
    if tgid:
        event["tgid"] = tgid
    if freq:
        event["frequency"] = freq
    if site:
        event["site"] = site
    return event


def _extract_event_from_line(line: str, fallback_ms: int) -> dict | None:
    raw = (line or "").strip()
    if not raw:
        return None
    stripped = _strip_log_prefix(raw)
    if not stripped:
        return None
    if _IGNORE_EVENT_RE.search(stripped):
        return None
    label, mode, label_from_field = _extract_label_mode(stripped)
    if not label:
        return None
    if not (label_from_field or _EVENT_HINT_RE.search(stripped)):
        return None
    time_ms = _parse_time_ms(raw, fallback_ms)
    event = {"type": "digital", "label": label, "timeMs": time_ms, "raw": stripped}
    if mode:
        event["mode"] = mode
    tgid = _extract_tgid(stripped)
    if tgid:
        event["tgid"] = tgid
    return event


def _extract_tgid(text: str) -> str:
    if not text:
        return ""
    m = _TGID_RE.search(text)
    if m:
        return m.group(1)
    if text.strip().isdigit():
        return text.strip()
    return ""


def _is_non_fatal_error(line: str) -> bool:
    return bool(_NON_FATAL_ERROR_RE.search(line or ""))


def _write_mute_state(muted: bool) -> None:
    payload = {"muted": bool(muted), "ts": int(time.time())}
    tmp = _MUTE_STATE_PATH + ".tmp"
    try:
        os.makedirs(os.path.dirname(_MUTE_STATE_PATH) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, _MUTE_STATE_PATH)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def get_digital_muted() -> bool:
    return bool(_DIGITAL_MUTED)


def set_digital_muted(muted: bool) -> bool:
    global _DIGITAL_MUTED
    _DIGITAL_MUTED = bool(muted)
    _write_mute_state(_DIGITAL_MUTED)
    return _DIGITAL_MUTED


def _digital_profile_paths(profile_id: str):
    base = _safe_realpath(DIGITAL_PROFILES_DIR)
    target = _safe_realpath(os.path.join(DIGITAL_PROFILES_DIR, profile_id))
    return base, target


def create_digital_profile_dir(profile_id: str):
    pid = _normalize_name(profile_id)
    if not validate_digital_profile_id(pid):
        return False, "invalid profileId"
    base, target = _digital_profile_paths(pid)
    if not base:
        return False, "profiles dir not configured"
    if not target.startswith(base + os.sep):
        return False, "invalid profile path"
    if os.path.exists(target):
        return False, "profile already exists"
    try:
        os.makedirs(target, exist_ok=False)
        note_path = os.path.join(target, "README.txt")
        with open(note_path, "w", encoding="utf-8") as f:
            f.write(_DEFAULT_PROFILE_NOTE)
    except Exception as e:
        return False, str(e)
    return True, ""


def delete_digital_profile_dir(profile_id: str):
    pid = _normalize_name(profile_id)
    if not validate_digital_profile_id(pid):
        return False, "invalid profileId"
    base, target = _digital_profile_paths(pid)
    if not base:
        return False, "profiles dir not configured"
    if not target.startswith(base + os.sep):
        return False, "invalid profile path"
    if not os.path.isdir(target):
        return False, "profile not found"
    if os.path.islink(target):
        return False, "profile path is a symlink"
    active_link = DIGITAL_ACTIVE_PROFILE_LINK
    if active_link and os.path.islink(active_link):
        try:
            active_target = _safe_realpath(active_link)
        except Exception:
            active_target = ""
        if active_target and active_target == target:
            return False, "profile is active"
    try:
        import shutil
        shutil.rmtree(target)
    except Exception as e:
        return False, str(e)
    return True, ""


def inspect_digital_profile(profile_id: str, max_files: int = 200, max_depth: int = 3, max_preview_bytes: int = 20000):
    pid = _normalize_name(profile_id)
    if not validate_digital_profile_id(pid):
        return False, "invalid profileId"
    base, target = _digital_profile_paths(pid)
    if not base:
        return False, "profiles dir not configured"
    if not target.startswith(base + os.sep):
        return False, "invalid profile path"
    if not os.path.isdir(target):
        return False, "profile not found"

    files = []
    has_more = False
    for dirpath, dirnames, filenames in os.walk(target, topdown=True, followlinks=False):
        rel_dir = os.path.relpath(dirpath, target)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        if depth >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        for name in filenames:
            if len(files) >= max_files:
                has_more = True
                break
            fpath = os.path.join(dirpath, name)
            if os.path.islink(fpath):
                continue
            rel = os.path.relpath(fpath, target)
            files.append(rel)
        if has_more:
            break

    files = sorted(files)

    preview_name = ""
    preview = ""
    preview_candidates = ("README.txt", "README.md", "notes.txt")
    for candidate in preview_candidates:
        candidate_path = os.path.join(target, candidate)
        if os.path.isfile(candidate_path) and not os.path.islink(candidate_path):
            preview_name = candidate
            try:
                with open(candidate_path, "rb") as f:
                    data = f.read(max_preview_bytes)
                preview = data.decode("utf-8", errors="ignore")
            except Exception:
                preview = ""
            break

    payload = {
        "ok": True,
        "profileId": pid,
        "files": files,
        "has_more": bool(has_more),
        "previewName": preview_name,
        "preview": preview,
    }
    return True, payload


def _get_profile_dir(profile_id: str):
    pid = _normalize_name(profile_id)
    if not validate_digital_profile_id(pid):
        return "", "invalid profileId"
    base, target = _digital_profile_paths(pid)
    if not base:
        return "", "profiles dir not configured"
    if not target.startswith(base + os.sep):
        return "", "invalid profile path"
    if not os.path.isdir(target):
        return "", "profile not found"
    return target, ""


def read_digital_talkgroups(profile_id: str, max_rows: int = 5000):
    profile_dir, err = _get_profile_dir(profile_id)
    if err:
        return False, err
    candidates = ("talkgroups.csv", "talkgroups_with_group.csv")
    path = ""
    for name in candidates:
        candidate = os.path.join(profile_dir, name)
        if os.path.isfile(candidate):
            path = candidate
            break
    if not path:
        return False, "talkgroups file not found"

    listen_map = {}
    listen_path = os.path.join(profile_dir, _LISTEN_FILENAME)
    if os.path.isfile(listen_path):
        try:
            with open(listen_path, "r", encoding="utf-8", errors="ignore") as f:
                payload = json.load(f) or {}
            if isinstance(payload, dict):
                items = payload.get("items")
                if isinstance(items, dict):
                    listen_map = {str(k): bool(v) for k, v in items.items()}
        except Exception:
            listen_map = {}

    items = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if not row:
                    continue
                row_norm = {str(k or "").strip().lower(): str(v or "").strip() for k, v in row.items()}
                dec = row_norm.get("dec") or row_norm.get("decimal") or ""
                if not dec.isdigit():
                    continue
                item = {
                    "dec": dec,
                    "hex": row_norm.get("hex") or "",
                    "mode": row_norm.get("mode") or "",
                    "alpha": row_norm.get("alpha tag") or row_norm.get("alpha_tag") or "",
                    "description": row_norm.get("description") or "",
                    "tag": row_norm.get("tag") or "",
                }
                if listen_map:
                    item["listen"] = bool(listen_map.get(dec, True))
                else:
                    item["listen"] = True
                items.append(item)
                if len(items) >= max_rows:
                    break
    except Exception as e:
        return False, str(e)

    return True, {
        "ok": True,
        "profileId": _normalize_name(profile_id),
        "items": items,
        "source": os.path.basename(path),
    }


def write_digital_listen(profile_id: str, items: list):
    profile_dir, err = _get_profile_dir(profile_id)
    if err:
        return False, err
    listen_path = os.path.join(profile_dir, _LISTEN_FILENAME)
    mapping = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        dec = str(item.get("dec") or "").strip()
        if not dec.isdigit():
            continue
        mapping[dec] = bool(item.get("listen"))
    payload = {
        "updated": int(time.time()),
        "items": mapping,
    }
    try:
        tmp = listen_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        os.replace(tmp, listen_path)
    except Exception as e:
        return False, str(e)
    return True, ""


class DigitalAdapter:
    """Interface for digital backends."""
    name = "base"

    def start(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    def restart(self):
        raise NotImplementedError

    def isActive(self):
        raise NotImplementedError

    def listProfiles(self):
        raise NotImplementedError

    def getProfile(self):
        raise NotImplementedError

    def setProfile(self, profileId: str):
        raise NotImplementedError

    def getLastEvent(self):
        raise NotImplementedError

    def getLastError(self):
        raise NotImplementedError

    def getLastWarning(self):
        raise NotImplementedError

    def getRecentEvents(self, limit: int = 20):
        raise NotImplementedError


class _BaseDigitalAdapter(DigitalAdapter):
    """Shared in-memory state for adapters."""

    def __init__(self):
        self._profile = ""
        self._last_event = None
        self._last_error = ""
        self._last_warning = ""
        self._last_event_time_ms = 0
        self._recent_events = []
        self._recent_event_keys = set()
        self._recent_limit = 50

    def _set_last_error(self, msg: str):
        self._last_error = (msg or "").strip()

    def _set_last_warning(self, msg: str):
        self._last_warning = (msg or "").strip()

    def _clear_error(self):
        self._last_error = ""

    def _clear_warning(self):
        self._last_warning = ""

    def _set_last_event(self, label: str, mode: str | None = None, raw=None):
        event = {
            "label": label or "",
            "timeMs": int(time.time() * 1000),
        }
        if mode:
            event["mode"] = mode
        if raw is not None:
            event["raw"] = raw
        self._last_event = event
        self._last_event_time_ms = int(event.get("timeMs") or 0)

    def _record_event(self, event: dict) -> None:
        if not event:
            return
        if "type" not in event:
            event = dict(event)
            event["type"] = "digital"
        key = f"{event.get('timeMs')}|{event.get('label')}|{event.get('mode','')}"
        if key in self._recent_event_keys:
            return
        event = dict(event)
        event["_key"] = key
        self._recent_event_keys.add(key)
        self._recent_events.append(event)
        if len(self._recent_events) > self._recent_limit:
            old = self._recent_events.pop(0)
            old_key = old.get("_key")
            if old_key:
                self._recent_event_keys.discard(old_key)

    def getRecentEvents(self, limit: int = 20):
        items = list(self._recent_events)[-max(1, limit):]
        cleaned = []
        for item in items:
            item = dict(item)
            item.pop("_key", None)
            cleaned.append(item)
        return cleaned

    def getLastEvent(self):
        if self._last_event:
            return dict(self._last_event)
        return {"label": "", "timeMs": 0}

    def getLastError(self):
        return self._last_error or None

    def getLastWarning(self):
        return self._last_warning or None

class NullDigitalAdapter(_BaseDigitalAdapter):
    """No-op adapter when digital is disabled or misconfigured."""
    name = "none"

    def __init__(self, reason: str = "digital backend disabled"):
        super().__init__()
        self._reason = reason
        if reason:
            self._set_last_error(reason)

    def start(self):
        return False, self._reason

    def stop(self):
        return False, self._reason

    def restart(self):
        return False, self._reason

    def isActive(self):
        return False

    def listProfiles(self):
        return []

    def getProfile(self):
        return ""

    def setProfile(self, profileId: str):
        return False, self._reason

    def getRecentEvents(self, limit: int = 20):
        return []


class SdrtrunkAdapter(_BaseDigitalAdapter):
    """Systemd-backed adapter for sdrtrunk."""
    name = "sdrtrunk"

    def __init__(self):
        super().__init__()
        self._service_name = _normalize_name(DIGITAL_SERVICE_NAME)
        self._profiles_dir = DIGITAL_PROFILES_DIR
        self._active_link = DIGITAL_ACTIVE_PROFILE_LINK
        self._log_path = DIGITAL_LOG_PATH
        self._last_log_mtime = None
        self._last_log_size = None
        self._event_log_dir = DIGITAL_EVENT_LOG_DIR
        self._event_log_mode = (DIGITAL_EVENT_LOG_MODE or "auto").strip().lower()
        self._event_log_tail_lines = int(DIGITAL_EVENT_LOG_TAIL_LINES or 500)
        self._event_log_offsets = {}
        self._event_log_headers = {}
        self._tg_map = {}
        self._tg_map_profile = ""
        self._tg_map_mtime = None
        self._listen_map = {}
        self._listen_map_profile = ""
        self._listen_map_mtime = None
        if not validate_digital_service_name(self._service_name):
            self._set_last_error("invalid digital service name")

    def _systemctl(self, args):
        if not validate_digital_service_name(self._service_name):
            return False, "invalid digital service name"
        cmd = ["systemctl"] + list(args) + [self._service_name]
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        except Exception as e:
            return False, str(e)
        if result.returncode == 0:
            return True, ""
        err = (result.stderr or result.stdout or "").strip()
        if not err:
            err = f"systemctl failed (code {result.returncode})"
        # Retry with sudo if policykit blocks non-root control.
        if "interactive authentication required" in err.lower() or "access denied" in err.lower():
            try:
                result = subprocess.run(
                    ["sudo"] + cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
            except Exception as e:
                return False, str(e)
            if result.returncode == 0:
                return True, ""
            err = (result.stderr or result.stdout or "").strip() or err
        return False, err

    def _refresh_log_cache(self):
        mode = self._event_log_mode or "auto"
        mode = mode if mode in ("auto", "event_logs", "app_log") else "auto"
        lines = []
        fallback_ms = int(time.time() * 1000)
        app_events = []

        try:
            stat = os.stat(self._log_path)
            mtime = stat.st_mtime
            size = stat.st_size
        except Exception:
            mtime = None
            size = None
        if mtime and size is not None:
            if self._last_log_mtime != mtime or self._last_log_size != size:
                self._last_log_mtime = mtime
                self._last_log_size = size
                lines = _read_tail_lines(self._log_path)
            if lines:
                fallback_ms = int(mtime * 1000) if mtime else fallback_ms
                if mode in ("auto", "app_log"):
                    for line in lines:
                        event = _extract_event_from_line(line, fallback_ms)
                        if event:
                            mapped = self._map_event_label(event)
                            if mapped and mapped.get("muted"):
                                continue
                            app_events.append(mapped)

                # Last error/warning (best-effort) from app log regardless of mode.
                last_err = None
                last_warn = None
                for line in reversed(lines):
                    if re.search(r"(error|exception)", line, re.I):
                        if _is_non_fatal_error(line):
                            if not last_warn:
                                last_warn = line.strip()
                            continue
                        last_err = line.strip()
                        break
                if last_err:
                    self._set_last_error(last_err)
                else:
                    self._clear_error()
                if last_warn:
                    self._set_last_warning(last_warn)
                else:
                    self._clear_warning()

        event_log_events = []
        if mode in ("auto", "event_logs"):
            event_log_events = self._read_event_logs()

        events = []
        if mode == "app_log":
            events = app_events
        elif mode == "event_logs":
            events = event_log_events
        else:
            events = app_events if app_events else event_log_events

        if events:
            for event in events:
                self._record_event(event)
            latest = max(events, key=lambda item: item.get("timeMs", 0))
            self._last_event = latest
            self._last_event_time_ms = int(latest.get("timeMs") or fallback_ms)
            return

        if mode == "app_log" and lines:
            # Last event fallback: use last non-empty line only for app_log mode.
            last_line = ""
            for line in reversed(lines):
                if line.strip():
                    last_line = line.strip()
                    break
            if not last_line:
                return
            if not (_EVENT_HINT_RE.search(last_line) or _TGID_RE.search(last_line)):
                return
            time_ms = _parse_time_ms(last_line, fallback_ms)
            label, mode_label, _ = _extract_label_mode(last_line)
            event = {"type": "digital", "label": label, "timeMs": time_ms, "raw": last_line}
            if mode_label:
                event["mode"] = mode_label
            event = self._map_event_label(event)
            self._last_event = event
            self._last_event_time_ms = time_ms
            return

    def _list_event_log_files(self):
        base = self._event_log_dir
        if not base or not os.path.isdir(base):
            return []
        candidates = []
        try:
            entries = os.listdir(base)
        except Exception:
            return []
        for name in entries:
            if name.startswith("."):
                continue
            path = os.path.join(base, name)
            if not os.path.isfile(path):
                continue
            if not re.search(r"\.(csv|log|txt|json)$", name, re.I):
                continue
            try:
                mtime = os.path.getmtime(path)
            except Exception:
                mtime = 0
            candidates.append((mtime, path, name))
        call_candidates = [item for item in candidates if "call_events" in item[2].lower()]
        if call_candidates:
            candidates = call_candidates
        candidates.sort(key=lambda item: item[0], reverse=True)
        return [path for _, path, _ in candidates[:5]]

    def _ensure_event_log_header(self, path: str) -> None:
        if path in self._event_log_headers:
            return
        header = None
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                first = f.readline()
            if first:
                row = next(csv.reader([first]))
                if row:
                    norm = [_norm_key(x) for x in row]
                    joined = " ".join(norm)
                    if any(_norm_key(k) in joined for k in _EVENT_HEADER_KEYS):
                        header = norm
        except Exception:
            header = None
        self._event_log_headers[path] = header

    def _read_event_log_lines(self, path: str):
        try:
            size = os.path.getsize(path)
        except Exception:
            return []
        last_offset = self._event_log_offsets.get(path)
        if last_offset is None:
            self._ensure_event_log_header(path)
            lines = _read_tail_lines(path, max_bytes=65536, max_lines=self._event_log_tail_lines)
            self._event_log_offsets[path] = size
            return lines
        if size < last_offset:
            last_offset = 0
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(last_offset)
                data = f.read()
                self._event_log_offsets[path] = f.tell()
        except Exception:
            return []
        return data.splitlines()

    def _parse_event_log_line(self, raw: str, path: str, fallback_ms: int):
        text = (raw or "").strip()
        if not text:
            return None
        if _IGNORE_EVENT_RE.search(text):
            return None
        if text.startswith("{") and text.endswith("}"):
            try:
                payload = json.loads(text)
            except Exception:
                payload = None
            if isinstance(payload, dict):
                row = {_norm_key(k): str(v) for k, v in payload.items()}
                return _row_to_event(row, text, fallback_ms)
        header = self._event_log_headers.get(path)
        try:
            row = next(csv.reader([text]))
        except Exception:
            row = []
        if row and header is None:
            norm = [_norm_key(x) for x in row]
            joined = " ".join(norm)
            if any(_norm_key(k) in joined for k in _EVENT_HEADER_KEYS):
                self._event_log_headers[path] = norm
                return None
            header = None
        if row and header:
            if row and all(_norm_key(x) == (header[i] if i < len(header) else "") for i, x in enumerate(row[: len(header)])):
                return None
            row_norm = {}
            for idx, key in enumerate(header):
                if idx >= len(row):
                    break
                row_norm[key] = row[idx]
            event = _row_to_event(row_norm, text, fallback_ms)
            if event:
                return event
        kv = {}
        if ":" in text or "=" in text:
            parts = re.split(r"[|,]", text)
            for part in parts:
                if ":" in part:
                    key, val = part.split(":", 1)
                elif "=" in part:
                    key, val = part.split("=", 1)
                else:
                    continue
                key = _norm_key(key)
                if key:
                    kv[key] = val.strip()
        if kv:
            event = _row_to_event(kv, text, fallback_ms)
            if event:
                return event
        return _extract_event_from_line(text, fallback_ms)

    def _read_event_logs(self):
        events = []
        paths = self._list_event_log_files()
        now_ms = int(time.time() * 1000)
        for path in paths:
            self._ensure_event_log_header(path)
            lines = self._read_event_log_lines(path)
            if not lines:
                continue
            try:
                mtime = os.path.getmtime(path)
            except Exception:
                mtime = None
            fallback_ms = int(mtime * 1000) if mtime else now_ms
            for line in lines:
                event = self._parse_event_log_line(line, path, fallback_ms)
                if event:
                    mapped = self._map_event_label(event)
                    if mapped and mapped.get("muted"):
                        continue
                    events.append(mapped)
        return events

    def _read_log_tail(self, max_lines: int | None = None):
        max_lines = max_lines or self._event_log_tail_lines or 500
        return _read_tail_lines(self._log_path, max_lines=max_lines)

    def _detect_tuner_busy(self, lines: list) -> list:
        hits = []
        now_ms = int(time.time() * 1000)
        for line in lines or []:
            if _TUNER_BUSY_RE.search(line or ""):
                raw = (line or "").strip()
                ts = _parse_time_ms(raw, now_ms)
                hits.append({"line": raw, "timeMs": ts})
        return hits

    def preflight(self):
        lines = self._read_log_tail()
        busy_hits = self._detect_tuner_busy(lines)
        busy_lines = [h.get("line") for h in busy_hits if h.get("line")]
        busy_lines = busy_lines[-10:]
        last_time = 0
        for hit in reversed(busy_hits):
            if hit.get("timeMs"):
                last_time = int(hit.get("timeMs"))
                break
        return {
            "tuner_busy": bool(busy_lines),
            "tuner_busy_lines": busy_lines,
            "tuner_busy_count": len(busy_hits),
            "tuner_busy_last_time_ms": last_time,
        }

    def start(self):
        ok, err = self._systemctl(["start"])
        if not ok:
            self._set_last_error(err or "start failed")
            return False, self._last_error
        self._clear_error()
        return True, ""

    def stop(self):
        ok, err = self._systemctl(["stop"])
        if not ok:
            self._set_last_error(err or "stop failed")
            return False, self._last_error
        self._clear_error()
        return True, ""

    def restart(self):
        ok, err = self._systemctl(["restart"])
        if not ok:
            self._set_last_error(err or "restart failed")
            return False, self._last_error
        self._clear_error()
        return True, ""

    def isActive(self):
        if not validate_digital_service_name(self._service_name):
            return False
        return unit_active(self._service_name)

    def _list_profile_dirs(self):
        profiles = []
        base = self._profiles_dir
        if not base:
            return profiles
        try:
            entries = os.listdir(base)
        except Exception:
            return profiles
        for name in entries:
            if not validate_digital_profile_id(name):
                continue
            path = os.path.join(base, name)
            if os.path.isdir(path):
                profiles.append(name)
        return sorted(profiles)

    def listProfiles(self):
        return self._list_profile_dirs()

    def _read_active_profile_id(self):
        link = self._active_link
        if not link:
            return ""
        if not os.path.islink(link):
            return ""
        try:
            target = _safe_realpath(link)
        except Exception:
            return ""
        base = _safe_realpath(self._profiles_dir)
        if base and target.startswith(base + os.sep):
            return os.path.basename(target)
        return ""

    def _read_active_profile_dir(self) -> str:
        link = self._active_link
        if not link or not os.path.islink(link):
            return ""
        try:
            target = _safe_realpath(link)
        except Exception:
            return ""
        if target and os.path.isdir(target):
            return target
        return ""

    def _load_talkgroup_map(self) -> dict:
        profile_dir = self._read_active_profile_dir()
        if not profile_dir:
            self._tg_map = {}
            self._tg_map_profile = ""
            self._tg_map_mtime = None
            return self._tg_map
        if profile_dir == self._tg_map_profile and self._tg_map_mtime:
            try:
                if os.path.getmtime(self._tg_map_mtime[0]) == self._tg_map_mtime[1]:
                    return self._tg_map
            except Exception:
                pass
        candidates = ["talkgroups.csv", "talkgroups_with_group.csv"]
        path = ""
        for name in candidates:
            candidate = os.path.join(profile_dir, name)
            if os.path.isfile(candidate):
                path = candidate
                break
        if not path:
            self._tg_map = {}
            self._tg_map_profile = profile_dir
            self._tg_map_mtime = None
            return self._tg_map
        try:
            mtime = os.path.getmtime(path)
        except Exception:
            mtime = None
        if self._tg_map_profile == profile_dir and self._tg_map_mtime and self._tg_map_mtime[0] == path:
            if mtime == self._tg_map_mtime[1]:
                return self._tg_map
        tg_map = {}
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if not row:
                        continue
                    row_norm = {str(k or "").strip().lower(): str(v or "").strip() for k, v in row.items()}
                    dec = row_norm.get("dec") or row_norm.get("decimal") or ""
                    if not dec.isdigit():
                        continue
                    alpha = row_norm.get("alpha tag") or row_norm.get("alpha_tag") or ""
                    desc = row_norm.get("description") or ""
                    label = alpha or desc
                    if label:
                        tg_map[dec] = label
        except Exception:
            tg_map = {}
        self._tg_map = tg_map
        self._tg_map_profile = profile_dir
        self._tg_map_mtime = (path, mtime)
        return self._tg_map

    def _load_listen_map(self) -> dict:
        profile_dir = self._read_active_profile_dir()
        if not profile_dir:
            self._listen_map = {}
            self._listen_map_profile = ""
            self._listen_map_mtime = None
            return self._listen_map
        listen_path = os.path.join(profile_dir, _LISTEN_FILENAME)
        if not os.path.isfile(listen_path):
            self._listen_map = {}
            self._listen_map_profile = profile_dir
            self._listen_map_mtime = None
            return self._listen_map
        try:
            mtime = os.path.getmtime(listen_path)
        except Exception:
            mtime = None
        if self._listen_map_profile == profile_dir and self._listen_map_mtime and self._listen_map_mtime[0] == listen_path:
            if mtime == self._listen_map_mtime[1]:
                return self._listen_map
        mapping = {}
        try:
            with open(listen_path, "r", encoding="utf-8", errors="ignore") as f:
                payload = json.load(f) or {}
            if isinstance(payload, dict):
                items = payload.get("items")
                if isinstance(items, dict):
                    mapping = {str(k): bool(v) for k, v in items.items()}
        except Exception:
            mapping = {}
        self._listen_map = mapping
        self._listen_map_profile = profile_dir
        self._listen_map_mtime = (listen_path, mtime)
        return self._listen_map

    def _map_event_label(self, event: dict) -> dict:
        if not event:
            return event
        label = str(event.get("label") or "").strip()
        raw = str(event.get("raw") or "")
        tgid = str(event.get("tgid") or "").strip()
        tg_map = self._load_talkgroup_map()
        if not tg_map:
            return event
        if not tgid:
            if label and (label.isdigit() or _TGID_RE.search(label)):
                tgid = _extract_tgid(label)
            if not tgid and raw:
                tgid = _extract_tgid(raw)
        if tgid:
            listen_map = self._load_listen_map()
            listen = listen_map.get(tgid, True) if listen_map else True
            if not listen:
                event = dict(event)
                event["muted"] = True
                event["tgid"] = tgid
                return event
            event = dict(event)
            event["tgid"] = tgid
            mapped_label = tg_map.get(tgid)
            if mapped_label:
                event["label"] = mapped_label
        return event

    def getProfile(self):
        current = self._read_active_profile_id()
        if current:
            self._profile = current
        return self._profile

    def setProfile(self, profileId: str):
        pid = _normalize_name(profileId)
        if not validate_digital_profile_id(pid):
            self._set_last_error("invalid profileId")
            return False, "invalid profileId"
        base = _safe_realpath(self._profiles_dir)
        target_dir = _safe_realpath(os.path.join(self._profiles_dir, pid))
        if not base or not target_dir.startswith(base + os.sep):
            self._set_last_error("invalid profile path")
            return False, "invalid profile path"
        if not os.path.isdir(target_dir):
            self._set_last_error("unknown profileId")
            return False, "unknown profileId"
        link = self._active_link
        if not link:
            self._set_last_error("active profile link not configured")
            return False, "active profile link not configured"
        link_dir = os.path.dirname(link) or "."
        try:
            os.makedirs(link_dir, exist_ok=True)
        except Exception:
            pass
        if os.path.exists(link) and not os.path.islink(link):
            self._set_last_error("active profile link is not a symlink")
            return False, "active profile link is not a symlink"
        tmp_link = f"{link}.tmp"
        try:
            if os.path.exists(tmp_link):
                os.remove(tmp_link)
            os.symlink(target_dir, tmp_link)
            os.replace(tmp_link, link)
        except Exception as e:
            self._set_last_error(str(e))
            return False, str(e)

        self._profile = pid
        ok, err = self.restart()
        if not ok:
            return False, err or "restart failed"
        self._clear_error()
        return True, ""

    def getLastEvent(self):
        self._refresh_log_cache()
        return super().getLastEvent()

    def getLastError(self):
        self._refresh_log_cache()
        return super().getLastError()

    def getLastWarning(self):
        self._refresh_log_cache()
        return super().getLastWarning()

    def getRecentEvents(self, limit: int = 20):
        self._refresh_log_cache()
        return super().getRecentEvents(limit)


class DigitalManager:
    """Selects and owns exactly one digital adapter."""

    def __init__(self, backend: str | None = None):
        selected = (backend or DIGITAL_BACKEND or "sdrtrunk").strip().lower()
        if not selected:
            selected = "sdrtrunk"
        self._backend = selected
        self._adapter = self._build_adapter(selected)

    @staticmethod
    def _build_adapter(backend: str):
        if backend in ("sdrtrunk",):
            return SdrtrunkAdapter()
        if backend in ("none", "disabled", "off"):
            return NullDigitalAdapter("digital backend disabled")
        return NullDigitalAdapter(f"unknown digital backend: {backend}")

    def backend(self):
        return self._backend

    def start(self):
        return self._adapter.start()

    def stop(self):
        return self._adapter.stop()

    def restart(self):
        return self._adapter.restart()

    def isActive(self):
        return self._adapter.isActive()

    def listProfiles(self):
        return self._adapter.listProfiles()

    def getProfile(self):
        return self._adapter.getProfile()

    def setProfile(self, profileId: str):
        return self._adapter.setProfile(profileId)

    def getLastEvent(self):
        return self._adapter.getLastEvent()

    def getLastError(self):
        return self._adapter.getLastError()

    def getLastWarning(self):
        return self._adapter.getLastWarning()
    def getRecentEvents(self, limit: int = 20):
        return self._adapter.getRecentEvents(limit)
    def preflight(self):
        if hasattr(self._adapter, "preflight"):
            try:
                return self._adapter.preflight()
            except Exception:
                return {"tuner_busy": False, "tuner_busy_lines": []}
        return {"tuner_busy": False, "tuner_busy_lines": []}

    def status_payload(self):
        event = self.getLastEvent() or {}
        label = str(event.get("label") or "")
        mode = event.get("mode")
        time_ms = int(event.get("timeMs") or 0)
        payload = {
            "digital_active": bool(self.isActive()),
            "digital_backend": self.backend(),
            "digital_profile": str(self.getProfile() or ""),
            "digital_muted": bool(get_digital_muted()),
            "digital_last_label": label,
            "digital_last_time": time_ms if time_ms > 0 else 0,
        }
        if mode:
            payload["digital_last_mode"] = str(mode)
        err = self.getLastError()
        if err:
            payload["digital_last_error"] = err
        warn = self.getLastWarning()
        if warn:
            payload["digital_last_warning"] = warn
        preflight = self.preflight() or {}
        payload["digital_tuner_busy_count"] = int(preflight.get("tuner_busy_count") or 0)
        payload["digital_tuner_busy_time"] = int(preflight.get("tuner_busy_last_time_ms") or 0)
        if preflight.get("tuner_busy"):
<<<<<<< ours
<<<<<<< ours
            air_serial = os.getenv("AIRBAND_RTL_SERIAL", "").strip()
            ground_serial = os.getenv("GROUND_RTL_SERIAL", "").strip()
            digital_serial = DIGITAL_RTL_SERIAL or ""
            serials_note = (
                f"expected serials: airband={air_serial or 'unknown'}, "
                f"ground={ground_serial or 'unknown'}, digital={digital_serial or 'unknown'}"
            )
            serial_note = f" (serial {DIGITAL_RTL_SERIAL})" if DIGITAL_RTL_SERIAL else ""
=======
            expected_serial = expected_digital_rtl_serial()
            serial_note = f" (serial {expected_serial})" if expected_serial else ""
>>>>>>> theirs
=======
            serial_note = f" (serial {DIGITAL_RTL_SERIAL})" if DIGITAL_RTL_SERIAL else ""
>>>>>>> theirs
            msg = (
                f"SDRTrunk tuner busy{serial_note}: likely dongle conflict with rtl-airband; "
                f"{serials_note}. In SDRTrunk, disable other RTL tuners and bind to serial {digital_serial or 'your digital dongle'}."
            )
            payload["digital_last_warning"] = msg
        elif not DIGITAL_RTL_SERIAL and DIGITAL_RTL_SERIAL_HINT:
            payload.setdefault("digital_last_warning", DIGITAL_RTL_SERIAL_HINT)
        return payload

    def isMuted(self):
        return get_digital_muted()

    def setMuted(self, muted: bool):
        return set_digital_muted(muted)


_MANAGER = None


def get_digital_manager():
    """Return the singleton DigitalManager."""
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = DigitalManager()
    return _MANAGER
