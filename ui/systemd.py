"""System unit control via systemd."""
import subprocess
from typing import Tuple

try:
    from .config import UNITS, BT_HEAL_TIMER_UNIT, DIGITAL_BACKEND
except ImportError:
    from ui.config import UNITS, BT_HEAL_TIMER_UNIT, DIGITAL_BACKEND


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


def _is_op25_backend() -> bool:
    return str(DIGITAL_BACKEND or "").strip().lower() == "op25"


def _op25_audio_unit() -> str:
    return str(UNITS.get("op25_audio") or "scanner-digital-op25-audio").strip() or "scanner-digital-op25-audio"


def _combine_results(results: list[Tuple[bool, str]]) -> Tuple[bool, str]:
    ok = all(item[0] for item in results)
    err = "; ".join(message for _ok, message in results if message)
    return ok, err


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

    try:
        result = _run_systemctl(["show", "-p", "ActiveEnterTimestampUSec", "--value", unit], use_sudo=False)
        epoch = parse_epoch(result)
        if epoch is not None:
            return epoch
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


def restart_digital() -> Tuple[bool, str]:
    """Restart the digital backend service."""
    results = [_restart_unit(UNITS["digital"], use_sudo=True)]
    if _is_op25_backend() and results[0][0]:
        results.append(_restart_unit(_op25_audio_unit(), use_sudo=True))
    return _combine_results(results)


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
    results: list[Tuple[bool, str]] = []
    if _is_op25_backend():
        results.append(_stop_unit(_op25_audio_unit()))
    results.append(_stop_unit(UNITS["digital"]))
    return _combine_results(results)


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
    results: list[Tuple[bool, str]] = [_start_unit(UNITS["digital"])]
    if _is_op25_backend() and results[0][0]:
        results.append(_start_unit(_op25_audio_unit()))
    return _combine_results(results)


def ground_control_unit():
    """Determine which unit controls the ground frequency."""
    if unit_active(UNITS["ground"]):
        return "ground"
    if unit_active(UNITS["rtl"]):
        return "rtl"
    return "ground"
