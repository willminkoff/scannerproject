#!/bin/bash
set -euo pipefail

journalctl -u rtl-airband -f -n 0 -o short-iso --no-pager \
| python3 -u - <<'PY'
import datetime
import re
import sys

LINE_RE = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?P<tz>[+-]\d{4})?\s+.*Activity on (?P<freq>[0-9]+\.[0-9]+)'
)

HIT_GAP_RESET_SECONDS = 10
current_freq = None
start_ts = None
last_ts = None

def parse_ts(ts, tz):
    if tz:
        tz = f"{tz[:3]}:{tz[3:]}"
        return datetime.datetime.fromisoformat(f"{ts}{tz}")
    return datetime.datetime.fromisoformat(ts)

for line in sys.stdin:
    match = LINE_RE.search(line)
    if not match:
        continue
    ts = parse_ts(match.group("ts"), match.group("tz"))
    freq = match.group("freq")
    gap = (ts - last_ts).total_seconds() if last_ts else None
    if freq != current_freq or (gap is not None and gap > HIT_GAP_RESET_SECONDS):
        current_freq = freq
        start_ts = ts
    duration = int((ts - start_ts).total_seconds()) if start_ts else 0
    last_ts = ts
    print(f"{ts.strftime('%H:%M:%S')} Activity on {freq} MHz (dur {duration}s)")
    sys.stdout.flush()
PY
