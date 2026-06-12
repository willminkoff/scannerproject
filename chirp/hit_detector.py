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
        get_cluster_center_hz: Optional[Any] = None,
        priority_gate: Optional[Any] = None,
        audio_trace_enabled: bool = False,
    ) -> None:
        """Construct a hit detector.

        Phase 4-pre adds ``get_cluster_center_hz``: an optional zero-arg
        callable returning the current LO cluster center in Hz (or None
        if no scheduler is active).  When provided, every hit_start /
        hit_end event AND every JSONL log record gains a
        ``cluster_center_hz`` field correlating the hit to the LO state
        at the moment of detection.  The field is BACKWARD-COMPATIBLE —
        old consumers that don't look at it stay green.
        """
        self._slots = slots
        self._server = server
        self._poll_s = float(poll_s)
        self._warmup_s = float(warmup_s)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # zero-arg callable -> Optional[float]
        self._get_cluster_center_hz = get_cluster_center_hz
        # Optional PriorityGate: when set, each tick selects one open channel
        # to pass and mutes the rest (single active channel). None = disabled.
        self._priority_gate = priority_gate

        # Per-slot in-flight hit state.
        # key = slot.index, value = dict(open_ts, peak_dbfs, ch_id, freq_mhz, warmup)
        self._in_flight: dict[int, dict[str, Any]] = {}
        # Track last-seen open state per slot to detect transitions.
        self._last_open: dict[int, bool] = {}

        # ---- audio-path diagnostics (2026-06-11) -----------------------------
        # Per-tick snapshot exposed via :py:meth:`audio_path_snapshot` and
        # emitted as an `audio_path_state` event each tick when
        # ``audio_trace_enabled`` is True.  Field meanings:
        #   tick_lag_ms     time since the prior tick.  poll_s * 1000 in
        #                   steady state; large values (>500 ms with
        #                   poll_s=0.2) signal Python thread starvation
        #                   under GR scheduler load.
        #   open_count      live channels with squelch open this tick.
        #   muted_count     live channels with priority_muted=True after
        #                   the gate update.  When priority_gate is
        #                   disabled this stays 0.
        #   parked_count    channels skipped because the LO scheduler has
        #                   them parked.  These are NOT included in
        #                   muted_count (they're outside the gate).
        #   live_count      claimed channels not parked this tick.
        #   selected_id     priority gate selection (None if no opens).
        #   audio_path_health
        #       "live"               at least one live, non-muted channel
        #                            is squelch-open → audio should flow
        #       "all_muted"          all live channels are priority-muted
        #                            but at least one is squelch-open.
        #                            THIS IS THE BUG SIGNATURE — gate
        #                            failed to unmute the open channel.
        #       "no_open"            channels are live but none are
        #                            squelch open right now.  Expected
        #                            quiet state; mp3 mount is silent
        #                            because nothing is keying, not
        #                            because the gate is broken.
        #       "no_live"            every claimed channel is parked.
        #                            LO scheduler hasn't unparked any
        #                            cluster yet (cold start) or in a
        #                            plan_failed degraded state.
        self._audio_trace_enabled = bool(audio_trace_enabled)
        self._last_tick_ts: Optional[float] = None
        # Latest tick snapshot (updated atomically per-tick by _tick()).
        # Reads do not lock: dict assignment is GIL-atomic in CPython, and
        # a slightly-stale read for an introspection endpoint is fine.
        self._audio_path: dict[str, Any] = {
            "tick_lag_ms": None,
            "open_count": 0,
            "muted_count": 0,
            "parked_count": 0,
            "live_count": 0,
            "selected_id": None,
            "audio_path_health": "no_live",
        }

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
        cluster_center_hz = self._read_cluster_center()
        # Priority gate: collect the live (non-parked, claimed) channels and
        # which of them are squelch-open this tick, then arbitrate after the
        # loop so exactly one passes audio.
        live_channels: dict[str, Any] = {}
        open_ids: list[str] = []
        for s in self._slots:
            if s.user_id is None:
                # Slot empty: clean up any stale in-flight state.
                if s.index in self._in_flight:
                    self._in_flight.pop(s.index, None)
                    self._last_open.pop(s.index, None)
                continue
            # Phase 4-pre: parked channels MUST NOT fire hit events.  Drop
            # any in-flight state silently (no hit_end emit) — the LO
            # scheduler is responsible for the parked transition and emits
            # its own cluster_hop event.  Reset last_open=False so the
            # next unpark + RF activity establishes a clean transition.
            if getattr(s.channel, "is_parked", False):
                if s.index in self._in_flight:
                    self._in_flight.pop(s.index, None)
                self._last_open[s.index] = False
                continue
            try:
                is_open = bool(s.channel.get_squelch_open())
                lvl = float(s.channel.get_signal_level_dbfs())
            except Exception:
                log.exception("could not read slot %d", s.index)
                continue
            if s.user_id is not None:
                live_channels[s.user_id] = s.channel
                if is_open:
                    open_ids.append(s.user_id)
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
                    "cluster_center_hz": cluster_center_hz,
                }
                hs_kwargs = {
                    "ch": s.user_id,
                    "freq_mhz": s.last_freq_mhz,
                    "level_dbfs": lvl,
                    "warmup": in_warmup,
                }
                if cluster_center_hz is not None:
                    hs_kwargs["cluster_center_hz"] = cluster_center_hz
                self._server.emit_event("hit_start", **hs_kwargs)
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
                    # Tag with cluster center from hit_start time — the LO
                    # might have hopped during the hit, but the start was
                    # observed on the original cluster.  Backward-compat:
                    # only emit when scheduler is wired and produced a
                    # non-None center.
                    if hit.get("cluster_center_hz") is not None:
                        record["cluster_center_hz"] = hit["cluster_center_hz"]
                    self._server.emit_event("hit_end", **record)
                    self._append_log(record)
            self._last_open[s.index] = is_open

        # Priority gate: select one open channel; mute the rest (live only).
        selected: Optional[str] = None
        if self._priority_gate is not None:
            try:
                selected = self._priority_gate.update(open_ids, now)
                for cid, ch in live_channels.items():
                    ch.set_priority_muted(cid != selected)
            except Exception:
                log.exception("priority gate update failed")

        # ---- audio-path diagnostics (2026-06-11) -----------------------------
        # Recompute the audio_path snapshot after the priority gate has
        # had its say.  Cheap: walks the slot list once, no GR work.
        # Always updates (even with audio_trace disabled) so get_status
        # callers see fresh data; the event emit is what trace gates.
        try:
            parked_count = 0
            muted_count = 0
            for s in self._slots:
                if s.user_id is None:
                    continue
                if getattr(s.channel, "is_parked", False):
                    parked_count += 1
                    continue
                # is_priority_muted is a property on Channel; treat absent
                # attribute as not-muted so older Channel instances don't
                # crash this counter.
                try:
                    if bool(getattr(s.channel, "is_priority_muted", False)):
                        muted_count += 1
                except Exception:
                    pass
            live_count = len(live_channels)
            open_count = len(open_ids)
            if live_count == 0:
                health = "no_live"
            elif open_count == 0:
                health = "no_open"
            elif muted_count >= live_count:
                # Every live channel is muted yet at least one is open.
                # This is the silent-mp3-while-hits-fire signature.
                health = "all_muted"
            else:
                health = "live"
            tick_lag_ms: Optional[float] = None
            if self._last_tick_ts is not None:
                tick_lag_ms = round((now - self._last_tick_ts) * 1000.0, 1)
            self._last_tick_ts = now

            self._audio_path = {
                "tick_lag_ms": tick_lag_ms,
                "open_count": open_count,
                "muted_count": muted_count,
                "parked_count": parked_count,
                "live_count": live_count,
                "selected_id": selected,
                "audio_path_health": health,
            }

            if self._audio_trace_enabled:
                self._server.emit_event(
                    "audio_path_state",
                    **self._audio_path,
                )
        except Exception:
            log.exception("audio_path snapshot failed")

    # -- Phase 4-pre helper -------------------------------------------------

    def _read_cluster_center(self) -> Optional[float]:
        """Read the current LO cluster center from the scheduler callback.

        Defensive: any exception from the callback returns None so the
        hit detector keeps working even if the scheduler is mid-recompute.
        """
        cb = self._get_cluster_center_hz
        if cb is None:
            return None
        try:
            v = cb()
            return None if v is None else float(v)
        except Exception:
            return None

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

    def audio_path_snapshot(self) -> dict[str, Any]:
        """Return the latest audio-path diagnostic snapshot.

        See the docstring on ``self._audio_path`` for field meanings.
        Always returns the most-recent tick's data; safe to call from
        any thread (dict read is GIL-atomic).
        """
        return dict(self._audio_path)


__all__ = ["HitDetector", "DEFAULT_HIT_LOG", "DEFAULT_POLL_S"]
