"""chirp.dsp.priority_gate — single-active-channel latch for the scan path.

2026-06-06.  Within an LO cluster several channels can be squelch-open at
once; without arbitration their audio sums in the mixer and the listener
hears overlapping voices.  The priority gate picks exactly ONE open channel
to pass and mutes the rest, so the operator hears one transmission at a time
(Uniden-style "one hit at a time").

Behaviour (latch):
  * The selected channel holds the audio path until **it** closes — a more
    recently opened channel does NOT preempt a live selection.
  * When the selection closes (or at start), the **most-recently-opened** of
    the currently-open channels takes over.
  * When nothing is open, the selection clears → silence (no closed-channel
    noise leaks through).

Pure Python, no GR import — the controller decides *which* channel; the
``Channel.set_priority_muted`` block does the muting.  Trivially unit-testable
with an injected clock.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterable, Optional


class PriorityGate:
    """Most-recently-opened latch over a set of squelch-open channel ids.

    Args:
        emit_event: optional ``(name, **kw) -> None`` — emits
            ``priority_locked`` (with ``channel_id``) when a new channel
            claims the audio path.  None disables telemetry.
        clock: injectable monotonic-ish clock (defaults to ``time.time``).
    """

    def __init__(
        self,
        emit_event: Optional[Callable[..., None]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._emit = emit_event
        self._clock = clock
        # channel id -> timestamp it (re)opened in the current open episode.
        self._open_since: dict[str, float] = {}
        self._selected: Optional[str] = None

    def update(self, open_ids: Iterable[str], now: Optional[float] = None) -> Optional[str]:
        """Recompute the selection from the currently squelch-open ids.

        Returns the selected channel id (or None if nothing is open).
        """
        if now is None:
            now = self._clock()
        open_set = set(open_ids)

        # Maintain per-id open-episode start timestamps.
        for cid in open_set:
            self._open_since.setdefault(cid, now)
        for cid in [c for c in self._open_since if c not in open_set]:
            del self._open_since[cid]

        prev = self._selected
        if self._selected is not None and self._selected in open_set:
            pass  # latch: live selection keeps the path until it closes
        elif open_set:
            # Selection closed (or none yet) → most-recently-opened takes over.
            self._selected = max(open_set, key=lambda c: self._open_since[c])
        else:
            self._selected = None

        if (
            self._selected is not None
            and self._selected != prev
            and self._emit is not None
        ):
            self._emit("priority_locked", channel_id=self._selected)
        return self._selected

    @property
    def selected(self) -> Optional[str]:
        return self._selected

    def reset(self) -> None:
        """Clear all state (e.g. on cluster recompute)."""
        self._open_since.clear()
        self._selected = None


__all__ = ["PriorityGate"]
