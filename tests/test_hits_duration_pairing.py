"""/api/hits reported duration 0.0 and peak null for every chirp hit.

chirp writes two records per transmission and they carry different fields
(chirp/hit_detector.py):

    hit_start  ts, ch, freq_mhz, level_dbfs, warmup, cluster_center_hz
    hit_end    ts, ch, freq_mhz, start_ts_ms, end_ts_ms, duration_s,
               peak_dbfs, warmup, cluster_center_hz

duration_s and peak_dbfs exist only on hit_end -- both are computed when the
squelch closes. The route read hit_start and nothing else, so those two fields
could never be populated on any band. Not a missing value: a structural
impossibility.

Cost of that, 2026-11-08: a 9.14 s automatic repeater identifier on GMRS 15
(462.5500), recurring every 30 minutes since 2026-09-21, surfaced as a run of
zero-duration nulls and was investigated as a software artifact. The real
duration was one record further down the same log.

The fixtures below are real record shapes copied from neptune's
ground.out.log, not invented ones.
"""
from __future__ import annotations

import ast
import io
import json
import unittest
from pathlib import Path

_ROUTES = Path(__file__).resolve().parents[1] / "sb3" / "ui" / "routes.py"
_SRC = io.open(_ROUTES, encoding="utf-8").read()


_WANT_CONSTS = {"_IN_FLIGHT_MAX_S"}


def _load(name):
    """exec one module-level function out of routes.py (it imports heavy deps).

    Module-level constants the function uses as argument defaults are pulled in
    too -- those are evaluated at def time, so omitting them raises NameError
    on compile rather than at call time.
    """
    tree = ast.parse(_SRC)
    body = [
        n for n in tree.body
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", "") in _WANT_CONSTS for t in n.targets)
    ]
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == name), None)
    if fn is None:
        raise AssertionError("%s() not found in routes.py" % name)
    ns = {}
    exec(compile(ast.Module(body=body + [fn], type_ignores=[]), "<sub>", "exec"), ns)
    return ns[name]


_merge = _load("_merge_chirp_hit_records")

# Verbatim shapes from neptune's ground.out.log.
START = {"v": 1, "evt": "hit_start", "ts": 1793926912.99854,
         "ch": "n4-3-mtears", "freq_mhz": 442.8,
         "level_dbfs": -48.00514123273814, "warmup": True,
         "cluster_center_hz": 442800000.0}
END = {"v": 1, "evt": "hit_end", "ts": 1793926913.201344,
       "ch": "n4-3-mtears", "freq_mhz": 442.8,
       "start_ts_ms": 1793926912998, "end_ts_ms": 1793926913201,
       "duration_s": 0.203, "peak_dbfs": -48.01, "warmup": True,
       "cluster_center_hz": 442800000.0}

# The GMRS 15 identifier that started all this.
GMRS15_START = {"v": 1, "evt": "hit_start", "ts": 1794146668.026,
                "ch": "bs-gmrs-0", "freq_mhz": 462.55,
                "level_dbfs": -56.94, "warmup": False}
GMRS15_END = {"v": 1, "evt": "hit_end", "ts": 1794146677.166,
              "ch": "bs-gmrs-0", "freq_mhz": 462.55,
              "start_ts_ms": 1794146668026, "end_ts_ms": 1794146677166,
              "duration_s": 9.14, "peak_dbfs": -56.94, "warmup": False}


def _log(*objs):
    return "\n".join(json.dumps(o) for o in objs) + "\n"


class PairingTests(unittest.TestCase):
    def test_a_completed_hit_yields_one_entry_with_its_duration(self):
        merged = _merge(_log(START, END))
        self.assertEqual(len(merged), 1, "start+end is ONE transmission, not two")
        (ch, start_ms), rec, in_flight = merged[0]
        self.assertEqual(ch, "n4-3-mtears")
        self.assertEqual(start_ms, 1793926912998)
        self.assertFalse(in_flight)
        self.assertEqual(rec["duration_s"], 0.203)
        self.assertEqual(rec["peak_dbfs"], -48.01)

    def test_the_key_derived_from_hit_start_matches_hit_end(self):
        """int(ts*1000) must equal start_ts_ms or nothing ever pairs."""
        self.assertEqual(int(START["ts"] * 1000), END["start_ts_ms"])
        self.assertEqual(int(GMRS15_START["ts"] * 1000), GMRS15_END["start_ts_ms"])

    def test_hit_end_alone_still_works(self):
        """The 200 kB tail routinely clips the start but keeps the end."""
        merged = _merge(_log(END))
        self.assertEqual(len(merged), 1)
        _, rec, in_flight = merged[0]
        self.assertFalse(in_flight)
        self.assertEqual(rec["duration_s"], 0.203)

    def test_hit_start_alone_is_in_flight_not_dropped(self):
        """A transmission still open must still be visible.

        now_s is passed explicitly because the fixtures carry real historical
        timestamps; against the wall clock they would age out as orphans (see
        OrphanedStartTests).
        """
        merged = _merge(_log(START), now_s=START["ts"] + 2.0)
        self.assertEqual(len(merged), 1)
        _, rec, in_flight = merged[0]
        self.assertTrue(in_flight)
        self.assertNotIn("duration_s", rec)
        self.assertIn("level_dbfs", rec)

    def test_the_gmrs15_identifier_reports_nine_seconds(self):
        """The regression that mattered: this must not read as 0.0 s."""
        merged = _merge(_log(GMRS15_START, GMRS15_END))
        self.assertEqual(len(merged), 1)
        _, rec, _ = merged[0]
        self.assertAlmostEqual(rec["duration_s"], 9.14, places=2)
        self.assertAlmostEqual(rec["peak_dbfs"], -56.94, places=2)

    def test_results_are_sorted_by_start_time(self):
        a = dict(GMRS15_END)
        b = dict(END)
        merged = _merge(_log(a, b))               # a is LATER than b
        starts = [k[1] for k, _, _ in merged]
        self.assertEqual(starts, sorted(starts))

    def test_same_channel_two_transmissions_stay_distinct(self):
        e2 = dict(END, start_ts_ms=END["start_ts_ms"] + 60000,
                  end_ts_ms=END["end_ts_ms"] + 60000, duration_s=1.5)
        merged = _merge(_log(END, e2))
        self.assertEqual(len(merged), 2)
        self.assertEqual(sorted(r["duration_s"] for _, r, _ in merged), [0.203, 1.5])

    def test_distinct_channels_at_the_same_instant_stay_distinct(self):
        """The overload bursts open many channels in one tick."""
        recs = [dict(END, ch="bs-gmrs-%d" % n) for n in range(5)]
        merged = _merge(_log(*recs))
        self.assertEqual(len(merged), 5)


class OrphanedStartTests(unittest.TestCase):
    """A hit_start stranded by a daemon restart is not a live transmission.

    chirp-ground was restarted twice during the 2026-11-08 deploy. A channel
    that was squelch-open at that moment never got its hit_end, so the first
    cut of this fix rendered it as in_flight with a duration of 9323 s.
    """

    NOW = 1794146668.026 + 10_000.0    # 10 ks after GMRS15_START opened

    def test_a_stale_unpaired_start_is_dropped(self):
        self.assertEqual(_merge(_log(GMRS15_START), now_s=self.NOW), [])

    def test_a_fresh_unpaired_start_is_kept(self):
        fresh = GMRS15_START["ts"] + 3.0
        merged = _merge(_log(GMRS15_START), now_s=fresh)
        self.assertEqual(len(merged), 1)
        self.assertTrue(merged[0][2], "should still be marked in_flight")

    def test_a_completed_hit_is_never_dropped_by_age(self):
        """Only UNPAIRED starts are aged out; history must survive."""
        merged = _merge(_log(GMRS15_START, GMRS15_END), now_s=self.NOW)
        self.assertEqual(len(merged), 1)
        self.assertFalse(merged[0][2])
        self.assertAlmostEqual(merged[0][1]["duration_s"], 9.14, places=2)

    def test_boundary_is_inclusive(self):
        edge = GMRS15_START["ts"] + 300.0
        self.assertEqual(len(_merge(_log(GMRS15_START), now_s=edge)), 1)
        self.assertEqual(len(_merge(_log(GMRS15_START), now_s=edge + 0.5)), 0)

    def test_the_bound_can_be_disabled(self):
        self.assertEqual(
            len(_merge(_log(GMRS15_START), now_s=self.NOW, in_flight_max_s=0)), 1)

    def test_default_bound_is_well_past_scan_hold_max(self):
        """Ground caps a hold at 15 s, so 300 s cannot clip a real hit."""
        import re
        m = re.search(r"_IN_FLIGHT_MAX_S = ([0-9.]+)", _SRC)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(float(m.group(1)), 60.0)


class RobustnessTests(unittest.TestCase):
    def test_other_event_types_are_ignored(self):
        noise = {"v": 1, "evt": "cluster_hop", "ts": 1793926912.0,
                 "center_hz": 442800000.0}
        merged = _merge(_log(noise, START, END))
        self.assertEqual(len(merged), 1)

    def test_garbage_lines_do_not_raise(self):
        data = ("not json at all\n"
                + json.dumps(END) + "\n"
                + '{"evt":"hit_end","start_ts_ms":"not-a-number"}\n'
                + '{"evt":"hit_start","ts":null}\n'
                + "[]\n\n")
        merged = _merge(data)
        self.assertEqual(len(merged), 1, "the one good record must survive")

    def test_a_record_with_no_timestamp_is_dropped(self):
        """It cannot be keyed, paired or sorted -- emitting it stamps 1970."""
        self.assertEqual(_merge(_log({"evt": "hit_start", "ts": None,
                                      "ch": "x", "freq_mhz": 1.0})), [])
        self.assertEqual(_merge(_log({"evt": "hit_end", "start_ts_ms": 0,
                                      "ch": "x", "duration_s": 1.0})), [])

    def test_empty_input(self):
        self.assertEqual(_merge(""), [])

    def test_duplicate_records_collapse(self):
        merged = _merge(_log(END, END, START, START))
        self.assertEqual(len(merged), 1)


class RouteWiringTests(unittest.TestCase):
    def setUp(self):
        self.src = _SRC

    def test_route_calls_the_helper(self):
        self.assertIn("_merge_chirp_hit_records(data)", self.src)

    def test_route_no_longer_filters_to_hit_start_only(self):
        self.assertNotRegex(
            self.src, r'if evt != "hit_start":\s*\n\s*continue',
            "that filter is what made duration and peak unreachable")

    def test_duration_is_not_hardcoded_from_hit_start(self):
        self.assertNotIn('"duration_s": float(obj.get("duration_s") or 0.0),', self.src,
                         "this was a duplicate dict key that always evaluated to 0.0")

    def test_emitted_item_carries_the_in_flight_flag(self):
        self.assertIn('"in_flight": in_flight,', self.src)

    def test_duration_key_appears_once_per_item(self):
        """The old dict literal set duration_s twice; the second won."""
        block = self.src.split("merged[-keep:]", 1)[1][:2200]
        self.assertEqual(block.count('"duration_s":'), 1,
                         "duration_s must be set exactly once in the item dict")


if __name__ == "__main__":
    unittest.main()
