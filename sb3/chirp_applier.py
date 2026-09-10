"""sb3.chirp_applier — apply an analog profile to the running chirp daemon.

Reads a Profile (air role), pushes its channels to chirp's UDP command bus,
and updates chirp-airband.env if the center frequency or gain changed. Idempotent.

Usage as a module:
    from sb3.chirp_applier import apply_profile_to_chirp
    result = apply_profile_to_chirp(profile, band="airband")

Usage from CLI:
    python3 -m sb3.chirp_applier <profile_name_or_path> [--band airband]
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


CHIRP_UDP_PORT = int(os.environ.get("CHIRP_CMD_PORT", "7400"))
CHIRP_UDP_HOST = os.environ.get("CHIRP_CMD_HOST", "127.0.0.1")
CHIRP_ENV_PATH = Path(
    os.environ.get(
        "CHIRP_AIRBAND_ENV", "/Users/willminkoff/.config/sb3/chirp-airband.env"
    )
)
CHIRP_SERVICE = os.environ.get(
    "CHIRP_LAUNCHD_SERVICE", "com.scannerproject.chirp-airband"
)


# Per-band chirp config: cmd port, env file, launchd service.
_BAND_CFG = {
    "airband": {
        "host": "100.114.219.115",
        "port": 7400,
        "env":  "/Users/willminkoff/.config/sb3/chirp-airband.env",
        "svc":  "com.scannerproject.chirp-airband",
    },
    "ground": {
        "host": "127.0.0.1",
        "port": 7401,
        "env":  "/Users/willminkoff/.config/sb3/chirp-ground.env",
        "svc":  "com.scannerproject.chirp-ground",
    },
    "vfo": {
        "host": "100.114.219.115",
        "port": 7400,
        "env":  "/Users/willminkoff/.config/sb3/chirp-airband.env",
        "svc":  "com.scannerproject.chirp-airband",
    },
}


def _select_band_cfg(band: str) -> dict:
    """Override the module-level chirp targets for one band. Mutates globals."""
    global CHIRP_UDP_PORT, CHIRP_ENV_PATH, CHIRP_SERVICE, CHIRP_UDP_HOST
    cfg = _BAND_CFG.get(band) or _BAND_CFG["airband"]
    CHIRP_UDP_HOST = str(cfg.get("host", "127.0.0.1"))
    CHIRP_UDP_PORT = int(cfg["port"])
    CHIRP_ENV_PATH = Path(cfg["env"])
    CHIRP_SERVICE = str(cfg["svc"])
    return cfg



def _send(cmd: dict, timeout: float = 3.0) -> dict:
    """Fire a JSON command at chirp's UDP bus and return its response."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendto(json.dumps(cmd).encode(), (CHIRP_UDP_HOST, CHIRP_UDP_PORT))
        s.settimeout(timeout)
        data, _ = s.recvfrom(65535)
        return json.loads(data)
    finally:
        s.close()


def _read_env() -> Dict[str, str]:
    if not CHIRP_ENV_PATH.is_file():
        return {}
    out: Dict[str, str] = {}
    for line in CHIRP_ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _write_env(env: Dict[str, str]) -> None:
    lines: List[str] = []
    if CHIRP_ENV_PATH.is_file():
        for line in CHIRP_ENV_PATH.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                lines.append(line)
                continue
            k = stripped.split("=", 1)[0].strip()
            if k in env:
                lines.append(f"{k}={env[k]}")
                del env[k]
            else:
                lines.append(line)
    for k, v in env.items():
        lines.append(f"{k}={v}")
    CHIRP_ENV_PATH.write_text("\n".join(lines) + "\n")


def _restart_chirp() -> None:
    uid = os.getuid()
    subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{uid}/{CHIRP_SERVICE}"],
        check=False,
    )
    # Wait for the daemon to come back up on UDP
    for _ in range(30):
        try:
            r = _send({"v": 1, "id": "ping", "cmd": "get_status", "args": {}}, timeout=1.0)
            if r.get("status") == "ok":
                return
        except (socket.timeout, ConnectionResetError, OSError):
            pass
        time.sleep(1)


def _profile_center_and_gain(prof: Dict[str, Any]) -> tuple[Optional[int], Optional[float]]:
    dev = prof.get("device", {})
    center = dev.get("center_freq_hz")
    # profile schema uses `if_gain_db` (SDRplay negative reduction) — convert to
    # a chirp-style positive gain by absolute value. Chirp's own SDR gain knob
    # is a single "gain_db" that SoapySDRPlay3 maps to IFGR internally.
    ifgr = dev.get("if_gain_db")
    gain: Optional[float] = None
    if ifgr is not None:
        gain = float(abs(ifgr))
    return (int(center) if center else None, gain)


def apply_profile_to_chirp(profile: Dict[str, Any], band: str = "airband") -> Dict[str, Any]:
    """Apply a Profile-shaped dict to the running chirp daemon.

    Returns {"ok": bool, "actions": [...], "warnings": [...], "errors": [...]}.
    """
    if profile.get("role") not in ("air", "vfo", "ground"):
        return {"ok": False, "errors": [
            f"profile role={profile.get('role')!r} — only 'air'/'vfo'/'ground' can drive chirp"]}

    # Per-band chirp target (port/env/service). Overrides module-level constants.
    _select_band_cfg(band)

    actions: List[str] = []
    warnings: List[str] = []
    errors: List[str] = []

    # 1) Update env if center or gain changed.
    center_hz, gain_db = _profile_center_and_gain(profile)
    env = _read_env()
    env_updates: Dict[str, str] = {}
    if center_hz and env.get("CHIRP_SDR_CENTER_FREQ_HZ") != str(center_hz):
        env_updates["CHIRP_SDR_CENTER_FREQ_HZ"] = str(center_hz)
    if gain_db is not None and env.get("CHIRP_SDR_GAIN_DB") != str(gain_db):
        env_updates["CHIRP_SDR_GAIN_DB"] = str(gain_db)
    if env_updates:
        keys = list(env_updates)
        _write_env(env_updates)
        actions.append(f"chirp env updated: {keys}")
        actions.append("restarting sdrplay + chirp to pick up new env")
        import subprocess
        # Kickstart sdrplay API service so chirp can grab RSP1B despite
        # acarsdec/dumpvdl2 holding the other RSPduo. Requires passwordless sudo.
        subprocess.run(
            ["sudo", "launchctl", "kickstart", "-k", "system/com.sdrplay.service"],
            check=False,
        )
        time.sleep(4)
        _restart_chirp()
    else:
        actions.append("chirp env unchanged")

    # 2) Query current chirp channels; remove ones not in profile; add new ones.
    try:
        status = _send({"v": 1, "id": "st", "cmd": "get_status", "args": {}})
    except (socket.timeout, OSError) as exc:
        errors.append(f"chirp daemon unreachable on {CHIRP_UDP_HOST}:{CHIRP_UDP_PORT}: {exc}")
        return {"ok": False, "actions": actions, "warnings": warnings, "errors": errors}

    if status.get("status") != "ok":
        errors.append(f"chirp status returned {status.get('status')}")
        return {"ok": False, "actions": actions, "warnings": warnings, "errors": errors}

    cur_channels = status.get("data", {}).get("channels", [])
    if isinstance(cur_channels, dict):
        cur_ids = set(cur_channels.keys())
    else:  # list of {id, freq_mhz, …}
        cur_ids = {c.get("id") for c in cur_channels if c.get("id")}

    want = []
    for c in profile.get("channels", []):
        title = c.get("title") or c.get("id") or f"ch{c.get('freq_hz')}"
        cid = str(title).lower().replace(" ", "-").replace("/", "-")[:32]
        freq_hz = int(c["freq_hz"])
        demod = str(c.get("demod", "AM")).lower()
        squelch = float(c.get("squelch_db", -55))
        want.append({
            "id": cid,
            "freq_mhz": round(freq_hz / 1e6, 6),
            "mode": demod,
            "squelch": squelch,
            "gain": float(c.get("volume", 3.0)) if c.get("volume") is not None else 3.0,
        })

    want_ids = {w["id"] for w in want}

    # 2a) Remove channels not in profile
    for stale in sorted(cur_ids - want_ids):
        try:
            r = _send({"v": 1, "id": f"rm-{stale}", "cmd": "remove_channel", "args": {"id": stale}})
            if r.get("status") == "ok":
                actions.append(f"removed channel {stale}")
            else:
                warnings.append(f"remove_channel {stale}: {r}")
        except Exception as exc:
            warnings.append(f"remove_channel {stale} raised: {exc}")

    # 2b) Add wanted channels
    add_batch = [{"id": w["id"],
                  "freq_mhz": w["freq_mhz"],
                  "mode": w["mode"],
                  "squelch_dbfs": w["squelch"],
                  "gain_db": w["gain"]}
                 for w in want if w["id"] not in cur_ids]
    if add_batch:
        try:
            r = _send({"v": 1, "id": "batch", "cmd": "add_channel", "args": {"channels": add_batch}})
            if r.get("status") == "ok":
                actions.append(f"added {len(add_batch)} channel(s)")
            else:
                errors.append(f"add_channel batch: {r}")
        except Exception as exc:
            errors.append(f"add_channel batch raised: {exc}")
    else:
        actions.append("no new channels to add")

    return {"ok": not errors,
            "actions": actions, "warnings": warnings, "errors": errors,
            "channels_applied": len(want)}


def _resolve_profile_arg(arg: str) -> Path:
    p = Path(arg)
    if p.is_file():
        return p
    # search under repo profiles/ dir
    root = Path(__file__).resolve().parent.parent  # <repo>
    for cand in (root / "profiles" / f"{arg}.json",
                 root / "profiles" / arg,
                 root / "profiles" / (arg.replace(".", "-") + ".json")):
        if cand.is_file():
            return cand
    raise FileNotFoundError(f"profile not found: {arg}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("profile", help="profile name or path to JSON file")
    ap.add_argument("--band", default="airband", choices=("airband", "vfo"))
    args = ap.parse_args()

    try:
        path = _resolve_profile_arg(args.profile)
    except FileNotFoundError as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)]}))
        return 2

    profile = json.loads(path.read_text())
    result = apply_profile_to_chirp(profile, band=args.band)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 3


if __name__ == "__main__":
    sys.exit(main())
