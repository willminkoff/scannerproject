# Research / fix spec — ground NFM-on-RTL produces −180 dBFS silence

> **RESOLVED 2026-06-18 — root cause was the RTL serial collision (§2b), NOT a DSP bug.**
> Ground and `scanner-vfo` were both configured for `rtl=83241970`; the loser of the open
> race got a dead stream → exact-zero PCM despite hits firing. Moving ground to the unused
> **orange dongle `80000003`** (`zz-rtl-ground.conf`: `CHIRP_SDR_DEVICE_ARGS=rtl=80000003`)
> immediately produced real audio (`/ANALOG_GROUND.mp3` RMS −17.3 dBFS, loud hits near full
> scale). The NFM/DSP investigation below was therefore **not needed** — kept as a record.
> Follow-up: RTL gain 40 dB clips (peak 0.0 dBFS); dial down to ~30–35.


**Status:** open, not started. Diagnostics below were gathered 2026-06-18; this
doc exists so a future session can pick up without re-deriving them.
**Branch:** `sb6-phase1-2-chirp-config-hardfail`.
**Scope guard:** this is the *ground* daemon only. **Do NOT touch airband** — it
is working, real-time, and fragile (the probe_rate WATCHDOG fix just landed and
is validated; see `docs/sb6-probe-rate-watchdog.md`).

As of this writing the ground daemon is **stopped** (`systemctl stop
gr-demod@ground`) because it was burning ~7–8 cores (700–800% CPU) spinning on
this wedge. Start it again when actively debugging.

---

## 1. Symptom

`/ANALOG_GROUND.mp3` is silent. Two stacked bugs, one fixed today, one open:

1. **(FIXED today)** Default squelch of **−7 dBFS** sat above the noise floor, so
   no channel ever opened → 0 hits → dead-silent mount, masking everything else.
   Corrected at runtime to **−44 dBFS** via the cmd-bus, after which the daemon
   fires ~**48 hits / 15 s** on the wide-open squelch.
2. **(OPEN — this spec)** Even with squelch wide open and hits firing, the NFM
   audio chain emits **exact 0.0 PCM** at the mount. Not "quiet" — literally
   zero. lame faithfully encodes the zeros into valid MP3 frames, so the byte
   stream and `chirp_audio_bytes_published_total` keep climbing and every
   byte-based health signal reads "green". This is exactly the telemetry-lie
   class the probe_rate gate was built for.

## 2. Confirmed facts (2026-06-18)

- **Daemon is healthy by every structural metric:** `active`, `NRestarts=0`,
  `chirp_config_load_status=1`, **20–24 channels** loaded, LO scheduler scanning
  all clusters, ~48 hits/15 s with squelch wide open.
- **Mount publishes a steady byte stream** (≈4096 B/s) — lame encoding zeros.
- **PCM at the sink is exact zeros**, not low-level noise.
- **Survives restarts** → structural, not a transient init race.
- **Onset = 2026-06-16 RTL migration.** Ground previously ran as the **RSPduo
  SLAVE** (`serial=1809063632, mode=SL, tuner=2`, `bandwidth_hz=1536000`) and
  worked. It was moved to an **RTL** on 6/16.
- **Same daemon code as airband**, and **airband works** — but airband is AM on
  the RSPduo. So the defect lives in the **NFM ∧ RTL** combination, not in the
  shared daemon/flowgraph scaffolding.

### 2a. Current ground source config (from drop-ins + ground.json)
| Key | Value (effective) | Note |
|---|---|---|
| `CHIRP_SDR_DEVICE_ARGS` | **`rtl=83241970`** | RTL via SoapySDR rtl driver |
| `pool_mode` | `nfm` | |
| `CHIRP_SOURCE_SAMP_RATE` | `2000000` (2 Msps) | RTL-V4 native is 2.4 Msps |
| `CHIRP_SDR_GAIN_DB` | `40` | `gain_mode_auto=false` |
| `CHIRP_SDR_BANDWIDTH_HZ` | **`0`** | RSPduo path used `1536000`; RTL sets 0 |
| `CHIRP_MAX_CHANNELS` | `24` | |
| `CHIRP_LO_MAX_CLUSTERS` | `16` | scan_hold enabled |
| ground.json default (overridden) | RSPduo `mode=SL,tuner=2`, BW 1.536 MHz | the pre-6/16 working source |

### 2b. ⚠️ NEW suspicion found while stopping ground — RTL serial collision
`rtl=83241970` is the **same serial** that `reliability.py` lists as
**"RTL-SDR Blog V4 — VFO"**, and `scanner-vfo.service` is **active and holding
83241970** (`driver=usbfs`) right now. So while ground was running, **two
services were configured to open the same physical RTL**. A
two-clients-one-RTL fight is a very plausible way to get a source that opens
"successfully" but delivers nothing → exact zeros downstream. **Check this
FIRST** — it may be the whole bug, or stacked with a real NFM-path issue.
Verify the intended 6/16 serial assignment (was ground supposed to get a
*different* free RTL?).

## 3. Diffs to investigate

- **`chirp/dsp/channel.py` — NFM path.** `quadrature_demod` gain, the
  decimation/resampler rates, and the audio bandpass. The quad-demod gain is
  `sample_rate / (2π·deviation)`-style; if it's computed from a rate that
  doesn't match the RTL's actual delivered rate, output can collapse.
- **`chirp/dsp/source_sdr.py` (+ any source_rtl handling).** Does the SoapySDR
  rtl source actually deliver IQ at the rate the channelizer expects? Does
  `set_bandwidth(0)` behave (RTL has no analog IF filter) vs the RSPduo's
  `1536000`? Any RSPduo-specific assumption (antenna, gain elements, stream
  args) that silently no-ops or misbehaves on an RTL?
- **`chirp/daemon.py` source-open** — branching for RTL vs RSPduo; element
  gains dict (`sdr_element_gains`) is RSPduo-shaped and may be applied blindly.
- **Sample-rate propagation.** RTL-V4 native 2.4 Msps; we request 2 Msps. Does
  that land cleanly, and does the channelized rate the NFM demod assumes match
  what the RTL path produces (vs the RSPduo path)?

## 4. Likely root causes, in order to chase

1. **RTL serial collision with VFO (§2b)** — two openers of 83241970. Cheapest
   to confirm; may be the whole thing.
2. **`quadrature_demod` gain computed for the RSPduo rate** → wrong scaling for
   the RTL stream → near-zero / NaN audio.
3. **Audio resampler mismatch** — chain expects a specific channelized rate
   (e.g. 40 kHz) but the RTL path yields something else → resampler outputs
   zeros.
4. **Per-channel demod emits zeros via NaN propagation / filter-init** when fed
   the RTL stream (e.g. an uninitialized tap or a div-by-zero in a rate calc).
5. **A hardcoded RSPduo assumption in `source_sdr.py`** (bandwidth, antenna,
   element gains) that breaks IQ delivery on an RTL on `gr-demod@ground`.

## 5. Diagnostic plan (runnable by the next session)

1. **Rule out §2b first:** reassign ground to a *known-free* RTL serial (or
   stop VFO temporarily), restart ground, re-check PCM amplitude. If audio
   appears, it was the collision.
2. **Probe taps along the NFM chain** (env-gated, debug-logged — see §6) to find
   exactly where the signal becomes zero. Tap amplitudes at:
   - post-channelizer IQ (is IQ even arriving non-zero per channel?)
   - post-`quadrature_demod` (is the demod output zero?)
   - pre-audio-bandpass
   - post-bandpass (the mount input)
   The first stage that reads ~0 localizes the bug.
3. **Mode isolation:** run the ground daemon in **AM mode** on the same RTL. If
   AM produces audio and NFM doesn't, the bug is NFM-specific (→ §4.2/§4.3). If
   AM is also zero, it's the RTL **source** path (→ §4.5 / §2b).
4. **Side-by-side flowgraph compare:** working AM/RSPduo vs broken NFM/RTL —
   diff the actual block rates/gains as constructed at runtime (log them).

## 6. Constraints (Will's workflow preferences)

- **Plan first** — present the plan before editing.
- **Env-var rollback** — gate every new probe/behavior behind an env flag,
  default off (mirror `CHIRP_AUDIO_PROBE_ENABLED`).
- **Debug logging** — the chain probes should log amplitudes at each tap.
- **Regression test** — add a test reproducing the zero-PCM-on-NFM condition
  and asserting the fix (mirror the Phase 2 / probe_rate test patterns).
- **Do NOT touch airband** during this work. Airband is real-time and fragile.
- The **probe_rate WATCHDOG** defect-class fix is already deployed on airband
  and guards against silent-wedge regressions; consider enabling it on ground
  once ground produces real audio (currently OFF on ground).

## 7. Pointers
- Probe_rate fix / mechanism: `docs/sb6-probe-rate-watchdog.md`,
  `chirp/audio_probe.py`.
- MASTER-safe restart (never bare `systemctl restart` the RSPduo daemons):
  `scripts/recover-sdrplay.sh`.
- Expected dongle serial map: `ui/reliability.py` (`EXPECTED_DONGLE_SERIALS`).
