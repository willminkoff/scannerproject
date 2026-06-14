#!/usr/bin/env python3
"""Decisive airband-demod test: capture raw IQ off the airband RSPduo at the
KBNA ATIS frequency (135.1 MHz, continuous AM voice) and demodulate it with
textbook AM code — completely outside chirp's DSP.

If the resulting WAV is clean voice, the RF/IQ is good and the bug is in
chirp's demod/channelization. If it's noise, the capture itself is bad
(RSPduo settings), which would be the real RF problem.

Run with airband + ground STOPPED (they hold the RSPduo). Writes:
  /tmp/atis_ref.wav   — textbook AM demod of the ATIS carrier
  prints carrier presence + level so we KNOW the signal was there.
"""
import sys
import numpy as np

ATIS_HZ   = 135.100e6        # KBNA ATIS (continuous AM voice)
CENTER_HZ = 135.000e6        # tune 100 kHz low so ATIS sits off the DC spike
SHIFT_HZ  = ATIS_HZ - CENTER_HZ   # +100 kHz: digital downconvert brings it to DC
FS        = 2_000_000.0
GAIN_DB   = 20.0             # the value that works best for airband (overload-safe)
ANTENNA   = "Tuner 1 50 ohm"
SERIAL    = "1809063632"
DUR_S     = 12.0
AUDIO_FS  = 16000
OUT_WAV   = "/tmp/atis_ref.wav"

def capture():
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32
    args = dict(driver="sdrplay", serial=SERIAL)
    sdr = SoapySDR.Device(args)
    try:
        sdr.setAntenna(SOAPY_SDR_RX, 0, ANTENNA)
    except Exception as e:
        print(f"WARN setAntenna: {e}")
    print("antenna ->", sdr.getAntenna(SOAPY_SDR_RX, 0))
    sdr.setSampleRate(SOAPY_SDR_RX, 0, FS)
    sdr.setFrequency(SOAPY_SDR_RX, 0, CENTER_HZ)
    try:
        sdr.setGainMode(SOAPY_SDR_RX, 0, False)  # no AGC, fixed gain
    except Exception:
        pass
    sdr.setGain(SOAPY_SDR_RX, 0, GAIN_DB)
    print("center=%.3f MHz fs=%.0f gain=%.1f" % (CENTER_HZ/1e6, FS, sdr.getGain(SOAPY_SDR_RX,0)))
    st = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
    sdr.activateStream(st)
    n_total = int(FS * DUR_S)
    buf = np.empty(n_total, np.complex64)
    got = 0
    chunk = np.empty(65536, np.complex64)
    import time
    t0 = time.time()
    while got < n_total and time.time() - t0 < DUR_S + 8:
        sr = sdr.readStream(st, [chunk], len(chunk), timeoutUs=2_000_000)
        n = sr.ret
        if n > 0:
            take = min(n, n_total - got)
            buf[got:got+take] = chunk[:take]
            got += take
    sdr.deactivateStream(st); sdr.closeStream(st)
    print("captured %d samples (%.1f s)" % (got, got/FS))
    return buf[:got]

def am_demod(iq, shift_hz, out_wav):
    from scipy import signal
    n = len(iq)
    t = np.arange(n) / FS
    # bring target carrier (at +shift) down to DC
    bb = iq * np.exp(-2j*np.pi*shift_hz*t).astype(np.complex64)
    # channel low-pass ~±5 kHz then decimate 2 MHz -> 50 kHz (factor 40)
    dec = 40
    taps = signal.firwin(255, 5000.0/(FS/2))
    bb = signal.lfilter(taps, 1.0, bb)[::dec]
    fs2 = FS/dec
    # report carrier strength: power at DC bin vs band edges
    mag = np.abs(bb)
    carrier_db = 20*np.log10(np.mean(mag) + 1e-12)
    # AM envelope detect
    env = np.abs(bb).astype(np.float64)
    # remove DC (carrier) + voice band-pass 300-3400 Hz
    bp = signal.firwin(255, [300.0, 3400.0], fs=fs2, pass_zero=False)
    audio = signal.lfilter(bp, 1.0, env)
    # resample to AUDIO_FS
    g = np.gcd(int(fs2), AUDIO_FS)
    audio = signal.resample_poly(audio, AUDIO_FS//g, int(fs2)//g)
    # normalize
    peak = np.max(np.abs(audio)) + 1e-12
    audio = (audio/peak * 0.9 * 32767).astype(np.int16)
    import wave
    w = wave.open(out_wav, "wb")
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(AUDIO_FS)
    w.writeframes(audio.tobytes()); w.close()
    print("  wrote %s  carrier_level=%.1f dB  dur=%.1fs" % (out_wav, carrier_db, len(audio)/AUDIO_FS))
    return audio, carrier_db, fs2

def main():
    iq = capture()
    if len(iq) < FS*2:
        print("FATAL: too few samples"); sys.exit(2)
    # spectrum: where is the strongest carrier in the captured band?
    from numpy.fft import fftshift, fft, fftfreq
    seg = iq[:1<<20]
    P = np.abs(fftshift(fft(seg * np.hanning(len(seg)))))**2
    f = fftshift(fftfreq(len(seg), 1/FS))
    pk = np.argmax(P)
    pk_shift = f[pk]
    print("strongest carrier in band: %+.1f kHz from center (%.4f MHz), %.1f dB" %
          (pk_shift/1e3, (CENTER_HZ+pk_shift)/1e6, 10*np.log10(P[pk]/np.median(P))))
    print("=== demod 135.1 (ATIS) ===")
    am_demod(iq, SHIFT_HZ, OUT_WAV)
    print("=== demod strongest carrier %.4f MHz ===" % ((CENTER_HZ+pk_shift)/1e6))
    am_demod(iq, pk_shift, "/tmp/peak_ref.wav")

if __name__ == "__main__":
    main()
