"""Generate a synthetic NFM IQ fixture (fc32 / complex64).

Carrier at `carrier_hz`, frequency-modulated by an audio `tone_hz` with peak
deviation `max_dev_hz`. The Phase 4a ground-band smoke test feeds this into
the daemon's FileIQSource and expects the demodulated audio to contain a
strong tone at `tone_hz`.

Usage:
    python3 -m chirp.tests.fixtures.make_nfm_iq \
        --out /tmp/nfm_smoke.iq \
        --samp-rate 1e6 \
        --duration 30 \
        --carrier 100e3 \
        --tone 500 \
        --max-dev 5e3
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np


def make_nfm_iq(
    samp_rate: float,
    duration_s: float,
    carrier_hz: float,
    tone_hz: float,
    max_dev_hz: float = 5e3,
    noise_sigma: float = 0.003,
    amplitude: float = 0.5,
    seed: int | None = 42,
) -> np.ndarray:
    """Build a baseband IQ vector where a carrier at `carrier_hz` is
    frequency-modulated by sin(2*pi*tone_hz*t) with peak deviation `max_dev_hz`.

    The transmitted phase is the integral of the instantaneous frequency:
        phi(t) = 2*pi * integral(carrier_hz + max_dev_hz * sin(2*pi*tone_hz*t)) dt
              = 2*pi * carrier_hz * t
                - (max_dev_hz / tone_hz) * cos(2*pi*tone_hz*t)
    """
    rng = np.random.default_rng(seed)
    n = int(round(samp_rate * duration_s))
    t = np.arange(n, dtype=np.float64) / samp_rate
    mod_index = max_dev_hz / tone_hz  # beta
    phase = (
        2.0 * np.pi * carrier_hz * t
        - mod_index * np.cos(2.0 * np.pi * tone_hz * t)
    )
    iq = (amplitude * np.exp(1j * phase)).astype(np.complex64)
    if noise_sigma > 0:
        noise = (
            rng.normal(0.0, noise_sigma, n)
            + 1j * rng.normal(0.0, noise_sigma, n)
        ).astype(np.complex64)
        iq = iq + noise
    return iq


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Synthesize an NFM IQ fixture")
    p.add_argument("--out", required=True, help="Output path (overwritten).")
    p.add_argument("--samp-rate", type=float, default=1e6)
    p.add_argument("--duration", type=float, default=30.0)
    p.add_argument("--carrier", type=float, default=100e3,
                   help="Carrier offset from baseband center (Hz).")
    p.add_argument("--tone", type=float, default=500.0,
                   help="Modulating audio tone (Hz).")
    p.add_argument("--max-dev", type=float, default=5e3,
                   help="Peak FM deviation (Hz). 5 kHz = mil-air NFM.")
    p.add_argument("--noise-sigma", type=float, default=0.003)
    p.add_argument("--amplitude", type=float, default=0.5)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args(argv)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    iq = make_nfm_iq(
        samp_rate=args.samp_rate,
        duration_s=args.duration,
        carrier_hz=args.carrier,
        tone_hz=args.tone,
        max_dev_hz=args.max_dev,
        noise_sigma=args.noise_sigma,
        amplitude=args.amplitude,
        seed=args.seed,
    )
    iq.tofile(out_path)
    bytes_written = out_path.stat().st_size
    print(
        f"wrote {bytes_written} bytes ({iq.size} complex samples = "
        f"{iq.size / args.samp_rate:.2f} s) to {out_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
