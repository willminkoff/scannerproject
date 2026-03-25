#!/usr/bin/env python3
"""Apply local audio monitor policy for SDRTrunk Pulse sink inputs.

Default behavior mutes SDRTrunk's direct local Java audio path so the
VLC/stream path is the only audible output.
"""

from __future__ import annotations

import json
import os
import shutil
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


def _have_cmd(name: str) -> bool:
    return bool(shutil.which(name))


def _candidate_audio_uids() -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for raw in (
        os.getenv("DIGITAL_LOCAL_MONITOR_UID"),
        os.getenv("VLC_AUDIO_UID"),
        os.getenv("SUDO_UID"),
        str(os.getuid()),
    ):
        try:
            value = int(str(raw).strip())
        except Exception:
            continue
        if value < 0 or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _prepare_audio_env() -> None:
    runtime_dir = str(os.getenv("XDG_RUNTIME_DIR") or "").strip()
    if runtime_dir and os.path.isdir(runtime_dir):
        return
    for uid in _candidate_audio_uids():
        candidate = f"/run/user/{uid}"
        if not os.path.isdir(candidate):
            continue
        os.environ["XDG_RUNTIME_DIR"] = candidate
        bus_path = os.path.join(candidate, "bus")
        pulse_path = os.path.join(candidate, "pulse", "native")
        if os.path.exists(bus_path):
            os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={bus_path}")
        if os.path.exists(pulse_path):
            os.environ.setdefault("PULSE_SERVER", f"unix:{pulse_path}")
        return


def _audio_session_available() -> bool:
    if _have_cmd("wpctl"):
        try:
            res = subprocess.run(
                ["wpctl", "status"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if res.returncode == 0:
                return True
        except Exception:
            pass
    if _have_cmd("pactl"):
        try:
            res = subprocess.run(
                ["pactl", "info"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if res.returncode == 0:
                return True
        except Exception:
            pass
    return False


def _parse_pactl_sink_inputs(text: str) -> list[dict]:
    if not text:
        return []

    try:
        lines = str(text).splitlines()
    except Exception:
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
                    "backend": "pactl",
                    "app_name": app_name,
                    "proc_binary": proc_binary,
                    "proc_pid": proc_pid,
                }
            )
        sink_id = ""
        app_name = ""
        proc_binary = ""
        proc_pid = ""

    for raw in lines:
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


def _list_sink_inputs_pactl() -> list[dict]:
    if not _have_cmd("pactl"):
        return []
    try:
        res = subprocess.run(
            ["pactl", "list", "sink-inputs"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except Exception:
        return []
    if res.returncode != 0 or not res.stdout:
        return []
    return _parse_pactl_sink_inputs(res.stdout)


def _parse_pw_dump_audio_objects(text: str) -> list[dict]:
    if not text:
        return []
    try:
        payload = json.loads(text)
    except Exception:
        return []
    if not isinstance(payload, list):
        return []

    out: list[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if not isinstance(item_id, int) or item_id <= 0:
            continue
        info = item.get("info")
        if not isinstance(info, dict):
            continue
        props = info.get("props")
        if not isinstance(props, dict):
            continue
        media_class = str(props.get("media.class") or "").strip().lower()
        if "stream/output/audio" not in media_class:
            continue
        out.append(
            {
                "id": str(item_id),
                "backend": "wpctl",
                "app_name": str(props.get("application.name") or "").strip(),
                "proc_binary": str(props.get("application.process.binary") or "").strip(),
                "proc_pid": str(
                    props.get("application.process.id")
                    or props.get("pipewire.sec.pid")
                    or ""
                ).strip(),
                "node_name": str(props.get("node.name") or "").strip(),
                "node_description": str(
                    props.get("node.description")
                    or props.get("media.name")
                    or ""
                ).strip(),
            }
        )
    return out


def _list_sink_inputs_wpctl() -> list[dict]:
    if not _have_cmd("pw-dump"):
        return []
    try:
        res = subprocess.run(
            ["pw-dump"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except Exception:
        return []
    if res.returncode != 0 or not res.stdout:
        return []
    return _parse_pw_dump_audio_objects(res.stdout)


def _list_sink_inputs() -> list[dict]:
    items = _list_sink_inputs_pactl()
    if items:
        return items
    return _list_sink_inputs_wpctl()


def _is_sdrtrunk_audio_object(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    proc_pid = str(item.get("proc_pid") or "").strip()
    if proc_pid and _is_sdrtrunk_java_pid(proc_pid):
        return True
    app_name = str(item.get("app_name") or "").lower()
    proc_binary = str(item.get("proc_binary") or "").lower()
    node_name = str(item.get("node_name") or "").lower()
    node_description = str(item.get("node_description") or "").lower()
    haystack = " ".join(token for token in (app_name, proc_binary, node_name, node_description) if token)
    return "sdrtrunk" in haystack


def _find_sdrtrunk_audio_objects() -> list[dict]:
    keyed: dict[tuple[str, str], dict] = {}
    for item in _list_sink_inputs():
        obj_id = str(item.get("id") or "").strip()
        backend = str(item.get("backend") or "").strip().lower()
        if not obj_id or not backend:
            continue
        if not _is_sdrtrunk_audio_object(item):
            continue
        keyed[(backend, obj_id)] = dict(item)
    return [keyed[key] for key in sorted(keyed.keys())]


def _set_sink_mute(item: dict, muted: bool) -> bool:
    sink_id = str((item or {}).get("id") or "").strip()
    backend = str((item or {}).get("backend") or "").strip().lower()
    if not sink_id:
        return False
    val = "1" if muted else "0"
    if backend == "wpctl":
        cmd = ["wpctl", "set-mute", sink_id, val]
    else:
        cmd = ["pactl", "set-sink-input-mute", sink_id, val]
    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return res.returncode == 0
    except Exception:
        return False


def apply_policy() -> int:
    _prepare_audio_env()
    local_monitor_enabled = env_flag("DIGITAL_LOCAL_MONITOR", False)
    wait_sec = env_int("DIGITAL_LOCAL_MONITOR_WAIT_SEC", 20)
    poll_sec = env_float("DIGITAL_LOCAL_MONITOR_POLL_SEC", 1.0)

    if local_monitor_enabled:
        _log("DIGITAL_LOCAL_MONITOR enabled; leaving SDRTrunk local audio unmuted")
        return 0
    if not _audio_session_available():
        _log("audio session unavailable; skipping")
        return 0

    deadline = time.time() + max(wait_sec, 0)
    while True:
        items = _find_sdrtrunk_audio_objects()
        if items:
            ok = 0
            muted_ids: list[str] = []
            for item in items:
                if _set_sink_mute(item, True):
                    ok += 1
                muted_ids.append(f"{item.get('backend')}:{item.get('id')}")
            _log(
                "muted SDRTrunk local audio objects: "
                f"{','.join(muted_ids)} (ok={ok}/{len(items)})"
            )
            return 0
        if time.time() >= deadline:
            _log("no SDRTrunk local audio objects found within wait window")
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
