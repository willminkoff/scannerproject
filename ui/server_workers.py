"""Server background workers."""
import logging
import time
import threading
from collections import deque

logger = logging.getLogger(__name__)

try:
    from .config import APPLY_DEBOUNCE_SEC, ACTION_WAIT_TIMEOUT_SEC
    from .actions import execute_action
    from .scanner import (
        refresh_analog_hit_state,
    )
except ImportError:
    from ui.config import APPLY_DEBOUNCE_SEC, ACTION_WAIT_TIMEOUT_SEC
    from ui.actions import execute_action
    from ui.scanner import (
        refresh_analog_hit_state,
    )

logger = logging.getLogger(__name__)

ACTION_QUEUE = deque()
ACTION_COND = threading.Condition()


def enqueue_action(action: dict) -> dict:
    """Enqueue an action and wait for result."""
    waiter = {"event": threading.Event(), "result": None}
    action["waiters"] = [waiter]
    with ACTION_COND:
        ACTION_QUEUE.append(action)
        ACTION_COND.notify()
    if not waiter["event"].wait(timeout=ACTION_WAIT_TIMEOUT_SEC):
        return {"status": 504, "payload": {"ok": False, "error": "action timed out"}}
    return waiter["result"] or {"status": 500, "payload": {"ok": False, "error": "no result"}}


def enqueue_apply(target: str, gain: float, squelch_mode: str, squelch_snr: float, squelch_dbfs: float) -> dict:
    """Enqueue an apply action with debouncing."""
    waiter = {"event": threading.Event(), "result": None}
    now = time.monotonic()
    with ACTION_COND:
        if ACTION_QUEUE:
            last = ACTION_QUEUE[-1]
            if last.get("type") == "apply" and last.get("target") == target:
                last["gain"] = gain
                last["squelch_mode"] = squelch_mode
                last["squelch_snr"] = squelch_snr
                last["squelch_dbfs"] = squelch_dbfs
                last["ready_at"] = now + APPLY_DEBOUNCE_SEC
                last["waiters"].append(waiter)
                ACTION_COND.notify()
            else:
                ACTION_QUEUE.append({
                    "type": "apply",
                    "target": target,
                    "gain": gain,
                    "squelch_mode": squelch_mode,
                    "squelch_snr": squelch_snr,
                    "squelch_dbfs": squelch_dbfs,
                    "ready_at": now + APPLY_DEBOUNCE_SEC,
                    "waiters": [waiter],
                })
                ACTION_COND.notify()
        else:
            ACTION_QUEUE.append({
                "type": "apply",
                "target": target,
                "gain": gain,
                "squelch_mode": squelch_mode,
                "squelch_snr": squelch_snr,
                "squelch_dbfs": squelch_dbfs,
                "ready_at": now + APPLY_DEBOUNCE_SEC,
                "waiters": [waiter],
            })
            ACTION_COND.notify()
    if not waiter["event"].wait(timeout=ACTION_WAIT_TIMEOUT_SEC):
        return {"status": 504, "payload": {"ok": False, "error": "action timed out"}}
    return waiter["result"] or {"status": 500, "payload": {"ok": False, "error": "no result"}}


def _finish_action(action: dict, result: dict) -> None:
    """Mark action as complete and notify waiters."""
    for waiter in action.get("waiters", []):
        waiter["result"] = result
        waiter["event"].set()


def config_worker() -> None:
    """Background worker that processes queued actions."""
    while True:
        with ACTION_COND:
            while not ACTION_QUEUE:
                ACTION_COND.wait()
            action = ACTION_QUEUE[0]
            if action.get("type") == "apply":
                delay = action.get("ready_at", 0) - time.monotonic()
                if delay > 0:
                    ACTION_COND.wait(timeout=delay)
                    continue
            action = ACTION_QUEUE.popleft()

        try:
            result = execute_action(action)
        except Exception as e:
            logger.debug("server_workers: execute_action failed for %s", action.get("type"), exc_info=True)
            result = {"status": 500, "payload": {"ok": False, "error": str(e)}}
        _finish_action(action, result)


def icecast_monitor_worker() -> None:
    """Background thread that keeps live analog hit state warm."""
    while True:
        try:
            refresh_analog_hit_state()
        except Exception:
            logger.debug("server_workers: refresh_analog_hit_state failed", exc_info=True)
        time.sleep(0.25)


def start_config_worker() -> None:
    """Start the config processing worker thread."""
    thread = threading.Thread(target=config_worker, daemon=True)
    thread.start()


def start_icecast_monitor() -> None:
    """Start the Icecast monitoring worker thread."""
    thread = threading.Thread(target=icecast_monitor_worker, daemon=True)
    thread.start()


# ---------------------------------------------------------------------------
# Weather sounding data (ACARS / Radiosonde)
# ---------------------------------------------------------------------------

_met_store = None
_wx_reader_thread = None
_wx_stop_event = None


def get_met_store():
    """Return the singleton MetStore, creating it if needed."""
    global _met_store
    if _met_store is None:
        from .wxdata import MetStore
        _met_store = MetStore()
    return _met_store


def _configure_spatial_filter(store) -> None:
    """Load station location from HPState and set spatial filter on the store."""
    try:
        from .hp_state import HPState
        state = HPState.load()
        if state.use_location and (state.lat != 0.0 or state.lon != 0.0):
            # Convert range_miles to nautical miles (1 nm = 1.15078 statute miles)
            radius_nm = state.range_miles / 1.15078
            store.set_spatial_filter(
                lat=state.lat, lon=state.lon,
                radius_nm=radius_nm, ceiling_ft=40000.0,
            )
            logger.info(
                "Wx spatial filter: %.4f/%.4f, radius %.1f nm (%.1f mi), ceiling 40000 ft",
                state.lat, state.lon, radius_nm, state.range_miles,
            )
        else:
            store.clear_spatial_filter()
            logger.info("Wx spatial filter disabled (no station location configured)")
    except Exception:
        logger.exception("Failed to load station location for wx filter")
        store.clear_spatial_filter()


def start_wx_reader(decoder: str) -> None:
    """Start the wx reader thread for the given decoder (acars or radiosonde)."""
    global _wx_reader_thread, _wx_stop_event
    stop_wx_reader()  # stop any existing reader first

    from .wxdata import acars_reader_worker, radiosonde_reader_worker

    store = get_met_store()
    if decoder == "acars":
        _configure_spatial_filter(store)
    else:
        store.clear_spatial_filter()
        logger.info("Wx spatial filter disabled for %s (tracking mobile source)", decoder)
    store.collecting = True
    store.active_decoder = decoder

    _wx_stop_event = threading.Event()

    if decoder == "acars":
        target = acars_reader_worker
    elif decoder == "radiosonde":
        target = radiosonde_reader_worker
    else:
        return

    _wx_reader_thread = threading.Thread(
        target=target, args=(store, _wx_stop_event), daemon=True
    )
    _wx_reader_thread.start()


def stop_wx_reader() -> None:
    """Stop the active wx reader thread."""
    global _wx_reader_thread, _wx_stop_event
    store = get_met_store()
    store.collecting = False
    store.active_decoder = None

    if _wx_stop_event:
        _wx_stop_event.set()
    if _wx_reader_thread and _wx_reader_thread.is_alive():
        _wx_reader_thread.join(timeout=3)
    _wx_reader_thread = None
    _wx_stop_event = None
