"""Generate a synthetic AM-modulated complex IQ fixture (fc32 / complex64).

Usage:
    python3 -m chirp.tests.fixtures.make_test_iq \
        --out /tmp/am_smoke.iq \
        --samp-rate 1e6 \
        --duration 30 \
        --carrier 200e3 \
        --tone 1000

Writes interleaved float32 I/Q pairs (GNU Radio's native fc32). The Phase 1
smoke test uses this to feed the daemon's FileIQSource at +200 kHz.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import numpy as np


def make_am_iq(
    samp_rate: float,
    duration_s: float,
    carrier_hz: float,
    tone_hz: float,
    mod_index: float = 0.8,
    noise_sigma: float = 0.003,
    seed: int | None = 42,
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(round(samp_rate * duration_s))
    t = np.arange(n, dtype=np.float64) / samp_rate
    envelope = 0.5 * (1.0 + mod_index * np.sin(2 * np.pi * tone_hz * t))
    carrier = np.exp(2j * np.pi * carrier_hz * t)
    iq = (envelope * carrier).astype(np.complex64)
    if noise_sigma > 0:
        noise = (
            rng.normal(0.0, noise_sigma, n)
            + 1j * rng.normal(0.0, noise_sigma, n)
        ).astype(np.complex64)
        iq = iq + noise
    return iq


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Synthesize an AM IQ fixture")
    p.add_argument("--out", required=True, help="Output path (will be overwritten).")
    p.add_argument("--samp-rate", type=float, default=1e6)
    p.add_argument("--duration", type=float, default=30.0)
    p.add_argument("--carrier", type=float, default=200e3,
                   help="Carrier offset from baseband center (Hz).")
    p.add_argument("--tone", type=float, default=1000.0,
                   help="Modulating audio tone (Hz).")
    p.add_argument("--mod-index", type=float, default=0.8)
    p.add_argument("--noise-sigma", type=float, default=0.003)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args(argv)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    iq = make_am_iq(
        samp_rate=args.samp_rate,
        duration_s=args.duration,
        carrier_hz=args.carrier,
        tone_hz=args.tone,
        mod_index=args.mod_index,
        noise_sigma=args.noise_sigma,
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
