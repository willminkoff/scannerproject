"""Scenario 2: ZIP -> full_database -> scan.

Variant of Scenario 1 with mode=full_database. Tests that:
  1. HP2 picker fires and rewrites systems.json
  2. systems.json contains MULTIPLE systems (broader than favorites)
  3. At least one system has MULTIPLE sites (HP2 multi-site emission)
  4. op25 stays active across the sync's restart cascade(s)
  5. The disco-classifier process is preserved (if running)

Generic — works from any ZIP. Uses the bumped assert_no_watchdog_loop
default (max_cycles=2) to match observed one-sync-produces-two-cascades
behavior documented in Scenario 1.

Run: pytest -s -v tests/integration/test_zip_to_full_db_to_scan.py
"""
from __future__ import annotations

import pytest

from .helpers import (
    assert_any_system_has_multiple_sites,
    assert_classifier_pid_unchanged,
    assert_multiple_systems,
    assert_no_watchdog_loop,
    assert_systems_json_changed,
    assert_systems_json_nonempty_with_sites,
    assert_unit_active,
    summarize_systems,
    voice_count_increased,
)


@pytest.mark.integration
@pytest.mark.requires_ssh
def test_zip_to_full_db_to_scan(
    state_sampler,
    prompt_user,
    wait_for,
    result_writer,
):
    # --- Pre-state ---
    pre = state_sampler()
    result_writer({"phase": "pre", "state": pre})
    assert pre.get("ssh_ok"), f"Initial SSH state sample failed: {pre}"

    assert_unit_active(pre, "airband-ui")
    assert_unit_active(pre, "scanner-digital-op25")
    assert_no_watchdog_loop(pre, max_cycles=2)

    print(f"\n[result file] {result_writer.path}")
    print(f"[pre] systems before: {list(summarize_systems(pre)) or '(none)'}")
    print(f"[pre] classifier PID: {pre['classifier']['main_pid'] or '(masked/inactive)'}")

    # --- User action ---
    prompt_user(
        "SCENARIO 2: ZIP → full DB → scan\n"
        "\n"
        "Action needed in SB3 UI:\n"
        "1. Change ZIP to a different one than current\n"
        "2. Switch mode to FULL DATABASE\n"
        "3. Pick service tags (Law Dispatch, Aircraft, etc. — your typical)\n"
        "4. Click Apply / Save\n"
        "\n"
        "When sync has fired and op25 has restarted, type \"done\".\n"
        "If anything goes wrong type \"abort\" with details."
    )

    # --- Wait for picker to fire AND the full cascade to complete ---
    # Scenario 1 documented that one sync triggers ~2 restart cycles over
    # ~97s. We wait 120s here to be sure both cycles have landed before
    # taking the post-state snapshot, otherwise the kill-cascade counts
    # would be mid-flight.
    sync_observed = wait_for(
        lambda: state_sampler().get("systems_json_mtime_epoch", 0)
        > pre.get("systems_json_mtime_epoch", 0),
        timeout=120.0,
        interval=3.0,
    )
    assert sync_observed, (
        "systems.json mtime did not advance within 120s of UI action. "
        "Picker may not have run."
    )

    # --- Post-state ---
    post = state_sampler()
    result_writer({"phase": "post", "state": post})
    assert post.get("ssh_ok"), f"Post-action SSH state sample failed: {post}"

    print(f"[post] systems after: {list(summarize_systems(post)) or '(none)'}")

    # --- Process invariants ---
    assert_systems_json_changed(pre, post)
    assert_systems_json_nonempty_with_sites(post)
    assert_unit_active(post, "scanner-digital-op25")
    assert_no_watchdog_loop(post, max_cycles=2)
    assert_classifier_pid_unchanged(pre, post)

    # --- Full-DB-specific assertions ---
    assert_multiple_systems(post, min_systems=2)
    assert_any_system_has_multiple_sites(post, min_sites=2)

    # --- Optional voice-lock wait (longer than S1 because full DB has
    # more talkgroup activity in flight at any given moment) ---
    locked = wait_for(
        lambda: voice_count_increased(post, state_sampler()),
        timeout=120.0,
        interval=5.0,
    )
    final = state_sampler()
    result_writer({
        "phase": "lock_attempt",
        "locked_within_120s": locked,
        "voice_count_pre": pre.get("op25_log_voice_recent_count", 0),
        "voice_count_post": post.get("op25_log_voice_recent_count", 0),
        "voice_count_final": final.get("op25_log_voice_recent_count", 0),
        "state": final,
    })
    print(f"[lock] voice traffic decoded within 120s: {locked}")
    if not locked:
        print(
            "[lock] no voice traffic in window. Could be transmission lull, "
            "or genuine lock failure. Inspect JSONL for control_channel_timeout."
        )
