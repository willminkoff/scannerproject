#!/usr/bin/env python3
"""OP25 supervisor — Phase C reliability.

Wraps multi_rx.py with pre-flight + startup + runtime watchdogs to defeat
the "active but flowgraph never produced output" wedge pattern.

Pre-flight (before spawn):
  - Verify every ``serial=`` referenced in multi_rx.json is enumerated in
    /sys/bus/usb/devices.  Missing serial -> exit 100, systemd restarts
    fast, watchdog escalates to safe_restart_rtl_airband.

Startup watchdog (after spawn, first OP25_STARTUP_DEADLINE_SEC s):
  - Monitor /var/log/op25/op25.log mtime.  If multi_rx.py doesn't write
    anything in the deadline, kill it and exit 100.  This catches the
    "stuck at sdrplay_api_Open" pattern within ~30 s instead of letting
    the process spin forever.

Runtime watchdog (post-startup):
  - If the log stays silent for OP25_RUNTIME_SILENT_DEADLINE_SEC s (default
    10 min), kill multi_rx.py and exit 100.  Real OP25 is event-driven
    but never goes truly silent for that long during the day.

Signal forwarding: SIGINT/SIGTERM are passed through so systemd's
graceful shutdown reaches multi_rx.py.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

CONFIG_PATH = os.environ.get("OP25_CONFIG", "/run/scannerproject/op25/multi_rx.json")
LOG_PATH = os.environ.get("OP25_LOG", "/var/log/op25/op25.log")
MULTI_RX_PATH = os.environ.get(
    "OP25_MULTI_RX_PATH",
    "/opt/op25/op25/gr-op25_repeater/apps/multi_rx.py",
)
STARTUP_DEADLINE_SEC = float(os.environ.get("OP25_STARTUP_DEADLINE_SEC", "30"))
RUNTIME_SILENT_DEADLINE_SEC = float(os.environ.get(
    "OP25_RUNTIME_SILENT_DEADLINE_SEC", "600"
))


def _log(msg: str) -> None:
    print(f"[op25-supervisor] {msg}", file=sys.stderr, flush=True)


def preflight() -> None:
    """Verify SDR serials in multi_rx.json are USB-enumerated."""
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except (OSError, ValueError) as exc:
        _log(f"config not readable: {exc}; exiting fast")
        sys.exit(100)
    needed_serials: set[str] = set()
    for dev in cfg.get("devices", []) or []:
        args = dev.get("args", "")
        m = re.search(r"serial=([A-Za-z0-9_-]+)", args)
        if m:
            needed_serials.add(m.group(1))
    if not needed_serials:
        _log("no serials in config; skipping enumeration check")
        return
    enumerated: set[str] = set()
    base = "/sys/bus/usb/devices"
    if os.path.isdir(base):
        for entry in os.listdir(base):
            try:
                with open(os.path.join(base, entry, "serial")) as f:
                    enumerated.add(f.read().strip())
            except OSError:
                continue
    missing = needed_serials - enumerated
    if missing:
        _log(
            f"SDR serial(s) NOT enumerated in /sys: {sorted(missing)}; "
            f"exiting fast (likely needs physical reconnect)"
        )
        sys.exit(100)
    _log(f"pre-flight OK (serials enumerated: {sorted(needed_serials)})")


def main() -> int:
    preflight()
    env = os.environ.copy()
    apps_dir = os.path.dirname(MULTI_RX_PATH)
    tdma_dir = os.path.join(apps_dir, "tdma")
    env["PYTHONPATH"] = ":".join(filter(None, [
        apps_dir, tdma_dir, env.get("PYTHONPATH", "")
    ]))
    cmd = [
        "python3", MULTI_RX_PATH,
        "-c", CONFIG_PATH,
        "-v", "1",
    ]
    _log(f"spawning multi_rx.py: {cmd}")
    proc = subprocess.Popen(cmd, env=env, cwd=apps_dir)

    def _forward(signo, _frame):
        try:
            proc.send_signal(signo)
        except ProcessLookupError:
            pass
    signal.signal(signal.SIGTERM, _forward)
    signal.signal(signal.SIGINT, _forward)

    started_at = time.time()
    try:
        initial_mtime = os.path.getmtime(LOG_PATH)
    except OSError:
        initial_mtime = 0.0

    saw_startup_progress = threading.Event()

    def watchdog() -> None:
        nonlocal_init = initial_mtime
        while proc.poll() is None:
            now = time.time()
            try:
                mtime = os.path.getmtime(LOG_PATH)
            except OSError:
                mtime = 0.0
            if not saw_startup_progress.is_set():
                if mtime > nonlocal_init + 0.5:
                    saw_startup_progress.set()
                    _log(f"startup progress observed at {now - started_at:.1f}s")
                elif now - started_at > STARTUP_DEADLINE_SEC:
                    _log(
                        f"startup deadline {STARTUP_DEADLINE_SEC:.0f}s missed — "
                        f"killing multi_rx.py and exiting fast"
                    )
                    proc.kill()
                    # Ensure systemd sees the non-zero exit via os._exit
                    # since main thread's proc.wait() will return SIGKILL.
                    os._exit(100)
            else:
                age = now - mtime if mtime else 0
                if age > RUNTIME_SILENT_DEADLINE_SEC:
                    _log(
                        f"runtime log silent for {age:.0f}s "
                        f"(> {RUNTIME_SILENT_DEADLINE_SEC:.0f}s) — "
                        f"killing multi_rx.py"
                    )
                    proc.kill()
                    os._exit(100)
            time.sleep(2.0)
    threading.Thread(target=watchdog, daemon=True, name="op25-watchdog").start()

    rc = proc.wait()
    _log(f"multi_rx.py exited with status {rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
