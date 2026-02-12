#!/usr/bin/env python3
"""Run a resilient Liquidsoap mixer for analog + digital scanner audio."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Optional


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
OUTPUT_MOUNT = os.getenv("DIGITAL_MIXER_OUTPUT_MOUNT", "scannerbox.mp3").lstrip("/")

AIRBAND_URL = os.getenv(
    "DIGITAL_MIXER_AIRBAND_URL",
    f"http://{ICECAST_HOST}:{ICECAST_PORT}/{AIRBAND_MOUNT}",
)
DIGITAL_URL = os.getenv(
    "DIGITAL_MIXER_DIGITAL_URL",
    f"http://{ICECAST_HOST}:{ICECAST_PORT}/{DIGITAL_MOUNT}",
)

SAMPLE_RATE = int(os.getenv("DIGITAL_MIXER_SAMPLE_RATE", "44100"))
CHANNELS = int(os.getenv("DIGITAL_MIXER_CHANNELS", "1"))
BITRATE = int(os.getenv("DIGITAL_MIXER_BITRATE", "32"))
LIQUIDSOAP_BIN = os.getenv("LIQUIDSOAP_BIN", "/usr/bin/liquidsoap")
LIQUIDSOAP_SCRIPT_PATH = os.getenv("DIGITAL_MIXER_LIQ_PATH", "/run/scanner-digital-mixer.liq")
LIQUIDSOAP_QUIET = env_flag("DIGITAL_MIXER_LIQ_QUIET", True)


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} digital-mixer: {msg}", flush=True)


def read_muted() -> bool:
    try:
        with open(MUTE_STATE_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return bool(payload.get("muted"))
    except Exception:
        return False


def _liq_quote(value: str) -> str:
    escaped = (value or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _liq_bool(value: bool) -> str:
    return "true" if value else "false"


def _build_liq_script(muted: bool) -> str:
    output_channels = 2 if CHANNELS >= 2 else 1
    # Keep HTTP inputs on the shared main clock; per-input mksafe wrappers
    # spawn extra safe_blank clocks and can trigger sync-source conflicts.
    air_input = f"input.http(self_sync=false,{_liq_quote(AIRBAND_URL)})"
    digital_input = "blank()" if muted else f"input.http(self_sync=false,{_liq_quote(DIGITAL_URL)})"
    return "\n".join(
        [
            "#!/usr/bin/liquidsoap",
            "set(\"log.stdout\",true)",
            "set(\"server.telnet\",false)",
            "settings.init.allow_root := true",
            "",
            "def with_silence(src) =",
            "  fallback(track_sensitive=false,[src,blank()])",
            "end",
            "",
            f"air_source = with_silence({air_input})",
            f"digital_source = with_silence({digital_input})",
            "mixed = add(normalize=false,[air_source,digital_source])",
            "",
            "output.icecast(",
            f"  %mp3(bitrate={BITRATE},samplerate={SAMPLE_RATE},stereo={_liq_bool(output_channels == 2)}),",
            f"  host={_liq_quote(ICECAST_HOST)},",
            f"  port={ICECAST_PORT},",
            f"  user={_liq_quote(ICECAST_USER)},",
            f"  password={_liq_quote(ICECAST_PASSWORD)},",
            f"  mount={_liq_quote('/' + OUTPUT_MOUNT)},",
            "  fallible=true,",
            "  name=\"SprontPi ScannerBox\",",
            "  description=\"Mixed analog + digital\",",
            "  genre=\"Scanner\",",
            "  mixed",
            ")",
            "",
        ]
    )


def _write_liq_script(script_text: str) -> None:
    path = Path(LIQUIDSOAP_SCRIPT_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(script_text, encoding="utf-8")
    os.replace(tmp, path)


def _stop_proc(proc: Optional[subprocess.Popen]) -> None:
    if proc is None:
        return
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


def _build_cmd() -> list[str]:
    cmd = [LIQUIDSOAP_BIN]
    if LIQUIDSOAP_QUIET:
        cmd.append("-q")
    cmd.append(LIQUIDSOAP_SCRIPT_PATH)
    return cmd


def main() -> int:
    if not DIGITAL_MIXER_ENABLED:
        _log("DIGITAL_MIXER_ENABLED is false; exiting")
        return 0
    if AIRBAND_MOUNT == OUTPUT_MOUNT or DIGITAL_MOUNT == OUTPUT_MOUNT:
        _log("input mount matches output mount; refusing to start")
        return 1
    if not OUTPUT_MOUNT:
        _log("DIGITAL_MIXER_OUTPUT_MOUNT is empty; refusing to start")
        return 1
    if not os.path.exists(LIQUIDSOAP_BIN):
        _log(f"liquidsoap binary not found at {LIQUIDSOAP_BIN}")
        return 1

    proc: Optional[subprocess.Popen] = None
    last_muted: Optional[bool] = None

    try:
        while True:
            muted = read_muted()
            restart_reason = ""
            if proc is None:
                restart_reason = "initial start"
            elif proc.poll() is not None:
                restart_reason = f"liquidsoap exited rc={proc.returncode}"
            elif last_muted is None or muted != last_muted:
                restart_reason = f"mute changed to {muted}"

            if restart_reason:
                _stop_proc(proc)
                script_text = _build_liq_script(muted)
                _write_liq_script(script_text)
                cmd = _build_cmd()
                _log(f"starting liquidsoap ({restart_reason}; muted={muted})")
                _log(f"liquidsoap cmd: {shlex.join(cmd)}")
                proc = subprocess.Popen(cmd)
                last_muted = muted

            time.sleep(POLL_SEC)
    except KeyboardInterrupt:
        pass
    finally:
        _stop_proc(proc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
