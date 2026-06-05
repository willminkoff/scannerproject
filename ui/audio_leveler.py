"""scanner-audio-leveler — apply per-mount calibration gains to the BT-speaker
path (2026-06-05, phase 6).

The browser player (phases 1-3d) reads ``data/mount_gains.json`` and scales
each icecast mount with a Web Audio calibration GainNode so the four mounts
are loudness-balanced and clip-safe. This daemon applies the SAME gains to the
SAME source of truth on the *speaker* side: each ``scanner-vlc-<mount>``
service is a PipeWire sink-input feeding the Bluetooth speaker, and we set that
sink-input's volume to the mount's gain via ``wpctl set-volume``.

Discovery is delegated to :mod:`ui.band_mute`, which already maps a band's
systemd unit -> MainPID -> wpctl stream node (descendant PIDs included) and
sets up the uid-1000 PipeWire subprocess env. We reuse that contract verbatim
and only swap ``set-mute`` for ``set-volume`` — mute (band_mute) and gain
(this module) are independent controls on the same node.

Design (enterprise framing):
  * Least-privilege: runs as the audio user (ubuntu/uid 1000), never root.
  * Structured logs: one JSON object per line to stdout -> journal.
  * Graceful shutdown: SIGTERM/SIGINT set a stop event; the loop exits cleanly.
  * Idempotent: only issues ``set-volume`` when the live volume diverges from
    target, so a steady state is silent; safe to restart at any time.
  * Self-healing: re-resolves node IDs every tick (VLC restarts mint new
    sink-inputs at volume 1.0) and re-applies, so a bounced VLC is re-leveled
    within one poll.
  * Fails loud: ERROR logs when the gains file is missing/unreadable/empty, or
    when NO sink-input resolves at all (PipeWire down / wrong env); partial
    misses (a single VLC mid-restart) log WARNING and retry next tick.
  * Live: watches the gains file mtime at 1s, so an API PUT to
    /api/audio/mount_gains is audible on the speaker within ~1s.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# Reuse band_mute's proven PipeWire discovery + subprocess env. Works whether
# launched as `python3 -m ui.audio_leveler` (package-relative) or as a script.
try:
    from . import band_mute as bm
except ImportError:  # pragma: no cover - direct-script fallback
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ui import band_mute as bm  # type: ignore

_REPO_ROOT = Path(__file__).resolve().parents[1]
GAINS_PATH = Path(
    os.getenv("MOUNT_GAINS_STATE_PATH", str(_REPO_ROOT / "data" / "mount_gains.json"))
)

# mount key (as written in mount_gains.json) -> systemd unit owning the
# PipeWire sink-input that feeds the BT speaker for that mount.
MOUNT_UNITS = {
    "ANALOG":        os.getenv("LEVELER_UNIT_ANALOG",        "scanner-vlc-analog.service"),
    "ANALOG_GROUND": os.getenv("LEVELER_UNIT_ANALOG_GROUND", "scanner-vlc-ground.service"),
    "DIGITAL":       os.getenv("LEVELER_UNIT_DIGITAL",       "scanner-vlc-digital.service"),
    "VFO":           os.getenv("LEVELER_UNIT_VFO",           "scanner-vlc-vfo.service"),
}
MOUNTS = tuple(MOUNT_UNITS.keys())

# Defensive clamp. ui.mount_gains already clamps writes to [0.1, 8.0]; we never
# push a wild value at the speaker. Max 4.0 matches the phase-2b calibration.
GAIN_MIN = float(os.getenv("LEVELER_GAIN_MIN", "0.0"))
GAIN_MAX = float(os.getenv("LEVELER_GAIN_MAX", "4.0"))

POLL_SEC = float(os.getenv("LEVELER_POLL_SEC", "5.0"))    # reconcile cadence (catch VLC restarts)
WATCH_SEC = float(os.getenv("LEVELER_WATCH_SEC", "1.0"))  # gains-file mtime poll (live edits)
VOL_EPS = 0.005                                            # treat |cur-target|<eps as in-sync

_VOL_RE = re.compile(r"Volume:\s*([0-9]+\.?[0-9]*)")
_stop = threading.Event()


def jlog(level: str, event: str, **fields) -> None:
    """Emit one structured JSON line to stdout (captured by the journal)."""
    rec = {"ts": round(time.time(), 3), "level": level, "event": event}
    rec.update(fields)
    try:
        sys.stdout.write(json.dumps(rec, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    except Exception:  # never let logging kill the daemon
        pass


# ---------------------------------------------------------------------------
# gains IO + wpctl get/set (set/get reuse band_mute's audio subprocess env)
# ---------------------------------------------------------------------------

def read_gains() -> dict[str, float]:
    """Parse mount_gains.json -> {MOUNT: clamped gain}. Raises on unreadable."""
    with GAINS_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("mount_gains.json is not a JSON object")
    out: dict[str, float] = {}
    for m in MOUNTS:
        v = data.get(m)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[m] = max(GAIN_MIN, min(GAIN_MAX, float(v)))
    return out


def _resolve_node(unit: str):
    pid = bm._service_main_pid(unit)
    if not pid:
        return None, None
    return pid, bm._sink_input_id_for_pid(pid)


def _get_volume(node: str):
    try:
        res = subprocess.run(
            ["wpctl", "get-volume", str(node)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=2.0, check=False, env=bm._audio_subprocess_env(),
        )
    except Exception:
        return None
    m = _VOL_RE.search(res.stdout or "")
    return float(m.group(1)) if m else None


def _set_volume(node: str, gain: float):
    try:
        res = subprocess.run(
            ["wpctl", "set-volume", str(node), f"{gain:.3f}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=2.0, check=False, env=bm._audio_subprocess_env(),
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"exec: {exc}"
    return res.returncode == 0, (res.stdout or "").strip()[:200]


# ---------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------

def reconcile(gains: dict[str, float], force: bool = False) -> tuple[int, list[str]]:
    """Apply each mount's gain to its sink-input. Only writes when the live
    volume diverges (or force=True on a gains-file change). Returns
    (applied_count, unresolved_mounts)."""
    applied = 0
    unresolved: list[str] = []
    for mount in MOUNTS:
        target = gains.get(mount)
        if target is None:
            continue
        unit = MOUNT_UNITS[mount]
        _pid, node = _resolve_node(unit)
        if not node:
            unresolved.append(mount)
            continue
        cur = None if force else _get_volume(node)
        if cur is not None and abs(cur - target) <= VOL_EPS:
            continue  # already at target — stay quiet
        ok, msg = _set_volume(node, target)
        if ok:
            applied += 1
            post = _get_volume(node)
            jlog("INFO", "applied", mount=mount, node=node,
                 gain=round(target, 3), prev=cur, result=post)
            # surface a cap: PipeWire/wpctl refusing >1.0 would show here
            if post is not None and abs(post - target) > 0.02:
                jlog("WARNING", "volume_capped", mount=mount, node=node,
                     target=round(target, 3), result=post)
        else:
            jlog("ERROR", "apply_failed", mount=mount, node=node,
                 gain=round(target, 3), err=msg)
    return applied, unresolved


def _handle_stop(signum, _frame):
    jlog("INFO", "signal", signal=signum)
    _stop.set()


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    jlog("INFO", "start", gains_path=str(GAINS_PATH), mounts=list(MOUNTS),
         poll_sec=POLL_SEC, watch_sec=WATCH_SEC)

    last_mtime = None
    last_poll = 0.0
    while not _stop.is_set():
        now = time.monotonic()
        try:
            mtime = GAINS_PATH.stat().st_mtime
        except FileNotFoundError:
            jlog("ERROR", "gains_missing", path=str(GAINS_PATH))
            _stop.wait(WATCH_SEC)
            continue
        except Exception as exc:  # noqa: BLE001
            jlog("ERROR", "gains_stat_failed", path=str(GAINS_PATH), err=str(exc))
            _stop.wait(WATCH_SEC)
            continue

        changed = mtime != last_mtime
        due = (now - last_poll) >= POLL_SEC
        if changed or due:
            try:
                gains = read_gains()
            except Exception as exc:  # noqa: BLE001
                jlog("ERROR", "gains_unreadable", path=str(GAINS_PATH), err=str(exc))
                _stop.wait(WATCH_SEC)
                continue
            if not gains:
                jlog("ERROR", "gains_empty_or_invalid", path=str(GAINS_PATH))
            else:
                applied, unresolved = reconcile(gains, force=changed)
                if len(unresolved) == len(MOUNTS):
                    jlog("ERROR", "no_sink_inputs_resolved", mounts=list(MOUNTS),
                         hint="pipewire/env down or all VLC services stopped")
                elif unresolved:
                    jlog("WARNING", "sink_inputs_unresolved", mounts=unresolved,
                         hint="VLC mid-restart? will retry next tick")
                if changed:
                    jlog("INFO", "gains_reloaded", gains=gains, applied=applied)
            last_mtime = mtime
            last_poll = now
        _stop.wait(WATCH_SEC)

    jlog("INFO", "stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
