"""Per-mount playback gain calibration + persistence (Phase 1, 2026-06-05).

The /sb5 player card mixes multiple icecast mounts in the browser via
Web Audio (Phase 3+).  Each mount carries a *calibration gain* — a linear
multiplier applied to that source so all four sit at a common perceived
loudness (``target_lufs``).  Phase 2 measures real baseline LUFS per mount
and writes the derived gains here; Phase 4's auto-leveler nudges them over
time and writes them back through the same endpoint.

State lives in ``data/mount_gains.json`` and is the single source of truth
shared by:
  - the browser player card, via ``GET/PUT /api/audio/mount_gains``
  - the host speaker leveler daemon (Phase 6), which reads the same file.

Schema (flat object, mirrors the JSON file verbatim)::

    {
      "ANALOG": 1.0,            # linear gain multiplier per icecast mount
      "ANALOG_GROUND": 1.0,
      "DIGITAL": 1.0,
      "VFO": 1.0,
      "target_lufs": -18.0      # loudness all mounts are calibrated toward
    }

Mount keys are the icecast mount basenames WITHOUT the ``.mp3`` suffix, so
they line up with ``data-mount`` in sb5.html (``ANALOG.mp3`` → ``ANALOG``).

Public API
----------
``get_state() -> dict``
    The persisted calibration, with every key present and defaulted.

``set_state(partial: dict) -> (ok, err, state)``
    Validate + merge a full-or-partial update, persist atomically, and
    return the merged state.  ``ok`` is False (with ``err``) on a rejected
    value; nothing is written in that case.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_STATE_PATH = (_REPO_ROOT / "data" / "mount_gains.json").resolve()
STATE_PATH = Path(os.getenv("MOUNT_GAINS_STATE_PATH", str(_DEFAULT_STATE_PATH)))

# Icecast mount basenames (no .mp3) — align with sb5.html data-mount values.
MOUNT_KEYS: tuple[str, ...] = ("ANALOG", "ANALOG_GROUND", "DIGITAL", "VFO")

DEFAULT_GAIN = 1.0
DEFAULT_TARGET_LUFS = -18.0

# Sanity bounds.  Gains are linear multipliers: 0 (silence) .. 8x (+18 dB).
# A calibration step never needs more than this; values outside the range
# almost certainly mean a bad write and are rejected rather than clamped.
GAIN_MIN, GAIN_MAX = 0.0, 8.0
# Broadcast/streaming loudness targets live well inside this window.
LUFS_MIN, LUFS_MAX = -40.0, 0.0

_state_lock = threading.Lock()


# ---------------------------------------------------------------------------
# State file IO  (atomic write via tmp + os.replace, matching band_mute.py)
# ---------------------------------------------------------------------------

def _default_state() -> dict[str, float]:
    state = {k: DEFAULT_GAIN for k in MOUNT_KEYS}
    state["target_lufs"] = DEFAULT_TARGET_LUFS
    return state


def _coerce_state(payload: object) -> dict[str, float]:
    """Merge an arbitrary parsed-JSON payload onto the defaults, keeping
    only known keys and coercing to float.  Unknown keys and bad values
    fall back to the default — read paths never raise."""
    out = _default_state()
    if isinstance(payload, dict):
        for k in MOUNT_KEYS:
            try:
                out[k] = float(payload[k])
            except (KeyError, TypeError, ValueError):
                pass
        try:
            out["target_lufs"] = float(payload["target_lufs"])
        except (KeyError, TypeError, ValueError):
            pass
    return out


def _read_state_file() -> dict[str, float]:
    try:
        with STATE_PATH.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        return _default_state()
    except Exception:
        logger.debug(
            "mount_gains: state read failed for %s, defaulting to unity gain",
            STATE_PATH,
            exc_info=True,
        )
        return _default_state()
    return _coerce_state(payload)


def _write_state_file(state: dict[str, float]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    body = {k: float(state.get(k, DEFAULT_GAIN)) for k in MOUNT_KEYS}
    body["target_lufs"] = float(state.get("target_lufs", DEFAULT_TARGET_LUFS))
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(body, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_state() -> dict[str, float]:
    with _state_lock:
        return _read_state_file()


def _validate(partial: dict) -> tuple[bool, str]:
    """Validate a partial update in isolation (only the keys present)."""
    for k, v in partial.items():
        if k == "target_lufs":
            try:
                fv = float(v)
            except (TypeError, ValueError):
                return False, f"target_lufs must be a number, got {v!r}"
            if not (LUFS_MIN <= fv <= LUFS_MAX):
                return False, f"target_lufs {fv} out of range [{LUFS_MIN}, {LUFS_MAX}]"
        elif k in MOUNT_KEYS:
            try:
                fv = float(v)
            except (TypeError, ValueError):
                return False, f"{k} gain must be a number, got {v!r}"
            if not (GAIN_MIN <= fv <= GAIN_MAX):
                return False, f"{k} gain {fv} out of range [{GAIN_MIN}, {GAIN_MAX}]"
        else:
            return False, f"unknown key {k!r}; allowed: {list(MOUNT_KEYS) + ['target_lufs']}"
    return True, ""


def set_state(partial: dict) -> tuple[bool, str, dict[str, float]]:
    """Validate + merge a full-or-partial update and persist it atomically.

    Returns ``(ok, err, state)``.  On a rejected value nothing is written
    and the *current* persisted state is returned alongside ``ok=False``.
    """
    if not isinstance(partial, dict):
        with _state_lock:
            return False, "body must be a JSON object", _read_state_file()
    ok, err = _validate(partial)
    with _state_lock:
        current = _read_state_file()
        if not ok:
            return False, err, current
        merged = dict(current)
        for k, v in partial.items():
            merged[k] = float(v)
        try:
            _write_state_file(merged)
        except Exception as exc:  # noqa: BLE001
            logger.exception("mount_gains: failed writing state to %s", STATE_PATH)
            return False, f"write failed: {exc}", current
        return True, "", merged
