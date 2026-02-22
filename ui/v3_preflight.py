"""V3 hard preflight gates with reason codes."""
from __future__ import annotations

import os
import re
import time
from typing import Any

try:
    from .config import (
        AIRBAND_RTL_SERIAL,
        DIGITAL_PREFERRED_TUNER,
        DIGITAL_PROFILES_DIR,
        DIGITAL_RTL_DEVICE,
        DIGITAL_RTL_SERIAL,
        DIGITAL_RTL_SERIAL_SECONDARY,
        GROUND_RTL_SERIAL,
        GROUND_CONFIG_PATH,
        V3_STRICT_PREFLIGHT,
    )
    from .digital import get_digital_manager
    from .profile_config import read_active_config_path
    from .system_stats import read_rtl_dongle_health
    from .v3_runtime import load_compiled_state
except ImportError:
    from ui.config import (
        AIRBAND_RTL_SERIAL,
        DIGITAL_PREFERRED_TUNER,
        DIGITAL_PROFILES_DIR,
        DIGITAL_RTL_DEVICE,
        DIGITAL_RTL_SERIAL,
        DIGITAL_RTL_SERIAL_SECONDARY,
        GROUND_RTL_SERIAL,
        GROUND_CONFIG_PATH,
        V3_STRICT_PREFLIGHT,
    )
    from ui.digital import get_digital_manager
    from ui.profile_config import read_active_config_path
    from ui.system_stats import read_rtl_dongle_health
    from ui.v3_runtime import load_compiled_state


_CC_RE = re.compile(r"\d+\.\d+")


def _reason(code: str, severity: str, message: str, hint: str = "") -> dict[str, str]:
    out = {
        "code": str(code),
        "severity": str(severity),
        "message": str(message),
    }
    if hint:
        out["hint"] = str(hint)
    return out


def _evaluate_state(reasons: list[dict[str, str]]) -> str:
    if any(r.get("severity") == "critical" for r in reasons):
        return "failed"
    if any(r.get("severity") == "warn" for r in reasons):
        return "degraded"
    return "healthy"


def _control_channels_count(profile_id: str) -> int:
    pid = str(profile_id or "").strip()
    if not pid:
        return 0
    path = os.path.join(str(DIGITAL_PROFILES_DIR or "").strip(), pid, "control_channels.txt")
    if not os.path.isfile(path):
        return 0
    seen: set[int] = set()
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                raw = line.split("#", 1)[0].strip()
                if not raw:
                    continue
                m = _CC_RE.search(raw)
                if not m:
                    continue
                hz = int(round(float(m.group(0)) * 1_000_000))
                if hz > 0:
                    seen.add(hz)
    except Exception:
        return 0
    return len(seen)


def _digital_targets() -> list[str]:
    targets = []
    for item in (
        DIGITAL_PREFERRED_TUNER,
        DIGITAL_RTL_SERIAL,
        DIGITAL_RTL_SERIAL_SECONDARY,
        DIGITAL_RTL_DEVICE,
    ):
        token = str(item or "").strip()
        if token and token not in targets:
            targets.append(token)
    return targets


def _filter_compiler_issues(prefix: str, state: dict[str, Any]) -> list[dict[str, str]]:
    issues = []
    for issue in state.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        code = str(issue.get("code") or "")
        if not code.startswith(prefix):
            continue
        severity = str(issue.get("severity") or "warn")
        msg = str(issue.get("message") or code)
        issues.append(_reason(code, severity, msg))
    return issues


def evaluate_analog_preflight(
    target: str,
    *,
    strict: bool | None = None,
    dongles: dict[str, Any] | None = None,
    compile_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    strict_mode = V3_STRICT_PREFLIGHT if strict is None else bool(strict)
    normalized = "ground" if str(target or "").strip().lower() == "ground" else "airband"
    reasons: list[dict[str, str]] = []

    compile_state = compile_state if isinstance(compile_state, dict) else load_compiled_state()
    reasons.extend(_filter_compiler_issues("ANALOG_", compile_state))

    dongles = dongles if isinstance(dongles, dict) else read_rtl_dongle_health()
    missing = set(dongles.get("missing_expected_serials") or [])
    slow = set(dongles.get("slow_expected_serials") or [])
    expected_serial = GROUND_RTL_SERIAL if normalized == "ground" else AIRBAND_RTL_SERIAL

    if dongles.get("status") == "critical":
        reasons.append(
            _reason(
                "DONGLE_CRITICAL",
                "critical",
                "RTL dongle health is critical",
                "Check USB connections and powered hub before applying changes.",
            )
        )
    elif dongles.get("status") == "degraded":
        reasons.append(_reason("DONGLE_DEGRADED", "warn", "RTL dongle health is degraded"))

    if expected_serial:
        if expected_serial in missing:
            reasons.append(
                _reason(
                    "ANALOG_EXPECTED_SERIAL_MISSING",
                    "critical",
                    f"Expected {normalized} dongle serial missing: {expected_serial}",
                    "Re-seat dongle or verify serial assignment.",
                )
            )
        if expected_serial in slow:
            reasons.append(
                _reason(
                    "ANALOG_EXPECTED_SERIAL_SLOW",
                    "critical",
                    f"Expected {normalized} dongle link speed below threshold: {expected_serial}",
                    "Move dongle to a high-speed USB port.",
                )
            )

    conf_path = os.path.realpath(read_active_config_path()) if normalized == "airband" else os.path.realpath(GROUND_CONFIG_PATH)
    if not conf_path or not os.path.exists(conf_path):
        reasons.append(
            _reason(
                "ANALOG_ACTIVE_CONFIG_MISSING",
                "critical",
                f"Active {normalized} config path missing",
            )
        )

    state = _evaluate_state(reasons)
    blocked = state == "failed"
    return {
        "ok": (not blocked) or (not strict_mode),
        "would_block": bool(blocked),
        "state": state,
        "strict": bool(strict_mode),
        "target": normalized,
        "checked_at": int(time.time()),
        "reasons": reasons,
        "dongles": dongles,
    }


def evaluate_digital_preflight(
    profile_id: str = "",
    *,
    strict: bool | None = None,
    dongles: dict[str, Any] | None = None,
    compile_state: dict[str, Any] | None = None,
    manager_preflight: dict[str, Any] | None = None,
) -> dict[str, Any]:
    strict_mode = V3_STRICT_PREFLIGHT if strict is None else bool(strict)
    reasons: list[dict[str, str]] = []

    compile_state = compile_state if isinstance(compile_state, dict) else load_compiled_state()
    reasons.extend(_filter_compiler_issues("DIGITAL_", compile_state))

    dongles = dongles if isinstance(dongles, dict) else read_rtl_dongle_health()
    missing = set(dongles.get("missing_expected_serials") or [])
    slow = set(dongles.get("slow_expected_serials") or [])

    if dongles.get("status") == "critical":
        reasons.append(
            _reason(
                "DONGLE_CRITICAL",
                "critical",
                "RTL dongle health is critical",
                "Verify all four role-bound dongles are present.",
            )
        )
    elif dongles.get("status") == "degraded":
        reasons.append(_reason("DONGLE_DEGRADED", "warn", "RTL dongle health is degraded"))

    targets = _digital_targets()
    if not targets:
        reasons.append(
            _reason(
                "DIGITAL_TUNER_TARGET_MISSING",
                "critical",
                "No digital tuner target configured",
                "Set DIGITAL_RTL_SERIAL or DIGITAL_PREFERRED_TUNER.",
            )
        )

    if DIGITAL_RTL_SERIAL and DIGITAL_RTL_SERIAL in missing:
        reasons.append(
            _reason(
                "DIGITAL_PRIMARY_SERIAL_MISSING",
                "critical",
                f"Digital primary serial missing: {DIGITAL_RTL_SERIAL}",
            )
        )
    if DIGITAL_RTL_SERIAL_SECONDARY and DIGITAL_RTL_SERIAL_SECONDARY in missing:
        reasons.append(
            _reason(
                "DIGITAL_SECONDARY_SERIAL_MISSING",
                "critical",
                f"Digital secondary serial missing: {DIGITAL_RTL_SERIAL_SECONDARY}",
            )
        )
    if DIGITAL_RTL_SERIAL and DIGITAL_RTL_SERIAL in slow:
        reasons.append(
            _reason(
                "DIGITAL_PRIMARY_SERIAL_SLOW",
                "critical",
                f"Digital primary serial under-speed: {DIGITAL_RTL_SERIAL}",
            )
        )
    if DIGITAL_RTL_SERIAL_SECONDARY and DIGITAL_RTL_SERIAL_SECONDARY in slow:
        reasons.append(
            _reason(
                "DIGITAL_SECONDARY_SERIAL_SLOW",
                "critical",
                f"Digital secondary serial under-speed: {DIGITAL_RTL_SERIAL_SECONDARY}",
            )
        )

    manager_preflight = manager_preflight if isinstance(manager_preflight, dict) else {}
    if not manager_preflight:
        try:
            manager_preflight = get_digital_manager().preflight() or {}
        except Exception as e:
            manager_preflight = {"error": str(e)}

    if manager_preflight.get("error"):
        reasons.append(
            _reason(
                "DIGITAL_PREFLIGHT_ERROR",
                "critical",
                f"Digital preflight failed: {manager_preflight.get('error')}",
            )
        )

    if manager_preflight.get("tuner_busy"):
        reasons.append(
            _reason(
                "DIGITAL_TUNER_BUSY",
                "critical",
                "Digital tuner is currently busy/contended",
                "Resolve tuner contention before start/profile apply.",
            )
        )

    if manager_preflight.get("listen_talkgroup_count") and manager_preflight.get("listen_enabled_count", 0) <= 0:
        reasons.append(
            _reason(
                "DIGITAL_LISTEN_EMPTY",
                "critical",
                "Digital listen set blocks all talkgroups",
                "Enable at least one talkgroup in listen settings.",
            )
        )

    if manager_preflight.get("playlist_source_ok") is False:
        msg = str(manager_preflight.get("playlist_source_error") or "Playlist source not ready")
        reasons.append(_reason("DIGITAL_PLAYLIST_SOURCE_INVALID", "critical", msg))

    pid = str(profile_id or "").strip()
    if pid:
        cc_count = _control_channels_count(pid)
        if cc_count <= 0:
            reasons.append(
                _reason(
                    "DIGITAL_PROFILE_CONTROL_CHANNELS_EMPTY",
                    "critical",
                    f"Digital profile '{pid}' has no control channels",
                )
            )

    state = _evaluate_state(reasons)
    blocked = state == "failed"
    return {
        "ok": (not blocked) or (not strict_mode),
        "would_block": bool(blocked),
        "state": state,
        "strict": bool(strict_mode),
        "checked_at": int(time.time()),
        "profile_id": pid,
        "targets": targets,
        "reasons": reasons,
        "dongles": dongles,
        "digital": manager_preflight,
    }


def gate_action(action: str, *, target: str = "", profile_id: str = "", strict: bool | None = None) -> dict[str, Any]:
    kind = str(action or "").strip().lower()
    if kind in ("apply", "apply_batch", "profile", "filter"):
        return evaluate_analog_preflight(target=target, strict=strict)
    if kind in ("digital_start", "digital_restart", "digital_profile"):
        return evaluate_digital_preflight(profile_id=profile_id, strict=strict)
    return {
        "ok": True,
        "would_block": False,
        "state": "healthy",
        "strict": bool(V3_STRICT_PREFLIGHT if strict is None else strict),
        "checked_at": int(time.time()),
        "reasons": [],
    }
