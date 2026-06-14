# Session handoff — 2026-06-14 (airband finally resolved)

**Headline: airband works.** The weeks-long "airband only plays noise / no voice"
problem is root-caused and fixed on the chirp side, and confirmed producing clean
voice. The one remaining piece is a hardware/driver tradeoff decision (below) that
only you can make.

**Current left state:** airband + ground UP and STABLE with clean voice. **op25
(digital) is intentionally OFF** — that's the only thing keeping airband stable
right now (see the driver section). All chirp fixes are committed.

---

## What was actually wrong (the whole chain, finally)

It was never one bug — it was a stack of them, each hiding the next:

1. **BT split-brain** — the BOOM showed "connected" but PipeWire's audio profile was
   `off` (no A2DP transport). Fixed by forced reconnect + `bt-connect-speaker.sh`.
   (Also: the BT sink volume keeps resetting to 0/9% on reconnect — a PipeWire
   gremlin; set it with `wpctl set-volume @DEFAULT_AUDIO_SINK@ 0.6`.)

2. **AM AGC flattening the voice** — chirp's fast `agc3_cc` on the AM path held the
   amplitude constant, erasing the modulation that *is* the voice. Fixed:
   `am_agc_enabled=false` (fixed gain instead). Commit `72560d5`.

3. **Gain overload** — RSPduo front-end gain was too high (overload, not starvation).
   `CHIRP_SDR_GAIN_DB=20`. (Drop-in `zz-gain.conf`.)

4. **The VAD gate muting clean voice** — the SB5 voice-activity gate passed corrupted
   noise but *muted* clean voice. Proven: mount audio flipped rms=0 → rms>0 the instant
   the gate was bypassed. Fixed: `vad_enabled=false`. Commit `ef923d8`.

5. **The root cause under all of it — the SDRplay driver crashing.** `sdrplay_apiService`
   segfaults deterministically at `0x6bed`, corrupting the live IQ stream → garbage demod.
   This is what made every other fix look like it didn't work (clean from a recorded file,
   noise live). See `memory/sdrplay-apiservice-segfault.md` and below.

**Proof airband voice works:** with a non-crashed stream, a 60s mount capture (134 hits)
shows transmission windows at spectral flatness **0.19–0.35 (clear voice structure)** vs
~0.8 noise floor between transmissions. Clean voice.

---

## The driver problem + the decision you need to make

The `0x6bed` crash is in SDRplay's closed-source binary — can't patch it. Findings:

- **No driver version fixes it.** Tried 3.15.0 / 3.15.1 / 3.15.2 — all crash on the
  dual-tuner device-open. 3.14.0 is **incompatible** (SoapySDRPlay3 was built against
  3.15 and version-checks; rejects 3.14 unless you rebuild SoapySDRPlay3 from source).
  Currently on **3.15.2**. Installers + a backup are staged in `~ubuntu/`.
- **The trigger is running TWO concurrent dual-tuner RSPduos** — airband+ground on one
  (`1809063632`), op25's TACN+MTRTRS on the other (`180903EF32`). A *single* dual-tuner
  RSPduo is stable (verified crash-free 60s+ with op25 off).

**So: don't run two concurrent dual-tuner RSPduos. Pick one (each costs something):**

| Option | Keeps | Loses | Effort |
|---|---|---|---|
| **op25 single-tuner** (run ONE trunk system) | airband + ground + one op25 system, all stable | one of TACN/MTRTRS | op25 config change + pick a system |
| **op25 off** (current) | airband + ground stable | all digital | none (already done) |
| Move an op25 system to an RTL dongle | everything | — | bigger reconfig (disco uses the RTLs) |

My recommendation: **op25 single-tuner** — costs the least (one trunk system) and keeps
airband working. I left it OFF rather than pick which trunk system to drop, since that's
your call.

---

## Operational notes

- **Re-enabling op25 as-is WILL reintroduce the airband crashes.** Don't `systemctl start
  scanner-digital-op25` expecting airband to stay clean — it'll crash sdrplay on the
  concurrent dual-tuner open.
- Safe SDR restart sequence still applies (stop bands+op25 → reset-failed → restart sdrplay
  → start airband → ground → op25). But **every dual-tuner open is a crash roll** — minimize
  restarts.
- Config that's now correct + committed: `chirp/config/airband.json` →
  `am_agc_enabled:false`, `vad_enabled:false`; gain via `zz-gain.conf`=20.
- New flags: `CHIRP_AM_AGC_ENABLED`, `CHIRP_VAD_ENABLED` env overrides exist too.
- Diagnostic tools added: `scripts/_chirp_demod.py` (run captured IQ through the real
  Channel), `scripts/_airband_capture.py`, `scripts/_noaa_capture.py`.

## Still-open (lower priority, all downstream of the above)
- BT sink volume resets to 0 on reconnect — pin it in the reconnect path.
- Box load was ~26 (disco AI subsystem + 36 FIR channels) — not the audio cause, but heavy.
- The recurring sdrplay retune-wedge / watchdog notes from 2026-06-13 still stand.
