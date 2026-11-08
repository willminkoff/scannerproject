"""Band-scan hits were labelled by pool slot, not by channel.

2026-11-08 bench test: the operator keyed GMRS channel 1 (462.5625) and the
hit list came back as "Bs Gmrs 0" ... "Bs Gmrs 10" -- "None of them GMRS 1 as
i'd expect".

The frequencies were never wrong. The GMRS preset sweeps 462.5500-462.7250 in
12.5 kHz steps, which lands exactly on the real plan. But the plan is two
INTERLEAVED 25 kHz combs -- channels 15-22 on the .5500 comb and 1-7 on the
.5625 comb -- so the Nth slot is not the Nth channel. Slot 0 is channel 15.

sb3/ui/routes.py pulls in dependencies this box does not have, so these tests
exec just the label helpers out of its AST.
"""
from __future__ import annotations

import ast
import io
import unittest
from pathlib import Path

_ROUTES = Path(__file__).resolve().parents[1] / "sb3" / "ui" / "routes.py"
_WANT_FUNCS = {"_build_gmrs_plan", "_channel_plan_label", "_pretty_chirp_label"}
_WANT_CONSTS = {"_GMRS_PLAN_MHZ", "_PLAN_TOLERANCE_MHZ"}


def _load_helpers():
    tree = ast.parse(io.open(_ROUTES, encoding="utf-8").read())
    body = [
        n for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name in _WANT_FUNCS)
        or (isinstance(n, ast.Assign)
            and any(getattr(t, "id", "") in _WANT_CONSTS for t in n.targets))
    ]
    missing = _WANT_FUNCS - {n.name for n in body if isinstance(n, ast.FunctionDef)}
    if missing:
        raise AssertionError("routes.py is missing %s" % sorted(missing))
    ns = {}
    exec(compile(ast.Module(body=body, type_ignores=[]), "<subset>", "exec"), ns)
    return ns


_NS = _load_helpers()
_pretty = _NS["_pretty_chirp_label"]
_plan_label = _NS["_channel_plan_label"]
_PLAN = _NS["_GMRS_PLAN_MHZ"]

# What the preset actually loads: buildLinearBandFrequencies(462.55, 462.725, .0125)
_PRESET = [round(462.5500 + i * 0.0125, 4) for i in range(15)]

# 47 CFR 95: the two interleaved 462 MHz combs.
_EXPECTED = {
    462.5500: "GMRS 15", 462.5625: "GMRS 1",  462.5750: "GMRS 16",
    462.5875: "GMRS 2",  462.6000: "GMRS 17", 462.6125: "GMRS 3",
    462.6250: "GMRS 18", 462.6375: "GMRS 4",  462.6500: "GMRS 19",
    462.6625: "GMRS 5",  462.6750: "GMRS 20", 462.6875: "GMRS 6",
    462.7000: "GMRS 21", 462.7125: "GMRS 7",  462.7250: "GMRS 22",
}


class PresetFrequencyTests(unittest.TestCase):
    def test_every_preset_slot_is_a_real_gmrs_channel(self):
        """The sweep was never the bug -- pin that so nobody 'fixes' it."""
        for f in _PRESET:
            with self.subTest(freq=f):
                self.assertIn(f, _EXPECTED, "%.4f is not a GMRS channel" % f)

    def test_the_preset_covers_the_whole_462_plan(self):
        self.assertEqual(sorted(_PRESET), sorted(_EXPECTED))


class LabelTests(unittest.TestCase):
    def test_each_slot_gets_its_real_channel_number(self):
        for i, f in enumerate(_PRESET):
            with self.subTest(slot=i, freq=f):
                self.assertEqual(_pretty("bs-gmrs-%d" % i, f), _EXPECTED[f])

    def test_slot_index_is_not_the_channel_number(self):
        """The specific confusion the operator hit: slot 0 is channel 15."""
        self.assertEqual(_pretty("bs-gmrs-0", 462.5500), "GMRS 15")

    def test_the_channel_that_was_keyed_reads_as_channel_1(self):
        self.assertEqual(_pretty("bs-gmrs-1", 462.5625), "GMRS 1")

    def test_hand_named_favourites_keep_their_names(self):
        """Only generated bs-* ids are relabelled."""
        self.assertEqual(_pretty("n4-0-tower", 118.600), "Tower")
        self.assertEqual(_pretty("kbna-tower", 462.5625), "Kbna Tower")

    def test_off_plan_bandscan_channel_falls_back(self):
        self.assertEqual(_pretty("bs-rail-3", 160.2450), "Bs Rail 3")

    def test_missing_frequency_falls_back(self):
        self.assertEqual(_pretty("bs-gmrs-0"), "Bs Gmrs 0")

    def test_empty_id_still_empty(self):
        self.assertEqual(_pretty("", 462.5625), "")


class PlanTests(unittest.TestCase):
    def test_frs_only_channels_are_named(self):
        self.assertEqual(_plan_label(467.5625), "FRS 8")
        self.assertEqual(_plan_label(467.7125), "FRS 14")

    def test_repeater_inputs_are_distinguished_from_outputs(self):
        self.assertEqual(_plan_label(462.5500), "GMRS 15")
        self.assertEqual(_plan_label(467.5500), "GMRS 15 in")

    def test_plan_has_no_duplicate_frequencies(self):
        self.assertEqual(len(_PLAN), 30, "7 + 8 + 7 + 8 = 30 assignments")

    def test_plan_has_no_duplicate_names(self):
        names = list(_PLAN.values())
        self.assertEqual(len(names), len(set(names)))

    def test_near_miss_within_tolerance_matches(self):
        """Hit-log frequencies are floats; a few Hz of drift must still match."""
        self.assertEqual(_plan_label(462.56249), "GMRS 1")

    def test_a_genuinely_different_frequency_does_not_match(self):
        self.assertEqual(_plan_label(462.5700), "")
        self.assertEqual(_plan_label(155.1600), "")

    def test_rubbish_input_is_safe(self):
        for bad in (None, "", "abc", 0, -1):
            with self.subTest(value=bad):
                self.assertEqual(_plan_label(bad), "")


if __name__ == "__main__":
    unittest.main()
