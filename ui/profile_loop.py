"""Background profile-loop scheduler for analog and digital scanners."""
from __future__ import annotations

import datetime
import json
import os
import threading
import time
import urllib.request
from typing import Any

try:
    from .config import (
        AIRBAND_MAX_MHZ,
        AIRBAND_MIN_MHZ,
        DIGITAL_STREAM_MOUNT,
        GROUND_CONFIG_PATH,
        HOLD_STATE_PATH,
        ICECAST_STATUS_URL,
        PLAYER_MOUNT,
        PROFILE_LOOP_STATE_PATH,
        PROFILE_LOOP_TICK_SEC,
        TUNE_BACKUP_PATH,
    )
    from .digital import get_digital_manager
    from .icecast import list_icecast_mounts
    from .profile_config import guess_current_profile, read_active_config_path, split_profiles
    from .scanner import read_hit_list_cached
    from .server_workers import enqueue_action
    from .v3_preflight import gate_action
    from .v3_runtime import set_active_analog_profile
except ImportError:
    from ui.config import (
        AIRBAND_MAX_MHZ,
        AIRBAND_MIN_MHZ,
        DIGITAL_STREAM_MOUNT,
        GROUND_CONFIG_PATH,
        HOLD_STATE_PATH,
        ICECAST_STATUS_URL,
        PLAYER_MOUNT,
        PROFILE_LOOP_STATE_PATH,
        PROFILE_LOOP_TICK_SEC,
        TUNE_BACKUP_PATH,
    )
    from ui.digital import get_digital_manager
    from ui.icecast import list_icecast_mounts
    from ui.profile_config import guess_current_profile, read_active_config_path, split_profiles
    from ui.scanner import read_hit_list_cached
    from ui.server_workers import enqueue_action
    from ui.v3_preflight import gate_action
    from ui.v3_runtime import set_active_analog_profile


_TARGETS = ("airband", "ground", "digital")
_MIN_DWELL_MS = 1_000
_MAX_DWELL_MS = 1_800_000
_MIN_HANG_MS = 1_000
_MAX_HANG_MS = 600_000
_MAX_SELECTED = 128
_DEFAULT_DWELL_MS = {
    "airband": 10_000,
    "ground": 10_000,
    "digital": 4_000,
}
_DEFAULT_HANG_MS = {
    "airband": 2_000,
    "ground": 2_000,
    "digital": 4_000,
}
_LEGACY_DEFAULT_DWELL_MS = {
    "airband": 45_000,
    "ground": 45_000,
    "digital": 30_000,
}
_LEGACY_DEFAULT_HANG_MS = {
    "airband": 12_000,
    "ground": 12_000,
    "digital": 8_000,
}
_PREVIOUS_DEFAULT_DWELL_MS = {
    "airband": 10_000,
    "ground": 10_000,
    "digital": 10_000,
}
_PREVIOUS_DEFAULT_HANG_MS = {
    "airband": 2_000,
    "ground": 2_000,
    "digital": 2_000,
}
_MOUNT_WAIT_SEC = 30.0
_MOUNT_WAIT_POLL_SEC = 1.0
_MOUNT_CACHE_TTL_SEC = 0.75
_MOUNT_FAILURE_GRACE_SEC = 5.0
_MOUNT_STATUS_TIMEOUT_SEC = 1.25
_ANALOG_KEEPALIVE_MOUNT = str(
    os.getenv("PROFILE_LOOP_ANALOG_KEEPALIVE_MOUNT", "keepalive-analog.mp3")
).strip().lstrip("/")
_DIGITAL_KEEPALIVE_MOUNT = str(
    os.getenv("PROFILE_LOOP_DIGITAL_KEEPALIVE_MOUNT", "keepalive-digital.mp3")
).strip().lstrip("/")
_DIGITAL_LOCK_TIMEOUT_MS = max(
    0,
    int(os.getenv("PROFILE_LOOP_DIGITAL_LOCK_TIMEOUT_MS", "20000")),
)
_DIGITAL_SWITCH_FAIL_LIMIT = max(
    1,
    int(os.getenv("PROFILE_LOOP_DIGITAL_SWITCH_FAIL_LIMIT", "3")),
)
_DIGITAL_LOCK_CACHE_TTL_SEC = min(
    5.0,
    max(
        0.25,
        float(os.getenv("PROFILE_LOOP_DIGITAL_LOCK_CACHE_TTL_SEC", "0.9")),
    ),
)


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            payload = json.load(f)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    return {}


def _coerce_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    token = str(raw or "").strip().lower()
    if token in ("1", "true", "yes", "on"):
        return True
    if token in ("0", "false", "no", "off", ""):
        return False
    return False


def _coerce_int(raw: Any, *, minimum: int, maximum: int, fallback: int) -> int:
    try:
        value = int(str(raw).strip())
    except Exception:
        return fallback
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


def _parse_selected_profiles(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        incoming = raw
    else:
        text = str(raw or "").strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                incoming = parsed if isinstance(parsed, list) else [text]
            except Exception:
                incoming = text.replace(";", ",").split(",")
        else:
            incoming = text.replace(";", ",").split(",")
    out: list[str] = []
    seen: set[str] = set()
    for item in incoming:
        value = str(item or "").strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
        if len(out) >= _MAX_SELECTED:
            break
    return out


class ProfileLoopManager:
    """Runs a dwell/hang profile loop for airband, ground, and digital."""

    def __init__(self):
        self._state_path = str(PROFILE_LOOP_STATE_PATH or "").strip()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._targets: dict[str, dict[str, Any]] = {
            target: self._default_target_state(target) for target in _TARGETS
        }
        self._mount_cache_ts = 0.0
        self._mount_success_ts = 0.0
        self._mount_cache: set[str] = set()
        self._digital_lock_cache_ts = 0.0
        self._digital_lock_metric_ready = False
        self._digital_lock_control_locked = False
        self._load_state()
        self._tick()
        self._thread = threading.Thread(
            target=self._loop,
            name="profile-loop-scheduler",
            daemon=True,
        )
        self._thread.start()

    @staticmethod
    def _default_target_state(target: str) -> dict[str, Any]:
        return {
            "enabled": False,
            "selected_profiles": [],
            "dwell_ms": int(_DEFAULT_DWELL_MS[target]),
            "hang_ms": int(_DEFAULT_HANG_MS[target]),
            "pause_on_hit": True,
            "active_profile": "",
            "current_profile": "",
            "next_profile": "",
            "switch_count": 0,
            "switch_fail_count": 0,
            "last_switch_time_ms": 0,
            "switch_reason": "manual",
            "last_error": "",
            "recent_hit": False,
            "in_hit_hold": False,
            "blocked_reason": "",
            "available_profiles": [],
            "bundle_signature": "",
            "pending_restore_profile": "",
            "updated_ms": 0,
        }

    @staticmethod
    def _next_profile(selected_profiles: list[str], current_profile: str) -> str:
        if not selected_profiles:
            return ""
        current = str(current_profile or "").strip()
        if current not in selected_profiles:
            return selected_profiles[0]
        idx = selected_profiles.index(current)
        return selected_profiles[(idx + 1) % len(selected_profiles)]

    @staticmethod
    def _selection_signature(selected_profiles: list[str]) -> str:
        if not selected_profiles:
            return ""
        return "|".join(str(pid or "").strip() for pid in selected_profiles if str(pid or "").strip())

    @staticmethod
    def _safe_hms_age_seconds(value: str) -> float:
        raw = str(value or "").strip()
        if not raw:
            return 999999.0
        try:
            hms = datetime.datetime.strptime(raw, "%H:%M:%S")
        except Exception:
            return 999999.0
        now = datetime.datetime.now()
        candidate = now.replace(
            hour=hms.hour,
            minute=hms.minute,
            second=hms.second,
            microsecond=0,
        )
        delta = (now - candidate).total_seconds()
        if delta < -43200:
            delta += 86400
        elif delta < 0:
            delta = 0
        return delta

    @staticmethod
    def _freq_in_target(freq_text: str, target: str) -> bool:
        try:
            freq = float(str(freq_text or "").strip())
        except Exception:
            return False
        in_airband = AIRBAND_MIN_MHZ <= freq <= AIRBAND_MAX_MHZ
        if target == "airband":
            return in_airband
        if target == "ground":
            return not in_airband
        return False

    def _available_profiles(self, target: str) -> list[dict[str, str]]:
        if target in ("airband", "ground"):
            try:
                _, profiles_airband, profiles_ground = split_profiles()
            except Exception:
                return []
            source = profiles_airband if target == "airband" else profiles_ground
            out = []
            for row in source:
                pid = str(row.get("id") or "").strip()
                if not pid or pid.startswith("none_"):
                    continue
                if not bool(row.get("exists")):
                    continue
                out.append(
                    {
                        "id": pid,
                        "label": str(row.get("label") or pid),
                    }
                )
            return out

        try:
            manager = get_digital_manager()
            profiles = manager.listProfiles()
        except Exception:
            profiles = []
        out = []
        for pid in profiles or []:
            clean = str(pid or "").strip()
            if clean:
                out.append({"id": clean, "label": clean})
        return out

    def _current_profile(self, target: str) -> str:
        if target == "airband":
            try:
                conf_path = read_active_config_path()
                _, profiles_airband, _ = split_profiles()
                tuples = [(p["id"], p["label"], p["path"]) for p in profiles_airband]
                return str(guess_current_profile(conf_path, tuples) or "")
            except Exception:
                return ""
        if target == "ground":
            try:
                conf_path = os.path.realpath(GROUND_CONFIG_PATH)
                _, _, profiles_ground = split_profiles()
                tuples = [(p["id"], p["label"], p["path"]) for p in profiles_ground]
                return str(guess_current_profile(conf_path, tuples) or "")
            except Exception:
                return ""
        try:
            manager = get_digital_manager()
            return str(manager.getProfile() or "")
        except Exception:
            return ""

    def _analog_blocked_reason(self, target: str) -> str:
        hold_payload = _load_json(HOLD_STATE_PATH)
        hold_entry = {}
        if hold_payload.get("active") and hold_payload.get("target") in ("airband", "ground"):
            if str(hold_payload.get("target")) == target:
                hold_entry = hold_payload
        else:
            candidate = hold_payload.get(target)
            if isinstance(candidate, dict):
                hold_entry = candidate
        if hold_entry.get("active"):
            return "hold"

        tune_payload = _load_json(TUNE_BACKUP_PATH)
        if isinstance(tune_payload.get(target), dict):
            return "tune"
        return ""

    def _target_blocked_reason(self, target: str) -> str:
        if not self._target_mount_present(target):
            return "mount_missing"
        if target in ("airband", "ground"):
            return self._analog_blocked_reason(target)
        try:
            manager = get_digital_manager()
            if not manager.isActive():
                return "decoder_stopped"
        except Exception:
            return "decoder_unavailable"
        return ""

    def _target_recent_hit(self, target: str, hang_ms: int) -> bool:
        if hang_ms <= 0:
            return False
        max_age_sec = max(1.0, float(hang_ms) / 1000.0)
        if target in ("airband", "ground"):
            for item in read_hit_list_cached(limit=12):
                if not isinstance(item, dict):
                    continue
                if not self._freq_in_target(item.get("freq"), target):
                    continue
                age = self._safe_hms_age_seconds(item.get("time"))
                if age <= max_age_sec:
                    return True
            return False
        try:
            manager = get_digital_manager()
            candidates = list(manager.getRecentEvents(limit=12) or [])
            if not candidates:
                candidates.append(manager.getLastEvent() or {})
        except Exception:
            return False
        now_ms = int(time.time() * 1000)
        for event in candidates:
            if not isinstance(event, dict):
                continue
            label = str(event.get("label") or "").strip()
            time_ms = int(event.get("timeMs") or 0)
            duration_ms = int(event.get("durationMs") or 0)
            if not label or time_ms <= 0:
                continue

            # Some call-event schemas timestamp the call start. Extend recency by
            # duration when it keeps the computed end near "now".
            effective_ms = time_ms
            if duration_ms > 0:
                candidate_end = time_ms + duration_ms
                if candidate_end <= (now_ms + int(hang_ms)):
                    effective_ms = max(effective_ms, candidate_end)
            if (now_ms - effective_ms) <= int(hang_ms):
                return True
        return False

    def _fetch_mount_status_text(self) -> str:
        try:
            with urllib.request.urlopen(ICECAST_STATUS_URL, timeout=_MOUNT_STATUS_TIMEOUT_SEC) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except Exception:
            return ""

    def _normalized_mounts(self, *, force_refresh: bool = False) -> set[str]:
        now = time.monotonic()
        if not force_refresh and self._mount_cache_ts > 0 and (now - float(self._mount_cache_ts)) <= _MOUNT_CACHE_TTL_SEC:
            return set(self._mount_cache)
        mounts: set[str] = set()
        try:
            status_text = self._fetch_mount_status_text()
            if status_text:
                for mount in list_icecast_mounts(status_text):
                    raw = str(mount or "").strip()
                    if not raw:
                        continue
                    if not raw.startswith("/"):
                        raw = f"/{raw}"
                    mounts.add(raw)
                self._mount_success_ts = now
        except Exception:
            mounts = set()
        if (
            not force_refresh
            and not mounts
            and self._mount_cache
            and (now - float(self._mount_success_ts)) <= _MOUNT_FAILURE_GRACE_SEC
        ):
            self._mount_cache_ts = now
            return set(self._mount_cache)
        self._mount_cache_ts = now
        self._mount_cache = set(mounts)
        return set(mounts)

    def _expected_mount_for_target(self, target: str) -> str:
        if target == "digital":
            mount = str(DIGITAL_STREAM_MOUNT or "").strip().lstrip("/")
        else:
            mount = str(PLAYER_MOUNT or "").strip().lstrip("/")
        if not mount:
            return ""
        return f"/{mount}"

    def _fallback_mounts_for_target(self, target: str) -> set[str]:
        out: set[str] = set()
        if target == "digital":
            if _DIGITAL_KEEPALIVE_MOUNT:
                out.add(f"/{_DIGITAL_KEEPALIVE_MOUNT}")
        else:
            if _ANALOG_KEEPALIVE_MOUNT:
                out.add(f"/{_ANALOG_KEEPALIVE_MOUNT}")
        return out

    def _target_mount_present(self, target: str, *, force_refresh: bool = False) -> bool:
        expected = self._expected_mount_for_target(target)
        if not expected:
            return True
        mounts = self._normalized_mounts(force_refresh=force_refresh)
        if expected in mounts:
            return True
        for fallback in self._fallback_mounts_for_target(target):
            if fallback in mounts:
                return True
        return False

    def _wait_for_mount_after_switch(self, target: str) -> bool:
        expected = self._expected_mount_for_target(target)
        if not expected:
            return True
        deadline = time.monotonic() + _MOUNT_WAIT_SEC
        while time.monotonic() <= deadline:
            if self._target_mount_present(target, force_refresh=True):
                return True
            time.sleep(_MOUNT_WAIT_POLL_SEC)
        return False

    def _digital_control_lock_state(self, *, force_refresh: bool = False) -> tuple[bool, bool]:
        now = time.monotonic()
        if (
            not force_refresh
            and self._digital_lock_cache_ts > 0
            and (now - float(self._digital_lock_cache_ts)) <= _DIGITAL_LOCK_CACHE_TTL_SEC
        ):
            return bool(self._digital_lock_metric_ready), bool(self._digital_lock_control_locked)
        metric_ready = False
        control_locked = False
        try:
            preflight = get_digital_manager().preflight() or {}
            metric_ready = bool(preflight.get("control_decode_available"))
            control_locked = bool(preflight.get("control_channel_locked"))
        except Exception:
            metric_ready = False
            control_locked = False
        self._digital_lock_cache_ts = now
        self._digital_lock_metric_ready = metric_ready
        self._digital_lock_control_locked = control_locked
        return metric_ready, control_locked

    @staticmethod
    def _gate_reason_text(gate_payload: Any) -> str:
        if not isinstance(gate_payload, dict):
            return ""
        reasons = gate_payload.get("reasons")
        if isinstance(reasons, list):
            for item in reasons:
                if not isinstance(item, dict):
                    continue
                code = str(item.get("code") or "").strip()
                message = str(item.get("message") or "").strip()
                if code and message:
                    return f"{code}: {message}"
                if message:
                    return message
                if code:
                    return code
        return str(gate_payload.get("state") or "").strip()

    def _preflight_allows_switch(self, target: str, profile_id: str) -> tuple[bool, str]:
        try:
            if target in ("airband", "ground"):
                gate = gate_action("profile", target=target)
                if bool((gate or {}).get("ok")):
                    return True, ""
                reason = self._gate_reason_text(gate)
                return False, f"preflight blocked: {reason or 'failed'}"

            # Keep profile-loop switching fast: avoid full digital preflight
            # (control-lock/tail parsing) on every dwell transition.
            manager = get_digital_manager()
            if not manager.isActive():
                return False, "preflight blocked: decoder_stopped"
            return True, ""
        except Exception as e:
            return False, f"preflight check failed: {e}"

    def _apply_profile(self, target: str, profile_id: str) -> tuple[bool, str]:
        pid = str(profile_id or "").strip()
        if not pid:
            return False, "missing profile"
        preflight_ok, preflight_error = self._preflight_allows_switch(target, pid)
        if not preflight_ok:
            return False, preflight_error
        if target in ("airband", "ground"):
            with self._lock:
                state = dict(self._targets.get(target) or {})
            loop_enabled = bool(state.get("enabled"))
            if loop_enabled:
                result = enqueue_action(
                    {
                        "type": "profile_loop_bundle",
                        "target": target,
                        "selected_profiles": list(state.get("selected_profiles") or []),
                        "active_profile": pid,
                    }
                )
            else:
                result = enqueue_action(
                    {
                        "type": "profile",
                        "profile": pid,
                        "target": target,
                        "restart_service": True,
                    }
                )
            status = int(result.get("status") or 500)
            payload = result.get("payload") or {}
            if status >= 300 or not payload.get("ok"):
                err = str(payload.get("error") or payload.get("restart_error") or "").strip()
                return False, err or f"profile switch failed ({status})"
            if not self._wait_for_mount_after_switch(target):
                return False, f"mount did not recover after switch: {self._expected_mount_for_target(target)}"
            if not loop_enabled:
                try:
                    set_active_analog_profile(target, pid)
                except Exception:
                    pass
            return True, ""
        try:
            ok, err = get_digital_manager().setProfile(pid, restart_service=False)
        except Exception as e:
            return False, str(e)
        if not ok:
            return False, str(err or "digital profile switch failed")
        # Force fresh lock metrics for the new profile on the next scheduler tick.
        self._digital_lock_cache_ts = 0.0
        self._digital_lock_metric_ready = False
        self._digital_lock_control_locked = False
        # Speed-first loop behavior: skip mount-validation waits and canonical
        # compile updates on each digital dwell transition.
        return True, ""

    def _load_state(self) -> None:
        path = self._state_path
        if not path or not os.path.isfile(path):
            return
        payload = _load_json(path)
        by_target = payload.get("targets")
        if not isinstance(by_target, dict):
            return
        for target in _TARGETS:
            raw = by_target.get(target)
            if not isinstance(raw, dict):
                continue
            state = self._targets[target]
            state["enabled"] = bool(raw.get("enabled"))
            selected = _parse_selected_profiles(raw.get("selected_profiles"))
            if selected is not None:
                state["selected_profiles"] = selected
            state["dwell_ms"] = _coerce_int(
                raw.get("dwell_ms"),
                minimum=_MIN_DWELL_MS,
                maximum=_MAX_DWELL_MS,
                fallback=int(state["dwell_ms"]),
            )
            state["hang_ms"] = _coerce_int(
                raw.get("hang_ms"),
                minimum=_MIN_HANG_MS,
                maximum=_MAX_HANG_MS,
                fallback=int(state["hang_ms"]),
            )
            if "pause_on_hit" in raw:
                state["pause_on_hit"] = _coerce_bool(raw.get("pause_on_hit"))
            # Migrate previously shipped high defaults to faster loop timing.
            if int(state["dwell_ms"]) == int(_LEGACY_DEFAULT_DWELL_MS[target]):
                state["dwell_ms"] = int(_DEFAULT_DWELL_MS[target])
            if int(state["hang_ms"]) == int(_LEGACY_DEFAULT_HANG_MS[target]):
                state["hang_ms"] = int(_DEFAULT_HANG_MS[target])
            # Migrate the first fast defaults for digital to the current tuned values.
            if int(state["dwell_ms"]) == int(_PREVIOUS_DEFAULT_DWELL_MS[target]):
                state["dwell_ms"] = int(_DEFAULT_DWELL_MS[target])
            if int(state["hang_ms"]) == int(_PREVIOUS_DEFAULT_HANG_MS[target]):
                state["hang_ms"] = int(_DEFAULT_HANG_MS[target])
        try:
            self._save_state_locked()
        except Exception:
            pass

    def _save_state_locked(self) -> None:
        path = self._state_path
        if not path:
            return
        payload = {
            "targets": {
                target: {
                    "enabled": bool(state.get("enabled")),
                    "selected_profiles": list(state.get("selected_profiles") or []),
                    "dwell_ms": int(state.get("dwell_ms") or _DEFAULT_DWELL_MS[target]),
                    "hang_ms": int(state.get("hang_ms") or _DEFAULT_HANG_MS[target]),
                    "pause_on_hit": bool(state.get("pause_on_hit")),
                }
                for target, state in self._targets.items()
            },
            "updated_ms": int(time.time() * 1000),
        }
        tmp = f"{path}.tmp"
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp, path)
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    def _snapshot_locked(self) -> dict[str, Any]:
        targets = {}
        for target, state in self._targets.items():
            targets[target] = {
                "enabled": bool(state.get("enabled")),
                "selected_profiles": list(state.get("selected_profiles") or []),
                "dwell_ms": int(state.get("dwell_ms") or _DEFAULT_DWELL_MS[target]),
                "hang_ms": int(state.get("hang_ms") or _DEFAULT_HANG_MS[target]),
                "pause_on_hit": bool(state.get("pause_on_hit")),
                "available_profiles": list(state.get("available_profiles") or []),
                "current_profile": str(state.get("current_profile") or ""),
                "active_profile": str(state.get("active_profile") or ""),
                "next_profile": str(state.get("next_profile") or ""),
                "switch_count": int(state.get("switch_count") or 0),
                "switch_fail_count": int(state.get("switch_fail_count") or 0),
                "last_switch_time_ms": int(state.get("last_switch_time_ms") or 0),
                "switch_reason": str(state.get("switch_reason") or ""),
                "last_error": str(state.get("last_error") or ""),
                "recent_hit": bool(state.get("recent_hit")),
                "in_hit_hold": bool(state.get("in_hit_hold")),
                "blocked_reason": str(state.get("blocked_reason") or ""),
                "updated_ms": int(state.get("updated_ms") or 0),
            }
        return {
            "targets": targets,
            "updated_ms": int(time.time() * 1000),
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def set_target_config(self, target: str, payload: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        name = str(target or "").strip().lower()
        if name not in _TARGETS:
            return False, "unknown target", {}
        if not isinstance(payload, dict):
            return False, "invalid payload", {}

        with self._lock:
            state = self._targets[name]
            was_enabled = bool(state.get("enabled"))
            available = self._available_profiles(name)
            available_ids = {str(item.get("id") or "").strip() for item in available}
            state["available_profiles"] = available

            selected_raw = _parse_selected_profiles(payload.get("selected_profiles"))
            selected_changed = False
            if selected_raw is not None:
                next_selected = [pid for pid in selected_raw if pid in available_ids]
                selected_changed = next_selected != list(state.get("selected_profiles") or [])
                state["selected_profiles"] = next_selected

            if "dwell_ms" in payload:
                state["dwell_ms"] = _coerce_int(
                    payload.get("dwell_ms"),
                    minimum=_MIN_DWELL_MS,
                    maximum=_MAX_DWELL_MS,
                    fallback=int(state.get("dwell_ms") or _DEFAULT_DWELL_MS[name]),
                )
            if "hang_ms" in payload:
                state["hang_ms"] = _coerce_int(
                    payload.get("hang_ms"),
                    minimum=_MIN_HANG_MS,
                    maximum=_MAX_HANG_MS,
                    fallback=int(state.get("hang_ms") or _DEFAULT_HANG_MS[name]),
                )
            if "pause_on_hit" in payload:
                state["pause_on_hit"] = _coerce_bool(payload.get("pause_on_hit"))
            if "enabled" in payload:
                state["enabled"] = _coerce_bool(payload.get("enabled"))

            if name in ("airband", "ground"):
                if selected_changed:
                    state["bundle_signature"] = ""
                if state["enabled"] and not was_enabled:
                    previous_profile = str(self._current_profile(name) or "").strip()
                    state["pending_restore_profile"] = previous_profile
                    state["bundle_signature"] = ""
                if (not state["enabled"]) and was_enabled:
                    state["bundle_signature"] = ""
                    if not str(state.get("pending_restore_profile") or "").strip():
                        fallback_restore = str(
                            state.get("active_profile")
                            or state.get("current_profile")
                            or self._current_profile(name)
                            or ""
                        ).strip()
                        state["pending_restore_profile"] = fallback_restore

            if state["enabled"] and len(state["selected_profiles"]) < 2:
                return False, "profile loop requires at least 2 selected profiles", self._snapshot_locked()

            if not state["enabled"]:
                state["in_hit_hold"] = False

            digital_start_from_first = (
                name == "digital"
                and bool(state.get("enabled"))
                and bool(state.get("selected_profiles"))
                and (selected_changed or not was_enabled)
            )

            state["last_error"] = ""
            state["switch_reason"] = "manual"
            if selected_changed or bool(state.get("enabled")) != was_enabled:
                state["switch_count"] = 0
                state["switch_fail_count"] = 0
            state["updated_ms"] = int(time.time() * 1000)
            if state["enabled"] and not int(state.get("last_switch_time_ms") or 0):
                state["last_switch_time_ms"] = int(time.time() * 1000)
            if state.get("active_profile") and state["active_profile"] not in state["selected_profiles"]:
                state["active_profile"] = ""
            if digital_start_from_first:
                first_selected = str((state.get("selected_profiles") or [""])[0] or "").strip()
                if first_selected:
                    # On enable/selection changes, snap scheduling to the first selected
                    # profile and force an immediate align on the next tick.
                    state["active_profile"] = first_selected
                    state["current_profile"] = ""
                    state["recent_hit"] = False
                    state["in_hit_hold"] = False
                    state["last_switch_time_ms"] = 0
                    state["switch_reason"] = "align"
            state["next_profile"] = self._next_profile(
                list(state.get("selected_profiles") or []),
                str(state.get("active_profile") or state.get("current_profile") or ""),
            )
            self._save_state_locked()
            snap = self._snapshot_locked()
        return True, "", snap

    def _prepare_switch(self, target: str, now_ms: int) -> tuple[str, str] | None:
        state = self._targets[target]
        available = self._available_profiles(target)
        available_ids = [str(item.get("id") or "").strip() for item in available if str(item.get("id") or "").strip()]
        available_set = set(available_ids)
        selected = [pid for pid in list(state.get("selected_profiles") or []) if pid in available_set]
        if selected != list(state.get("selected_profiles") or []):
            state["selected_profiles"] = selected
            if state.get("enabled") and len(selected) < 2:
                state["enabled"] = False
                state["last_error"] = "profile loop requires at least 2 selected profiles"
                state["switch_reason"] = "selection_too_small"

        current_profile = self._current_profile(target)
        active_profile = str(state.get("active_profile") or "")
        if active_profile and active_profile not in selected:
            active_profile = ""

        digital_enabled = target == "digital" and bool(state.get("enabled"))
        if digital_enabled:
            # Keep digital loop anchored to selected order while enabled.
            if not active_profile and selected:
                active_profile = selected[0]
                if not int(state.get("last_switch_time_ms") or 0):
                    state["last_switch_time_ms"] = now_ms
        else:
            if current_profile in selected and current_profile:
                if active_profile != current_profile:
                    active_profile = current_profile
                    state["last_switch_time_ms"] = now_ms
                    state["switch_reason"] = "manual"
                    state["last_error"] = ""
            if not active_profile and selected:
                active_profile = current_profile if current_profile in selected else selected[0]
                if not int(state.get("last_switch_time_ms") or 0):
                    state["last_switch_time_ms"] = now_ms

        state["available_profiles"] = available
        state["current_profile"] = current_profile
        state["active_profile"] = active_profile
        state["next_profile"] = self._next_profile(selected, active_profile)
        state["updated_ms"] = now_ms

        if not state.get("enabled"):
            state["blocked_reason"] = ""
            state["recent_hit"] = False
            state["in_hit_hold"] = False
            if target in ("airband", "ground"):
                restore_profile = str(state.get("pending_restore_profile") or "").strip()
                if restore_profile:
                    if restore_profile == current_profile:
                        state["pending_restore_profile"] = ""
                    elif restore_profile in available_set:
                        return restore_profile, "disable_restore"
            return None
        state["blocked_reason"] = self._target_blocked_reason(target)
        state["recent_hit"] = self._target_recent_hit(target, int(state.get("hang_ms") or 0))
        if len(selected) < 2:
            state["in_hit_hold"] = False
            state["enabled"] = False
            state["last_error"] = "profile loop requires at least 2 selected profiles"
            state["switch_reason"] = "selection_too_small"
            return None
        if state["blocked_reason"]:
            state["in_hit_hold"] = False
            state["switch_reason"] = state["blocked_reason"]
            return None
        if not active_profile:
            return None

        if target in ("airband", "ground"):
            selection_sig = self._selection_signature(selected)
            applied_sig = str(state.get("bundle_signature") or "")
            if selection_sig != applied_sig:
                desired = active_profile if active_profile in selected else selected[0]
                state["active_profile"] = desired
                state["next_profile"] = self._next_profile(selected, desired)
                return desired, "bundle_sync"
            state["switch_reason"] = "bundle"
            state["in_hit_hold"] = False
            return None

        if current_profile != active_profile:
            if current_profile in selected and current_profile:
                # If runtime already moved to a selected profile, trust it and
                # re-anchor scheduling to avoid align ping-pong.
                state["active_profile"] = current_profile
                state["next_profile"] = self._next_profile(selected, current_profile)
                state["last_switch_time_ms"] = now_ms
                state["switch_reason"] = "manual"
                active_profile = current_profile
            else:
                return active_profile, "align"

        elapsed = now_ms - int(state.get("last_switch_time_ms") or 0)
        if elapsed < 0:
            elapsed = 0
        dwell_ms = int(state.get("dwell_ms") or _DEFAULT_DWELL_MS[target])
        _metric_ready, control_locked = self._digital_control_lock_state()
        wait_for_lock_ms = max(int(_DIGITAL_LOCK_TIMEOUT_MS), dwell_ms)
        if int(_DIGITAL_LOCK_TIMEOUT_MS) > 0 and not control_locked:
            # Hold on a profile while the control channel is still being acquired.
            # This applies even when decode metrics are temporarily unavailable.
            if elapsed < wait_for_lock_ms:
                state["in_hit_hold"] = False
                state["switch_reason"] = "acquiring_lock"
                return None
            next_profile = self._next_profile(selected, active_profile)
            if next_profile and next_profile != active_profile:
                return next_profile, "lock_timeout"
        if control_locked and str(state.get("switch_reason") or "") == "acquiring_lock":
            # Start dwell timing from lock acquisition, not from profile switch.
            state["last_switch_time_ms"] = now_ms
            elapsed = 0
            state["switch_reason"] = "lock_acquired"

        if state.get("pause_on_hit") and state["recent_hit"]:
            state["in_hit_hold"] = True
            state["switch_reason"] = "hit_hold"
            return None
        if state.get("in_hit_hold") and not state["recent_hit"]:
            state["in_hit_hold"] = False

        if elapsed < dwell_ms:
            return None
        next_profile = self._next_profile(selected, active_profile)
        if not next_profile or next_profile == active_profile:
            return None
        return next_profile, "dwell"

    def _tick(self) -> None:
        for target in _TARGETS:
            now_ms = int(time.time() * 1000)
            with self._lock:
                prepared = self._prepare_switch(target, now_ms)
            if not prepared:
                continue
            next_profile, reason = prepared
            ok, err = self._apply_profile(target, next_profile)
            with self._lock:
                state = self._targets[target]
                state["updated_ms"] = int(time.time() * 1000)
                if ok:
                    state["switch_fail_count"] = 0
                    selected_now = list(state.get("selected_profiles") or [])
                    if target in ("airband", "ground") and reason == "bundle_sync":
                        desired = next_profile if next_profile in selected_now else (selected_now[0] if selected_now else next_profile)
                        state["active_profile"] = desired
                        state["current_profile"] = desired
                        state["last_switch_time_ms"] = int(time.time() * 1000)
                        state["switch_count"] = int(state.get("switch_count") or 0) + 1
                        state["switch_reason"] = "bundle"
                        state["last_error"] = ""
                        state["bundle_signature"] = self._selection_signature(selected_now)
                        state["next_profile"] = self._next_profile(selected_now, desired)
                    else:
                        state["active_profile"] = next_profile
                        state["current_profile"] = next_profile
                        state["last_switch_time_ms"] = int(time.time() * 1000)
                        state["switch_count"] = int(state.get("switch_count") or 0) + 1
                        state["switch_reason"] = reason
                        state["last_error"] = ""
                        state["next_profile"] = self._next_profile(selected_now, next_profile)
                        if target in ("airband", "ground") and reason == "disable_restore":
                            state["pending_restore_profile"] = ""
                            state["bundle_signature"] = ""
                else:
                    state["last_switch_time_ms"] = int(time.time() * 1000)
                    fail_count = int(state.get("switch_fail_count") or 0) + 1
                    state["switch_fail_count"] = fail_count
                    state["switch_reason"] = "error"
                    state["last_error"] = str(err or "profile switch failed")
                    if target == "digital" and fail_count >= int(_DIGITAL_SWITCH_FAIL_LIMIT):
                        state["enabled"] = False
                        state["in_hit_hold"] = False
                        state["switch_reason"] = "switch_fail_limit"
                        state["last_error"] = (
                            f"digital loop stopped after {fail_count} consecutive profile switch failures. "
                            "Go back to Standard/Tune mode and confirm LOCKED status before re-enabling loop."
                        )

    def _loop(self) -> None:
        while not self._stop.wait(PROFILE_LOOP_TICK_SEC):
            try:
                self._tick()
            except Exception:
                continue

    def __del__(self):
        try:
            self._stop.set()
        except Exception:
            pass


_PROFILE_LOOP_MANAGER: ProfileLoopManager | None = None
_PROFILE_LOOP_LOCK = threading.Lock()


def get_profile_loop_manager() -> ProfileLoopManager:
    """Get the singleton profile-loop manager."""
    global _PROFILE_LOOP_MANAGER
    if _PROFILE_LOOP_MANAGER is not None:
        return _PROFILE_LOOP_MANAGER
    with _PROFILE_LOOP_LOCK:
        if _PROFILE_LOOP_MANAGER is None:
            _PROFILE_LOOP_MANAGER = ProfileLoopManager()
    return _PROFILE_LOOP_MANAGER
