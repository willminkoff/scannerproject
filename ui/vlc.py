"""VLC playback control for the local Icecast stream."""
import subprocess

try:
    from .config import ICECAST_HOST, ICECAST_PORT, PLAYER_MOUNT
except ImportError:
    from ui.config import ICECAST_HOST, ICECAST_PORT, PLAYER_MOUNT

STREAM_URL = f"http://{ICECAST_HOST}:{ICECAST_PORT}/{PLAYER_MOUNT}"


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


def start_vlc(stream_url: str = STREAM_URL):
    """Start background VLC playback."""
    if vlc_running():
        _mute_sdrtrunk_pulse_streams()
        return True, "already running"
    cmd = ["cvlc", "--intf", "dummy", "--aout=pulse", "--quiet", stream_url]
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _mute_sdrtrunk_pulse_streams()
        return True, ""
    except FileNotFoundError:
        return False, "cvlc not found"
    except Exception as e:
        return False, str(e)


def stop_vlc():
    """Stop VLC playback."""
    try:
        result = subprocess.run(
            ["pkill", "vlc"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode in (0, 1):
            return True, ""
        return False, f"pkill returned {result.returncode}"
    except FileNotFoundError:
        return False, "pkill not found"
    except Exception as e:
        return False, str(e)


def vlc_running() -> bool:
    """Check if VLC is running."""
    try:
        if subprocess.run(
            ["pgrep", "-x", "vlc"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0:
            return True
        if subprocess.run(
            ["pgrep", "-x", "cvlc"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0:
            return True
    except FileNotFoundError:
        return False
    return False
