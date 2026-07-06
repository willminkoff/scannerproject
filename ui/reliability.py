"""SB5 reliability — single-call snapshot of every system that can wedge.

Reads (deliberately bounded — no probe blocks > 3s):
  - systemd state per service (active / failed / NRestarts / uptime)
  - chirp daemon responsiveness (get_status latency + channel count)
  - icecast mount publishing rate (bytes_sent delta over a short window)
  - OP25 log freshness (log mtime → wedge if > 60s with service active)
  - VFO daemon dongle health + last_frame_age_ms
  - BT speaker pairing / connection / transport state
  - Expected USB serials enumerated

Each check returns a verdict from {"ok", "warn", "wedged"}. The overall
verdict is the worst per-check verdict.  Designed so a SITREP panel can
turn each tile red/yellow/green from a single endpoint response.
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("reliability")

# Services that should be active for normal operation.  Values: optional
# description for the UI tile.
TRACKED_SERVICES = {
    "gr-demod@airband.service": "Chirp Airband",
    "gr-demod@ground.service": "Chirp Ground",
    "sdrplay.service": "SDRplay API",
    "scanner-digital-op25.service": "OP25 (Digital)",
    "scanner-vfo.service": "VFO",
    "scanner-waterfall.service": "Waterfall",
    "airband-ui.service": "Airband UI",
    "scanner-vlc-analog.service": "VLC Analog",
    "scanner-vlc-ground.service": "VLC Ground",
    "scanner-vlc-digital.service": "VLC Digital",
    "scanner-vlc-vfo.service": "VLC VFO",
    "icecast2.service": "Icecast",
    "bluetooth.service": "Bluetooth",
}

# Expected RTL-SDR + RSPduo serials.  Drift here = a dongle dropped off
# the bus.  Edit when the hardware allocation changes.
EXPECTED_DONGLE_SERIALS = {
    # RSPduos (analog + digital)
    "1809063632": "RSPduo Analog (airband+ground)",
    "180903EF32": "RSPduo Digital (op25)",
    # RTL-SDRs by role
    "45469635": "NESDR — disco sweep",
    "70613472": "NESDR — waterfall A",
    "83241970": "RTL-SDR Blog V4 — VFO",
    "61108285": "NESDR — waterfall B",
    # Optional / disabled — present in tree but expected absent at runtime
    # "80000003": "Orange Realtek (unused)",
    # "VDL2A001": "VDL2 dongle (off)",
}

ICECAST_PROBE_INTERVAL_SEC = 0.6
PROC_TIMEOUT_SEC = 3.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd, timeout=PROC_TIMEOUT_SEC):
    """Run a subprocess with timeout; return (rc, stdout, stderr)."""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError as exc:
        return -1, "", str(exc)


def _journal_mtime(unit: str) -> Optional[float]:
    """Last log entry epoch time for a unit; None if unknown."""
    rc, out, _ = _run(
        ["journalctl", "-u", unit, "-n", "1", "--no-pager", "-o", "short-unix"],
        timeout=2.0,
    )
    if rc != 0 or not out.strip():
        return None
    m = re.match(r"^(\d+\.\d+) ", out.strip().splitlines()[0])
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _systemd_show(unit: str) -> dict:
    """Return systemd show fields we care about for ``unit``."""
    rc, out, _ = _run(
        [
            "systemctl", "show", unit,
            "-p", "ActiveState",
            "-p", "SubState",
            "-p", "NRestarts",
            "-p", "ActiveEnterTimestamp",
            "-p", "MainPID",
            "-p", "Result",
        ],
        timeout=2.0,
    )
    fields = {}
    if rc != 0:
        return {"active_state": "unknown", "sub_state": "unknown",
                "n_restarts": 0, "uptime_sec": None, "main_pid": 0}
    for line in out.strip().splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        fields[k] = v
    enter = fields.get("ActiveEnterTimestamp", "")
    uptime = None
    if enter and enter != "n/a" and enter != "0":
        try:
            # Parse format like "Tue 2026-06-10 12:30:23 EDT"
            import datetime
            for fmt in ("%a %Y-%m-%d %H:%M:%S %Z", "%a %Y-%m-%d %H:%M:%S"):
                try:
                    dt = datetime.datetime.strptime(enter, fmt)
                    uptime = max(0, time.time() - dt.timestamp())
                    break
                except ValueError:
                    continue
        except Exception:
            uptime = None
    return {
        "active_state": fields.get("ActiveState", "unknown"),
        "sub_state": fields.get("SubState", "unknown"),
        "n_restarts": int(fields.get("NRestarts", "0") or 0),
        "uptime_sec": uptime,
        "main_pid": int(fields.get("MainPID", "0") or 0),
        "result": fields.get("Result", ""),
    }


def _icecast_snapshot() -> dict:
    """Pull /status-json.xsl and turn it into a per-mount dict."""
    import urllib.request
    snapshot: dict[str, dict] = {}
    try:
        with urllib.request.urlopen("http://localhost:8000/status-json.xsl", timeout=2) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        return {"_error": str(exc)}
    src = data.get("icestats", {}).get("source", [])
    if isinstance(src, dict):
        src = [src]
    for s in src or []:
        url = s.get("listenurl") or ""
        mount = url.rsplit("/", 1)[-1] if url else ""
        if not mount or mount.startswith("keepalive-"):
            continue
        # Bitrate isn't a top-level field on icecast2's JSON; parse the
        # "bitrate=32;samplerate=...;channels=..." audio_info string.
        bitrate_kbps = None
        info = s.get("audio_info") or ""
        for part in info.split(";"):
            if part.startswith("bitrate="):
                try:
                    bitrate_kbps = int(part.split("=", 1)[1])
                except ValueError:
                    pass
        # Server type is also a reliable "source is publishing" signal —
        # only set when a source has actually connected to the mount.
        snapshot[mount] = {
            "listeners": int(s.get("listeners", 0) or 0),
            "stream_start": s.get("stream_start_iso8601"),
            "bytes_sent": int(s.get("total_bytes_sent", 0) or 0),
            "bitrate_kbps": bitrate_kbps,
            "server_type": s.get("server_type"),
        }
    return snapshot


# ---------------------------------------------------------------------------
# Per-area checks
# ---------------------------------------------------------------------------

def check_services() -> dict:
    """systemd state for the tracked unit list."""
    out = {}
    for unit, label in TRACKED_SERVICES.items():
        fields = _systemd_show(unit)
        active = fields["active_state"]
        nr = fields["n_restarts"]
        verdict = "ok" if active == "active" and nr == 0 else (
            "warn" if active == "active" and nr > 0 else "wedged"
        )
        out[unit] = {
            "label": label,
            "active_state": active,
            "sub_state": fields["sub_state"],
            "n_restarts": nr,
            "uptime_sec": fields["uptime_sec"],
            "verdict": verdict,
        }
    return out


def check_chirp() -> dict:
    """Chirp daemon responsiveness + channel inventory."""
    out = {}
    try:
        from .chirp_client import get_airband_client, get_ground_client
    except ImportError:
        from ui.chirp_client import get_airband_client, get_ground_client
    for name, getter in (("airband", get_airband_client), ("ground", get_ground_client)):
        t0 = time.monotonic()
        try:
            s = getter().get_status()
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            chs = s.get("channels", []) or []
            sig_levels = [float(c.get("signal_level_dbfs") or -180.0) for c in chs]
            all_dead = all(x <= -150 for x in sig_levels) if sig_levels else False
            verdict = "wedged" if all_dead else ("warn" if elapsed_ms > 2000 else "ok")
            out[name] = {
                "responsive_ms": elapsed_ms,
                "channels": len(chs),
                "all_dead_silence": all_dead,
                "median_signal_dbfs": (
                    sorted(sig_levels)[len(sig_levels) // 2] if sig_levels else None
                ),
                "verdict": verdict,
            }
        except Exception as exc:
            out[name] = {
                "responsive_ms": None,
                "channels": 0,
                "error": str(exc)[:120],
                "verdict": "wedged",
            }
    return out


def check_icecast() -> dict:
    """Icecast mount presence + source health.

    Verdict logic: mount present with a bitrate field = source connected.
    Bytes-sent delta-over-time isn't reliable on low-bitrate streams with
    no listeners; counter only ticks on serve events.  Mount-present + a
    valid bitrate is the canonical "is this source publishing" signal.
    """
    snap = _icecast_snapshot()
    if "_error" in snap:
        return {"_error": snap.get("_error"), "verdict": "wedged"}
    out: dict[str, dict] = {}
    for mount, info in snap.items():
        listeners = info.get("listeners", 0)
        bytes_sent = info.get("bytes_sent", 0)
        bitrate = info.get("bitrate_kbps")
        server_type = info.get("server_type") or ""
        # A source is publishing if icecast knows its server_type (only set
        # on actual source connection) OR we parsed a bitrate from audio_info.
        publishing = bool(server_type) or (bitrate is not None)
        verdict = "ok" if publishing else "wedged"
        out[mount] = {
            "listeners": listeners,
            "bytes_sent_total": bytes_sent,
            "bitrate_kbps": bitrate,
            "server_type": server_type,
            "stream_start": info.get("stream_start"),
            "verdict": verdict,
        }
    return out


def check_op25() -> dict:
    """OP25 health: log freshness + multi_rx.py CPU sanity check.

    OP25 is event-driven; long quiet stretches are normal.  Use a generous
    log-age threshold (5 min warn, 10 min wedged) but ALSO probe the
    multi_rx.py process — if it's running but not pegging CPU, that's the
    classic "active but flowgraph never started producing" wedge.
    """
    log_path = "/var/log/op25/op25.log"
    try:
        mtime = os.path.getmtime(log_path)
        age_sec = time.time() - mtime
    except OSError as exc:
        return {"error": str(exc), "verdict": "wedged"}
    svc = _systemd_show("scanner-digital-op25.service")
    if svc["active_state"] != "active":
        return {"log_age_sec": age_sec, "service": svc, "verdict": "wedged"}
    cpu_busy = None
    rc, child_out, _ = _run(["pgrep", "-f", "multi_rx.py"], timeout=1.0)
    if child_out.strip():
        child_pid = child_out.strip().splitlines()[0]
        rc, child_ps, _ = _run(["ps", "-o", "pcpu=", "-p", child_pid], timeout=1.0)
        try:
            cpu_busy = float(child_ps.strip())
        except (ValueError, AttributeError):
            cpu_busy = None
    if age_sec < 300:
        verdict = "ok"
    elif age_sec < 600:
        verdict = "warn"
    else:
        verdict = "wedged"
    if cpu_busy is not None and cpu_busy < 5.0 and age_sec > 60:
        verdict = "wedged"
    return {
        "log_age_sec": age_sec,
        "multi_rx_cpu": cpu_busy,
        "service": svc,
        "verdict": verdict,
    }


def check_vfo() -> dict:
    """VFO daemon dongle health + frame age."""
    path = os.path.join(
        os.getenv("VFO_STATE_DIR", "/run/scannerproject/vfo"), "state.json"
    )
    try:
        with open(path) as f:
            state = json.load(f)
    except Exception as exc:
        return {"error": str(exc), "verdict": "wedged"}
    dongle = state.get("dongle") or {}
    last_frame_age_ms = state.get("last_frame_age_ms") or dongle.get("last_frame_age_ms")
    state_str = state.get("state") or dongle.get("state")
    verdict = "ok"
    if state_str != "ok":
        verdict = "wedged"
    elif last_frame_age_ms is not None and last_frame_age_ms > 1000:
        verdict = "warn"
    return {
        "state": state_str,
        "dongle_serial": (dongle or {}).get("serial"),
        "last_frame_age_ms": last_frame_age_ms,
        "freq_mhz": state.get("freq_mhz"),
        "mod": state.get("mod"),
        "verdict": verdict,
    }


def check_dongles() -> dict:
    """Walk /sys/bus/usb/devices for attached SDR hardware.

    rtl_test only sees free dongles; a claimed dongle looks "missing".
    /sys enumeration is claim-state agnostic and gives the truth.
    """
    rtl_serials: set[str] = set()
    rspduo_count = 0
    try:
        base = "/sys/bus/usb/devices"
        for entry in os.listdir(base):
            dev_path = os.path.join(base, entry)
            try:
                with open(os.path.join(dev_path, "idVendor")) as f:
                    vendor = f.read().strip()
                with open(os.path.join(dev_path, "idProduct")) as f:
                    product = f.read().strip()
            except OSError:
                continue
            if vendor == "0bda" and product in ("2832", "2838"):
                try:
                    with open(os.path.join(dev_path, "serial")) as f:
                        sn = f.read().strip()
                    if sn:
                        rtl_serials.add(sn)
                except OSError:
                    pass
            elif vendor == "1df7" and product == "3020":
                rspduo_count += 1
    except Exception:
        log.exception("dongle enum walk failed")
    expected_rtl = sorted(s for s in EXPECTED_DONGLE_SERIALS.keys() if not s.startswith("180"))
    missing_rtl = sorted(s for s in expected_rtl if s not in rtl_serials)
    extra_rtl = sorted(s for s in rtl_serials if s not in EXPECTED_DONGLE_SERIALS)
    verdict = "ok"
    if missing_rtl or rspduo_count < 2:
        verdict = "wedged" if rspduo_count < 2 else "warn"
    return {
        "expected_rtl": expected_rtl,
        "enumerated_rtl": sorted(rtl_serials),
        "missing_rtl": missing_rtl,
        "extra_rtl": extra_rtl,
        "rspduo_enumerated": rspduo_count,
        "rspduo_expected": 2,
        "verdict": verdict,
    }


# 2026-06-11 — per-VLC stall detector.  Cached across snapshot() calls so
# we can compute rchar deltas without an in-snapshot sleep.  Keyed by
# systemd unit name.  Reset whenever we miss a sample (PID change, error).
_VLC_SAMPLE_CACHE: dict[str, tuple[float, int]] = {}

# Each VLC -> the icecast mount it pulls from.  Used to gate "stall" on
# "is the source actually publishing?" so we don't fire on a dead mount.
_VLC_MOUNTS = {
    "scanner-vlc-analog.service": "ANALOG.mp3",
    "scanner-vlc-ground.service": "ANALOG_GROUND.mp3",
    "scanner-vlc-digital.service": "DIGITAL.mp3",
    "scanner-vlc-vfo.service": "VFO.mp3",
}

# Each VLC -> short band tag used by the watchdog recovery dispatch.
_VLC_BANDS = {
    "scanner-vlc-analog.service": "analog",
    "scanner-vlc-ground.service": "ground",
    "scanner-vlc-digital.service": "digital",
    "scanner-vlc-vfo.service": "vfo",
}

# Threshold for "VLC isn't consuming the stream."  Icecast publishes at
# 32 kbps = ~4000 B/s; a stalled VLC reads << 500 B/s.  500 gives us 8x
# margin for clock jitter / silence-frame compression.
_VLC_STALL_BPS = 500.0
# Minimum observation window before we trust a delta.  Shorter than this
# and we just remember the sample for next call.
_VLC_MIN_WINDOW_SEC = 4.0


def _read_vlc_socket_bytes(unit: str) -> tuple[int | None, int | None]:
    """Return (pid, tcp_bytes_received) for the VLC's connection to icecast.

    Why not /proc/<pid>/io rchar?  VLC reads its socket in ways that don't
    always flow through the read() syscalls rchar counts (splice/iovec/
    larger buffered reads), so rchar can grow at 100-200 B/s while the
    TCP socket is receiving 4-10 kB/s -- the false-positive that led to
    spurious watchdog restarts.  bytes_received in `ss -tnpi` is the
    kernel-level counter of "TCP delivered this many bytes to this
    application," which is the real signal.
    """
    pid = _systemd_show(unit).get("main_pid") or 0
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None, None
    if pid <= 0:
        return None, None
    # `ss -tnpi` includes the per-socket info block where bytes_received lives.
    # Filter to lines that mention this pid AND the icecast port (8000).
    rc, out, _ = _run(["ss", "-tnpi"], timeout=2.0)
    if rc != 0 or not out:
        return pid, None
    pid_marker = f"pid={pid}"
    icecast_target = ":8000"
    # ss output: ESTAB line, then an indented info line.  We need both.
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if pid_marker not in line or icecast_target not in line:
            continue
        # The info line is the NEXT non-empty line.
        for j in range(i + 1, min(i + 3, len(lines))):
            info = lines[j]
            m = re.search(r"bytes_received:(\d+)", info)
            if m:
                return pid, int(m.group(1))
        # Found the ESTAB but no info block; treat as unknown.
        return pid, None
    return pid, None


def check_vlc(icecast_snap: dict | None = None) -> dict:
    """Per-VLC stall detector.

    A VLC service can be `active` in systemd but in a wedged state where
    it doesn't consume the icecast stream (post-restart pattern we've
    seen repeatedly).  The audio chain goes silent on BOOM but nothing
    on the surface looks broken.  Detect by comparing rchar across two
    snapshot() calls; if the consume rate is below _VLC_STALL_BPS while
    icecast is actually publishing the source mount, mark wedged.

    The snapshot caller passes in the just-computed icecast snapshot
    (saves a duplicate HTTP fetch); if None, we'll fetch fresh.
    """
    if icecast_snap is None:
        icecast_snap = check_icecast()
    now = time.time()
    out: dict[str, dict] = {}
    for unit, mount in _VLC_MOUNTS.items():
        band = _VLC_BANDS[unit]
        svc = _systemd_show(unit)
        if svc.get("active_state") != "active":
            out[band] = {
                "unit": unit,
                "mount": mount,
                "active_state": svc.get("active_state"),
                "verdict": "wedged",
                "reason": "service_not_active",
            }
            # Clear cached sample so a fresh measurement starts post-recovery.
            _VLC_SAMPLE_CACHE.pop(unit, None)
            continue
        pid, tcp_bytes = _read_vlc_socket_bytes(unit)
        mount_info = icecast_snap.get(mount) if isinstance(icecast_snap, dict) else None
        mount_publishing = bool(mount_info and mount_info.get("verdict") == "ok")
        entry = {
            "unit": unit,
            "mount": mount,
            "pid": pid,
            "tcp_bytes_received": tcp_bytes,
            "mount_publishing": mount_publishing,
        }
        prev = _VLC_SAMPLE_CACHE.get(unit)
        # Remember the current sample for next call.  Reset cache when PID
        # changes (restart) so the post-restart delta is clean.
        if tcp_bytes is not None and pid is not None:
            if prev and prev[2] != pid:
                _VLC_SAMPLE_CACHE.pop(unit, None)
                prev = None
            _VLC_SAMPLE_CACHE[unit] = (now, tcp_bytes, pid)
        if tcp_bytes is None:
            entry["verdict"] = "warn"
            entry["reason"] = "socket_unreadable"
        elif not mount_publishing:
            # Source isn't even publishing; not VLC's fault.  Don't
            # flag wedged here -- the icecast check already did.
            entry["verdict"] = "ok"
            entry["reason"] = "mount_not_publishing"
        elif prev is None:
            entry["verdict"] = "ok"
            entry["reason"] = "no_prior_sample"
        else:
            dt = now - prev[0]
            dbytes = tcp_bytes - prev[1]
            if dt < _VLC_MIN_WINDOW_SEC:
                entry["verdict"] = "ok"
                entry["reason"] = "window_too_short"
            else:
                rate_bps = dbytes / dt if dt > 0 else 0.0
                entry["sample_window_sec"] = round(dt, 1)
                entry["consume_bps"] = round(rate_bps, 1)
                # Threshold: icecast publishes ~4000 B/s @ 32 kbps; a real
                # stall sits at 0-200 B/s on the TCP socket.  500 gives 8x
                # margin for silence-frame compression + clock jitter.
                if rate_bps < _VLC_STALL_BPS:
                    entry["verdict"] = "wedged"
                    entry["reason"] = f"stall_tcp_recv_{rate_bps:.0f}_bps"
                else:
                    entry["verdict"] = "ok"
        out[band] = entry
    return out


def check_bt() -> dict:
    """BT speaker pair + connect + transport state."""
    mac = "C0:28:8D:34:6E:67"
    rc, info_out, _ = _run(["bluetoothctl", "info", mac], timeout=2.0)
    paired = bool(re.search(r"^\s*Paired:\s*yes", info_out, re.MULTILINE))
    connected = bool(re.search(r"^\s*Connected:\s*yes", info_out, re.MULTILINE))
    transport_state = None
    if connected:
        # busctl tree to find the active transport
        rc, tree_out, _ = _run(["busctl", "tree", "org.bluez"], timeout=2.0)
        for line in (tree_out or "").splitlines():
            m = re.search(r"/org/bluez/hci0/dev_C0_28_8D_34_6E_67/sep\d+/fd\d+", line)
            if m:
                fd_path = m.group(0)
                rc2, st_out, _ = _run(
                    ["busctl", "get-property", "org.bluez", fd_path,
                     "org.bluez.MediaTransport1", "State"],
                    timeout=1.0,
                )
                if rc2 == 0:
                    parts = st_out.strip().split()
                    if parts:
                        transport_state = parts[-1].strip('"')
                        if transport_state:
                            break
    verdict = "ok"
    if not paired:
        verdict = "wedged"
    elif not connected:
        verdict = "warn"
    elif connected and transport_state not in ("active", "pending"):
        verdict = "warn"
    return {
        "paired": paired,
        "connected": connected,
        "transport_state": transport_state,
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------

def _worst_verdict(*verdicts: str) -> str:
    order = {"ok": 0, "warn": 1, "wedged": 2}
    return max(verdicts, key=lambda v: order.get(v, 2))


def snapshot() -> dict:
    """One-shot reliability snapshot for /api/reliability/status."""
    t0 = time.monotonic()
    services = check_services()
    chirp = check_chirp()
    icecast = check_icecast()
    vlc = check_vlc(icecast)
    op25 = check_op25()
    vfo = check_vfo()
    dongles = check_dongles()
    bt = check_bt()
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    section_verdicts = [
        _worst_verdict(*[s["verdict"] for s in services.values()]) if services else "wedged",
        _worst_verdict(*[c["verdict"] for c in chirp.values()]) if chirp else "wedged",
        _worst_verdict(*[m["verdict"] for m in icecast.values() if isinstance(m, dict) and "verdict" in m]) if isinstance(icecast, dict) and icecast else "wedged",
        _worst_verdict(*[v["verdict"] for v in vlc.values()]) if vlc else "ok",
        op25.get("verdict", "wedged"),
        vfo.get("verdict", "wedged"),
        dongles.get("verdict", "wedged"),
        bt.get("verdict", "wedged"),
    ]
    return {
        "ok": True,
        "checked_at_ms": int(time.time() * 1000),
        "elapsed_ms": elapsed_ms,
        "verdict": _worst_verdict(*section_verdicts),
        "services": services,
        "chirp": chirp,
        "icecast": icecast,
        "vlc": vlc,
        "op25": op25,
        "vfo": vfo,
        "dongles": dongles,
        "bt": bt,
    }
