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
        emit("REFUSED: --execute is not enabled in this build.")
        emit("")
        emit("  Phase 1 ships the kill ORDERING and the invariant CHECK, dry-run")
        emit("  only, so both can be reviewed before anything can stop a process.")
        emit("  Phase 1.1 enables execution after Will's review (§6).")
        emit("  Re-run without --execute to see the full plan.")
        return EXIT_REFUSED

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

    emit("  2-4. teardown, in §4.3 order — lease consumers BEFORE the broker:")
    if seq:
        for label in seq:
            emit(f"     would: {' '.join(settle.bootout_command(label, uid))}")
            if label == "com.scannerproject.tuner-broker":
                emit("            ^ LAST. Killing the broker before its children "
                     "would yank the")
                emit("              lease socket out from under a live child.")
    else:
        emit("     (no SB3 agents loaded — nothing to stop)")
        emit("     NOTE: the ordering above is still committed and reviewable;")
        emit("           it is the part §6 warns is hard to retrofit.")
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
               state: Optional[State] = None) -> int:
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
    emit("sb3-ctl resume  [Phase 1: adopt-only, no reconciler yet]")
    emit("")
    state = state or State()

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
