"""sb3.settle — stop mechanics and the settle beat.

The mechanisms here are the one genuinely valuable thing salvaged from
``macos/killswitch/`` (prior art, 7bf15f3).  Its *architecture* is inverted from
SB3's and must not be reused; its *mechanics* were hard-won and are reused
deliberately, reimplemented rather than copied.

Two rules that will bite anyone who does not know them:

1. **bootout, never kill.**  Every scanner agent is a launchd user-agent with
   ``KeepAlive=true``.  A plain ``kill`` is not a stop — launchd respawns the
   process and the operator concludes the tool is broken.  The only real stop is
   ``launchctl bootout gui/$UID/<label>``.  §4.3 depends on this and does not
   say it.

2. **Wait after stopping a SoapySDR/apiService holder.**  The shared
   ``sdrplay_apiService`` does not release an RSP instantly, and the next opener
   inherits the mess.  ``DRAIN_SECONDS`` is the settle beat.

Note the asymmetry SB3 inherits (§3.5, §5.4): SDRTrunk must only ever be
SIGTERM'd and waited on — a SIGKILL or ungraceful release makes the RSPduo drop
off the USB bus, and only a REBOOT recovers it.  SB3 never stops SDRTrunk at all
(it is BACKEND), but any code that grows the ability to must respect that.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Callable, List, Optional

#: Settle beat after stopping a SoapySDR/apiService holder, in seconds.
#: 6s is the prior art's value, itself inherited from `scripts/disco-svc-ctl`.
DRAIN_SECONDS = 6.0

#: How long to wait for a bootout to actually take effect before reporting it
#: still loaded. bootout returns BEFORE teardown finishes.
BOOTOUT_GRACE_SEC = 2.0


def gui_domain(uid: int) -> str:
    return f"gui/{uid}"


def bootout_command(label: str, uid: int) -> List[str]:
    """The exact argv `kill` would run to stop one agent.

    Pure — builds the command, never runs it. This is what dry-run prints, which
    means the printed command and the executed command cannot drift apart.
    """
    return ["launchctl", "bootout", f"{gui_domain(uid)}/{label}"]


def is_loaded(label: str, uid: int, timeout: float = 5.0) -> bool:
    try:
        rc = subprocess.run(
            ["launchctl", "print", f"{gui_domain(uid)}/{label}"],
            capture_output=True, text=True, timeout=timeout,
        ).returncode
        return rc == 0
    except (OSError, subprocess.SubprocessError):
        return False


def bootout(label: str, uid: int, *, execute: bool,
            emit: Optional[Callable[[str], None]] = None) -> bool:
    """Stop one agent. With execute=False this ONLY prints what it would run.

    Returns True if the agent is stopped (or would be, in dry-run).
    """
    cmd = bootout_command(label, uid)
    if not execute:
        if emit:
            emit(f"would: {' '.join(cmd)}")
        return True
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=10.0)
    except (OSError, subprocess.SubprocessError) as exc:
        if emit:
            emit(f"ERROR: bootout {label} raised {exc!r}")
        return False
    time.sleep(BOOTOUT_GRACE_SEC)
    still = is_loaded(label, uid)
    if emit:
        emit(f"{'WARN: still loaded' if still else 'stopped'}: {label}")
    return not still


def drain(*, execute: bool, seconds: float = DRAIN_SECONDS,
          emit: Optional[Callable[[str], None]] = None) -> None:
    """The settle beat. Dry-run prints it rather than sleeping."""
    if not execute:
        if emit:
            emit(f"would: sleep {seconds:g}  # apiService settle beat")
        return
    if emit:
        emit(f"draining {seconds:g}s for the apiService to release…")
    time.sleep(seconds)


def bootstrap(label: str, plist: "Path", uid: int, *, execute: bool,
              emit: Optional[Callable[[str], None]] = None,
              settle_sec: float = 2.0) -> bool:
    """Load one agent. With execute=False this ONLY prints what it would run.

    Returns True if the agent is loaded afterwards.  `bootstrap` returning 0 is
    NOT proof the job is up — launchd accepts the job and starts it
    asynchronously, so we wait a beat and then confirm with `launchctl print`.
    Trusting the return code here would be a check that doesn't check (§4.6).
    """
    cmd = ["launchctl", "bootstrap", gui_domain(uid), str(plist)]
    if not execute:
        if emit:
            emit(f"would: {' '.join(cmd)}")
        return True
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15.0)
    except (OSError, subprocess.SubprocessError) as exc:
        if emit:
            emit(f"ERROR: bootstrap {label} raised {exc!r}")
        return False
    time.sleep(settle_sec)
    up = is_loaded(label, uid)
    if emit:
        if up:
            emit(f"started {label}")
        else:
            detail = (proc.stderr or proc.stdout or "").strip()
            emit(f"ERROR: {label} not loaded after bootstrap"
                 f"{f' — {detail}' if detail else ''}")
    return up
