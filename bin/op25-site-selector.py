#!/usr/bin/env python3
"""Run the op25 site selector on a timer, next to op25.

ui.op25_adapter already implements the whole policy: it polls op25's HTTP
status endpoint, scores the active site on real decode (control lock, tsbk
age, recent grants, monitored-talkgroup hits), demotes a site that goes
stale, promotes the next candidate, restarts op25 when it switches, and
persists all of it to site_selector_state.json.

Nothing ran it.  ``_evaluate_site_selector`` had exactly one caller --
``Op25Adapter.preflight()`` -- which is an on-demand route handler, not a
loop, so the scoring logic had never executed.  On the live box the state
file sat untouched for a day while op25 decoded happily, every candidate
still reading ``control_locked: False, last_tsbk_age_sec: None``.

This process is the missing timer and nothing more.  It deliberately adds no
policy of its own: all decisions, grace periods and restart guards stay in
the adapter, so there is one implementation to reason about.

It runs HERE, beside op25, rather than on the UI host, because op25's status
endpoint binds localhost, the profile and selector state are on this box, and
the digital chain must keep correcting itself when the UI host is down.

Env:
  OP25_SELECTOR_INTERVAL_SEC  seconds between evaluations (default 30)
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path

# Running a script puts bin/ on sys.path, never the repo root, so `import ui`
# fails no matter what the working directory is.  Resolve the root from this
# file instead of relying on cwd or on the unit setting PYTHONPATH.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s op25-site-selector: %(message)s",
)
log = logging.getLogger("op25-site-selector")

_STOP = False


def _stop(signum, _frame):
    global _STOP
    _STOP = True
    log.info("signal %s — stopping after this cycle", signum)


def _interval() -> float:
    raw = os.environ.get("OP25_SELECTOR_INTERVAL_SEC", "30")
    try:
        return max(5.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        return 30.0


# After op25 (re)starts, how long to leave it alone before scoring it. A cold
# decoder reads as "stale" and would trigger a pointless site switch -- the
# same reason the unit sleeps 60 s before the first evaluation.
_WARMUP_SEC = 60.0


def _op25_ready(unit_active, unit_active_enter_epoch, unit: str) -> tuple[bool, str]:
    """Is op25 running and warmed up enough to be judged?

    The selector exists to restart an op25 that is running but stuck. It must
    never act on one that is stopped: preflight() reads a stopped decoder as
    stale and, after a few windows, restarts it -- which would undo an
    operator's deliberate `systemctl stop`. BindsTo= on the unit used to
    prevent that by accident (and killed the selector on every restart it
    made itself); this check states the intent instead.
    """
    try:
        if not unit_active(unit):
            return False, f"{unit} is not active (stopped on purpose, or restarting)"
    except Exception:
        return True, ""  # can't tell -- evaluate, as before this check existed
    try:
        since = unit_active_enter_epoch(unit)
    except Exception:
        since = None
    if since is not None and time.time() - since < _WARMUP_SEC:
        return False, f"{unit} started {time.time() - since:.0f}s ago; warming up"
    return True, ""


def _sleep_interval(interval: float) -> None:
    # Sleep in short slices so SIGTERM is honoured promptly.
    waited = 0.0
    while waited < interval and not _STOP:
        time.sleep(min(1.0, interval - waited))
        waited += 1.0


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    try:
        from ui.op25_adapter import Op25Adapter
        from ui.config import OP25_SERVICE_NAME
        from ui.systemd import unit_active, unit_active_enter_epoch
    except Exception:
        log.exception("cannot import ui.op25_adapter — is this running from the repo root?")
        return 1

    interval = _interval()
    log.info("started; evaluating every %.0fs", interval)

    adapter = Op25Adapter()
    # Only log when something actually changes; this runs twice a minute
    # forever and a per-cycle line would bury the decisions that matter.
    last_seen: dict[str, tuple] = {}
    consecutive_errors = 0
    skip_reason = ""

    while not _STOP:
        ready, why = _op25_ready(unit_active, unit_active_enter_epoch, OP25_SERVICE_NAME)
        if not ready:
            # Log the transition, not every cycle (the warm-up countdown
            # would otherwise log twice a minute with a new number).
            if ("not active" in why) != ("not active" in skip_reason) or not skip_reason:
                log.info("not evaluating: %s", why)
            skip_reason = why
            _sleep_interval(interval)
            continue
        if skip_reason:
            log.info("%s ready -- evaluating again", OP25_SERVICE_NAME)
            skip_reason = ""

        try:
            result = adapter.preflight() or {}
            consecutive_errors = 0

            # preflight() returns this already flattened to {system: state}.
            selection = result.get("op25_site_selection") or {}
            for system, state in selection.items():
                key = (
                    str(state.get("selected_site_name") or ""),
                    str(state.get("reason_code") or ""),
                )
                if last_seen.get(system) != key:
                    log.info(
                        "system=%r site=%r reason=%s switches=%s",
                        system,
                        state.get("selected_site_name"),
                        state.get("reason_code"),
                        state.get("switch_count"),
                    )
                    last_seen[system] = key
        except Exception:
            consecutive_errors += 1
            # Never die on a transient: op25 restarts, and a status poll
            # during that window raises.  Log the first few, then go quiet
            # so a persistent fault does not fill the journal.
            if consecutive_errors <= 3:
                log.exception("evaluation failed (%d in a row)", consecutive_errors)
            elif consecutive_errors % 60 == 0:
                log.error("evaluation still failing (%d in a row)", consecutive_errors)

        _sleep_interval(interval)

    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
