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
    ) -> None:
        if iq_bw_hz <= 0:
            raise ValueError(f"iq_bw_hz must be > 0, got {iq_bw_hz!r}")
        if dwell_s <= 0:
            raise ValueError(f"dwell_s must be > 0, got {dwell_s!r}")
        if max_clusters < 1:
            raise ValueError(f"max_clusters must be >= 1, got {max_clusters!r}")
        if tick_s <= 0:
            raise ValueError(f"tick_s must be > 0, got {tick_s!r}")

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

        self._lock = threading.RLock()
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
        """
        if now is None:
            now = self._clock()
        with self._lock:
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

            # 4) Single-cluster plans don't rotate.
            if len(self._current_plan) == 1:
                return

            # 5) Multi-cluster: check dwell expiry.
            elapsed = now - self._cluster_dwell_start_ts
            if elapsed >= self._dwell_s:
                next_idx = (self._current_cluster_idx + 1) % len(self._current_plan)
                self._apply_cluster(
                    next_idx, now, from_idx=self._current_cluster_idx
                )

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

        self._emit_event(
            "cluster_hop",
            from_center_hz=from_center,
            to_center_hz=target.center_hz,
            cluster_idx=idx,
            n_clusters=len(self._current_plan),
            dwell_actual_sec=round(dwell_actual, 3),
            live_channel_ids=sorted(target_ids),
            parked_channel_ids=sorted(to_park),
            ts=now,
        )


__all__ = ["LoScheduler", "DEFAULT_DWELL_S", "DEFAULT_MAX_CLUSTERS"]
