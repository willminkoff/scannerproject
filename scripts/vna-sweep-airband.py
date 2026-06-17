#!/usr/bin/env python3
"""NanoVNA S11 / VSWR sweep for the airband RF chain (110-140 MHz).

Standalone health check for the discone -> FM-notch -> splitter -> coax ->
RSPduo Tuner-1 50-ohm path. Written 2026-06-16 after a raw IQ capture showed
the SDR sees only noise floor (no airband carriers) -> suspected antenna/feed
fault rather than gain or DSP. This measures the feed directly.

Talks to a NanoVNA over USB-serial (CDC ACM) using the common DiSlord/standard
command set: ``sweep <start> <stop> <points>``, ``frequencies``, ``data 0``
(S11 as "real imag" pairs). Writes a Touchstone .s1p and prints a VSWR verdict.

Usage:
  sudo python3 vna-sweep-airband.py
  sudo python3 vna-sweep-airband.py --port /dev/ttyACM0 --points 101 \
       --start 110e6 --stop 140e6 --out /home/ubuntu/sweep/airband-vna.s1p

Notes:
  * Connect the coax-under-test to CH0 (S11 / reflection port).
  * Calibrate the NanoVNA on-device for absolute accuracy, but an
    open/short/disconnect is unmistakable even uncalibrated (VSWR >> 4 across
    the whole band). A healthy wideband discone reads VSWR < 2 across most of
    110-140 MHz.
  * Airband proper is 118-137 MHz; the verdict is based on that in-band span,
    with the full 110-140 sweep saved for plotting.
"""
from __future__ import annotations

import argparse
import glob
import statistics
import sys
import time
from datetime import datetime

try:
    import serial  # pyserial
except ImportError:
    print("ERROR: pyserial not installed (pip3 install --break-system-packages pyserial)")
    sys.exit(2)


def find_port() -> str | None:
    for pat in ("/dev/ttyACM*", "/dev/ttyUSB*"):
        matches = sorted(glob.glob(pat))
        if matches:
            return matches[0]
    return None


def converse(ser: serial.Serial, cmd: str, read_timeout: float = 12.0) -> str:
    """Send a command, read until the 'ch>' prompt returns, return raw text."""
    ser.reset_input_buffer()
    ser.write((cmd + "\r\n").encode())
    ser.flush()
    buf = b""
    deadline = time.time() + read_timeout
    while time.time() < deadline:
        chunk = ser.read(8192)
        if chunk:
            buf += chunk
            if buf.rstrip().endswith(b"ch>"):
                break
        else:
            time.sleep(0.01)
    return buf.decode(errors="replace")


def data_lines(text: str, cmd: str) -> list[str]:
    """Strip the echoed command and the trailing prompt; return data rows."""
    out = []
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line or line == cmd or line.startswith("ch>"):
            continue
        out.append(line)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="NanoVNA airband VSWR sweep")
    ap.add_argument("--port", help="serial port (default: first /dev/ttyACM*|ttyUSB*)")
    ap.add_argument("--points", type=int, default=101, help="sweep points (NanoVNA max often 101)")
    ap.add_argument("--start", type=float, default=110e6, help="start Hz")
    ap.add_argument("--stop", type=float, default=140e6, help="stop Hz")
    ap.add_argument("--inband-lo", type=float, default=118e6, help="verdict band low Hz")
    ap.add_argument("--inband-hi", type=float, default=137e6, help="verdict band high Hz")
    ap.add_argument("--out", default=None, help="Touchstone .s1p path (default timestamped in ~/sweep)")
    args = ap.parse_args()

    port = args.port or find_port()
    if not port:
        print("ERROR: no /dev/ttyACM* or /dev/ttyUSB* found — is the NanoVNA plugged in "
              "and the coax on CH0?")
        return 2

    out = args.out or f"/home/ubuntu/sweep/airband-vna-{datetime.now():%Y%m%d-%H%M%S}.s1p"
    print(f"port:   {port}")
    print(f"sweep:  {args.start/1e6:.1f}-{args.stop/1e6:.1f} MHz, {args.points} points")

    ser = serial.Serial(port, 115200, timeout=0.3)
    time.sleep(0.3)
    converse(ser, "")  # flush boot banner / get a clean prompt

    start, stop, pts = int(args.start), int(args.stop), int(args.points)
    converse(ser, f"sweep {start} {stop} {pts}")
    time.sleep(max(1.0, pts * 0.02))  # let at least one full sweep complete

    freqs = []
    for x in data_lines(converse(ser, "frequencies"), "frequencies"):
        try:
            freqs.append(float(x.split()[0]))
        except (ValueError, IndexError):
            pass

    s11 = []
    for line in data_lines(converse(ser, "data 0"), "data 0"):
        parts = line.split()
        if len(parts) >= 2:
            try:
                s11.append(complex(float(parts[0]), float(parts[1])))
            except ValueError:
                pass

    ser.close()

    n = min(len(freqs), len(s11))
    if n == 0:
        print("ERROR: no sweep data returned. Check the NanoVNA firmware command set "
              "(expected 'sweep'/'frequencies'/'data 0') and that it is not in a menu.")
        return 3
    freqs, s11 = freqs[:n], s11[:n]

    def vswr_of(z: complex) -> float:
        g = min(abs(z), 0.999999)
        return (1 + g) / (1 - g)

    vswr = [vswr_of(z) for z in s11]

    # Touchstone .s1p (real/imag, 50 ohm). Plot-ready for nanovna-saver / skrf.
    with open(out, "w") as f:
        f.write("! airband RF-chain S11 sweep (discone -> FM-notch -> splitter -> RSPduo T1)\n")
        f.write(f"! captured {datetime.now():%Y-%m-%d %H:%M:%S}  points={n}\n")
        f.write("# Hz S RI R 50\n")
        for fr, z in zip(freqs, s11):
            f.write(f"{fr:.0f} {z.real:.6f} {z.imag:.6f}\n")

    # Full-sweep stats.
    vmax = max(vswr)
    f_worst = freqs[vswr.index(vmax)]
    print(f"\nFull sweep ({freqs[0]/1e6:.1f}-{freqs[-1]/1e6:.1f} MHz):")
    print(f"  VSWR  mean={statistics.mean(vswr):.2f}  min={min(vswr):.2f}  "
          f"max={vmax:.2f} @ {f_worst/1e6:.3f} MHz")

    # In-band stats drive the verdict.
    inband = [(fr, v) for fr, v in zip(freqs, vswr) if args.inband_lo <= fr <= args.inband_hi]
    if inband:
        iv = [v for _, v in inband]
        ivmax = max(iv)
        ifw = inband[iv.index(ivmax)][0]
        mean_in = statistics.mean(iv)
        print(f"In-band ({args.inband_lo/1e6:.0f}-{args.inband_hi/1e6:.0f} MHz, airband):")
        print(f"  VSWR  mean={mean_in:.2f}  min={min(iv):.2f}  max={ivmax:.2f} @ {ifw/1e6:.3f} MHz")
        basis = mean_in
    else:
        basis = statistics.mean(vswr)

    if basis < 2:
        verdict = "healthy (<2)"
    elif basis <= 4:
        verdict = "mediocre (2-4)"
    else:
        verdict = "bad (>4) — likely open/short/disconnect"
    print(f"\nVERDICT: {verdict}   (in-band mean VSWR = {basis:.2f})")
    print(f"Touchstone: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
