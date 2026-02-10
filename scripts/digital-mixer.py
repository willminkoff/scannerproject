#!/usr/bin/env python3
"""Mix RTL-Airband (analog) + SDRTrunk (digital) into a single Icecast mount."""
import json
import os
import subprocess
import time
import urllib.request


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


DIGITAL_MIXER_ENABLED = env_flag("DIGITAL_MIXER_ENABLED", False)
POLL_SEC = float(os.getenv("DIGITAL_MIXER_POLL_SEC", "2.0"))
MUTE_STATE_PATH = os.getenv("DIGITAL_MUTE_STATE_PATH", "/run/airband_ui_digital_mute.json")

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


def stream_ok(url: str, timeout: float = 1.2) -> bool:
    try:
        req = urllib.request.Request(url, headers={"Icy-MetaData": "1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(1)
        return True
    except Exception:
        return False


def build_cmd(airband_ok: bool, digital_ok: bool) -> list:
    cmd = [FFMPEG_BIN, "-nostdin", "-hide_banner", "-loglevel", "warning"]

    def add_http_input(url: str) -> None:
        cmd.extend([
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_at_eof", "1",
            "-reconnect_delay_max", "2",
            "-i", url,
        ])

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
    try:
        while True:
            muted = read_muted()
            airband_ok = stream_ok(AIRBAND_URL)
            digital_ok = (not muted) and stream_ok(DIGITAL_URL)
            state = (airband_ok, digital_ok, muted)

            needs_restart = False
            if proc is None or proc.poll() is not None:
                needs_restart = True
            if state != last_state:
                needs_restart = True

            if needs_restart:
                stop_proc(proc)
                cmd = build_cmd(airband_ok, digital_ok)
                label = f"airband={'ok' if airband_ok else 'silence'} digital={'ok' if digital_ok else 'silence'} muted={muted}"
                print(f"digital-mixer: starting ffmpeg ({label})")
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
