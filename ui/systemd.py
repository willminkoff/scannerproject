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


def _restart_unit(unit: str) -> Tuple[bool, str]:
    """Restart a systemd unit and return (ok, error)."""
    try:
        result = subprocess.run(
            ["systemctl", "restart", unit],
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
        err = f"restart failed (code {result.returncode})"
    return False, err


def unit_active_enter_epoch(unit: str):
    """Return ActiveEnterTimestampUSec as epoch seconds, or None."""
    try:
        result = subprocess.run(
            ["systemctl", "show", "-p", "ActiveEnterTimestampUSec", "--value", unit],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    val = (result.stdout or "").strip()
    if not val.isdigit():
        return None
    try:
        return int(val) / 1_000_000.0
    except Exception:
        return None


def restart_rtl() -> Tuple[bool, str]:
    """Restart the rtl-airband scanner."""
    return _restart_unit(UNITS["rtl"])


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


def ground_control_unit():
    """Determine which unit controls the ground frequency."""
    if unit_active(UNITS["ground"]):
        return "ground"
    if unit_active(UNITS["rtl"]):
        return "rtl"
    return "ground"
