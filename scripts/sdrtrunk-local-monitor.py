#!/usr/bin/env python3
"""Apply local audio monitor policy for SDRTrunk Pulse sink inputs.

Default behavior mutes SDRTrunk's direct local Java audio path so the
VLC/stream path is the only audible output.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except Exception:
        return default


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(str(raw).strip())
    except Exception:
        return default


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} sdrtrunk-local-monitor: {msg}")


def _is_sdrtrunk_java_pid(pid: str) -> bool:
    if not pid or not pid.isdigit():
        return False
    path = f"/proc/{pid}/cmdline"
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception:
        return False
    if not raw:
        return False
    cmdline = raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore").lower()
    return "sdrtrunk" in cmdline


def _list_sink_inputs() -> list[dict]:
    try:
        res = subprocess.run(
            ["pactl", "list", "sink-inputs"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except Exception:
        return []
    if res.returncode != 0 or not res.stdout:
        return []

    sink_id = ""
    app_name = ""
    proc_binary = ""
    proc_pid = ""
    out: list[dict] = []

    def flush():
        nonlocal sink_id, app_name, proc_binary, proc_pid, out
        if sink_id:
            out.append(
                {
                    "id": sink_id,
                    "app_name": app_name,
                    "proc_binary": proc_binary,
                    "proc_pid": proc_pid,
                }
            )
        sink_id = ""
        app_name = ""
        proc_binary = ""
        proc_pid = ""

    for raw in res.stdout.splitlines():
        line = raw.strip()
        if line.startswith("Sink Input #"):
            flush()
            sink_id = line.split("#", 1)[1].strip()
            continue
        if line.startswith("application.name ="):
            app_name = line.split("=", 1)[1].strip().strip('"')
            continue
        if line.startswith("application.process.binary ="):
            proc_binary = line.split("=", 1)[1].strip().strip('"')
            continue
        if line.startswith("application.process.id ="):
            proc_pid = line.split("=", 1)[1].strip().strip('"')
            continue
    flush()
    return out


def _find_sdrtrunk_sink_ids() -> list[str]:
    ids: list[str] = []
    for item in _list_sink_inputs():
        sid = str(item.get("id") or "").strip()
        if not sid:
            continue
        app_name = str(item.get("app_name") or "").lower()
        proc_binary = str(item.get("proc_binary") or "").lower()
        proc_pid = str(item.get("proc_pid") or "").strip()
        is_java = proc_binary == "java" or "java" in app_name
        if is_java and _is_sdrtrunk_java_pid(proc_pid):
            ids.append(sid)
    return sorted(set(ids))


def _set_sink_mute(sink_id: str, muted: bool) -> bool:
    val = "1" if muted else "0"
    try:
        res = subprocess.run(
            ["pactl", "set-sink-input-mute", sink_id, val],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return res.returncode == 0
    except Exception:
        return False


def apply_policy() -> int:
    local_monitor_enabled = env_flag("DIGITAL_LOCAL_MONITOR", False)
    wait_sec = env_int("DIGITAL_LOCAL_MONITOR_WAIT_SEC", 20)
    poll_sec = env_float("DIGITAL_LOCAL_MONITOR_POLL_SEC", 1.0)

    if local_monitor_enabled:
        _log("DIGITAL_LOCAL_MONITOR enabled; leaving SDRTrunk local audio unmuted")
        return 0

    deadline = time.time() + max(wait_sec, 0)
    while True:
        ids = _find_sdrtrunk_sink_ids()
        if ids:
            ok = 0
            for sid in ids:
                if _set_sink_mute(sid, True):
                    ok += 1
            _log(f"muted SDRTrunk local sink inputs: {','.join(ids)} (ok={ok}/{len(ids)})")
            return 0
        if time.time() >= deadline:
            _log("no SDRTrunk local sink inputs found within wait window")
            return 0
        time.sleep(max(poll_sec, 0.2))


def main() -> int:
    action = (sys.argv[1] if len(sys.argv) > 1 else "apply").strip().lower()
    if action not in ("apply", "start"):
        _log(f"unknown action: {action}")
        return 2
    return apply_policy()


if __name__ == "__main__":
    raise SystemExit(main())
