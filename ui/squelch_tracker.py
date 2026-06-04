"""SB5 Phase 2: continuous squelch noise-floor tracker.

Background thread that wakes every ``SQUELCH_TRACKER_PERIOD_SEC`` (default
30 s), reads the current per-channel noise floor for each AUTO-enabled
band from /run/rtl_airband_<svc>_stats.txt, recomputes per-channel
squelch thresholds at the band's active preset margin, and — if any
channel changed by at least ``SQUELCH_TRACKER_HYSTERESIS_DB`` (default
5 dB) vs the on-disk values — writes the new list and restarts that
band's rtl-airband unit.

Why a restart at all
--------------------
rtl-airband v5.1.1 has no hot-reload signal.  ``strings`` on the binary
shows exactly one signal-handling string ("Got signal %d, exiting") and
only one ``sighandler`` symbol.  SIGHUP, SIGUSR1, SIGUSR2 are all caught
by the same exit handler.  Phase 1 already accepted a restart on chip
click; Phase 2 mitigates the cost with wide hysteresis so the tracker
fires once an hour or less in steady state, not every cycle.

SDRplay master/slave race
-------------------------
We delegate restarts to ``safe_restart_rtl_airband`` so the tracker
shares the same idempotency lock + sdrplay-daemon recovery path as
the operator's Sitrep → Reset Radios button.

Older versions of this module ran a bare ``sudo -n /bin/systemctl
restart`` and explicitly avoided the escalating helper, on the
reasoning that "the autopilot must never bounce SDRplay as a fix."
That stance turned out to be a footgun: on 2026-06-03 the bare-restart
path SIGKILLed rtl-airband mid-SDRplay-teardown, corrupted the
shared-memory semaphores, and wedged every subsequent open.  Routing
through the safe wrapper means (a) the tracker no-ops if another
restart is already in flight (no stacking with operator chip clicks),
and (b) if the gentle restart probe-fails, the wrapper transparently
performs the sdrplay daemon recovery rather than leaving a wedged
mount behind.

Failure modes (rock-solid pillar)
---------------------------------
* stats file missing                 → hold thresholds, log warning
* stats present but channel NaN      → skip that channel, keep its threshold
* stats values garbled               → skip, log, continue
* rtl-airband unit not active        → skip cycle, log, continue
* restart command failed             → log, do NOT retry, do NOT escalate
"""
from __future__ import annotations

import json
import logging
import os
import statistics
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

try:
    from .config import (
        RTL_AIRBAND_AIRBAND_STATS_PATH,
        RTL_AIRBAND_GROUND_STATS_PATH,
    )
    from .squelch_preset import (
        DEFAULT_PRESET,
        margin_for,
        normalize_preset,
        read_noise_floor_for_target,
        read_profile_freqs,
        compute_threshold_list,
        write_per_channel_squelch_list,
        read_current_thresholds,
    )
    from .managed_analog_controls import (
        recommended_managed_controls,
        persist_managed_controls_override,
        get_band_squelch_auto,
        record_tracker_apply,
    )
    from .profile_config import resolve_controls_path, parse_controls
except ImportError:
    from ui.config import (
        RTL_AIRBAND_AIRBAND_STATS_PATH,
        RTL_AIRBAND_GROUND_STATS_PATH,
    )
    from ui.squelch_preset import (
        DEFAULT_PRESET,
        margin_for,
        normalize_preset,
        read_noise_floor_for_target,
        read_profile_freqs,
        compute_threshold_list,
        write_per_channel_squelch_list,
        read_current_thresholds,
    )
    from ui.managed_analog_controls import (
        recommended_managed_controls,
        persist_managed_controls_override,
        get_band_squelch_auto,
        record_tracker_apply,
    )
    from ui.profile_config import resolve_controls_path, parse_controls

logger = logging.getLogger(__name__)


# --- tunables ----------------------------------------------------------------
# Deployable pillar: every knob below is an env var with a safe default.

SQUELCH_TRACKER_PERIOD_SEC: float = float(
    os.getenv("SQUELCH_TRACKER_PERIOD_SEC", "30.0")
)
# Apply only if at least one channel's threshold would change by this many dB.
# Wide-hysteresis fallback (Option B): the rtl-airband binary has no
# hot-reload signal, so every apply costs a service restart.  5 dB keeps
# steady-state restarts at roughly once an hour or less.
SQUELCH_TRACKER_HYSTERESIS_DB: float = float(
    os.getenv("SQUELCH_TRACKER_HYSTERESIS_DB", "5.0")
)
# Floor on time between successive applies for the same band.  Defends
# against any pathological case where two consecutive cycles happen to
# straddle the hysteresis window.
SQUELCH_TRACKER_MIN_REAPPLY_SEC: float = float(
    os.getenv("SQUELCH_TRACKER_MIN_REAPPLY_SEC", "60.0")
)

# Poison-noise-floor rejection ceilings.  rtl-airband's NFM noise
# estimator returns its init constant (linear 0.283, ~-10.964 dBFS)
# before audio samples buffer post-restart.  Tracker reading that
# stale init applies threshold ~-5 dBFS, silencing the entire band
# until the next 30s cycle reads a real value.  Reject medians above
# these ceilings as poison.
#
# Real NFM hiss baseline floats around -32 to -38 dBFS; real AM
# quiescent around -70 to -75 dBFS.  The 35-40 dB gap leaves room
# for genuine RF anomalies (strong nearby signal raising the median)
# without catching them as poison.
SQUELCH_TRACKER_POISON_CEILING_AM_DBFS: float = float(
    os.getenv("SQUELCH_TRACKER_POISON_CEILING_AM_DBFS", "-55.0")
)
SQUELCH_TRACKER_POISON_CEILING_NFM_DBFS: float = float(
    os.getenv("SQUELCH_TRACKER_POISON_CEILING_NFM_DBFS", "-20.0")
)

# Audit log — rolling JSONL, one event per line.  Mirrors the existing
# dongle-events.jsonl pattern under ~/.cache/airband-ui/.
SQUELCH_TRACKER_AUDIT_LOG_PATH: str = os.getenv(
    "SQUELCH_TRACKER_AUDIT_LOG_PATH",
    str(Path.home() / ".cache" / "airband-ui" / "squelch_tracker.jsonl"),
)
SQUELCH_TRACKER_AUDIT_LOG_MAX_BYTES: int = int(
    os.getenv("SQUELCH_TRACKER_AUDIT_LOG_MAX_BYTES", "1048576")  # 1 MiB
)

# Band <-> systemd service mapping.  We deliberately do NOT use
# restart_rtl_airband() / restart_rtl_ground() because those escalate
# to bouncing the SDRplay daemon on failure.  The autopilot must not
# attempt recovery; that's an operator-initiated action.
_BAND_TO_UNIT = {
    "airband": "rtl-airband-airband.service",
    "ground":  "rtl-airband-ground.service",
}


def _poison_ceiling_for_band(band: str) -> float:
    """Per-band noise-floor ceiling above which a sample is "poison".

    airband uses AM modulation, ground uses NFM; rtl-airband uses a
    different noise estimator per modulation and the stale init values
    differ.  Real-world hiss baselines also differ, so we use a
    different ceiling per band.
    """
    if band == "airband":
        return SQUELCH_TRACKER_POISON_CEILING_AM_DBFS
    return SQUELCH_TRACKER_POISON_CEILING_NFM_DBFS


# --- runtime state ----------------------------------------------------------
# Tracks last-applied wall-clock per band and an opt-out flag if a band's
# restart command has failed (so we don't keep hammering a wedged unit).

_state_lock = threading.Lock()
_last_apply_ms: dict[str, int] = {"airband": 0, "ground": 0}
_last_cycle_ms: dict[str, int] = {"airband": 0, "ground": 0}
_band_disabled_until_ms: dict[str, int] = {"airband": 0, "ground": 0}


def get_tracker_status(band: str) -> dict[str, Any]:
    """Read-only snapshot of tracker state for SSE / UI consumption."""
    b = "airband" if str(band or "").lower() in ("air", "airband") else "ground"
    with _state_lock:
        return {
            "last_apply_ms": int(_last_apply_ms.get(b) or 0),
            "last_cycle_ms": int(_last_cycle_ms.get(b) or 0),
            "disabled_until_ms": int(_band_disabled_until_ms.get(b) or 0),
        }


# --- audit log writer -------------------------------------------------------

def _audit_log_path() -> str:
    return str(SQUELCH_TRACKER_AUDIT_LOG_PATH or "").strip()


def _rotate_if_needed(path: str) -> None:
    """Cap audit log size; rename to .1 when over the byte limit."""
    try:
        sz = os.path.getsize(path)
    except OSError:
        return
    if sz < SQUELCH_TRACKER_AUDIT_LOG_MAX_BYTES:
        return
    backup = path + ".1"
    try:
        if os.path.exists(backup):
            os.remove(backup)
        os.replace(path, backup)
    except OSError:
        logger.debug("squelch_tracker: audit log rotate failed", exc_info=True)


def write_audit_event(event: dict[str, Any]) -> None:
    """Append a single JSON line to the rolling audit log.

    Best-effort: failures here NEVER raise into the tracker loop — at
    worst we lose audit visibility, not the autopilot.
    """
    path = _audit_log_path()
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        _rotate_if_needed(path)
        line = json.dumps(event, separators=(",", ":"), sort_keys=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        logger.debug("squelch_tracker: audit write failed", exc_info=True)


# --- restart helper ---------------------------------------------------------

def _restart_band_unit(band: str) -> tuple[bool, str]:
    """Restart the rtl-airband-<band> service via the safe wrapper.

    Delegates to ``ui.airband_restart.safe_restart_rtl_airband`` so
    the tracker:
      * shares the module-level idempotency lock with the operator's
        Sitrep → Reset Radios path (no overlapping restarts),
      * gets sdrplay-daemon recovery on probe failure (the bare
        ``systemctl restart`` path silently leaves a wedged mount),
      * never restarts OP25 / VFO from the tracker (those are not
        the tracker's job — only the operator-initiated path touches
        them).

    Returns ``(ok, err)`` to keep the existing caller contract.
    """
    if band not in _BAND_TO_UNIT:
        return False, f"unknown band: {band}"
    try:
        try:
            from .airband_restart import safe_restart_rtl_airband
        except ImportError:
            from ui.airband_restart import safe_restart_rtl_airband  # type: ignore
    except Exception as exc:
        return False, f"safe_restart import failed: {exc}"
    try:
        result = safe_restart_rtl_airband(
            bands=(band,),
            reason=f"squelch_tracker:{band}",
            also_restart_op25=False,
            also_restart_vfo=False,
        )
    except Exception as exc:
        return False, f"safe_restart raised: {exc}"
    status = str(result.get("status") or "")
    if status == "ok":
        return True, ""
    if status == "in_flight_skipped":
        # Another restart (likely the operator's) is in flight — the
        # tracker should not stack on top.  Treat as a soft success so
        # we don't disable the band; the in-flight restart will apply
        # the freshly-written thresholds when it brings the unit back.
        return True, ""
    per_band = result.get("results") or {}
    err_detail = (per_band.get(band) or {}).get("error") or status
    return False, f"safe_restart {status}: {err_detail}"


# --- one tracker cycle for one band -----------------------------------------

def _max_abs_delta(new: list[int], old: list[int]) -> int:
    """Largest absolute per-channel change between two equal-length lists."""
    if not new or not old:
        return 0
    n = min(len(new), len(old))
    return max((abs(int(new[i]) - int(old[i])) for i in range(n)), default=0)


def _run_cycle_for_band(band: str, *, force: bool = False) -> dict[str, Any]:
    """One sample-and-maybe-apply pass for a single band.

    Returns a dict capturing what happened (used by the audit log
    writer in the cycle loop).  Never raises; failures degrade to
    "skip" with ``error`` populated.
    """
    now_ms = int(time.time() * 1000)
    with _state_lock:
        _last_cycle_ms[band] = now_ms
        cooldown = _last_apply_ms.get(band, 0) + int(SQUELCH_TRACKER_MIN_REAPPLY_SEC * 1000)
        disabled_until = _band_disabled_until_ms.get(band, 0)

    if disabled_until and disabled_until > now_ms and not force:
        return {"band": band, "skipped": "band_disabled", "until_ms": disabled_until}

    try:
        conf_path = resolve_controls_path(band)
    except Exception as exc:
        return {"band": band, "skipped": "no_profile", "error": str(exc)}

    # Pull current preset/margin from persisted override; if absent,
    # default to "balanced".  We do NOT change the preset — the operator
    # owns that choice via the chips.
    rec = recommended_managed_controls(band, conf_path) or {}
    preset = normalize_preset(rec.get("squelch_preset") or DEFAULT_PRESET)
    margin = margin_for(preset)

    noise_map = read_noise_floor_for_target(band)
    if not noise_map:
        return {"band": band, "skipped": "no_stats", "preset": preset}

    freqs = read_profile_freqs(conf_path)
    if not freqs:
        return {"band": band, "skipped": "no_freqs", "preset": preset}

    new_thresholds, noise_used = compute_threshold_list(freqs, noise_map, margin)

    # --- poison noise-floor rejection ----------------------------------
    # See SQUELCH_TRACKER_POISON_CEILING_* docs above.  Catches the
    # rtl-airband pre-buffer init constant that would otherwise
    # collapse thresholds to ~-5 dBFS and silence the band.
    ceiling = _poison_ceiling_for_band(band)
    median_noise_used = (
        float(statistics.median(noise_used)) if noise_used else None
    )
    if median_noise_used is not None and median_noise_used > ceiling:
        would_threshold_median = (
            int(statistics.median(new_thresholds))
            if new_thresholds else None
        )
        return {
            "band": band,
            "preset": preset,
            "margin_db": margin,
            "freqs_count": len(freqs),
            "noise_floor_median": median_noise_used,
            "skipped": "poison_noise_floor",
            "poison_ceiling_dbfs": float(ceiling),
            "would_threshold_median": would_threshold_median,
            "reason": "noise_floor_median above poison ceiling",
        }

    cur_thresholds = read_current_thresholds(conf_path)

    # --- per-channel poison sanity -------------------------------------
    # Even when the band median is sane, a single-channel race could
    # still hand us a poison sample for one freq.  Fall back to that
    # channel's prior threshold so we never write a poison-derived
    # value into the on-disk profile.
    sanitized_channels: list[dict[str, Any]] = []
    if noise_used:
        for i, n in enumerate(noise_used):
            try:
                n_f = float(n)
            except (TypeError, ValueError):
                continue
            if n_f <= ceiling:
                continue
            if cur_thresholds and i < len(cur_thresholds):
                fallback = int(cur_thresholds[i])
                src_label = "prior_threshold"
            else:
                # No prior threshold on disk yet — clamp to the safe
                # floor rather than a poison-derived value.
                fallback = -100
                src_label = "floor"
            sanitized_channels.append({
                "i": i,
                "noise_used_dbfs": n_f,
                "poisoned_threshold": int(new_thresholds[i]),
                "fallback": fallback,
                "fallback_source": src_label,
            })
            new_thresholds[i] = fallback

    delta = _max_abs_delta(new_thresholds, cur_thresholds) if cur_thresholds else 0

    cooldown_active = (not force) and (now_ms < cooldown)
    triggered = bool(new_thresholds) and (
        force
        or not cur_thresholds
        or (delta >= SQUELCH_TRACKER_HYSTERESIS_DB and not cooldown_active)
    )

    result: dict[str, Any] = {
        "band": band,
        "preset": preset,
        "margin_db": margin,
        "freqs_count": len(freqs),
        "noise_floor_median": (
            float(sorted(noise_used)[len(noise_used) // 2]) if noise_used else None
        ),
        "poison_ceiling_dbfs": float(ceiling),
        "sanitized_channels": sanitized_channels,
        "sanitized_count": len(sanitized_channels),
        "max_delta_db": int(delta),
        "hysteresis_db": SQUELCH_TRACKER_HYSTERESIS_DB,
        "current_thresholds_present": bool(cur_thresholds),
        "applied": False,
    }

    if not triggered:
        result["skipped"] = (
            "cooldown" if cooldown_active else "below_hysteresis"
        )
        return result

    # Identify which channels actually moved so the audit log captures
    # the operator-facing "what changed" answer, not just the median.
    touched: list[dict[str, Any]] = []
    for i, (new_v, freq_mhz) in enumerate(zip(new_thresholds, freqs)):
        old_v = cur_thresholds[i] if i < len(cur_thresholds) else None
        if old_v is None or abs(int(new_v) - int(old_v)) >= 1:
            touched.append({
                "i": i,
                "freq_mhz": float(freq_mhz),
                "old": (int(old_v) if old_v is not None else None),
                "new": int(new_v),
            })

    try:
        changed = write_per_channel_squelch_list(conf_path, new_thresholds)
    except Exception as exc:
        result["error"] = f"write_failed: {exc}"
        return result

    result["write_changed"] = bool(changed)
    result["touched"] = touched
    result["touched_count"] = len(touched)

    ok, err = _restart_band_unit(band)
    result["restart_ok"] = bool(ok)
    if not ok:
        # Operator-initiated recovery only.  Stop adjusting this band
        # for SQUELCH_TRACKER_MIN_REAPPLY_SEC * 4 to avoid hammering.
        with _state_lock:
            _band_disabled_until_ms[band] = now_ms + int(SQUELCH_TRACKER_MIN_REAPPLY_SEC * 4 * 1000)
        result["error"] = f"restart_failed: {err}"
        return result

    with _state_lock:
        _last_apply_ms[band] = now_ms

    # Persist the new noise/threshold metadata into the managed
    # override so the SSE readout shows fresh data (Phase 1 hydration
    # path already reads these fields).
    try:
        try:
            cur_gain, cur_snr, _cur_dbfs, _cur_mode = parse_controls(conf_path)
        except Exception:
            cur_gain, cur_snr = 32.8, 10.0
        median_threshold = (
            int(sorted(new_thresholds)[len(new_thresholds) // 2])
            if new_thresholds else -60
        )
        median_noise = (
            float(sorted(noise_used)[len(noise_used) // 2])
            if noise_used else 0.0
        )
        persist_managed_controls_override(
            band,
            conf_path,
            gain=float(cur_gain),
            squelch_mode="dbfs",
            squelch_snr=float(cur_snr),
            squelch_dbfs=float(median_threshold),
            squelch_preset=preset,
            squelch_preset_margin_db=float(margin),
            squelch_preset_noise_floor_dbfs=median_noise,
            squelch_preset_computed_at_ms=now_ms,
        )
    except Exception:
        logger.debug("squelch_tracker: persist override skipped", exc_info=True)

    # Stamp the tracker apply timestamp into the state file so MANUAL
    # UI knows when AUTO last fired for this band.
    try:
        record_tracker_apply(band, now_ms)
    except Exception:
        logger.debug("squelch_tracker: record_tracker_apply skipped", exc_info=True)

    result["applied"] = True
    return result


# --- main loop --------------------------------------------------------------

_thread: threading.Thread | None = None
_stop_event = threading.Event()


def _tracker_loop() -> None:
    """Background loop body.  Wakes every SQUELCH_TRACKER_PERIOD_SEC."""
    logger.info(
        "squelch_tracker: starting period=%.1fs hysteresis=%.1fdB log=%s",
        SQUELCH_TRACKER_PERIOD_SEC,
        SQUELCH_TRACKER_HYSTERESIS_DB,
        _audit_log_path(),
    )
    write_audit_event({
        "ts_ms": int(time.time() * 1000),
        "event": "tracker_start",
        "period_sec": SQUELCH_TRACKER_PERIOD_SEC,
        "hysteresis_db": SQUELCH_TRACKER_HYSTERESIS_DB,
    })

    while not _stop_event.is_set():
        cycle_start = time.monotonic()
        for band in ("airband", "ground"):
            try:
                if not get_band_squelch_auto(band):
                    # Operator turned AUTO off for this band — nothing
                    # to do, don't even log (would spam the audit file).
                    continue
                result = _run_cycle_for_band(band)
                # Always log applies; log skips only when they carry
                # an unusual signal (no_stats / error / restart_failed).
                if result.get("applied") or result.get("error") or result.get("skipped") in ("no_stats", "no_profile", "no_freqs", "band_disabled", "poison_noise_floor") or result.get("sanitized_count"):
                    event = {
                        "ts_ms": int(time.time() * 1000),
                        "event": (
                            "apply" if result.get("applied")
                            else ("error" if result.get("error") else "skip")
                        ),
                        **result,
                    }
                    write_audit_event(event)
            except Exception:
                logger.exception("squelch_tracker: cycle failed for %s", band)
                write_audit_event({
                    "ts_ms": int(time.time() * 1000),
                    "event": "cycle_exception",
                    "band": band,
                })
        # Sleep to next tick, accounting for cycle time
        elapsed = time.monotonic() - cycle_start
        wait = max(0.5, SQUELCH_TRACKER_PERIOD_SEC - elapsed)
        if _stop_event.wait(timeout=wait):
            break

    write_audit_event({
        "ts_ms": int(time.time() * 1000),
        "event": "tracker_stop",
    })
    logger.info("squelch_tracker: stopped")


def start_squelch_tracker() -> None:
    """Idempotent — safe to call from app.main()."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(
        target=_tracker_loop, name="squelch-tracker", daemon=True
    )
    _thread.start()


def stop_squelch_tracker() -> None:
    _stop_event.set()