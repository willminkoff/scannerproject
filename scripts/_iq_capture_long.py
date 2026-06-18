#!/usr/bin/env python3
"""Long IQ capture + per-window carrier detection on chirp's RSPduo.

Companion to _iq_spectrum.py (which only grabs 100 ms). Captures several
SECONDS so it spans multiple AM-airband keyings — settling the "was the short
snapshot just unlucky?" question. Stops chirp first (chirp holds the device);
the caller is responsible for stopping both analog bands (ground holds the SL
tuner on the same RSPduo) and restarting them afterward.

Per window it computes the noise floor (median PSD) and the strongest peak
EXCLUDING the DC spike and the known internal ~120.000 MHz spur, then flags a
"carrier" when that peak rises CARRIER_DB above the window's noise floor.

Usage:
  sudo python3 _iq_capture_long.py [center_mhz] [duration_s]
  (defaults: 119.35 MHz, 8 s, gain 40 dB, 2 Msps, Tuner 1 50 ohm)
"""
import sys
import time
import math

import numpy as np
import SoapySDR

CENTER_MHZ = float(sys.argv[1]) if len(sys.argv) > 1 else 119.35
DUR_S = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0
SAMP_RATE = 2_000_000.0
GAIN_DB = 40.0
N_FFT = 4096
WIN_S = 0.1                       # analysis window length (s)
WIN_N = int(SAMP_RATE * WIN_S)    # samples per window
CARRIER_DB = 12.0                 # peak must exceed window noise floor by this
SPUR_MHZ = 120.000               # known internal spur to exclude
SPUR_GUARD_KHZ = 15.0
DC_GUARD_KHZ = 15.0

dev_args = {"driver": "sdrplay",
            "label": "SDRplay Dev1 RSPduo 1809063632 - Master"}
print(f"opening: {dev_args}")
print(f"center {CENTER_MHZ} MHz, {SAMP_RATE/1e6} Msps, gain {GAIN_DB} dB, dur {DUR_S}s")
sdr = SoapySDR.Device(dev_args)

for a in sdr.listAntennas(SoapySDR.SOAPY_SDR_RX, 0):
    if "Tuner 1 50" in a:
        sdr.setAntenna(SoapySDR.SOAPY_SDR_RX, 0, a)
        break
print(f"antenna: {sdr.getAntenna(SoapySDR.SOAPY_SDR_RX, 0)!r}")
sdr.setGainMode(SoapySDR.SOAPY_SDR_RX, 0, False)
sdr.setGain(SoapySDR.SOAPY_SDR_RX, 0, GAIN_DB)
sdr.setSampleRate(SoapySDR.SOAPY_SDR_RX, 0, SAMP_RATE)
sdr.setFrequency(SoapySDR.SOAPY_SDR_RX, 0, CENTER_MHZ * 1e6)

total = int(SAMP_RATE * DUR_S)
buf = np.empty(total, dtype=np.complex64)
stream = sdr.setupStream(SoapySDR.SOAPY_SDR_RX, SoapySDR.SOAPY_SDR_CF32)
sdr.activateStream(stream)
got = 0
deadline = time.time() + DUR_S + 5.0
while got < total and time.time() < deadline:
    chunk = np.empty(min(65536, total - got), dtype=np.complex64)
    sr = sdr.readStream(stream, [chunk], len(chunk), timeoutUs=500_000)
    n = sr.ret if hasattr(sr, "ret") else (sr[0] if isinstance(sr, tuple) else 0)
    if n > 0:
        buf[got:got + n] = chunk[:n]
        got += n
sdr.deactivateStream(stream)
sdr.closeStream(stream)
del sdr

print(f"\ncaptured {got} samples ({got/SAMP_RATE:.2f} s)")
buf = buf[:got]
avg_p = (np.abs(buf) ** 2).mean()
print(f"avg power: {avg_p:.3e} ({10*math.log10(avg_p+1e-30):.1f} dBFS)")

freqs = np.fft.fftshift(np.fft.fftfreq(N_FFT, 1.0 / SAMP_RATE))
fmhz = CENTER_MHZ + freqs / 1e6
window = np.hanning(N_FFT)
wgain = (window ** 2).sum() / N_FFT

# Mask: exclude DC and the internal spur from carrier detection.
mask = np.ones(N_FFT, dtype=bool)
mask &= np.abs(fmhz - CENTER_MHZ) > DC_GUARD_KHZ / 1000.0
mask &= np.abs(fmhz - SPUR_MHZ) > SPUR_GUARD_KHZ / 1000.0

n_windows = got // WIN_N
print(f"windows: {n_windows} x {WIN_S*1000:.0f} ms\n")

carrier_windows = 0
detections = []   # (t_s, freq_mhz, peak_db, floor_db, snr_db)
overall_peak_db = -999.0
overall_peak_mhz = None
for w in range(n_windows):
    seg = buf[w * WIN_N:(w + 1) * WIN_N]
    nchunks = len(seg) // N_FFT
    if nchunks == 0:
        continue
    psd = np.zeros(N_FFT)
    for i in range(nchunks):
        s = seg[i * N_FFT:(i + 1) * N_FFT] * window
        psd += np.abs(np.fft.fftshift(np.fft.fft(s))) ** 2
    psd /= nchunks * N_FFT * N_FFT * wgain
    psd_db = 10 * np.log10(psd + 1e-30)
    floor = float(np.median(psd_db))
    cand = psd_db.copy()
    cand[~mask] = -999.0
    pk_idx = int(np.argmax(cand))
    pk_db = float(cand[pk_idx])
    snr = pk_db - floor
    if pk_db > overall_peak_db:
        overall_peak_db = pk_db
        overall_peak_mhz = float(fmhz[pk_idx])
    if snr >= CARRIER_DB:
        carrier_windows += 1
        detections.append((w * WIN_S, float(fmhz[pk_idx]), pk_db, floor, snr))

print(f"carrier windows (peak >= floor+{CARRIER_DB:.0f}dB, spur/DC excluded): "
      f"{carrier_windows}/{n_windows}")
print(f"strongest non-spur peak overall: {overall_peak_mhz:.4f} MHz @ {overall_peak_db:.1f} dBFS")

if detections:
    detections.sort(key=lambda d: d[4], reverse=True)
    print("\nTop carrier detections (by SNR):")
    print("   t(s)   freq(MHz)   peak(dBFS)  floor   SNR(dB)")
    for t, fr, pk, fl, snr in detections[:12]:
        print(f"  {t:5.1f}   {fr:9.4f}   {pk:8.1f}   {fl:6.1f}   {snr:5.1f}")
    # distinct carrier freqs (round to 25 kHz channel grid)
    chans = sorted({round(fr / 0.025) * 0.025 for _, fr, _, _, _ in detections})
    print(f"\ndistinct carrier freqs (~25kHz grid): "
          f"{', '.join(f'{c:.3f}' for c in chans)}")

print("\nVERDICT:", end=" ")
if carrier_windows == 0:
    print("FLAT NOISE FLOOR across the whole capture — no carriers in any window.")
    print("  -> snapshot was NOT just unlucky. Points to insertion loss in the")
    print("     feed (VSWR can't see loss) or the RSPduo input, NOT a missing antenna.")
else:
    frac = 100.0 * carrier_windows / max(n_windows, 1)
    print(f"REAL CARRIERS PRESENT in {carrier_windows} windows ({frac:.0f}%).")
    print("  -> the 100ms snapshot was just unlucky (caught a no-key gap). The feed")
    print("     and SDR ARE receiving airband. Pivot to gain/squelch tuning.")
