#!/usr/bin/env python3
"""Mix RTL-Airband (analog) + SDRTrunk (digital) into a single Icecast mount."""
import json
import os
import shlex
import socket
import subprocess
import time
import urllib.request
import urllib.error


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


DIGITAL_MIXER_ENABLED = env_flag("DIGITAL_MIXER_ENABLED", False)
POLL_SEC = float(os.getenv("DIGITAL_MIXER_POLL_SEC", "2.0"))
MUTE_STATE_PATH = os.getenv("DIGITAL_MUTE_STATE_PATH", "/run/airband_ui_digital_mute.json")
FAILS_TO_DOWN = int(os.getenv("DIGITAL_MIXER_FAILS_TO_DOWN", "3"))
SUCCESSES_TO_UP = int(os.getenv("DIGITAL_MIXER_SUCCESSES_TO_UP", "2"))
CHECK_BYTES = int(os.getenv("DIGITAL_MIXER_CHECK_BYTES", "4096"))
CHECK_TIMEOUT = float(os.getenv("DIGITAL_MIXER_CHECK_TIMEOUT", "1.8"))
FFMPEG_RW_TIMEOUT = int(os.getenv("DIGITAL_MIXER_RW_TIMEOUT", "8000000"))  # microseconds
FFMPEG_ANALYZE_DURATION = int(os.getenv("DIGITAL_MIXER_ANALYZE_DURATION", "2000000"))
FFMPEG_PROBE_SIZE = int(os.getenv("DIGITAL_MIXER_PROBE_SIZE", "2000000"))
FFMPEG_USER_AGENT = os.getenv("DIGITAL_MIXER_USER_AGENT", "scannerproject-digital-mixer/1.0")
FFMPEG_RECONNECT_ON_NETWORK_ERROR = env_flag("DIGITAL_MIXER_RECONNECT_ON_NETWORK_ERROR", True)
FFMPEG_RECONNECT_ON_HTTP_ERROR = os.getenv("DIGITAL_MIXER_RECONNECT_ON_HTTP_ERROR", "4xx,5xx").strip()

ICECAST_HOST = os.getenv("ICECAST_HOST", "127.0.0.1")
ICECAST_PORT = int(os.getenv("ICECAST_PORT", "8000"))
ICECAST_USER = os.getenv("ICECAST_SOURCE_USER", "source")
ICECAST_PASSWORD = os.getenv("ICECAST_SOURCE_PASSWORD", "062352")

AIRBAND_MOUNT = os.getenv("DIGITAL_MIXER_AIRBAND_MOUNT", "GND-air.mp3").lstrip("/")
DIGITAL_MOUNT = os.getenv("DIGITAL_MIXER_DIGITAL_MOUNT", "DIGITAL.mp3").lstrip("/")
OUTPUT_MOUNT = os.getenv(
    "DIGITAL_MIXER_OUTPUT_MOUNT",
    os.getenv("MOUNT_NAME", "GND.mp3"),
).lstrip("/")

AIRBAND_URL = os.getenv(
    "DIGITAL_MIXER_AIRBAND_URL",
    f"http://{ICECAST_HOST}:{ICECAST_PORT}/{AIRBAND_MOUNT}",
)
DIGITAL_URL = os.getenv(
    "DIGITAL_MIXER_DIGITAL_URL",
    f"http://{ICECAST_HOST}:{ICECAST_PORT}/{DIGITAL_MOUNT}",
)
OUTPUT_URL = os.getenv(
    "DIGITAL_MIXER_OUTPUT_URL",
    f"icecast://{ICECAST_USER}:{ICECAST_PASSWORD}@{ICECAST_HOST}:{ICECAST_PORT}/{OUTPUT_MOUNT}",
)

SAMPLE_RATE = int(os.getenv("DIGITAL_MIXER_SAMPLE_RATE", "44100"))
CHANNELS = int(os.getenv("DIGITAL_MIXER_CHANNELS", "1"))
BITRATE = int(os.getenv("DIGITAL_MIXER_BITRATE", "32"))
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "/usr/bin/ffmpeg")


def read_muted() -> bool:
    try:
        with open(MUTE_STATE_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return bool(payload.get("muted"))
    except Exception:
        return False


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} digital-mixer: {msg}")


def _mp3_sync_present(data: bytes) -> bool:
    if not data:
        return False
    for i in range(len(data) - 1):
        if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:
            return True
    return False


def stream_ok(url: str) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(
            url,
            headers={"Icy-MetaData": "1", "User-Agent": FFMPEG_USER_AGENT},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=CHECK_TIMEOUT) as resp:
            status = getattr(resp, "status", 200)
            if status and status >= 400:
                return False, f"http {status}"
            data = resp.read(CHECK_BYTES)
        if not data:
            return False, "empty read"
        if _mp3_sync_present(data):
            return True, "mp3 sync"
        return True, "data read"
    except urllib.error.HTTPError as e:
        return False, f"http {e.code}"
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), socket.timeout):
            return False, "timeout"
        return False, str(getattr(e, "reason", e))
    except socket.timeout:
        return False, "timeout"
    except Exception as e:
        return False, str(e)


class InputHealth:
    def __init__(self, name: str):
        self.name = name
        self.is_up = False
        self.successes = 0
        self.failures = 0

    def update(self, ok: bool, reason: str = "") -> bool:
        changed = False
        if ok:
            self.successes += 1
            self.failures = 0
            if not self.is_up and self.successes >= SUCCESSES_TO_UP:
                self.is_up = True
                changed = True
                _log(f"{self.name} input UP ({reason})")
        else:
            self.failures += 1
            self.successes = 0
            if self.is_up and self.failures >= FAILS_TO_DOWN:
                self.is_up = False
                changed = True
                _log(f"{self.name} input DOWN ({reason})")
        return changed


def build_cmd(airband_ok: bool, digital_ok: bool) -> list:
    cmd = [FFMPEG_BIN, "-nostdin", "-hide_banner", "-loglevel", "warning"]

    def add_http_input(url: str) -> None:
        cmd.extend([
            "-rw_timeout", str(FFMPEG_RW_TIMEOUT),
            "-analyzeduration", str(FFMPEG_ANALYZE_DURATION),
            "-probesize", str(FFMPEG_PROBE_SIZE),
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_at_eof", "1",
            "-reconnect_on_network_error", "1" if FFMPEG_RECONNECT_ON_NETWORK_ERROR else "0",
            "-reconnect_delay_max", "2",
            "-method", "GET",
            "-user_agent", FFMPEG_USER_AGENT,
        ])
        if FFMPEG_RECONNECT_ON_HTTP_ERROR:
            cmd.extend(["-reconnect_on_http_error", FFMPEG_RECONNECT_ON_HTTP_ERROR])
        cmd.extend(["-i", url])

    def add_silence() -> None:
        cmd.extend([
            "-f", "lavfi",
            "-i", f"anullsrc=channel_layout=mono:sample_rate={SAMPLE_RATE}",
        ])

    if airband_ok:
        add_http_input(AIRBAND_URL)
    else:
        add_silence()

    if digital_ok:
        add_http_input(DIGITAL_URL)
    else:
        add_silence()

    mix_filter = "[0:a][1:a]amix=inputs=2:duration=longest:dropout_transition=2"
    cmd.extend([
        "-filter_complex", mix_filter,
        "-ac", str(CHANNELS),
        "-ar", str(SAMPLE_RATE),
        "-c:a", "libmp3lame",
        "-b:a", f"{BITRATE}k",
        "-content_type", "audio/mpeg",
        "-f", "mp3",
        OUTPUT_URL,
    ])
    return cmd


def stop_proc(proc) -> None:
    if not proc:
        return
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except Exception:
        proc.kill()


def main() -> int:
    if not DIGITAL_MIXER_ENABLED:
        print("digital-mixer: DIGITAL_MIXER_ENABLED is false; exiting.")
        return 0
    if AIRBAND_MOUNT == OUTPUT_MOUNT:
        print("digital-mixer: AIRBAND mount matches output mount; refusing to start.")
        return 1

    proc = None
    last_state = None
    airband_health = InputHealth("airband")
    digital_health = InputHealth("digital")
    try:
        while True:
            muted = read_muted()
            air_ok, air_reason = stream_ok(AIRBAND_URL)
            dig_ok, dig_reason = stream_ok(DIGITAL_URL) if not muted else (False, "muted")

            air_changed = airband_health.update(air_ok, air_reason)
            dig_changed = digital_health.update(dig_ok, dig_reason)

            airband_ok = airband_health.is_up
            digital_ok = digital_health.is_up and not muted
            state = (airband_ok, digital_ok, muted)

            needs_restart = False
            if proc is None or proc.poll() is not None:
                needs_restart = True
            if state != last_state:
                needs_restart = True
            if air_changed or dig_changed:
                needs_restart = True

            if needs_restart:
                stop_proc(proc)
                cmd = build_cmd(airband_ok, digital_ok)
                label = f"airband={'ok' if airband_ok else 'silence'} digital={'ok' if digital_ok else 'silence'} muted={muted}"
                _log(f"starting ffmpeg ({label})")
                _log(f"ffmpeg cmd: {shlex.join(cmd)}")
                proc = subprocess.Popen(cmd)
                last_state = state

            time.sleep(POLL_SEC)
    except KeyboardInterrupt:
        pass
    finally:
        stop_proc(proc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
