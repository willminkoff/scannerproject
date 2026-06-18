"""Per-band BT-speaker mute control + state persistence (2026-06-05).

Independent of the per-band VLC / op25 services — the audio chain keeps
running; we only mute the *sink-input* that feeds the bluez (BT speaker)
sink.

Implementation per band:

* ``airband``, ``ground``
  ``scanner-vlc-analog.service`` / ``scanner-vlc-ground.service`` each
  attach to PipeWire as a sink-input.  We toggle the sink-input's mute
  flag via ``wpctl set-mute`` (falling back to ``pactl
  set-sink-input-mute``).  PipeWire mute state is ephemeral — it lives
  with the sink-input lifetime — so we persist *intent* to
  ``data/band_mute.json`` and a background watcher thread re-applies
  the intent every :data:`BAND_MUTE_WATCHER_INTERVAL_SEC` seconds.  A
  sink-input that gets re-created (service restart, reboot) is
  re-muted within one watcher tick.

* ``digital``
  Not a VLC process; digital audio is emitted directly via ``pw-cat``
  from the op25 audio bridge.  The bridge polls a touchfile —
  :data:`DIGITAL_MUTE_FLAG`-equivalent (see
  ``OP25_AUDIO_MUTE_FLAG`` in ``ui/vlc.py``) — and when the file
  exists it does not write to its local PipeWire sink.  We touch /
  remove that file here.

Public API
----------
``BAND_KEYS``
    Iterable of band names this module understands.

``get_state() -> dict[str, bool]``
    Returns the persisted mute intent for all bands.

``set_band(band, muted) -> (ok, err)``
    Persists + applies the requested state.  Persist happens FIRST so a
    transient hardware error doesn't lose the operator's intent (the
    watcher will pick it up on the next tick).

``start_watcher() -> None``
    Spawns the daemon reconciler.  Idempotent — safe to call from app
    startup unconditionally.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_STATE_PATH = (_REPO_ROOT / "data" / "band_mute.json").resolve()
STATE_PATH = Path(os.getenv("BAND_MUTE_STATE_PATH", str(_DEFAULT_STATE_PATH)))

# Band → systemd unit (PipeWire sink-input owner).
#   airband -> rtl-airband-airband produces ANALOG.mp3 → scanner-vlc-analog
#   ground  -> rtl-airband-ground  produces ANALOG_GROUND.mp3 → scanner-vlc-ground
#   vfo     -> scripts/vfo.py      produces VFO.mp3            → scanner-vlc-vfo
# (Digital is the touchfile path — handled separately.)
_BAND_UNITS = {
    "airband": os.getenv("BAND_MUTE_UNIT_AIRBAND", "scanner-vlc-analog.service"),
    "ground":  os.getenv("BAND_MUTE_UNIT_GROUND",  "scanner-vlc-ground.service"),
    "vfo":     os.getenv("BAND_MUTE_UNIT_VFO",     "scanner-vlc-vfo.service"),
}

# Match ui/vlc.py:DIGITAL_MUTE_FLAG so both paths agree on the contract.
_DIGITAL_MUTE_FLAG = Path(
    os.getenv("OP25_AUDIO_MUTE_FLAG", "/run/scannerproject/op25/digital_local_mute")
)

# 2026-06-17: digital ALSO has a VLC consumer (scanner-vlc-digital) playing
# DIGITAL.mp3 to the BT sink.  In practice that VLC sink-input is the audio
# actually heard on BOOM — the op25 audio-bridge direct pw-cat path is often
# inactive/already-muted (its touchfile present).  The old digital mute only
# touched the op25 flag, so it never silenced the VLC the operator was hearing.
# Mute BOTH paths: the op25 touchfile AND the scanner-vlc-digital sink-input.
_DIGITAL_VLC_UNIT = os.getenv("BAND_MUTE_UNIT_DIGITAL", "scanner-vlc-digital.service")

BAND_KEYS: tuple[str, ...] = ("airband", "ground", "digital", "vfo")
BAND_MUTE_WATCHER_INTERVAL_SEC = float(
    os.getenv("BAND_MUTE_WATCHER_INTERVAL_SEC", "5.0")
)

_state_lock = threading.Lock()
_watcher_thread: Optional[threading.Thread] = None
_watcher_started = False
_watcher_started_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Subprocess environment — required when running under systemd (no user
# session env inherited).  wpctl talks to PipeWire via
# $XDG_RUNTIME_DIR/pipewire-0; without it set, every wpctl call dies with
# "Could not connect to PipeWire" and silently returns empty output.  The
# same uid-1000 runtime dir is used by ui/vlc.py:_vlc_launch_env() — we
# keep the contracts aligned so the audio chain is reachable from either
# module under the same systemd unit.
# ---------------------------------------------------------------------------

def _audio_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    try:
        uid_text = os.getenv("BAND_MUTE_AUDIO_UID")
        uid = int(uid_text) if uid_text else os.getuid()
    except (ValueError, TypeError):
        uid = os.getuid()
    runtime_dir = os.getenv("BAND_MUTE_XDG_RUNTIME_DIR", f"/run/user/{uid}")
    if runtime_dir and os.path.isdir(runtime_dir):
        env["XDG_RUNTIME_DIR"] = runtime_dir
        # Both wpctl AND pactl benefit from PULSE_SERVER pointing at the
        # pipewire-pulse shim socket; PipeWire itself just needs the
        # runtime dir to find pipewire-0.
        pulse_native = os.path.join(runtime_dir, "pulse", "native")
        if os.path.exists(pulse_native):
            env.setdefault("PULSE_SERVER", f"unix:{pulse_native}")
    return env


# ---------------------------------------------------------------------------
# State file IO
# ---------------------------------------------------------------------------

def _default_state() -> dict[str, bool]:
    return {k: False for k in BAND_KEYS}


def _read_state_file() -> dict[str, bool]:
    try:
        with STATE_PATH.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        return _default_state()
    except Exception:
        logger.debug(
            "band_mute: state read failed for %s, defaulting to unmuted",
            STATE_PATH,
            exc_info=True,
        )
        return _default_state()
    out = _default_state()
    if isinstance(payload, dict):
        for k in BAND_KEYS:
            out[k] = bool(payload.get(k, False))
    return out


def _write_state_file(state: dict[str, bool]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
        body = {k: bool(state.get(k, False)) for k in BAND_KEYS}
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(body, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_PATH)
    except Exception:
        logger.exception("band_mute: failed writing state to %s", STATE_PATH)


def get_state() -> dict[str, bool]:
    with _state_lock:
        return _read_state_file()


# ---------------------------------------------------------------------------
# PipeWire / wpctl helpers — airband + ground
# ---------------------------------------------------------------------------

def _service_main_pid(unit: str) -> Optional[int]:
    """Return MainPID for a systemd unit (system OR user), or None if
    not active.  Tries system-level first since the scanner-vlc-* units
    are installed as system services on micro."""
    for extra_flags in ([], ["--user"]):
        try:
            res = subprocess.run(
                ["systemctl", *extra_flags,
                 "show", "-p", "MainPID", "--value", unit],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=2.0, check=False,
            )
        except Exception:  # noqa: BLE001
            continue
        pid_text = (res.stdout or "").strip()
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid > 1:
            return pid
    return None


_PACTL_SINK_INPUT_RE = re.compile(r"^Sink Input #(\d+)\b")
_WPCTL_NODE_LINE_RE = re.compile(r"^[\s│├└─*]*(\d+)\.\s+")
_INSPECT_PID_RE = re.compile(r'application\.process\.id\s*=\s*"(\d+)"')
_INSPECT_MUTE_RE = re.compile(r'mute\s*=\s*(true|false)', re.IGNORECASE)
_GET_VOLUME_MUTED_RE = re.compile(r"\[MUTED\]")


def _descendant_pids(root: int) -> set[int]:
    """Best-effort descendant PID enumeration via /proc.  VLC may spawn
    audio worker processes whose pulse application.process.id differs
    from the systemd MainPID; we accept the whole subtree."""
    out: set[int] = set()
    proc = Path("/proc")
    try:
        children: dict[int, list[int]] = {}
        for entry in proc.iterdir():
            name = entry.name
            if not name.isdigit():
                continue
            try:
                stat = (entry / "status").read_text(encoding="utf-8", errors="ignore")
            except Exception:  # noqa: BLE001
                continue
            ppid = 0
            for line in stat.splitlines():
                if line.startswith("PPid:"):
                    try:
                        ppid = int(line.split()[1])
                    except (IndexError, ValueError):
                        ppid = 0
                    break
            if ppid:
                children.setdefault(ppid, []).append(int(name))
        stack = [root]
        while stack:
            cur = stack.pop()
            for child in children.get(cur, []):
                if child not in out:
                    out.add(child)
                    stack.append(child)
    except Exception:  # noqa: BLE001
        return out
    return out


def _pactl_sink_inputs_text() -> str:
    """Legacy pulse-shim sink-input listing.  Kept for backward compat
    with hosts that still have ``pactl`` installed; on PipeWire-only
    hosts (e.g. micro post-2026-06-05) this returns empty and we fall
    through to the native wpctl path below."""
    if not shutil.which("pactl"):
        return ""
    try:
        res = subprocess.run(
            ["pactl", "list", "sink-inputs"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=3.0, check=False,
            env=_audio_subprocess_env(),
        )
    except Exception:  # noqa: BLE001
        return ""
    return res.stdout or ""


def _wpctl_status_text() -> str:
    if not shutil.which("wpctl"):
        return ""
    try:
        res = subprocess.run(
            ["wpctl", "status"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=3.0, check=False,
            env=_audio_subprocess_env(),
        )
    except Exception:  # noqa: BLE001
        return ""
    return res.stdout or ""


def _wpctl_inspect_text(node_id: str) -> str:
    if not shutil.which("wpctl"):
        return ""
    try:
        res = subprocess.run(
            ["wpctl", "inspect", str(node_id)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=2.0, check=False,
            env=_audio_subprocess_env(),
        )
    except Exception:  # noqa: BLE001
        return ""
    return res.stdout or ""


def _enumerate_wpctl_audio_stream_ids() -> list[str]:
    """Walk ``wpctl status`` and return the IDs of every node listed
    under ``Audio -> Streams:``.  These are the PipeWire output streams
    that ``wpctl set-mute`` operates on."""
    text = _wpctl_status_text()
    if not text:
        return []
    out: list[str] = []
    in_audio = False
    in_streams = False
    indent_floor: Optional[int] = None
    for raw in text.splitlines():
        # Top-level section banners are flush-left.
        if raw.startswith("Audio"):
            in_audio = True
            in_streams = False
            indent_floor = None
            continue
        if raw.startswith("Video") or raw.startswith("Settings"):
            in_audio = False
            in_streams = False
            indent_floor = None
            continue
        if not in_audio:
            continue
        if "Streams:" in raw:
            in_streams = True
            indent_floor = None
            continue
        if not in_streams:
            continue
        # Stop scanning Streams at the next top-level audio sub-section
        # (Sinks:, Sources:, Endpoints, ...) — those don't carry "Streams:"
        # in their header so we detect them by an explicit ":" suffix.
        if raw.strip().endswith(":") and "Streams" not in raw:
            in_streams = False
            indent_floor = None
            continue
        m = _WPCTL_NODE_LINE_RE.match(raw)
        if not m:
            continue
        # Pick the OUTER-MOST stream depth — VLC nests its port nodes
        # (output_FL / output_FR) under the stream node and we don't
        # want to mute individual ports.
        indent = len(raw) - len(raw.lstrip(" │"))
        if indent_floor is None:
            indent_floor = indent
        if indent > indent_floor:
            continue
        out.append(m.group(1))
    return out


def _stream_node_pid(node_id: str) -> Optional[int]:
    text = _wpctl_inspect_text(node_id)
    if not text:
        return None
    m = _INSPECT_PID_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _wpctl_find_stream_for_pid(pid: int) -> Optional[str]:
    if not pid or pid <= 1:
        return None
    acceptable_pids = {pid} | _descendant_pids(pid)
    for node_id in _enumerate_wpctl_audio_stream_ids():
        node_pid = _stream_node_pid(node_id)
        if node_pid is not None and node_pid in acceptable_pids:
            return node_id
    return None


def _sink_input_id_for_pid(pid: int) -> Optional[str]:
    """Find the audio output node for a process PID (or any descendant).

    Tries the legacy pulse-shim ``pactl list sink-inputs`` first for
    backward compat with hosts that still have pulseaudio's CLI tools;
    falls back to the PipeWire-native ``wpctl status`` + ``wpctl
    inspect`` discovery path, which is the only thing available on a
    pipewire-only host (micro)."""
    if not pid or pid <= 1:
        return None
    # Pulse-shim path (legacy).
    text = _pactl_sink_inputs_text()
    if text:
        acceptable_pids = {str(pid)} | {str(p) for p in _descendant_pids(pid)}
        current_id: Optional[str] = None
        for line in text.splitlines():
            m = _PACTL_SINK_INPUT_RE.match(line)
            if m:
                current_id = m.group(1)
                continue
            if current_id is None:
                continue
            stripped = line.strip()
            if stripped.startswith("application.process.id"):
                val = stripped.split("=", 1)[-1].strip().strip('"')
                if val in acceptable_pids:
                    return current_id
    # PipeWire-native path.
    return _wpctl_find_stream_for_pid(pid)


def _set_sink_input_mute(sink_input_id: str, muted: bool) -> tuple[bool, str]:
    """Mute or unmute a specific sink-input by ID.

    Prefers ``wpctl set-mute`` (native PipeWire control); falls back to
    ``pactl set-sink-input-mute`` (works through the pulse shim).
    """
    arg = "1" if muted else "0"
    env = _audio_subprocess_env()
    if shutil.which("wpctl"):
        try:
            res = subprocess.run(
                ["wpctl", "set-mute", str(sink_input_id), arg],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=2.0, check=False,
                env=env,
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"wpctl exec: {exc}"
        if res.returncode == 0:
            return True, ""
        # fall through to pactl as a second attempt
    if shutil.which("pactl"):
        try:
            res = subprocess.run(
                ["pactl", "set-sink-input-mute", str(sink_input_id), arg],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=2.0, check=False,
                env=env,
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"pactl exec: {exc}"
        return (res.returncode == 0, (res.stdout or "").strip()[:200])
    return False, "neither wpctl nor pactl installed"


def _current_sink_input_mute(sink_input_id: str) -> Optional[bool]:
    """Read the current Mute state of an audio stream.

    Tries pulse-shim pactl first (when present), then falls back to
    ``wpctl get-volume`` which prints ``[MUTED]`` after the volume when
    the node is muted.  Returns None when the state can't be read."""
    # Pulse-shim path.
    text = _pactl_sink_inputs_text()
    if text:
        in_target = False
        for line in text.splitlines():
            m = _PACTL_SINK_INPUT_RE.match(line)
            if m:
                in_target = (m.group(1) == str(sink_input_id))
                continue
            if in_target and line.strip().startswith("Mute:"):
                val = line.split(":", 1)[1].strip().lower()
                return val.startswith("yes")
    # PipeWire-native path.
    if shutil.which("wpctl"):
        try:
            res = subprocess.run(
                ["wpctl", "get-volume", str(sink_input_id)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=2.0, check=False,
                env=_audio_subprocess_env(),
            )
        except Exception:  # noqa: BLE001
            return None
        if res.returncode == 0 and res.stdout:
            return bool(_GET_VOLUME_MUTED_RE.search(res.stdout))
    return None


def _apply_vlc_mute(unit: str, muted: bool) -> tuple[bool, str]:
    """Apply mute to a per-band VLC sink-input.

    Returns ``(ok, message)``.  When the upstream service is up but no
    audio stream node currently exists (e.g. squelch is closed, VLC is
    connected to icecast but not emitting frames), we return ok=True
    with a message saying the watcher will apply mute when the stream
    appears.  Intent is already persisted before this call returns from
    set_band() so the watcher's 5s reconcile loop will catch it."""
    pid = _service_main_pid(unit)
    if pid is None:
        return False, f"{unit} not active"
    sink_id = _sink_input_id_for_pid(pid)
    if sink_id is None:
        # No live audio stream — common when the band is squelched.  Not
        # an error; the watcher will apply mute as soon as a stream node
        # appears for this pid.
        return True, (
            f"{unit} pid={pid} has no live audio stream "
            f"(squelched?); mute will apply when audio resumes"
        )
    return _set_sink_input_mute(sink_id, muted)


# ---------------------------------------------------------------------------
# Digital — touchfile mute flag
# ---------------------------------------------------------------------------

def _apply_digital_mute(muted: bool) -> tuple[bool, str]:
    """Mute digital on BOTH consumer paths so it works regardless of config:

    1. op25 audio-bridge direct pw-cat — gated by the touchfile flag.
    2. scanner-vlc-digital VLC sink-input — the consumer actually heard on
       BOOM in the current deployment.

    Returns ok if at least one path applied cleanly (the inactive path
    reports a benign message rather than failing the toggle)."""
    msgs: list[str] = []
    # Path 1: op25 touchfile.
    flag_ok = True
    try:
        _DIGITAL_MUTE_FLAG.parent.mkdir(parents=True, exist_ok=True)
        if muted:
            _DIGITAL_MUTE_FLAG.touch(exist_ok=True)
        else:
            try:
                _DIGITAL_MUTE_FLAG.unlink()
            except FileNotFoundError:
                pass
    except Exception as exc:  # noqa: BLE001
        flag_ok = False
        msgs.append(f"touchfile error: {exc}")
    # Path 2: scanner-vlc-digital VLC sink-input (reuse the VLC band logic).
    vlc_ok, vlc_msg = _apply_vlc_mute(_DIGITAL_VLC_UNIT, muted)
    if vlc_msg:
        msgs.append(vlc_msg)
    return (flag_ok or vlc_ok), "; ".join(m for m in msgs if m)


def _digital_is_muted() -> bool:
    try:
        return _DIGITAL_MUTE_FLAG.exists()
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _apply_band(band: str, muted: bool) -> tuple[bool, str]:
    if band in ("airband", "ground", "vfo"):
        unit = _BAND_UNITS[band]
        return _apply_vlc_mute(unit, muted)
    if band == "digital":
        return _apply_digital_mute(muted)
    return False, f"unknown band {band!r}"


def set_band(band: str, muted: bool) -> tuple[bool, str]:
    """Persist + apply mute intent for a band.  Persist happens FIRST so
    intent survives a transient hardware error (the watcher will pick it
    up).  Returns (ok, err) reflecting the apply result; even on err
    the persisted state IS updated."""
    band = str(band or "").strip().lower()
    if band not in BAND_KEYS:
        return False, f"unknown band {band!r}, must be one of {BAND_KEYS}"
    muted = bool(muted)
    with _state_lock:
        state = _read_state_file()
        state[band] = muted
        _write_state_file(state)
    ok, msg = _apply_band(band, muted)
    if not ok:
        logger.warning(
            "band_mute: %s set %s failed: %s (persisted; watcher will retry)",
            band, muted, msg,
        )
    return ok, msg


def reconcile_once() -> None:
    """Re-apply persisted intent to the system.  Called from the watcher
    tick AND at startup.  Cheap, idempotent — only fires the mute /
    unmute command when current divergence is observed (so we don't spam
    wpctl)."""
    state = get_state()
    for band, want in state.items():
        try:
            if band in ("airband", "ground", "vfo"):
                unit = _BAND_UNITS[band]
                pid = _service_main_pid(unit)
                if pid is None:
                    continue
                sink_id = _sink_input_id_for_pid(pid)
                if sink_id is None:
                    continue
                current = _current_sink_input_mute(sink_id)
                if current is None or current != want:
                    ok, msg = _set_sink_input_mute(sink_id, want)
                    if not ok:
                        logger.debug(
                            "band_mute reconcile: %s -> %s failed: %s",
                            band, want, msg,
                        )
            elif band == "digital":
                # Diverged if EITHER path is off: the op25 touchfile, or the
                # scanner-vlc-digital sink-input (re-created on VLC restart).
                diverged = (_digital_is_muted() != want)
                if not diverged:
                    pid = _service_main_pid(_DIGITAL_VLC_UNIT)
                    if pid is not None:
                        sink_id = _sink_input_id_for_pid(pid)
                        if sink_id is not None:
                            cur = _current_sink_input_mute(sink_id)
                            if cur is not None and cur != want:
                                diverged = True
                if diverged:
                    ok, msg = _apply_digital_mute(want)
                    if not ok:
                        logger.debug(
                            "band_mute reconcile: digital -> %s failed: %s",
                            want, msg,
                        )
        except Exception:  # noqa: BLE001
            logger.debug("band_mute reconcile loop body failed", exc_info=True)


def _watcher_loop() -> None:
    logger.info(
        "band_mute watcher running every %.1fs",
        BAND_MUTE_WATCHER_INTERVAL_SEC,
    )
    while True:
        try:
            reconcile_once()
        except Exception:  # noqa: BLE001
            logger.debug("band_mute watcher tick failed", exc_info=True)
        time.sleep(BAND_MUTE_WATCHER_INTERVAL_SEC)


def start_watcher() -> None:
    """Idempotent — safe to call unconditionally from app startup."""
    global _watcher_thread, _watcher_started
    with _watcher_started_lock:
        if _watcher_started:
            return
        _watcher_started = True
    try:
        reconcile_once()
    except Exception:  # noqa: BLE001
        logger.debug("band_mute initial reconcile failed", exc_info=True)
    t = threading.Thread(
        target=_watcher_loop,
        name="band-mute-watcher",
        daemon=True,
    )
    t.start()
    _watcher_thread = t
