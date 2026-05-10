"""Disco Phase 5 — auto-decode trigger.

When the user clicks "Listen" on a discovered detection, disco wires that
frequency into the running rtl-airband instance so audio actually comes out
the existing Icecast stream.

Strategy (Path C — see Phase 5 plan):
  - rtl-airband does NOT support SIGHUP config reload (verified by inspecting
    binary strings + upstream behavior). Full systemctl restart is the only
    option, but that drops audio for ~few seconds.
  - We therefore minimize restarts by keeping a single "disco scratch" file
    that lists currently-listened freqs. To wire a freq, we read the
    *currently active* airband or ground profile, append the disco scratch
    freqs to it, write a temporary merged profile, swap the symlink, rebuild
    the combined config, and restart rtl-airband once.
  - To unwire, we remove from scratch, regenerate the merged profile (or
    revert to original if scratch is empty), restart again.
  - Scratch state is persisted to JSON under STATE_DIR so it survives
    dashboard restarts.

Supported modulation classes:
  - FM_BROADCAST → mode=fm, bandwidth=200 kHz (rtl-airband supports wide FM
    via larger bandwidth; v0 we accept it but warn it's experimental).
  - FM_NARROW    → mode=nfm, bandwidth=12.5 kHz.
  - AM_VOICE     → mode=am, bandwidth=12 kHz.
  - Anything else returns status=unsupported and is logged.

We use the airband-ui's own profile_config / favorites_runtime helpers when
possible; otherwise we operate via simple file ops + sudo systemctl.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

LOGGER = logging.getLogger("disco.listen")

# Add scannerproject root so we can import ui.profile_config helpers — the
# parsing/writing logic for libconfig-style rtl-airband profiles is non-trivial
# and battle-tested there. Falls back to a local minimal parser if import
# fails (we want disco to be robust to airband-ui code path changes).
_REPO_ROOT = "/home/ubuntu/scannerproject"
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    from ui.profile_config import (
        parse_freqs_labels,
        replace_freqs_labels,
        write_freqs_labels,
        read_active_config_path,
    )
    _UI_AVAILABLE = True
except Exception as e:  # pragma: no cover
    LOGGER.warning("ui.profile_config unavailable: %s — falling back to local parser", e)
    _UI_AVAILABLE = False

# --- constants --------------------------------------------------------------

AIRBAND_MIN_MHZ = 118.0
AIRBAND_MAX_MHZ = 136.0

# Mapping from disco modulation_class to rtl-airband channel parameters.
# bandwidth_hz is what we emit into the profile if the detection's bandwidth
# is missing; the profile only takes a single bandwidth per channel block,
# but rtl-airband supports per-channel modulation in mixed configs.
MOD_TO_CHANNEL_SPEC = {
    "FM_BROADCAST": {"modulation": "fm",  "bandwidth_default_hz": 200_000, "max_bandwidth_hz": 250_000},
    "FM_NARROW":    {"modulation": "nfm", "bandwidth_default_hz": 12_500,  "max_bandwidth_hz": 25_000},
    "AM_VOICE":     {"modulation": "am",  "bandwidth_default_hz": 12_000,  "max_bandwidth_hz": 16_000},
}

ICECAST_HOST_PUBLIC = os.environ.get("DISCO_ICECAST_HOST", "100.67.20.40")
ICECAST_PORT_PUBLIC = int(os.environ.get("DISCO_ICECAST_PORT", "8000"))

# Currently the combined config emits a single "ANALOG.mp3" mount fed by the
# mixer that combines airband + ground devices. Both targets feed that mount,
# so the user always tunes the same URL.
DEFAULT_STREAM_MOUNT = os.environ.get("DISCO_STREAM_MOUNT", "ANALOG.mp3")

PROFILES_DIR = os.environ.get("DISCO_PROFILES_DIR", "/home/ubuntu/scannerproject/profiles")
RUNTIME_DIR = os.environ.get("DISCO_RUNTIME_DIR", "/home/ubuntu/scannerproject/runtime")
STATE_DIR = os.environ.get("DISCO_STATE_DIR", "/run/scannerproject/disco")

# Where we persist the scratch list across restarts.
SCRATCH_STATE_PATH = os.path.join(STATE_DIR, "disco_scratch.json")
# The merged profile (active profile + scratch freqs) we write and symlink.
DISCO_AIRBAND_PROFILE = os.path.join(PROFILES_DIR, "rtl_airband_disco_listen_airband.conf")
DISCO_GROUND_PROFILE = os.path.join(PROFILES_DIR, "rtl_airband_disco_listen_ground.conf")

# The active config symlinks airband-ui maintains.
CONFIG_SYMLINK_AIR = os.path.join(RUNTIME_DIR, "rtl_airband.conf")
CONFIG_SYMLINK_GROUND = os.path.join(RUNTIME_DIR, "rtl_airband_ground.conf")

RTL_UNIT = "rtl-airband"
SYSTEMCTL_BIN = "/bin/systemctl"
BUILD_COMBINED_SCRIPT = "/home/ubuntu/scannerproject/scripts/build-combined-config.py"

# Path used to remember which user-selected profile we hijacked, so on Stop
# (when scratch goes empty) we can restore the symlink to its original target.
ORIGINAL_PROFILE_STATE_PATH = os.path.join(STATE_DIR, "disco_listen_original_profiles.json")

_LOCK = threading.Lock()

# --- data types -------------------------------------------------------------

@dataclass
class ListenRequest:
    freq_hz: float
    bandwidth_hz: float | None
    modulation_class: str
    protocol_tag: str | None = None


@dataclass
class ListenResult:
    status: str            # 'wired' | 'unsupported' | 'error' | 'unwired' | 'no-op'
    detail: str            # stream URL on success, error message otherwise
    stream_url: str = ""
    target: str = ""       # 'airband' | 'ground'
    modulation: str = ""   # rtl-airband mode token


# --- helpers ----------------------------------------------------------------

def _ensure_state_dir() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)


def _is_airband_freq(freq_mhz: float) -> bool:
    return AIRBAND_MIN_MHZ <= freq_mhz <= AIRBAND_MAX_MHZ


def _load_scratch() -> dict[str, Any]:
    """Return persisted scratch state: {airband:[entries], ground:[entries]}.

    Each entry: {freq_hz, label, modulation, bandwidth_hz, ts}.
    """
    try:
        with open(SCRATCH_STATE_PATH) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"airband": [], "ground": []}
        return {
            "airband": list(data.get("airband") or []),
            "ground":  list(data.get("ground") or []),
        }
    except FileNotFoundError:
        return {"airband": [], "ground": []}
    except Exception as e:
        LOGGER.warning("failed reading scratch state: %s", e)
        return {"airband": [], "ground": []}


def _save_scratch(scratch: dict[str, Any]) -> None:
    _ensure_state_dir()
    tmp = SCRATCH_STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(scratch, f, indent=2)
    os.replace(tmp, SCRATCH_STATE_PATH)


def _load_original_profiles() -> dict[str, str]:
    try:
        with open(ORIGINAL_PROFILE_STATE_PATH) as f:
            data = json.load(f)
        return {
            "airband": str(data.get("airband") or ""),
            "ground":  str(data.get("ground") or ""),
        }
    except FileNotFoundError:
        return {"airband": "", "ground": ""}
    except Exception:
        return {"airband": "", "ground": ""}


def _save_original_profiles(orig: dict[str, str]) -> None:
    _ensure_state_dir()
    tmp = ORIGINAL_PROFILE_STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(orig, f, indent=2)
    os.replace(tmp, ORIGINAL_PROFILE_STATE_PATH)


def _resolve_active_profile(target: str) -> str:
    """Return absolute path to the profile currently pointed at by the
    runtime symlink for the given target."""
    sym = CONFIG_SYMLINK_AIR if target == "airband" else CONFIG_SYMLINK_GROUND
    try:
        return os.path.realpath(sym)
    except Exception:
        return sym


def _profile_is_managed_by_disco(path: str) -> bool:
    return os.path.basename(path).startswith("rtl_airband_disco_listen")


def _decide_target(freq_hz: float) -> str:
    return "airband" if _is_airband_freq(freq_hz / 1e6) else "ground"


def _format_label(req: ListenRequest) -> str:
    mhz = req.freq_hz / 1e6
    base = f"DISCO {mhz:.4f}"
    if req.protocol_tag:
        base += f" {req.protocol_tag}"
    elif req.modulation_class:
        base += f" {req.modulation_class}"
    return base[:64]


# --- profile editing --------------------------------------------------------

# Local fallback regex parser, only used if ui.profile_config import fails.
_RE_FREQS = re.compile(r"freqs\s*=\s*\(([^)]*)\)\s*;", re.MULTILINE | re.DOTALL)
_RE_LABELS = re.compile(r"labels\s*=\s*\(([^)]*)\)\s*;", re.MULTILINE | re.DOTALL)


def _local_parse_freqs_labels(text: str):
    m = _RE_FREQS.search(text or "")
    if not m:
        raise ValueError("freqs block not found")
    freqs = []
    for tok in re.findall(r"-?\d+(?:\.\d+)?", m.group(1) or ""):
        try:
            freqs.append(float(tok))
        except ValueError:
            continue
    labels = None
    lm = _RE_LABELS.search(text or "")
    if lm:
        labels = re.findall(r'"([^"]*)"', lm.group(1) or "")
    return freqs, labels


def _read_profile_freqs_labels(path: str) -> tuple[list[float], list[str] | None, str]:
    """Read freqs/labels from a profile file. Return (freqs_mhz, labels, raw_text)."""
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()
    if _UI_AVAILABLE:
        try:
            freqs, labels = parse_freqs_labels(text)
            return freqs, labels, text
        except Exception as e:
            LOGGER.warning("ui parse_freqs_labels failed (%s); falling back", e)
    freqs, labels = _local_parse_freqs_labels(text)
    return freqs, labels, text


def _write_merged_profile(
    *,
    base_profile_path: str,
    output_path: str,
    extra_freqs_mhz: list[float],
    extra_labels: list[str],
) -> bool:
    """Read the base profile, replace its freqs/labels with (base + extras),
    and write to output_path. Returns True if file was created/changed."""
    base_freqs, base_labels, base_text = _read_profile_freqs_labels(base_profile_path)

    # If base has no labels, synthesize labels from frequency strings to keep
    # parity. rtl-airband happily accepts a labels block whose entries match
    # the freq count.
    if base_labels is None:
        base_labels = [f"{f:.4f}" for f in base_freqs]

    # Deduplicate: skip extras that already exist in base (within 1 kHz).
    final_freqs = list(base_freqs)
    final_labels = list(base_labels)
    for f, lab in zip(extra_freqs_mhz, extra_labels):
        if any(abs(f - existing) < 0.001 for existing in final_freqs):
            continue
        final_freqs.append(f)
        final_labels.append(lab or f"{f:.4f}")

    if _UI_AVAILABLE:
        try:
            new_text = replace_freqs_labels(base_text, final_freqs, final_labels)
        except Exception as e:
            LOGGER.warning("replace_freqs_labels failed (%s); cannot merge profile", e)
            return False
    else:
        # Without ui helpers we can't safely rewrite libconfig blocks. Bail.
        LOGGER.error("ui.profile_config not importable; cannot rewrite profile")
        return False

    # Read current output to detect changes
    prior = ""
    try:
        with open(output_path, "r", encoding="utf-8") as f:
            prior = f.read()
    except FileNotFoundError:
        pass
    if prior == new_text:
        return False

    tmp = output_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_text)
    os.replace(tmp, output_path)
    return True


def _swap_symlink(target: str, new_dest: str) -> bool:
    """Atomically point the active config symlink for target to new_dest."""
    sym = CONFIG_SYMLINK_AIR if target == "airband" else CONFIG_SYMLINK_GROUND
    try:
        cur = os.readlink(sym)
    except OSError:
        cur = ""
    if cur == new_dest:
        return False
    tmp = sym + ".tmp"
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(new_dest, tmp)
    os.replace(tmp, sym)
    return True


def _restart_rtl_airband() -> tuple[bool, str]:
    """Rebuild combined config + restart rtl-airband (the only way to pick up
    new freqs since rtl-airband doesn't honor SIGHUP for config reload)."""
    # build-combined-config.py needs the same env file the systemd unit uses;
    # easiest is to invoke systemctl restart which will re-run ExecStartPre.
    cmd = ["sudo", "-n", SYSTEMCTL_BIN, "restart", RTL_UNIT]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return False, "systemctl restart timed out"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "restart failed").strip()
    return True, ""


# --- main API ---------------------------------------------------------------

def _classify_supported(modulation_class: str) -> dict[str, Any] | None:
    return MOD_TO_CHANNEL_SPEC.get((modulation_class or "").upper())


def _stream_url() -> str:
    return f"http://{ICECAST_HOST_PUBLIC}:{ICECAST_PORT_PUBLIC}/{DEFAULT_STREAM_MOUNT}"


def _record_request(
    db_path: str,
    *,
    freq_hz: float,
    bandwidth_hz: float | None,
    modulation_class: str | None,
    requested_action: str,
    status: str,
    detail: str,
    user_id: str | None = None,
) -> None:
    try:
        c = sqlite3.connect(db_path, timeout=5.0)
        c.execute(
            "INSERT INTO decode_requests (ts, freq_hz, bandwidth_hz, modulation_class,"
            " requested_action, status, detail, user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), freq_hz, bandwidth_hz, modulation_class,
             requested_action, status, detail, user_id),
        )
        c.commit()
        c.close()
    except Exception as e:  # pragma: no cover
        LOGGER.warning("failed recording decode_request: %s", e)


def init_schema(db_path: str) -> None:
    """Idempotently create the decode_requests table."""
    c = sqlite3.connect(db_path, timeout=5.0)
    c.execute(
        "CREATE TABLE IF NOT EXISTS decode_requests ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "ts REAL NOT NULL,"
        "freq_hz REAL NOT NULL,"
        "bandwidth_hz REAL,"
        "modulation_class TEXT,"
        "requested_action TEXT,"
        "status TEXT,"
        "detail TEXT,"
        "user_id TEXT)"
    )
    c.commit()
    c.close()


def _apply_scratch_to_runtime(scratch: dict[str, Any]) -> tuple[bool, str]:
    """Synchronize the runtime symlinks + merged profiles to match scratch.

    For each target (airband/ground):
      - If scratch is empty for that target: revert symlink to original (saved
        when we first hijacked it).
      - Else: merge active profile + scratch freqs into the disco-managed
        profile, point symlink at it.

    Then rebuild combined config + restart rtl-airband once.
    """
    orig = _load_original_profiles()
    changed_any = False
    errors = []

    for target, disco_profile in (
        ("airband", DISCO_AIRBAND_PROFILE),
        ("ground",  DISCO_GROUND_PROFILE),
    ):
        entries = scratch.get(target) or []
        cur_active = _resolve_active_profile(target)

        if not entries:
            # Revert to original if we hijacked it.
            if _profile_is_managed_by_disco(cur_active) and orig.get(target):
                if _swap_symlink(target, orig[target]):
                    changed_any = True
                # Wipe the disco profile so it doesn't accumulate stale state.
                try:
                    if os.path.exists(disco_profile):
                        os.remove(disco_profile)
                except Exception:
                    pass
                orig[target] = ""
            continue

        # Determine the base profile we should merge from.
        if _profile_is_managed_by_disco(cur_active):
            # The symlink already points at disco-merged; the *base* is what
            # we recorded in orig.
            base = orig.get(target) or ""
            if not base or not os.path.isfile(base):
                errors.append(f"{target}: lost reference to original profile")
                continue
        else:
            base = cur_active
            orig[target] = base  # remember so we can revert later

        extras_mhz = [e["freq_hz"] / 1e6 for e in entries]
        extras_labels = [e.get("label") or f"{e['freq_hz']/1e6:.4f}" for e in entries]

        try:
            wrote = _write_merged_profile(
                base_profile_path=base,
                output_path=disco_profile,
                extra_freqs_mhz=extras_mhz,
                extra_labels=extras_labels,
            )
            if wrote:
                changed_any = True
        except Exception as e:
            errors.append(f"{target}: merge failed: {e}")
            continue

        if _swap_symlink(target, disco_profile):
            changed_any = True

    _save_original_profiles(orig)

    if not changed_any:
        return True, ""

    ok, err = _restart_rtl_airband()
    if not ok:
        errors.append(f"rtl-airband restart failed: {err}")
        return False, "; ".join(errors)
    return True, "; ".join(errors)


def listen(req: ListenRequest, *, db_path: str, user_id: str | None = None) -> ListenResult:
    spec = _classify_supported(req.modulation_class)
    if spec is None:
        result = ListenResult(
            status="unsupported",
            detail=f"decoder not configured for modulation_class={req.modulation_class}",
        )
        _record_request(
            db_path,
            freq_hz=req.freq_hz, bandwidth_hz=req.bandwidth_hz,
            modulation_class=req.modulation_class,
            requested_action="listen", status=result.status, detail=result.detail,
            user_id=user_id,
        )
        return result

    target = _decide_target(req.freq_hz)
    label = _format_label(req)

    with _LOCK:
        scratch = _load_scratch()
        # Add (or update) entry for this freq.
        existing = [e for e in scratch[target] if abs(e["freq_hz"] - req.freq_hz) > 1.0]
        existing.append({
            "freq_hz": float(req.freq_hz),
            "label": label,
            "modulation": spec["modulation"],
            "bandwidth_hz": float(req.bandwidth_hz or spec["bandwidth_default_hz"]),
            "ts": time.time(),
        })
        scratch[target] = existing
        _save_scratch(scratch)

        ok, err = _apply_scratch_to_runtime(scratch)

    if not ok:
        result = ListenResult(status="error", detail=err or "unknown error")
    else:
        result = ListenResult(
            status="wired",
            detail=_stream_url(),
            stream_url=_stream_url(),
            target=target,
            modulation=spec["modulation"],
        )
    _record_request(
        db_path,
        freq_hz=req.freq_hz, bandwidth_hz=req.bandwidth_hz,
        modulation_class=req.modulation_class,
        requested_action="listen", status=result.status, detail=result.detail,
        user_id=user_id,
    )
    return result


def stop(freq_hz: float, *, db_path: str, user_id: str | None = None) -> ListenResult:
    target = _decide_target(freq_hz)
    with _LOCK:
        scratch = _load_scratch()
        before = len(scratch[target])
        scratch[target] = [e for e in scratch[target] if abs(e["freq_hz"] - freq_hz) > 1.0]
        if len(scratch[target]) == before:
            result = ListenResult(status="no-op", detail="frequency was not active")
            _record_request(
                db_path, freq_hz=freq_hz, bandwidth_hz=None, modulation_class=None,
                requested_action="stop", status=result.status, detail=result.detail,
                user_id=user_id,
            )
            return result
        _save_scratch(scratch)
        ok, err = _apply_scratch_to_runtime(scratch)

    if not ok:
        result = ListenResult(status="error", detail=err or "unknown error")
    else:
        result = ListenResult(status="unwired", detail="stopped")
    _record_request(
        db_path, freq_hz=freq_hz, bandwidth_hz=None, modulation_class=None,
        requested_action="stop", status=result.status, detail=result.detail,
        user_id=user_id,
    )
    return result


def list_active() -> dict[str, Any]:
    """Return current scratch + computed stream URL."""
    scratch = _load_scratch()
    items = []
    for target in ("airband", "ground"):
        for e in scratch.get(target) or []:
            items.append({
                "freq_hz": e["freq_hz"],
                "label": e.get("label") or "",
                "modulation": e.get("modulation") or "",
                "bandwidth_hz": e.get("bandwidth_hz"),
                "target": target,
                "ts": e.get("ts"),
            })
    items.sort(key=lambda x: x["freq_hz"])
    return {"items": items, "stream_url": _stream_url() if items else ""}


def recent_requests(db_path: str, limit: int = 50) -> list[dict[str, Any]]:
    try:
        c = sqlite3.connect(db_path, timeout=5.0)
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT ts, freq_hz, bandwidth_hz, modulation_class, requested_action,"
            " status, detail, user_id FROM decode_requests ORDER BY ts DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        c.close()
        return [dict(r) for r in rows]
    except Exception:
        return []
