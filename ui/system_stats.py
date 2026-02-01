"""System stats helpers for Pi health telemetry."""
import os
import time
import shutil
import subprocess

_last_cpu = {"total": None, "idle": None, "ts": None}


def _read_first_line(path: str):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.readline().strip()
    except FileNotFoundError:
        return None


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


def get_system_stats():
    return {
        "ok": True,
        "timestamp": time.time(),
        "cpu_temp_c": read_cpu_temp_c(),
        "gpu_temp_c": read_gpu_temp_c(),
        "uptime_s": read_uptime_s(),
        "cpu_usage": read_cpu_usage_percent(),
        "net": read_net_bytes(),
    }
