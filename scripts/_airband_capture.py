#!/usr/bin/env python3
"""Capture airband AM IQ (longer window to catch intermittent ATC), save the
raw IQ, locate the strongest AM carrier, and write a textbook AM reference.
Pairs with _chirp_demod.py to A/B chirp's AM AGC.

Run with airband+ground STOPPED and sdrplay freshly bounced. Writes:
  /tmp/airband.iq            raw complex64 IQ
  /tmp/airband_textbook.wav  textbook AM demod of the strongest carrier
  prints OFFSET_HZ=<n> for the strongest carrier (feed to _chirp_demod.py)
"""
import sys, wave
import numpy as np

CENTER_HZ = float(sys.argv[1]) if len(sys.argv) > 1 else 119.000e6
FS        = 2_000_000.0
GAIN_DB   = 20.0
ANTENNA   = "Tuner 1 50 ohm"
SERIAL    = "1809063632"
DUR_S     = 25.0
AUDIO_FS  = 16000
IQ_OUT    = "/tmp/airband.iq"
WAV_OUT   = "/tmp/airband_textbook.wav"

def capture():
    import SoapySDR, time
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32
    sdr = SoapySDR.Device(dict(driver="sdrplay", serial=SERIAL))
    try: sdr.setAntenna(SOAPY_SDR_RX, 0, ANTENNA)
    except Exception as e: print("WARN setAntenna:", e)
    sdr.setSampleRate(SOAPY_SDR_RX, 0, FS)
    sdr.setFrequency(SOAPY_SDR_RX, 0, CENTER_HZ)
    try: sdr.setGainMode(SOAPY_SDR_RX, 0, False)
    except Exception: pass
    sdr.setGain(SOAPY_SDR_RX, 0, GAIN_DB)
    print("center=%.3f MHz fs=%.0f gain=%.1f dur=%ds" % (CENTER_HZ/1e6, FS, sdr.getGain(SOAPY_SDR_RX,0), DUR_S))
    st = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32); sdr.activateStream(st)
    n_total = int(FS*DUR_S); buf = np.empty(n_total, np.complex64); got = 0
    chunk = np.empty(65536, np.complex64); t0 = time.time()
    while got < n_total and time.time()-t0 < DUR_S+10:
        sr = sdr.readStream(st, [chunk], len(chunk), timeoutUs=2_000_000)
        if sr.ret > 0:
            take = min(sr.ret, n_total-got); buf[got:got+take] = chunk[:take]; got += take
    sdr.deactivateStream(st); sdr.closeStream(st)
    print("captured %d samples (%.1fs)" % (got, got/FS))
    return buf[:got]

def strongest_carrier(iq):
    """Find the offset (Hz) of the strongest non-DC carrier, scanning the
    whole capture in 0.25s windows so a brief transmission still shows up."""
    from numpy.fft import fftshift, fft, fftfreq
    win = 1 << 19
    acc = None
    for i in range(0, len(iq)-win, win):
        seg = iq[i:i+win] * np.hanning(win)
        P = np.abs(fftshift(fft(seg)))**2
        acc = P if acc is None else np.maximum(acc, P)   # peak-hold across time
    f = fftshift(fftfreq(win, 1/FS))
    # null out +-20 kHz around DC (SDR DC spike)
    acc[np.abs(f) < 20e3] = 0
    pk = np.argmax(acc)
    return f[pk], 10*np.log10(acc[pk]/np.median(acc))

def am_demod(iq, shift):
    from scipy import signal
    n = len(iq); t = np.arange(n)/FS
    bb = iq * np.exp(-2j*np.pi*shift*t).astype(np.complex64)
    dec = 40
    bb = signal.lfilter(signal.firwin(255, 5000.0/(FS/2)), 1.0, bb)[::dec]
    fs2 = FS/dec
    env = np.abs(bb).astype(np.float64)
    bp = signal.firwin(255, [300.0, 3400.0], fs=fs2, pass_zero=False)
    audio = signal.lfilter(bp, 1.0, env)
    g = np.gcd(int(fs2), AUDIO_FS); audio = signal.resample_poly(audio, AUDIO_FS//g, int(fs2)//g)
    return (audio/(np.max(np.abs(audio))+1e-12)*0.9*32767).astype(np.int16)

def main():
    iq = capture()
    iq.tofile(IQ_OUT); print("saved", IQ_OUT)
    shift, snr = strongest_carrier(iq)
    print("strongest carrier %+.1f kHz (%.4f MHz) %.1f dB peak-hold" %
          (shift/1e3, (CENTER_HZ+shift)/1e6, snr))
    print("OFFSET_HZ=%d" % int(round(shift)))
    pcm = am_demod(iq, shift)
    w = wave.open(WAV_OUT,"wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(AUDIO_FS)
    w.writeframes(pcm.tobytes()); w.close()
    print("wrote", WAV_OUT, "%.1fs" % (len(pcm)/AUDIO_FS))

if __name__ == "__main__":
    main()
