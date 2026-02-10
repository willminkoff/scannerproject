"""Digital backend integration (live-only, in-memory metadata)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime

try:
    from .config import (
        DIGITAL_ACTIVE_PROFILE_LINK,
        DIGITAL_BACKEND,
        DIGITAL_LOG_PATH,
        DIGITAL_PROFILES_DIR,
        DIGITAL_SERVICE_NAME,
    )
    from .systemd import unit_active
except ImportError:
    from ui.config import (
        DIGITAL_ACTIVE_PROFILE_LINK,
        DIGITAL_BACKEND,
        DIGITAL_LOG_PATH,
        DIGITAL_PROFILES_DIR,
        DIGITAL_SERVICE_NAME,
    )
    from ui.systemd import unit_active


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$")
_MODE_RE = re.compile(r"\b(P25|P25P1|P25P2|DMR|NXDN|D-STAR|TETRA|YSF|EDACS|LTR)\b", re.I)
_LABEL_RE = re.compile(r"\b(label|alias|name|talkgroup|tgid|channel)[=:]\s*([^|]+)", re.I)
_TS_RE = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})[ T](?P<time>\d{2}:\d{2}:\d{2})")
_TS_COMPACT_RE = re.compile(r"(?P<date>\d{8})\s+(?P<time>\d{6})(?:\.\d+)?")
_LOG_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+")
_LOG_PREFIX_COMPACT_RE = re.compile(r"^\d{8}\s+\d{6}(?:\.\d+)?\s+")
_LOG_LEVEL_RE = re.compile(r"^(INFO|WARN|ERROR|DEBUG|TRACE)\s+", re.I)
_MUTE_STATE_PATH = "/run/airband_ui_digital_mute.json"
_DIGITAL_MUTED = False
_DEFAULT_PROFILE_NOTE = (
    "This is a placeholder SDRTrunk profile directory.\n"
    "Export or copy your SDRTrunk configuration into this folder.\n"
    "Then set this profile active from the UI or by updating the active symlink.\n"
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
        return "", ""
    label = ""
    mode = ""
    m_label = _LABEL_RE.search(line)
    if m_label:
        label = m_label.group(2).strip().strip('"')
    m_mode = _MODE_RE.search(line)
    if m_mode:
        mode = m_mode.group(1).upper()
    if not label:
        label = line
    return label, mode


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


class _BaseDigitalAdapter(DigitalAdapter):
    """Shared in-memory state for adapters."""

    def __init__(self):
        self._profile = ""
        self._last_event = None
        self._last_error = ""
        self._last_event_time_ms = 0

    def _set_last_error(self, msg: str):
        self._last_error = (msg or "").strip()

    def _clear_error(self):
        self._last_error = ""

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

    def getLastEvent(self):
        if self._last_event:
            return dict(self._last_event)
        return {"label": "", "timeMs": 0}

    def getLastError(self):
        return self._last_error or None


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
        try:
            stat = os.stat(self._log_path)
            mtime = stat.st_mtime
            size = stat.st_size
        except Exception:
            return
        if self._last_log_mtime == mtime and self._last_log_size == size:
            return
        self._last_log_mtime = mtime
        self._last_log_size = size
        lines = _read_tail_lines(self._log_path)
        if not lines:
            return
        # Last error (best-effort)
        last_err = None
        for line in reversed(lines):
            if re.search(r"(error|exception)", line, re.I):
                last_err = line.strip()
                break
        if last_err:
            self._set_last_error(last_err)

        # Last event (best-effort)
        last_line = ""
        for line in reversed(lines):
            if line.strip():
                last_line = line.strip()
                break
        if not last_line:
            return
        fallback_ms = int(mtime * 1000) if mtime else int(time.time() * 1000)
        time_ms = _parse_time_ms(last_line, fallback_ms)
        label, mode = _extract_label_mode(last_line)
        event = {"label": label, "timeMs": time_ms, "raw": last_line}
        if mode:
            event["mode"] = mode
        self._last_event = event
        self._last_event_time_ms = time_ms

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
