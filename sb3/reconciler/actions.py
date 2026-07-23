"""sb3.reconciler.actions — the ONLY module in this package that writes.

Phase 4.1's reconciler could not act at all, and a test enforced that.  4.2
keeps the same shape but concentrates every write into this one file, so the
audit question stays answerable in one place: *what, exactly, is this thing
allowed to do to the radio?*

The answer is five things, one per RECOVERABLE category, all of them through
SDRangel's REST API and nothing else:

  phantom_deviceset              re-apply the whole profile (the deviceset has
                                 no real device bound; there is nothing to patch)
  missing_channel                add back one channel the profile expects
  missing_keepalive              add back the always-open channel
  unbound_device / idle          POST device/run
  mount_404_with_healthy_backend re-toggle copyToUDP 0→1 on the tap

Everything here is deliberately *surgical* except the phantom case.  A profile
re-apply clears and rebuilds every channel and re-arms the tap; doing that for a
single missing channel would take a working mount down for several seconds to
fix a problem that one POST fixes.  The phantom case has no cheaper option — a
deviceset with ``serial=None`` has nothing to patch onto.

Every action returns an :class:`ActionResult` carrying the REST calls it made,
so :mod:`sb3.reconciler.safety` can audit what actually happened rather than
trusting what this file was reviewed to do.
"""

from __future__ import annotations

from typing import Callable, List, NamedTuple, Optional, Sequence, Tuple

from .. import backends
from ..profile import Profile
from ..sdrangel import SDRangelClient
from ..translator import _channel_settings

Emit = Callable[[str], None]


class ActionResult(NamedTuple):
    ok: bool
    action: str
    detail: str
    calls: Sequence[Tuple[str, str, Optional[dict]]] = ()
    base: str = ""


class ActionContext(NamedTuple):
    """Everything an action needs. Built by the observer, never by an action."""

    role: str
    action: str
    profile: Optional[Profile]        # None when the profile file is unreadable
    deviceset_index: int
    mount: str
    dry_run: bool
    # for missing-channel / keepalive repairs
    missing_offsets: Sequence[int] = ()
    audio_name: str = ""
    audio_index: int = 0
    udp_address: str = "127.0.0.1"
    udp_port: int = 9998

    @property
    def tap_key(self):
        """Identifies the shared copyToUDP tap, for de-duplication.

        Air and VFO share one tap (idx -1 → :9998); two roles must not toggle
        it in the same pass.
        """
        return (self.audio_index, self.udp_port)


def _client(ctx: ActionContext, emit: Emit) -> SDRangelClient:
    """Build the REST client.

    dry_run maps onto the client's own ``execute`` flag, so a dry run takes the
    IDENTICAL code path and records every call it would have made instead of
    sending it. The plan a human reviews is therefore produced by the same code
    that will later act, not by a description of it.
    """
    return SDRangelClient(execute=not ctx.dry_run,
                          emit=lambda m: emit(f"      {m}"))


def _result(ok: bool, ctx: ActionContext, detail: str,
            c: SDRangelClient) -> ActionResult:
    return ActionResult(ok=ok, action=ctx.action, detail=detail,
                        calls=list(c.calls), base=c.base)


# ---------------------------------------------------------------------------
# the five actions
# ---------------------------------------------------------------------------

def fix_phantom_deviceset(ctx: ActionContext, *, emit: Emit) -> ActionResult:
    """A deviceset with no real device bound → re-apply the whole profile.

    Imported lazily: `translator` is the writing path, and keeping the import
    inside the one function that needs it means the module-level surface of the
    reconciler package stays read-only for anything that inspects it.
    """
    from .. import translator

    if ctx.profile is None:
        return ActionResult(False, ctx.action, "profile_unreadable")
    c = _client(ctx, emit)
    rc = translator.apply(ctx.profile, execute=not ctx.dry_run,
                          emit=lambda m: emit(f"      {m}"), client=c)
    ok = (rc == translator.OK)
    return _result(ok, ctx, f"apply rc={rc}", c)


def fix_missing_channel(ctx: ActionContext, *, emit: Emit) -> ActionResult:
    """Add back channels the profile expects but the deviceset does not have.

    One channel per call, with the spacing SDRangel needs — bulk channel adds
    crash it (CHANNEL_DELAY exists for that reason).
    """
    if ctx.profile is None:
        return ActionResult(False, ctx.action, "profile_unreadable")
    if not ctx.missing_offsets:
        return ActionResult(False, ctx.action, "nothing_missing")

    prof = ctx.profile
    c = _client(ctx, emit)
    wanted = {ch.offset_from(prof.center_freq_hz): ch
              for ch in prof.channels_apply_order()}
    added: List[str] = []
    for off in ctx.missing_offsets:
        ch = wanted.get(off)
        if ch is None:
            continue
        emit(f"      + re-adding {ch.title!r} @ {ch.freq_hz/1e6:.4f} MHz "
             f"(offset {off:+d} Hz)")
        if not c.add_channel(ctx.deviceset_index, prof.channel_type,
                             prof.settings_key,
                             _channel_settings(prof, ch, ctx.audio_name)):
            return _result(False, ctx, f"add_channel_failed after {len(added)}", c)
        added.append(ch.title)
    if not added:
        return _result(False, ctx, "no_matching_channel_in_profile", c)
    return _result(True, ctx, f"added={len(added)}", c)


def fix_missing_keepalive(ctx: ActionContext, *, emit: Emit) -> ActionResult:
    """Add back the always-open channel that holds the mount up.

    Same mechanism as a missing channel — the distinction is kept because the
    consequence differs: without the keepalive the mount drops as soon as every
    real channel squelches closed, so this one is worth naming in the log.
    """
    if ctx.profile is None:
        return ActionResult(False, ctx.action, "profile_unreadable")
    ka = [ch for ch in ctx.profile.channels_apply_order() if ch.keepalive]
    if not ka:
        return ActionResult(False, ctx.action, "profile_has_no_keepalive")
    ch = ka[0]
    sub = ctx._replace(missing_offsets=[ch.offset_from(ctx.profile.center_freq_hz)])
    res = fix_missing_channel(sub, emit=emit)
    return res._replace(action=ctx.action)


def fix_unbound_device(ctx: ActionContext, *, emit: Emit) -> ActionResult:
    """A bound but idle deviceset → start it."""
    c = _client(ctx, emit)
    emit(f"      starting ds{ctx.deviceset_index}")
    c.run(ctx.deviceset_index)
    if ctx.dry_run:
        return _result(True, ctx, "would_run", c)
    state = c.device_state(ctx.deviceset_index)
    ok = (state == "running")
    return _result(ok, ctx, f"state_after_run={state}", c)


def fix_mount_404(ctx: ActionContext, *, emit: Emit) -> ActionResult:
    """A dark mount behind a healthy backend → re-arm the copyToUDP tap.

    Plain arming does not start SDRangel's sender thread; the 0→1 toggle does.
    set_copy_to_udp also refuses to re-toggle a tap that is already emitting to
    this port, which is what keeps a shared mount (Air + VFO both on idx -1 →
    :9998) from being blipped by whichever role noticed first.
    """
    c = _client(ctx, emit)
    emit(f"      re-arming copyToUDP → {ctx.udp_address}:{ctx.udp_port} "
         f"on audio index {ctx.audio_index}")
    c.set_copy_to_udp(address=ctx.udp_address, port=ctx.udp_port,
                      audio_index=ctx.audio_index)
    if ctx.dry_run:
        return _result(True, ctx, "would_toggle_tap", c)
    # Verify the thing we actually care about: audio reaching listeners. The
    # bridge needs a moment to connect to icecast after the sender starts.
    st = backends.mount_state(ctx.mount)
    return _result(bool(st.present), ctx, f"mount={st.http_status}", c)


#: category → handler. A category absent here is not actionable, which is the
#: same list config.ACTIONABLE advertises; the test suite asserts they agree so
#: a category can never be enabled in config without a handler behind it.
HANDLERS = {
    "phantom_deviceset": fix_phantom_deviceset,
    "missing_channel": fix_missing_channel,
    "missing_keepalive": fix_missing_keepalive,
    "unbound_device": fix_unbound_device,
    "mount_404_with_healthy_backend": fix_mount_404,
}


#: classifier issue → action category, in the order they must be attempted.
#:
#: Order is causal, not alphabetical: there is no point adding a channel to a
#: deviceset with no device bound, and no point re-arming a tap on a deviceset
#: that is not running. The first match wins, and only ONE action runs per role
#: per pass, so the most fundamental repair always goes first and the next pass
#: re-classifies against whatever that changed.
#:
#: `serial_mismatch` is deliberately ABSENT. The wrong device being bound is
#: RECOVERABLE to a human, but "rebind the radio" is not a repair to attempt
#: unattended: on this fleet a device may have been moved on purpose, and a
#: rebind on a shared apiService is the operation with the worst failure mode
#: (§5.4 — a dirty release drops an RSPduo off the USB bus until reboot). It
#: stays logged and unfixed until a phase that has earned it.
ISSUE_TO_ACTION: Sequence[Tuple[str, str]] = (
    ("phantom_deviceset", "phantom_deviceset"),
    ("deviceset_missing", "phantom_deviceset"),
    ("deviceset_not_running", "unbound_device"),
    ("keepalive_missing", "missing_keepalive"),
    ("channel_missing", "missing_channel"),
    ("mount_absent", "mount_404_with_healthy_backend"),
)


def action_for(issues: Sequence[str]) -> Optional[str]:
    """The one action to attempt for this pass, or None if nothing is actionable."""
    for issue, action in ISSUE_TO_ACTION:
        if issue in issues:
            return action
    return None


def run_action(ctx: ActionContext, *, emit: Emit) -> ActionResult:
    """Dispatch one action. Never raises — a crash here must not kill the loop."""
    handler = HANDLERS.get(ctx.action)
    if handler is None:
        return ActionResult(False, ctx.action, "no_handler")
    try:
        return handler(ctx, emit=emit)
    except Exception as exc:                     # noqa: BLE001 — see docstring
        return ActionResult(False, ctx.action, f"exception={exc!r}")
