"""sb3.reconciler.classifier — drift categories, as pure functions.

This module decides WHAT KIND of difference exists between the profile SB3
recorded as loaded and what the backends are actually doing right now.  It has
no I/O: it takes plain observation data in and returns a classification out, so
every category is unit-testable without a live box.  The observer does the
reading; this file does the judging.

**Phase 4.1 is log-only.**  Nothing here acts, and nothing here tells anything
else to act.  The point of the overnight run is to find out what drift on this
box actually looks like *before* granting anything the right to correct it — the
inverse of ``sdrangel-restore.py``, which re-asserts a stored config every 10
minutes and would clobber a human mid-tune.

The four categories, and the question each answers:

  CLEAN        Live state matches the profile.  Nothing to say.
  BENIGN       A human is driving.  Squelch, volume and tuning are the controls
               the UI exposes; finding them changed is the system working, not
               failing.  §4.4: on divergence the LIVE backend wins.  LEAVE ALONE.
  RECOVERABLE  The profile is not live — a phantom deviceset, a missing channel,
               a dead mount behind a healthy backend.  Re-applying the profile
               would plausibly fix it.  A LATER phase may act; 4.1 only logs.
  BROKEN       The backend itself is not answering — REST down, every mount
               dark, backend processes churning.  Re-applying a profile would
               not help; something needs a restart.  4.1 only logs.

Severity is ordered and the most severe finding wins, because a pass that is
simultaneously "user retuned the VFO" and "DS0 is phantom" is not a BENIGN pass.
"""

from __future__ import annotations

from typing import Dict, List, NamedTuple, Optional, Sequence

# -- the categories ---------------------------------------------------------

CLEAN = "CLEAN"
BENIGN = "BENIGN"
RECOVERABLE = "RECOVERABLE"
BROKEN = "BROKEN"

#: Most severe wins when one pass turns up findings in several categories.
SEVERITY: Dict[str, int] = {CLEAN: 0, BENIGN: 1, RECOVERABLE: 2, BROKEN: 3}


# -- thresholds -------------------------------------------------------------
#
# Named, not inline, because these ARE the policy: they draw the line between
# "the user is tuning" and "this drifted somewhere a human would not have put
# it".  Tuning them is a deliberate decision, reviewable in one place.

#: Float compare epsilon.  SDRangel round-trips settings through float32, so a
#: volume written as 0.4 reads back 0.4000000059604645.  Comparing those with
#: == would report drift on a channel nobody has touched — a permanent false
#: BENIGN on every pass, which is how a log becomes noise nobody reads.
EPS = 1e-3

#: Squelch drift beyond this is no longer "a user nudged the slider".  The band
#: is wide on purpose: the UI squelch slider spans roughly -100..0 dBFS and
#: normal hunting moves it tens of dB.  Beyond 30 dB, though, the observed
#: failure mode is real — a slider dragged to -80, below the ~-78 noise floor,
#: produces 100+ phantom hits/min and a dead-silent mount while every process
#: reports healthy.  That is worth separating from ordinary tuning.
SQUELCH_BENIGN_MAX_DELTA_DB = 30.0

#: Legal squelch range; a value outside it did not come from the UI.
SQUELCH_MIN_DB, SQUELCH_MAX_DB = -140.0, 20.0

#: Volume drift is judged as a RATIO, not a delta — "same order of magnitude"
#: per the 4.1 brief.  0.4 -> 3.0 is a user turning it up; 3.0 -> 0.0 is not a
#: volume change, it is a channel that stopped producing audio.
VOLUME_BENIGN_MIN_RATIO = 0.1
VOLUME_BENIGN_MAX_RATIO = 10.0

#: A keepalive channel is the always-open one holding the mount up, so its
#: squelch is driven to the floor deliberately.  Anything above this means the
#: keepalive is no longer keeping anything alive.
KEEPALIVE_MAX_SQUELCH_DB = -80.0


# -- data in ----------------------------------------------------------------

class ExpectedChannel(NamedTuple):
    """One channel as the profile specifies it, in APPLY order."""

    offset_hz: int          # freq_hz - center_freq_hz, as translator sets it
    squelch_db: float
    volume: float
    rf_bw_hz: int
    keepalive: bool = False
    title: str = ""


class LiveChannel(NamedTuple):
    """One channel as SDRangel currently reports it."""

    index: int
    offset_hz: Optional[int]
    squelch_db: Optional[float]
    volume: Optional[float]
    rf_bw_hz: Optional[int] = None


class RoleObservation(NamedTuple):
    """Everything one analog role's classification needs. Plain data."""

    role: str
    profile_name: str
    deviceset_index: int
    mode: str                       # "camp" | "hunt"
    # expected
    want_serial: Optional[str]
    want_center_hz: Optional[int]
    want_mount: str
    expected_channels: Sequence[ExpectedChannel]
    # live
    sdrangel_reachable: bool
    ds_present: bool
    ds_phantom: bool
    ds_hw_type: Optional[str]
    ds_serial: Optional[str]
    ds_state: Optional[str]         # "running" | "idle" | ...
    ds_center_hz: Optional[int]
    live_channels: Sequence[LiveChannel]
    mount_status: Optional[int]     # HTTP status of want_mount, None=unreachable
    mount_present: bool


class DigitalObservation(NamedTuple):
    """The digital role lives in SDRTrunk, not SDRangel — different evidence."""

    profile_name: str
    running: bool                   # launchd says the agent is loaded
    mount_status: Optional[int]
    mount_present: bool
    connected: Optional[bool] = None    # SDRTrunk broadcaster "Connected"
    tuners: Sequence[str] = ()


# -- data out ---------------------------------------------------------------

class Classification(NamedTuple):
    state: str
    issues: List[str]        # RECOVERABLE/BROKEN reasons, e.g. "phantom_deviceset"
    drifts: List[str]        # BENIGN details, e.g. "ch0.squelch:-55→-42"
    facts: Dict[str, str]    # extra key=value context for the log line
    #: Channel offsets the profile expects that are NOT live. Phase 4.2's
    #: missing-channel repair re-adds exactly these. Populated ONLY when the
    #: live channel COUNT is short — see the note in classify_role.
    missing_offsets: Sequence[int] = ()

    @property
    def is_clean(self) -> bool:
        return self.state == CLEAN


def _worst(*states: str) -> str:
    return max(states, key=lambda s: SEVERITY.get(s, 0))


def _fmt_num(v) -> str:
    """Compact number formatting for log lines: 3.0 -> '3', -42.5 -> '-42.5'."""
    if v is None:
        return "?"
    f = float(v)
    return str(int(f)) if abs(f - round(f)) < EPS else f"{f:g}"


def _drift(label: str, want, live) -> str:
    return f"{label}:{_fmt_num(want)}→{_fmt_num(live)}"


# -- channel-level comparison ----------------------------------------------

def classify_channel(want: ExpectedChannel, live: LiveChannel,
                     idx: int) -> Classification:
    """Compare one expected channel against its live counterpart.

    Deliberately generous: squelch/volume/tuning are the three controls the UI
    exposes, so finding them changed is evidence the system is being used, not
    evidence it is broken.  Only drift far outside the band a human would dial
    escalates.
    """
    issues: List[str] = []
    drifts: List[str] = []
    state = CLEAN

    # --- tuning: the user moved this channel. Always benign; it is intent.
    if (want.offset_hz is not None and live.offset_hz is not None
            and want.offset_hz != live.offset_hz):
        drifts.append(_drift(f"ch{idx}.offset", want.offset_hz, live.offset_hz))
        state = _worst(state, BENIGN)

    # --- squelch
    if live.squelch_db is None:
        issues.append(f"ch{idx}_squelch_unreadable")
        state = _worst(state, RECOVERABLE)
    elif abs(float(live.squelch_db) - float(want.squelch_db)) > EPS:
        delta = abs(float(live.squelch_db) - float(want.squelch_db))
        out_of_range = not (SQUELCH_MIN_DB <= float(live.squelch_db) <= SQUELCH_MAX_DB)
        if out_of_range or delta > SQUELCH_BENIGN_MAX_DELTA_DB:
            issues.append(f"ch{idx}_squelch_out_of_band")
            drifts.append(_drift(f"ch{idx}.squelch", want.squelch_db, live.squelch_db))
            state = _worst(state, RECOVERABLE)
        else:
            drifts.append(_drift(f"ch{idx}.squelch", want.squelch_db, live.squelch_db))
            state = _worst(state, BENIGN)

    # --- a keepalive that stopped keeping alive is structural, not taste
    if want.keepalive and live.squelch_db is not None:
        if float(live.squelch_db) > KEEPALIVE_MAX_SQUELCH_DB:
            issues.append(f"ch{idx}_keepalive_squelch_raised")
            state = _worst(state, RECOVERABLE)

    # --- volume, judged by ratio ("same order of magnitude")
    if live.volume is None:
        issues.append(f"ch{idx}_volume_unreadable")
        state = _worst(state, RECOVERABLE)
    elif abs(float(live.volume) - float(want.volume)) > EPS:
        lv, wv = float(live.volume), float(want.volume)
        drifts.append(_drift(f"ch{idx}.volume", wv, lv))
        if lv <= 0.0 < wv:
            # Not a volume change — a channel that went silent.
            issues.append(f"ch{idx}_volume_zero")
            state = _worst(state, RECOVERABLE)
        elif wv > 0:
            ratio = lv / wv
            state = _worst(state, BENIGN if
                           VOLUME_BENIGN_MIN_RATIO <= ratio <= VOLUME_BENIGN_MAX_RATIO
                           else RECOVERABLE)
            if not (VOLUME_BENIGN_MIN_RATIO <= ratio <= VOLUME_BENIGN_MAX_RATIO):
                issues.append(f"ch{idx}_volume_out_of_band")
        else:
            state = _worst(state, BENIGN)

    return Classification(state, issues, drifts, {})


# -- role-level comparison --------------------------------------------------

def classify_role(obs: RoleObservation) -> Classification:
    """Classify one analog (SDRangel-backed) role. Pure."""
    issues: List[str] = []
    drifts: List[str] = []
    facts: Dict[str, str] = {}

    # --- BROKEN: the backend is not answering at all. Nothing below this can
    #     be trusted, so report it and stop — a phantom-deviceset finding
    #     derived from an empty read would be a guess dressed as a fact.
    if not obs.sdrangel_reachable:
        # No `ds` fact here: format_line already emits ds= from its own
        # argument, and a duplicated key makes the line ambiguous to parse.
        return Classification(BROKEN, ["sdrangel_rest_down"], [], {})

    state = CLEAN

    # --- deviceset structure
    if not obs.ds_present:
        issues.append("deviceset_missing")
        state = _worst(state, RECOVERABLE)
        facts["ds_present"] = "no"
    elif obs.ds_phantom:
        issues.append("phantom_deviceset")
        facts["current_hw"] = str(obs.ds_hw_type)
        state = _worst(state, RECOVERABLE)
    else:
        # WHAT device is bound is structural; WHERE it is tuned is user intent.
        if obs.want_serial and obs.ds_serial != obs.want_serial:
            issues.append("serial_mismatch")
            facts["want_serial"] = str(obs.want_serial)
            facts["live_serial"] = str(obs.ds_serial)
            state = _worst(state, RECOVERABLE)
        if obs.ds_state and obs.ds_state != "running":
            issues.append("deviceset_not_running")
            facts["ds_state"] = str(obs.ds_state)
            state = _worst(state, RECOVERABLE)
        # A moved centre is a human retuning (VFO tune moves the LO by design).
        if (obs.want_center_hz and obs.ds_center_hz
                and obs.want_center_hz != obs.ds_center_hz):
            drifts.append(_drift("center_mhz", obs.want_center_hz / 1e6,
                                 obs.ds_center_hz / 1e6))
            state = _worst(state, BENIGN)

    # --- channels (only meaningful once a real device is bound)
    missing_offsets: List[int] = []
    if obs.ds_present and not obs.ds_phantom:
        n_want, n_live = len(obs.expected_channels), len(obs.live_channels)
        if n_live < n_want:
            issues.append("channel_missing")
            facts["channels"] = f"{n_live}/{n_want}"
            state = _worst(state, RECOVERABLE)
            # WHICH channels are missing — computed by OFFSET, but ONLY when the
            # count is short. That guard is load-bearing: with a full count, an
            # offset that does not match expectation is a human RETUNING a
            # channel (BENIGN, §4.4 the live backend wins). Diffing offsets
            # unconditionally would read that retune as "channel missing" and
            # Phase 4.2 would helpfully undo the user's tuning — the single
            # worst thing an active reconciler could do.
            live_offsets = {ch.offset_hz for ch in obs.live_channels
                            if ch.offset_hz is not None}
            missing_offsets = [c.offset_hz for c in obs.expected_channels
                               if c.offset_hz not in live_offsets]
        elif n_live > n_want:
            # Extra channels are additive, not a failure — a human added one.
            drifts.append(f"channels:{n_want}→{n_live}")
            state = _worst(state, BENIGN)

        for i, want in enumerate(obs.expected_channels):
            if i >= len(obs.live_channels):
                break
            sub = classify_channel(want, obs.live_channels[i], i)
            issues.extend(sub.issues)
            drifts.extend(sub.drifts)
            state = _worst(state, sub.state)

        # keepalive present at all? (camp mode holds the mount open with one)
        # Same count-short guard as above, for the same reason.
        if obs.mode == "camp":
            want_ka = [c for c in obs.expected_channels if c.keepalive]
            if want_ka and n_live < n_want:
                live_offsets = {ch.offset_hz for ch in obs.live_channels
                                if ch.offset_hz is not None}
                if want_ka[0].offset_hz not in live_offsets:
                    issues.append("keepalive_missing")
                    state = _worst(state, RECOVERABLE)

    # --- the mount: audio actually reaching listeners
    if not obs.mount_present:
        # A dark mount behind a live deviceset is the classic silent outage:
        # every process reports healthy and no audio exists.
        issues.append("mount_absent")
        facts["mount"] = f"{obs.want_mount}={obs.mount_status}"
        state = _worst(state, RECOVERABLE)

    return Classification(state, issues, drifts, facts, tuple(missing_offsets))


def classify_digital(obs: DigitalObservation) -> Classification:
    """Classify the digital (SDRTrunk) role. Pure.

    SDRTrunk holds no deviceset in SDRangel, so the evidence is different: is
    the agent loaded, is the broadcaster connected, is the mount live.
    """
    issues: List[str] = []
    facts: Dict[str, str] = {}
    state = CLEAN

    if not obs.running:
        issues.append("sdrtrunk_not_running")
        state = _worst(state, BROKEN)
    if not obs.mount_present:
        issues.append("mount_absent")
        facts["mount_status"] = str(obs.mount_status)
        # A dead trunk mount with the agent up is recoverable; with the agent
        # down it is already BROKEN above.
        state = _worst(state, RECOVERABLE if obs.running else BROKEN)
    if obs.running and obs.connected is False:
        issues.append("broadcaster_disconnected")
        state = _worst(state, RECOVERABLE)
    if obs.tuners:
        facts["tuners"] = ",".join(obs.tuners[:2])

    return Classification(state, issues, [], facts)


def classify_system(*, sdrangel_reachable: bool, mounts_present: Sequence[bool],
                    backend_pids_changed: bool = False,
                    missing_backends: Sequence[str] = ()) -> Classification:
    """Box-level findings that are nobody's role in particular. Pure."""
    issues: List[str] = []
    facts: Dict[str, str] = {}
    state = CLEAN

    if not sdrangel_reachable:
        issues.append("sdrangel_rest_down")
        state = _worst(state, BROKEN)
    if mounts_present and not any(mounts_present):
        issues.append("all_mounts_down")
        state = _worst(state, BROKEN)
    if backend_pids_changed:
        issues.append("backend_pid_churn")
        state = _worst(state, BROKEN)
    if missing_backends:
        issues.append("backend_agent_missing")
        facts["missing"] = ",".join(sorted(missing_backends))
        state = _worst(state, BROKEN)

    return Classification(state, issues, [], facts)


# -- log rendering ----------------------------------------------------------

def format_line(ts: str, cls: Classification, *, role: str = "",
                profile: str = "", ds: Optional[int] = None,
                scope: str = "") -> str:
    """Render one structured log line. Pure — the observer just writes it.

    Shape (stable, greppable, one line per classification):

        ts=… profile=vfo.default role=vfo ds=1 state=BENIGN drift=[ch0.squelch:-55→-42]
        ts=… profile=air…rsp1b role=air ds=0 state=RECOVERABLE issue=phantom_deviceset current_hw=AaroniaRTSA
    """
    parts = [f"ts={ts}"]
    if scope:
        parts.append(f"scope={scope}")
    if profile:
        parts.append(f"profile={profile}")
    if role:
        parts.append(f"role={role}")
    if ds is not None:
        parts.append(f"ds={ds}")
    parts.append(f"state={cls.state}")
    if cls.issues:
        parts.append("issue=" + ",".join(cls.issues))
    if cls.drifts:
        parts.append("drift=[" + " ".join(cls.drifts) + "]")
    for k, v in cls.facts.items():
        parts.append(f"{k}={v}")
    return " ".join(parts)
