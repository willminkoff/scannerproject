"""sb3.sdrtrunk_client — READ-ONLY observer of SDRTrunk state via its log.

SDRTrunk has no REST API (unlike SDRangel), so its state is read by tailing
~/SDRTrunk/logs/sdrtrunk_app.log. Everything here reads files and returns; it
never starts, stops, or reconfigures SDRTrunk. Bounded (reads only the tail), so
a status poll never stalls on a huge log.

Log line format:  YYYYMMDD HHMMSS.mmm [thread] LEVEL logger - message [mem]
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

DEFAULT_LOG = Path(os.environ.get(
    "SB3_SDRTRUNK_LOG", os.path.expanduser("~/SDRTrunk/logs/sdrtrunk_app.log")))

TAIL_BYTES = 200_000        # read only the last ~200 KB
_TS_RE = re.compile(r"^(\d{8}) (\d{6})\.(\d{3})")
_BROADCAST_RE = re.compile(r"AudioStreamingBroadcaster - \[([^\]]+)\] status: (\w+)")
_TUNER_RE = re.compile(r"Tuner: (RSPduo Tuner \d+ SER#\w+)")
# Decode/activity signals (best-effort — presence of these = the decoder is live)
_ACTIVITY_RE = re.compile(
    r"GRP_VCH_GRNT|call|talkgroup|TSBK|NET_STATUS|RFSS_STATUS|control channel",
    re.IGNORECASE)
_LOCK_RE = re.compile(r"control channel|NAC|WACN|System:", re.IGNORECASE)


def _epoch(ts_date: str, ts_time: str, ms: str) -> Optional[float]:
    try:
        st = time.strptime(ts_date + ts_time, "%Y%m%d%H%M%S")
        return time.mktime(st) + int(ms) / 1000.0
    except (ValueError, OverflowError):
        return None


def _tail_lines(path: Path, nbytes: int = TAIL_BYTES) -> List[str]:
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > nbytes:
                f.seek(size - nbytes)
                f.readline()   # drop the partial first line
            return f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def observe(log_path: Optional[Path] = None, *, running: bool = True) -> Dict:
    """Structured SDRTrunk state for the Digital tab. Read-only.

    `running` is passed in by the caller (from launchctl) — this module does not
    probe processes, only the log.
    """
    path = log_path or DEFAULT_LOG
    lines = _tail_lines(path)

    broadcaster_status = None
    broadcaster_name = None
    tuners: List[str] = []
    last_ts: Optional[float] = None
    last_activity_ts: Optional[float] = None
    last_error: Optional[str] = None
    last_warning: Optional[str] = None
    lock_seen = False
    activity_count = 0
    now = time.time()

    for ln in lines:
        m = _TS_RE.match(ln)
        if m:
            last_ts = _epoch(*m.groups()) or last_ts
        bm = _BROADCAST_RE.search(ln)
        if bm:
            broadcaster_name, broadcaster_status = bm.group(1), bm.group(2)
        tm = _TUNER_RE.search(ln)
        if tm and tm.group(1) not in tuners:
            tuners.append(tm.group(1))
        if " ERROR " in ln:
            last_error = ln.split(" - ", 1)[-1][:200]
        elif " WARN " in ln:
            last_warning = ln.split(" - ", 1)[-1][:200]
        if _ACTIVITY_RE.search(ln):
            activity_count += 1
            if m:
                last_activity_ts = _epoch(*m.groups()) or last_activity_ts
        if _LOCK_RE.search(ln):
            lock_seen = True

    connected = (broadcaster_status == "Connected")
    # "recent" = activity within the last 2 minutes
    recent = bool(last_activity_ts and (now - last_activity_ts) < 120)

    return {
        "digital_active": bool(running and connected),
        "digital_backend": "sdrtrunk",
        "digital_profile": broadcaster_name or "",
        "digital_broadcaster_status": broadcaster_status,
        "digital_last_time": int(last_ts * 1000) if last_ts else 0,
        "digital_last_error": last_error or "",
        "digital_last_warning": last_warning or "",
        "digital_tuner_targets": tuners,
        "digital_control_channel_locked": bool(running and (recent or lock_seen)),
        "digital_control_channel_count": len(tuners),
        "digital_control_channel_last_time": int(last_activity_ts * 1000) if last_activity_ts else 0,
        "digital_control_channel_metric_ready": bool(lines),
        "digital_playlist_source_ok": bool(lines),
        "digital_playlist_source_type": "sdrtrunk-log",
        "digital_log_path": str(path),
        "digital_log_present": path.is_file() if hasattr(path, "is_file") else False,
    }
