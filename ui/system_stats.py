"""System stats helpers for host/PC health telemetry."""
from collections import deque
import json
import logging
import os
import re
import time
import shutil
import subprocess
import socket
import threading

try:
    from .config import BT_HEAL_DEFAULT_ENABLED, BT_HEAL_SERVICE_UNIT, BT_HEAL_TIMER_UNIT
except ImportError:
    from ui.config import BT_HEAL_DEFAULT_ENABLED, BT_HEAL_SERVICE_UNIT, BT_HEAL_TIMER_UNIT

logger = logging.getLogger(__name__)

_last_cpu = {"total": None, "idle": None, "ts": None}
_RTL_USB_SYSFS_ROOT = os.getenv("RTL_USB_SYSFS_ROOT", "/sys/bus/usb/devices")
_RTL_USB_VENDOR = os.getenv("RTL_USB_VENDOR", "0bda").strip().lower()
_RTL_USB_PRODUCTS = {p.strip().lower() for p in os.getenv("RTL_USB_PRODUCT", "2838,2832").split(",") if p.strip()}
try:
    _RTL_MIN_USB_SPEED_MBPS = max(1, int(os.getenv("RTL_MIN_USB_SPEED_MBPS", "480")))
except Exception:
    _RTL_MIN_USB_SPEED_MBPS = 480

try:
    _RTL_DONGLE_TARGET = max(1, int(os.getenv("RTL_DONGLE_TARGET", "4")))
except Exception:
    _RTL_DONGLE_TARGET = 4
try:
    _RTL_DONGLE_EVENT_HISTORY_LIMIT = max(1, int(os.getenv("RTL_DONGLE_EVENT_HISTORY_LIMIT", "40")))
except Exception:
    _RTL_DONGLE_EVENT_HISTORY_LIMIT = 40
_RTL_ALLOW_EXTRA = str(os.getenv("RTL_ALLOW_EXTRA", "1")).strip().lower() in ("1", "true", "yes", "on")


def _default_rtl_dongle_event_log_path() -> str:
    cache_root = str(os.getenv("XDG_CACHE_HOME", "") or "").strip()
    if cache_root:
        return os.path.join(cache_root, "airband-ui", "dongle-events.jsonl")
    home = os.path.expanduser("~")
    if home and home != "~":
        return os.path.join(home, ".cache", "airband-ui", "dongle-events.jsonl")
    return "/tmp/airband_ui_dongle_events.jsonl"


_RTL_DONGLE_EVENT_LOG_PATH = os.getenv(
    "RTL_DONGLE_EVENT_LOG_PATH",
    _default_rtl_dongle_event_log_path(),
).strip()
_RTL_DONGLE_EVENT_LOCK = threading.Lock()
_RTL_DONGLE_EVENT_STATE = {
    "initialized": False,
    "present_serials": [],
    "details_by_serial": {},
    "events": [],
}


def _systemctl_capture(args: list[str]):
    try:
        return subprocess.run(
            ["systemctl", *list(args or [])],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except Exception:
        logger.debug("system_stats: systemctl capture failed for args=%s", args, exc_info=True)
        return None


def _systemctl_show_value(unit: str, key: str) -> str:
    target = str(unit or "").strip()
    prop = str(key or "").strip()
    if not target or not prop:
        return ""
    result = _systemctl_capture(["show", "-p", prop, "--value", target])
    if result is None or result.returncode != 0:
        return ""
    return str(result.stdout or "").strip()


def _systemctl_is_enabled_state(unit: str) -> str:
    target = str(unit or "").strip()
    if not target:
        return ""
    result = _systemctl_capture(["is-enabled", target])
    if result is None:
        return ""
    output = str(result.stdout or result.stderr or "").strip().lower()
    if result.returncode == 0:
        return output or "enabled"
    if output in ("disabled", "static", "masked", "indirect", "generated", "transient", "linked", "alias"):
        return output
    return ""


def _systemctl_is_active_state(unit: str) -> str:
    target = str(unit or "").strip()
    if not target:
        return ""
    result = _systemctl_capture(["is-active", target])
    if result is None:
        return ""
    output = str(result.stdout or result.stderr or "").strip().lower()
    if result.returncode == 0:
        return output or "active"
    if output in ("inactive", "failed", "activating", "deactivating"):
        return output
    return ""


def read_bt_audio_heal_status():
    timer_unit = str(BT_HEAL_TIMER_UNIT or "").strip()
    service_unit = str(BT_HEAL_SERVICE_UNIT or "").strip()
    timer_load = _systemctl_show_value(timer_unit, "LoadState")
    service_load = _systemctl_show_value(service_unit, "LoadState")
    timer_exists = bool(timer_unit) and timer_load not in ("", "not-found")
    service_exists = bool(service_unit) and service_load not in ("", "not-found")
    timer_enabled_state = _systemctl_is_enabled_state(timer_unit) if timer_exists else "not-found"
    timer_active_state = _systemctl_is_active_state(timer_unit) if timer_exists else "not-found"
    service_active_state = _systemctl_is_active_state(service_unit) if service_exists else "not-found"
    timer_enabled = timer_enabled_state in ("enabled", "enabled-runtime")
    return {
        "timer_unit": timer_unit,
        "service_unit": service_unit,
        "timer_exists": bool(timer_exists),
        "service_exists": bool(service_exists),
        "timer_enabled": bool(timer_enabled),
        "timer_enabled_state": timer_enabled_state or "unknown",
        "timer_active": timer_active_state == "active",
        "timer_active_state": timer_active_state or "unknown",
        "service_active": service_active_state == "active",
        "service_active_state": service_active_state or "unknown",
        "auto_recovery_enabled": bool(timer_enabled),
        "default_enabled": bool(BT_HEAL_DEFAULT_ENABLED),
        "status": "on" if timer_enabled else "off",
    }


def _read_first_line(path: str):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.readline().strip()
    except FileNotFoundError:
        return None


def _parse_temp_c(value) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        temp = float(text)
    except Exception:
        return None
    # Linux thermal sysfs often reports milli-Celsius.
    if abs(temp) > 1000:
        temp = temp / 1000.0
    if temp < -40.0 or temp > 180.0:
        return None
    return float(temp)


def _read_temp_c_file(path: str):
    return _parse_temp_c(_read_first_line(path))


def _iter_hwmon_temp_entries(expected_name: str | None = None):
    root = "/sys/class/hwmon"
    if not os.path.isdir(root):
        return
    for node in sorted(os.listdir(root)):
        hw_path = os.path.join(root, node)
        name = str(_read_first_line(os.path.join(hw_path, "name")) or "").strip()
        if expected_name and name != expected_name:
            continue
        try:
            files = sorted(os.listdir(hw_path))
        except Exception:
            continue
        for filename in files:
            match = re.fullmatch(r"temp(\d+)_input", filename)
            if not match:
                continue
            idx = match.group(1)
            temp_c = _read_temp_c_file(os.path.join(hw_path, filename))
            if temp_c is None:
                continue
            label = _read_first_line(os.path.join(hw_path, f"temp{idx}_label")) or f"temp{idx}"
            yield {
                "hwmon": node,
                "name": name,
                "label": str(label).strip(),
                "temp_c": float(temp_c),
            }


def _read_hwmon_label_temp_c(expected_name: str, expected_label: str):
    want = str(expected_label or "").strip().lower()
    if not want:
        return None
    for row in _iter_hwmon_temp_entries(expected_name=expected_name):
        label = str(row.get("label") or "").strip().lower()
        if label == want:
            return row.get("temp_c")
    return None


def _read_coretemp_package_temp_c():
    package_candidates = []
    for row in _iter_hwmon_temp_entries(expected_name="coretemp"):
        label = str(row.get("label") or "").strip().lower()
        if label.startswith("package id"):
            temp = row.get("temp_c")
            if temp is not None:
                package_candidates.append(float(temp))
    if package_candidates:
        return max(package_candidates)
    return None


def _read_thermal_zone_temp_c_by_type(*type_names: str):
    wanted = {str(name or "").strip().lower() for name in type_names if str(name or "").strip()}
    if not wanted:
        return None
    root = "/sys/class/thermal"
    if not os.path.isdir(root):
        return None
    for node in sorted(os.listdir(root)):
        if not node.startswith("thermal_zone"):
            continue
        zone_path = os.path.join(root, node)
        zone_type = str(_read_first_line(os.path.join(zone_path, "type")) or "").strip().lower()
        if zone_type not in wanted:
            continue
        temp_c = _read_temp_c_file(os.path.join(zone_path, "temp"))
        if temp_c is not None:
            return float(temp_c)
    return None


def _read_cpu_temp_with_source() -> tuple[float | None, str]:
    temp = _read_coretemp_package_temp_c()
    if temp is not None:
        return float(temp), "hwmon:coretemp:package"

    temp = _read_thermal_zone_temp_c_by_type("x86_pkg_temp", "tcpu", "cpu", "cpu-thermal")
    if temp is not None:
        return float(temp), "thermal_zone"

    # Last-resort fallback for platforms where zone0 is the CPU sensor (Pi, etc).
    temp = _read_temp_c_file("/sys/class/thermal/thermal_zone0/temp")
    if temp is not None:
        return float(temp), "thermal_zone0"
    return None, ""


def _read_ambient_temp_with_source() -> tuple[float | None, str]:
    temp = _read_hwmon_label_temp_c("dell_ddv", "Ambient")
    if temp is not None:
        return float(temp), "hwmon:dell_ddv:Ambient"

    # Fallbacks for non-Dell systems that expose an explicit ambient label.
    for driver in ("acpitz", "nct6775", "it87"):
        temp = _read_hwmon_label_temp_c(driver, "Ambient")
        if temp is not None:
            return float(temp), f"hwmon:{driver}:Ambient"

    temp = _read_thermal_zone_temp_c_by_type("ambient")
    if temp is not None:
        return float(temp), "thermal_zone:ambient"
    return None, ""


def _expected_rtl_serials():
    serials = []
    for key in (
        "AIRBAND_RTL_SERIAL",
        "GROUND_RTL_SERIAL",
        "DIGITAL_RTL_SERIAL",
        "DIGITAL_RTL_SERIAL_SECONDARY",
        "DIGITAL_RTL_SERIAL_2",
        "DIGITAL_RTL_SERIAL_TERTIARY",
        "DIGITAL_RTL_SERIAL_3",
        "VDL2_RTL_SERIAL",
    ):
        value = str(os.getenv(key, "") or "").strip()
        if value and value not in serials:
            serials.append(value)
    return serials


def _rtl_dongle_event_message(status: str, serial: str, *, path: str = "", speed_mbps=None) -> str:
    action = "connected" if status == "connected" else "lost"
    detail = serial or "unknown serial"
    extras = []
    if path:
        extras.append(f"path {path}")
    if speed_mbps not in (None, ""):
        try:
            extras.append(f"{int(round(float(speed_mbps)))} Mbps")
        except Exception:
            logger.debug("Failed formatting RTL dongle speed %r for serial %s", speed_mbps, serial, exc_info=True)
    if extras:
        return f"RTL serial {detail} {action} ({', '.join(extras)})"
    return f"RTL serial {detail} {action}"


def _load_rtl_dongle_event_log() -> list[dict]:
    path = str(_RTL_DONGLE_EVENT_LOG_PATH or "").strip()
    if not path or not os.path.isfile(path):
        return []
    rows = deque(maxlen=_RTL_DONGLE_EVENT_HISTORY_LIMIT)
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = str(line or "").strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    logger.debug("system_stats: failed to decode dongle event row from %s", path, exc_info=True)
                    continue
                if isinstance(row, dict):
                    rows.append(dict(row))
    except Exception:
        logger.debug("system_stats: failed to read dongle event log %s", path, exc_info=True)
        return []
    return sorted(
        [dict(item) for item in rows],
        key=lambda item: int(item.get("time_ms") or 0),
        reverse=True,
    )


def _persist_rtl_dongle_event_log(events: list[dict]) -> None:
    path = str(_RTL_DONGLE_EVENT_LOG_PATH or "").strip()
    if not path:
        return
    rows = sorted(
        [dict(item) for item in (events or []) if isinstance(item, dict)],
        key=lambda item: int(item.get("time_ms") or 0),
        reverse=True,
    )[:_RTL_DONGLE_EVENT_HISTORY_LIMIT]
    tmp_path = f"{path}.tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            for item in reversed(rows):
                f.write(json.dumps(item, sort_keys=True) + "\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except Exception:
            logger.debug("system_stats: failed to clean temp dongle event log %s", tmp_path, exc_info=True)
        logger.debug("system_stats: failed to persist dongle event log %s", path, exc_info=True)
        return
def _record_rtl_dongle_events(
    *,
    present_paths: list[dict],
    expected_serials: list[str],
) -> list[dict]:
    now_ms = int(time.time() * 1000)
    current_details = {}
    for row in present_paths or []:
        serial = str((row or {}).get("serial") or "").strip()
        if not serial:
            continue
        current_details[serial] = {
            "path": str((row or {}).get("path") or "").strip(),
            "speed_mbps": (row or {}).get("speed_mbps"),
        }
    current_serials = sorted(current_details.keys())
    expected = {str(token or "").strip() for token in (expected_serials or []) if str(token or "").strip()}

    with _RTL_DONGLE_EVENT_LOCK:
        initialized = bool(_RTL_DONGLE_EVENT_STATE.get("initialized"))
        previous_serials = {
            str(token or "").strip()
            for token in (_RTL_DONGLE_EVENT_STATE.get("present_serials") or [])
            if str(token or "").strip()
        }
        previous_details = dict(_RTL_DONGLE_EVENT_STATE.get("details_by_serial") or {})
        events = list(_RTL_DONGLE_EVENT_STATE.get("events") or [])
        if not events:
            events = _load_rtl_dongle_event_log()

        if not initialized:
            _RTL_DONGLE_EVENT_STATE["initialized"] = True
            _RTL_DONGLE_EVENT_STATE["present_serials"] = list(current_serials)
            _RTL_DONGLE_EVENT_STATE["details_by_serial"] = dict(current_details)
            _RTL_DONGLE_EVENT_STATE["events"] = events
            return [dict(item) for item in events]

        added = sorted(set(current_serials) - previous_serials)
        removed = sorted(previous_serials - set(current_serials))
        new_events = []
        for serial in added:
            detail = current_details.get(serial) or {}
            event = {
                "event_id": f"{now_ms}:connected:{serial}",
                "status": "connected",
                "serial": serial,
                "expected": serial in expected,
                "path": str(detail.get("path") or ""),
                "speed_mbps": detail.get("speed_mbps"),
                "time_ms": now_ms,
                "message": _rtl_dongle_event_message(
                    "connected",
                    serial,
                    path=str(detail.get("path") or ""),
                    speed_mbps=detail.get("speed_mbps"),
                ),
            }
            new_events.append(event)
        for serial in removed:
            detail = previous_details.get(serial) or {}
            event = {
                "event_id": f"{now_ms}:lost:{serial}",
                "status": "lost",
                "serial": serial,
                "expected": serial in expected,
                "path": str(detail.get("path") or ""),
                "speed_mbps": detail.get("speed_mbps"),
                "time_ms": now_ms,
                "message": _rtl_dongle_event_message(
                    "lost",
                    serial,
                    path=str(detail.get("path") or ""),
                    speed_mbps=detail.get("speed_mbps"),
                ),
            }
            new_events.append(event)

        if new_events:
            events = sorted(
                [dict(item) for item in new_events] + events,
                key=lambda item: int(item.get("time_ms") or 0),
                reverse=True,
            )[:_RTL_DONGLE_EVENT_HISTORY_LIMIT]
            _persist_rtl_dongle_event_log(events)

        _RTL_DONGLE_EVENT_STATE["initialized"] = True
        _RTL_DONGLE_EVENT_STATE["present_serials"] = list(current_serials)
        _RTL_DONGLE_EVENT_STATE["details_by_serial"] = dict(current_details)
        _RTL_DONGLE_EVENT_STATE["events"] = list(events)
        return [dict(item) for item in events]


def read_rtl_dongle_health():
    present_serials = []
    present_paths = []
    speed_by_serial = {}
    root = _RTL_USB_SYSFS_ROOT
    if os.path.isdir(root):
        for dev in sorted(os.listdir(root)):
            dev_path = os.path.join(root, dev)
            vendor = _read_first_line(os.path.join(dev_path, "idVendor"))
            product = _read_first_line(os.path.join(dev_path, "idProduct"))
            if not vendor or not product:
                continue
            if vendor.strip().lower() != _RTL_USB_VENDOR:
                continue
            if product.strip().lower() not in _RTL_USB_PRODUCTS:
                continue
            serial = _read_first_line(os.path.join(dev_path, "serial")) or ""
            serial = serial.strip()
            speed_raw = _read_first_line(os.path.join(dev_path, "speed")) or ""
            speed_mbps = None
            if speed_raw:
                try:
                    speed_mbps = int(round(float(speed_raw)))
                except Exception:
                    speed_mbps = None
            if serial and serial not in present_serials:
                present_serials.append(serial)
                present_paths.append({"path": dev, "serial": serial, "speed_mbps": speed_mbps})
            if serial and speed_mbps is not None:
                prev = speed_by_serial.get(serial)
                if prev is None or speed_mbps > prev:
                    speed_by_serial[serial] = speed_mbps

    expected_serials = _expected_rtl_serials()
    events = _record_rtl_dongle_events(
        present_paths=present_paths,
        expected_serials=expected_serials,
    )
    missing_expected_serials = [s for s in expected_serials if s not in present_serials]
    unexpected_serials = [s for s in present_serials if s not in expected_serials]
    slow_expected_serials = [
        s for s in expected_serials
        if s in speed_by_serial and int(speed_by_serial[s]) < int(_RTL_MIN_USB_SPEED_MBPS)
    ]

    present_count = len(present_serials)
    expected_count = len(expected_serials)
    target_count = max(_RTL_DONGLE_TARGET, expected_count) if expected_count else _RTL_DONGLE_TARGET

    enough_present = present_count >= target_count if _RTL_ALLOW_EXTRA else present_count == target_count

    if enough_present and not missing_expected_serials and not slow_expected_serials:
        status = "ideal"
    elif present_count >= max(1, target_count - 1):
        status = "degraded"
    else:
        status = "critical"

    return {
        "target_count": target_count,
        "present_count": present_count,
        "expected_count": expected_count,
        "status": status,
        "healthy": status == "ideal",
        "present_serials": present_serials,
        "expected_serials": expected_serials,
        "expected_serial_speeds_mbps": speed_by_serial,
        "min_speed_mbps": _RTL_MIN_USB_SPEED_MBPS,
        "missing_expected_serials": missing_expected_serials,
        "slow_expected_serials": slow_expected_serials,
        "unexpected_serials": unexpected_serials,
        "present_paths": present_paths,
        "events": events,
        "last_event_time_ms": int((events[0] or {}).get("time_ms") or 0) if events else 0,
    }


def read_cpu_temp_c():
    temp, _source = _read_cpu_temp_with_source()
    return temp


def read_gpu_temp_c():
    if not shutil.which("vcgencmd"):
        return None
    try:
        out = subprocess.check_output(["vcgencmd", "measure_temp"], text=True).strip()
        # format: temp=48.0'C
        if "temp=" in out:
            val = out.split("temp=")[-1].split("'")[0]
            return float(val)
    except Exception:
        return None
    return None


def read_uptime_s():
    line = _read_first_line("/proc/uptime")
    if not line:
        return None
    try:
        return float(line.split()[0])
    except (ValueError, IndexError):
        return None


def read_cpu_usage_percent():
    line = _read_first_line("/proc/stat")
    if not line or not line.startswith("cpu "):
        return None
    parts = line.split()
    if len(parts) < 5:
        return None
    try:
        nums = [int(p) for p in parts[1:]]
    except ValueError:
        return None
    total = sum(nums)
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
    now = time.time()
    last_total = _last_cpu.get("total")
    last_idle = _last_cpu.get("idle")
    _last_cpu.update({"total": total, "idle": idle, "ts": now})
    if last_total is None or last_idle is None:
        return None
    total_delta = total - last_total
    idle_delta = idle - last_idle
    if total_delta <= 0:
        return None
    usage = 1.0 - (idle_delta / total_delta)
    return max(0.0, min(100.0, usage * 100.0))


def read_load_avg():
    try:
        one, five, fifteen = os.getloadavg()
        return {
            "one": float(one),
            "five": float(five),
            "fifteen": float(fifteen),
        }
    except Exception:
        return None


def read_mem_stats():
    meminfo_path = "/proc/meminfo"
    try:
        rows = {}
        with open(meminfo_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if ":" not in line:
                    continue
                key, raw = line.split(":", 1)
                parts = str(raw or "").strip().split()
                if not parts:
                    continue
                try:
                    rows[key.strip()] = int(parts[0])
                except Exception:
                    continue
    except Exception:
        return None

    total_kb = int(rows.get("MemTotal") or 0)
    if total_kb <= 0:
        return None
    available_kb = int(rows.get("MemAvailable") or 0)
    if available_kb <= 0:
        available_kb = int(rows.get("MemFree") or 0) + int(rows.get("Buffers") or 0) + int(rows.get("Cached") or 0)
    if available_kb < 0:
        available_kb = 0
    used_kb = max(0, total_kb - available_kb)
    used_pct = (float(used_kb) / float(total_kb)) * 100.0 if total_kb > 0 else 0.0
    return {
        "total_kb": total_kb,
        "available_kb": available_kb,
        "used_kb": used_kb,
        "used_percent": max(0.0, min(100.0, used_pct)),
    }


def read_disk_stats(path: str = "/"):
    try:
        usage = shutil.disk_usage(path)
    except Exception:
        return None
    total = int(usage.total or 0)
    used = int(usage.used or 0)
    free = int(usage.free or 0)
    used_pct = (float(used) / float(total)) * 100.0 if total > 0 else 0.0
    return {
        "path": path,
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "used_percent": max(0.0, min(100.0, used_pct)),
    }


def read_net_bytes():
    base = "/sys/class/net"
    if not os.path.isdir(base):
        return {"rx_bytes": 0, "tx_bytes": 0, "ifaces": []}
    total_rx = 0
    total_tx = 0
    ifaces = []
    for iface in sorted(os.listdir(base)):
        if iface == "lo":
            continue
        rx_path = os.path.join(base, iface, "statistics", "rx_bytes")
        tx_path = os.path.join(base, iface, "statistics", "tx_bytes")
        try:
            with open(rx_path, "r", encoding="utf-8") as f:
                rx = int(f.read().strip())
            with open(tx_path, "r", encoding="utf-8") as f:
                tx = int(f.read().strip())
        except (FileNotFoundError, ValueError):
            continue
        total_rx += rx
        total_tx += tx
        ifaces.append({"name": iface, "rx_bytes": rx, "tx_bytes": tx})
    return {"rx_bytes": total_rx, "tx_bytes": total_tx, "ifaces": ifaces}

def read_pressure(path: str):
    line = _read_first_line(path)
    if not line:
        return None
    parts = line.split()
    if len(parts) < 2:
        return None
    data = {}
    for tok in parts[1:]:
        if "=" not in tok:
            continue
        key, val = tok.split("=", 1)
        try:
            data[key] = float(val)
        except ValueError:
            continue
    return data or None


def get_system_stats():
    hostname = ""
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = ""
    cpu_temp_c, cpu_temp_source = _read_cpu_temp_with_source()
    ambient_temp_c, ambient_temp_source = _read_ambient_temp_with_source()
    return {
        "ok": True,
        "timestamp": time.time(),
        "hostname": hostname,
        "cpu_count": int(os.cpu_count() or 0),
        "cpu_temp_c": cpu_temp_c,
        "cpu_temp_source": cpu_temp_source,
        "ambient_temp_c": ambient_temp_c,
        "ambient_temp_source": ambient_temp_source,
        "gpu_temp_c": read_gpu_temp_c(),
        "uptime_s": read_uptime_s(),
        "cpu_usage": read_cpu_usage_percent(),
        "load_avg": read_load_avg(),
        "memory": read_mem_stats(),
        "disk": read_disk_stats("/"),
        "net": read_net_bytes(),
        "cpu_pressure": read_pressure("/proc/pressure/cpu"),
        "memory_pressure": read_pressure("/proc/pressure/memory"),
        "io_pressure": read_pressure("/proc/pressure/io"),
        "dongles": read_rtl_dongle_health(),
        "bt_heal": read_bt_audio_heal_status(),
    }
