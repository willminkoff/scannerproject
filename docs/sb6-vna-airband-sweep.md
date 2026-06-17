# Airband RF-chain VNA sweep (NanoVNA)

**Why:** 2026-06-16 — a raw IQ capture at 119.35 MHz (gain 40 dB, chirp bypassed)
showed only noise floor, no airband carriers, across 2 MHz during busy ATC time.
That points to the **antenna/feed**, not gain or chirp DSP. This sweep measures
the feed directly: discone → FM-notch → splitter (port-A airband) → coax →
RSPduo Tuner-1 50-ohm.

**Tool:** `scripts/vna-sweep-airband.py` (pyserial, NanoVNA standard command set).

## Before running — physical setup
1. **Disconnect** the coax from the RSPduo Tuner-1 50-ohm port.
2. Connect that **same coax to the NanoVNA CH0** (S11 / reflection port).
3. Plug the NanoVNA into the Micro by USB. It enumerates as `/dev/ttyACM0`
   (verify: `ls /dev/ttyACM*`).
4. (Optional, for absolute accuracy) calibrate the NanoVNA on-device over
   110–140 MHz. Not required to spot an open/short/disconnect — those read
   VSWR ≫ 4 across the whole band regardless of calibration.

## Run it (one line)
```bash
cd /home/ubuntu/scannerproject && sudo python3 scripts/vna-sweep-airband.py
```

That sweeps 110–140 MHz (101 points), writes a timestamped Touchstone to
`/home/ubuntu/sweep/airband-vna-<ts>.s1p`, and prints VSWR mean/min/max, the
worst-VSWR frequency, and a verdict.

Options: `--port /dev/ttyACM0`, `--points 101`, `--start 110e6 --stop 140e6`,
`--out /path/file.s1p`.

## Reading the result
- **healthy (<2)** — feed is good; the no-signal problem is *not* the antenna
  path. Look elsewhere (RSPduo input, mode/tuner mapping).
- **mediocre (2–4)** — marginal; lossy connector/cable or partial mismatch.
- **bad (>4)** — open / short / disconnect. Inspect connectors at the splitter,
  the FM-notch filter, and the coax run; the antenna path is not delivering RF.

A wideband discone should read VSWR < 2 across most of 110–140 MHz. A flat,
very-high VSWR everywhere = disconnected/open feed (matches the no-carrier IQ
capture).

## After running
1. Disconnect the coax from the NanoVNA.
2. **Reconnect it to the RSPduo Tuner-1 50-ohm port.**
3. The `.s1p` is at `/home/ubuntu/sweep/` — pull it for plotting in
   nanovna-saver or `scikit-rf` if you want the curve.

## Firmware note
The script uses the common NanoVNA serial command set (`sweep <start> <stop>
<points>`, `frequencies`, `data 0`). If your NanoVNA returns no data, it may use
a different command set/menu state — check `ls /dev/ttyACM*` and that it is at
the main screen, then re-run.
