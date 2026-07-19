"""sb3.state — $SB3_STATE and the fail-CLOSED sentinel.

§4.4's rule, and the reason this file is separate from everything else:

    **The absence of the sentinel is NOT permission to reconcile.**
    Positive state is required to act.

This inverts the `.sdrangel-restore-paused` bug — and that bug is not
hypothetical. Observed on Neptune 2026-07-16 (docs/phase-0-status.md §4):
a pause marker dropped on Jul 12, presumably for a few minutes of debugging,
silently survived a reboot and kept the analog path dead for ~27 hours while
every launchd agent reported healthy. No alarm, no non-zero exit. It is the
textbook "useful liar" / "third state" that sb7-northstar-program.md is named
for.

The failure mode of a fail-OPEN marker is that losing the file means "resume
clobbering." The failure mode of fail-CLOSED is that losing the file means
"refuse to act until a human says so." Only one of those is safe to be wrong
about.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

DEFAULT_STATE_DIR = Path(os.environ.get(
    "SB3_STATE", os.path.expanduser("~/.local/state/sb3")))

KILLED_SENTINEL = "killed"
LOADED_PROFILE = "loaded_profile.json"

# The analog pause marker (§ pause-marker interaction). Its presence keeps the
# analog path deliberately quiet; `profile load` treats a 404 mount + present
# marker as the intended baseline and removes the marker as its final step.
# Configurable because sb3 runs from ~/sb3 but the marker lives elsewhere.
DEFAULT_PAUSE_MARKER = Path(os.environ.get(
    "SB3_PAUSE_MARKER",
    os.path.expanduser("~/scannerproject/.sdrangel-restore-paused")))


class State:
    """Filesystem-backed SB3 control state. Read-only in Phase 1."""

    def __init__(self, state_dir: Path | None = None) -> None:
        self.state_dir = Path(state_dir) if state_dir else DEFAULT_STATE_DIR

    @property
    def killed_path(self) -> Path:
        return self.state_dir / KILLED_SENTINEL

    def is_killed(self) -> bool:
        """True only if the sentinel positively exists.

        Any error reading it returns True — refusing to act — because the safe
        default is to stay out of the way, not to start asserting.
        """
        try:
            return self.killed_path.is_file()
        except OSError:
            return True

    def describe_arm(self) -> str:
        """The command `kill` would run to arm the sentinel. Pure."""
        return f"mkdir -p {self.state_dir} && touch {self.killed_path}"

    def describe_clear(self) -> str:
        """The command `resume` would run to clear it. Pure."""
        return f"rm -f {self.killed_path}"

    # -- loaded-profile runtime record ------------------------------------

    @property
    def loaded_profile_path(self) -> Path:
        return self.state_dir / LOADED_PROFILE

    def read_loaded_profile(self) -> Optional[dict]:
        """The profile SB3 last applied, or None. Read-only, never raises."""
        try:
            return json.loads(self.loaded_profile_path.read_text())
        except (OSError, ValueError):
            return None

    def write_loaded_profile(self, record: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.loaded_profile_path.write_text(json.dumps(record, indent=2))

    def clear_loaded_profile(self) -> None:
        try:
            self.loaded_profile_path.unlink()
        except FileNotFoundError:
            pass

    # -- pause marker ------------------------------------------------------

    @property
    def pause_marker(self) -> Path:
        return DEFAULT_PAUSE_MARKER

    def marker_present(self) -> bool:
        try:
            return self.pause_marker.is_file()
        except OSError:
            return True

    def remove_marker(self) -> None:
        try:
            self.pause_marker.unlink()
        except FileNotFoundError:
            pass

    # -- mutation (Phase 1.1) ---------------------------------------------

    def arm(self) -> None:
        """Positively mark SB3 as killed.

        Armed FIRST, before anything is stopped (§4.3 step 1). If the teardown
        dies halfway, the sentinel is already there and the reconciler stays
        out — the half-torn-down state reads as "killed", not as "healthy".
        Arming last would invert that.
        """
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.killed_path.touch()

    def clear(self) -> None:
        """Clear the sentinel. Only `resume` does this, and only at the END.

        Cleared LAST, after the agents are back, so a resume that fails partway
        leaves SB3 still marked killed rather than half-alive and unmarked.
        """
        try:
            self.killed_path.unlink()
        except FileNotFoundError:
            pass
