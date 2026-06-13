#!/usr/bin/env python3
"""Capture ~200 ms of IQ from chirp's RSPduo and dump the spectrum.

Stops chirp airband briefly (chirp holds the device), opens the RSPduo
directly via SoapySDR, captures a window centered on whichever airband
cluster the operator picks, FFTs it, and prints the per-bin peak so we
can SEE whether real RF is reaching the SDR.

Usage:
  sudo python3 _iq_spectrum.py <center_mhz>
"""

import sys, time, math
import numpy as np
import SoapySDR

CENTER_MHZ = float(sys.argv[1]) if len(sys.argv) > 1 else 135.1  # ATIS default
SAMP_RATE = 2_000_000.0
N_FFT = 4096
N_SAMPLES = 200_000  # ~100 ms at 2 Msps

# Open via the enumeration label form — earlier we proved this is the
# only way to reliably pick Master mode on chirp's RSPduo.
dev_args = {
    "driver": "sdrplay",
    "label": "SDRplay Dev1 RSPduo 1809063632 - Master",
}
print(f"opening device: {dev_args}")
print(f"center: {CENTER_MHZ} MHz, sample_rate: {SAMP_RATE/1e6} Msps")
sdr = SoapySDR.Device(dev_args)
try:
    hw_info = sdr.getHardwareInfo()
    print(f"opened device hardware info (raw): {str(hw_info)[:200]}")
except Exception as e:
    print(f"getHardwareInfo: {e}")

antennas = sdr.listAntennas(SoapySDR.SOAPY_SDR_RX, 0)
print(f"antennas available: {antennas}")
# Choose the 50-ohm tuner-1 input.
target_ant = None
for a in antennas:
    if "Tuner 1 50" in a:
        target_ant = a
        break
if target_ant:
    sdr.setAntenna(SoapySDR.SOAPY_SDR_RX, 0, target_ant)
    print(f"set antenna -> {target_ant!r}")
print(f"actual antenna: {sdr.getAntenna(SoapySDR.SOAPY_SDR_RX, 0)!r}")

# Manual gain.  RSPduo total range 0..48 dB.  Try 40.
sdr.setGainMode(SoapySDR.SOAPY_SDR_RX, 0, False)
sdr.setGain(SoapySDR.SOAPY_SDR_RX, 0, 40.0)

sdr.setSampleRate(SoapySDR.SOAPY_SDR_RX, 0, SAMP_RATE)
sdr.setFrequency(SoapySDR.SOAPY_SDR_RX, 0, CENTER_MHZ * 1e6)
print(f"actual sample rate: {sdr.getSampleRate(SoapySDR.SOAPY_SDR_RX, 0)/1e6} Msps")
print(f"actual center freq: {sdr.getFrequency(SoapySDR.SOAPY_SDR_RX, 0)/1e6} MHz")

stream = sdr.setupStream(SoapySDR.SOAPY_SDR_RX, SoapySDR.SOAPY_SDR_CF32)
sdr.activateStream(stream)
buf = np.empty(N_SAMPLES, dtype=np.complex64)
got = 0
deadline = time.time() + 5.0
while got < N_SAMPLES and time.time() < deadline:
    chunk = np.empty(min(8192, N_SAMPLES - got), dtype=np.complex64)
    sr = sdr.readStream(stream, [chunk], len(chunk), timeoutUs=200_000)
    n = sr.ret if hasattr(sr, "ret") else (sr[0] if isinstance(sr, tuple) else 0)
    if n > 0:
        buf[got:got + n] = chunk[:n]
        got += n
sdr.deactivateStream(stream)
sdr.closeStream(stream)
del sdr

print(f"\ncaptured {got} samples ({got/SAMP_RATE*1000:.0f} ms)")
print(f"raw stats: mean_re={np.real(buf[:got]).mean():.3e} mean_im={np.imag(buf[:got]).mean():.3e}")
power = (np.abs(buf[:got]) ** 2).mean()
print(f"avg power: {power:.3e}  ({10*math.log10(power):.1f} dBFS)")
print(f"peak |x|^2 in samples: {(np.abs(buf[:got]) ** 2).max():.3e}")

# FFT averaging — Welch-ish
n_chunks = got // N_FFT
print(f"\nFFT chunks: {n_chunks}")
psd = np.zeros(N_FFT)
window = np.hanning(N_FFT)
window_gain = (window**2).sum() / N_FFT
for i in range(n_chunks):
    seg = buf[i*N_FFT:(i+1)*N_FFT] * window
    spec = np.fft.fftshift(np.fft.fft(seg))
    psd += np.abs(spec) ** 2
psd /= max(n_chunks, 1) * N_FFT * N_FFT * window_gain
psd_db = 10 * np.log10(psd + 1e-30)

# Frequency axis (Hz, around center)
freqs = np.fft.fftshift(np.fft.fftfreq(N_FFT, 1.0/SAMP_RATE))
abs_freqs_mhz = CENTER_MHZ + freqs / 1e6

# Print top 20 peaks
print("\nTop 20 spectral peaks:")
order = np.argsort(psd_db)[::-1]
seen_bins = set()
shown = 0
for idx in order:
    # Avoid printing many adjacent bins of the same peak — require 5-bin separation
    if any(abs(idx - s) < 5 for s in seen_bins):
        continue
    seen_bins.add(idx)
    print(f"  {abs_freqs_mhz[idx]:>9.4f} MHz   {psd_db[idx]:>7.2f} dBFS")
    shown += 1
    if shown >= 20:
        break

# Also a coarse histogram of dB levels
bins = list(range(-110, -10, 10))
hist = [0] * (len(bins) + 1)
for v in psd_db:
    placed = False
    for i, b in enumerate(bins):
        if v < b:
            hist[i] += 1
            placed = True
            break
    if not placed:
        hist[-1] += 1
print("\nPSD level histogram:")
prev = "-inf"
for i, b in enumerate(bins):
    print(f"  {prev}..{b} dBFS: {hist[i]}")
    prev = str(b)
print(f"  {prev}..+inf dBFS: {hist[-1]}")
