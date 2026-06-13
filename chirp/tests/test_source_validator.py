"""Unit tests for :mod:`chirp.dsp.source_validator` — Phase 1 contract guard.

The validator's purpose is to catch the "alive but useless" failure mode
where the SDR has opened successfully but is returning junk samples
(constant wedge, all-ones saturation, DC-only, starved arrival).  These
tests cover the pure evaluation function ``evaluate_capture`` against
synthetic sample sequences that simulate each known failure mode plus a
healthy AWGN baseline.

GR-side wiring (``SourceContractValidator``) is covered by integration
tests in ``chirp/tests/test_phase4b.py`` and the live-box smoke test
after rollout; that side requires gnuradio + an SDR or fixture.
"""

from __future__ import annotations

import unittest

from chirp.dsp.source_validator import (
    SourceEnvelope,
    SourceContractViolation,
    evaluate_capture,
)


def _awgn(n: int, sigma: float = 0.01, seed: int = 0) -> list[complex]:
    """Synthesize n complex AWGN samples (white Gaussian noise).

    Uses a deterministic linear-congruential PRNG (no numpy dependency
    in the test module).
    """
    state = seed or 1
    out: list[complex] = []
    for _ in range(n):
        # Two uniforms via LCG, Box-Muller for normality.
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        u1 = max(state / 0x80000000, 1e-12)
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        u2 = state / 0x80000000
        # Box-Muller — approximate via cheap polynomial; this is plenty
        # for variance/mean tests where we only need O(1e-2) accuracy.
        import math
        r = math.sqrt(-2.0 * math.log(u1))
        re = sigma * r * math.cos(2 * math.pi * u2)
        im = sigma * r * math.sin(2 * math.pi * u2)
        out.append(complex(re, im))
    return out


class EvaluateCaptureBaselineTests(unittest.TestCase):
    """Healthy AWGN should pass the Phase 1 default envelope."""

    def setUp(self):
        self.envelope = SourceEnvelope()
        self.n = 4000  # ~2 ms at 2 Msps; plenty for stats
        self.samples = _awgn(self.n, sigma=0.01, seed=42)

    def test_healthy_awgn_passes_envelope(self):
        result = evaluate_capture(
            samples=self.samples,
            elapsed_s=0.2,
            expected_samples=self.n,
            envelope=self.envelope,
        )
        self.assertTrue(result.ok, msg=f"violations={result.violations}")
        self.assertGreater(result.stats.variance, 0.0)
        self.assertAlmostEqual(result.stats.arrival_fraction, 1.0, places=3)
        self.assertEqual(result.stats.saturation_rate, 0.0)


class EvaluateCaptureFailureModeTests(unittest.TestCase):
    """Each known driver-junk failure mode must trip the right violation."""

    def setUp(self):
        self.envelope = SourceEnvelope()

    def test_constant_zero_wedge_trips_variance(self):
        # The 'driver returns zeros' wedge → variance = 0 → constant-sample.
        samples = [complex(0.0, 0.0)] * 4000
        result = evaluate_capture(
            samples=samples, elapsed_s=0.2,
            expected_samples=4000, envelope=self.envelope,
        )
        self.assertFalse(result.ok)
        self.assertTrue(
            any("variance" in v for v in result.violations),
            msg=f"violations={result.violations}",
        )

    def test_dc_offset_trips_mean(self):
        # Strong DC offset (constant offset, no noise) → both variance AND
        # mean trip; check the mean violation explicitly.
        samples = [complex(0.8, 0.0)] * 4000
        result = evaluate_capture(
            samples=samples, elapsed_s=0.2,
            expected_samples=4000, envelope=self.envelope,
        )
        self.assertFalse(result.ok)
        self.assertTrue(
            any("mean_real" in v for v in result.violations),
            msg=f"violations={result.violations}",
        )

    def test_all_ones_saturation_trips_saturation(self):
        # The catastrophic 'driver returns ones/maxints' mode — every
        # sample at the edge of the dynamic range.
        samples = [complex(1.0, 1.0)] * 4000
        result = evaluate_capture(
            samples=samples, elapsed_s=0.2,
            expected_samples=4000, envelope=self.envelope,
        )
        self.assertFalse(result.ok)
        self.assertTrue(
            any("saturation_rate" in v for v in result.violations),
            msg=f"violations={result.violations}",
        )

    def test_starved_arrival_trips_arrival_fraction(self):
        # The sample pump only delivered 30% of the expected window.
        samples = _awgn(1200, sigma=0.01, seed=7)
        result = evaluate_capture(
            samples=samples, elapsed_s=0.2,
            expected_samples=4000, envelope=self.envelope,
        )
        self.assertFalse(result.ok)
        self.assertTrue(
            any("arrival_fraction" in v for v in result.violations),
            msg=f"violations={result.violations}",
        )

    def test_no_samples_at_all(self):
        # Source emitted nothing during the window — hard failure.
        result = evaluate_capture(
            samples=[], elapsed_s=0.5,
            expected_samples=4000, envelope=self.envelope,
        )
        self.assertFalse(result.ok)
        self.assertIn("no_samples_captured", result.violations)


class EvaluateCaptureEnvelopeOverrideTests(unittest.TestCase):
    """An operator can tighten the envelope — make sure overrides apply."""

    def test_tightened_variance_threshold_fails_quiet_signal(self):
        # AWGN at sigma=0.001 → variance ~ 2e-6 (sum of two squared
        # gaussians).  A tightened envelope catches it.
        samples = _awgn(4000, sigma=0.001, seed=11)
        tight = SourceEnvelope(min_variance=1e-3)
        result = evaluate_capture(
            samples=samples, elapsed_s=0.2,
            expected_samples=4000, envelope=tight,
        )
        self.assertFalse(result.ok)
        self.assertTrue(
            any("variance" in v for v in result.violations),
            msg=f"violations={result.violations}",
        )

    def test_loosened_saturation_lets_high_signal_pass(self):
        # AWGN at sigma=0.5 → many samples spill past 0.95 → tightened
        # envelope fails; loosened envelope passes.
        samples = _awgn(4000, sigma=0.6, seed=13)
        loose = SourceEnvelope(max_saturation_rate=0.95)
        result = evaluate_capture(
            samples=samples, elapsed_s=0.2,
            expected_samples=4000, envelope=loose,
        )
        self.assertTrue(result.ok, msg=f"violations={result.violations}")


class SourceContractViolationTests(unittest.TestCase):
    """The exception carries the structured result for journalctl."""

    def test_exception_message_includes_violations(self):
        samples = [complex(0.0, 0.0)] * 4000
        result = evaluate_capture(
            samples=samples, elapsed_s=0.2,
            expected_samples=4000, envelope=SourceEnvelope(),
        )
        exc = SourceContractViolation(result)
        msg = str(exc)
        self.assertIn("source contract violated", msg)
        self.assertIn("variance", msg)
        self.assertIs(exc.result, result)


if __name__ == "__main__":
    unittest.main()
