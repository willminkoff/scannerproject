"""chirp.dsp.cluster_planner — group channels into LO clusters.

Phase 4-pre.  rtl-airband solves the "channels span much more than one IQ
window" problem with ``mode = scan`` — it retunes the LO across the entire
band, demodulating one frequency at a time.  chirp needs the equivalent at
its higher-level slot granularity: pick 2-3 LO centers, pack channels into
those LO windows, and have the scheduler (chirp.dsp.lo_scheduler) round-
robin between them.

This module is the **planner** half of that design: a pure function that
takes the channel list, the IQ window width, and a cluster cap, and returns
a list of :class:`ClusterPlan` records.  No GR / no asyncio / no I/O — easy
to test and easy to call from the daemon on every add/remove.

Algorithm
---------

Greedy 1D bin packing on the freq axis (optimal for unit-length intervals
on a sorted list):

1. Sort channels ascending by ``freq_hz``.
2. Walk left to right.  Each cluster starts at the first un-claimed channel.
   Keep extending the cluster while
   ``(next_channel.freq_hz - cluster.min_freq_hz) <= iq_bw_hz``.
3. When the extension would push the cluster wider than ``iq_bw_hz``, close
   the current cluster and start a new one at the next channel.
4. Center each cluster at the midpoint of its min/max channel.  All channels
   in the cluster are guaranteed to be within ``±iq_bw_hz/2`` of the
   center (this is enforced and asserted at the end).

The cluster count this produces is the **minimum** count needed at the
given ``iq_bw_hz``.  If that minimum exceeds ``max_clusters``, the function
raises :class:`ClusterPlanError` with a clear message naming the count and
the constraint.

Priority field
--------------

Every channel can carry an optional ``recent_hits`` score (typically a
rolling sum of recent hit-log activity for that channel).  The cluster's
``priority`` is the sum of its channels' scores.  The scheduler can read
this to weight dwell time toward busier clusters (Phase 5).  In Phase 4-pre
the scheduler is round-robin, so ``priority`` is informational.

Edge cases
----------

* Empty channel list → ``[]``.
* Single channel → one cluster centered exactly on it.
* ``iq_bw_hz <= 0`` → :class:`ValueError`.
* ``max_clusters < 1`` → :class:`ValueError`.
* Greedy fit count > ``max_clusters`` → :class:`ClusterPlanError`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence


# Default channel-id type — strings, since the daemon's _Slot.user_id is str.
ChannelId = str


@dataclass(frozen=True)
class PlanChannel:
    """A channel as the planner sees it.

    Only three fields matter to the algorithm:
        id: stable channel identifier (matches daemon ``_Slot.user_id``).
        freq_hz: absolute RF frequency in Hz.
        recent_hits: optional activity score (sum of recent hits or any
            non-negative number — the planner just sums it).
    """

    id: ChannelId
    freq_hz: float
    recent_hits: float = 0.0


@dataclass(frozen=True)
class ClusterPlan:
    """A planned LO cluster.

    Fields:
        center_hz: LO retune target; the source is asked to tune here.
        channel_ids: ids of channels that are LIVE while this cluster is
            the active LO target.  All other channels are parked.
        priority: sum of ``recent_hits`` for ``channel_ids``.  Higher =
            this cluster has been busier recently.
        min_freq_hz / max_freq_hz: bounds of the channels in the cluster.
            ``max - min <= iq_bw_hz`` is invariant.
    """

    center_hz: float
    channel_ids: tuple[ChannelId, ...]
    priority: float
    min_freq_hz: float
    max_freq_hz: float

    def to_dict(self) -> dict:
        return {
            "center_hz": self.center_hz,
            "center_mhz": self.center_hz / 1e6,
            "channel_ids": list(self.channel_ids),
            "priority": self.priority,
            "min_freq_hz": self.min_freq_hz,
            "max_freq_hz": self.max_freq_hz,
            "span_hz": self.max_freq_hz - self.min_freq_hz,
            "n_channels": len(self.channel_ids),
        }


class ClusterPlanError(ValueError):
    """Greedy fit needs more clusters than ``max_clusters`` allows.

    The exception's ``needed`` attribute carries the count the greedy fit
    actually produced, so the caller can surface a "bump CHIRP_LO_MAX_CLUSTERS
    to N" hint to operators.
    """

    def __init__(self, message: str, needed: int, max_allowed: int) -> None:
        super().__init__(message)
        self.needed = needed
        self.max_allowed = max_allowed


def plan_clusters(
    channels: Sequence[PlanChannel] | Iterable[PlanChannel],
    iq_bw_hz: float,
    max_clusters: int = 3,
) -> list[ClusterPlan]:
    """Pack ``channels`` into clusters of width ``iq_bw_hz`` (greedy 1D).

    Args:
        channels: PlanChannel records.  Order does not matter; we sort.
        iq_bw_hz: usable IQ window width (Hz).  Each cluster's
            ``max - min`` must be ``<= iq_bw_hz``.
        max_clusters: hard upper bound on cluster count.  Raises if the
            greedy fit produces more.

    Returns:
        list of :class:`ClusterPlan`, ordered by ascending ``center_hz``.

    Raises:
        ValueError: bad ``iq_bw_hz`` or ``max_clusters``.
        ClusterPlanError: greedy fit > ``max_clusters``.
    """
    if iq_bw_hz <= 0:
        raise ValueError(f"iq_bw_hz must be > 0, got {iq_bw_hz!r}")
    if max_clusters < 1:
        raise ValueError(f"max_clusters must be >= 1, got {max_clusters!r}")

    ch_list = list(channels)
    if not ch_list:
        return []

    # Detect duplicate ids early — the daemon's _by_id mapping already
    # prevents this, but a bad caller could trigger silent priority double-
    # counting.  Fail loudly.
    seen: set[ChannelId] = set()
    for c in ch_list:
        if c.id in seen:
            raise ValueError(f"duplicate channel id in plan input: {c.id!r}")
        seen.add(c.id)

    sorted_ch = sorted(ch_list, key=lambda c: c.freq_hz)

    # Greedy bin packing.
    groups: list[list[PlanChannel]] = []
    current: list[PlanChannel] = [sorted_ch[0]]
    for ch in sorted_ch[1:]:
        if (ch.freq_hz - current[0].freq_hz) <= iq_bw_hz:
            current.append(ch)
        else:
            groups.append(current)
            current = [ch]
    groups.append(current)

    if len(groups) > max_clusters:
        # Sketch the centers we WOULD have used, so the operator can decide
        # whether to bump max_clusters or trim the channel list.
        would_be_centers_mhz = [
            (g[0].freq_hz + g[-1].freq_hz) / 2.0 / 1e6 for g in groups
        ]
        raise ClusterPlanError(
            (
                f"cluster planner: greedy fit needs {len(groups)} clusters at "
                f"iq_bw_hz={iq_bw_hz:.0f} but max_clusters={max_clusters}. "
                f"Would-be centers (MHz): "
                f"{', '.join(f'{c:.3f}' for c in would_be_centers_mhz)}. "
                f"Either raise CHIRP_LO_MAX_CLUSTERS or trim the channel list."
            ),
            needed=len(groups),
            max_allowed=max_clusters,
        )

    plans: list[ClusterPlan] = []
    for g in groups:
        lo = g[0].freq_hz
        hi = g[-1].freq_hz
        span = hi - lo
        # Invariant: each channel within ±iq_bw_hz/2 of center.
        # By choosing center at midpoint, max offset is span/2 <= iq_bw_hz/2.
        assert span <= iq_bw_hz + 1e-6, (
            f"internal: cluster span {span} > iq_bw_hz {iq_bw_hz} "
            f"(channels: {[c.id for c in g]})"
        )
        center = (lo + hi) / 2.0
        priority = sum(c.recent_hits for c in g)
        plans.append(ClusterPlan(
            center_hz=center,
            channel_ids=tuple(c.id for c in g),
            priority=float(priority),
            min_freq_hz=lo,
            max_freq_hz=hi,
        ))
    return plans


__all__ = [
    "PlanChannel",
    "ClusterPlan",
    "ClusterPlanError",
    "plan_clusters",
]
