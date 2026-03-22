"""System stats helpers for host/PC health telemetry."""
import os
import time
import shutil
import subprocess
import socket

_last_cpu = {"total": None, "idle": None, "ts": None}
_RTL_USB_SYSFS_ROOT = os.getenv("RTL_USB_SYSFS_ROOT", "/sys/bus/usb/devices")
_RTL_USB_VENDOR = os.getenv("RTL_USB_VENDOR", "0bda").strip().lower()
_RTL_USB_PRODUCT = os.getenv("RTL_USB_PRODUCT", "2838").strip().lower()
try:
    _RTL_MIN_USB_SPEED_MBPS = max(1, int(os.getenv("RTL_MIN_USB_SPEED_MBPS", "480")))
except Exception:
    _RTL_MIN_USB_SPEED_MBPS = 480

try:
    _RTL_DONGLE_TARGET = max(1, int(os.getenv("RTL_DONGLE_TARGET", "4")))
except Exception:
    _RTL_DONGLE_TARGET = 4


def _read_first_line(path: str):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.readline().strip()
    except FileNotFoundError:
        return None


def _expected_rtl_serials():
    serials = []
    for key in (
        "AIRBAND_RTL_SERIAL",
        "GROUND_RTL_SERIAL",
        "DIGITAL_RTL_SERIAL",
        "DIGITAL_RTL_SERIAL_SECONDARY",
        "DIGITAL_RTL_SERIAL_2",
    ):
        value = str(os.getenv(key, "") or "").strip()
        if value and value not in serials:
            serials.append(value)
    return serials


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
            if product.strip().lower() != _RTL_USB_PRODUCT:
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
    missing_expected_serials = [s for s in expected_serials if s not in present_serials]
    unexpected_serials = [s for s in present_serials if s not in expected_serials]
    slow_expected_serials = [
        s for s in expected_serials
        if s in speed_by_serial and int(speed_by_serial[s]) < int(_RTL_MIN_USB_SPEED_MBPS)
    ]

    present_count = len(present_serials)
    expected_count = len(expected_serials)
    target_count = max(_RTL_DONGLE_TARGET, expected_count) if expected_count else _RTL_DONGLE_TARGET

    if (
        present_count == target_count
        and not missing_expected_serials
        and not slow_expected_serials
    ):
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
    }


def read_cpu_temp_c():
    line = _read_first_line("/sys/class/thermal/thermal_zone0/temp")
    if not line:
        return None
    try:
        return float(line) / 1000.0
    except ValueError:
        return None


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
    return {
        "ok": True,
        "timestamp": time.time(),
        "hostname": hostname,
        "cpu_count": int(os.cpu_count() or 0),
        "cpu_temp_c": read_cpu_temp_c(),
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
    }
