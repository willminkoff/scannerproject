"""VLC playback control for local Icecast streams."""
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Optional
from urllib.parse import urlparse

try:
    from .config import ICECAST_HOST, ICECAST_PORT, PLAYER_MOUNT, DIGITAL_STREAM_MOUNT
except ImportError:
    from ui.config import ICECAST_HOST, ICECAST_PORT, PLAYER_MOUNT, DIGITAL_STREAM_MOUNT

logger = logging.getLogger(__name__)

VLC_HTTP_RECONNECT = str(os.getenv("VLC_HTTP_RECONNECT", "1")).strip().lower() in ("1", "true", "yes", "on")
try:
    VLC_NETWORK_CACHING_MS = max(0, int(str(os.getenv("VLC_NETWORK_CACHING_MS", "150")).strip()))
except Exception:
    VLC_NETWORK_CACHING_MS = 150
try:
    VLC_START_VERIFY_SEC = max(0.0, float(str(os.getenv("VLC_START_VERIFY_MS", "350")).strip()) / 1000.0)
except Exception:
    VLC_START_VERIFY_SEC = 0.35
try:
    VLC_START_VERIFY_POLL_SEC = max(0.02, float(str(os.getenv("VLC_START_VERIFY_POLL_MS", "50")).strip()) / 1000.0)
except Exception:
    VLC_START_VERIFY_POLL_SEC = 0.05

try:
    _VLC_AUDIO_UID = int(str(os.getenv("VLC_AUDIO_UID", os.getuid())).strip())
except Exception:
    _VLC_AUDIO_UID = os.getuid()
VLC_XDG_RUNTIME_DIR = str(os.getenv("VLC_XDG_RUNTIME_DIR", f"/run/user/{_VLC_AUDIO_UID}")).strip()
VLC_DBUS_SESSION_BUS_ADDRESS = str(
    os.getenv("VLC_DBUS_SESSION_BUS_ADDRESS", f"unix:path={VLC_XDG_RUNTIME_DIR}/bus")
).strip()
VLC_PULSE_SERVER = str(os.getenv("VLC_PULSE_SERVER", f"unix:{VLC_XDG_RUNTIME_DIR}/pulse/native")).strip()
VLC_PULSE_SINK = str(os.getenv("VLC_PULSE_SINK", "")).strip()

VLC_PID_DIR = os.getenv("VLC_PID_DIR", os.path.join(VLC_XDG_RUNTIME_DIR, "airband-ui"))
VLC_PID_PREFIX = os.getenv("VLC_PID_PREFIX", "airband-ui-vlc")
VLC_STOP_TIMEOUT_SEC = max(0.2, float(os.getenv("VLC_STOP_TIMEOUT_SEC", "2.0")))

# Per-target VLC playback gain.  1.0 = unity (no change).
# Digital audio is now normalized in the bridge, so it should not need
# a large gain boost.  Analog gain can be tuned separately.
try:
    VLC_GAIN_ANALOG = float(os.getenv("VLC_GAIN_ANALOG", "1.0"))
except Exception:
    VLC_GAIN_ANALOG = 1.0
try:
    VLC_GAIN_DIGITAL = float(os.getenv("VLC_GAIN_DIGITAL", "1.5"))
except Exception:
    VLC_GAIN_DIGITAL = 1.5
_VLC_GAINS = {"analog": VLC_GAIN_ANALOG, "digital": VLC_GAIN_DIGITAL}

VLC_TARGETS = ("analog", "digital")
DEFAULT_TARGET = "analog"
DEFAULT_MOUNTS = {
    "analog": PLAYER_MOUNT,
    "digital": DIGITAL_STREAM_MOUNT or PLAYER_MOUNT,
}

_MOUNT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_LOCAL_MONITOR_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "sdrtrunk-local-monitor.py",
)
_STREAM_URL_RE = re.compile(r"https?://\S+")
_RUNTIME_LOCK = threading.RLock()


def _normalize_target(target: str) -> str:
    value = (target or DEFAULT_TARGET).strip().lower()
    return value if value in VLC_TARGETS else ""


def _sanitize_mount(mount: str) -> str:
    raw = str(mount or "").strip().lstrip("/")
    if not raw:
        return ""
    return raw if _MOUNT_RE.fullmatch(raw) else ""


def _stream_url_for(target: str, mount: str = "") -> str:
    picked_mount = _sanitize_mount(mount) or DEFAULT_MOUNTS.get(target) or PLAYER_MOUNT
    picked_mount = picked_mount.lstrip("/")
    return f"http://{ICECAST_HOST}:{ICECAST_PORT}/{picked_mount}"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _runtime_template(target: str) -> dict:
    mount = _sanitize_mount(DEFAULT_MOUNTS.get(target) or PLAYER_MOUNT)
    return {
        "target": target,
        "state": "idle",
        "running": False,
        "process_running": False,
        "verified": False,
        "pid": None,
        "mount": mount,
        "actual_mount": "",
        "stream_url": _stream_url_for(target, mount),
        "actual_stream_url": "",
        "audio_sink": str(VLC_PULSE_SINK or "").strip(),
        "actual_audio_sink": "",
        "error": "",
        "last_transition_ms": _now_ms(),
    }


_TARGET_RUNTIME = {name: _runtime_template(name) for name in VLC_TARGETS}


def _target_runtime(target: str) -> dict:
    with _RUNTIME_LOCK:
        return dict(_TARGET_RUNTIME[target])


def _set_target_runtime(
    target: str,
    *,
    state: str,
    pid: Optional[int],
    mount: str,
    stream_url: str,
    audio_sink: str,
    actual_mount: str,
    actual_stream_url: str,
    actual_audio_sink: str,
    error: str,
    process_running: bool,
    verified: bool,
) -> dict:
    normalized_mount = _sanitize_mount(mount) or _sanitize_mount(DEFAULT_MOUNTS.get(target) or PLAYER_MOUNT)
    normalized_stream_url = str(stream_url or "").strip() or _stream_url_for(target, normalized_mount)
    desired_sink = str(audio_sink or "").strip()
    payload = {
        "target": target,
        "state": str(state or "idle"),
        "running": str(state or "idle") == "running",
        "process_running": bool(process_running),
        "verified": bool(verified),
        "pid": int(pid) if isinstance(pid, int) and pid > 1 else None,
        "mount": normalized_mount,
        "actual_mount": str(actual_mount or "").strip(),
        "stream_url": normalized_stream_url,
        "actual_stream_url": str(actual_stream_url or "").strip(),
        "audio_sink": desired_sink,
        "actual_audio_sink": str(actual_audio_sink or "").strip(),
        "error": str(error or "").strip(),
    }
    with _RUNTIME_LOCK:
        current = _TARGET_RUNTIME[target]
        changed = any(current.get(key) != payload.get(key) for key in payload)
        payload["last_transition_ms"] = _now_ms() if changed else current.get("last_transition_ms", _now_ms())
        _TARGET_RUNTIME[target] = payload
        return dict(payload)


def _status_from_probe(
    target: str,
    probe: dict,
    *,
    mount: str,
    stream_url: str,
    audio_sink: str,
) -> dict:
    current = _target_runtime(target)
    if probe.get("process_running") and probe.get("verified"):
        state = "running"
        error = ""
    elif probe.get("process_running"):
        state = "error"
        error = str(probe.get("error") or "startup verification failed")
    elif current.get("state") == "error" and current.get("error"):
        state = "error"
        error = str(current.get("error") or "")
    else:
        state = "idle"
        error = ""
    return _set_target_runtime(
        target,
        state=state,
        pid=probe.get("pid"),
        mount=mount,
        stream_url=stream_url,
        audio_sink=audio_sink,
        actual_mount=probe.get("actual_mount", ""),
        actual_stream_url=probe.get("actual_stream_url", ""),
        actual_audio_sink=probe.get("actual_audio_sink", ""),
        error=error,
        process_running=bool(probe.get("process_running")),
        verified=bool(probe.get("verified")),
    )


def _status_error(
    target: str,
    *,
    mount: str,
    stream_url: str,
    audio_sink: str,
    pid: Optional[int],
    error: str,
    process_running: bool,
    actual_mount: str = "",
    actual_stream_url: str = "",
    actual_audio_sink: str = "",
) -> dict:
    return _set_target_runtime(
        target,
        state="error",
        pid=pid,
        mount=mount,
        stream_url=stream_url,
        audio_sink=audio_sink,
        actual_mount=actual_mount,
        actual_stream_url=actual_stream_url,
        actual_audio_sink=actual_audio_sink,
        error=error,
        process_running=process_running,
        verified=False,
    )


def _pid_path(target: str) -> str:
    pid_dir = VLC_PID_DIR
    try:
        if not os.path.isdir(pid_dir):
            os.makedirs(pid_dir, exist_ok=True)
        if not os.access(pid_dir, os.W_OK):
            raise PermissionError("pid dir not writable")
    except Exception:
        pid_dir = "/tmp"
    return os.path.join(pid_dir, f"{VLC_PID_PREFIX}-{target}.pid")


def _unix_socket_path(address: str) -> str:
    value = str(address or "").strip()
    if value.startswith("unix:path="):
        return value.split("unix:path=", 1)[1].strip()
    if value.startswith("unix:"):
        return value.split("unix:", 1)[1].strip()
    return value


def _vlc_launch_env() -> dict:
    env = os.environ.copy()
    runtime_dir = str(VLC_XDG_RUNTIME_DIR or "").strip()
    if runtime_dir and os.path.isdir(runtime_dir):
        env["XDG_RUNTIME_DIR"] = runtime_dir

    pulse_server = str(VLC_PULSE_SERVER or "").strip()
    pulse_socket = _unix_socket_path(pulse_server)
    if pulse_server and (not pulse_socket or os.path.exists(pulse_socket)):
        env["PULSE_SERVER"] = pulse_server
    elif runtime_dir:
        default_pulse_socket = os.path.join(runtime_dir, "pulse", "native")
        if os.path.exists(default_pulse_socket):
            env["PULSE_SERVER"] = f"unix:{default_pulse_socket}"

    dbus_addr = str(VLC_DBUS_SESSION_BUS_ADDRESS or "").strip()
    dbus_socket = _unix_socket_path(dbus_addr)
    if dbus_addr and (not dbus_socket or os.path.exists(dbus_socket)):
        env["DBUS_SESSION_BUS_ADDRESS"] = dbus_addr

    if VLC_PULSE_SINK:
        env["PULSE_SINK"] = VLC_PULSE_SINK
    return env


def _pulse_tool_output(cmd: list[str]) -> str:
    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=_vlc_launch_env(),
            timeout=2.0,
            check=False,
        )
    except Exception:
        return ""
    return res.stdout if res.returncode == 0 else ""


def _preferred_sink_name() -> str:
    return str(VLC_PULSE_SINK or "").strip()


def _wpctl_sink_id_for_node_name(node_name: str) -> str:
    if not node_name or not shutil.which("wpctl"):
        return ""
    status = _pulse_tool_output(["wpctl", "status"])
    if not status:
        return ""
    in_sinks = False
    for line in status.splitlines():
        stripped = line.strip()
        if stripped.startswith("Sinks:"):
            in_sinks = True
            continue
        if in_sinks and stripped.startswith("Sink endpoints:"):
            break
        if not in_sinks:
            continue
        match = re.match(r"^[^0-9]*([0-9]+)\.", line)
        if not match:
            continue
        sink_id = match.group(1)
        inspect = _pulse_tool_output(["wpctl", "inspect", sink_id])
        if f'node.name = "{node_name}"' in inspect:
            return sink_id
    return ""


def _prefer_configured_pulse_sink() -> None:
    sink_name = _preferred_sink_name()
    if not sink_name:
        return
    sink_id = _wpctl_sink_id_for_node_name(sink_name)
    if sink_id:
        try:
            subprocess.run(
                ["wpctl", "set-default", sink_id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=_vlc_launch_env(),
                timeout=2.0,
                check=False,
            )
            return
        except Exception:
            logger.debug("Failed setting wpctl default sink for VLC playback", exc_info=True)
    if shutil.which("pactl"):
        try:
            subprocess.run(
                ["pactl", "set-default-sink", sink_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=_vlc_launch_env(),
                timeout=2.0,
                check=False,
            )
        except Exception:
            logger.debug("Failed setting pactl default sink for VLC playback", exc_info=True)


def _read_pid(target: str) -> Optional[int]:
    path = _pid_path(target)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        pid = int(raw)
        return pid if pid > 1 else None
    except Exception:
        return None


def _write_pid(target: str, pid: int) -> None:
    path = _pid_path(target)
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(pid))


def _clear_pid(target: str) -> None:
    path = _pid_path(target)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception:
        logger.debug("Failed clearing VLC pid file for %s at %s", target, path, exc_info=True)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _pid_cmdline(pid: int) -> str:
    if not isinstance(pid, int) or pid <= 1:
        return ""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except Exception:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore").lower()


def _is_vlc_pid(pid: int) -> bool:
    cmdline = _pid_cmdline(pid)
    return bool(cmdline and ("vlc" in cmdline or "cvlc" in cmdline))


def _pid_environ(pid: int) -> dict:
    if not isinstance(pid, int) or pid <= 1:
        return {}
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            raw = f.read()
    except Exception:
        return {}
    env = {}
    for entry in raw.split(b"\x00"):
        if b"=" not in entry:
            continue
        key, value = entry.split(b"=", 1)
        try:
            env[key.decode("utf-8", errors="ignore")] = value.decode("utf-8", errors="ignore")
        except Exception:
            continue
    return env


def _normalize_stream_url(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value)
    except Exception:
        return value.lower()
    scheme = str(parsed.scheme or "http").lower()
    netloc = str(parsed.netloc or "").lower()
    path = str(parsed.path or "").rstrip("/")
    return f"{scheme}://{netloc}{path}".lower()


def _extract_stream_url(cmdline: str) -> str:
    match = _STREAM_URL_RE.search(str(cmdline or ""))
    return match.group(0).strip() if match else ""


def _mount_from_url(url: str) -> str:
    try:
        return str(urlparse(str(url or "")).path or "").strip().lstrip("/")
    except Exception:
        return ""


def _target_cmdline_token(target: str, mount: str = "") -> str:
    picked_mount = _sanitize_mount(mount) or _target_runtime(target).get("mount") or _sanitize_mount(DEFAULT_MOUNTS.get(target))
    return f"/{picked_mount}".lower() if picked_mount else ""


def _find_target_pid(target: str, mount: str = "", stream_url: str = "") -> Optional[int]:
    token = _target_cmdline_token(target, mount)
    expected_url = _normalize_stream_url(stream_url)
    try:
        entries = os.listdir("/proc")
    except Exception:
        return None
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        cmdline = _pid_cmdline(pid)
        if not cmdline or ("vlc" not in cmdline and "cvlc" not in cmdline):
            continue
        if expected_url:
            actual_url = _normalize_stream_url(_extract_stream_url(cmdline))
            if actual_url == expected_url:
                return pid
        if token and token in cmdline:
            return pid
    return None


def _probe_target_process(
    target: str,
    *,
    mount: str,
    stream_url: str,
    audio_sink: str,
    pid_hint: Optional[int] = None,
) -> dict:
    expected_mount = _sanitize_mount(mount) or _sanitize_mount(DEFAULT_MOUNTS.get(target) or PLAYER_MOUNT)
    expected_url = str(stream_url or "").strip() or _stream_url_for(target, expected_mount)
    expected_url_norm = _normalize_stream_url(expected_url)
    expected_sink = str(audio_sink or "").strip()

    pid = pid_hint if isinstance(pid_hint, int) and pid_hint > 1 else _read_pid(target)
    if pid and (not _pid_alive(pid) or not _is_vlc_pid(pid)):
        pid = None
    if not pid:
        pid = _find_target_pid(target, mount=expected_mount, stream_url=expected_url)
        if pid:
            try:
                _write_pid(target, pid)
            except Exception:
                logger.debug("Failed writing discovered VLC pid for %s", target, exc_info=True)
    if not pid:
        _clear_pid(target)
        return {
            "pid": None,
            "process_running": False,
            "verified": False,
            "actual_mount": "",
            "actual_stream_url": "",
            "actual_audio_sink": "",
            "error": "",
        }

    cmdline = _pid_cmdline(pid)
    if not cmdline or ("vlc" not in cmdline and "cvlc" not in cmdline):
        _clear_pid(target)
        return {
            "pid": None,
            "process_running": False,
            "verified": False,
            "actual_mount": "",
            "actual_stream_url": "",
            "actual_audio_sink": "",
            "error": "",
        }

    actual_stream_url = _extract_stream_url(cmdline)
    actual_stream_norm = _normalize_stream_url(actual_stream_url)
    actual_mount = _mount_from_url(actual_stream_url)
    actual_audio_sink = str(_pid_environ(pid).get("PULSE_SINK", "")).strip()

    mismatches = []
    if expected_url_norm and actual_stream_norm != expected_url_norm:
        mismatches.append(f"stream mismatch (expected {expected_url}, got {actual_stream_url or 'unknown'})")
    if expected_sink and actual_audio_sink != expected_sink:
        mismatches.append(f"audio sink mismatch (expected {expected_sink}, got {actual_audio_sink or 'default'})")
    verified = not mismatches
    return {
        "pid": pid,
        "process_running": True,
        "verified": verified,
        "actual_mount": actual_mount,
        "actual_stream_url": actual_stream_url,
        "actual_audio_sink": actual_audio_sink,
        "error": "; ".join(mismatches),
    }


def _refresh_target_status(target: str, mount: str = "", stream_url: str = "", audio_sink: str = "") -> dict:
    runtime = _target_runtime(target)
    expected_mount = _sanitize_mount(mount) or runtime.get("mount") or _sanitize_mount(DEFAULT_MOUNTS.get(target) or PLAYER_MOUNT)
    expected_stream_url = str(stream_url or "").strip() or runtime.get("stream_url") or _stream_url_for(target, expected_mount)
    expected_sink = str(audio_sink or runtime.get("audio_sink") or "").strip()
    probe = _probe_target_process(
        target,
        mount=expected_mount,
        stream_url=expected_stream_url,
        audio_sink=expected_sink,
    )
    return _status_from_probe(
        target,
        probe,
        mount=expected_mount,
        stream_url=expected_stream_url,
        audio_sink=expected_sink,
    )


def _mute_sdrtrunk_pulse_streams() -> None:
    """Mute local SDRTrunk ALSA sink inputs so UI VLC playback is the only audio path."""
    if not os.path.isfile(_LOCAL_MONITOR_SCRIPT):
        return
    env = _vlc_launch_env()
    env.setdefault("DIGITAL_LOCAL_MONITOR_WAIT_SEC", "1")
    env.setdefault("DIGITAL_LOCAL_MONITOR_POLL_SEC", "0.25")
    try:
        subprocess.run(
            [sys.executable or "python3", _LOCAL_MONITOR_SCRIPT, "apply"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            timeout=2.5,
            check=False,
        )
    except Exception:
        logger.debug("Failed muting SDRTrunk local monitor streams for VLC playback", exc_info=True)
        return


def start_vlc(stream_url: str = "", target: str = DEFAULT_TARGET, mount: str = ""):
    """Start target-scoped background VLC playback."""
    resolved_target = _normalize_target(target)
    if not resolved_target:
        return False, "invalid target"
    if mount and not _sanitize_mount(mount):
        return False, "invalid mount"

    desired_mount = _sanitize_mount(mount) or _sanitize_mount(DEFAULT_MOUNTS.get(resolved_target) or PLAYER_MOUNT)
    desired_stream_url = str(stream_url or "").strip() or _stream_url_for(resolved_target, desired_mount)
    desired_sink = _preferred_sink_name()

    current = _refresh_target_status(resolved_target, mount=desired_mount, stream_url=desired_stream_url, audio_sink=desired_sink)
    if current.get("running"):
        _mute_sdrtrunk_pulse_streams()
        return True, "already running"
    if current.get("process_running"):
        stop_ok, stop_err = stop_vlc(target=resolved_target)
        if not stop_ok:
            return False, stop_err or "failed stopping mismatched VLC instance"

    _prefer_configured_pulse_sink()
    _set_target_runtime(
        resolved_target,
        state="starting",
        pid=None,
        mount=desired_mount,
        stream_url=desired_stream_url,
        audio_sink=desired_sink,
        actual_mount="",
        actual_stream_url="",
        actual_audio_sink="",
        error="",
        process_running=False,
        verified=False,
    )

    gain = _VLC_GAINS.get(resolved_target, 1.0)
    cmd = [
        "cvlc",
        "--intf",
        "dummy",
        "--aout=pulse",
        "--quiet",
        "--no-video",
        "--clock-jitter=0",
        "--clock-synchro=0",
    ]
    if gain and gain != 1.0:
        cmd.extend(["--gain", str(gain)])
    if VLC_HTTP_RECONNECT:
        cmd.append("--http-reconnect")
    if VLC_NETWORK_CACHING_MS > 0:
        cmd.extend(["--network-caching", str(VLC_NETWORK_CACHING_MS)])
    cmd.append(desired_stream_url)

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_vlc_launch_env(),
            start_new_session=True,
        )
    except FileNotFoundError:
        _status_error(
            resolved_target,
            mount=desired_mount,
            stream_url=desired_stream_url,
            audio_sink=desired_sink,
            pid=None,
            error="cvlc not found",
            process_running=False,
        )
        return False, "cvlc not found"
    except Exception as exc:
        _status_error(
            resolved_target,
            mount=desired_mount,
            stream_url=desired_stream_url,
            audio_sink=desired_sink,
            pid=None,
            error=str(exc),
            process_running=False,
        )
        return False, str(exc)

    try:
        _write_pid(resolved_target, proc.pid)
    except Exception:
        logger.debug("Failed writing VLC pid for %s", resolved_target, exc_info=True)

    deadline = time.monotonic() + max(VLC_START_VERIFY_SEC, 0.0)
    while True:
        exit_code = proc.poll()
        if exit_code is not None:
            _clear_pid(resolved_target)
            _status_error(
                resolved_target,
                mount=desired_mount,
                stream_url=desired_stream_url,
                audio_sink=desired_sink,
                pid=None,
                error=f"cvlc exited immediately (code {exit_code})",
                process_running=False,
            )
            return False, f"cvlc exited immediately (code {exit_code})"

        probe = _probe_target_process(
            resolved_target,
            mount=desired_mount,
            stream_url=desired_stream_url,
            audio_sink=desired_sink,
            pid_hint=proc.pid,
        )
        if probe.get("verified"):
            _status_from_probe(
                resolved_target,
                probe,
                mount=desired_mount,
                stream_url=desired_stream_url,
                audio_sink=desired_sink,
            )
            _mute_sdrtrunk_pulse_streams()
            return True, ""
        if time.monotonic() >= deadline:
            error = str(probe.get("error") or "startup verification timed out")
            stop_vlc(target=resolved_target)
            _status_error(
                resolved_target,
                mount=desired_mount,
                stream_url=desired_stream_url,
                audio_sink=desired_sink,
                pid=probe.get("pid"),
                error=error,
                process_running=bool(probe.get("process_running")),
                actual_mount=probe.get("actual_mount", ""),
                actual_stream_url=probe.get("actual_stream_url", ""),
                actual_audio_sink=probe.get("actual_audio_sink", ""),
            )
            return False, error
        time.sleep(VLC_START_VERIFY_POLL_SEC)


def stop_vlc(target: str = DEFAULT_TARGET):
    """Stop target-scoped VLC playback."""
    resolved_target = _normalize_target(target)
    if not resolved_target:
        return False, "invalid target"

    runtime = _target_runtime(resolved_target)
    desired_mount = runtime.get("mount") or _sanitize_mount(DEFAULT_MOUNTS.get(resolved_target) or PLAYER_MOUNT)
    desired_stream_url = runtime.get("stream_url") or _stream_url_for(resolved_target, desired_mount)
    desired_sink = runtime.get("audio_sink") or _preferred_sink_name()
    probe = _probe_target_process(
        resolved_target,
        mount=desired_mount,
        stream_url=desired_stream_url,
        audio_sink=desired_sink,
    )
    pid = probe.get("pid")
    if not pid:
        _set_target_runtime(
            resolved_target,
            state="idle",
            pid=None,
            mount=desired_mount,
            stream_url=desired_stream_url,
            audio_sink=desired_sink,
            actual_mount="",
            actual_stream_url="",
            actual_audio_sink="",
            error="",
            process_running=False,
            verified=False,
        )
        return True, ""

    _set_target_runtime(
        resolved_target,
        state="stopping",
        pid=pid,
        mount=desired_mount,
        stream_url=desired_stream_url,
        audio_sink=desired_sink,
        actual_mount=probe.get("actual_mount", ""),
        actual_stream_url=probe.get("actual_stream_url", ""),
        actual_audio_sink=probe.get("actual_audio_sink", ""),
        error="",
        process_running=True,
        verified=bool(probe.get("verified")),
    )

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_pid(resolved_target)
        _set_target_runtime(
            resolved_target,
            state="idle",
            pid=None,
            mount=desired_mount,
            stream_url=desired_stream_url,
            audio_sink=desired_sink,
            actual_mount="",
            actual_stream_url="",
            actual_audio_sink="",
            error="",
            process_running=False,
            verified=False,
        )
        return True, ""
    except Exception as exc:
        _status_error(
            resolved_target,
            mount=desired_mount,
            stream_url=desired_stream_url,
            audio_sink=desired_sink,
            pid=pid,
            error=str(exc),
            process_running=True,
            actual_mount=probe.get("actual_mount", ""),
            actual_stream_url=probe.get("actual_stream_url", ""),
            actual_audio_sink=probe.get("actual_audio_sink", ""),
        )
        return False, str(exc)

    deadline = time.monotonic() + VLC_STOP_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            _clear_pid(resolved_target)
            _set_target_runtime(
                resolved_target,
                state="idle",
                pid=None,
                mount=desired_mount,
                stream_url=desired_stream_url,
                audio_sink=desired_sink,
                actual_mount="",
                actual_stream_url="",
                actual_audio_sink="",
                error="",
                process_running=False,
                verified=False,
            )
            return True, ""
        time.sleep(0.05)

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception as exc:
        _status_error(
            resolved_target,
            mount=desired_mount,
            stream_url=desired_stream_url,
            audio_sink=desired_sink,
            pid=pid,
            error=str(exc),
            process_running=True,
            actual_mount=probe.get("actual_mount", ""),
            actual_stream_url=probe.get("actual_stream_url", ""),
            actual_audio_sink=probe.get("actual_audio_sink", ""),
        )
        return False, str(exc)

    _clear_pid(resolved_target)
    _set_target_runtime(
        resolved_target,
        state="idle",
        pid=None,
        mount=desired_mount,
        stream_url=desired_stream_url,
        audio_sink=desired_sink,
        actual_mount="",
        actual_stream_url="",
        actual_audio_sink="",
        error="",
        process_running=False,
        verified=False,
    )
    return True, ""


def restart_vlc(target: str = DEFAULT_TARGET, mount: str = ""):
    """Restart target-scoped VLC playback with explicit stop completion and startup verification."""
    stop_ok, stop_err = stop_vlc(target=target)
    if not stop_ok:
        return False, stop_err
    return start_vlc(target=target, mount=mount)


def vlc_running(target: str = "") -> bool:
    """Check verified VLC playback status for a target or for any target."""
    if target:
        resolved_target = _normalize_target(target)
        if not resolved_target:
            return False
        return bool(_refresh_target_status(resolved_target).get("running"))
    return any(bool(_refresh_target_status(name).get("running")) for name in VLC_TARGETS)


def vlc_status() -> dict:
    """Return structured playback status per target."""
    return {name: _refresh_target_status(name) for name in VLC_TARGETS}
