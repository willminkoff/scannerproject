"""sb3.reconcilercmd — ``sb3-ctl reconciler …``: the human's switch for Phase 4.2.

The reconciler ships disabled.  This is how a human turns it on, watches it, and
turns it off again — and, critically, how they turn it off *fast*:

    sb3-ctl reconciler passive --execute     # revert to 4.1 observe-only, NOW

``passive`` writes a sentinel file rather than editing config, so it works even
if the config is unreadable, takes effect within one poll interval without
restarting the agent, and cannot be undone by the reconciler itself.

Mutations follow the sb3-ctl convention: DRY RUN by default, ``--execute`` to
act.  Reading status never needs it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

from .killswitch import EXIT_OK, EXIT_REFUSED
from .reconciler import config as CFG
from .reconciler import safety as S

Emit = Callable[[str], None]

#: Where the agent persists quarantine so this CLI can clear it.
QUARANTINE_PATH = "~/.local/state/sb3/reconciler_quarantine.json"


def _counter() -> S.FailureCounter:
    return S.FailureCounter(path=Path(os.path.expanduser(QUARANTINE_PATH)))


def cmd_status(*, emit: Emit = print) -> int:
    cfg = CFG.load()
    emit("sb3-ctl reconciler status")
    emit("")
    emit(f"  mode          : {cfg.describe()}")
    emit(f"  config        : {cfg.path}"
         f"{'' if cfg.path.is_file() else '   (absent — shipped defaults)'}")
    emit(f"  enabled       : {cfg.enabled}")
    emit(f"  dry_run       : {cfg.dry_run}")
    emit(f"  PASSIVE sentinel: {cfg.sentinel}"
         f"   {'PRESENT — observe-only, wins over config' if cfg.passive_sentinel_present() else '(absent)'}")
    emit("")
    emit("  Actions (a category not listed here can never be enabled):")
    for name in CFG.ACTIONABLE:
        emit(f"    {'✓' if cfg.action_enabled(name) else '·'} {name}")
    emit("")
    emit(f"  rate limit    : {cfg.base_backoff:g}s → {cfg.max_backoff:g}s "
         f"(exponential, per role)")
    emit(f"  quarantine at : {cfg.quarantine_threshold} consecutive failures")
    emit(f"  emergency pause: {cfg.emergency_pause_seconds:g}s")
    emit("")
    q = _counter().list_quarantined()
    if q:
        emit("  QUARANTINED (cleared only here):")
        for role, action, why in q:
            emit(f"    ✗ {role}/{action}: {why}")
            emit(f"      → sb3-ctl reconciler resume-action {role} {action} --execute")
    else:
        emit("  quarantined   : none")
    return EXIT_OK


def _set(update: dict, *, execute: bool, emit: Emit, label: str) -> int:
    cfg = CFG.load()
    data = dict(cfg.data)
    data.update(update)
    if not execute:
        emit(f"sb3-ctl reconciler {label}  [DRY RUN]")
        emit(f"  would write {cfg.path}")
        for k, v in sorted(update.items()):
            emit(f"    {k}: {cfg.data.get(k)!r} → {v!r}")
        emit("")
        emit("  DRY RUN — nothing written. Re-run with --execute.")
        return EXIT_OK
    path = CFG.save(data, cfg.path)
    emit(f"sb3-ctl reconciler {label} --execute")
    emit(f"  ✓ wrote {path} (atomic)")
    new = CFG.load(path)
    emit(f"  mode now: {new.describe()}")
    emit("  The running agent re-reads config every pass — this takes effect")
    emit("  within one poll interval (~30s). No restart needed.")
    return EXIT_OK


def cmd_enable(*, execute: bool, emit: Emit = print) -> int:
    return _set({"enabled": True, "dry_run": False},
                execute=execute, emit=emit, label="enable")


def cmd_disable(*, execute: bool, emit: Emit = print) -> int:
    return _set({"enabled": False}, execute=execute, emit=emit, label="disable")


def cmd_dry_run(*, execute: bool, emit: Emit = print) -> int:
    return _set({"enabled": True, "dry_run": True},
                execute=execute, emit=emit, label="dry-run")


def cmd_passive(*, execute: bool, emit: Emit = print) -> int:
    """Write the sentinel → force 4.1 observe-only behaviour."""
    cfg = CFG.load()
    if not execute:
        emit("sb3-ctl reconciler passive  [DRY RUN]")
        emit(f"  would: touch {cfg.sentinel}")
        emit("  Effect: reconciler reverts to Phase 4.1 (log-only, no writes)")
        emit("  within one poll interval, regardless of config.")
        return EXIT_OK
    cfg.sentinel.parent.mkdir(parents=True, exist_ok=True)
    cfg.sentinel.touch()
    emit("sb3-ctl reconciler passive --execute")
    emit(f"  ✓ armed {cfg.sentinel}")
    emit("  Reconciler is observe-only. The agent itself can never remove this;")
    emit("  clear it with `sb3-ctl reconciler unpassive --execute`.")
    return EXIT_OK


def cmd_unpassive(*, execute: bool, emit: Emit = print) -> int:
    cfg = CFG.load()
    if not execute:
        emit("sb3-ctl reconciler unpassive  [DRY RUN]")
        emit(f"  would: rm {cfg.sentinel}")
        emit(f"  config would then govern: {'ENABLED' if cfg.enabled else 'DISABLED'}")
        return EXIT_OK
    try:
        cfg.sentinel.unlink()
        emit(f"  ✓ removed {cfg.sentinel}")
    except FileNotFoundError:
        emit(f"  · {cfg.sentinel} not present")
    emit(f"  config now governs: {CFG.load().describe()}")
    return EXIT_OK


def cmd_resume_action(role: Optional[str], action: Optional[str], *,
                      execute: bool, emit: Emit = print) -> int:
    if not role or not action:
        emit("usage: sb3-ctl reconciler resume-action <role> <action> --execute")
        return EXIT_REFUSED
    counter = _counter()
    if not counter.quarantined(role, action):
        emit(f"  · {role}/{action} is not quarantined — nothing to do")
        return EXIT_OK
    if not execute:
        emit("sb3-ctl reconciler resume-action  [DRY RUN]")
        emit(f"  would clear quarantine on {role}/{action}")
        return EXIT_OK
    counter.release(role, action)
    emit(f"  ✓ released {role}/{action} — it will be retried next pass")
    return EXIT_OK


def run(action: str, *, role: Optional[str] = None,
        action_name: Optional[str] = None, execute: bool = False,
        emit: Emit = print) -> int:
    if action == "status":
        return cmd_status(emit=emit)
    if action == "enable":
        return cmd_enable(execute=execute, emit=emit)
    if action == "disable":
        return cmd_disable(execute=execute, emit=emit)
    if action == "dry-run":
        return cmd_dry_run(execute=execute, emit=emit)
    if action == "passive":
        return cmd_passive(execute=execute, emit=emit)
    if action == "unpassive":
        return cmd_unpassive(execute=execute, emit=emit)
    if action == "resume-action":
        return cmd_resume_action(role, action_name, execute=execute, emit=emit)
    emit(f"unknown reconciler action {action!r}")
    return EXIT_REFUSED
