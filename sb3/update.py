"""sb3.update — pull the deploy checkout forward and bounce agents onto it.

The flow, and why each guard exists:

    fetch → decide target → checkout → kill → resume → verify

`update` is the first SB3 command that changes the CODE the agents run, so it
carries the strongest guards in the package:

  * **Refuse a dirty tree.** A local edit in the deploy checkout is either an
    accident or a hand-fix nobody wrote down; `git checkout` would either fail
    or silently stomp it. Either way, stop and let a human look.
  * **Backend PIDs are sampled before AND after, and a change ABORTS.** The
    whole safety story of SB3 is "the backend never moves." An update that
    coincided with SDRangel restarting must not be reported as a clean update —
    that would launder a backend disruption as success. If a backend PID
    changed across the bounce, update returns non-zero even though its own
    steps succeeded.
  * **kill/resume are reused verbatim.** update does not invent a second way to
    stop and start the agents; it calls the same `kill --execute` /
    `resume --execute` that were proven in Phase 1.1, so the ordering, the
    settle beat, and the mount invariant all come along for free.

Dry-run (no --execute) fetches — a read-only network op — and prints the plan:
current SHA, target SHA, whether they diverge, and what it WOULD do. It changes
nothing on disk and touches no agent.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

from . import backends, gitdeploy, killswitch, ownership
from .state import State

Emit = Callable[[str], None]

EXIT_OK = killswitch.EXIT_OK
EXIT_INVARIANT_VIOLATED = killswitch.EXIT_INVARIANT_VIOLATED
EXIT_REFUSED = killswitch.EXIT_REFUSED


def _backend_pids() -> dict:
    """Label -> launchd PID for every loaded BACKEND agent. The safety anchor."""
    out = {}
    try:
        proc = __import__("subprocess").run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=5.0)
    except Exception:
        return out
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2].strip() in ownership.BACKEND:
            pid = parts[0].strip()
            out[parts[2].strip()] = pid if pid != "-" else None
    return out


def cmd_update(*, execute: bool = False, emit: Emit = print,
               target: Optional[str] = None) -> int:
    root = gitdeploy.deploy_root()

    emit(f"sb3-ctl update{' --execute' if execute else '  [DRY RUN]'}")
    emit("")

    if not gitdeploy.is_git_checkout(root):
        emit(f"  ✗ {root} is not a git checkout.")
        emit("    `update` pulls a git deploy. This looks like the old rsync")
        emit("    deploy — migrate to a checkout first (Phase B).")
        return EXIT_REFUSED

    # fetch is a READ. Safe in dry-run: it updates remote-tracking refs and the
    # object store, never the working tree or HEAD.
    emit("  git fetch --tags --prune origin …")
    rc, out, err = gitdeploy._git(root, "fetch", "--tags", "--prune", "origin")
    if rc != 0:
        emit(f"  ✗ fetch failed: {err or out}")
        return EXIT_INVARIANT_VIOLATED
    emit("    fetched.")
    emit("")

    st = gitdeploy.observe(root, check_remote=True)
    branch = st.branch
    tgt = target or (f"origin/{branch}" if branch else None)
    if tgt is None:
        emit("  ✗ detached HEAD and no explicit target — refusing to guess.")
        return EXIT_REFUSED

    rc, target_sha, err = gitdeploy._git(root, "rev-parse", tgt)
    if rc != 0:
        emit(f"  ✗ cannot resolve target {tgt!r}: {err}")
        return EXIT_INVARIANT_VIOLATED

    emit(f"  deployed : {st.short_sha}  on {branch or 'DETACHED'}")
    emit(f"  target   : {target_sha[:7]}  ({tgt})")
    emit(f"  dirty    : {st.dirty}")
    if st.sha == target_sha:
        emit("")
        emit("  Already at target — nothing to update.")
        return EXIT_OK
    emit(f"  divergent: YES — {st.short_sha} → {target_sha[:7]}")
    emit("")

    if not execute:
        emit("  WOULD (with --execute):")
        emit(f"    1. refuse if the tree is dirty (currently: "
             f"{'DIRTY — would refuse' if st.dirty else 'clean'})")
        emit("    2. record backend PIDs")
        emit(f"    3. git checkout {tgt}")
        emit("    4. sb3-ctl kill --execute   (bounce agents onto new code)")
        emit("    5. sb3-ctl resume --execute")
        emit("    6. verify: HEAD==target, agents up, backend PIDs UNCHANGED, "
             "mounts held")
        emit("")
        emit("  DRY RUN — fetched only. Working tree and agents untouched.")
        return EXIT_OK

    # ---- execute ---------------------------------------------------------
    if st.dirty:
        emit("  ✗ REFUSING: deploy checkout has uncommitted changes.")
        emit("    A dirty tree is an accident or an unrecorded hand-fix; either")
        emit("    way `git checkout` must not run over it. Resolve by hand.")
        rc2, porc, _ = gitdeploy._git(root, "status", "--porcelain")
        for line in porc.splitlines()[:10]:
            emit(f"      {line}")
        return EXIT_REFUSED

    pids_before = _backend_pids()
    emit(f"  backend PIDs before: {pids_before}")

    emit(f"  git checkout {tgt} …")
    rc, out, err = gitdeploy._git(root, "checkout", tgt)
    if rc != 0:
        emit(f"  ✗ checkout failed: {err or out}")
        return EXIT_INVARIANT_VIOLATED
    # Confirm HEAD actually moved — checkout returning 0 is not proof (§4.6).
    now = gitdeploy.observe(root, check_remote=False)
    if now.sha != target_sha:
        emit(f"  ✗ HEAD is {now.short_sha}, expected {target_sha[:7]} — aborting")
        return EXIT_INVARIANT_VIOLATED
    emit(f"    now at {now.short_sha}")
    emit("")

    uid = os.getuid()
    state = State()

    emit("  ── bounce: kill ──")
    krc = killswitch.cmd_kill(execute=True, emit=lambda m: emit(f"    {m}"),
                              state=state, uid=uid)
    if krc != EXIT_OK:
        emit("  ✗ kill did not report a clean invariant — NOT resuming. Inspect.")
        return krc
    emit("")
    emit("  ── bounce: resume ──")
    rrc = killswitch.cmd_resume(execute=True, emit=lambda m: emit(f"    {m}"),
                                state=state, uid=uid)
    if rrc != EXIT_OK:
        emit("  ✗ resume did not report a clean invariant. Inspect.")
        return rrc
    emit("")

    # Final safety: the backend must not have moved across the whole operation.
    pids_after = _backend_pids()
    emit(f"  backend PIDs after:  {pids_after}")
    moved = [l for l in pids_before
             if pids_before.get(l) != pids_after.get(l)]
    if moved:
        emit(f"  ✗ BACKEND MOVED during update: {moved}")
        emit("    An update that disturbed the backend is NOT a clean update,")
        emit("    even though every SB3 step succeeded. Returning non-zero.")
        return EXIT_INVARIANT_VIOLATED

    emit("")
    emit(f"  update complete: {st.short_sha} → {now.short_sha}, "
         f"agents bounced, backend untouched, invariant held.")
    return EXIT_OK
