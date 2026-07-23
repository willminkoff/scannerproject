"""sb3.reconciler.safety — the brakes.  Every one of them is mandatory.

Phase 4.1 watched.  Phase 4.2 acts, and everything in this file exists to bound
what "acts" can mean.  The design rule throughout: **a brake must fail toward
doing nothing.**  An unreadable sentinel, an unknown REST path, an
indeterminate mount, a counter that cannot be trusted — all of them resolve to
"skip this pass", never to "proceed and hope".

Five independent brakes, deliberately not sharing a failure mode:

  1. :class:`PathAuditor`    — a REST path allowlist, enforced at RUNTIME on the
     calls an action actually made.  This is trunk protection: SDRangel's REST
     base cannot reach SDRTrunk, icecast, the bridges or the apiService, and any
     path that tries to escape that base is an emergency, not a warning.
  2. :class:`BackendGuard`   — PID sampling around every action.  The auditor
     proves we did not *ask* for something forbidden; this proves nothing
     forbidden *moved* regardless of what we asked.
  3. :class:`RateLimiter`    — exponential backoff per role, so a role that
     cannot be fixed is retried 30s, 60s, 120s, 240s and then left alone with an
     alarm instead of being hammered every 30 s all night.
  4. :class:`FailureCounter` — quarantine per (role, action) after N consecutive
     failures, cleared only by a human.  Backoff limits the RATE of a doomed
     retry; quarantine ends it.
  5. broken-state pause      — if SDRangel REST is unreachable or a mount status
     is indeterminate, take no action at all this pass.  Acting on a reading you
     could not take is how a reconciler makes an outage worse.

None of these can be disabled from the config file.  The knobs tune thresholds;
they cannot remove a brake.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 1. Path allowlist — trunk protection, enforced at runtime
# ---------------------------------------------------------------------------

#: The ONLY REST paths any reconciler action may touch, all relative to
#: SDRangel's base (http://127.0.0.1:8091/sdrangel).
#:
#: `/deviceset/*` is the analog roles' devicesets and channels.  `/audio*` is
#: the copyToUDP tap that feeds the ffmpeg bridge — SDRangel's own audio output
#: settings, NOT the bridge or icecast.  The empty path is GET / (liveness).
#:
#: SDRTrunk has no REST surface here at all, the apiService is a shm IPC with no
#: HTTP endpoint, and icecast lives on :8000 — so the base URL alone already
#: makes them unreachable.  This allowlist is the second lock on that door.
ALLOWED_PATH_RE = re.compile(r"^(|/deviceset/\d+(/.*)?|/audio(/.*)?)$")

#: SDRangel's REST base. A client pointed anywhere else is an emergency.
EXPECTED_BASE = "http://127.0.0.1:8091/sdrangel"


class SafetyViolation(Exception):
    """A brake tripped in a way that must stop the reconciler, loudly."""


class PathAuditor:
    """Checks the REST calls an action actually made against the allowlist."""

    @staticmethod
    def audit(calls: Sequence[Tuple[str, str, Optional[dict]]],
              base: str = EXPECTED_BASE) -> List[str]:
        """Return a list of violations. Empty list = clean.

        Takes the recorded calls (SDRangelClient.calls) rather than trusting the
        code that produced them — an action that grew a new endpoint is caught
        by what it DID, not by what it was reviewed to do.
        """
        problems: List[str] = []
        if base != EXPECTED_BASE:
            problems.append(f"client base {base!r} != SDRangel base {EXPECTED_BASE!r}")
        for method, path, _body in calls:
            # clear_channels records a human-readable pseudo-path in dry-run;
            # normalise the annotated form before matching.
            probe = path.split("  ")[0].strip()
            if not ALLOWED_PATH_RE.match(probe):
                problems.append(f"{method} {path} — outside the SDRangel allowlist")
        return problems


# ---------------------------------------------------------------------------
# 2. Backend PID guard
# ---------------------------------------------------------------------------

class BackendGuard:
    """Samples protected backend PIDs around an action and pauses on movement.

    SDRangel is intentionally NOT protected here: the reconciler drives it, and
    a legitimate rebind is allowed to churn it.  SDRTrunk, icecast, the two
    ffmpeg bridges and sdrplay_apiService must be byte-identical before and
    after, always.
    """

    def __init__(self, pause_seconds: float = 300.0, clock=time.monotonic) -> None:
        self.pause_seconds = float(pause_seconds)
        self._clock = clock
        self._paused_until: Optional[float] = None
        self.last_violation: Optional[str] = None

    def paused(self) -> bool:
        if self._paused_until is None:
            return False
        if self._clock() >= self._paused_until:
            self._paused_until = None
            return False
        return True

    def pause_remaining(self) -> float:
        if self._paused_until is None:
            return 0.0
        return max(0.0, self._paused_until - self._clock())

    def compare(self, before: Dict[str, Optional[str]],
                after: Dict[str, Optional[str]]) -> List[str]:
        """Return the names of protected processes whose PID moved."""
        moved = []
        for name, pid in sorted(before.items()):
            if after.get(name) != pid:
                moved.append(f"{name}:{pid}→{after.get(name)}")
        return moved

    def trip(self, moved: Sequence[str]) -> str:
        """Enter the emergency pause. Returns the message to log."""
        self._paused_until = self._clock() + self.pause_seconds
        self.last_violation = ",".join(moved)
        return (f"EMERGENCY backend_pid_moved={self.last_violation} "
                f"pausing={self.pause_seconds:g}s")


# ---------------------------------------------------------------------------
# 3. Rate limiter — exponential backoff per role
# ---------------------------------------------------------------------------

class RateLimiter:
    """30s → 60s → 120s → 240s, then alarm and stop retrying that role.

    Keyed by ROLE, not by action: a role that needs three different fixes on
    consecutive passes is still a role that is not converging, and hammering it
    with a different action each 30 s is the same pathology as repeating one.
    """

    def __init__(self, base: float = 30.0, cap: float = 240.0,
                 clock=time.monotonic) -> None:
        self.base = float(base)
        self.cap = float(cap)
        self._clock = clock
        self._streak: Dict[str, int] = {}
        self._next_allowed: Dict[str, float] = {}
        self.alarmed: Dict[str, bool] = {}

    def backoff_for(self, streak: int) -> float:
        """Delay after `streak` consecutive acting passes. 1→30, 2→60, 3→120…"""
        if streak <= 0:
            return 0.0
        return min(self.base * (2 ** (streak - 1)), self.cap)

    def allowed(self, role: str) -> bool:
        """May this role be acted on right now?"""
        if self.alarmed.get(role):
            return False
        nxt = self._next_allowed.get(role)
        return nxt is None or self._clock() >= nxt

    def at_cap(self, role: str) -> bool:
        return self.backoff_for(self._streak.get(role, 0)) >= self.cap

    def record_action(self, role: str) -> float:
        """Note that we just acted on `role`; returns the new backoff."""
        streak = self._streak.get(role, 0) + 1
        self._streak[role] = streak
        delay = self.backoff_for(streak)
        self._next_allowed[role] = self._clock() + delay
        # Past the cap the role is not converging. Stop retrying and say so;
        # an action repeated every 4 minutes forever is an outage nobody reads.
        if streak > 4:
            self.alarmed[role] = True
        return delay

    def record_clean(self, role: str) -> None:
        """A pass where the role needed nothing — reset its backoff."""
        self._streak.pop(role, None)
        self._next_allowed.pop(role, None)
        self.alarmed.pop(role, None)

    def streak(self, role: str) -> int:
        return self._streak.get(role, 0)


# ---------------------------------------------------------------------------
# 4. Failure counter — quarantine per (role, action)
# ---------------------------------------------------------------------------

class FailureCounter:
    """Quarantines a (role, action) pair after N consecutive failures.

    Cleared only by ``sb3-ctl reconciler resume-action <role> <action>``.  An
    action that has failed three times running is not going to succeed on the
    fourth for the same reason, and the fourth attempt is indistinguishable from
    the first to anyone reading the log.
    """

    def __init__(self, threshold: int = 3,
                 path: Optional["Path"] = None) -> None:
        self.threshold = int(threshold)
        self._fails: Dict[Tuple[str, str], int] = {}
        self._quarantined: Dict[Tuple[str, str], str] = {}
        # Quarantine is PERSISTED. It has to be: the agent quarantines, and a
        # human clears it later from a different process via `sb3-ctl reconciler
        # resume-action`. In-memory-only state would make that command a no-op
        # and the quarantine would silently evaporate on the next agent restart
        # — turning "stop trying, a human must look" into "retry forever".
        self.path = Path(os.path.expanduser(str(path))) if path else None
        if self.path:
            self.load()

    def key(self, role: str, action: str) -> Tuple[str, str]:
        return (role, action)

    # -- persistence -------------------------------------------------------

    def load(self) -> None:
        if not self.path:
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        q = data.get("quarantined") or {}
        if isinstance(q, dict):
            self._quarantined = {tuple(k.split("|", 1)): v            # type: ignore[misc]
                                 for k, v in q.items() if "|" in k}

    def save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"quarantined": {f"{r}|{a}": why
                                       for (r, a), why in self._quarantined.items()}}
            self.path.write_text(json.dumps(payload, indent=2) + "\n")
        except OSError:
            pass    # a lost quarantine file must not crash the loop

    def quarantined(self, role: str, action: str) -> bool:
        return self.key(role, action) in self._quarantined

    def record_failure(self, role: str, action: str, detail: str = "") -> bool:
        """Count a failure. Returns True if this one triggered quarantine."""
        k = self.key(role, action)
        n = self._fails.get(k, 0) + 1
        self._fails[k] = n
        if n >= self.threshold and k not in self._quarantined:
            self._quarantined[k] = detail or f"{n} consecutive failures"
            self.save()
            return True
        return False

    def record_success(self, role: str, action: str) -> None:
        self._fails.pop(self.key(role, action), None)

    def failures(self, role: str, action: str) -> int:
        return self._fails.get(self.key(role, action), 0)

    def release(self, role: str, action: str) -> bool:
        """Manual un-quarantine. Returns True if something was released."""
        k = self.key(role, action)
        had = k in self._quarantined
        self._quarantined.pop(k, None)
        self._fails.pop(k, None)
        self.save()
        return had

    def list_quarantined(self) -> List[Tuple[str, str, str]]:
        return sorted((r, a, why) for (r, a), why in self._quarantined.items())


# ---------------------------------------------------------------------------
# 5. Broken-state pause
# ---------------------------------------------------------------------------

def readings_trustworthy(*, sdrangel_reachable: bool,
                         mount_status: Optional[int]) -> Tuple[bool, str]:
    """May we act on this pass at all?  (ok, reason-if-not).

    Two ways a reading is untrustworthy, and both mean skip:

      * SDRangel REST unreachable — every deviceset read comes back empty, so
        "phantom deviceset" and "missing channel" would be inferred from an
        absence of data rather than from data showing absence.
      * mount status None — the mount probe could not complete, so
        "mount_404_with_healthy_backend" cannot be distinguished from "we could
        not tell".  Toggling a live tap on a guess would break working audio.
    """
    if not sdrangel_reachable:
        return False, "sdrangel_rest_unreachable"
    if mount_status is None:
        return False, "mount_status_indeterminate"
    return True, ""


# ---------------------------------------------------------------------------
# shared-tap de-duplication
# ---------------------------------------------------------------------------

def dedupe_tap_actions(planned: Sequence[Tuple[str, str, object]]
                       ) -> Tuple[List[Tuple[str, str, object]], List[str]]:
    """Collapse concurrent copyToUDP fixes that target the SAME tap.

    Air (DS0) and VFO (DS1) SHARE neptune-analog.mp3: both route audio to
    SDRangel's idx -1 output and both feed :9998.  When that mount goes dark
    BOTH roles independently classify mount_absent, and both would toggle the
    same tap in the same pass — the second toggle stopping the sender the first
    just started.  Keep the first, drop the rest, and say which were dropped.

    `planned` is [(role, action, ctx)] where ctx exposes .tap_key.
    """
    kept: List[Tuple[str, str, object]] = []
    dropped: List[str] = []
    seen = set()
    for role, action, ctx in planned:
        if action != "mount_404_with_healthy_backend":
            kept.append((role, action, ctx))
            continue
        tap = getattr(ctx, "tap_key", None)
        if tap is not None and tap in seen:
            dropped.append(role)
            continue
        if tap is not None:
            seen.add(tap)
        kept.append((role, action, ctx))
    return kept, dropped
