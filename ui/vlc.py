"""VLC playback control for the local Icecast stream."""
import subprocess

STREAM_URL = "http://localhost:8000/GND.mp3"


def start_vlc(stream_url: str = STREAM_URL):
    """Start background VLC playback."""
    if vlc_running():
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
