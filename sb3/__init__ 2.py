"""sb3 — the SB3 control plane.

SB3 orchestrates SDRangel and SDRTrunk. It does not replace them, and it does
not own the audio path. The load-bearing property, from
docs/sb3-neptune-architecture.md §4.2:

    SB3 never holds audio state. It only *asserts* state onto backends that then
    hold it themselves. Kill the asserter and the assertions stand.

That is why `sb3-ctl kill` is safe to attempt at all — and it is a property to
protect deliberately (sb3/ownership.py), not one to rely on by luck.
"""

__all__ = ["ownership", "backends", "killswitch", "settle", "state"]
