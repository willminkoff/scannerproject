"""System unit control via systemd."""
import subprocess
from typing import Tuple

try:
    from .config import UNITS
except ImportError:
    from ui.config import UNITS


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
    return _restart_unit(UNITS["digital"], use_sudo=True)


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
