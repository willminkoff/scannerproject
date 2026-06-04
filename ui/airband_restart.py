"""Safe-restart wrapper for rtl-airband band services.

Why this exists
===============
Earlier today the analog stack wedged after every dashboard interaction
that bounced rtl-airband (chip change, AUTO/MANUAL toggle, favorite
swap, Sitrep → Reset Radios).  The journal showed the same pattern
every time:

    Got signal 2, exiting → Cleaning up → [silence] → 30s later,
    State 'stop-sigterm' timed out → SIGKILL

When the SIGKILL hits during the SDRplay master/slave teardown the
``/dev/shm/Glbl*sdrSrv*`` semaphores get corrupted, the next
``sdrplay_api_Open`` hangs indefinitely, and the only way back is
``systemctl restart sdrplay.service`` followed by clean starts.

``ui.systemd.restart_rtl_airband`` / ``restart_rtl_ground`` already
know how to do that escalation dance — they gentle-restart, probe,
and on probe failure escalate to a full peer + OP25 stop + sdrplay
daemon bounce + master-then-slave start.  But the bypass paths that
fire on every UI chip change (``/api/sitrep/action reset_radios``,
``squelch_tracker._restart_band_unit``) skip those helpers and call
``systemctl restart`` directly.  When THAT path's SIGKILL hits, no
sdrplay recovery happens and the next user action is dead.

This module wraps the existing safe helpers with a module-level
idempotency lock so that:

1. Concurrent restart requests (user clicks rapidly + tracker auto-
   apply firing simultaneously) don't stack — second caller no-ops
   instead of corrupting the in-flight teardown.
2. All the previously-unsafe call sites get the same escalating
   recovery the per-band helpers already implement.
3. The operator gets a structured audit-log record per safe-restart
   cycle (band, reason, whether escalation fired, mounts verified,
   elapsed).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

# Module-level idempotency lock.  Non-blocking acquire by default so
# the second concurrent caller short-circuits with a structured
# "skipped" result rather than stacking another stop/start cycle on
# top of an in-flight teardown.
_SAFE_RESTART_LOCK = threading.Lock()
_SAFE_RESTART_IN_FLIGHT: dict[str, Any] = {
    "active": False,
    "started_ts": 0.0,
    "bands": (),
    "reason": "",
}
# Guards reads of _SAFE_RESTART_IN_FLIGHT from snapshotters.
_INFLIGHT_LOCK = threading.Lock()

# Audit log path.  Rolls when size exceeds the byte cap.
_AUDIT_LOG_PATH = os.getenv(
    "SAFE_RESTART_AUDIT_LOG",
    "/var/log/airband-ui/safe_restart.jsonl",
)
_AUDIT_LOG_MAX_BYTES = int(
    os.getenv("SAFE_RESTART_AUDIT_LOG_MAX_BYTES", "1048576")  # 1 MiB
)

# Canonical band names accepted as input.
_BAND_CANONICAL = {
    "air": "airband", "airband": "airband",
    "ground": "ground", "gnd": "ground", "ground_vhf": "ground",
}


def _audit(event: dict) -> None:
    """Append one JSON line to the safe-restart audit log.

    Never raises — audit failures must not break the restart path.
    """
    try:
        path = _AUDIT_LOG_PATH
        d = os.path.dirname(path) or "."
        try:
            os.makedirs(d, exist_ok=True)
        except PermissionError:
            # Fall back to /tmp if the configured dir isn't writable.
            path = "/tmp/safe_restart.jsonl"
        try:
            if os.path.getsize(path) >= _AUDIT_LOG_MAX_BYTES:
                os.replace(path, path + ".1")
        except (FileNotFoundError, OSError):
            pass
        event = dict(event)
        event.setdefault("ts", time.time())
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n")
    except Exception:
        logger.debug("safe_restart: audit write failed", exc_info=True)


def _import_restart_helpers():
    """Resolve restart_rtl_airband / restart_rtl_ground at call time.

    Late import keeps this module importable in test contexts where
    ui.systemd may not yet be initialized, and avoids any circular
    import between handlers/squelch_tracker → airband_restart → systemd.
    """
    try:
        try:
            from .systemd import (  # type: ignore[no-redef]
                restart_rtl_airband as _ra,
                restart_rtl_ground as _rg,
                rtl_airband_restart_state as _ras,
                rtl_ground_restart_state as _rgs,
            )
        except ImportError:
            from ui.systemd import (  # type: ignore[no-redef]
                restart_rtl_airband as _ra,
                restart_rtl_ground as _rg,
                rtl_airband_restart_state as _ras,
                rtl_ground_restart_state as _rgs,
            )
        return _ra, _rg, _ras, _rgs
    except Exception:
        logger.exception("safe_restart: failed to import restart helpers")
        return None, None, None, None


def _import_unit_helpers():
    """Late-bind _restart_unit + UNITS for OP25/VFO follow-up."""
    try:
        try:
            from .systemd import _restart_unit as _ru  # type: ignore
        except ImportError:
            from ui.systemd import _restart_unit as _ru  # type: ignore
        try:
            from .config import UNITS as _UNITS  # type: ignore
        except ImportError:
            from ui.config import UNITS as _UNITS  # type: ignore
        return _ru, _UNITS
    except Exception:
        logger.exception("safe_restart: failed to import unit helpers")
        return None, {}


def _normalize_bands(bands: Iterable[str]) -> list[str]:
    """Canonicalize + de-duplicate, ordering Master (airband) first."""
    seen: list[str] = []
    for b in bands or []:
        key = str(b or "").strip().lower()
        canon = _BAND_CANONICAL.get(key)
        if canon and canon not in seen:
            seen.append(canon)
    # Master before Slave when both requested — Slave's open requires
    # Master already running.
    seen.sort(key=lambda b: 0 if b == "airband" else 1)
    return seen


def safe_restart_rtl_airband(
    bands: Sequence[str] = ("airband", "ground"),
    *,
    reason: str = "unspecified",
    also_restart_op25: bool = False,
    also_restart_vfo: bool = False,
    block_if_in_flight: bool = False,
    in_flight_timeout_sec: float = 1.0,
) -> dict:
    """Restart rtl-airband band services with SDRplay-recovery fallback.

    ``bands``: subset of {"airband", "ground"}.  Always canonicalized
    and de-duplicated; Master (``airband``) is restarted first if both
    are requested.

    Behavior:

    1. Acquire the module-level idempotency lock.  If another restart
       is already in flight and ``block_if_in_flight`` is False (the
       default), return immediately with ``status="in_flight_skipped"``.
       If True, wait up to ``in_flight_timeout_sec`` for the lock.
    2. For each band, call the existing sequenced-restart helper in
       ``ui.systemd`` (gentle → escalate-with-sdrplay-bounce → probe).
       Snapshot ``wedge_recovery_total`` before/after to detect
       whether escalation actually fired.
    3. If ``also_restart_op25`` is True, restart OP25 AFTER the bands
       are healthy.  Failure does not fail the overall result.
    4. If ``also_restart_vfo`` is True, restart scanner-vfo similarly.
    5. Audit the event and return.

    Returns:
        {
          "status": "ok" | "in_flight_skipped" | "error",
          "bands": [...],
          "results": {band: {"ok", "error", "escalated", "elapsed_s"}},
          "restarted_sdrplay": bool,
          "mounts_ok": [band, ...],   # bands whose underlying probe passed
          "op25": {"ok", "error"} | None,
          "vfo":  {"ok", "error"} | None,
          "elapsed_s": float,
          "reason": str,
        }
    """
    started = time.time()
    norm_bands = _normalize_bands(bands)

    # ------------------------------------------------------------------
    # Idempotency gate.
    # ------------------------------------------------------------------
    deadline = started + max(0.0, float(in_flight_timeout_sec))
    acquired = _SAFE_RESTART_LOCK.acquire(blocking=False)
    while not acquired and block_if_in_flight and time.time() < deadline:
        time.sleep(0.05)
        acquired = _SAFE_RESTART_LOCK.acquire(blocking=False)
    if not acquired:
        with _INFLIGHT_LOCK:
            in_flight_snapshot = dict(_SAFE_RESTART_IN_FLIGHT)
        elapsed = time.time() - started
        result = {
            "status": "in_flight_skipped",
            "bands": norm_bands,
            "results": {},
            "restarted_sdrplay": False,
            "mounts_ok": [],
            "op25": None,
            "vfo": None,
            "elapsed_s": round(elapsed, 3),
            "reason": reason,
            "in_flight": in_flight_snapshot,
        }
        _audit({"event": "in_flight_skipped", **result})
        logger.info(
            "safe_restart: skipped (in flight) reason=%r in_flight=%r",
            reason, in_flight_snapshot,
        )
        return result

    try:
        with _INFLIGHT_LOCK:
            _SAFE_RESTART_IN_FLIGHT.update({
                "active": True,
                "started_ts": started,
                "bands": tuple(norm_bands),
                "reason": reason,
            })

        _ra, _rg, _ras, _rgs = _import_restart_helpers()
        if not (_ra and _rg and _ras and _rgs):
            elapsed = time.time() - started
            result = {
                "status": "error",
                "bands": norm_bands,
                "results": {},
                "restarted_sdrplay": False,
                "mounts_ok": [],
                "op25": None, "vfo": None,
                "elapsed_s": round(elapsed, 3),
                "reason": reason,
                "error": "restart helpers not importable",
            }
            _audit({"event": "import_failed", **result})
            return result

        _audit({"event": "started", "bands": norm_bands, "reason": reason,
                "also_op25": bool(also_restart_op25),
                "also_vfo": bool(also_restart_vfo)})

        # Snapshot wedge-recovery counters from both bands so we can
        # detect which one (if any) escalated.
        pre_recoveries = {
            "airband": int((_ras() or {}).get("wedge_recovery_total", 0)),
            "ground":  int((_rgs() or {}).get("wedge_recovery_total", 0)),
        }

        per_band: dict[str, dict] = {}
        mounts_ok: list[str] = []
        any_escalated = False
        for band in norm_bands:
            fn = _ra if band == "airband" else _rg
            state_fn = _ras if band == "airband" else _rgs
            band_started = time.time()
            try:
                ok, err = fn(reason=f"safe_restart:{reason}")
            except Exception as exc:
                logger.exception("safe_restart: %s raised", band)
                ok, err = False, f"exception: {exc}"
            band_elapsed = time.time() - band_started
            post = int((state_fn() or {}).get("wedge_recovery_total", 0))
            escalated = post > pre_recoveries[band]
            any_escalated = any_escalated or escalated
            per_band[band] = {
                "ok": bool(ok),
                "error": err or "",
                "escalated": bool(escalated),
                "elapsed_s": round(band_elapsed, 3),
            }
            if ok:
                # The per-band helper only returns True after its
                # post-start probe verifies mount publishing (see
                # _wait_for_rtl_airband_health in ui.systemd).
                mounts_ok.append(band)
            pre_recoveries[band] = post  # update for the next band

        # OP25 / VFO follow-up.  Failure does NOT fail the wrapper —
        # surface in the result, operator can re-trigger.
        op25_result = None
        vfo_result = None
        if also_restart_op25 or also_restart_vfo:
            _ru, UNITS = _import_unit_helpers()
            if _ru:
                if also_restart_op25:
                    op25_unit = str(UNITS.get("digital")
                                    or "scanner-digital-op25.service")
                    ok, err = _ru(op25_unit, use_sudo=True)
                    op25_result = {"ok": bool(ok), "error": err or ""}
                if also_restart_vfo:
                    vfo_unit = str(UNITS.get("vfo") or "scanner-vfo.service")
                    ok, err = _ru(vfo_unit, use_sudo=True)
                    vfo_result = {"ok": bool(ok), "error": err or ""}

        all_ok = all(r["ok"] for r in per_band.values()) if per_band else False
        elapsed = time.time() - started
        result = {
            "status": "ok" if all_ok else "error",
            "bands": norm_bands,
            "results": per_band,
            "restarted_sdrplay": bool(any_escalated),
            "mounts_ok": mounts_ok,
            "op25": op25_result,
            "vfo": vfo_result,
            "elapsed_s": round(elapsed, 3),
            "reason": reason,
        }
        _audit({"event": "completed", **result})
        logger.info(
            "safe_restart: %s bands=%s elapsed=%.1fs sdrplay_recovered=%s",
            result["status"], norm_bands, elapsed, any_escalated,
        )
        return result

    finally:
        with _INFLIGHT_LOCK:
            _SAFE_RESTART_IN_FLIGHT.update({
                "active": False,
                "started_ts": 0.0,
                "bands": (),
                "reason": "",
            })
        try:
            _SAFE_RESTART_LOCK.release()
        except RuntimeError:
            pass


def safe_restart_in_flight() -> dict:
    """Read-only snapshot of the in-flight state (for sitrep / debug)."""
    with _INFLIGHT_LOCK:
        return dict(_SAFE_RESTART_IN_FLIGHT)
