#!/usr/bin/env python3
"""SB5 reliability watchdog — Phase B.

Polls reliability.snapshot() every WATCHDOG_INTERVAL_SEC. Tracks how many
consecutive polls each area has been in WEDGED state. Once an area crosses
its escalation threshold, fires the corresponding recovery action and
enters a cooldown so we don't loop.

Recovery actions (each can be disabled individually via env):
  chirp_wedged   -> safe_restart_rtl_airband(bands=("airband","ground"))
  op25_wedged    -> systemctl restart scanner-digital-op25
  vfo_wedged     -> systemctl restart scanner-vfo
  bt_warn        -> bt-connect-speaker.sh

Every observation + recovery is appended as a JSON Lines event to
/var/log/scanner/reliability.jsonl so we can analyze failure patterns
(time-of-day, post-BT-reconnect, etc.).

Design constraints:
  - Bounded resource: never spawn more than one in-flight recovery
  - Rate-limited: max RECOVERY_MAX_PER_HOUR per area to avoid loops
  - Operator-controllable: env vars + a WATCHDOG_ENABLED flag file
  - Auditable: every decision logged with context so it's debuggable
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Callable, Optional

# Path setup so we can import the reliability module.
sys.path.insert(0, "/home/ubuntu/scannerproject")
from ui.reliability import snapshot  # noqa: E402

# ---------------------------------------------------------------------------
# Tunables (override via env vars)
# ---------------------------------------------------------------------------
WATCHDOG_INTERVAL_SEC = float(os.environ.get("WATCHDOG_INTERVAL_SEC", "15.0"))
# Consecutive WEDGED polls before recovery fires.  A single wedged poll is
# often a transient probe failure; require a couple in a row.
ESCALATE_AFTER_POLLS = int(os.environ.get("WATCHDOG_ESCALATE_AFTER_POLLS", "3"))
# Cooldown after a recovery action — don't re-fire on the same area.
RECOVERY_COOLDOWN_SEC = float(os.environ.get("WATCHDOG_RECOVERY_COOLDOWN_SEC", "180"))
# Rate limit per area.
RECOVERY_MAX_PER_HOUR = int(os.environ.get("WATCHDOG_RECOVERY_MAX_PER_HOUR", "4"))
# Disable file: touching /run/scannerproject/watchdog.disabled pauses recovery.
DISABLE_FILE = Path(os.environ.get("WATCHDOG_DISABLE_FILE",
                                   "/run/scannerproject/watchdog.disabled"))

JOURNAL_PATH = Path(os.environ.get("WATCHDOG_JOURNAL",
                                   "/var/log/scanner/reliability.jsonl"))
JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)

# Per-area individual disable env vars (default: enabled).
ENABLE_RECOVERY = {
    "chirp": os.environ.get("WATCHDOG_RECOVER_CHIRP", "1") != "0",
    "op25": os.environ.get("WATCHDOG_RECOVER_OP25", "1") != "0",
    "vfo": os.environ.get("WATCHDOG_RECOVER_VFO", "1") != "0",
    "bt": os.environ.get("WATCHDOG_RECOVER_BT", "1") != "0",
    # 2026-06-11: per-VLC stalls (post-chirp-restart audio drop pattern).
    "vlc_analog": os.environ.get("WATCHDOG_RECOVER_VLC", "1") != "0",
    "vlc_ground": os.environ.get("WATCHDOG_RECOVER_VLC", "1") != "0",
    "vlc_digital": os.environ.get("WATCHDOG_RECOVER_VLC", "1") != "0",
    "vlc_vfo": os.environ.get("WATCHDOG_RECOVER_VLC", "1") != "0",
}

# ---------------------------------------------------------------------------
log = logging.getLogger("watchdog")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# Track consecutive wedged poll count per area.
_wedged_streak: dict[str, int] = defaultdict(int)
# Last recovery timestamp per area (for cooldown).
_last_recovery_ts: dict[str, float] = {}
# Rolling deque of recovery timestamps per area (for hourly rate limit).
_recovery_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=RECOVERY_MAX_PER_HOUR))
# Track previous snapshot for change-detection logging.
_prev_verdicts: dict[str, str] = {}

_running = True


def _shutdown(*args):
    global _running
    _running = False


signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)


def _audit(event: dict) -> None:
    """Append a JSON line to the reliability journal."""
    event.setdefault("ts", time.time())
    try:
        with JOURNAL_PATH.open("a") as f:
            f.write(json.dumps(event) + "\n")
    except OSError as exc:
        log.warning("journal write failed: %s", exc)


def _can_recover(area: str) -> tuple[bool, str]:
    """Return (allowed, reason). Checks: enable flag, disable file, cooldown,
    rate limit."""
    if not ENABLE_RECOVERY.get(area, True):
        return False, "recovery_disabled_by_env"
    if DISABLE_FILE.exists():
        return False, "globally_disabled"
    last = _last_recovery_ts.get(area, 0)
    if time.time() - last < RECOVERY_COOLDOWN_SEC:
        return False, "cooldown"
    hist = _recovery_history[area]
    now = time.time()
    while hist and now - hist[0] > 3600:
        hist.popleft()
    if len(hist) >= RECOVERY_MAX_PER_HOUR:
        return False, "rate_limit"
    return True, "ok"


def _mark_recovered(area: str) -> None:
    now = time.time()
    _last_recovery_ts[area] = now
    _recovery_history[area].append(now)


def _run_cmd(cmd: list[str], timeout: float = 60.0) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError as exc:
        return -1, "", str(exc)


# ---------------------------------------------------------------------------
# Recovery actions
# ---------------------------------------------------------------------------

def recover_chirp() -> dict:
    """Bounce the analog chirp daemons via safe_restart_rtl_airband.

    This wraps the existing escalation path which knows about MA/SL
    sequencing and sdrplay-daemon recovery on probe failure.
    """
    log.warning("recovery: chirp daemons -> safe_restart_rtl_airband")
    try:
        from ui.airband_restart import safe_restart_rtl_airband
    except Exception as exc:
        return {"ok": False, "error": f"import_failed: {exc}"}
    try:
        result = safe_restart_rtl_airband(
            bands=("airband", "ground"),
            reason="watchdog_chirp_wedge",
        )
        return {"ok": result.get("status") == "ok", "detail": result}
    except Exception as exc:
        log.exception("safe_restart_rtl_airband raised")
        return {"ok": False, "error": str(exc)}


def recover_op25() -> dict:
    """Restart the OP25 systemd unit. Doesn't bounce sdrplay (that affects
    the analog daemons too); a fresh OP25 process often un-wedges on its
    own without disturbing analog."""
    log.warning("recovery: op25 -> systemctl restart scanner-digital-op25")
    rc, _, err = _run_cmd(
        ["sudo", "-n", "/bin/systemctl", "restart", "scanner-digital-op25.service"],
        timeout=30,
    )
    return {"ok": rc == 0, "rc": rc, "stderr": err.strip()}


def recover_vfo() -> dict:
    """Restart scanner-vfo. Cheap (no SDR sharing implications)."""
    log.warning("recovery: vfo -> systemctl restart scanner-vfo")
    rc, _, err = _run_cmd(
        ["sudo", "-n", "/bin/systemctl", "restart", "scanner-vfo.service"],
        timeout=20,
    )
    return {"ok": rc == 0, "rc": rc, "stderr": err.strip()}


def _recover_vlc(band: str) -> dict:
    """Restart the per-band scanner-vlc-<band> systemd unit.

    The chirp-VLC audio-drop pattern: chirp daemon restarts -> the VLC
    that pulls from its mount keeps the old TCP connection alive but
    silently stops consuming, /proc/<pid>/io rchar stops advancing,
    audio goes silent on BOOM.  Restart fixes it.
    """
    unit = "scanner-vlc-" + band + ".service"
    log.warning("recovery: vlc/%s -> systemctl restart %s", band, unit)
    rc, _, err = _run_cmd(
        ["sudo", "-n", "/bin/systemctl", "restart", unit],
        timeout=20,
    )
    return {"ok": rc == 0, "rc": rc, "stderr": err.strip()}


def recover_vlc_analog() -> dict:
    return _recover_vlc("analog")


def recover_vlc_ground() -> dict:
    return _recover_vlc("ground")


def recover_vlc_digital() -> dict:
    return _recover_vlc("digital")


def recover_vlc_vfo() -> dict:
    return _recover_vlc("vfo")


def recover_bt() -> dict:
    """Re-run bt-connect-speaker.sh to reconnect a dropped BOOM."""
    log.warning("recovery: bt -> /home/ubuntu/scannerproject/scripts/bt-connect-speaker.sh")
    rc, _, err = _run_cmd(
        ["/home/ubuntu/scannerproject/scripts/bt-connect-speaker.sh"],
        timeout=90,
    )
    return {"ok": rc == 0, "rc": rc, "stderr": err.strip()[:300]}


RECOVERY_ACTIONS: dict[str, Callable[[], dict]] = {
    "chirp": recover_chirp,
    "op25": recover_op25,
    "vfo": recover_vfo,
    "bt": recover_bt,
    "vlc_analog": recover_vlc_analog,
    "vlc_ground": recover_vlc_ground,
    "vlc_digital": recover_vlc_digital,
    "vlc_vfo": recover_vlc_vfo,
}


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _extract_per_area_verdicts(snap: dict) -> dict[str, str]:
    """Pull the area-level verdicts we know how to recover from."""
    verdicts: dict[str, str] = {}
    chirp = snap.get("chirp", {})
    if any(c.get("verdict") == "wedged" for c in chirp.values()):
        verdicts["chirp"] = "wedged"
    else:
        verdicts["chirp"] = "ok"
    verdicts["op25"] = snap.get("op25", {}).get("verdict", "ok")
    verdicts["vfo"] = snap.get("vfo", {}).get("verdict", "ok")
    # BT: warn (disconnected) is recoverable; wedged (not paired) is operator action.
    bt_v = snap.get("bt", {}).get("verdict", "ok")
    verdicts["bt"] = "wedged" if bt_v == "warn" else "ok"
    # 2026-06-11: per-band VLC stall verdicts (post-chirp-restart audio drop).
    vlc_map = snap.get("vlc") or {}
    for band in ("analog", "ground", "digital", "vfo"):
        verdicts["vlc_" + band] = vlc_map.get(band, {}).get("verdict", "ok") if isinstance(vlc_map, dict) else "ok"
    return verdicts


def evaluate_and_act(snap: dict) -> dict:
    per_area = _extract_per_area_verdicts(snap)
    actions: dict[str, dict] = {}
    for area, verdict in per_area.items():
        prev = _prev_verdicts.get(area)
        if prev != verdict:
            _audit({
                "event": "verdict_change",
                "area": area,
                "from": prev,
                "to": verdict,
            })
            _prev_verdicts[area] = verdict
        if verdict == "wedged":
            _wedged_streak[area] += 1
        else:
            if _wedged_streak[area] > 0:
                _audit({
                    "event": "wedge_self_cleared",
                    "area": area,
                    "streak": _wedged_streak[area],
                })
            _wedged_streak[area] = 0
            continue
        if _wedged_streak[area] < ESCALATE_AFTER_POLLS:
            continue
        allowed, reason = _can_recover(area)
        if not allowed:
            _audit({
                "event": "recovery_skipped",
                "area": area,
                "reason": reason,
                "streak": _wedged_streak[area],
            })
            continue
        fn = RECOVERY_ACTIONS.get(area)
        if not fn:
            continue
        log.info("escalating %s after %d wedged polls", area, _wedged_streak[area])
        outcome = fn()
        _mark_recovered(area)
        _audit({
            "event": "recovery_fired",
            "area": area,
            "streak": _wedged_streak[area],
            "outcome": outcome,
        })
        actions[area] = outcome
        # Reset streak so we don't immediately re-fire if next poll still shows wedge
        # — the cooldown will hold us off.
        _wedged_streak[area] = 0
    return actions


def main_loop() -> None:
    log.info("watchdog starting interval=%.1fs escalate_after=%d cooldown=%.0fs",
             WATCHDOG_INTERVAL_SEC, ESCALATE_AFTER_POLLS, RECOVERY_COOLDOWN_SEC)
    _audit({"event": "watchdog_started"})
    while _running:
        try:
            snap = snapshot()
        except Exception as exc:
            log.exception("snapshot raised")
            _audit({"event": "snapshot_failed", "error": str(exc)})
            time.sleep(WATCHDOG_INTERVAL_SEC)
            continue
        try:
            evaluate_and_act(snap)
        except Exception:
            log.exception("evaluate_and_act raised")
        # Always record the overall verdict so we have a trace.
        _audit({
            "event": "tick",
            "overall": snap.get("verdict"),
            "elapsed_ms": snap.get("elapsed_ms"),
        })
        # Sleep with early-wake on shutdown.
        deadline = time.monotonic() + WATCHDOG_INTERVAL_SEC
        while _running and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))
    _audit({"event": "watchdog_stopping"})
    log.info("watchdog exiting cleanly")


if __name__ == "__main__":
    main_loop()
