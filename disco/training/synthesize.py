"""Synthesize labeled IQ slices that mimic disco's deployed input view.

Target: 1 MS/s, 1024 samples per slice, peak-normalized — i.e. exactly what
the deployed classifier feeds into the ONNX model after its TARGET_RATE_HZ
resample + central crop + peak normalize.

Output: HDF5 at /Volumes/1tb/disco-training/data/disco_synth_v1.hdf5
        X shape (N, 1024, 2) float32 (real, imag)
        Y shape (N,) int64
        attribute "classes" = json list of class names

Usage: python synthesize.py [--per-class 5000] [--seed 0] [--validate]
"""
import argparse
import json
import os
import time

import h5py
import numpy as np
from scipy.signal import resample_poly, firwin, lfilter

CLASSES_DISCO = [
    "NOISE",
    "FM_BROADCAST",
    "FM_NARROW",
    "AM_VOICE",
    "DMR",
    "GMSK",
    "QPSK",
    "OQPSK",
    "QAM",
    "OOK",
]

# Target output sample rate (matches deployed classifier TARGET_RATE_HZ default)
FS_OUT = 1_000_000.0  # 1 MS/s
N_SAMP = 1024  # samples per slice (~ 1 ms at 1 MS/s)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def _audio_source(rng, n, fs, max_freq):
    """Bandlimited-noise stand-in for audio. Returns real array, normalized to ~unit peak.

    Used to drive FM_BROADCAST/FM_NARROW deviation and AM modulation index. The
    output is real-valued, lowpass-filtered white noise with cutoff at max_freq.
    """
    # Generate at fs, lowpass to max_freq.
    raw = rng.standard_normal(n).astype(np.float32)
    nyq = fs / 2.0
    cutoff = min(max_freq, nyq * 0.95)
    # FIR length scales with steepness need; 81 taps is a safe default.
    taps = firwin(81, cutoff / nyq)
    audio = lfilter(taps, [1.0], raw).astype(np.float32)
    pk = np.max(np.abs(audio)) + 1e-12
    return audio / pk


def _rrc(beta, sps, span):
    """Root-raised-cosine pulse, length span*sps+1, normalized to unit energy."""
    N = span * sps
    t = (np.arange(-N // 2, N // 2 + 1)) / sps
    eps = 1e-9
    out = np.empty_like(t, dtype=np.float64)
    for i, ti in enumerate(t):
        if abs(ti) < eps:
            out[i] = 1.0 - beta + 4.0 * beta / np.pi
        elif abs(abs(4.0 * beta * ti) - 1.0) < 1e-6:
            out[i] = (
                beta
                / np.sqrt(2.0)
                * (
                    (1.0 + 2.0 / np.pi) * np.sin(np.pi / (4.0 * beta))
                    + (1.0 - 2.0 / np.pi) * np.cos(np.pi / (4.0 * beta))
                )
            )
        else:
            num = np.sin(np.pi * ti * (1.0 - beta)) + 4.0 * beta * ti * np.cos(np.pi * ti * (1.0 + beta))
            den = np.pi * ti * (1.0 - (4.0 * beta * ti) ** 2)
            out[i] = num / den
    out /= np.sqrt(np.sum(out ** 2))
    return out.astype(np.float32)


def _gaussian(bt, sps, span):
    """Gaussian pulse for GMSK-style filtering. BT product, span symbols."""
    alpha = np.sqrt(np.log(2.0) / 2.0) / bt
    N = span * sps
    t = np.arange(-N // 2, N // 2 + 1) / sps
    g = (np.sqrt(np.pi) / alpha) * np.exp(-((np.pi * t / alpha) ** 2))
    g /= np.sum(g)
    return g.astype(np.float32)


def _add_awgn(rng, sig, snr_db):
    """Add complex AWGN at given SNR (signal power vs noise power)."""
    p_sig = np.mean(np.abs(sig) ** 2) + 1e-20
    p_noise = p_sig / (10.0 ** (snr_db / 10.0))
    n = (rng.standard_normal(len(sig)) + 1j * rng.standard_normal(len(sig))).astype(np.complex64)
    n *= np.sqrt(p_noise / 2.0)
    return sig + n


def _resample_to_out(sig, fs_in):
    """Resample signal at fs_in to FS_OUT using polyphase. Sig is complex."""
    if abs(fs_in - FS_OUT) < 1.0:
        return sig
    # Find rational up/down. Round to integer ratio that matches within ~0.1%.
    from fractions import Fraction

    frac = Fraction(FS_OUT / fs_in).limit_denominator(1000)
    up = frac.numerator
    down = frac.denominator
    return resample_poly(sig, up, down).astype(np.complex64)


def _take_slice(sig, n=N_SAMP, rng=None):
    """Take a random n-sample window from sig, padding/truncating as needed."""
    if len(sig) >= n:
        if rng is None:
            start = (len(sig) - n) // 2
        else:
            start = rng.integers(0, len(sig) - n + 1)
        return sig[start : start + n]
    pad = np.zeros(n, dtype=sig.dtype)
    pad[: len(sig)] = sig
    return pad


def _impair_and_finalize(rng, sig):
    """Apply freq offset, phase offset, amplitude scale, AWGN, then peak normalize."""
    f_off = rng.uniform(-2000.0, 2000.0)
    phase = rng.uniform(0.0, 2.0 * np.pi)
    amp = rng.uniform(0.3, 1.0)
    snr_db = rng.uniform(12.0, 50.0)

    n = len(sig)
    t = np.arange(n) / FS_OUT
    sig = sig * np.exp(1j * (2.0 * np.pi * f_off * t + phase)).astype(np.complex64) * amp
    sig = _add_awgn(rng, sig, snr_db)

    # Peak-normalize (matches classifier preprocessing on the Micro).
    pk = np.max(np.abs(sig)) + 1e-12
    sig = sig / pk
    return sig.astype(np.complex64), snr_db


# ----------------------------------------------------------------------------
# Per-class generators (all return a 1024-complex64 array, post-impair, post-norm)
# ----------------------------------------------------------------------------
def gen_noise(rng):
    sig = (rng.standard_normal(N_SAMP) + 1j * rng.standard_normal(N_SAMP)).astype(np.complex64)
    pk = np.max(np.abs(sig)) + 1e-12
    return (sig / pk).astype(np.complex64), float("nan")


def _fm_signal(rng, dev_hz, audio_max):
    """Return complex64 FM signal at FS_OUT with peak deviation ~dev_hz."""
    # Audio is bandlimited; normalized to ~unit peak by _audio_source.
    audio = _audio_source(rng, N_SAMP * 4, FS_OUT, audio_max)
    # Phase = 2*pi * dev * integral(audio) where audio is bounded in [-1, 1].
    # Discrete integration: phase[n] = 2*pi*dev * sum(audio)/fs.
    phase = 2.0 * np.pi * dev_hz * np.cumsum(audio) / FS_OUT
    # Remove linear drift (DC of audio after lowpass leaves residual mean).
    n = len(phase)
    t = np.arange(n)
    slope = (phase[-1] - phase[0]) / (n - 1)
    phase = phase - slope * t
    return np.exp(1j * phase).astype(np.complex64)


def gen_fm_broadcast(rng):
    bw = rng.uniform(150e3, 200e3)
    dev = bw / 2.0
    sig = _fm_signal(rng, dev_hz=dev, audio_max=15e3)
    sig = _take_slice(sig, rng=rng)
    return _impair_and_finalize(rng, sig)


def gen_fm_narrow(rng):
    bw = rng.uniform(8e3, 25e3)
    dev = bw / 2.0
    audio_max = max(1.5e3, dev * 0.6)
    sig = _fm_signal(rng, dev_hz=dev, audio_max=audio_max)
    sig = _take_slice(sig, rng=rng)
    return _impair_and_finalize(rng, sig)


def gen_am_voice(rng):
    bw = rng.uniform(4e3, 10e3)
    audio_max = bw / 2.0
    m = rng.uniform(0.7, 0.95)
    audio = _audio_source(rng, N_SAMP * 2, FS_OUT, audio_max)
    env = (1.0 + m * audio).astype(np.float32)
    # Carrier at DC. Real envelope makes this DSB-WC at baseband.
    sig = env.astype(np.complex64)
    sig = _take_slice(sig, rng=rng)
    return _impair_and_finalize(rng, sig)


def gen_dmr(rng):
    sym_rate = 4800.0
    sps = int(round(FS_OUT / sym_rate))
    fs_in = sps * sym_rate  # ~999_840 Hz, close to 1 MS/s
    # 4-FSK deviations: ±648, ±1944 Hz
    devs = np.array([-1944.0, -648.0, 648.0, 1944.0], dtype=np.float64)
    n_syms = N_SAMP // sps + 8
    syms = devs[rng.integers(0, 4, size=n_syms)]
    # Upsample as freq-vs-time, gaussian-smooth a little.
    f_t = np.repeat(syms, sps)
    g = _gaussian(0.5, sps, span=4)
    f_t = lfilter(g, [1.0], f_t)
    # Optional TDMA bursting: ~50% of slices have a gap.
    if rng.random() < 0.4:
        gap_start = rng.integers(0, max(1, len(f_t) - 200))
        gap_len = rng.integers(50, 250)
        f_t[gap_start : gap_start + gap_len] *= 0.05
    phase = 2.0 * np.pi * np.cumsum(f_t) / fs_in
    env = np.where(np.abs(f_t) < 50.0, 0.05, 1.0).astype(np.float32) if rng.random() < 0.4 else np.ones_like(f_t, dtype=np.float32)
    sig = (env * np.exp(1j * phase)).astype(np.complex64)
    sig = _resample_to_out(sig, fs_in)
    sig = _take_slice(sig, rng=rng)
    return _impair_and_finalize(rng, sig)


def gen_gmsk(rng):
    # GSM: 270.833 kbaud, BT=0.3, 200 kHz BW.
    sym_rate = 270_833.0
    sps = max(2, int(round(FS_OUT / sym_rate)))  # ~3.7 -> 4
    fs_in = sps * sym_rate
    n_syms = int(np.ceil((N_SAMP + 64) * fs_in / FS_OUT / sps)) + 4
    bits = rng.integers(0, 2, size=n_syms) * 2 - 1  # ±1 NRZ
    # Upsample, gaussian filter (BT=0.3, span 4 symbols).
    up = np.repeat(bits.astype(np.float32), sps)
    g = _gaussian(0.3, sps, span=4)
    g = g * 1.0  # already normalized to sum=1
    filt = lfilter(g, [1.0], up)
    # MSK phase: integrate to half-cycle per symbol.
    phase = np.pi / 2.0 * np.cumsum(filt) / sps
    sig = np.exp(1j * phase).astype(np.complex64)
    sig = _resample_to_out(sig, fs_in)
    sig = _take_slice(sig, rng=rng)
    return _impair_and_finalize(rng, sig)


def _gen_psk_qam(rng, mod):
    """Shared generator for QPSK / OQPSK / QAM. mod in {"qpsk","oqpsk","qam"}."""
    if mod == "qpsk":
        sym_rate = rng.uniform(50e3, 500e3)
    elif mod == "oqpsk":
        sym_rate = rng.uniform(50e3, 400e3)  # CDMA-like but lower so it fits
    else:
        sym_rate = rng.uniform(40e3, 300e3)

    # Pick sps to oversample at integer factor; output at fs_in then resample.
    sps = max(4, int(round(8.0 if sym_rate > 200e3 else 16.0)))
    fs_in = sps * sym_rate
    alpha = rng.uniform(0.25, 0.4)
    rrc = _rrc(alpha, sps, span=8)

    n_syms = int(np.ceil((N_SAMP + 64) * fs_in / FS_OUT / sps)) + 16

    if mod == "qpsk":
        # Gray-coded QPSK
        idx = rng.integers(0, 4, size=n_syms)
        const = np.array([1 + 1j, -1 + 1j, -1 - 1j, 1 - 1j], dtype=np.complex64) / np.sqrt(2.0)
        syms = const[idx]
    elif mod == "oqpsk":
        idx = rng.integers(0, 4, size=n_syms)
        const = np.array([1 + 1j, -1 + 1j, -1 - 1j, 1 - 1j], dtype=np.complex64) / np.sqrt(2.0)
        syms = const[idx]
    else:
        # QAM: pick 16/64/256
        order = rng.choice([16, 64, 256])
        m = int(np.sqrt(order))
        levels = (np.arange(m) * 2 - (m - 1)).astype(np.float32)
        re = levels[rng.integers(0, m, size=n_syms)]
        im = levels[rng.integers(0, m, size=n_syms)]
        syms = (re + 1j * im).astype(np.complex64)
        syms = syms / np.sqrt(np.mean(np.abs(syms) ** 2))  # unit avg power

    # Upsample by sps (insert zeros), then RRC filter.
    up = np.zeros(len(syms) * sps, dtype=np.complex64)
    up[::sps] = syms
    if mod == "oqpsk":
        # Offset Q by sps/2 samples.
        re = lfilter(rrc, [1.0], up.real).astype(np.float32)
        im_up = np.zeros_like(up.imag)
        im_up[::sps] = syms.imag
        # Roll im by half-symbol
        half = sps // 2
        im_up_shift = np.roll(im_up, half)
        im = lfilter(rrc, [1.0], im_up_shift).astype(np.float32)
        sig = (re + 1j * im).astype(np.complex64)
    else:
        sig = lfilter(rrc, [1.0], up).astype(np.complex64)

    sig = _resample_to_out(sig, fs_in)
    sig = _take_slice(sig, rng=rng)
    return _impair_and_finalize(rng, sig)


def gen_qpsk(rng):
    return _gen_psk_qam(rng, "qpsk")


def gen_oqpsk(rng):
    return _gen_psk_qam(rng, "oqpsk")


def gen_qam(rng):
    return _gen_psk_qam(rng, "qam")


def gen_ook(rng):
    rate = rng.uniform(2e3, 50e3)
    chip_samps = max(2, int(round(FS_OUT / rate)))
    n_chips = int(np.ceil(N_SAMP / chip_samps)) + 4
    # On/off pattern. Average duty ~50%, but vary on-time per chip.
    bits = rng.integers(0, 2, size=n_chips).astype(np.float32)
    # Smooth edges so we don't have impulse-like discontinuities.
    env = np.repeat(bits, chip_samps)
    # Soft edges: 5% of a chip rise/fall.
    edge = max(2, chip_samps // 10)
    kernel = np.ones(edge, dtype=np.float32) / edge
    env = lfilter(kernel, [1.0], env)
    # Carrier at DC (impair adds offset later).
    sig = env.astype(np.complex64)
    sig = _take_slice(sig, rng=rng)
    return _impair_and_finalize(rng, sig)


GENERATORS = {
    "NOISE": gen_noise,
    "FM_BROADCAST": gen_fm_broadcast,
    "FM_NARROW": gen_fm_narrow,
    "AM_VOICE": gen_am_voice,
    "DMR": gen_dmr,
    "GMSK": gen_gmsk,
    "QPSK": gen_qpsk,
    "OQPSK": gen_oqpsk,
    "QAM": gen_qam,
    "OOK": gen_ook,
}


# ----------------------------------------------------------------------------
# Diagnostics
# ----------------------------------------------------------------------------
def diagnose(name, sig):
    """Print a one-line diagnostic about a synthesized example."""
    amp = np.abs(sig)
    spec = np.abs(np.fft.fftshift(np.fft.fft(sig))) ** 2
    spec /= spec.sum() + 1e-20
    freqs = np.fft.fftshift(np.fft.fftfreq(len(sig), d=1.0 / FS_OUT))
    peak_f = freqs[np.argmax(spec)]
    # 90% power BW
    sorted_idx = np.argsort(spec)[::-1]
    cum = np.cumsum(spec[sorted_idx])
    n90 = np.searchsorted(cum, 0.9) + 1
    bw_bins = sorted_idx[:n90]
    bw_hz = freqs[bw_bins].max() - freqs[bw_bins].min()
    print(
        f"  {name:14s}  mean|x|={amp.mean():.3f}  std|x|={amp.std():.3f}  "
        f"peak_f={peak_f / 1e3:+7.1f}kHz  90%-BW={bw_hz / 1e3:6.1f}kHz"
    )


def validate_classes(rng, n=4):
    """Synthesize a few examples per class and print diagnostics."""
    print("=== diagnostic samples (1 per class, no impairments removed) ===")
    for name, fn in GENERATORS.items():
        for _ in range(n):
            sig, _snr = fn(rng)
            diagnose(name, sig)
            break  # just one per class for the summary
    print()


# ----------------------------------------------------------------------------
# Build dataset
# ----------------------------------------------------------------------------
def build_dataset(per_class, seed, out_path):
    rng = np.random.default_rng(seed)
    n_classes = len(CLASSES_DISCO)
    N = per_class * n_classes
    print(f"generating {N} examples ({per_class}/class × {n_classes} classes)")

    X = np.empty((N, N_SAMP, 2), dtype=np.float32)
    Y = np.empty((N,), dtype=np.int64)

    idx = 0
    for cls_idx, cls_name in enumerate(CLASSES_DISCO):
        gen = GENERATORS[cls_name]
        t0 = time.time()
        for _ in range(per_class):
            sig, _snr = gen(rng)
            X[idx, :, 0] = sig.real.astype(np.float32)
            X[idx, :, 1] = sig.imag.astype(np.float32)
            Y[idx] = cls_idx
            idx += 1
        elapsed = time.time() - t0
        print(f"  {cls_name:14s}  {per_class} examples in {elapsed:5.1f}s")

    # Shuffle so classes are interleaved (matters less since we random_split, but harmless).
    perm = rng.permutation(N)
    X = X[perm]
    Y = Y[perm]

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    print(f"writing {out_path} ...")
    with h5py.File(out_path, "w") as f:
        f.create_dataset("X", data=X, compression=None)
        f.create_dataset("Y", data=Y, compression=None)
        f.attrs["classes"] = json.dumps(CLASSES_DISCO)
        f.attrs["fs_out_hz"] = float(FS_OUT)
        f.attrs["n_samp"] = int(N_SAMP)
    sz = os.path.getsize(out_path)
    print(f"done. {N} samples, file size {sz / 1e6:.1f} MB")
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-class", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default="data/disco_synth_v1.hdf5")
    ap.add_argument("--validate", action="store_true", help="Print per-class diagnostics and exit")
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    validate_classes(rng)
    if args.validate:
        return
    build_dataset(args.per_class, args.seed, args.out)


if __name__ == "__main__":
    main()
