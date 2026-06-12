"""chirp.dsp.source_validator — Source contract validation for the SDR.

Phase 1 of the chirp data path rebuild (2026-06-12).

Goal: catch the "alive but useless" failure mode where ``sdrplay_api_Open``
returned success but the sample stream is degraded — flat zeros, all-ones
saturation, DC-only, or arriving at a fraction of the configured rate.
Today's only escape from that state is a Micro reboot, because every
downstream block (channels, demod, hit detector) trusts whatever bytes
the driver hands it.

Approach
--------
A small, opt-in validator block sits in parallel with the channel pool on
the SDR source's output port: ``source -> head(N) -> vector_sink``.  The
``head`` block emits the first N samples then stops, so the validator
branch is inert after the initial window — no ongoing CPU cost.  The
caller (the daemon, after ``start()``) calls
:py:func:`evaluate_capture` to compute mean magnitude, variance, DC
offset, saturation rate, and sample arrival rate, then compares them
against an envelope.  A violation raises :class:`SourceContractViolation`,
which the daemon turns into a structured journalctl event + a clean exit
so systemd's restart sees a deterministic failure instead of a stuck
process.

Gating
------
The validator is constructed only when ``CHIRP_SOURCE_VALIDATE=1``.  Zero
overhead by default.  The deployment plan is:

1. Ship the validator code (this module) inactive on the live box.
2. Enable on one band with a deliberately loose envelope.
3. Observe healthy operation; tighten the envelope to match.
4. Make the validator default-on once the envelope is honest.

Envelope shape (Phase 1 defaults — generous on purpose)
-------------------------------------------------------
- ``variance >= 1e-9``: detect a constant-sample wedge.
- ``abs(mean_real) < 0.5`` and ``abs(mean_imag) < 0.5``: bounded DC.
- ``saturation_rate < 0.5``: a real antenna under any plausible signal
  level stays below this.  ``>= 0.5`` is the catastrophic "driver returns
  ones" failure mode we're trying to catch.
- ``arrival_fraction >= 0.5``: the validator window should fill within
  2x the expected wall-clock time at the configured sample rate.

These thresholds will tighten in Phase 6 (soak + chaos) once we have
field data from healthy runs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional, Sequence

try:  # pragma: no cover — system-installed gnuradio
    from gnuradio import blocks, gr
except ImportError:  # pragma: no cover
    blocks = None  # type: ignore[assignment]
    gr = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceEnvelope:
    """Acceptable bounds on the SDR's sample-stream characteristics.

    Values OUTSIDE these bounds are treated as a source-contract violation.
    Loose by design (Phase 1) — tighten in Phase 6 once we have field data.
    """

    min_variance: float = 1e-9
    """Lowest variance the driver may return before we call it a wedge."""

    max_abs_mean_real: float = 0.5
    """Largest |mean(real)| allowed.  DC offset cap."""

    max_abs_mean_imag: float = 0.5
    """Largest |mean(imag)| allowed."""

    max_saturation_rate: float = 0.5
    """Largest allowed fraction of samples with |re|>0.95 or |im|>0.95.

    A real antenna under realistic signal level stays well below 0.5.
    The catastrophic 'driver returns ones/maxints' failure mode we're
    trying to catch produces values close to 1.0.
    """

    min_arrival_fraction: float = 0.5
    """Minimum (observed_samples / expected_samples) over the probe window.

    1.0 = perfect; 0.5 = the validator window filled in 2x the expected
    wall-clock time.  Below this is a strong "sample pump is starved"
    signal.
    """


# ---------------------------------------------------------------------------
# Validator block
# ---------------------------------------------------------------------------

class SourceContractValidator:
    """GR blocks that tap the SDR source's output for the first N samples.

    Wired in parallel with the channel pool — does NOT sit on the channel
    chain's path, so it can't violate the mixer's sync-block contract (the
    P0-1 lesson from 2026-06-12).  After N samples the head block emits
    EOS to its downstream vector_sink and stops consuming; the production
    branch is unaffected.

    Args:
        sample_rate: SDR sample rate (sps).  Used to size the probe window.
        window_s: how many seconds of samples to capture (default 0.2).
    """

    def __init__(self, sample_rate: float, window_s: float = 0.2) -> None:
        if blocks is None:
            raise RuntimeError(
                "gnuradio.blocks is not importable; cannot construct validator"
            )
        if sample_rate < 1e3 or window_s <= 0.0:
            raise ValueError(
                "SourceContractValidator: sample_rate must be > 1 kHz and "
                "window_s > 0"
            )
        self._sample_rate = float(sample_rate)
        self._window_s = float(window_s)
        self._n_samples = int(self._sample_rate * self._window_s)
        # head emits exactly n_samples then EOS.  vector_sink_c stores
        # complex64 samples into a list accessible via .data().
        self._head = blocks.head(gr.sizeof_gr_complex, self._n_samples)
        self._sink = blocks.vector_sink_c()

    # -- wiring ------------------------------------------------------------

    def connect_to(self, hier_block: "gr.hier_block2", source) -> None:
        """Wire ``source -> head -> sink`` inside ``hier_block``.

        ``source`` is the producer of complex samples (typically the
        osmocom_source).  The hier block's existing ``source -> output``
        edge is not touched — this is a parallel branch.
        """
        hier_block.connect(source, self._head, self._sink)

    # -- introspection -----------------------------------------------------

    @property
    def expected_samples(self) -> int:
        return self._n_samples

    @property
    def window_s(self) -> float:
        return self._window_s

    @property
    def sample_rate(self) -> float:
        return self._sample_rate

    # -- capture readback --------------------------------------------------

    def wait_for_window(
        self,
        max_wait_s: Optional[float] = None,
        poll_s: float = 0.05,
    ) -> tuple[Sequence[complex], float]:
        """Block until the vector_sink has captured ``expected_samples``
        complex samples, or until ``max_wait_s`` elapses.

        Default ``max_wait_s`` is ``2 * window_s`` (so an arrival rate
        of 50% is the lowest the validator will accept before timeout).

        Returns ``(samples, elapsed_wall_s)``.  The wall time is used by
        :py:func:`evaluate_capture` to compute the arrival fraction.
        """
        if max_wait_s is None:
            max_wait_s = max(self._window_s * 2.0, 0.5)
        target = self.expected_samples
        t0 = time.monotonic()
        deadline = t0 + max_wait_s
        while True:
            data = self._sink.data()
            elapsed = time.monotonic() - t0
            if len(data) >= target or elapsed >= max_wait_s:
                return data[:target] if len(data) >= target else data, elapsed
            time.sleep(poll_s)


# ---------------------------------------------------------------------------
# Stats + evaluation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CaptureStats:
    """What the validator computed from one window of samples."""

    n_samples: int
    elapsed_s: float
    arrival_fraction: float
    mean_real: float
    mean_imag: float
    variance: float
    saturation_rate: float

    def as_dict(self) -> dict:
        return {
            "n_samples": self.n_samples,
            "elapsed_s": round(self.elapsed_s, 4),
            "arrival_fraction": round(self.arrival_fraction, 4),
            "mean_real": round(self.mean_real, 6),
            "mean_imag": round(self.mean_imag, 6),
            "variance": round(self.variance, 9),
            "saturation_rate": round(self.saturation_rate, 6),
        }


@dataclass
class EvaluationResult:
    """Outcome of comparing capture stats against the envelope."""

    ok: bool
    stats: CaptureStats
    envelope: SourceEnvelope
    violations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "stats": self.stats.as_dict(),
            "envelope": {
                "min_variance": self.envelope.min_variance,
                "max_abs_mean_real": self.envelope.max_abs_mean_real,
                "max_abs_mean_imag": self.envelope.max_abs_mean_imag,
                "max_saturation_rate": self.envelope.max_saturation_rate,
                "min_arrival_fraction": self.envelope.min_arrival_fraction,
            },
            "violations": list(self.violations),
        }


def evaluate_capture(
    samples: Sequence[complex],
    elapsed_s: float,
    expected_samples: int,
    envelope: SourceEnvelope,
) -> EvaluationResult:
    """Pure function: compute stats from ``samples`` and compare to envelope.

    ``elapsed_s`` is the wall-clock time the validator's
    ``wait_for_window`` actually consumed.  ``expected_samples`` is the
    target window size (the head block's N).  Together they define the
    arrival fraction.

    No GR imports needed — this is pure arithmetic so it's trivially
    unit-testable.
    """
    n = len(samples)
    if n <= 0 or expected_samples <= 0:
        # No data → arrival_fraction=0, all other stats are degenerate.
        # Treat this as a hard violation.
        stats = CaptureStats(
            n_samples=0,
            elapsed_s=elapsed_s,
            arrival_fraction=0.0,
            mean_real=0.0,
            mean_imag=0.0,
            variance=0.0,
            saturation_rate=0.0,
        )
        return EvaluationResult(
            ok=False,
            stats=stats,
            envelope=envelope,
            violations=["no_samples_captured"],
        )

    # Online (single-pass) stats over real and imaginary parts.
    sum_r = 0.0
    sum_i = 0.0
    sum_sq = 0.0
    sat = 0
    for s in samples:
        re = s.real
        im = s.imag
        sum_r += re
        sum_i += im
        # Variance over magnitude-squared keeps the result on a sane scale
        # for both DC-stuck (var≈0) and saturated (var capped) failures.
        mag_sq = re * re + im * im
        sum_sq += mag_sq
        if abs(re) > 0.95 or abs(im) > 0.95:
            sat += 1

    mean_r = sum_r / n
    mean_i = sum_i / n
    mean_mag_sq = sum_sq / n
    # Variance about origin (E[|x|^2] - |E[x]|^2).  Bounded ≥ 0.
    variance = max(0.0, mean_mag_sq - (mean_r * mean_r + mean_i * mean_i))
    saturation_rate = sat / n
    arrival_fraction = n / expected_samples

    stats = CaptureStats(
        n_samples=n,
        elapsed_s=elapsed_s,
        arrival_fraction=arrival_fraction,
        mean_real=mean_r,
        mean_imag=mean_i,
        variance=variance,
        saturation_rate=saturation_rate,
    )

    violations: list[str] = []
    if variance < envelope.min_variance:
        violations.append(
            f"variance {variance:.3e} < min {envelope.min_variance:.3e}"
            " (constant-sample wedge)"
        )
    if abs(mean_r) > envelope.max_abs_mean_real:
        violations.append(
            f"|mean_real| {abs(mean_r):.3f} > max {envelope.max_abs_mean_real}"
            " (DC offset out of bounds)"
        )
    if abs(mean_i) > envelope.max_abs_mean_imag:
        violations.append(
            f"|mean_imag| {abs(mean_i):.3f} > max {envelope.max_abs_mean_imag}"
            " (DC offset out of bounds)"
        )
    if saturation_rate > envelope.max_saturation_rate:
        violations.append(
            f"saturation_rate {saturation_rate:.3f} > max"
            f" {envelope.max_saturation_rate} (driver returning"
            " ones/maxints?)"
        )
    if arrival_fraction < envelope.min_arrival_fraction:
        violations.append(
            f"arrival_fraction {arrival_fraction:.3f} < min"
            f" {envelope.min_arrival_fraction} (sample pump starved)"
        )

    return EvaluationResult(
        ok=len(violations) == 0,
        stats=stats,
        envelope=envelope,
        violations=violations,
    )


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class SourceContractViolation(RuntimeError):
    """Raised when the SDR's sample stream falls outside the envelope.

    The daemon catches this, emits a structured journalctl event, and
    exits non-zero so systemd's restart sees a deterministic failure
    instead of a stuck "alive but useless" process.
    """

    def __init__(self, result: EvaluationResult) -> None:
        self.result = result
        msg = (
            "source contract violated: "
            + "; ".join(result.violations)
            + f" (stats={result.stats.as_dict()})"
        )
        super().__init__(msg)


__all__ = [
    "CaptureStats",
    "EvaluationResult",
    "SourceContractValidator",
    "SourceContractViolation",
    "SourceEnvelope",
    "evaluate_capture",
]
