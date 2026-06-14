#!/usr/bin/env python3
"""Capture NOAA Weather Radio (162.550 MHz, continuous NFM voice) off the
airband/ground RSPduo, save the raw IQ, and produce a textbook FM-demod
reference WAV. NOAA is the only continuous signal that reaches Will, and it
exercises the SAME front-end as airband (xlating tune / decimate / channel
filter / squelch / resampler) with a different back-end (FM discriminator,
no AGC). Pairs with _chirp_demod.py to isolate chirp's bug.

Run with airband + ground STOPPED (they hold the RSPduo). Writes:
  /tmp/noaa.iq            raw complex64 IQ (for _chirp_demod.py)
  /tmp/noaa_textbook.wav  textbook FM demod (reference for Will's ears)
"""
import sys, wave
import numpy as np

NOAA_HZ   = 162.550e6
CENTER_HZ = 162.400e6              # NOAA at +150 kHz, off the DC spike
SHIFT_HZ  = NOAA_HZ - CENTER_HZ
FS        = 2_000_000.0
GAIN_DB   = 20.0
ANTENNA   = "Tuner 1 50 ohm"
SERIAL    = "1809063632"
DUR_S     = 12.0
AUDIO_FS  = 16000
IQ_OUT    = "/tmp/noaa.iq"
WAV_OUT   = "/tmp/noaa_textbook.wav"

def capture():
    import SoapySDR, time
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32
    sdr = SoapySDR.Device(dict(driver="sdrplay", serial=SERIAL))
    try: sdr.setAntenna(SOAPY_SDR_RX, 0, ANTENNA)
    except Exception as e: print("WARN setAntenna:", e)
    print("antenna ->", sdr.getAntenna(SOAPY_SDR_RX, 0))
    sdr.setSampleRate(SOAPY_SDR_RX, 0, FS)
    sdr.setFrequency(SOAPY_SDR_RX, 0, CENTER_HZ)
    try: sdr.setGainMode(SOAPY_SDR_RX, 0, False)
    except Exception: pass
    sdr.setGain(SOAPY_SDR_RX, 0, GAIN_DB)
    print("center=%.3f MHz fs=%.0f gain=%.1f" % (CENTER_HZ/1e6, FS, sdr.getGain(SOAPY_SDR_RX,0)))
    st = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32); sdr.activateStream(st)
    n_total = int(FS*DUR_S); buf = np.empty(n_total, np.complex64); got = 0
    chunk = np.empty(65536, np.complex64); t0 = time.time()
    while got < n_total and time.time()-t0 < DUR_S+8:
        sr = sdr.readStream(st, [chunk], len(chunk), timeoutUs=2_000_000)
        if sr.ret > 0:
            take = min(sr.ret, n_total-got); buf[got:got+take] = chunk[:take]; got += take
    sdr.deactivateStream(st); sdr.closeStream(st)
    print("captured %d samples (%.1fs)" % (got, got/FS))
    return buf[:got]

def main():
    iq = capture()
    iq.tofile(IQ_OUT); print("saved", IQ_OUT)
    # confirm NOAA carrier present
    from numpy.fft import fftshift, fft, fftfreq
    seg = iq[:1<<20]
    P = np.abs(fftshift(fft(seg*np.hanning(len(seg)))))**2
    f = fftshift(fftfreq(len(seg), 1/FS)); pk = np.argmax(P)
    print("strongest carrier %+.1f kHz (%.4f MHz) %.1f dB; NOAA expected %+.0f kHz" %
          (f[pk]/1e3, (CENTER_HZ+f[pk])/1e6, 10*np.log10(P[pk]/np.median(P)), SHIFT_HZ/1e3))
    # textbook FM demod
    from scipy import signal
    n = len(iq); t = np.arange(n)/FS
    bb = iq * np.exp(-2j*np.pi*SHIFT_HZ*t).astype(np.complex64)
    dec = 40
    bb = signal.lfilter(signal.firwin(255, 8000.0/(FS/2)), 1.0, bb)[::dec]
    fs2 = FS/dec
    disc = np.angle(bb[1:]*np.conj(bb[:-1]))           # FM discriminator
    bp = signal.firwin(255, [300.0, 3400.0], fs=fs2, pass_zero=False)
    audio = signal.lfilter(bp, 1.0, disc)
    g = np.gcd(int(fs2), AUDIO_FS); audio = signal.resample_poly(audio, AUDIO_FS//g, int(fs2)//g)
    pcm = (audio/(np.max(np.abs(audio))+1e-12)*0.9*32767).astype(np.int16)
    w = wave.open(WAV_OUT,"wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(AUDIO_FS)
    w.writeframes(pcm.tobytes()); w.close()
    print("wrote", WAV_OUT, "%.1fs" % (len(pcm)/AUDIO_FS))

if __name__ == "__main__":
    main()
