#!/usr/bin/env python3
"""Keep Neptune's ground chirp producing audio by recovering its SDRplay radio.

Why this exists
---------------
On 2026-10-16 ground started dying every ~25-30 minutes. The macOS kernel
log shows exactly why, ten seconds before each death:

    AppleUSBIORequest::complete: device 10 (IOUSBHostDevice@02200000)
        endpoint 0x81: status 0xe00002ed (transaction error)
    AppleEmbeddedUSBXHCIFL1100@02000000: ... completed with 0xe0005000

The RSP1B's sample endpoint stops responding at the USB layer. When that
happens chirp does not crash -- its flowgraph simply stops receiving
samples, the icecast mount drops, and the daemon sits "alive but useless".
After that the radio stays wedged: restarting chirp, or even the
sdrplay_apiService, gives ``activateStream() Init() failed:
sdrplay_api_Fail`` on every attempt. Only re-enumerating the USB device
brings it back -- which is what a hand on the cable was doing.

``usb-reenumerate`` (macos/usb-reenumerate/) does that from software, and
this supervisor runs the recovery that was proven by hand:

    stop chirp-ground (+ any other SDRplay consumers)
    stop sdrplay_apiService            -- release the device
    usb-reenumerate <serial>           -- software unplug / replug
    start sdrplay_apiService, settle
    start chirp-ground (+ restore the others)

What it replaces
----------------
chirp-mount-watchdog used to ``kickstart -k`` chirp-ground every time its
mount was 404, every 30 s, forever. That could never fix a wedged radio
(it never re-enumerates or resets the API), and its restart churn is what
knocked the SDRplay API over on 2026-10-14/15. The watchdog no longer
handles ground; this does, with a rolling rate limit and exponential
backoff so it cannot churn.

Root cause is still hardware: every USB error since 2026-10-12 is on the
FL1100 controller that drives the M1 mini's USB-A ports. The radios belong
on the USB-C ports (Apple's own controller). This supervisor keeps ground
alive in the meantime, and keeps it alive afterwards if anything else goes
wrong -- it is defence in depth, not a substitute for the cable move.

Liveness signal
---------------
``chirp_audio_bytes_published_total`` from ground's metrics endpoint. It
climbs ~4 KB/s (the 32 kbps MP3) even when every channel is squelched,
because chirp publishes silence. If it stops climbing, the stream is dead.

Runs as a one-shot every 30 s (launchd StartInterval); state persists in a
JSON file between runs.

Env (all optional):
  HEALER_STATE_PATH        state file (default ~/Library/Application Support/scannerproject/sdrplay-healer.json)
  HEALER_METRICS_URL       ground metrics (default http://127.0.0.1:9102/metrics)
  HEALER_SDR_SERIAL        radio serial (default: parsed from ~/.config/sb3/chirp-ground.env)
  HEALER_USB_REENUMERATE   tool path (default <repo>/macos/usb-reenumerate/usb-reenumerate)
  HEALER_RESTORE_OTHERS    1/0: restart ACARS/VDL2 after recovery if they were running (default 1)
  HEALER_DRY_RUN           1: decide and log, but take no action
"""

from __future__ import annotations

import fcntl
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

GROUND_LABEL = "com.scannerproject.chirp-ground"
SDRPLAY_LABEL = "com.sdrplay.service"
# Other processes that hold an SDRplay device. They have to come down while
# the API service is restarted, and go back up afterwards.
OTHER_SDRPLAY_LABELS = (
    "com.scannerproject.acarsdec",
    "com.scannerproject.dumpvdl2",
)
GROUND_MOUNT = "/neptune-ground.mp3"

# Seconds without byte progress before a running daemon counts as stalled.
STALL_SEC = 90.0
# After a (re)start, how long to wait for the first progress before judging.
START_GRACE_SEC = 150.0
# Never recover more often than this in any rolling hour.
MAX_RECOVERIES_PER_HOUR = 4
# Wait before recovery attempt N+1 after N consecutive failed recoveries.
BACKOFF_SEC = (0.0, 120.0, 300.0, 600.0, 1200.0, 1800.0)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_HOME = Path.home()
_LAUNCH_AGENTS = _HOME / "Library" / "LaunchAgents"


# --------------------------------------------------------------------------
# Pure decision logic -- no side effects, unit-tested.
# --------------------------------------------------------------------------

@dataclass
class State:
    last_bytes: Optional[float] = None
    last_progress_ts: Optional[float] = None  # last time the counter advanced
    recoveries: list = field(default_factory=list)  # recovery start times
    last_recovery_ts: Optional[float] = None
    last_failure_ts: Optional[float] = None  # when a recovery was judged failed
    failed_streak: int = 0  # recoveries not followed by progress
    awaiting_verify: bool = False  # a recovery ran; waiting to see progress
    last_action: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "State":
        known = {f for f in cls.__dataclass_fields__}  # noqa: C416
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass
class Observation:
    agent_loaded: bool
    bytes: Optional[float]  # None = metrics unreachable


def decide(state: State, obs: Observation, now: float) -> tuple[str, State, str]:
    """Return (action, new_state, reason).

    action is one of: idle, healthy, recovered, wait, hold, recover.
    Only ``recover`` asks the caller to do anything.
    """
    s = State.from_dict(asdict(state))
    s.recoveries = [t for t in s.recoveries if now - t < 3600.0]

    # Operator stopped ground on purpose: never fight that. Forget the
    # progress clock so a later reload gets a fresh start grace.
    if not obs.agent_loaded:
        s.last_bytes = None
        s.last_progress_ts = None
        s.awaiting_verify = False
        return "idle", s, "chirp-ground not loaded (stopped on purpose)"

    if obs.bytes is not None:
        if s.last_bytes is not None and obs.bytes < s.last_bytes:
            # Counter went backwards: the daemon restarted. New baseline,
            # and give the new instance its start grace.
            s.last_bytes = obs.bytes
            s.last_progress_ts = now
            return "wait", s, "metrics counter reset (daemon restarted)"
        if s.last_bytes is None:
            # A single sample proves nothing; progress needs two.
            s.last_bytes = obs.bytes
            if s.last_progress_ts is None:
                s.last_progress_ts = now
            return "wait", s, "first sample; baseline taken"
        if obs.bytes > s.last_bytes:
            s.last_bytes = obs.bytes
            s.last_progress_ts = now
            if s.awaiting_verify:
                s.awaiting_verify = False
                s.failed_streak = 0
                return "recovered", s, "audio flowing again after recovery"
            return "healthy", s, "audio bytes advancing"

    # No progress this tick (counter flat, or metrics unreachable).
    if s.last_progress_ts is None:
        s.last_progress_ts = now
        return "wait", s, "no baseline yet; starting the clock"

    stalled_for = now - s.last_progress_ts
    limit = START_GRACE_SEC if s.awaiting_verify else STALL_SEC
    why = "metrics unreachable" if obs.bytes is None else "audio bytes not advancing"
    if stalled_for < limit:
        return "wait", s, f"{why} for {stalled_for:.0f}s (limit {limit:.0f}s)"

    if s.awaiting_verify:
        # The last recovery did not bring audio back.
        s.awaiting_verify = False
        s.failed_streak += 1
        s.last_failure_ts = now

    if len(s.recoveries) >= MAX_RECOVERIES_PER_HOUR:
        return "hold", s, (
            f"stalled {stalled_for:.0f}s but already {len(s.recoveries)} recoveries "
            f"this hour (max {MAX_RECOVERIES_PER_HOUR}); needs a human"
        )
    # Measured from when the failure was detected, not from when the failed
    # recovery started: the start grace alone outlasts the first backoff step,
    # so timing from the start would make that step a no-op.
    delay = BACKOFF_SEC[min(s.failed_streak, len(BACKOFF_SEC) - 1)]
    if s.failed_streak and s.last_failure_ts is not None and now - s.last_failure_ts < delay:
        return "hold", s, (
            f"stalled {stalled_for:.0f}s; backing off {delay:.0f}s after "
            f"{s.failed_streak} failed recover{'y' if s.failed_streak == 1 else 'ies'}"
        )

    s.recoveries.append(now)
    s.last_recovery_ts = now
    s.awaiting_verify = True
    s.last_progress_ts = now
    s.last_bytes = None
    return "recover", s, f"{why} for {stalled_for:.0f}s"


_BYTES_RE = re.compile(
    r'^chirp_audio_bytes_published_total\{[^}]*mount="' + re.escape(GROUND_MOUNT)
    + r'"[^}]*\}\s+([0-9.eE+-]+)\s*$',
    re.MULTILINE,
)


def parse_bytes(metrics_text: str) -> Optional[float]:
    m = _BYTES_RE.search(metrics_text or "")
    return float(m.group(1)) if m else None


def parse_serial(env_text: str) -> Optional[str]:
    """Serial from CHIRP_SDR_DEVICE_ARGS=...serial=XXXX... in an env file."""
    for line in (env_text or "").splitlines():
        line = line.strip()
        if line.startswith("#") or not line.startswith("CHIRP_SDR_DEVICE_ARGS="):
            continue
        m = re.search(r"serial=([0-9A-Za-z]+)", line)
        if m:
            return m.group(1)
    return None


# --------------------------------------------------------------------------
# Side effects.
# --------------------------------------------------------------------------

def _log(event: str, **kw) -> None:
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **kw}
    print(json.dumps(rec), flush=True)


def _run(argv: list[str], timeout: float = 30.0) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"
    except OSError as exc:
        return 127, str(exc)


def _uid() -> int:
    return os.getuid()


def agent_loaded(label: str) -> bool:
    rc, _ = _run(["/bin/launchctl", "list", label])
    return rc == 0


def read_bytes(url: str) -> Optional[float]:
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return parse_bytes(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 -- unreachable is a valid observation
        return None


def _usb_present(serial: str) -> bool:
    rc, out = _run(["/usr/sbin/ioreg", "-p", "IOUSB", "-l", "-w0"], timeout=10)
    return rc == 0 and f'"USB Serial Number" = "{serial}"' in out


def _pgrep(pattern: str) -> bool:
    rc, _ = _run(["/usr/bin/pgrep", "-f", pattern])
    return rc == 0


def _sdrplay_plist() -> Optional[str]:
    """The API service plist. Resolve it while the service is still loaded
    (the path embeds the API version), falling back to the newest install."""
    rc, out = _run(["/usr/bin/sudo", "-n", "/bin/launchctl", "print",
                    f"system/{SDRPLAY_LABEL}"])
    if rc == 0:
        m = re.search(r"^\s*path = (\S+)", out, re.MULTILINE)
        if m and os.path.exists(m.group(1)):
            return m.group(1)
    found = sorted(glob.glob("/Library/SDRplayAPI/*/plist/com.sdrplay.service.plist"))
    return found[-1] if found else None


def _bootout_gui(label: str) -> None:
    _run(["/bin/launchctl", "bootout", f"gui/{_uid()}/{label}"])


def _bootstrap_gui(label: str) -> bool:
    plist = str(_LAUNCH_AGENTS / f"{label}.plist")
    rc, out = _run(["/bin/launchctl", "bootstrap", f"gui/{_uid()}", plist])
    if rc != 0 and not agent_loaded(label):
        # "Bootstrap failed: 5" is a half-registration race (see the neptune
        # launchd README): clear it and try once more.
        _run(["/bin/launchctl", "bootout", f"gui/{_uid()}", plist])
        _run(["/bin/launchctl", "enable", f"gui/{_uid()}/{label}"])
        rc, out = _run(["/bin/launchctl", "bootstrap", f"gui/{_uid()}", plist])
    return agent_loaded(label)


def _wait(pred, timeout: float, step: float = 1.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


def recover(serial: str, reenum_tool: str, restore_others: bool) -> dict:
    """The proven sequence. Never raises; returns a step-by-step record."""
    steps: dict = {}
    others = [l for l in OTHER_SDRPLAY_LABELS if agent_loaded(l)]
    steps["others_running"] = others
    plist = _sdrplay_plist()
    steps["sdrplay_plist"] = plist

    for label in others:
        _bootout_gui(label)
    _bootout_gui(GROUND_LABEL)
    steps["consumers_stopped"] = _wait(
        lambda: not _pgrep("chirp.daemon") and not _pgrep("acarsdec")
        and not _pgrep("dumpvdl2"), 20)

    rc, out = _run(["/usr/bin/sudo", "-n", "/bin/launchctl", "bootout",
                    f"system/{SDRPLAY_LABEL}"])
    steps["sdrplay_stopped"] = _wait(lambda: not _pgrep("sdrplay_apiService"), 10)

    rc, out = _run([reenum_tool, serial], timeout=20)
    steps["reenumerate"] = {"rc": rc, "out": out[-200:]}
    steps["usb_present"] = _wait(lambda: _usb_present(serial), 15)

    if plist:
        _run(["/usr/bin/sudo", "-n", "/bin/launchctl", "bootstrap", "system", plist])
    steps["sdrplay_started"] = _wait(lambda: _pgrep("sdrplay_apiService"), 15)
    time.sleep(8)  # the API needs a moment before the first open succeeds

    steps["ground_started"] = _bootstrap_gui(GROUND_LABEL)

    if restore_others and others:
        # Give ground the device first; start order has mattered before.
        time.sleep(20)
        steps["others_restored"] = {l: _bootstrap_gui(l) for l in others}
    return steps


# --------------------------------------------------------------------------
# Entry point: one tick.
# --------------------------------------------------------------------------

def _state_path() -> Path:
    default = _HOME / "Library" / "Application Support" / "scannerproject" / "sdrplay-healer.json"
    return Path(os.environ.get("HEALER_STATE_PATH", str(default)))


def _serial() -> Optional[str]:
    if os.environ.get("HEALER_SDR_SERIAL"):
        return os.environ["HEALER_SDR_SERIAL"]
    try:
        return parse_serial((_HOME / ".config" / "sb3" / "chirp-ground.env").read_text())
    except OSError:
        return None


def tick() -> int:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(str(path) + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0  # another tick is mid-recovery

    try:
        state = State.from_dict(json.loads(path.read_text()))
    except (OSError, ValueError):
        state = State()

    obs = Observation(
        agent_loaded=agent_loaded(GROUND_LABEL),
        bytes=read_bytes(os.environ.get("HEALER_METRICS_URL",
                                        "http://127.0.0.1:9102/metrics")),
    )
    now = time.time()
    action, state, reason = decide(state, obs, now)

    # Log transitions and every non-routine decision; stay quiet when steady.
    if action != state.last_action or action in ("recover", "recovered"):
        _log(action, reason=reason, bytes=obs.bytes, loaded=obs.agent_loaded,
             recoveries_last_hour=len(state.recoveries),
             failed_streak=state.failed_streak)
    state.last_action = action

    if action == "recover":
        serial = _serial()
        tool = os.environ.get(
            "HEALER_USB_REENUMERATE",
            str(_REPO_ROOT / "macos" / "usb-reenumerate" / "usb-reenumerate"))
        if os.environ.get("HEALER_DRY_RUN") == "1":
            _log("dry_run", would="recover", serial=serial, tool=tool)
        elif not serial:
            _log("error", reason="no radio serial (set HEALER_SDR_SERIAL)")
        elif not os.access(tool, os.X_OK):
            _log("error", reason=f"usb-reenumerate missing: {tool} (run build.sh)")
        else:
            # Persist BEFORE the slow sequence, so a crash mid-recovery still
            # counts against the rate limit.
            path.write_text(json.dumps(asdict(state)))
            steps = recover(serial, tool,
                            os.environ.get("HEALER_RESTORE_OTHERS", "1") != "0")
            # The sequence takes about a minute; start ground's grace clock
            # when it finishes, not when it began.
            state.last_progress_ts = time.time()
            _log("recover_done", steps=steps)

    path.write_text(json.dumps(asdict(state)))
    return 0


if __name__ == "__main__":
    sys.exit(tick())
