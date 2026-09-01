"""op25_client — sb3-ui interface to remote op25 state (replaces sdrtrunk_client).

Reads op25 log via HTTP log server on Venus. Returns the same dict shape as
sdrtrunk_client.observe() so routes.py can drop-in swap.
"""
from __future__ import annotations
import json
import os
import re
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, List, Optional

REMOTE_URL = os.environ.get("SB3_OP25_REMOTE_URL", "").rstrip("/")
TAIL_BYTES = 500_000

# op25 log line formats
_TS_RE = re.compile(r'^(\d{2}/\d{2}/\d{2})\s+(\d{2}:\d{2}:\d{2})\.(\d+)')
_VOICE_RE = re.compile(r'voice update:\s+tg\((\d+)\),\s+rid\((\d+)\),\s+freq\(([\d.]+)\),\s+slot\((\S+)\),\s+prio\((\d+)\)')
_CC_TIMEOUT_RE = re.compile(r'control channel timeout,\s+freq\(([\d.]+)\)')
_CC_LOCK_RE = re.compile(r'\[MTRTRS|\[TACN|Initializing P25 system|added talkgroup')

def _epoch(mmdd: str, hhmmss: str, ms: str) -> Optional[float]:
    try:
        # MM/DD/YY HH:MM:SS
        month, day, year = mmdd.split('/')
        year = int(year) + 2000 if int(year) < 100 else int(year)
        struct = time.strptime(f'{year}-{month}-{day} {hhmmss}', '%Y-%m-%d %H:%M:%S')
        return time.mktime(struct) + int(ms) / 10 ** len(ms) if ms else time.mktime(struct)
    except Exception:
        return None

def _fetch_log_tail(nbytes: int = TAIL_BYTES) -> List[str]:
    if not REMOTE_URL:
        return []
    try:
        url = f'{REMOTE_URL}/op25.log?tail={nbytes}'
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = resp.read()
        return data.decode('utf-8', errors='ignore').splitlines()
    except (urllib.error.URLError, TimeoutError, OSError):
        return []

def _fetch_instances() -> List[Dict]:
    if not REMOTE_URL:
        return []
    try:
        with urllib.request.urlopen(f'{REMOTE_URL}/instances', timeout=3) as resp:
            return json.loads(resp.read())
    except Exception:
        return []

def observe(log_path: Optional[Path] = None, *, running: bool = True) -> Dict:
    """Structured op25 state for the Digital tab. Read-only.

    Mirrors sdrtrunk_client.observe() output shape so sb3-ui doesn't need
    to change.
    """
    lines = _fetch_log_tail()
    instances = _fetch_instances()

    tuners: List[str] = []
    for inst in instances:
        sys_name = inst.get('system_name')
        if sys_name and sys_name not in tuners:
            tuners.append(sys_name)

    last_ts: Optional[float] = None
    last_activity_ts: Optional[float] = None
    last_error: Optional[str] = None
    last_warning: Optional[str] = None
    lock_seen = False
    activity_count = 0
    recent_calls: List[Dict] = []

    for ln in lines:
        m = _TS_RE.match(ln)
        ts = _epoch(*m.groups()) if m else None
        if ts:
            last_ts = ts
        if 'ERROR' in ln:
            last_error = ln[:200]
        elif 'WARNING' in ln or 'WARN' in ln:
            last_warning = ln[:200]
        vm = _VOICE_RE.search(ln)
        if vm:
            activity_count += 1
            if ts:
                last_activity_ts = ts
            recent_calls.append({
                'tg': int(vm.group(1)),
                'rid': int(vm.group(2)),
                'freq_mhz': float(vm.group(3)),
                'slot': vm.group(4),
                'prio': int(vm.group(5)),
                'ts': ts,
            })
        if _CC_TIMEOUT_RE.search(ln):
            last_warning = ln[:200]
        elif _CC_LOCK_RE.search(ln):
            lock_seen = True

    # Keep only most recent 50 calls
    recent_calls = recent_calls[-50:]

    now = time.time()
    return {
        'digital_log_present': bool(REMOTE_URL) and bool(lines),
        'digital_broadcaster_name': 'op25',
        'digital_broadcaster_status': 'CONNECTED' if activity_count > 0 or lock_seen else 'IDLE',
        'digital_tuners': tuners,
        'digital_last_ts': last_ts,
        'digital_last_activity_ts': last_activity_ts,
        'digital_activity_age_sec': (now - last_activity_ts) if last_activity_ts else None,
        'digital_last_error': last_error,
        'digital_last_warning': last_warning,
        'digital_lock_seen': lock_seen,
        'digital_activity_count': activity_count,
        'digital_recent_calls': recent_calls,
        'digital_running': running,
    }
