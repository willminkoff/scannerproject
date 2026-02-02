"""Server background workers."""
import time
import threading
from collections import deque

try:
    from .config import APPLY_DEBOUNCE_SEC
    from .actions import execute_action
    from .scanner import update_icecast_hit_log, read_last_hit_from_icecast
except ImportError:
    from ui.config import APPLY_DEBOUNCE_SEC
    from ui.actions import execute_action
    from ui.scanner import update_icecast_hit_log, read_last_hit_from_icecast

ACTION_QUEUE = deque()
ACTION_COND = threading.Condition()


def enqueue_action(action: dict) -> dict:
    """Enqueue an action and wait for result."""
    waiter = {"event": threading.Event(), "result": None}
    action["waiters"] = [waiter]
    with ACTION_COND:
        ACTION_QUEUE.append(action)
        ACTION_COND.notify()
    waiter["event"].wait()
    return waiter["result"] or {"status": 500, "payload": {"ok": False, "error": "no result"}}


def enqueue_apply(target: str, gain: float, squelch: float) -> dict:
    """Enqueue an apply action with debouncing."""
    waiter = {"event": threading.Event(), "result": None}
    now = time.monotonic()
    with ACTION_COND:
        if ACTION_QUEUE:
            last = ACTION_QUEUE[-1]
            if last.get("type") == "apply" and last.get("target") == target:
                last["gain"] = gain
                last["squelch"] = squelch
                last["ready_at"] = now + APPLY_DEBOUNCE_SEC
                last["waiters"].append(waiter)
                ACTION_COND.notify()
            else:
                ACTION_QUEUE.append({
                    "type": "apply",
                    "target": target,
                    "gain": gain,
                    "squelch": squelch,
                    "ready_at": now + APPLY_DEBOUNCE_SEC,
                    "waiters": [waiter],
                })
                ACTION_COND.notify()
        else:
            ACTION_QUEUE.append({
                "type": "apply",
                "target": target,
                "gain": gain,
                "squelch": squelch,
                "ready_at": now + APPLY_DEBOUNCE_SEC,
                "waiters": [waiter],
            })
            ACTION_COND.notify()
    waiter["event"].wait()
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
            result = {"status": 500, "payload": {"ok": False, "error": str(e)}}
        _finish_action(action, result)


def icecast_monitor_worker() -> None:
    """Background thread that continuously monitors Icecast stream title and logs hits."""
    prev_title = None
    while True:
        try:
            current_title = read_last_hit_from_icecast()
            if current_title != prev_title:
                update_icecast_hit_log(current_title)
                prev_title = current_title
        except Exception:
            pass
        time.sleep(0.5)


def start_config_worker() -> None:
    """Start the config processing worker thread."""
    thread = threading.Thread(target=config_worker, daemon=True)
    thread.start()


def start_icecast_monitor() -> None:
    """Start the Icecast monitoring worker thread."""
    thread = threading.Thread(target=icecast_monitor_worker, daemon=True)
    thread.start()
