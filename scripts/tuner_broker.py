#!/usr/bin/env python3
"""Phase 6d — Tuner ownership broker.

Watches /run/scannerproject/broker/mode.json for {"sounding": bool}
and swaps RTL-SDR dongle ownership between Disco (default) and the
sounding-mode consumers (acarsdec / dumpvdl2) per a declarative policy
file at /home/ubuntu/scannerproject/config/broker_policy.json.

Policy shape:
  {
    "dongles": [
      {"serial": "45469635", "normal_role": "disco", "sounding_role": "disco"},
      {"serial": "61108285", "normal_role": "disco", "sounding_role": "acars"}
    ]
  }

Roles:
  disco   -> disco-sweep@<serial>.service
  acars   -> acarsdec.service (singleton; serial is set in /etc/airband-ui.conf)
  vdl2    -> dumpvdl2@<serial>.service (templated) OR dumpvdl2.service
            (singleton) — broker picks the templated form first; falls back
            to the singleton if the template doesn't exist.

Transitions:
  OFF -> ON:
    1. stop disco-sweep@<serial> for every dongle whose sounding_role != disco
    2. start the sounding consumer for each such dongle
    3. wait up to 15s for activation; if anything fails to activate,
       roll back: stop the sounding consumers, wait 3s, restart the
       disco-sweep instances, set sounding=false in state with last_error.

  ON -> OFF:
    1. stop the sounding consumers
    2. wait 3s for USB release
    3. start disco-sweep@<serial> for the affected dongles

State is written atomically to /run/scannerproject/broker/state.json:
  {
    "updated_ts": "...",
    "sounding": false,
    "dongles": [
      {"serial": "45469635", "current_role": "disco",
       "current_service": "disco-sweep@45469635.service"},
      ...
    ],
    "last_transition_ts": "...",
    "last_error": "..."          (only present after a failure)
  }
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

# -----------------------------------------------------------------
# Paths.
# -----------------------------------------------------------------
STATE_DIR = "/run/scannerproject/broker"
MODE_PATH = os.path.join(STATE_DIR, "mode.json")
STATE_PATH = os.path.join(STATE_DIR, "state.json")

POLICY_CANDIDATES = [
    os.environ.get("BROKER_POLICY"),
    "/home/ubuntu/scannerproject/config/broker_policy.json",
    "/etc/scanner/broker_policy.json",
]

# -----------------------------------------------------------------
# Tuning constants.
# -----------------------------------------------------------------
POLL_INTERVAL_S = 0.5                # mode.json mtime poll cadence
USB_RELEASE_WAIT_S = 3.0             # delay after stopping sounding service before re-claiming for Disco
SOUNDING_START_TIMEOUT_S = 15.0      # max time for a sounding service to become active
SYSTEMCTL_TIMEOUT_S = 10.0           # per-call timeout
STATE_HEARTBEAT_S = 2.0              # rewrite state.json at least this often so the UI's freshness check passes

LOG = logging.getLogger("broker.tuner")
_STOP = False


# -----------------------------------------------------------------
# Helpers — atomic write, systemctl wrappers, role resolution.
# -----------------------------------------------------------------
def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _atomic_write_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def _systemctl(*args: str) -> tuple[int, str]:
    """Run systemctl with a hard timeout. Returns (rc, combined-output)."""
    try:
        proc = subprocess.run(
            ["systemctl", *args],
            capture_output=True,
            text=True,
            timeout=SYSTEMCTL_TIMEOUT_S,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out.strip()
    except subprocess.TimeoutExpired:
        return 124, "systemctl timeout"
    except FileNotFoundError:
        return 127, "systemctl not found"


def _unit_exists(unit: str) -> bool:
    rc, _ = _systemctl("cat", unit)
    return rc == 0


def _is_active(unit: str) -> bool:
    rc, out = _systemctl("is-active", unit)
    return rc == 0 and out.startswith("active")


def _disco_sweep_unit(serial: str) -> str:
    return f"disco-sweep@{serial}.service"


def _sounding_unit_for_role(role: str, serial: str) -> str | None:
    """Map (role, serial) -> systemd unit name.

    For roles that have a templated form, prefer the template so multiple
    serials can run in parallel; fall back to the singleton if no
    template is installed.  For acars we only have a singleton today
    (acarsdec.service); the per-serial wiring lives in /etc/airband-ui.conf.
    """
    role = (role or "").lower().strip()
    if role == "disco":
        return _disco_sweep_unit(serial)
    if role == "acars":
        # Singleton; the configured ACARS_RTL_SERIAL env in the unit
        # picks which dongle it claims.
        return "acarsdec.service"
    if role == "vdl2":
        templated = f"dumpvdl2@{serial}.service"
        if _unit_exists(templated):
            return templated
        return "dumpvdl2.service"
    return None


# -----------------------------------------------------------------
# Policy loading.
# -----------------------------------------------------------------
def _resolve_policy_path() -> str | None:
    for p in POLICY_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    return None


def _load_policy() -> dict | None:
    path = _resolve_policy_path()
    if not path:
        LOG.error(
            "no policy file found; tried: %s",
            ", ".join([p for p in POLICY_CANDIDATES if p]),
        )
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        LOG.error("failed to read policy %s: %s", path, e)
        return None
    if not isinstance(data, dict):
        LOG.error("policy root must be an object: %s", path)
        return None
    dongles = data.get("dongles")
    if not isinstance(dongles, list) or not dongles:
        LOG.error("policy.dongles missing or empty: %s", path)
        return None
    # Light validation.
    cleaned = []
    for d in dongles:
        if not isinstance(d, dict):
            continue
        serial = str(d.get("serial") or "").strip()
        if not serial:
            continue
        normal = str(d.get("normal_role") or "disco").lower()
        sounding = str(d.get("sounding_role") or normal).lower()
        cleaned.append(
            {"serial": serial, "normal_role": normal, "sounding_role": sounding}
        )
    if not cleaned:
        LOG.error("policy contained no valid dongles after validation")
        return None
    return {"path": path, "dongles": cleaned}


# -----------------------------------------------------------------
# Mode reading.
# -----------------------------------------------------------------
_LAST_MODE_MTIME = 0.0


def _read_mode() -> bool:
    """Read the desired sounding boolean. Missing file -> False."""
    try:
        with open(MODE_PATH) as f:
            data = json.load(f)
        return bool(data.get("sounding", False))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False


# -----------------------------------------------------------------
# Swap execution.
# -----------------------------------------------------------------
def _role_for(d: dict, sounding: bool) -> str:
    return d["sounding_role"] if sounding else d["normal_role"]


def _affected_dongles(policy: dict) -> list[dict]:
    """Dongles whose role changes between normal and sounding mode.
    These are the ones the broker actually manipulates on a transition."""
    return [d for d in policy["dongles"] if d["normal_role"] != d["sounding_role"]]


def _wait_for_active(unit: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _is_active(unit):
            return True
        time.sleep(0.5)
    return False


def _stop_unit_safely(unit: str) -> None:
    LOG.info("systemctl stop %s", unit)
    rc, out = _systemctl("stop", unit)
    if rc != 0:
        LOG.warning("stop %s rc=%d: %s", unit, rc, out)


def _start_unit(unit: str) -> tuple[bool, str]:
    LOG.info("systemctl start %s", unit)
    rc, out = _systemctl("start", unit)
    if rc != 0:
        return False, f"start {unit} rc={rc}: {out}"
    return True, ""


def _apply_off_to_on(policy: dict) -> tuple[bool, str]:
    """Transition normal -> sounding. Returns (ok, error_msg)."""
    affected = _affected_dongles(policy)
    if not affected:
        return True, ""

    # 1. Stop disco-sweep@<serial> for every affected dongle.
    for d in affected:
        if d["normal_role"] == "disco":
            _stop_unit_safely(_disco_sweep_unit(d["serial"]))
    # Give the USB stack a moment to release.
    time.sleep(1.0)

    # 2. Start sounding consumers. acarsdec is a singleton; only start once.
    started: list[str] = []
    pending_units: list[tuple[str, str]] = []  # (unit, serial-for-log)
    seen_units: set[str] = set()
    for d in affected:
        unit = _sounding_unit_for_role(d["sounding_role"], d["serial"])
        if not unit:
            continue
        if unit in seen_units:
            continue
        seen_units.add(unit)
        ok, err = _start_unit(unit)
        if not ok:
            # rollback
            for u in started:
                _stop_unit_safely(u)
            time.sleep(USB_RELEASE_WAIT_S)
            for d2 in affected:
                if d2["normal_role"] == "disco":
                    _systemctl("start", _disco_sweep_unit(d2["serial"]))
            return False, err
        started.append(unit)
        pending_units.append((unit, d["serial"]))

    # 3. Watchdog: wait for each unit to reach active.
    for unit, serial in pending_units:
        if not _wait_for_active(unit, SOUNDING_START_TIMEOUT_S):
            err = f"sounding unit {unit} (serial {serial}) failed to reach active within {SOUNDING_START_TIMEOUT_S:.0f}s"
            LOG.error(err)
            # Rollback.
            for u in started:
                _stop_unit_safely(u)
            time.sleep(USB_RELEASE_WAIT_S)
            for d2 in affected:
                if d2["normal_role"] == "disco":
                    _systemctl("start", _disco_sweep_unit(d2["serial"]))
            return False, err

    return True, ""


def _apply_on_to_off(policy: dict) -> tuple[bool, str]:
    """Transition sounding -> normal. Returns (ok, error_msg).

    On this path we tolerate slow disco-sweep startup (sweep takes a
    few seconds to settle on the dongle); we don't roll back from a
    Disco failure since rolling forward into sounding isn't sensible.
    """
    affected = _affected_dongles(policy)
    if not affected:
        return True, ""

    # 1. Stop the sounding consumers. Dedupe (acarsdec singleton).
    seen_units: set[str] = set()
    for d in affected:
        unit = _sounding_unit_for_role(d["sounding_role"], d["serial"])
        if not unit or unit in seen_units:
            continue
        seen_units.add(unit)
        _stop_unit_safely(unit)

    # 2. Wait for USB handle release.
    time.sleep(USB_RELEASE_WAIT_S)

    # 3. Restart disco-sweep@<serial> for affected dongles.
    errors: list[str] = []
    for d in affected:
        if d["normal_role"] != "disco":
            continue
        ok, err = _start_unit(_disco_sweep_unit(d["serial"]))
        if not ok:
            errors.append(err)

    if errors:
        return False, "; ".join(errors)
    return True, ""


# -----------------------------------------------------------------
# State emission.
# -----------------------------------------------------------------
def _write_state(
    policy: dict,
    sounding: bool,
    last_transition_ts: str | None,
    last_error: str | None,
) -> None:
    dongles_view = []
    for d in policy["dongles"]:
        role = _role_for(d, sounding)
        svc = _sounding_unit_for_role(role, d["serial"]) or ""
        dongles_view.append(
            {
                "serial": d["serial"],
                "current_role": role,
                "current_service": svc,
            }
        )
    out = {
        "updated_ts": _now_iso(),
        "sounding": bool(sounding),
        "dongles": dongles_view,
        "last_transition_ts": last_transition_ts,
        "policy_path": policy.get("path"),
    }
    if last_error:
        out["last_error"] = last_error
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
    except OSError as e:
        LOG.error("mkdir %s failed: %s", STATE_DIR, e)
        return
    try:
        _atomic_write_json(STATE_PATH, out)
    except OSError as e:
        LOG.error("state write failed: %s", e)


# -----------------------------------------------------------------
# Main loop.
# -----------------------------------------------------------------
def _ensure_runtime_dir() -> None:
    """Create the broker runtime dir and make it writable by the UI user
    (ubuntu) so /api/sounding POSTs can update mode.json.  The broker
    runs as root; the UI runs as ubuntu.  We chown to ubuntu:ubuntu
    (1000:1000) so the UI can write without sudo.  Falls back to chmod
    0777 if chown fails (e.g. unprivileged dev run)."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
    except OSError as e:
        LOG.error("mkdir %s failed: %s", STATE_DIR, e)
        return
    try:
        # Hardcode 1000:1000 (ubuntu) to match airband-ui.service.
        os.chown(STATE_DIR, 1000, 1000)
        os.chmod(STATE_DIR, 0o755)
    except OSError:
        # If we're not root (e.g. dev run), fall back to permissive mode.
        try:
            os.chmod(STATE_DIR, 0o777)
        except OSError:
            pass
    # Keep any existing mode.json writable by ubuntu.
    if os.path.exists(MODE_PATH):
        try:
            os.chown(MODE_PATH, 1000, 1000)
            os.chmod(MODE_PATH, 0o664)
        except OSError:
            pass


def _handle_stop(signum, _frame):
    global _STOP
    LOG.info("stopping on signal %s", signum)
    _STOP = True


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("BROKER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    policy = _load_policy()
    if not policy:
        LOG.error("no usable policy; exiting")
        return 2

    LOG.info(
        "broker starting (policy=%s, %d dongles)",
        policy["path"],
        len(policy["dongles"]),
    )
    _ensure_runtime_dir()

    # Initial state: whatever mode.json currently says, but DO NOT
    # apply a transition unless the on-disk service state contradicts
    # the request.  Practically: we publish state.json describing the
    # current (assumed) ownership and let real toggles drive swaps.
    current_sounding = _read_mode()
    last_transition_ts: str | None = None
    last_error: str | None = None
    _write_state(policy, current_sounding, last_transition_ts, last_error)
    LOG.info("initial published state: sounding=%s", current_sounding)

    last_mode_mtime = 0.0
    try:
        last_mode_mtime = os.stat(MODE_PATH).st_mtime
    except OSError:
        last_mode_mtime = 0.0

    last_state_emit = time.monotonic()
    while not _STOP:
        try:
            mtime = os.stat(MODE_PATH).st_mtime
        except OSError:
            mtime = 0.0
        if mtime != last_mode_mtime:
            desired = _read_mode()
            last_mode_mtime = mtime
            if desired != current_sounding:
                LOG.info(
                    "mode change requested: sounding %s -> %s",
                    current_sounding,
                    desired,
                )
                if desired:
                    ok, err = _apply_off_to_on(policy)
                else:
                    ok, err = _apply_on_to_off(policy)
                if ok:
                    current_sounding = desired
                    last_error = None
                    last_transition_ts = _now_iso()
                    LOG.info(
                        "transition complete: sounding=%s",
                        current_sounding,
                    )
                else:
                    # Rollback already happened inside the apply call;
                    # sounding stayed False (or got reverted to False
                    # via rollback).
                    current_sounding = False
                    last_error = err or "unknown transition failure"
                    last_transition_ts = _now_iso()
                    LOG.error(
                        "transition failed; rolled back to sounding=false: %s",
                        last_error,
                    )
                _write_state(
                    policy, current_sounding, last_transition_ts, last_error
                )
                last_state_emit = time.monotonic()
        # Periodic heartbeat write so /api/sounding's freshness check
        # never sees a stale file during quiet stretches.
        if time.monotonic() - last_state_emit >= STATE_HEARTBEAT_S:
            _write_state(
                policy, current_sounding, last_transition_ts, last_error
            )
            last_state_emit = time.monotonic()
        time.sleep(POLL_INTERVAL_S)

    LOG.info("broker exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
