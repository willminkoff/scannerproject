"""chirp.audio_probe — sample-flow probe that gates the systemd watchdog.

SB6 (2026-06-18).  Fixes the "telemetry-lies" wedge class: twice on
2026-06-18 a chirp daemon reported ``audio_path_health: live`` + active
services + healthy metrics while its audio branch published EXACT -180 dBFS
digital silence at the Icecast mount.  Hits fired, channels opened, the
``chirp_audio_bytes_published_total`` counter kept climbing — yet no audio.

Why the existing signals miss it
--------------------------------
The pre-existing health story is bytes-based: ``icecast_bytes_sent`` /
``chirp_audio_bytes_published_total`` and the UI's ``mount_publishing``
heuristic all watch *bytes per second at the mount*.  But — as
``icecast_sink.py`` itself documents — libmp3lame emits perfectly valid MP3
frames for a constant-ZERO PCM input.  So a wedged branch that feeds the
encoder nothing but zeros still produces a steady byte stream and the mount
stays "green".  **Measuring bytes cannot detect this wedge.**  The only place
the difference is visible is the *PCM amplitude at the sink input*: real audio
(even an open-squelch carrier with just noise floor) is far above zero, while
the wedge is exact digital silence.

What this probe does
--------------------
:class:`AudioFlowProbe` is fed every audio block's peak amplitude by
``IcecastSink.work()`` via :meth:`observe_peak`.  It records the monotonic
timestamp of the last NON-SILENT block.  The systemd watchdog ping is then
gated on :meth:`evaluate`, which combines that timestamp with the hit
detector's ``audio_path_health`` so it never punishes a legitimately quiet
period:

  - ``health != "live"`` (``no_open`` / ``no_live`` / ``all_muted``): silence
    is EXPECTED — nothing is keyed, every channel is parked, or the priority
    gate has muted everything.  Never gate; ping the watchdog normally.
  - ``health == "live"``: a channel is open AND un-muted, so audio SHOULD be
    flowing.  If no non-silent block has been seen for >= ``silence_grace_s``
    (and the path has been continuously "live" that long), the audio branch
    is wedged → withhold the watchdog ping so systemd's ``WatchdogSec=``
    restarts the daemon.

The probe is PURE PYTHON (no gnuradio / numpy import) so its decision logic is
unit-testable in any environment.  ``IcecastSink`` does the one cheap numpy
reduction (``abs(block).max()``) and hands the scalar peak in.

Rollback
--------
Disabled by default.  ``CHIRP_AUDIO_PROBE_ENABLED=1`` turns the gating on;
with it off (or the probe absent) :meth:`evaluate` always returns
``(True, ...)`` — i.e. exactly the pre-probe behaviour of pinging the watchdog
unconditionally.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional


# float32 peak below this counts as "silent".  ~ -80 dBFS (10**(-80/20)).
# The wedge is exact 0.0 (-inf dBFS); a real open-squelch channel passes its
# noise floor at roughly -60..-40 dBFS, well above this.  Anything in the
# (-120, -70) dBFS band works; -80 dBFS keeps a wide margin on both sides.
DEFAULT_SILENCE_EPS = 1e-4

# How long the audio path may report "live" with no non-silent PCM before we
# call it wedged.  Generous on purpose: the failure modes we saw are PERMANENT
# silence, so the risk is false positives, not missed wedges.  Must comfortably
# exceed the longest legitimate live-but-silent gap (e.g. a VAD hangover inside
# a transmission), which is a couple of seconds at most.
DEFAULT_SILENCE_GRACE_S = 10.0

# Health value that means "audio is supposed to be flowing right now".  Matches
# HitDetector._audio_path["audio_path_health"].
_HEALTH_LIVE = "live"


class AudioFlowProbe:
    """Measures real audio sample-flow and decides whether to ping the
    systemd watchdog.  Thread-safe: ``observe_peak`` runs on the GR work
    thread while ``evaluate`` runs on the main loop thread.
    """

    def __init__(
        self,
        enabled: bool = False,
        silence_grace_s: float = DEFAULT_SILENCE_GRACE_S,
        silence_eps: float = DEFAULT_SILENCE_EPS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.enabled = bool(enabled)
        self._grace = float(silence_grace_s)
        self._eps = float(silence_eps)
        self._mono = monotonic
        self._lock = threading.Lock()
        now = self._mono()
        # Last block whose peak exceeded eps.  Seeded to construction time so a
        # branch that NEVER produces audio (the afternoon-ground "silent since
        # migration" case) is flagged once it goes live for the grace window.
        self._last_flow = now
        # When the path most recently ENTERED the live state.  Used so a freshly
        # opened channel that has not yet had a block reach the probe is not
        # flagged before the grace window — we require both "no flow for grace"
        # AND "live for grace".
        self._live_since: Optional[float] = None
        # Diagnostics surfaced via snapshot() / get_status.
        self._blocks = 0
        self._nonsilent_blocks = 0
        self._last_peak = 0.0

    # -- producer side (GR work thread) ------------------------------------

    def observe_peak(self, peak: float) -> None:
        """Record one audio block's peak |sample| (float32 domain).

        Called from ``IcecastSink.work()`` once per block.  Cheap; no-op when
        the probe is disabled so the encode path pays nothing in the default
        configuration.
        """
        if not self.enabled:
            return
        with self._lock:
            self._blocks += 1
            self._last_peak = float(peak)
            if peak > self._eps:
                self._nonsilent_blocks += 1
                self._last_flow = self._mono()

    # -- consumer side (main loop thread) ----------------------------------

    def evaluate(self, now: float, health: str) -> tuple[bool, str]:
        """Decide whether to ping ``WATCHDOG=1`` this cycle.

        Returns ``(should_ping, status)``.  ``now`` MUST be on the same clock
        the probe was constructed with (``time.monotonic()`` in production).
        """
        if not self.enabled:
            return True, "probe_disabled"

        with self._lock:
            gap = now - self._last_flow
            if health == _HEALTH_LIVE:
                if self._live_since is None:
                    self._live_since = now
                live_for = now - self._live_since
            else:
                # Not live → silence is expected.  Reset the live timer so the
                # next live spell measures its own grace window from scratch.
                self._live_since = None
                live_for = 0.0

        if health != _HEALTH_LIVE:
            return True, f"flow_ok health={health} gap={gap:.1f}s"

        # health == live: audio SHOULD be flowing.
        if gap >= self._grace and live_for >= self._grace:
            return (
                False,
                f"audio_branch_silent health=live gap={gap:.1f}s "
                f"live_for={live_for:.1f}s grace={self._grace:.0f}s",
            )
        return True, f"flow_ok health=live gap={gap:.1f}s live_for={live_for:.1f}s"

    # -- introspection -----------------------------------------------------

    def snapshot(self) -> dict:
        """Diagnostic snapshot for get_status / the /metrics collector."""
        with self._lock:
            now = self._mono()
            return {
                "enabled": self.enabled,
                "silence_grace_s": self._grace,
                "silence_eps": self._eps,
                "blocks_observed": self._blocks,
                "nonsilent_blocks": self._nonsilent_blocks,
                "last_peak": round(self._last_peak, 6),
                "seconds_since_flow": round(now - self._last_flow, 2),
            }


__all__ = [
    "AudioFlowProbe",
    "DEFAULT_SILENCE_EPS",
    "DEFAULT_SILENCE_GRACE_S",
]
