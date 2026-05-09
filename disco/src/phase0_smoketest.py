#!/usr/bin/env python3
"""Phase 0 smoke test (option a): sequential single-channel via DT mode at 6.144 Msps."""
import os, sys, time
import SoapySDR
from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32
import numpy as np

CAPTURE_DIR = "/home/ubuntu/scannerproject/disco/captures"
SAMPLE_RATE = 6.144e6
DURATION_S = 1.0
TEST_FREQ = 100e6
SERIALS = ["1809063632", "180903EF32"]
EXPECTED_BYTES = int(SAMPLE_RATE * DURATION_S) * 8

os.makedirs(CAPTURE_DIR, exist_ok=True)
results = []

for serial in SERIALS:
    print(f"\n=== RSPduo serial {serial} (DT mode) ===")
    try:
        sdr = SoapySDR.Device({"driver": "sdrplay", "serial": serial, "mode": "DT"})
    except Exception as e:
        print(f"  FAILED to open: {e}")
        results.append((serial, "-", "open_failed", str(e)[:80]))
        continue

    n_chans = sdr.getNumChannels(SOAPY_SDR_RX)
    print(f"  channels exposed: {n_chans}")

    for ch in range(n_chans):
        sdr.setSampleRate(SOAPY_SDR_RX, ch, SAMPLE_RATE)
        sdr.setFrequency(SOAPY_SDR_RX, ch, TEST_FREQ)
        actual_rate = sdr.getSampleRate(SOAPY_SDR_RX, ch)
        actual_freq = sdr.getFrequency(SOAPY_SDR_RX, ch)
        print(f"  ch{ch}: rate={actual_rate/1e6:.4f} Msps  freq={actual_freq/1e6:.3f} MHz")

        try:
            stream = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32, [ch])
        except Exception as e:
            print(f"  ch{ch} setupStream failed: {e}")
            results.append((serial, ch, "setup_failed", str(e)[:80]))
            continue

        sdr.activateStream(stream)

        n_samps = int(SAMPLE_RATE * DURATION_S)
        buf = np.zeros(n_samps, dtype=np.complex64)
        chunk = 65536
        pos = 0
        t0 = time.time()
        while pos < n_samps:
            remaining = n_samps - pos
            this_chunk = min(chunk, remaining)
            sr = sdr.readStream(stream, [buf[pos:pos+this_chunk]], this_chunk, timeoutUs=5_000_000)
            if sr.ret < 0:
                print(f"  ch{ch} readStream error: ret={sr.ret} flags={sr.flags}")
                break
            pos += sr.ret
            if time.time() - t0 > 15:
                print(f"  ch{ch} timeout — got {pos}/{n_samps}")
                break
        elapsed = time.time() - t0
        print(f"  ch{ch} captured {pos}/{n_samps} samples in {elapsed:.2f}s")

        sdr.deactivateStream(stream)
        sdr.closeStream(stream)

        path = f"{CAPTURE_DIR}/phase0_{serial}_ch{ch}.bin"
        buf[:pos].tofile(path)
        size = os.path.getsize(path)
        status = "ok" if size == EXPECTED_BYTES else "short"
        print(f"  wrote {path}  size={size} bytes  status={status}")
        results.append((serial, ch, status, size))

    sdr = None

print("\n=== Summary ===")
print(f"{'serial':<14}{'ch':<4}{'status':<14}{'bytes':<14}")
for r in results:
    s, c, st, sz = r
    print(f"{s:<14}{str(c):<4}{st:<14}{str(sz):<14}")

print(f"\nExpected bytes per channel: {EXPECTED_BYTES}")
all_ok = sum(1 for r in results if r[2] == "ok") == 4
print(f"4-channel pass: {all_ok}")
sys.exit(0 if all_ok else 1)
