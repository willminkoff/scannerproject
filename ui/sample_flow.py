"""Sample-flow liveness signals for rtl-airband + icecast.

Background
----------
Both ``rtl-airband`` and ``icecast2`` can be reported "active" by systemd
while their actual data pipelines are dead:

- ``rtl-airband`` hits a SoapySDR ``readStream TIMEOUT`` or ``Device has
  been removed`` from the sdrplay daemon, logs a warning, and *keeps the
  process alive* in a wait state — no samples flow, no stats updates.
  systemd sees a running PID and reports ``active (running)``.

- ``icecast2`` (the SysV-init wrapped daemon) can die after a runtime
  signal; systemd's wrapper still reports ``active (exited)`` because
  the init script's start phase succeeded once.  The mount entries
  remain in icecast's status JSON for fallback chaining, so a naive
  "mount listed → mount alive" check is also misleading.

Both failure modes silently break the audio pipeline.  This module
provides two narrow, trustworthy signals that the API layer can use to
stop lying:

1. ``rtl_airband_sample_flow_state()`` — reads the stats file mtime.
   Fresh mtime ⇒ samples flowing.  Stale ⇒ pipeline stuck regardless of
   what systemd thinks.

2. ``mount_publishing()`` — checks the icecast status JSON for a
   ``stream_start`` (or ``stream_start_iso8601``) timestamp on the
   target mount.  icecast resets that field to null/empty when the
   source disconnects; a non-empty value means a source is actively
   publishing bytes to that mount right now.

Neither signal is heuristic: they read the exact field a healthy
upstream component is contractually required to update.  When the
field stops updating, the pipeline IS broken — full stop.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional


def rtl_airband_sample_flow_state(
    stats_path: str,
    stale_threshold_sec: float,
    *,
    now: Optional[float] = None,
) -> dict:
    """Return a dict describing rtl-airband sample-flow liveness.

    Parameters
    ----------
    stats_path:
        Filesystem path to ``rtl_airband_stats.txt``.  In normal
        operation rtl-airband rewrites this file on every output_thread
        cycle (~1 s).
    stale_threshold_sec:
        Maximum acceptable age, in seconds, before the pipeline is
        considered stuck.  Calibrate to leave headroom for legitimate
        startup retunes (which can pause writes for a few seconds) but
        tight enough to catch real zombies within the watchdog window.
    now:
        Optional wall-clock seed (for testability).  Defaults to
        ``time.time()``.

    Returns
    -------
    A small dict suitable for direct inclusion in /api/status:

    - ``stats_path``        : the path being probed
    - ``stats_mtime``       : float epoch seconds, or None if missing
    - ``stats_age_sec``     : age in seconds, or None if missing
    - ``stale_threshold_sec``: the threshold this evaluation used
    - ``sample_flow_ok``    : bool — True iff age < threshold
    - ``reason``            : short string when not ok, "" when ok
    """
    now_ts = float(now) if now is not None else time.time()
    threshold = float(stale_threshold_sec)
    try:
        mtime = os.path.getmtime(stats_path)
    except (FileNotFoundError, OSError):
        return {
            "stats_path": stats_path,
            "stats_mtime": None,
            "stats_age_sec": None,
            "stale_threshold_sec": threshold,
            "sample_flow_ok": False,
            "reason": "stats file missing",
        }
    age = now_ts - float(mtime)
    if age < 0:
        # Clock skew or stats file written in the future; treat as fresh
        # to avoid false alarms during system clock resync.
        age = 0.0
    ok = age < threshold
    return {
        "stats_path": stats_path,
        "stats_mtime": float(mtime),
        "stats_age_sec": float(age),
        "stale_threshold_sec": threshold,
        "sample_flow_ok": bool(ok),
        "reason": "" if ok else f"stats stale ({age:.1f}s > {threshold:.1f}s)",
    }


def _find_source_for_mount(status_text: str, mount_name: str) -> Optional[dict]:
    """Return the source dict matching ``mount_name`` from icecast JSON.

    ``mount_name`` is matched both against the bare mount string (after
    stripping leading ``/``) and against the tail of the ``listenurl``,
    so callers can pass either ``"ANALOG.mp3"`` or ``"/ANALOG.mp3"``.
    Returns ``None`` when the mount is not in the sources list at all.
    """
    if not status_text or not mount_name:
        return None
    try:
        data = json.loads(status_text)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    sources = (data.get("icestats") or {}).get("source")
    if not sources:
        return None
    if not isinstance(sources, list):
        sources = [sources]
    wanted = str(mount_name).strip().lstrip("/")
    if not wanted:
        return None
    for source in sources:
        if not isinstance(source, dict):
            continue
        mount = str(source.get("mount") or "").strip().lstrip("/")
        listenurl = str(source.get("listenurl") or "").strip()
        if mount == wanted:
            return source
        if listenurl.rsplit("/", 1)[-1] == wanted:
            return source
    return None


def mount_publishing(status_text: str, mount_name: str) -> bool:
    """True iff a source is actively publishing to ``mount_name``.

    The truth signal is icecast's ``stream_start`` (or
    ``stream_start_iso8601``) field — it gets populated when a source
    client successfully connects, and reset to null/empty when that
    source disconnects.  Other metadata fields (``server_name``,
    ``server_type``, ``audio_info``) are NOT reliable for this — icecast
    holds them across mount-fallback transitions even after the
    originating source has gone away.  We learned this the hard way
    when a zombie ``ANALOG.mp3`` mount reported ``server_name =
    "SprontPi Radio"`` while ``stream_start=None`` and zero listeners
    were actually receiving bytes.
    """
    source = _find_source_for_mount(status_text, mount_name)
    if source is None:
        return False
    for key in ("stream_start", "stream_start_iso8601"):
        value = source.get(key)
        if value is None:
            continue
        if isinstance(value, str) and value.strip():
            return True
        if not isinstance(value, str) and value:
            return True
    return False
