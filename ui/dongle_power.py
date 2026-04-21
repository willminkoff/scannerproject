"""SDR dongle power control — stops/starts scanner services without touching the UI."""
from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)

try:
    from .config import DONGLE_POWER_STATE_PATH, DONGLE_SCHEDULE_CONFIG_PATH, UNITS, UI_PORT
    from .systemd import _start_unit, _stop_unit, unit_active
except ImportError:
    from ui.config import DONGLE_POWER_STATE_PATH, DONGLE_SCHEDULE_CONFIG_PATH, UNITS, UI_PORT
    from ui.systemd import _start_unit, _stop_unit, unit_active

# SDR dongle-holding services: stop order on power-off.
# digital_audio must stop before digital to release UDP ports cleanly.
_STOP_ORDER = ["digital_audio", "digital", "ground", "rtl", "acars", "vdl2"]

# Core scanner services to restore on power-on.
# WX decoders (acars/vdl2) are omitted — users start them explicitly.
# rtl starts first so icecast mounts populate before digital decoder connects.
_START_ORDER = ["rtl", "ground", "digital", "digital_audio"]

# Services that need sudo for stop/start (match patterns in systemd.py)
_SUDO_KEYS = {"digital", "digital_audio", "acars", "vdl2"}

_CRON_TAG = "# sb3-dongle-schedule"


def _sdr_service_units() -> dict[str, str]:
    return {
        key: UNITS[key]
        for key in ("rtl", "ground", "digital", "digital_audio", "acars", "vdl2")
        if key in UNITS and UNITS.get(key)
    }


def _load_state() -> dict:
    try:
        return json.loads(Path(DONGLE_POWER_STATE_PATH).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: str, set_by: str = "api") -> None:
    payload = {"state": state, "set_at": time.time(), "set_by": set_by}
    try:
        p = Path(DONGLE_POWER_STATE_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.debug("Dongle power state written: %s (by %s)", state, set_by)
    except Exception:
        logger.exception("Failed to write dongle power state to %s", DONGLE_POWER_STATE_PATH)


def get_power_state() -> str:
    """Return 'on', 'off', or 'partial' based on live service status."""
    units = _sdr_service_units()
    active_count = sum(1 for k in _START_ORDER if k in units and unit_active(units[k]))
    if active_count == 0:
        state = "off"
    elif active_count == len(_START_ORDER):
        state = "on"
    else:
        state = "partial"

    # If state file says off but services are back on, a manual restart happened — update the file.
    saved_state = str(_load_state().get("state") or "").strip().lower()
    if saved_state == "off" and state == "on":
        logger.info("Dongle power: services came back on (manual restart?) — updating state file to 'on'")
        _save_state("on", set_by="auto-detected")

    return state


def power_off(set_by: str = "api") -> Tuple[bool, list[str]]:
    """Stop all SDR dongle-consuming services and write state file."""
    logger.info("SDR power OFF requested (by=%s)", set_by)
    units = _sdr_service_units()
    lines: list[str] = []
    failures: list[str] = []

    for key in _STOP_ORDER:
        unit = units.get(key)
        if not unit:
            continue
        if not unit_active(unit):
            logger.debug("SDR power off: %s (%s) already inactive — skip", key, unit)
            continue
        use_sudo = key in _SUDO_KEYS
        logger.info("SDR power off: stopping %s (%s) sudo=%s", key, unit, use_sudo)
        ok, err = _stop_unit(unit, use_sudo=use_sudo)
        if ok:
            lines.append(f"Stopped {unit}.")
            logger.info("SDR power off: stopped %s OK", unit)
        else:
            failures.append(f"{unit}: {err}")
            logger.warning("SDR power off: failed to stop %s: %s", unit, err)

    _save_state("off", set_by=set_by)

    if failures:
        lines.extend(["", "Errors:"] + [f"- {item}" for item in failures])
        logger.warning("SDR power off completed with %d error(s): %s", len(failures), failures)
        return False, lines

    if not lines:
        lines.append("SDR dongles were already off.")
    else:
        lines.append("RTL dongles released.")
    logger.info("SDR power off complete")
    return True, lines


def power_on(set_by: str = "api") -> Tuple[bool, list[str]]:
    """Start the core SDR scanner services and write state file."""
    logger.info("SDR power ON requested (by=%s)", set_by)
    units = _sdr_service_units()
    lines: list[str] = []
    failures: list[str] = []

    for key in _START_ORDER:
        unit = units.get(key)
        if not unit:
            continue
        if unit_active(unit):
            logger.debug("SDR power on: %s (%s) already active — skip", key, unit)
            continue
        use_sudo = key in _SUDO_KEYS
        logger.info("SDR power on: starting %s (%s) sudo=%s", key, unit, use_sudo)
        ok, err = _start_unit(unit, use_sudo=use_sudo)
        if ok:
            lines.append(f"Started {unit}.")
            logger.info("SDR power on: started %s OK", unit)
        else:
            failures.append(f"{unit}: {err}")
            logger.warning("SDR power on: failed to start %s: %s", unit, err)

    _save_state("on", set_by=set_by)

    if failures:
        lines.extend(["", "Errors:"] + [f"- {item}" for item in failures])
        logger.warning("SDR power on completed with %d error(s): %s", len(failures), failures)
        return False, lines

    if not lines:
        lines.append("SDR scanners were already running.")
    else:
        lines.append("SDR scanners started.")
    logger.info("SDR power on complete")
    return True, lines


# ---------------------------------------------------------------------------
# Schedule management
# ---------------------------------------------------------------------------

def load_schedule() -> dict:
    """Return the dongle schedule config (enabled, auto_off, auto_on)."""
    try:
        return json.loads(Path(DONGLE_SCHEDULE_CONFIG_PATH).read_text(encoding="utf-8"))
    except Exception:
        return {"enabled": False, "auto_off": "", "auto_on": ""}


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    """Parse HH:MM → (hour, minute), or None on invalid input."""
    value = str(value or "").strip()
    if ":" not in value:
        return None
    parts = value.split(":", 1)
    try:
        h, m = int(parts[0]), int(parts[1])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, TypeError):
        pass
    return None


def save_schedule(auto_off: str, auto_on: str, enabled: bool) -> Tuple[bool, str]:
    """Persist schedule config file and sync user crontab entries."""
    config: dict = {
        "enabled": bool(enabled),
        "auto_off": str(auto_off or "").strip(),
        "auto_on": str(auto_on or "").strip(),
    }
    try:
        p = Path(DONGLE_SCHEDULE_CONFIG_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(config, indent=2), encoding="utf-8")
    except Exception as e:
        logger.exception("Failed to write dongle schedule config to %s", DONGLE_SCHEDULE_CONFIG_PATH)
        return False, str(e)

    ok, err = _sync_crontab(config)
    if not ok:
        logger.warning("Schedule config saved but crontab sync failed: %s", err)
    return ok, err


def _sync_crontab(config: dict) -> Tuple[bool, str]:
    """Add/replace/remove cron entries tagged with _CRON_TAG."""
    enabled = bool(config.get("enabled"))
    auto_off = str(config.get("auto_off") or "").strip()
    auto_on = str(config.get("auto_on") or "").strip()

    try:
        result = subprocess.run(
            ["crontab", "-l"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        existing = result.stdout if result.returncode == 0 else ""
    except Exception as e:
        logger.exception("crontab -l failed")
        return False, f"crontab -l failed: {e}"

    lines = [line for line in existing.splitlines() if _CRON_TAG not in line]

    if enabled:
        off_t = _parse_hhmm(auto_off)
        on_t = _parse_hhmm(auto_on)
        base_url = f"http://127.0.0.1:{UI_PORT}/api/dongles/power"
        if off_t:
            h, m = off_t
            lines.append(
                f"{m} {h} * * * /usr/bin/curl -s -X POST {base_url} -d 'action=off' > /dev/null 2>&1 {_CRON_TAG}"
            )
        if on_t:
            h, m = on_t
            lines.append(
                f"{m} {h} * * * /usr/bin/curl -s -X POST {base_url} -d 'action=on' > /dev/null 2>&1 {_CRON_TAG}"
            )

    new_cron = "\n".join(lines)
    if new_cron and not new_cron.endswith("\n"):
        new_cron += "\n"

    try:
        proc = subprocess.run(
            ["crontab", "-"],
            input=new_cron,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip() or f"crontab write failed (code {proc.returncode})"
            logger.warning("Failed to update crontab for dongle schedule: %s", err)
            return False, err
    except Exception as e:
        logger.exception("Exception updating crontab for dongle schedule")
        return False, str(e)

    logger.info(
        "Dongle schedule crontab updated: enabled=%s auto_off=%s auto_on=%s",
        enabled, auto_off, auto_on,
    )
    return True, ""
