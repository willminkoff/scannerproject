"""Reusable assertion helpers for integration tests.

All helpers take a *state* dict produced by the `state_sampler` fixture in
conftest.py. They raise AssertionError with a useful message on failure;
otherwise return None.

Helpers are intentionally generic — they don't bake in location-specific
frequencies or system names, so they work from any ZIP / scan-pool.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional


def assert_unit_active(state: Dict[str, Any], unit: str) -> None:
    actual = state.get("services", {}).get(unit)
    assert actual == "active", (
        f"Expected unit {unit!r} to be active, got {actual!r}. "
        f"All sampled services: {state.get('services')}"
    )


def assert_unit_inactive(state: Dict[str, Any], unit: str) -> None:
    actual = state.get("services", {}).get(unit)
    assert actual in ("inactive", "failed"), (
        f"Expected unit {unit!r} to be inactive/failed, got {actual!r}."
    )


def assert_systems_json_nonempty_with_sites(state: Dict[str, Any]) -> None:
    """Generic assertion: systems.json has at least one system, every system
    has at least one site, every site has at least one control_channel."""
    systems = state.get("systems", [])
    assert systems, (
        "systems.json is empty (picker emitted no systems). "
        f"Sampler returned: {state.get('systems_json_parse_error') or 'no systems key'}"
    )
    for sys_entry in systems:
        name = sys_entry.get("name", "<unnamed>")
        sites = sys_entry.get("sites") or []
        assert sites, f"system {name!r} has no sites"
        for site in sites:
            ccs = site.get("control_channels_hz") or []
            assert ccs, (
                f"system {name!r} site {site.get('site_id')!r} "
                f"({site.get('site_name')!r}) has no control_channels_hz"
            )


def assert_systems_json_has(state: Dict[str, Any], system_name: str) -> None:
    """Specific assertion: a particular system name is present. Use sparingly
    — most assertions should be generic so the test works from any ZIP."""
    names = [s.get("name") for s in state.get("systems", [])]
    assert system_name in names, (
        f"Expected system {system_name!r} in systems.json. "
        f"Found: {names}"
    )


def assert_multiple_systems(state: Dict[str, Any], min_systems: int = 2) -> None:
    """Assert systems.json has at least min_systems systems. Useful for
    full-DB-mode tests where the picker is expected to emit a broader
    scan pool than a tight favorites pick.

    Default min_systems=2 — full DB mode should bring back more than one
    system. Bump higher if your test has stronger expectations."""
    systems = state.get("systems", [])
    actual = len(systems)
    assert actual >= min_systems, (
        f"Expected >= {min_systems} systems in systems.json (full DB mode), "
        f"got {actual}: {[s.get('name') for s in systems]}"
    )


def assert_any_system_has_multiple_sites(state: Dict[str, Any], min_sites: int = 2) -> None:
    """Assert at least one system has multiple sites. Proves HP2's
    multi-site emission is working — full DB mode should produce at
    least one system with several site candidates (Davidson Simulcast
    + Mount Juliet + ... etc).

    Default min_sites=2 — at least one system must have >=2 sites for
    the assertion to pass. The other systems may be single-site."""
    systems = state.get("systems", [])
    assert systems, "systems.json is empty"
    max_sites_seen = max((len(s.get("sites") or []) for s in systems), default=0)
    assert max_sites_seen >= min_sites, (
        f"Expected at least one system with >= {min_sites} sites "
        f"(HP2 multi-site emission). Max sites in any system: "
        f"{max_sites_seen}. Systems: "
        f"{[(s.get('name'), len(s.get('sites') or [])) for s in systems]}"
    )


# One legitimate sync-driven restart_digital() call produces a fixed
# sequence of ~8 sudo systemctl events targeting scanner-digital-op25 and
# sdrplay:
#   1. systemctl stop scanner-digital-op25-audio
#   2. systemctl stop scanner-digital-op25
#   3. systemctl kill -s SIGKILL scanner-digital-op25
#   4. systemctl kill -s SIGKILL scanner-digital-op25-audio
#   5. systemctl reset-failed scanner-digital-op25 scanner-digital-op25-audio
#   6. systemctl restart sdrplay
#   7. systemctl start scanner-digital-op25
#   8. systemctl restart scanner-digital-op25-audio
# A watchdog kill-loop fires this cascade repeatedly (~30-40s apart), so
# 16+ events in a 60s window indicates >=2 cycles -> a loop, not a single
# legitimate restart.
_CASCADE_EVENTS_PER_CYCLE = 8


def assert_no_watchdog_loop(state: Dict[str, Any], max_cycles: int = 2) -> None:
    """Asserts the airband-ui watchdog isn't cycling op25 in a loop.

    Counts sudo systemctl kill/restart events in the sampled 60s window
    targeting scanner-digital-op25 or sdrplay, then divides by 8 (one
    full cascade cycle = ~8 events) to estimate cycle count.

    Default max_cycles=2 — empirically (S1 runs, May 13 2026), ONE HP2
    picker firing reliably produces TWO restart_digital() cascades
    back-to-back with ~42s gap. The threshold accommodates that. Pass
    max_cycles=1 if you want a stricter assertion that a single sync
    produced exactly one restart (will fail on normal sync behavior —
    use only for observational tests with no UI action).

    Use this in place of the older assert_no_kill_cascade which counted
    raw events and was too strict for tests that legitimately trigger a
    sync.
    """
    events = state.get("kill_cascade_count_60s", 0)
    cycles = events // _CASCADE_EVENTS_PER_CYCLE
    assert cycles <= max_cycles, (
        f"Watchdog kill-loop detected: {events} sudo systemctl events in "
        f"last 60s ~ {cycles} cascade cycles (max allowed: {max_cycles}). "
        f"One cycle is ~8 events (stop+kill+reset+sdrplay-restart+start). "
        f">={max_cycles + 1} cycles in 60s indicates the airband-ui "
        f"watchdog is cycling op25 instead of leaving it stable."
    )


def assert_no_kill_cascade(state: Dict[str, Any], max_count: int = 8) -> None:
    """DEPRECATED: prefer assert_no_watchdog_loop.

    Kept as a thin wrapper for backwards-compat with tests that want raw
    event-count semantics. Default raised from 0 to 8 (one full legitimate
    cascade) so a legitimate sync doesn't trip it. Pass max_count=0 only
    if you want to assert "absolutely no restart activity at all" (e.g.
    a pure observation test that performs no UI action).
    """
    actual = state.get("kill_cascade_count_60s", 0)
    assert actual <= max_count, (
        f"Kill-cascade events: {actual} in last 60s (max allowed: {max_count})."
    )


def assert_classifier_pid_unchanged(
    state_before: Dict[str, Any], state_after: Dict[str, Any]
) -> None:
    """Asserts disco-classifier MainPID is the same before/after. Skips the
    check if classifier wasn't running before (PID 0 / unset) — that's a
    legitimate masked-classifier scenario, not a regression."""
    pid_before = state_before.get("classifier", {}).get("main_pid", 0)
    pid_after = state_after.get("classifier", {}).get("main_pid", 0)
    if not pid_before:
        return
    assert pid_after == pid_before, (
        f"Classifier was restarted: PID {pid_before} -> {pid_after}. "
        f"Warm cache lost during the test action."
    )


def assert_systems_json_changed(
    state_before: Dict[str, Any], state_after: Dict[str, Any]
) -> None:
    """Asserts systems.json mtime advanced between two samples.

    Use when the action SHOULD change trunked systems / sites / control
    channels (ZIP change, mode change, system add/remove). For TG-only
    changes (service tag swap), the digital picker correctly leaves
    systems.json untouched — use assert_systems_json_unchanged instead.
    """
    mtime_before = state_before.get("systems_json_mtime_epoch", 0)
    mtime_after = state_after.get("systems_json_mtime_epoch", 0)
    assert mtime_after > mtime_before, (
        f"systems.json mtime did not advance: before={mtime_before} after={mtime_after}. "
        f"The UI action may not have triggered a picker re-run, or the action only "
        f"affected the TG whitelist (use assert_talkgroups_csv_changed instead)."
    )


def assert_systems_json_unchanged(
    state_before: Dict[str, Any], state_after: Dict[str, Any]
) -> None:
    """Asserts systems.json mtime did NOT advance — the intentional behavior
    for TG-only changes (service tag swaps).

    sync_scan_pool_to_digital_runtime + save_digital_editor_payload use
    per-file content-diff write semantics: systems.json is only rewritten
    when trunked systems / sites / control channels change. A service-tag
    swap that adds/removes TGIDs but leaves the underlying trunked
    systems alone will correctly leave systems.json untouched while
    rewriting talkgroups.csv (see assert_talkgroups_csv_changed).
    """
    mtime_before = state_before.get("systems_json_mtime_epoch", 0)
    mtime_after = state_after.get("systems_json_mtime_epoch", 0)
    assert mtime_after == mtime_before, (
        f"systems.json mtime unexpectedly advanced: before={mtime_before} "
        f"after={mtime_after}. The action was expected to be TG-only "
        f"(intentionally skips systems.json rewrite). If trunked systems "
        f"actually changed, use assert_systems_json_changed instead."
    )


def assert_talkgroups_csv_changed(
    state_before: Dict[str, Any], state_after: Dict[str, Any]
) -> None:
    """Asserts talkgroups.csv mtime advanced — the file that captures TG
    whitelist changes.

    save_digital_editor_payload rewrites talkgroups.csv whenever the TG
    list differs from disk. For service-tag swaps that add/remove TGIDs
    without changing trunked systems, this is the file that should
    advance (NOT systems.json).
    """
    mtime_before = state_before.get("talkgroups_csv_mtime_epoch", 0)
    mtime_after = state_after.get("talkgroups_csv_mtime_epoch", 0)
    assert mtime_after > mtime_before, (
        f"talkgroups.csv mtime did not advance: before={mtime_before} "
        f"after={mtime_after}. The UI action may not have changed the TG "
        f"whitelist (or the sync was an exact no-op)."
    )


def voice_count_increased(
    state_before: Dict[str, Any], state_after: Dict[str, Any]
) -> bool:
    """Returns True if op25 decoded more voice updates between the two
    samples. Useful as a wait_for predicate; not an assert because lock
    success depends on real RF traffic during the test window."""
    before = state_before.get("op25_log_voice_recent_count", 0)
    after = state_after.get("op25_log_voice_recent_count", 0)
    return after > before


def summarize_systems(state: Dict[str, Any]) -> Iterable[str]:
    """Human-readable one-line summaries of each system in systems.json.
    Useful for printing into JSONL records / test output."""
    for sys_entry in state.get("systems", []):
        name = sys_entry.get("name", "<unnamed>")
        sites = sys_entry.get("sites") or []
        for site in sites:
            ccs = site.get("control_channels_hz") or []
            yield (
                f"{name} | site {site.get('site_id')} {site.get('site_name')!r} "
                f"| {len(ccs)} control channels"
            )
