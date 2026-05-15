"""System unit control via systemd."""
import os
import subprocess
import time
from typing import Optional, Tuple

try:
    from .config import UNITS, BT_HEAL_TIMER_UNIT
except ImportError:
    from ui.config import UNITS, BT_HEAL_TIMER_UNIT


def unit_active(unit: str) -> bool:
    """Check if a systemd unit is currently active."""
    return subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0


def unit_exists(unit: str) -> bool:
    """Check if a systemd unit exists."""
    result = subprocess.run(
        ["systemctl", "show", "-p", "LoadState", "--value", unit],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode != 0:
        return False
    return result.stdout.strip() != "not-found"


def _run_systemctl(args, use_sudo: bool = False):
    cmd = ["systemctl"] + list(args)
    if use_sudo:
        cmd = ["sudo"] + cmd
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def _enabled_from_result(result: subprocess.CompletedProcess[str]) -> tuple[bool, str]:
    if result.returncode != 0:
        return False, (result.stdout or result.stderr or "").strip()
    state = str(result.stdout or "").strip().lower()
    return state in ("enabled", "enabled-runtime"), state


def unit_enabled(unit: str, use_sudo: bool = False) -> bool:
    """Check if a systemd unit is enabled."""
    try:
        result = _run_systemctl(["is-enabled", unit], use_sudo=use_sudo)
    except Exception:
        return False
    enabled, _state = _enabled_from_result(result)
    return enabled


def _restart_unit(unit: str, use_sudo: bool = False) -> Tuple[bool, str]:
    """Restart a systemd unit and return (ok, error)."""
    try:
        result = _run_systemctl(["restart", unit], use_sudo=use_sudo)
    except Exception as e:
        return False, str(e)
    if result.returncode == 0:
        return True, ""
    err = (result.stderr or result.stdout or "").strip()
    if not err:
        err = f"restart failed (code {result.returncode})"
    return False, err


def _start_unit(unit: str, use_sudo: bool = False) -> Tuple[bool, str]:
    """Start a systemd unit and return (ok, error)."""
    try:
        result = _run_systemctl(["start", unit], use_sudo=use_sudo)
    except Exception as e:
        return False, str(e)
    if result.returncode == 0:
        return True, ""
    err = (result.stderr or result.stdout or "").strip()
    if not err:
        err = f"start failed (code {result.returncode})"
    return False, err


def _stop_unit(unit: str, use_sudo: bool = False) -> Tuple[bool, str]:
    """Stop a systemd unit and return (ok, error)."""
    try:
        result = _run_systemctl(["stop", unit], use_sudo=use_sudo)
    except Exception as e:
        return False, str(e)
    if result.returncode == 0:
        return True, ""
    err = (result.stderr or result.stdout or "").strip()
    if not err:
        err = f"stop failed (code {result.returncode})"
    return False, err


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _unit_configured(unit: str) -> bool:
    text = str(unit or "").strip()
    if not text:
        return False
    try:
        return unit_exists(text)
    except Exception:
        return False


def _kill_unit(unit: str) -> None:
    unit = str(unit or "").strip()
    if not unit:
        return
    try:
        _run_systemctl(["kill", "-s", "SIGKILL", unit], use_sudo=True)
    except Exception:
        pass


def _reset_failed_units(units) -> None:
    names = [str(unit or "").strip() for unit in units if str(unit or "").strip()]
    if not names:
        return
    try:
        _run_systemctl(["reset-failed"] + names, use_sudo=True)
    except Exception:
        pass


def _sdrplay_daemon_healthy() -> Tuple[bool, str]:
    """Probe whether the sdrplay daemon can be left alone during op25 restart.

    Healthy = daemon process exists AND no recent sdrplay_apiServ segfault
    in the kernel journal within OP25_SDRPLAY_HEALTH_PROBE_WINDOW_SEC (default
    30s). When healthy, restart_digital() skips the daemon restart — every
    restart-cycle was observed to segfault the daemon, so skipping is a
    correctness win, not just a perf win.

    Returns (healthy, reason). On probe errors, defaults to "unhealthy"
    so the cascade falls through to the existing restart path.
    """
    window_sec = int(_env_float("OP25_SDRPLAY_HEALTH_PROBE_WINDOW_SEC", 30.0, minimum=1.0))

    try:
        pgrep = subprocess.run(
            ["pgrep", "-x", "sdrplay_apiServ"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except Exception as e:
        return False, f"pgrep failed: {e}"
    if pgrep.returncode != 0 or not (pgrep.stdout or "").strip():
        return False, "daemon process not running"

    try:
        jc = subprocess.run(
            ["journalctl", "-k", "--since", f"{window_sec} seconds ago", "--no-pager"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except Exception as e:
        return False, f"journalctl probe failed: {e}"
    if jc.returncode != 0:
        return False, f"journalctl returned {jc.returncode}"
    for line in (jc.stdout or "").splitlines():
        if "sdrplay_apiServ" in line and "segfault" in line:
            return False, f"recent segfault within {window_sec}s"
    return True, f"daemon running, no segfault in {window_sec}s"


def unit_active_enter_epoch(unit: str):
    """Return ActiveEnterTimestampUSec as epoch seconds, or None."""
    def parse_epoch(result):
        if result.returncode != 0:
            return None
        val = (result.stdout or "").strip()
        if not val.isdigit():
            return None
        try:
            return int(val) / 1_000_000.0
        except Exception:
            return None

    def needs_sudo_fallback(result):
        if result.returncode == 0:
            return False
        detail = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
        return (
            "interactive authentication required" in detail
            or "access denied" in detail
            or "permission denied" in detail
        )

    try:
        result = _run_systemctl(["show", "-p", "ActiveEnterTimestampUSec", "--value", unit], use_sudo=False)
        epoch = parse_epoch(result)
        if epoch is not None:
            return epoch
        if not needs_sudo_fallback(result):
            return None
        result = _run_systemctl(["show", "-p", "ActiveEnterTimestampUSec", "--value", unit], use_sudo=True)
        return parse_epoch(result)
    except Exception:
        return None


def restart_rtl() -> Tuple[bool, str]:
    """Restart the rtl-airband scanner."""
    return _restart_unit(UNITS["rtl"], use_sudo=True)


def restart_ground() -> Tuple[bool, str]:
    """Restart the ground scanner."""
    return _restart_unit(UNITS["ground"])


def restart_icecast() -> Tuple[bool, str]:
    """Restart the Icecast service."""
    return _restart_unit(UNITS["icecast"])


def restart_keepalive() -> Tuple[bool, str]:
    """Restart the Icecast keepalive service."""
    return _restart_unit(UNITS["keepalive"])


def restart_ui() -> Tuple[bool, str]:
    """Restart the UI service."""
    return _restart_unit(UNITS["ui"])


def restart_digital(unit: Optional[str] = None) -> Tuple[bool, str]:
    """Recover and restart the OP25 digital backend service."""
    digital_unit = str(unit or UNITS["digital"] or "").strip()
    audio_unit = str(UNITS.get("digital_audio", "") or "").strip()
    audio_exists = _unit_configured(audio_unit)
    sdrplay_unit = (
        os.getenv("UNIT_SDRPLAY")
        or os.getenv("SDRPLAY_SERVICE_NAME")
        or "sdrplay"
    ).strip()
    sdrplay_exists = _unit_configured(sdrplay_unit)
    sdrplay_settle_sec = _env_float("DIGITAL_RESTART_SDRPLAY_SETTLE_SEC", 3.0)
    op25_settle_sec = _env_float("DIGITAL_RESTART_OP25_SETTLE_SEC", 16.0)

    if not digital_unit:
        return False, "digital unit not configured"

    if audio_exists:
        _stop_unit(audio_unit, use_sudo=True)
    _stop_unit(digital_unit, use_sudo=True)
    _kill_unit(digital_unit)
    if audio_exists:
        _kill_unit(audio_unit)
    _reset_failed_units([digital_unit, audio_unit if audio_exists else ""])

    if sdrplay_exists:
        healthy, reason = _sdrplay_daemon_healthy()
        if healthy:
            print(f"restart_digital: sdrplay daemon healthy ({reason}); skipping restart", flush=True)
        else:
            print(f"restart_digital: sdrplay daemon needs restart ({reason})", flush=True)
            ok, err = _restart_unit(sdrplay_unit, use_sudo=True)
            if not ok:
                return False, f"sdrplay restart: {err}"
            if sdrplay_settle_sec > 0:
                time.sleep(sdrplay_settle_sec)

    ok, err = _start_unit(digital_unit, use_sudo=True)
    if not ok:
        return False, f"digital start: {err}"
    if op25_settle_sec > 0:
        time.sleep(op25_settle_sec)

    if audio_exists:
        ok, err = _restart_unit(audio_unit, use_sudo=True)
        if not ok:
            return False, f"digital audio restart: {err}"
    return True, ""


def restart_digital_audio() -> Tuple[bool, str]:
    """Restart the OP25 audio bridge service."""
    return _restart_unit(UNITS["digital_audio"], use_sudo=True)


def set_bt_heal_auto_recovery(enabled: bool) -> Tuple[bool, str]:
    """Enable/disable periodic BT-heal auto-recovery timer."""
    timer_unit = str(BT_HEAL_TIMER_UNIT or "").strip()
    if not timer_unit:
        return False, "BT-heal timer unit not configured"
    args = ["enable", "--now", timer_unit] if enabled else ["disable", "--now", timer_unit]
    try:
        result = _run_systemctl(args, use_sudo=True)
    except Exception as e:
        return False, str(e)
    if result.returncode == 0:
        return True, ""
    err = (result.stderr or result.stdout or "").strip()
    if not err:
        err = f"bt-heal timer update failed (code {result.returncode})"
    return False, err


def reboot_host() -> Tuple[bool, str]:
    """Request a host reboot through systemd."""
    try:
        result = _run_systemctl(["reboot"], use_sudo=True)
    except Exception as e:
        return False, str(e)
    if result.returncode == 0:
        return True, ""
    err = (result.stderr or result.stdout or "").strip()
    if not err:
        err = f"host reboot request failed (code {result.returncode})"
    return False, err


def stop_rtl():
    """Stop the rtl-airband scanner."""
    subprocess.run(
        ["systemctl", "stop", UNITS["rtl"]],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def stop_ground():
    """Stop the ground scanner."""
    subprocess.run(
        ["systemctl", "stop", UNITS["ground"]],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def stop_digital() -> Tuple[bool, str]:
    """Stop the digital backend service."""
    return _stop_unit(UNITS["digital"])


def start_rtl():
    """Start the rtl-airband scanner."""
    subprocess.Popen(
        ["systemctl", "start", "--no-block", UNITS["rtl"]],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def start_ground():
    """Start the ground scanner."""
    subprocess.Popen(
        ["systemctl", "start", "--no-block", UNITS["ground"]],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def start_acars() -> Tuple[bool, str]:
    """Start the ACARS decoder service."""
    return _start_unit(UNITS["acars"], use_sudo=True)


def stop_acars() -> Tuple[bool, str]:
    """Stop the ACARS decoder service."""
    return _stop_unit(UNITS["acars"], use_sudo=True)


def restart_acars() -> Tuple[bool, str]:
    """Restart the ACARS decoder service."""
    return _restart_unit(UNITS["acars"], use_sudo=True)


def start_vdl2() -> Tuple[bool, str]:
    """Start the VDL2 decoder service."""
    return _start_unit(UNITS["vdl2"], use_sudo=True)


def stop_vdl2() -> Tuple[bool, str]:
    """Stop the VDL2 decoder service."""
    return _stop_unit(UNITS["vdl2"], use_sudo=True)


def restart_vdl2() -> Tuple[bool, str]:
    """Restart the VDL2 decoder service."""
    return _restart_unit(UNITS["vdl2"], use_sudo=True)


def start_radiosonde() -> Tuple[bool, str]:
    """Start the radiosonde decoder service."""
    return _start_unit(UNITS["radiosonde"], use_sudo=True)


def stop_radiosonde() -> Tuple[bool, str]:
    """Stop the radiosonde decoder service."""
    return _stop_unit(UNITS["radiosonde"], use_sudo=True)


def restart_radiosonde() -> Tuple[bool, str]:
    """Restart the radiosonde decoder service."""
    return _restart_unit(UNITS["radiosonde"], use_sudo=True)


def start_digital() -> Tuple[bool, str]:
    """Start the digital backend service."""
    return _start_unit(UNITS["digital"])


def ground_control_unit():
    """Determine which unit controls the ground frequency."""
    if unit_active(UNITS["ground"]):
        return "ground"
    if unit_active(UNITS["rtl"]):
        return "rtl"
    return "ground"
