"""sb3.killswitch — status / kill / resume.

Phase 1 scope (see docs/sb3-neptune-architecture.md §6):

  * ``status``  — REAL. Fully implemented, read-only, safe on a live box.
  * ``kill``    — DRY-RUN ONLY in this iteration. ``--execute`` is accepted and
                  refused; Phase 1.1 enables it after review.
  * ``resume``  — a no-op adopt. There is no reconciler yet, so it observes and
                  reports; it never asserts.

``status`` is deliberately the first real deliverable, and that is not a
consolation prize. Run against Neptune on 2026-07-16 it would have caught, in
one command, an outage that five green launchd agents had been hiding for 27
hours: the analog mount 404 (absent, not silent), SDRangel bound to a phantom
deviceset, and the two agents that would have self-healed it not loaded at all.
A control plane that cannot describe the system is not a control plane.

THE INVARIANT (§4.3 step 6), and the whole reason this file exists:

    kill -> SDRangel and SDRTrunk keep producing audio; every live mount stays
    200; no config is lost.

`kill` must PROVE that, not assume it. It samples the guarded mounts before and
after, and exits non-zero if a mount that was live stops being live. A kill
switch that does not verify the invariant it exists to protect is a wish.
"""

from __future__ import annotations

import os
from typing import Callable, Dict, List, Optional

from . import backends, ownership, settle
from .state import State

Emit = Callable[[str], None]

EXIT_OK = 0
EXIT_INVARIANT_VIOLATED = 1
EXIT_NOT_IMPLEMENTED = 2
EXIT_REFUSED = 3


def _emit_default(msg: str) -> None:
    print(msg)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def cmd_status(*, emit: Emit = _emit_default, state: Optional[State] = None) -> int:
    state = state or State()
    snap = backends.snapshot(ownership.GUARDED_MOUNTS)
    buckets = ownership.classify_all(snap["loaded"])

    emit("sb3-ctl status")
    emit("")
    emit(f"  sentinel : {'KILLED' if state.is_killed() else 'live'}"
         f"  ({state.killed_path})")
    emit("")

    emit("  SB3 layer (dies on `kill`)")
    if buckets["sb3"]:
        for label in buckets["sb3"]:
            emit(f"    * {label}")
    else:
        emit("    (none loaded — SB3 is not running on this box)")
    emit("")

    emit("  Backend (NEVER touched by `kill`)")
    for label in sorted(ownership.BACKEND):
        mark = "*" if label in buckets["backend"] else "·"
        suffix = "" if label in buckets["backend"] else "  (not loaded)"
        emit(f"    {mark} {label}{suffix}")
    emit("")

    if buckets["unclassified"]:
        emit("  ⚠ UNCLASSIFIED — not in sb3/ownership.py; `kill` has no opinion")
        for label in buckets["unclassified"]:
            emit(f"    ? {label}")
        emit("")

    emit("  Guarded mounts")
    for m in snap["mounts"]:
        code = m.http_status if m.http_status is not None else "unreachable"
        note = ""
        if m.http_status == 404:
            note = "  ← ABSENT from icecast (no source ever connected)"
        elif m.present:
            note = "  ← live"
        emit(f"    {m.mount:<22} {code}{note}")
    emit("")

    if snap["devicesets"]:
        emit("  SDRangel devicesets")
        for ds in snap["devicesets"]:
            serial = ds.serial or "None"
            flag = "  ← PHANTOM (no real device bound)" if ds.is_phantom else ""
            emit(f"    DS{ds.index}  hw={ds.hw_type}  serial={serial}  "
                 f"state={ds.state}{flag}")
            for ch in ds.channels:
                emit(f"          ch: {ch}")
    else:
        emit("  SDRangel devicesets: (unreachable)")
    emit("")

    emit("  Ownership table (§4.2)")
    for row in ownership.STATE_TABLE:
        emit(f"    {row.state:<44} {row.owner:<22} {row.on_kill}")

    return EXIT_OK


# ---------------------------------------------------------------------------
# kill
# ---------------------------------------------------------------------------

def cmd_kill(*, execute: bool = False, emit: Emit = _emit_default,
             state: Optional[State] = None, uid: Optional[int] = None) -> int:
    state = state or State()
    uid = uid if uid is not None else os.getuid()

    if execute:
        return _kill_execute(emit=emit, state=state, uid=uid)

    emit("sb3-ctl kill  [DRY RUN — nothing will be stopped]")
    emit("")

    before = {m.mount: m for m in
              (backends.mount_state(m) for m in ownership.GUARDED_MOUNTS)}
    emit("  Guarded mounts BEFORE:")
    for m in before.values():
        emit(f"    {m.mount:<22} {m.http_status}")
    emit("")

    loaded = backends.launchctl_loaded()
    buckets = ownership.classify_all(loaded)
    seq = ownership.kill_sequence(loaded)

    emit(f"  1. touch {state.killed_path}   # fail-CLOSED sentinel (§4.4)")
    emit(f"     would: {state.describe_arm()}")
    emit("")

    emit("  2-4. teardown, in §4.3 order — lease consumers BEFORE the broker.")
    emit("       The FULL canonical order is shown; only loaded agents are acted on.")
    emit("")
    brokers = ("com.scannerproject.tuner-broker", "com.scannerproject.sb3-broker")
    for label in ownership.KILL_ORDER:
        is_loaded = label in loaded
        managed = " [stub]" if label in ownership.MANAGED_AGENTS else ""
        if is_loaded:
            emit(f"     would: {' '.join(settle.bootout_command(label, uid))}{managed}")
        else:
            emit(f"     ·      {label}{managed}  (not loaded — skipped)")
    emit("")
    emit("            ^ the two broker labels are LAST, deliberately. Killing a")
    emit("              broker before its children would yank the lease socket")
    emit("              out from under a live child (§4.3 step 3).")
    if not seq:
        emit("")
        emit("     (nothing loaded to stop — but the ordering above IS committed")
        emit("      and reviewable; §6 warns it is the part hard to retrofit.)")
    emit("")
    emit(f"     would: sleep {settle.DRAIN_SECONDS:g}  # apiService settle beat")
    emit("")

    emit("  5. LEAVE RUNNING, always:")
    for label in sorted(ownership.BACKEND):
        live = " (loaded)" if label in buckets["backend"] else " (not loaded)"
        emit(f"     · {label}{live}")
    emit("")

    emit("  6. VERIFY every mount still 200; exit non-zero if any dropped.")
    emit("     (this is the point — a kill switch that does not verify the")
    emit("      invariant it exists to protect is a wish)")
    emit("")

    if buckets["unclassified"]:
        emit("  ⚠ REFUSING-WORTHY: unclassified agents present. `kill` has no")
        emit("    opinion about these; classify them in sb3/ownership.py:")
        for label in buckets["unclassified"]:
            emit(f"      ? {label}")
        emit("")

    rc = verify_mounts(before, emit=emit, label="DRY RUN — mounts unchanged by this run")
    emit("")
    emit("  DRY RUN complete. Nothing was stopped. No sentinel was written.")
    return rc


def _kill_execute(*, emit: Emit, state: State, uid: int) -> int:
    """Phase 1.1: the real teardown, in §4.3 order, with the invariant proven.

    Touches ONLY SB3_LAYER labels. There is no code path here that can reach a
    BACKEND label — `kill_sequence()` is built from KILL_ORDER, and a test
    asserts KILL_ORDER and BACKEND are disjoint.
    """
    emit("sb3-ctl kill --execute")
    emit("")

    # 1. Sample the invariant BEFORE touching anything. Without a before-state
    #    there is nothing to compare against, and "verified" would be a guess.
    before = {m: backends.mount_state(m) for m in ownership.GUARDED_MOUNTS}
    emit("  Guarded mounts BEFORE:")
    for m in before.values():
        emit(f"    {m.mount:<22} {m.http_status}")
    emit("")

    loaded = backends.launchctl_loaded()
    buckets = ownership.classify_all(loaded)

    if buckets["unclassified"]:
        emit("  ✗ REFUSING: unclassified com.scannerproject.* agents are loaded.")
        emit("    `kill` has no opinion about these, and guessing is how the")
        emit("    boundary stops being real. Classify them in sb3/ownership.py:")
        for label in buckets["unclassified"]:
            emit(f"      ? {label}")
        return EXIT_REFUSED

    # 2. Arm the sentinel FIRST (§4.3 step 1). If teardown dies halfway, the
    #    half-torn-down state must read as "killed", not as "healthy".
    state.arm()
    emit(f"  ✓ armed sentinel: {state.killed_path}")
    emit("")

    # 3. Teardown, consumers -> brokers.
    seq = ownership.kill_sequence(loaded)
    if seq:
        emit("  Teardown (§4.3 order — lease consumers BEFORE the brokers):")
        for label in seq:
            settle.bootout(label, uid, execute=True,
                           emit=lambda m: emit(f"    {m}"))
    else:
        emit("  (no SB3 agents loaded — nothing to stop)")
    emit("")

    # 4. Settle beat.
    settle.drain(execute=True, emit=lambda m: emit(f"  {m}"))
    emit("")

    # 5. Confirm the SB3 layer is actually gone. bootout returning is not proof.
    still = [l for l in seq if settle.is_loaded(l, uid)]
    if still:
        emit(f"  ✗ still loaded after bootout: {', '.join(still)}")
        return EXIT_INVARIANT_VIOLATED
    emit("  ✓ SB3 layer is gone — every targeted agent is unloaded")
    emit("")

    # 6. Confirm the backend is untouched. This is the invariant `kill` exists
    #    to protect, and it is proven, not assumed.
    emit("  Backend (must be untouched):")
    now_loaded = set(backends.launchctl_loaded())
    backend_before = set(buckets["backend"])
    lost = sorted(backend_before - now_loaded)
    for label in sorted(backend_before):
        emit(f"    {'✓' if label in now_loaded else '✗ LOST'} {label}")
    if lost:
        emit(f"  ✗ BACKEND AGENTS DISAPPEARED: {', '.join(lost)}")
        return EXIT_INVARIANT_VIOLATED
    emit("")

    rc = verify_mounts(before, emit=emit, label="Invariant check (§4.3 step 6)")
    emit("")
    if rc == EXIT_OK:
        emit("  kill complete, invariant held.")
    else:
        emit("  kill completed but THE INVARIANT FAILED — see above.")
    return rc


def cmd_resume_execute(*, emit: Emit, state: State, uid: int) -> int:
    """Phase 1.1: bring the SB3 layer back.

    §4.4: resume brings the BROKER back FIRST and observes before asserting —
    the reverse of the kill order. It adopts live backend state and never
    replays a snapshot; on divergence the live backend wins.

    Phase 1's agents are stubs with nothing to reconcile, so "observe before
    asserting" is currently just "observe". The ordering is what is being
    established.
    """
    emit("sb3-ctl resume --execute")
    emit("")

    before = {m: backends.mount_state(m) for m in ownership.GUARDED_MOUNTS}
    emit("  Guarded mounts BEFORE:")
    for m in before.values():
        emit(f"    {m.mount:<22} {m.http_status}")
    emit("")

    # Brokers first — the reverse of the teardown order (§4.4).
    order = [l for l in reversed(ownership.KILL_ORDER)
             if l in install_mod_managed()]
    emit("  Bringing SB3 back (brokers FIRST — reverse of kill order, §4.4):")
    for label in order:
        plist = install_mod_target(label)
        if not plist.exists():
            emit(f"    ✗ {label}: plist not installed ({plist}) — run `install --execute`")
            return EXIT_INVARIANT_VIOLATED
        if settle.is_loaded(label, uid):
            emit(f"    · {label} already loaded")
            continue
        rc = settle.bootstrap(label, plist, uid, execute=True,
                              emit=lambda m: emit(f"    {m}"))
        if not rc:
            return EXIT_INVARIANT_VIOLATED
    emit("")

    emit("  Observing live backend state (adopt, never clobber — §4.4):")
    ds_list = backends.sdrangel_devicesets()
    if ds_list:
        for ds in ds_list:
            flag = "  ← PHANTOM" if ds.is_phantom else ""
            emit(f"    DS{ds.index}  hw={ds.hw_type}  serial={ds.serial}  "
                 f"state={ds.state}{flag}")
    else:
        emit("    (SDRangel unreachable — observed, not corrected)")
    emit("    No assertions made. On divergence the LIVE backend wins; a human")
    emit("    may have retuned by hand while SB3 was gone, and that is intent.")
    emit("")

    # Sentinel cleared LAST — a resume that fails partway must leave SB3 still
    # marked killed rather than half-alive and unmarked.
    state.clear()
    emit(f"  ✓ cleared sentinel: {state.killed_path}")
    emit("")

    rc = verify_mounts(before, emit=emit, label="Invariant check")
    emit("")
    emit("  resume complete, invariant held." if rc == EXIT_OK
         else "  resume completed but THE INVARIANT FAILED — see above.")
    return rc


def install_mod_managed():
    from . import ownership as _o
    return _o.MANAGED_AGENTS


def install_mod_target(label: str):
    from .install import LAUNCH_AGENTS_DIR
    return LAUNCH_AGENTS_DIR / f"{label}.plist"


def verify_mounts(before: Dict[str, backends.MountState], *, emit: Emit,
                  label: str = "Invariant check") -> int:
    """Re-sample the guarded mounts and compare. Non-zero if one regressed.

    Only a mount that WAS live and is no longer live is a violation. A mount
    that was already down (e.g. neptune-angel.mp3 under a deliberate pause) is
    reported but does not fail the check — `kill` is accountable for what it
    breaks, not for what it inherited.
    """
    emit(f"  {label}:")
    violated: List[str] = []
    for name, prev in before.items():
        now = backends.mount_state(name)
        if prev.present and not now.present:
            violated.append(name)
            emit(f"    ✗ {name}: was {prev.http_status}, now {now.http_status} "
                 f"— INVARIANT VIOLATED")
        elif prev.present:
            emit(f"    ✓ {name}: {now.http_status} (held)")
        else:
            emit(f"    · {name}: {now.http_status} (was already down; not ours)")
    if violated:
        emit(f"    RESULT: FAILED — {len(violated)} mount(s) dropped: "
             f"{', '.join(violated)}")
        return EXIT_INVARIANT_VIOLATED
    emit("    RESULT: OK — every mount that was live is still live")
    return EXIT_OK


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------

def cmd_resume(*, execute: bool = False, emit: Emit = _emit_default,
               state: Optional[State] = None, uid: Optional[int] = None) -> int:
    """Phase 1: observe and report. Never assert.

    §4.4's invariant: resume must read what the backends are ACTUALLY doing right
    now and reconcile forward from there. It must never replay a snapshot taken
    at kill time, and on divergence **the live backend wins** — a human may have
    retuned SDRangel by hand while SB3 was gone, and that is not drift to be
    corrected, it is intent.

    This deliberately inverts `sdrangel-restore.py`, which re-asserts a stored
    config every 10 minutes and would clobber exactly that. There is no
    reconciler yet, so all this can honestly do is show the divergence.
    """
    state = state or State()
    uid = uid if uid is not None else os.getuid()

    if execute:
        return cmd_resume_execute(emit=emit, state=state, uid=uid)

    emit("sb3-ctl resume  [Phase 1: adopt-only, no reconciler yet]")
    emit("")

    if not state.is_killed():
        emit("  sentinel absent — SB3 is not marked killed.")
        emit("  NOTE: absence is NOT permission to reconcile (§4.4, fail-closed).")
        emit("  Positive state is required to act. Nothing to do.")
        return EXIT_OK

    emit(f"  sentinel present: {state.killed_path}")
    emit("  would: observe live backend state, then clear the sentinel.")
    emit("")
    emit("  Live backend state right now:")
    for ds in backends.sdrangel_devicesets():
        flag = "  ← PHANTOM" if ds.is_phantom else ""
        emit(f"    DS{ds.index}  hw={ds.hw_type}  serial={ds.serial}  "
             f"state={ds.state}{flag}")
    emit("")
    emit("  Reconciler not implemented (Phase 1.1+). On divergence the LIVE")
    emit("  backend wins — the human is right. resume adopts; it never clobbers.")
    return EXIT_NOT_IMPLEMENTED
