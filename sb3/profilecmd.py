"""sb3.profilecmd — CLI glue for `sb3-ctl profile <load|status|unload> <name>`.

Resolves a profile name to its file, loads+validates it, and dispatches to the
translator. Kept thin: all the real work is in profile.py (schema) and
translator.py (REST). This module only maps a CLI name to a Profile and picks
the verb.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from . import translator
from .gitdeploy import deploy_root
from .profile import Profile, ProfileError, load_profile
from .state import State

Emit = Callable[[str], None]


def resolve_profile_path(name: str) -> Optional[Path]:
    """Map a profile name/path to a file under <deploy>/profiles/.

    Accepts a dotted name (air.airband.nashville → air-airband-nashville.json),
    a bare basename, or a direct path.
    """
    p = Path(name)
    if p.is_file():
        return p
    root = deploy_root()
    candidates = [
        root / "profiles" / f"{name}.json",
        root / "profiles" / f"{name.replace('.', '-')}.json",
        root / "profiles" / name,
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def run(action: str, name: str, *, execute: bool, emit: Emit = print) -> int:
    state = State()

    if action == "status":
        return _status(name, emit=emit, state=state)

    path = resolve_profile_path(name)
    if path is None:
        emit(f"sb3-ctl profile {action}: no profile found for {name!r}")
        emit(f"  looked under {deploy_root()/'profiles'}/ "
             f"(tried {name}.json and {name.replace('.', '-')}.json)")
        return translator.INVARIANT_VIOLATED
    try:
        prof = load_profile(path)
    except ProfileError as exc:
        emit(f"sb3-ctl profile {action}: profile INVALID — refusing")
        emit(f"  {exc}")
        return translator.REFUSED

    if action == "load":
        return translator.apply(prof, execute=execute, emit=emit, state=state)
    if action == "unload":
        return translator.unload(prof, execute=execute, emit=emit, state=state)
    emit(f"unknown action {action!r}")
    return translator.INVARIANT_VIOLATED


def _status(name: str, *, emit: Emit, state: State) -> int:
    """Show the loaded profile and classify live divergence. Read-only."""
    record = state.read_loaded_profile()
    emit("sb3-ctl profile status")
    emit("")
    if record is None:
        emit("  loaded profile: (none)")
    else:
        emit(f"  loaded profile: {record.get('name')}  "
             f"(role={record.get('role')}, mode={record.get('mode')})")
        emit(f"    deviceset : ds{record.get('deviceset_index')}")
        emit(f"    serial    : {record.get('serial')}")
        emit(f"    mount     : {record.get('mount')}")
        emit(f"    audio idx0: {record.get('audio_device')}")
    emit("")

    # Try to load the named/loaded profile file for a richer compare; fall back
    # to the runtime record alone.
    prof = None
    target = name if name and name != "-" else (record or {}).get("name")
    if target:
        path = resolve_profile_path(target)
        if path:
            try:
                prof = load_profile(path)
            except ProfileError:
                prof = None

    emit("  live divergence:")
    cls = translator.observe(prof, emit=lambda m: emit(f"  {m}"), state=state)
    emit("")
    emit(f"  classification: {cls}")
    return translator.OK
