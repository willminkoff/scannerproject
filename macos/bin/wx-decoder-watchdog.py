#!/usr/bin/env python3
"""Never leave the box with no weather/ACARS decoder running.

WHY THIS EXISTS
---------------
The RSPduo (serial 180903EF32) can serve either ACARS+VDL2 or radiosonde, not
both: acarsdec opens it ``mode=MA`` and dumpvdl2 ``mode=SL tuner=2`` as a
master/slave pair, while auto_rx needs ``mode=ST`` -- the whole device. So
``/api/wx/decoder`` hands the radio over by booting one side out and starting
the other.

That handoff has a sharp edge. ``com.scannerproject.radiosonde`` ships
``RunAtLoad=false`` **and ``KeepAlive=false``**, so if auto_rx dies nothing
restarts it -- and ACARS does not come back either, because the handoff booted
it out of the launchd domain, which is precisely what stops its own KeepAlive
from helping. Before this watchdog, a crash at 3am meant waking up to no ACARS,
no radiosonde, and no indication anything had happened.

acarsdec and dumpvdl2 both have ``KeepAlive=true``, so ACARS is the resilient
baseline and radiosonde is the fragile guest. This watchdog therefore:

  * leaves things alone whenever the requested decoder is actually running;
  * retries radiosonde a few times if that is what was asked for, so a
    transient failure does not silently end an overnight sonde session;
  * falls back to ACARS once the retry budget is spent, so the box is never
    left deaf.

INTENT, NOT GUESSWORK
---------------------
``_wx_decoder_switch`` in sb3/ui/routes.py records the requested decoder in
``~/.local/share/sb3/wx_decoder_intent`` before it starts work. Without that
record a watchdog cannot tell "radiosonde died and should be retried" from "we
are deliberately on ACARS", and would either fight a deliberate session or end
it at the first hiccup.

RUNNING vs LOADED
-----------------
``launchctl list`` prints ``PID  Status  Label`` and the PID column is ``-`` for
a job that is loaded but has no process. Reading only the label is the bug that
made the UI report radiosonde as active for weeks while ACARS was the thing
actually running; this script reads the PID column.

Deliberately standalone: no imports from the repo, so it keeps working when the
UI is broken -- which is exactly when it is needed.

Install: see etc/mac/launchd/neptune/com.scannerproject.wx-decoder-watchdog.plist
(StartInterval 60).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ACARS = "com.scannerproject.acarsdec"
VDL2 = "com.scannerproject.dumpvdl2"
SONDE = "com.scannerproject.radiosonde"

# Every service a mode needs, in START order. The RSPduo master (acarsdec,
# mode=MA) must come up before the slave (dumpvdl2, mode=SL tuner=2).
SERVICES = {
    "acars": [ACARS, VDL2],
    "radiosonde": [SONDE],
}
# The label whose state decides whether a mode is "up".
PRIMARY = {"acars": ACARS, "radiosonde": SONDE}

INTENT_PATH = Path("~/.local/share/sb3/wx_decoder_intent").expanduser()
STATE_PATH = Path("~/.local/share/sb3/wx_decoder_watchdog.json").expanduser()
LOG_PATH = Path("~/Library/Logs/sb3/wx-decoder-watchdog.log").expanduser()

# Two consecutive misses before acting. The handoff itself takes ~10-30 s
# (device release, sdrplay kick, SDRplay open), and at a 60 s interval one
# strike would let the watchdog interrupt a switch that is still in progress.
STRIKES_BEFORE_ACTION = 2
# How many times to put radiosonde back before giving up and restoring ACARS.
# Reset whenever it is seen running, so this is "consecutive failures".
MAX_SONDE_RESTARTS = 3
# A healthy check is silent, because at a 60 s interval logging every pass
# would be 1440 lines a day. But a watchdog that says nothing is a watchdog you
# cannot distinguish from a dead one, so emit one proof-of-life line an hour.
HEARTBEAT_SEC = 3600


def log(msg: str) -> None:
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def launchctl(args: list, use_sudo: bool = False) -> subprocess.CompletedProcess:
    cmd = (["/usr/bin/sudo", "-n"] if use_sudo else []) + ["/bin/launchctl"] + args
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)


def service_states() -> dict:
    """``label -> "running" | "loaded"``; absent labels are omitted.

    The PID column is what distinguishes the two -- see the module docstring.
    """
    out: dict = {}
    try:
        r = launchctl(["list"])
    except Exception as exc:
        log("ERROR could not run launchctl list: %r" % (exc,))
        return out
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[-1] == "Label":
            continue
        out[parts[-1]] = "running" if parts[0].strip() not in ("-", "") else "loaded"
    return out


def read_intent() -> str:
    try:
        v = INTENT_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        return "acars"
    return v if v in SERVICES else "acars"


def write_intent(target: str) -> None:
    try:
        INTENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        INTENT_PATH.write_text(target + "\n", encoding="utf-8")
    except Exception as exc:
        log("WARN could not write intent: %r" % (exc,))


def read_state() -> dict:
    try:
        d = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def write_state(d: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(d), encoding="utf-8")
    except Exception as exc:
        log("WARN could not write state: %r" % (exc,))


def start_service(label: str) -> bool:
    """kickstart, falling back to bootstrap+kickstart when not loaded.

    ``bootstrap`` alone cannot start a ``RunAtLoad=false`` job -- that is why
    radiosonde sat at ``runs = 0`` for a month -- so kickstart is always the
    verb that actually launches the process.
    """
    uid = os.getuid()
    target = "gui/%d/%s" % (uid, label)
    try:
        if launchctl(["kickstart", target]).returncode == 0:
            return True
        plist = Path("~/Library/LaunchAgents/%s.plist" % label).expanduser()
        if not plist.is_file():
            log("ERROR no plist at %s" % plist)
            return False
        launchctl(["bootstrap", "gui/%d" % uid, str(plist)])
        return launchctl(["kickstart", target]).returncode == 0
    except Exception as exc:
        log("ERROR start %s: %r" % (label, exc))
        return False


def kick_sdrplay() -> None:
    """Best effort. A decoder that died mid-stream can leave the SDRplay API
    wedged, and then the replacement cannot open the device either."""
    try:
        r = launchctl(["kickstart", "-k", "system/com.sdrplay.service"], use_sudo=True)
        log("kicked com.sdrplay.service (rc=%s)" % r.returncode)
    except Exception as exc:
        log("WARN sdrplay kick failed (continuing): %r" % (exc,))


def restore_acars(reason: str) -> None:
    log("FALLBACK restoring ACARS: %s" % reason)
    kick_sdrplay()
    time.sleep(3)
    for label in SERVICES["acars"]:
        ok = start_service(label)
        log("  start %s -> %s" % (label, "ok" if ok else "FAILED"))
        time.sleep(1)
    # Stop retrying radiosonde: the operator's session is over.
    write_intent("acars")
    write_state({"strikes": 0, "sonde_restarts": 0, "last_fallback": time.time()})


def main() -> int:
    intent = read_intent()
    states = service_states()
    primary = PRIMARY[intent]
    st = states.get(primary, "absent")
    state = read_state()
    strikes = int(state.get("strikes") or 0)
    restarts = int(state.get("sonde_restarts") or 0)

    if st == "running":
        now = time.time()
        last_hb = float(state.get("last_heartbeat") or 0)
        if strikes or restarts:
            log("OK %s running again (clearing %d strike(s), %d restart(s))"
                % (intent, strikes, restarts))
            write_state({"strikes": 0, "sonde_restarts": 0, "last_heartbeat": now})
        elif now - last_hb >= HEARTBEAT_SEC:
            log("OK %s running (heartbeat)" % intent)
            write_state({"strikes": 0, "sonde_restarts": 0, "last_heartbeat": now})
        return 0

    strikes += 1
    log("MISS requested=%s primary=%s state=%s strike=%d/%d"
        % (intent, primary, st, strikes, STRIKES_BEFORE_ACTION))

    if strikes < STRIKES_BEFORE_ACTION:
        # Probably a handoff in flight; give it another interval.
        write_state({"strikes": strikes, "sonde_restarts": restarts})
        return 0

    if intent == "radiosonde" and restarts < MAX_SONDE_RESTARTS:
        restarts += 1
        log("RETRY radiosonde (attempt %d/%d)" % (restarts, MAX_SONDE_RESTARTS))
        kick_sdrplay()
        time.sleep(3)
        ok = start_service(SONDE)
        log("  start %s -> %s" % (SONDE, "ok" if ok else "FAILED"))
        write_state({"strikes": 0 if ok else strikes, "sonde_restarts": restarts})
        return 0

    why = ("radiosonde failed %d times" % restarts
           if intent == "radiosonde" else "no decoder running")
    restore_acars(why)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never die silently; launchd would just re-run us
        log("FATAL %r" % (exc,))
        sys.exit(1)
