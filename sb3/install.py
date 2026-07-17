"""sb3.install — write/remove the SB3 launchd plists. DRY-RUN ONLY in Phase 1.

This is the mechanism Phase 1.1 uses to actually install the agents once Will
greenlights it.  Right now every path prints ``would:`` and ``--execute`` is
refused, so nothing here can touch ``~/Library/LaunchAgents/``.

The templates in ``macos/launchd/sb3/`` carry ``{{PYTHON}}``, ``{{REPO_DIR}}``
and ``{{HOME}}`` placeholders.  Rendering happens here so the plist on disk is
always absolute and always matches the checkout it was installed from — a plist
with a stale path is a launchd agent that fails silently at boot, which is §4.6's
pattern with a different costume.

**install is deliberately NOT the same operation as bootstrap.**  ``install``
writes the file; ``launchctl bootstrap`` loads it.  Keeping them separate means
Phase 1.1 can land the plists, inspect them, and *then* decide to load — rather
than discovering a bad plist by watching an agent flap.  ``install`` never loads.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, List, NamedTuple, Optional

from . import ownership

Emit = Callable[[str], None]

LAUNCH_AGENTS_DIR = Path(os.path.expanduser("~/Library/LaunchAgents"))


class PlistPlan(NamedTuple):
    label: str
    template: Path
    target: Path
    module: str
    template_exists: bool
    target_exists: bool


def repo_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def render(template_text: str, *, python: str, repo: str, home: str) -> str:
    """Substitute the template placeholders. Pure."""
    return (template_text
            .replace("{{PYTHON}}", python)
            .replace("{{REPO_DIR}}", repo)
            .replace("{{HOME}}", home))


def plan(agents_dir: Optional[Path] = None) -> List[PlistPlan]:
    """What install would do. Pure-ish: stats files, changes nothing."""
    agents_dir = agents_dir or LAUNCH_AGENTS_DIR
    tmpl_dir = repo_dir() / ownership.PLIST_TEMPLATE_DIR
    out: List[PlistPlan] = []
    for label, module in sorted(ownership.MANAGED_AGENTS.items()):
        template = tmpl_dir / f"{label}.plist"
        target = agents_dir / f"{label}.plist"
        out.append(PlistPlan(
            label=label,
            template=template,
            target=target,
            module=module,
            template_exists=template.is_file(),
            target_exists=target.is_file(),
        ))
    return out


def cmd_install(*, execute: bool = False, emit: Emit = print,
                agents_dir: Optional[Path] = None) -> int:
    from .killswitch import EXIT_OK, EXIT_REFUSED

    if execute:
        emit("REFUSED: `install --execute` is not enabled in this build.")
        emit("")
        emit("  Phase 1 ships the install MECHANISM and its plan output, dry-run")
        emit("  only, so both can be reviewed before anything is written to")
        emit("  ~/Library/LaunchAgents/. Phase 1.1 = install + enable --execute,")
        emit("  and that is Will's explicit greenlight, not a default (§6).")
        return EXIT_REFUSED

    plans = plan(agents_dir)
    python = sys.executable or "/usr/bin/python3"
    repo = str(repo_dir())
    home = os.path.expanduser("~")

    emit("sb3-ctl install  [DRY RUN — nothing will be written]")
    emit("")
    emit(f"  python : {python}")
    emit(f"  repo   : {repo}")
    emit(f"  target : {(agents_dir or LAUNCH_AGENTS_DIR)}")
    emit("")

    for p in plans:
        emit(f"  {p.label}")
        emit(f"    module   : {p.module}")
        emit(f"    template : {p.template.relative_to(repo_dir())}"
             f"{'' if p.template_exists else '   ⚠ MISSING'}")
        emit(f"    would write: {p.target}"
             f"{'   (OVERWRITES existing)' if p.target_exists else ''}")
        emit(f"    would: render {{{{PYTHON}}}} {{{{REPO_DIR}}}} {{{{HOME}}}} → {p.target.name}")
        emit("")

    emit("  NOT loaded by install. `install` writes the file; `launchctl")
    emit("  bootstrap` loads it. They are kept separate on purpose so Phase 1.1")
    emit("  can land the plists, inspect them, and only then decide to load.")
    emit("")
    emit("  After Phase 1.1 enables this, loading would be:")
    for p in plans:
        emit(f"    launchctl bootstrap gui/$(id -u) {p.target}")
    emit("")
    emit("  DRY RUN complete. Nothing was written. Nothing was loaded.")
    return EXIT_OK


def cmd_uninstall(*, execute: bool = False, emit: Emit = print,
                  agents_dir: Optional[Path] = None) -> int:
    from .killswitch import EXIT_OK, EXIT_REFUSED

    if execute:
        emit("REFUSED: `uninstall --execute` is not enabled in this build.")
        emit("  Phase 1.1 enables it after review (§6).")
        return EXIT_REFUSED

    plans = plan(agents_dir)
    emit("sb3-ctl uninstall  [DRY RUN — nothing will be removed]")
    emit("")
    emit("  Stop before removing: a plist deleted out from under a loaded agent")
    emit("  leaves launchd holding a job whose definition is gone.")
    emit("")
    for p in plans:
        emit(f"  {p.label}")
        if p.target_exists:
            emit(f"    would: launchctl bootout gui/$(id -u)/{p.label}")
            emit(f"    would: rm {p.target}")
        else:
            emit(f"    · not installed ({p.target}) — nothing to do")
        emit("")
    emit("  Uninstall touches ONLY the SB3 plists above. It never touches a")
    emit("  backend agent — SDRangel, SDRTrunk, icecast and the bridges are not")
    emit("  SB3's to remove (§4.2).")
    emit("")
    emit("  DRY RUN complete. Nothing was removed.")
    return EXIT_OK
