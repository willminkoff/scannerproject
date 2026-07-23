"""sb3.reconciler.config — the Phase 4.2 enable flag and safety knobs.

The reconciler ships **disabled**.  Phase 4.1 proved it could watch the box
without touching it; 4.2 gives it hands, and the config file is the thing that
decides whether those hands are attached.  ``enabled: false`` is the shipped
default and the value a fresh deploy gets, so landing this code on Neptune
changes nothing until a human runs ``sb3-ctl reconciler enable``.

Three separate off-switches exist on purpose, because they fail in different
directions:

  * ``enabled: false``     — config says don't act.  The normal state.
  * ``dry_run: true``      — act through the SAME code path, but every write is
                             recorded and printed instead of sent.  This is how
                             4.2's plan gets reviewed before it is trusted.
  * the PASSIVE sentinel   — a file on disk (``.sb3-reconciler-passive``) that
                             forces 4.1 behaviour regardless of config.  It is
                             checked every pass, so it takes effect within 30 s
                             without editing config or restarting the agent, and
                             it WINS over everything else.

The sentinel is deliberately a file rather than a config key: §4.4's lesson from
the ``.sdrangel-restore-paused`` incident is that a marker on disk is the thing
a human reaches for at 2 a.m., and it must be impossible for the process to
un-set it.  Nothing here ever removes it.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

#: Phase 4.2's config. Phase 4.1 read ~/sb3/config.json for poll/log knobs; that
#: file is still honoured for those, so an existing deploy keeps working.
DEFAULT_CONFIG_PATH = "~/sb3/config/reconciler.json"
LEGACY_CONFIG_PATH = "~/sb3/config.json"

#: The global revert-to-passive switch. Its PRESENCE means "observe only".
#: Lives next to the other scannerproject markers, not under ~/sb3, so it
#: survives a redeploy of the checkout.
PASSIVE_SENTINEL = "~/scannerproject/.sb3-reconciler-passive"

#: Every RECOVERABLE category the reconciler is allowed to act on. A category
#: absent from this map is NEVER actionable, no matter what the config says —
#: adding one is a code change, reviewed, not a config edit on the box.
ACTIONABLE = (
    "phantom_deviceset",
    "missing_channel",
    "missing_keepalive",
    "unbound_device",
    "mount_404_with_healthy_backend",
)

DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "dry_run": False,
    "actions": {name: True for name in ACTIONABLE},
    "rate_limit": {
        "base_backoff_seconds": 30,
        "max_backoff_seconds": 240,
        "quarantine_threshold": 3,
    },
    "emergency_pause_seconds": 300,
}


class ReconcilerConfig:
    """Loaded config + the questions the observer actually asks of it."""

    def __init__(self, data: Optional[Dict] = None, *,
                 path: Optional[Path] = None,
                 sentinel: Optional[Path] = None) -> None:
        self.path = Path(os.path.expanduser(str(path or DEFAULT_CONFIG_PATH)))
        self.sentinel = Path(os.path.expanduser(str(sentinel or PASSIVE_SENTINEL)))
        self.data = _merge(DEFAULTS, data or {})

    # -- the three off-switches -------------------------------------------

    def passive_sentinel_present(self) -> bool:
        """True if the on-disk revert-to-4.1 marker exists.

        Any error reading it returns True — refusing to act. The safe default
        is to stay out of the way (§4.4 fail-CLOSED); a sentinel we cannot read
        must not be treated as absent.
        """
        try:
            return self.sentinel.is_file()
        except OSError:
            return True

    @property
    def enabled(self) -> bool:
        return bool(self.data.get("enabled", False))

    @property
    def dry_run(self) -> bool:
        return bool(self.data.get("dry_run", False))

    def may_act(self) -> bool:
        """The single question the observer asks before touching anything."""
        return self.enabled and not self.passive_sentinel_present()

    def action_enabled(self, name: str) -> bool:
        """Per-category switch. Unknown categories are never actionable."""
        if name not in ACTIONABLE:
            return False
        return bool((self.data.get("actions") or {}).get(name, False))

    # -- safety knobs ------------------------------------------------------

    @property
    def base_backoff(self) -> float:
        return float(self._rl("base_backoff_seconds", 30))

    @property
    def max_backoff(self) -> float:
        return float(self._rl("max_backoff_seconds", 240))

    @property
    def quarantine_threshold(self) -> int:
        return int(self._rl("quarantine_threshold", 3))

    @property
    def emergency_pause_seconds(self) -> float:
        return float(self.data.get("emergency_pause_seconds", 300))

    def _rl(self, key: str, default):
        return (self.data.get("rate_limit") or {}).get(key, default)

    def describe(self) -> str:
        mode = ("PASSIVE (sentinel present)" if self.passive_sentinel_present()
                else "DRY-RUN" if (self.enabled and self.dry_run)
                else "ACTIVE" if self.enabled else "DISABLED")
        return mode


def _merge(base: Dict, over: Dict) -> Dict:
    """Shallow-recursive merge so a partial config keeps the defaults."""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: Optional[Path] = None,
         sentinel: Optional[Path] = None) -> ReconcilerConfig:
    """Read the config. Missing or malformed → shipped defaults (disabled).

    Fail-SOFT to DISABLED, never to enabled: a corrupt config must not be able
    to switch the reconciler on, and a missing one is the normal fresh-deploy
    state. The one thing that must never happen is a parse error being read as
    permission to act.
    """
    p = Path(os.path.expanduser(str(path or DEFAULT_CONFIG_PATH)))
    try:
        data = json.loads(p.read_text())
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    return ReconcilerConfig(data, path=p, sentinel=sentinel)


def save(cfg_data: Dict, path: Optional[Path] = None) -> Path:
    """Write the config ATOMICALLY (temp file + rename in the same dir).

    `sb3-ctl reconciler enable` must never be able to leave a half-written
    config behind: a truncated file parses as {} and reads back as DISABLED,
    which is safe, but a partially-written one that happens to parse could
    enable a subset of actions nobody chose. rename(2) within a directory is
    atomic, so a reader sees either the old file or the new one.
    """
    p = Path(os.path.expanduser(str(path or DEFAULT_CONFIG_PATH)))
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".reconciler.", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(cfg_data, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p
