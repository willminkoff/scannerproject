"""VLC playback control for local Icecast streams."""
import os
import re
import signal
import subprocess
import time
from typing import Optional

try:
    from .config import ICECAST_HOST, ICECAST_PORT, PLAYER_MOUNT, DIGITAL_STREAM_MOUNT
except ImportError:
    from ui.config import ICECAST_HOST, ICECAST_PORT, PLAYER_MOUNT, DIGITAL_STREAM_MOUNT

VLC_HTTP_RECONNECT = str(os.getenv("VLC_HTTP_RECONNECT", "1")).strip().lower() in ("1", "true", "yes", "on")
try:
    VLC_NETWORK_CACHING_MS = max(0, int(str(os.getenv("VLC_NETWORK_CACHING_MS", "1000")).strip()))
except Exception:
    VLC_NETWORK_CACHING_MS = 1000

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

VLC_TARGETS = ("analog", "digital")
DEFAULT_TARGET = "analog"
DEFAULT_MOUNTS = {
    "analog": PLAYER_MOUNT,
    "digital": DIGITAL_STREAM_MOUNT or PLAYER_MOUNT,
}

_MOUNT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


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
        pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _is_vlc_pid(pid: int) -> bool:
    cmdline = _pid_cmdline(pid)
    return bool(cmdline and ("vlc" in cmdline or "cvlc" in cmdline))


def _pid_cmdline(pid: int) -> str:
    if not isinstance(pid, int) or pid <= 1:
        return ""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except Exception:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore").lower()


def _target_cmdline_token(target: str) -> str:
    mount = str(DEFAULT_MOUNTS.get(target) or PLAYER_MOUNT or "").strip().lstrip("/").lower()
    return f"/{mount}" if mount else ""


def _find_target_pid(target: str) -> Optional[int]:
    token = _target_cmdline_token(target)
    if not token:
        return None
    proc_dir = "/proc"
    try:
        entries = os.listdir(proc_dir)
    except Exception:
        return None
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        cmdline = _pid_cmdline(pid)
        if not cmdline:
            continue
        if ("vlc" in cmdline or "cvlc" in cmdline) and token in cmdline:
            return pid
    return None


def _target_running(target: str) -> bool:
    pid = _read_pid(target)
    if pid and _pid_alive(pid) and _is_vlc_pid(pid):
        return True
    if pid:
        _clear_pid(target)
    recovered_pid = _find_target_pid(target)
    if not recovered_pid:
        return False
    try:
        _write_pid(target, recovered_pid)
    except Exception:
        pass
    return True


def _is_sdrtrunk_java_pid(pid: str) -> bool:
    if not pid or not pid.isdigit():
        return False
    cmdline_path = f"/proc/{pid}/cmdline"
    try:
        with open(cmdline_path, "rb") as f:
            raw = f.read()
    except Exception:
        return False
    if not raw:
        return False
    cmdline = raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore")
    return "sdrtrunk" in cmdline.lower()


def _mute_sdrtrunk_pulse_streams() -> None:
    """Mute local SDRTrunk ALSA sink inputs so UI VLC playback is the only audio path."""
    try:
        result = subprocess.run(
            ["pactl", "list", "sink-inputs"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except Exception:
        return
    if result.returncode != 0 or not result.stdout:
        return

    sink_id = ""
    app_name = ""
    proc_binary = ""
    proc_pid = ""
    to_mute = []

    def flush_current():
        nonlocal sink_id, app_name, proc_binary, proc_pid
        if not sink_id:
            return
        is_java = proc_binary.lower() == "java" or "java" in app_name.lower()
        if is_java and _is_sdrtrunk_java_pid(proc_pid):
            to_mute.append(sink_id)
        sink_id = ""
        app_name = ""
        proc_binary = ""
        proc_pid = ""

    for raw in result.stdout.splitlines():
        line = raw.strip()
        if line.startswith("Sink Input #"):
            flush_current()
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
    flush_current()

    for sid in sorted(set(to_mute)):
        try:
            subprocess.run(
                ["pactl", "set-sink-input-mute", sid, "1"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            continue


def start_vlc(stream_url: str = "", target: str = DEFAULT_TARGET, mount: str = ""):
    """Start target-scoped background VLC playback."""
    resolved_target = _normalize_target(target)
    if not resolved_target:
        return False, "invalid target"
    if mount and not _sanitize_mount(mount):
        return False, "invalid mount"
    url = _stream_url_for(resolved_target, mount) if mount else (str(stream_url or "").strip() or _stream_url_for(resolved_target))

    if _target_running(resolved_target):
        _mute_sdrtrunk_pulse_streams()
        return True, "already running"
    cmd = ["cvlc", "--intf", "dummy", "--aout=pulse", "--quiet"]
    if VLC_HTTP_RECONNECT:
        cmd.append("--http-reconnect")
    if VLC_NETWORK_CACHING_MS > 0:
        cmd.extend(["--network-caching", str(VLC_NETWORK_CACHING_MS)])
    cmd.append(url)
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_vlc_launch_env(),
            start_new_session=True,
        )
        _write_pid(resolved_target, proc.pid)
        _mute_sdrtrunk_pulse_streams()
        return True, ""
    except FileNotFoundError:
        return False, "cvlc not found"
    except Exception as e:
        return False, str(e)


def stop_vlc(target: str = DEFAULT_TARGET):
    """Stop target-scoped VLC playback."""
    resolved_target = _normalize_target(target)
    if not resolved_target:
        return False, "invalid target"

    pid = _read_pid(resolved_target)
    if not pid:
        pid = _find_target_pid(resolved_target)
        if pid:
            try:
                _write_pid(resolved_target, pid)
            except Exception:
                pass
    if not pid:
        return True, ""
    if not _pid_alive(pid):
        _clear_pid(resolved_target)
        return True, ""
    if not _is_vlc_pid(pid):
        _clear_pid(resolved_target)
        return False, "pid is not vlc"

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_pid(resolved_target)
        return True, ""
    except Exception as e:
        return False, str(e)

    deadline = time.time() + VLC_STOP_TIMEOUT_SEC
    while time.time() < deadline:
        if not _pid_alive(pid):
            _clear_pid(resolved_target)
            return True, ""
        time.sleep(0.05)

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception as e:
        return False, str(e)
    _clear_pid(resolved_target)
    return True, ""


def vlc_running(target: str = "") -> bool:
    """Check VLC process status for a target or for any target."""
    if target:
        resolved_target = _normalize_target(target)
        if not resolved_target:
            return False
        return _target_running(resolved_target)
    return any(_target_running(name) for name in VLC_TARGETS)


def vlc_status() -> dict:
    """Return running status per playback target."""
    return {name: _target_running(name) for name in VLC_TARGETS}
