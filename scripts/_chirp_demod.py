#!/usr/bin/env python3
"""Run captured IQ through chirp's REAL Channel block and write the demod to
WAV. Isolates whether chirp's DSP (not RF) produces voice from a known-good
IQ capture. VAD is bypassed and squelch is wide open so we hear the raw
demodulator, not the gate.

Usage:
  _chirp_demod.py <iq_file> <am|nfm> <offset_hz> <out_wav> [agc_max_gain]
"""
import sys, wave
import numpy as np
from gnuradio import gr, blocks
sys.path.insert(0, "/home/ubuntu/scannerproject")
from chirp.dsp.channel import Channel

FS = 2_000_000.0
AUDIO = 16000.0

def main():
    iq_file = sys.argv[1]
    mode    = sys.argv[2]
    offset  = float(sys.argv[3])
    out_wav = sys.argv[4]
    agc_arg = sys.argv[5] if len(sys.argv) > 5 else "1000"
    attack  = float(sys.argv[6]) if len(sys.argv) > 6 else 0.1
    decay   = float(sys.argv[7]) if len(sys.argv) > 7 else 1e-4
    # agc="off" exercises the real am_agc_enabled=False (fixed-gain) path.
    am_agc_enabled = agc_arg.strip().lower() != "off"
    agc = float(agc_arg) if am_agc_enabled else 0.0

    tb = gr.top_block()
    src = blocks.file_source(gr.sizeof_gr_complex, iq_file, repeat=False)
    ch = Channel(
        samp_rate=FS, audio_rate=AUDIO, mode=mode,
        center_freq_offset=offset,
        squelch_dbfs=-200.0,                       # wide open
        channel_bw_hz=(6000.0 if mode == "am" else 12500.0),
        agc_max_gain=(agc or 1000.0), agc_attack=attack, agc_decay=decay,
        am_agc_enabled=am_agc_enabled,
    )
    try:
        ch.set_vad_bypass(True)                    # raw demod, not VAD-gated
    except Exception as e:
        print("WARN set_vad_bypass:", e)
    snk = blocks.vector_sink_f()
    tb.connect(src, ch, snk)
    tb.run()

    audio = np.array(snk.data(), dtype=np.float32)
    if audio.size == 0:
        print("FATAL: no audio out"); sys.exit(2)
    peak = float(np.max(np.abs(audio)))
    pcm = (audio/(peak+1e-12)*0.9*32767).astype(np.int16)
    w = wave.open(out_wav, "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(int(AUDIO))
    w.writeframes(pcm.tobytes()); w.close()
    print("wrote %s  mode=%s agc_max=%.0f attack=%.4g decay=%.4g  %.1fs peak_pre=%.4g" %
          (out_wav, mode, agc, attack, decay, len(audio)/AUDIO, peak))

if __name__ == "__main__":
    main()
