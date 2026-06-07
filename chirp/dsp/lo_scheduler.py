"""chirp.dsp.lo_scheduler — round-robin LO retuning state machine.

Phase 4-pre.  This module is the runtime counterpart to
:mod:`chirp.dsp.cluster_planner`.  Given a callback that returns the current
pool channels and a callback that retunes the SDR source, the scheduler:

* recomputes the cluster plan whenever pool membership changes (via
  :py:meth:`invalidate`);
* rotates through the planned clusters on a configurable dwell;
* parks all channels NOT in the active cluster (silencing them and
  suppressing hit detection) and unparks the active cluster's channels;
* emits ``cluster_hop`` events on every transition with ``from_center_hz``,
  ``to_center_hz``, ``cluster_idx``, ``dwell_actual_sec``, and a timestamp.

Design notes
------------

* **No GR imports here.** The scheduler is plain Python with a background
  thread + ``threading.Event`` for stop / invalidate signals.  All
  flowgraph interaction is via injected callbacks (``retune_to``,
  ``park_channels``, ``unpark_channels``, ``emit_event``).  This makes the
  state machine trivially unit-testable.

* **Thread model.** The scheduler runs a single background thread that
  wakes up every ``tick_s`` seconds (default 0.25 s — same cadence as the
  daemon's main loop).  Each wake checks dwell expiry / invalidation and
  steps the state machine.  All mutations are guarded by ``self._lock``.

* **Canonical lock order (CRITICAL).** When a thread takes BOTH the
  daemon's ``ChirpFlowgraph._lock`` (the "D-lock") AND this scheduler's
  ``self._lock`` (the "S-lock"), it MUST acquire **D-lock before S-lock**.
  The scheduler's ``step()`` enforces this internally by acquiring the
  injected ``daemon_lock`` before the S-lock.  Callers that already hold
  the daemon lock (the cmd-server path via
  ``ChirpFlowgraph._invalidate_and_apply_now``) pay only an RLock-reentry
  on D.  Threads that take only S (``current_cluster_center_hz`` from
  the hit-detector) are unaffected.  See ``DESIGN_lo_scheduler_lockfix.md``
  for the historical wedge (2026-06-04 21:26:21 EDT) this invariant
  prevents.

* **Single-cluster degenerate case.** If the planner returns exactly one
  cluster, the scheduler tunes there once, unparks its channels, and stops
  rotating.  This is the "small channel list" case — no LO retuning
  needed — and it MUST NOT regress relative to the pre-Phase-4-pre daemon
  behaviour.  We assert this in the tests.

* **Plan-failed degraded mode.** If :py:func:`plan_clusters` raises
  :class:`~chirp.dsp.cluster_planner.ClusterPlanError` (channel list
  needs more clusters than the cap allows), the scheduler logs the
  reason, emits a ``scheduler_plan_failed`` event, and parks every
  channel until the operator fixes the configuration (raises
  ``CHIRP_LO_MAX_CLUSTERS`` or trims the channel list).  No retunes
  happen in this state.

* **Cluster_hop is the source of truth.** The hit detector tags every
  hit with the current cluster center via a separate callback
  (``current_cluster_center_hz``).  The pair lets downstream consumers
  correlate hits to LO state without parsing the cluster_hop stream.

* **Operator changes mid-dwell.** Adding or removing a channel calls
  :py:meth:`invalidate`, which causes the next tick to recompute.  The
  CURRENT dwell is NOT cut short — the rationale is that mid-dwell
  retunes would create audio glitches and possible double-counted hits;
  better to wait out the current dwell and pick up the new plan at the
  next normal hop.  (Configurable Phase 5: ``invalidate(immediate=True)``.)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Iterable, Optional

from chirp.dsp.cluster_planner import (
    ClusterPlan,
    ClusterPlanError,
    PlanChannel,
    plan_clusters,
)


log = logging.getLogger("chirp.lo_scheduler")


DEFAULT_DWELL_S = 60.0
DEFAULT_MAX_CLUSTERS = 3
DEFAULT_TICK_S = 0.25


class LoScheduler:
    """Round-robin LO retuning state machine.

    All flowgraph interaction is via injected callbacks so the scheduler
    can be unit-tested without GR.

    Args:
        get_channels: zero-arg callable returning the current pool
            channels (with optional ``recent_hits`` activity scores).
        retune_to: ``(hz: float) -> None`` — retune the source LO.
        park_channels: ``(ids: set[str]) -> None`` — park these channels.
        unpark_channels: ``(ids: set[str]) -> None`` — unpark these
            channels.
        emit_event: ``(name: str, **kwargs) -> None`` — emit a daemon
            event (forwarded to the UDP event stream).  Same signature
            as ``CommandServer.emit_event``.
        iq_bw_hz: source IQ window width (Hz).  Passed to
            :py:func:`plan_clusters`.
        dwell_s: dwell on each cluster (seconds).  Default 60.
        max_clusters: cap on cluster count.  Default 3.
        tick_s: scheduler wake interval (seconds).  Default 0.25.
        clock: injectable monotonic-ish clock (defaults to ``time.time``).
            Tests pass a fake clock.
        daemon_lock: optional reentrant lock (the daemon's ``D-lock``)
            that ``step()`` will acquire BEFORE its internal S-lock to
            honour the canonical lock order (see module docstring).
            Production wires ``ChirpFlowgraph._lock`` here.  Tests that
            do not exercise the cross-lock path may pass ``None``; a
            private RLock is created so the acquire-D-first code path
            still runs (no real serialisation, just uniform shape).
    """

    def __init__(
        self,
        *,
        get_channels: Callable[[], Iterable[PlanChannel]],
        retune_to: Callable[[float], None],
        park_channels: Callable[[set[str]], None],
        unpark_channels: Callable[[set[str]], None],
        emit_event: Callable[..., None],
        iq_bw_hz: float,
        dwell_s: float = DEFAULT_DWELL_S,
        max_clusters: int = DEFAULT_MAX_CLUSTERS,
        tick_s: float = DEFAULT_TICK_S,
        clock: Callable[[], float] = time.time,
        daemon_lock: Optional[threading.RLock] = None,
        is_open: Optional[Callable[[str], bool]] = None,
        scan_hold_enabled: bool = False,
        scan_hold_hang_sec: float = 2.0,
        scan_hold_max_sec: float = 30.0,
    ) -> None:
        if iq_bw_hz <= 0:
            raise ValueError(f"iq_bw_hz must be > 0, got {iq_bw_hz!r}")
        if dwell_s <= 0:
            raise ValueError(f"dwell_s must be > 0, got {dwell_s!r}")
        if max_clusters < 1:
            raise ValueError(f"max_clusters must be >= 1, got {max_clusters!r}")
        if tick_s <= 0:
            raise ValueError(f"tick_s must be > 0, got {tick_s!r}")
        if scan_hold_enabled:
            if scan_hold_hang_sec <= 0:
                raise ValueError(
                    f"scan_hold_hang_sec must be > 0, got {scan_hold_hang_sec!r}"
                )
            if scan_hold_max_sec <= 0:
                raise ValueError(
                    f"scan_hold_max_sec must be > 0, got {scan_hold_max_sec!r}"
                )

        self._get_channels = get_channels
        self._retune_to = retune_to
        self._park_channels = park_channels
        self._unpark_channels = unpark_channels
        self._emit_event = emit_event
        self._iq_bw_hz = float(iq_bw_hz)
        self._dwell_s = float(dwell_s)
        self._max_clusters = int(max_clusters)
        self._tick_s = float(tick_s)
        self._clock = clock

        # Scan-hold: latch the LO on a cluster while a TX is live, instead of
        # hopping away mid-transmission.  ``is_open(id)`` reads a live
        # channel's squelch-open state (backed by Channel.get_squelch_open).
        self._is_open = is_open
        self._scan_hold_enabled = bool(scan_hold_enabled)
        self._scan_hold_hang_sec = float(scan_hold_hang_sec)
        self._scan_hold_max_sec = float(scan_hold_max_sec)
        # Hold state (None/0 when not holding).
        self._hold_active: bool = False
        self._hold_start_ts: Optional[float] = None
        self._hold_last_open_ts: Optional[float] = None
        self._hold_trigger_id: Optional[str] = None
        self._hold_extended_n: int = 0
        # Ids that were open on the previous scan-hold evaluation, so a
        # closed→open transition (re-key or a new channel) can extend the hold.
        self._hold_prev_open_ids: set[str] = set()

        self._lock = threading.RLock()
        # See canonical-lock-order note in module docstring.  When the
        # caller does not provide a daemon lock (unit tests, primarily)
        # we still create a private RLock so ``step()`` exercises the
        # acquire-D-before-S code path uniformly — there is no other
        # contender for the private lock, so it serialises nothing.
        self._daemon_lock: threading.RLock = (
            daemon_lock if daemon_lock is not None else threading.RLock()
        )
        self._stop = threading.Event()
        self._invalidate_evt = threading.Event()
        # Force initial plan compute on the first step.
        self._invalidate_evt.set()
        self._thread: Optional[threading.Thread] = None

        self._current_plan: list[ClusterPlan] = []
        self._current_cluster_idx: int = 0
        self._cluster_dwell_start_ts: Optional[float] = None
        self._last_recompute_ts: Optional[float] = None
        self._plan_failed_reason: Optional[str] = None
        self._plan_needed: Optional[int] = None
        # Track all pool ids we've ever seen in the plan, so when a plan
        # shrinks we can park the channels that fell out.
        self._known_pool_ids: set[str] = set()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="chirp-lo-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def invalidate(self) -> None:
        """Mark the cluster plan as stale.

        The next scheduler tick will recompute the plan from
        ``get_channels()``.  Cheap; safe to call from any thread.  The
        current dwell timer is NOT cut short — see module docstring.
        """
        self._invalidate_evt.set()

    # -- introspection -----------------------------------------------------

    @property
    def dwell_s(self) -> float:
        return self._dwell_s

    @property
    def max_clusters(self) -> int:
        return self._max_clusters

    @property
    def iq_bw_hz(self) -> float:
        return self._iq_bw_hz

    def current_cluster_center_hz(self) -> Optional[float]:
        """Return the active cluster's LO center, or None if no plan yet."""
        with self._lock:
            if not self._current_plan:
                return None
            return self._current_plan[self._current_cluster_idx].center_hz

    def snapshot(self) -> dict:
        """Return a JSON-serializable snapshot for ``get_status``.

        Always returns the same key set, so the dashboard can rely on the
        shape (None-valued fields when no plan is active).
        """
        with self._lock:
            base = {
                "dwell_s": self._dwell_s,
                "max_clusters": self._max_clusters,
                "iq_bw_hz": self._iq_bw_hz,
                "tick_s": self._tick_s,
                "plan_failed_reason": self._plan_failed_reason,
                "plan_needed": self._plan_needed,
                "last_recompute_ts": self._last_recompute_ts,
                "scan_hold_enabled": self._scan_hold_enabled,
                "scan_hold_state": "holding" if self._hold_active else "scanning",
                "held_channel_id": self._hold_trigger_id,
                "hold_elapsed_sec": (
                    round(self._clock() - self._hold_start_ts, 3)
                    if self._hold_active and self._hold_start_ts is not None
                    else None
                ),
                "hold_extended_n_times": self._hold_extended_n,
            }
            if not self._current_plan:
                base.update({
                    "current_cluster_center_hz": None,
                    "current_cluster_idx": None,
                    "dwell_remaining_sec": None,
                    "clusters": [],
                    "live_channel_ids": [],
                    "parked_channel_ids": [],
                    "single_cluster": False,
                    "n_clusters": 0,
                })
                return base
            cur = self._current_plan[self._current_cluster_idx]
            now = self._clock()
            if self._cluster_dwell_start_ts is None:
                remaining = None
            else:
                elapsed = now - self._cluster_dwell_start_ts
                remaining = max(0.0, self._dwell_s - elapsed)
            live = set(cur.channel_ids)
            all_planned: set[str] = set()
            for p in self._current_plan:
                all_planned.update(p.channel_ids)
            parked = all_planned - live
            base.update({
                "current_cluster_center_hz": cur.center_hz,
                "current_cluster_idx": self._current_cluster_idx,
                "dwell_remaining_sec": (
                    None if remaining is None else round(remaining, 3)
                ),
                "clusters": [p.to_dict() for p in self._current_plan],
                "live_channel_ids": sorted(live),
                "parked_channel_ids": sorted(parked),
                "single_cluster": len(self._current_plan) == 1,
                "n_clusters": len(self._current_plan),
            })
            return base

    # -- main loop ---------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.step(self._clock())
            except Exception:
                log.exception("lo_scheduler step failed")
            self._stop.wait(self._tick_s)

    def step(self, now: Optional[float] = None) -> None:
        """Advance the state machine by one tick.

        Public so tests can drive the scheduler deterministically without
        a background thread.  Holds ``self._lock`` for the duration of
        the step so callbacks (``retune_to``, ``park_channels``,
        ``unpark_channels``, ``emit_event``) see a consistent state.

        Lock-order discipline: acquires ``self._daemon_lock`` BEFORE
        ``self._lock`` to honour the canonical D-before-S order.  When the
        caller is the cmd-server thread already holding the daemon lock
        (``_invalidate_and_apply_now`` path), this is a free RLock reentry
        on D.  When the caller is the scheduler's own ``_loop``, this
        acquires D fresh — possibly briefly blocking on an in-flight cmd
        handler, which is the intended serialisation.  See the
        "Canonical lock order" note in the module docstring.
        """
        if now is None:
            now = self._clock()
        with self._daemon_lock, self._lock:
            # 1) Recompute plan if needed.
            if self._invalidate_evt.is_set():
                self._invalidate_evt.clear()
                self._recompute_plan(now)

            # 2) If no usable plan, nothing more to do this tick.
            if not self._current_plan:
                return

            # 3) Apply first cluster on first tick after recompute.
            if self._cluster_dwell_start_ts is None:
                self._apply_cluster(0, now, from_idx=None)
                return

            # 4) Single-cluster plans don't rotate (scan-hold is moot).
            if len(self._current_plan) == 1:
                return

            # 5) Scan-hold: if a TX is live on the current cluster, latch here
            #    instead of hopping.  Returns True when the hop must be
            #    suppressed (held this tick, or just advanced on release/cap).
            if self._scan_hold_enabled and self._is_open is not None:
                if self._step_scan_hold(now):
                    return

            # 6) Multi-cluster: check dwell expiry.
            elapsed = now - self._cluster_dwell_start_ts
            if elapsed >= self._dwell_s:
                next_idx = (self._current_cluster_idx + 1) % len(self._current_plan)
                self._apply_cluster(
                    next_idx, now, from_idx=self._current_cluster_idx
                )

    def _step_scan_hold(self, now: float) -> bool:
        """Evaluate scan-hold for the active cluster.

        Returns True if the normal dwell hop must be suppressed this tick
        (either we are holding, or we just released-and-advanced).  Returns
        False to let normal rotation proceed (no activity, not holding).
        """
        live_ids = set(self._current_plan[self._current_cluster_idx].channel_ids)
        open_ids = {cid for cid in live_ids if self._safe_is_open(cid)}

        if open_ids:
            if not self._hold_active:
                # Engage: freeze rotation, latch on the triggering channel.
                self._hold_active = True
                self._hold_start_ts = now
                self._hold_extended_n = 0
                self._hold_trigger_id = sorted(open_ids)[0]
                self._emit_event(
                    "scan_hold_engaged",
                    cluster_idx=self._current_cluster_idx,
                    channel_id=self._hold_trigger_id,
                )
            else:
                # A new closed→open transition (re-key or another channel in
                # the cluster) extends the hold and bumps the counter.
                if open_ids - self._hold_prev_open_ids:
                    self._hold_extended_n += 1
            self._hold_last_open_ts = now
            self._hold_prev_open_ids = open_ids
            # Cap: bound total time on a constantly-busy cluster.
            if now - self._hold_start_ts >= self._scan_hold_max_sec:
                self._release_hold(now, "max_cap")
                self._advance_cluster(now)
            return True

        # Nothing open right now.
        self._hold_prev_open_ids = set()
        if self._hold_active:
            # Release once the cluster has been quiet for the hang window.
            if now - self._hold_last_open_ts >= self._scan_hold_hang_sec:
                self._release_hold(now, "hang")
                self._advance_cluster(now)
            return True  # still within hang → keep holding (suppress hop)
        return False  # not holding, no activity → normal rotation

    def _safe_is_open(self, cid: str) -> bool:
        try:
            return bool(self._is_open(cid))  # type: ignore[misc]
        except Exception:
            return False

    def _release_hold(self, now: float, reason: str) -> None:
        held_for = now - (self._hold_start_ts or now)
        self._emit_event(
            "scan_hold_released",
            cluster_idx=self._current_cluster_idx,
            held_for_sec=round(held_for, 3),
            extended_n_times=self._hold_extended_n,
            released_reason=reason,
        )
        self._reset_hold()

    def _reset_hold(self) -> None:
        self._hold_active = False
        self._hold_start_ts = None
        self._hold_last_open_ts = None
        self._hold_trigger_id = None
        self._hold_extended_n = 0
        self._hold_prev_open_ids = set()

    def _advance_cluster(self, now: float) -> None:
        next_idx = (self._current_cluster_idx + 1) % len(self._current_plan)
        self._apply_cluster(next_idx, now, from_idx=self._current_cluster_idx)

    # -- internal helpers --------------------------------------------------

    def _recompute_plan(self, now: float) -> None:
        """Pull current channels from the daemon and rebuild the plan."""
        channels = list(self._get_channels())
        try:
            new_plan = plan_clusters(
                channels, self._iq_bw_hz, self._max_clusters
            )
            self._plan_failed_reason = None
            self._plan_needed = None
        except ClusterPlanError as e:
            log.warning("scheduler plan failed: %s", e)
            self._emit_event(
                "scheduler_plan_failed",
                reason=str(e),
                needed=e.needed,
                max_allowed=e.max_allowed,
            )
            self._plan_failed_reason = str(e)
            self._plan_needed = e.needed
            new_plan = []
            # In failed state, park every known channel so we don't leave
            # stale unparked channels emitting hits on the wrong LO.
            stale_ids = self._known_pool_ids | {c.id for c in channels}
            if stale_ids:
                self._park_channels(stale_ids)

        self._current_plan = new_plan
        self._current_cluster_idx = 0
        self._cluster_dwell_start_ts = None
        self._last_recompute_ts = now
        # A new plan invalidates any in-progress hold (channel set changed).
        self._reset_hold()

        # Refresh pool-id memory from the new plan.
        new_pool_ids = set()
        for p in new_plan:
            new_pool_ids.update(p.channel_ids)
        # Park any ids that disappeared from the new plan (channel removed
        # via remove_channel).
        gone = self._known_pool_ids - new_pool_ids
        if gone:
            self._park_channels(gone)
        self._known_pool_ids = new_pool_ids

    def _apply_cluster(
        self,
        idx: int,
        now: float,
        from_idx: Optional[int],
    ) -> None:
        """Retune to cluster ``idx`` and park/unpark accordingly."""
        target = self._current_plan[idx]
        target_ids = set(target.channel_ids)
        all_planned: set[str] = set()
        for p in self._current_plan:
            all_planned.update(p.channel_ids)
        to_park = all_planned - target_ids

        if from_idx is not None and self._cluster_dwell_start_ts is not None:
            dwell_actual = now - self._cluster_dwell_start_ts
            from_center: Optional[float] = self._current_plan[from_idx].center_hz
        else:
            dwell_actual = 0.0
            from_center = None

        # Retune the LO first.  Channels are still parked at this point
        # (or transitioning), so we don't fire spurious hit events on the
        # old LO with the new one already moving.
        try:
            self._retune_to(target.center_hz)
        except Exception:
            log.exception(
                "retune_to(%.6f MHz) failed", target.center_hz / 1e6
            )

        # Park outside-cluster channels first (silences them BEFORE we
        # unpark the new cluster, so there's no momentary collision).
        if to_park:
            self._park_channels(to_park)
        if target_ids:
            self._unpark_channels(target_ids)

        self._current_cluster_idx = idx
        self._cluster_dwell_start_ts = now

        # NOTE: do NOT pass ``ts`` here — the chirp CommandServer's
        # emit_event() already stamps every event with the current
        # wall-clock ts; passing one here triggers
        # ``got multiple values for keyword argument 'ts'``.
        self._emit_event(
            "cluster_hop",
            from_center_hz=from_center,
            to_center_hz=target.center_hz,
            cluster_idx=idx,
            n_clusters=len(self._current_plan),
            dwell_actual_sec=round(dwell_actual, 3),
            live_channel_ids=sorted(target_ids),
            parked_channel_ids=sorted(to_park),
        )


__all__ = ["LoScheduler", "DEFAULT_DWELL_S", "DEFAULT_MAX_CLUSTERS"]
