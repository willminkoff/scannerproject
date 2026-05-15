"""Scenario 1: ZIP -> favorites mode -> service tags -> scan pool sync.

Tests that the SB3 UI's ZIP-driven scan-pool workflow:
  1. Triggers the HP2 picker to write a fresh systems.json. ZIP/mode
     changes update the trunked-systems config; per the per-file
     content-diff write semantics in save_digital_editor_payload,
     systems.json is rewritten when the system set changes. Talkgroups.csv
     also typically advances (broader system set brings new TGs).
  2. Leaves op25 active (no kill-cascade)
  3. Preserves the disco-classifier process (warm cache intact, if running)
  4. Produces a non-empty systems.json with valid sites + control channels

NOTE on file-mtime expectations vs S3 (tag swap):
  ZIP/mode change   → BOTH systems.json AND talkgroups.csv typically advance
  Service-tag swap  → ONLY talkgroups.csv advances; systems.json is
                      intentionally preserved when trunked systems unchanged
                      (see test_service_tag_swap.py)

Generic — works from any ZIP. Does not assert specific frequencies or
system names; just shape and process invariants.

Run: pytest -s -v tests/integration/test_zip_to_favorites_to_scan.py
"""
from __future__ import annotations

import pytest

from .helpers import (
    assert_classifier_pid_unchanged,
    assert_no_watchdog_loop,
    assert_systems_json_changed,
    assert_systems_json_nonempty_with_sites,
    assert_unit_active,
    summarize_systems,
    voice_count_increased,
)


@pytest.mark.integration
@pytest.mark.requires_ssh
def test_zip_to_favorites_to_scan(
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
    assert_no_watchdog_loop(pre, max_cycles=1)

    print(f"\n[result file] {result_writer.path}")
    print(f"[pre] systems before: {list(summarize_systems(pre)) or '(none)'}")
    print(f"[pre] classifier PID: {pre['classifier']['main_pid'] or '(masked/inactive)'}")

    # --- User action ---
    # Prompt asks for THREE distinct changes (ZIP + service tag + scan
    # system). Empirically (May 13 2026 run), an "Apply favorites" with no
    # net change leaves systems.json mtime untouched — only rtl-airband
    # (analog) is rewritten. Forcing three independent changes guarantees
    # the digital picker rebuilds and systems.json gets a fresh write.
    prompt_user(
        "SCENARIO 1: ZIP → favorites → scan\n"
        "\n"
        "Action needed in SB3 UI (all three to force digital recompile):\n"
        "1. Change ZIP to a different one than current\n"
        "2. Change at least one service tag\n"
        "3. Change scan system (e.g. favorites → full DB or different favorites set)\n"
        "4. Click Apply / Save\n"
        "\n"
        "When sync has fired and op25 has restarted with new config, type \"done\".\n"
        "If anything goes wrong type \"abort\" with details."
    )

    # --- Wait for picker to fire (systems.json mtime advances) ---
    sync_observed = wait_for(
        lambda: state_sampler().get("systems_json_mtime_epoch", 0)
        > pre.get("systems_json_mtime_epoch", 0),
        timeout=30.0,
        interval=2.0,
    )
    assert sync_observed, (
        "systems.json mtime did not advance within 30s of UI action. "
        "Picker may not have run. Check that the UI sync action actually fired."
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
    assert_no_watchdog_loop(post, max_cycles=1)
    assert_classifier_pid_unchanged(pre, post)

    # --- Optional: did op25 manage to lock during a brief observation window? ---
    # This is informational, not a hard assertion. Coverage may legitimately
    # have no traffic during the test window; we record the result either way.
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
            "[lock] no voice traffic in window — this is informational only. "
            "Could be no live transmissions during the test window, or genuine "
            "lock failure. Inspect the JSONL for control_channel_timeout details."
        )
