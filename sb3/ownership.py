"""sb3.ownership — the §4.2 state-ownership boundary, encoded as data.

This module is the executable form of the ownership diagram in
``docs/sb3-neptune-architecture.md`` §4.2.  It is deliberately pure data plus
pure functions: no side effects, no subprocess, no imports beyond stdlib.  A
diagram in a doc drifts silently; a table that ``status`` renders and a test
asserts against does not.

The boundary, in one sentence:

    SB3 owns the SB3 layer.  On ``sb3-ctl kill``, SB3 leaves.  SDRangel and
    SDRTrunk keep running and keep producing audio, holding their last-applied
    config.

Why that is safe (§4.2): **SB3 never holds audio state.**  It only *asserts*
state onto backends that then hold it themselves.  Kill the asserter and the
assertions stand.  That property should be protected deliberately, not relied
on by luck — which is what the ``BACKEND`` set below is for.

⚠️  POLARITY WARNING.  ``macos/killswitch/`` (imported as prior art in 7bf15f3)
contains a tool called ``sdr-killswitch`` whose ``kill`` means the OPPOSITE of
this one.  That tool tears the scanner down *in order to hand the radios TO*
SDRangel for a human to drive — both its ``release`` and ``scanner`` paths quit
SDRangel.  Here, quitting SDRangel is the one action §4.2 forbids.  Same word,
opposite meaning.  Do not merge the two vocabularies; "kill" belongs to SB3.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, List, NamedTuple

# ---------------------------------------------------------------------------
# The launchd label sets — the boundary itself
# ---------------------------------------------------------------------------

#: Everything SB3 owns.  ``sb3-ctl kill`` stops all of these, and only these.
SB3_LAYER: FrozenSet[str] = frozenset({
    "com.scannerproject.sb3-broker",       # Phase 1 stub -> Phase 2 `python3 -m broker`
    "com.scannerproject.sb3-controller",   # Phase 1 stub -> Phase 2 reconciler
    "com.scannerproject.tuner-broker",
    "com.scannerproject.sb3-reconciler",
    "com.scannerproject.sb3-ui",
    "com.scannerproject.disco",
    "com.scannerproject.acars",
    "com.scannerproject.survey",
})

#: Labels whose plist templates live in ``macos/launchd/sb3/`` and that
#: ``sb3-ctl install`` would write to ``~/Library/LaunchAgents/``.
#:
#: These are the Phase 1 lifecycle placeholders (``sb3/agents/``): idle loops
#: that log a heartbeat and exit cleanly on SIGTERM.  They do no SDR work and
#: hold no leases.  They exist so ``kill``'s teardown ordering has a real process
#: to stop — enabling ``--execute`` against an empty SB3 layer would prove
#: nothing, and a kill switch first exercised against load-bearing processes is
#: one tested in production.
#:
#: NOT INSTALLED.  ``install`` is dry-run only until Phase 1.1 (§6).
MANAGED_AGENTS: Dict[str, str] = {
    "com.scannerproject.sb3-broker": "sb3.agents.broker_stub",
    "com.scannerproject.sb3-controller": "sb3.agents.controller_stub",
    "com.scannerproject.sb3-ui": "sb3.ui",
}

#: Where the plist templates live in-repo.
PLIST_TEMPLATE_DIR = "macos/launchd/sb3"

#: The backend.  ``sb3-ctl kill`` must NEVER touch these — they are what keeps
#: producing audio while SB3 is gone.  This set is the whole point of the
#: exercise; treat additions to it as load-bearing.
BACKEND: FrozenSet[str] = frozenset({
    "com.scannerproject.sdrangel",
    "com.scannerproject.sdrtrunk",
    "com.scannerproject.icecast",
    "com.scannerproject.neptune-audio-bridge",
    "com.scannerproject.neptune-ground-bridge",   # Ground role's ffmpeg bridge
                                                  # (Phase 3.3). Sibling of the Air
                                                  # bridge; plumbing that survives
                                                  # kill. Was added as a plist +
                                                  # GUARDED_MOUNTS entry but missed
                                                  # here, so classify() fail-closed
                                                  # and blocked every kill/resume
                                                  # once RunAtLoad loaded it.
    "com.scannerproject.venus-audio-bridge",
    "com.scannerproject.copytoudp-watchdog",
    "com.scannerproject.caffeinate",
})

#: Teardown order for ``kill`` (§4.3).  Lease consumers FIRST, broker LAST.
#:
#: Each consumer runs under ``broker.client run … -- <proc>`` and the lease IS
#: the open socket, so a clean stop closes the socket = clean release.  Killing
#: the broker first would yank the socket out from under a live child — which is
#: undefined, and exactly the churn the broker exists to prevent.  This ordering
#: is the part §6 warns "is hard to retrofit", so it is committed now even
#: though most of these agents do not exist yet.
KILL_ORDER: tuple = (
    "com.scannerproject.sb3-controller",   # stop asserting first
    "com.scannerproject.sb3-reconciler",
    "com.scannerproject.sb3-ui",
    "com.scannerproject.disco",            # ── lease consumers ──
    "com.scannerproject.acars",
    "com.scannerproject.survey",
    "com.scannerproject.tuner-broker",     # ── brokers LAST ──
    "com.scannerproject.sb3-broker",
)

#: Mounts whose continuity is the invariant `kill` exists to protect (§4.3
#: step 6).  A kill switch that does not verify these is a wish.
GUARDED_MOUNTS: tuple = (
    "neptune-trunk.mp3",
    "neptune-analog.mp3",  # Analog scanner mount (neptune-angel.mp3 -> neptune-air.mp3
                           # 2026-07-19 -> neptune-analog.mp3 2026-07-21, renamed now
                           # that VFO gets its own neptune-vfo.mp3). kill must guard the
                           # mount the analog profile streams to, or a live analog mount
                           # would go unprotected across a kill.
    "neptune-ground.mp3",  # Ground role (Phase 3.3). When a Ground profile is
                           # live it is 200 and kill guards it; when no Ground is
                           # loaded it is 404 and the verify treats it as
                           # "already down; not ours" — safe either way.
)


class StateRow(NamedTuple):
    """One row of the §4.2 ownership table."""

    state: str
    owner: str
    on_kill: str


#: The §4.2 table, verbatim, as data.  ``sb3-ctl status`` renders this.
STATE_TABLE: tuple = (
    StateRow("Profile definitions, bindings, active set", "SB3", "on disk, inert"),
    StateRow("disco classifications, ACARS messages, hit log", "SB3", "on disk; collection stops"),
    StateRow("Web UI", "SB3", "GONE — by design"),
    StateRow("Reconciler", "SB3", "GONE — by design"),
    StateRow("Device leases, policy, flocks", "SB3 (broker)", "GONE — by design"),
    StateRow("Deviceset config, channels, gain, center", "SDRangel (RAM)", "untouched, still running"),
    StateRow("copyToUDP tap", "SDRangel (RAM)", "untouched"),
    StateRow("Tap-arming watchdog", "backend plumbing", "stays up"),
    StateRow("Playlist, aliases, streams, live decode", "SDRTrunk (disk+RAM)", "untouched"),
    StateRow("ffmpeg bridge, icecast, mounts", "launchd (independent)", "untouched"),
)


# ---------------------------------------------------------------------------
# The live check — this is what keeps the boundary honest
# ---------------------------------------------------------------------------

class UnclassifiedLabel(Exception):
    """A com.scannerproject.* label belongs to neither set.

    This is a HARD failure, not a warning.  An unclassified agent is one that
    ``kill`` has no opinion about — which is how you find out at Phase 4 that
    the boundary was never real (§6).  Classify it, on purpose, in this file.
    """


def classify(label: str) -> str:
    """Return 'sb3' | 'backend' for a label, or raise UnclassifiedLabel."""
    if label in SB3_LAYER:
        return "sb3"
    if label in BACKEND:
        return "backend"
    raise UnclassifiedLabel(
        f"{label!r} is in neither SB3_LAYER nor BACKEND. Every "
        f"com.scannerproject.* agent must be classified deliberately in "
        f"sb3/ownership.py — an unclassified agent is one `sb3-ctl kill` has no "
        f"opinion about. Add it to the correct set."
    )


def classify_all(labels) -> Dict[str, List[str]]:
    """Bucket labels into sb3/backend/unclassified. Never raises."""
    out: Dict[str, List[str]] = {"sb3": [], "backend": [], "unclassified": []}
    for label in labels:
        try:
            out[classify(label)].append(label)
        except UnclassifiedLabel:
            out["unclassified"].append(label)
    for v in out.values():
        v.sort()
    return out


def kill_sequence(loaded_labels) -> List[str]:
    """The labels `kill` would stop, in §4.3 order, filtered to what is loaded.

    Order comes from KILL_ORDER, never from the caller's iteration order.
    """
    loaded = set(loaded_labels)
    return [label for label in KILL_ORDER if label in loaded]


def assert_disjoint() -> None:
    """SB3_LAYER and BACKEND must never overlap."""
    overlap = SB3_LAYER & BACKEND
    if overlap:
        raise AssertionError(
            f"ownership sets overlap: {sorted(overlap)} — a label cannot be both "
            f"killed and preserved"
        )
