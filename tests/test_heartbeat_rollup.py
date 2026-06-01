"""Heartbeat rollup contract: the overall state must reflect EVERY evidence row.

Regression guard for the Phase R1 "lying heartbeat" bug: ``/api/heartbeat``
reported "All systems healthy / 13 checks pass" while embedded evidence rows
said "Waterfall dongle A DOWN" (bad) and "/ANALOG.mp3 byte rate 0 B" (warn).
The old rollup only consulted the core-pipeline ``wedged_reasons`` list and
ignored the per-dongle / byte-rate evidence rows entirely.

These cover the pure rollup helper without touching hardware or HTTP.
"""

import unittest

from ui import handlers as h


def _row(label, status, value="x"):
    return {"label": label, "status": status, "value": value}


class HeartbeatRollupTest(unittest.TestCase):
    def test_all_ok_is_quiet(self):
        evidence = [_row("stats file", "ok"), _row("airband-ui", "ok")]
        state, _worst = h._heartbeat_rollup_state(evidence, [])
        self.assertEqual(state, "quiet")

    def test_warn_row_downgrades_to_degraded(self):
        # The "/ANALOG.mp3 byte rate 0 B" frame-gap warn must move the badge
        # off QUIET even though no core service is down.
        evidence = [_row("stats file", "ok"),
                    _row("/ANALOG.mp3 byte rate", "warn", "0 B in 1.5s (frame gap)")]
        state, worst = h._heartbeat_rollup_state(evidence, [])
        self.assertEqual(state, "degraded")
        self.assertEqual(worst["label"], "/ANALOG.mp3 byte rate")

    def test_bad_dongle_row_is_wedged_even_without_core_reason(self):
        # "Waterfall dongle A DOWN" is a bad row but not a core wedged_reason;
        # it must still flip the badge to WEDGED.
        evidence = [_row("stats file", "ok"),
                    _row("Waterfall dongle A", "bad", "DOWN")]
        state, worst = h._heartbeat_rollup_state(evidence, [])
        self.assertEqual(state, "wedged")
        self.assertEqual(worst["label"], "Waterfall dongle A")

    def test_core_wedged_reason_forces_wedged(self):
        # Even if every visible row were healthy, a core wedged_reason wins.
        evidence = [_row("stats file", "ok")]
        state, _worst = h._heartbeat_rollup_state(evidence, ["rtl-airband-airband inactive"])
        self.assertEqual(state, "wedged")

    def test_bad_outranks_warn_for_worst_row(self):
        evidence = [_row("byte rate", "warn"), _row("dongle A", "bad", "DOWN")]
        state, worst = h._heartbeat_rollup_state(evidence, [])
        self.assertEqual(state, "wedged")
        self.assertEqual(worst["label"], "dongle A")

    def test_info_and_unknown_statuses_are_healthy(self):
        evidence = [_row("vfo", "info"), _row("misc", "mysterious")]
        state, _worst = h._heartbeat_rollup_state(evidence, [])
        self.assertEqual(state, "quiet")

    def test_summarize_row_formats_label_value(self):
        self.assertEqual(
            h._heartbeat_summarize_row({"label": "dongle A", "value": "DOWN"}),
            "dongle A: DOWN",
        )
        self.assertEqual(h._heartbeat_summarize_row(None), "")


if __name__ == "__main__":
    unittest.main()
