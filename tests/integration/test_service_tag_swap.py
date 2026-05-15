"""Scenario 3: Service tag swap — change ONE tag, no ZIP/mode change.

Tests that toggling a service tag (e.g. add Fire Dispatch, remove
Aircraft) updates ONLY the TG whitelist (talkgroups.csv), not the
underlying trunked systems config (systems.json). This is intentional
behavior of sync_scan_pool_to_digital_runtime + save_digital_editor_payload:
per-file content-diff writes mean systems.json is preserved when only
the TG list changes, and talkgroups.csv is rewritten in its place.

Assertions:
  1. systems.json mtime is UNCHANGED (trunked systems didn't change)
  2. talkgroups.csv mtime ADVANCED (TG whitelist did change)
  3. systems.json still non-empty with valid sites/control channels
  4. op25 stays active across the cascade
  5. classifier preserved (skipped if masked)

Generic — no hardcoded freqs / system names / tag names.

Run: pytest -s -v tests/integration/test_service_tag_swap.py
"""
from __future__ import annotations

import pytest

from .helpers import (
    assert_classifier_pid_unchanged,
    assert_no_watchdog_loop,
    assert_systems_json_nonempty_with_sites,
    assert_systems_json_unchanged,
    assert_talkgroups_csv_changed,
    assert_unit_active,
    summarize_systems,
    voice_count_increased,
)


@pytest.mark.integration
@pytest.mark.requires_ssh
def test_service_tag_swap(
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
    print(f"[pre] systems.json mtime: {pre.get('systems_json_mtime_epoch')}")
    print(f"[pre] talkgroups.csv mtime: {pre.get('talkgroups_csv_mtime_epoch')}")
    print(f"[pre] classifier PID: {pre['classifier']['main_pid'] or '(masked/inactive)'}")

    # --- User action ---
    prompt_user(
        "SCENARIO 3: Service tag swap\n"
        "\n"
        "Action needed in SB3 UI:\n"
        "1. Keep current ZIP and current scan mode UNCHANGED\n"
        "2. Change ONE service tag (add or remove just one — e.g. toggle\n"
        "   Fire Dispatch on, or Aircraft off)\n"
        "3. Click Apply / Save\n"
        "\n"
        "When sync has fired and op25 has restarted, type \"done\".\n"
        "If anything goes wrong type \"abort\" with details."
    )

    # --- Wait for talkgroups.csv to be rewritten ---
    # Tag swaps update the TG whitelist (talkgroups.csv), NOT the
    # trunked-systems config (systems.json). The picker correctly
    # leaves systems.json untouched when only the TG set changes.
    sync_observed = wait_for(
        lambda: state_sampler().get("talkgroups_csv_mtime_epoch", 0)
        > pre.get("talkgroups_csv_mtime_epoch", 0),
        timeout=60.0,
        interval=2.0,
    )
    assert sync_observed, (
        "talkgroups.csv mtime did not advance within 60s of UI action. "
        "Service tag toggle may not have changed the TG whitelist "
        "(empty toggle? or sync was a content-identical no-op)."
    )

    # --- Post-state ---
    post = state_sampler()
    result_writer({"phase": "post", "state": post})
    assert post.get("ssh_ok"), f"Post-action SSH state sample failed: {post}"

    print(f"[post] systems after: {list(summarize_systems(post)) or '(none)'}")
    print(f"[post] systems.json mtime: {post.get('systems_json_mtime_epoch')} "
          f"(unchanged from pre — expected)")
    print(f"[post] talkgroups.csv mtime: {post.get('talkgroups_csv_mtime_epoch')} "
          f"(advanced from pre — expected)")

    # --- Process invariants ---
    assert_unit_active(post, "scanner-digital-op25")
    assert_no_watchdog_loop(post, max_cycles=2)
    assert_classifier_pid_unchanged(pre, post)
    assert_systems_json_nonempty_with_sites(post)

    # --- S3-specific: TG whitelist changed, trunked config did not ---
    # save_digital_editor_payload uses per-file content-diff writes.
    # Tag-only changes leave systems.json untouched and rewrite
    # talkgroups.csv. Asserting both invariants explicitly.
    assert_systems_json_unchanged(pre, post)
    assert_talkgroups_csv_changed(pre, post)

    # --- Optional voice-lock wait ---
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
