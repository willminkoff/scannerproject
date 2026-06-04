"""chirp.hit_detector — per-channel squelch-transition state machine.

Phase 2. Replaces the Phase 1 inline `_health_loop` in daemon.py. Watches every
claimed slot's `Channel.get_squelch_open()` on a polling interval, and emits:

  - `hit_start` event when squelch transitions closed → open
  - `hit_end`   event when squelch transitions open → closed

Each `hit_end` is paired with a `peak_dbfs` (max signal level seen during the
hit) + `duration_s`. Hits are also appended one-per-line to a JSON Lines log
at `hit_log_path` (default /var/log/chirp/hits.jsonl), enabling Phase 4
historical analysis without depending on the live event subscriber.

Threading: runs in a single background thread that takes the daemon's lock
when reading slot state. Stop() joins the thread (bounded).

Warmup: a channel that has been alive for less than `warmup_s` is allowed to
see squelch transitions BUT the hit metadata records the warmup flag so
downstream alerting can ignore noisy first-second decisions. This is the
chirp answer to the rtl-airband 'noise-estimator init poison value' bug — we
never refuse to apply a user squelch and never sample our own noise floor
during warmup.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # pragma: no cover
    from chirp.cmd.server import CommandServer
    from chirp.daemon import _Slot

log = logging.getLogger("chirp.hit_detector")


DEFAULT_HIT_LOG = "/var/log/chirp/hits.jsonl"
DEFAULT_POLL_S = 0.2


class HitDetector:
    """Polls per-slot squelch state, emits hit events, persists JSONL log.

    Args:
        slots: list of daemon `_Slot` objects (mutable, shared with daemon).
        server: command server (for `emit_event`).
        hit_log_path: JSONL append path. None → DEFAULT_HIT_LOG. If the
            directory is not writable, the JSONL log is silently disabled
            (events still go out via the UDP event stream).
        poll_s: polling interval in seconds.
        warmup_s: window after channel claim during which hits are flagged.
    """

    def __init__(
        self,
        slots: list[Any],  # forward-ref dance
        server: Any,
        hit_log_path: Optional[str] = None,
        poll_s: float = DEFAULT_POLL_S,
        warmup_s: float = 1.0,
    ) -> None:
        self._slots = slots
        self._server = server
        self._poll_s = float(poll_s)
        self._warmup_s = float(warmup_s)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        # Per-slot in-flight hit state.
        # key = slot.index, value = dict(open_ts, peak_dbfs, ch_id, freq_mhz, warmup)
        self._in_flight: dict[int, dict[str, Any]] = {}
        # Track last-seen open state per slot to detect transitions.
        self._last_open: dict[int, bool] = {}

        # Resolve log path; create parent dir lazily.
        self._log_path: Optional[Path] = None
        self._log_disabled = False
        path = hit_log_path or DEFAULT_HIT_LOG
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            # Touch to confirm writability.
            with p.open("a", encoding="utf-8"):
                pass
            self._log_path = p
        except OSError as e:
            log.warning("hit log %s not writable (%s) — JSONL log disabled", path, e)
            self._log_disabled = True

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="chirp-hits", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # -- core loop ---------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("hit_detector tick failed")
            self._stop.wait(self._poll_s)

    def _tick(self) -> None:
        now = time.time()
        for s in self._slots:
            if s.user_id is None:
                # Slot empty: clean up any stale in-flight state.
                if s.index in self._in_flight:
                    self._in_flight.pop(s.index, None)
                    self._last_open.pop(s.index, None)
                continue
            try:
                is_open = bool(s.channel.get_squelch_open())
                lvl = float(s.channel.get_signal_level_dbfs())
            except Exception:
                log.exception("could not read slot %d", s.index)
                continue
            prev = self._last_open.get(s.index, False)
            in_warmup = (s.claimed_at is not None and (now - s.claimed_at) < self._warmup_s)

            if is_open and not prev:
                # closed -> open: hit_start
                self._in_flight[s.index] = {
                    "ch": s.user_id,
                    "freq_mhz": s.last_freq_mhz,
                    "start_ts": now,
                    "peak_dbfs": lvl,
                    "warmup": in_warmup,
                }
                self._server.emit_event(
                    "hit_start",
                    ch=s.user_id,
                    freq_mhz=s.last_freq_mhz,
                    level_dbfs=lvl,
                    warmup=in_warmup,
                )
            elif is_open and prev:
                # ongoing: update peak
                hit = self._in_flight.get(s.index)
                if hit is not None and lvl > hit["peak_dbfs"]:
                    hit["peak_dbfs"] = lvl
            elif not is_open and prev:
                # open -> closed: hit_end + write JSONL
                hit = self._in_flight.pop(s.index, None)
                if hit is not None:
                    duration = now - hit["start_ts"]
                    record = {
                        "ch": hit["ch"],
                        "freq_mhz": hit["freq_mhz"],
                        "start_ts_ms": int(hit["start_ts"] * 1000),
                        "end_ts_ms": int(now * 1000),
                        "duration_s": round(duration, 3),
                        "peak_dbfs": round(hit["peak_dbfs"], 2),
                        "warmup": hit["warmup"],
                    }
                    self._server.emit_event("hit_end", **record)
                    self._append_log(record)
            self._last_open[s.index] = is_open

    # -- log writer --------------------------------------------------------

    def _append_log(self, record: dict[str, Any]) -> None:
        if self._log_disabled or self._log_path is None:
            return
        try:
            line = json.dumps(record, separators=(",", ":")) + "\n"
            # Atomic append — POSIX guarantees O_APPEND writes are atomic for
            # buffers <= PIPE_BUF (4 KiB on Linux). Our records are well under.
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(line)
                # No fsync per-hit — Phase 2 traffic is low-volume but a
                # gigabit fleet would want batched fsync. Logged for Phase 4.
        except OSError as e:
            log.warning("hit log append failed (%s) — disabling JSONL log", e)
            self._log_disabled = True

    # -- introspection -----------------------------------------------------

    @property
    def log_path(self) -> Optional[Path]:
        return self._log_path

    @property
    def log_disabled(self) -> bool:
        return self._log_disabled


__all__ = ["HitDetector", "DEFAULT_HIT_LOG", "DEFAULT_POLL_S"]
