"""Resolver contract: never enqueue more trunked systems than digital tuners.

Regression guard for the Sea Isle City (NJICS) outage of 2026-06-01: the
``cape_may_shore`` favorite's geographic expansion enqueued *two* trunked
systems — NJICS and the NJ Turnpike system — while only one RSPduo was free
for digital.  The old resolver passed ``max_tuners=len(systems)`` to the
RSPduo prober, which synthesised a phantom second tuner on the *same* physical
box; the two op25 children then collided on the one SDR (master-less Slave ->
``status=1/FAILURE`` loop -> wedged sdrplay daemon) and NJICS never locked.

These tests cover the pure selection logic — :func:`_cap_systems_to_tuners`
and :func:`_system_has_operator_intent` — without touching hardware.
"""

import unittest

from ui import favorites_runtime as fr


def _sys(name: str, distance_first: bool = False) -> dict:
    # Minimal system dict as produced by _normalize_digital_pool.
    return {"name": name, "sites": [{"site_id": "x"}]}


# op25_system_config-style overrides: NJICS is the operator-configured
# ("intended") system for the Sea Isle City favorite; NJ Turnpike is only
# pulled in by proximity and carries no operator intent.
SIC_OVERRIDES = {
    "New Jersey Interoperability Communications System (NJICS)": {
        "nac": "0x39B",
        "site_policy": {"pinned_site_id": "21557"},
    },
    "New Jersey Turnpike Authority": {
        "site_policy": {"pinned_site_id": "", "preferred_site_ids": ["26743"]},
    },
}

NJICS = "New Jersey Interoperability Communications System (NJICS)"
TURNPIKE = "New Jersey Turnpike Authority"


class OperatorIntentTests(unittest.TestCase):
    def test_nac_marks_intent(self):
        self.assertTrue(fr._system_has_operator_intent(_sys(NJICS), SIC_OVERRIDES))

    def test_pinned_site_marks_intent(self):
        ov = {"S": {"site_policy": {"pinned_site_id": "100"}}}
        self.assertTrue(fr._system_has_operator_intent({"name": "S"}, ov))

    def test_no_override_is_not_intent(self):
        self.assertFalse(fr._system_has_operator_intent(_sys(TURNPIKE), SIC_OVERRIDES))

    def test_empty_pin_and_no_nac_is_not_intent(self):
        # preferred_site_ids alone (no pin, no nac) is not operator intent —
        # this is exactly the NJ Turnpike shape that must NOT win a tuner.
        self.assertFalse(fr._system_has_operator_intent(_sys(TURNPIKE), SIC_OVERRIDES))

    def test_missing_system_is_not_intent(self):
        self.assertFalse(fr._system_has_operator_intent({"name": "Unknown"}, SIC_OVERRIDES))


class CapSystemsToTunersTests(unittest.TestCase):
    def test_sic_oversubscription_keeps_njics_drops_turnpike(self):
        # Distance sort puts NJ Turnpike first (Swainton is geographically
        # closest), NJICS second — the exact order seen in the pre-fix
        # systems.json backup.  With one tuner the operator-intent ranking
        # must override distance and keep NJICS.
        systems = [_sys(TURNPIKE), _sys(NJICS)]
        kept = fr._cap_systems_to_tuners(systems, 1, SIC_OVERRIDES)
        self.assertEqual([s["name"] for s in kept], [NJICS])

    def test_not_oversubscribed_is_unchanged(self):
        systems = [_sys(NJICS)]
        kept = fr._cap_systems_to_tuners(systems, 1, SIC_OVERRIDES)
        self.assertEqual(kept, systems)

    def test_two_tuners_two_systems_keeps_both(self):
        systems = [_sys(TURNPIKE), _sys(NJICS)]
        kept = fr._cap_systems_to_tuners(systems, 2, SIC_OVERRIDES)
        self.assertEqual({s["name"] for s in kept}, {NJICS, TURNPIKE})

    def test_zero_or_unknown_tuners_does_not_truncate(self):
        # n_tuners <= 0 means the probe is unknown; preserve prior behavior
        # (emit all systems, let the allocator report no_dongles) rather than
        # silently dropping everything.
        systems = [_sys(TURNPIKE), _sys(NJICS)]
        self.assertEqual(fr._cap_systems_to_tuners(systems, 0, SIC_OVERRIDES), systems)

    def test_no_intent_falls_back_to_closest_first_order(self):
        # When no system has operator intent, the incoming closest-first order
        # is preserved as a stable tiebreaker and the tail is dropped.
        systems = [_sys("Closest"), _sys("Farther"), _sys("Farthest")]
        kept = fr._cap_systems_to_tuners(systems, 2, {})
        self.assertEqual([s["name"] for s in kept], ["Closest", "Farther"])

    def test_intent_wins_then_closest_fills_remaining_slots(self):
        # 3 systems, 2 tuners: NJICS (intent) kept first, then the closest of
        # the remaining non-intent systems fills the second slot.
        systems = [_sys(TURNPIKE), _sys(NJICS), _sys("Other")]
        kept = fr._cap_systems_to_tuners(systems, 2, SIC_OVERRIDES)
        self.assertEqual([s["name"] for s in kept], [NJICS, TURNPIKE])


if __name__ == "__main__":
    unittest.main()
