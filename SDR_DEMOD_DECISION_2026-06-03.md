# SDR Demod Decision Brief — Replacing rtl-airband

**Date:** 2026-06-03
**Question:** Should SB5 replace rtl-airband, and with what?
**TL;DR:** Yes. Strongest path is a Python-controlled GNU Radio flowgraph patterned on op25 (already in our stack and proven), with ham2mon as a reference implementation we can borrow from directly.

---

## Question 1 — Does trunk-recorder support hot config reload in 2026?

**Verdict: NO.**

- `trunk-recorder` v5.2.1 (latest, 2026-04-08) supports exactly two signals: `SIGINT` for clean shutdown and `SIGHUP` for log-file rotation only (added v5.2.0 via PR #1080). Source: `monitor_systems.cc` — `signal(SIGINT, exit_interupt); signal(SIGHUP, rotate_log_signal);` and no `load_config()` outside `main()`.
- The canonical request — [Issue #500 "Allow for Plugins to change certain Config values"](https://github.com/TrunkRecorder/trunk-recorder/issues/500) — has been open since August 2021, assigned to robotastic, last touched 2024-12-15 with **no implementation**.
- Restart cost: 5-15s SDR re-acquire window per the community's documented `RestartSec=5` systemd workaround. During the window: active recordings killed, control channel undecoded (lost call grants), GNU Radio flowgraph torn down and rebuilt from scratch, downstream Icecast/VLC listeners hit silence.
- **No mechanism exists:** no SIGUSR1/2, no inotify file-watch, no REST endpoint, no Unix socket, no CLI control tool.

**Also: trunk-recorder is not a viable rtl-airband replacement structurally.** The analog recorder is FM-only at the source-code level (`analog_recorder.cc` instantiates `gr::analog::quadrature_demod_cf` with 5 kHz NFM deviation). No AM demod path. Aviation airband is AM. Trunk-recorder also writes WAV files per call (not continuous streams), has no native Icecast output (community uses simplestream → Liquidsoap → Icecast as a workaround), and SDRplay RSPduo master/slave mode is not a first-class config feature.

**Sources:**
- [Issue #500 (open, no PR)](https://github.com/TrunkRecorder/trunk-recorder/issues/500)
- [`analog_recorder.cc` — FM-only](https://github.com/TrunkRecorder/trunk-recorder/blob/master/trunk-recorder/recorders/analog_recorder.cc)
- [v5.2.1 release notes (2026-04-08)](https://github.com/TrunkRecorder/trunk-recorder/releases/tag/v5.2.1)
- [Issue #130 "ENHANCEMENT - IceCast Streaming" (never landed)](https://github.com/robotastic/trunk-recorder/issues/130)

---

## Question 2 — Who's doing GNU Radio aviation scanning?

**Headline:** The niche is small but **ham2mon is the reference borrow candidate** — exactly the architectural pattern SB5 needs.

### ham2mon (madengr/ham2mon) — strongest borrow candidate

- **What:** GNU Radio + Python multi-channel scanner that instantiates N parallel demodulator hierarchical blocks inside one osmosdr source. Each demod chain: `freq_xlating_fir_filter_ccc` → decimating FIR → `analog.pwr_squelch_cc` (non-blocking) → AGC + AM demod → audio LPF → resampler → audio sink + WAV sink. Scanner control loop probes FFT spectrum at ~10 Hz, identifies channels above threshold, and assigns demodulators by calling `freq_xlating_fir_filter.set_center_freq()` at runtime — **no flowgraph restart**.
- **Aviation support:** explicit `-d 1` flag for AM mode. README example: `./ham2mon.py -a "uhd" -n 8 -d 1 -f 135E6 -r 4E6 -g 30 -s -70`.
- **Status:** 281 stars / 69 forks, lightly maintained. Original GR 3.7-era code (may need a port to GR 3.10+).
- **Fit:** This is essentially a reference implementation of the exact pattern (multi-channel AM, squelched, hot-tuned, audio out) that SB5 needs. `receiver.py` / `scanner.py` split maps cleanly onto SB5's DSP/control split. GPL.
- **Risk:** PyQt4/Python 2 in some examples. `receiver.py` may need GR 3.10 port — call it 1-3 days of porting work.

### SDRangel (f4exb/sdrangel) — heavy but feature-complete prior art

- Qt5 app with explicit "Airband Voice" channel plugin (multi-channel AM with 25 kHz and 8.33 kHz spacing), active v7.25 (May 2026), has REST API + Reverse API for runtime control.
- As a library to borrow from: awkward (Qt monolith). As substrate: heavy footprint, ARM headless story uncertain (sdrangelsrv historically didn't build on aarch64).
- Useful as prior art for parameter choices (filter widths, AGC, squelch hysteresis).

### gr-scan, Hackaday tutorials — pedagogical only

Other GR-based scanners exist (gr-scan forks, Hackaday hackaday.io/project/173553 "Build Air Band Receiver Step by Step GNU-RADIO") but they're either energy-only sweepers or single-shot tutorials. Not borrowable.

### Honest assessment

End-user multi-channel airband listening converged on **rtl-airband** (non-GR, C++) for headless, **SDRangel** for interactive. ham2mon stayed visible because it shipped as exactly the thing — a GR-based scanner with AM/airband mode. The niche is sparse but the one good answer is high enough quality that sparseness doesn't matter.

**Sources:**
- [ham2mon repo](https://github.com/madengr/ham2mon)
- [ham2mon am_flow_example.py](https://github.com/madengr/ham2mon/blob/master/apps/am_flow_example.py)
- [SDRangel Airband Voice plugin](https://github.com/f4exb/sdrangel/blob/master/plugins/channelrx/freqscanner/readme.md)
- [rtl-sdr.com — Ham2Mon multi-channel receiver](https://www.rtl-sdr.com/ham2mon-a-nbfm-multi-channel-receiver-for-the-rtl-sdr/)

---

## How op25 already solves this in our codebase

op25 (which we already run successfully) uses **exactly the pattern we'd build:**

- Single long-running GR flowgraph
- Steered via JSON command messages over an internal message-queue (msgq) plus an optional UDP socket
- Python-side `trunking.rx_ctl` consumes control-channel events, dispatches commands like `set_freq`, `hold`, `lockout`, `whitelist`, `blacklist`
- Commands → demod/decoder blocks via GR msgq (PMT-style JSON), and to the SDR source via `set_center_freq` / `set_gain`
- When the target voice channel is within the current sample-rate window, retunes via `freq_xlating_fir_filter` instead of the dongle (the `--offset` mode)
- Terminal/UI is a **separate process** talking to `rx.py` over UDP — operator can attach/detach without restart
- **Live whitelist/blacklist/tag reload** without restart

This is the pattern op25 has used in production for years. It's the reference success case in our own stack.

**Sources:**
- [op25 README](https://github.com/boatbod/op25/blob/master/README.md)
- [op25 terminal.py — UDP JSON command protocol](https://github.com/boatbod/op25/blob/master/op25/gr-op25_repeater/apps/terminal.py)
- [GR Runtime Updating Variables (wiki)](https://wiki.gnuradio.org/index.php/Runtime_Updating_Variables)
- [GR Message Passing](https://wiki.gnuradio.org/index.php/Message_Passing)
- [GR Polyphase Channelizer](https://wiki.gnuradio.org/index.php/Polyphase_Channelizer)

---

## Decision matrix

### Option A — Build SB5 analog demod as a GR flowgraph, ham2mon-patterned (RECOMMENDED)

- **What:** Python-controlled GR flowgraph: N parallel AM/NFM demodulators inside one source, msgq command bus, JSON command schema, `safe_restart` never needed for tuning. Mirror op25's architecture exactly.
- **Cost:** 2-4 weeks focused. ham2mon gives us ~50% of the DSP code; op25's control plane shows us how to structure the schema.
- **Outcome:** rtl-airband goes away. No more SDRplay wedges from squelch changes. Tracker can run continuously without restart cycles. Phase 1 + Phase 2 + Phase 3 (auto-gain) become straightforward to implement.
- **Risk:** GR 3.10 port of ham2mon's `receiver.py` (1-3 days). Per-channel AM demod CPU profile on Micro (need a benchmark; SDRangel docs suggest Pi 3B+ "acceptable" for the equivalent work).
- **North-star fit:** Best. Fast, rock-solid, enterprise-class, deployable, all aligned.

### Option B — Keep rtl-airband, add downstream audio gate

- **What:** rtl-airband stays with squelch wide open (never restarts for our changes). Python process reads its raw audio output, applies adjustable squelch + hit detection downstream, publishes to icecast.
- **Cost:** ~1 week.
- **Outcome:** Bandaid. We never restart rtl-airband for squelch, but if rtl-airband itself crashes we're back to the wedge dance. Auto-gain still requires rtl-airband restart.
- **North-star fit:** Partial. Stops the bleeding without curing the disease.

### Option C — SDRangel headless substrate

- **What:** Migrate analog scanning to SDRangel via its REST API + Frequency Scanner plugin. SB5 becomes orchestrator.
- **Cost:** Maybe 2 weeks but with high uncertainty (ARM headless build risk, RSPduo dual-tuner cap at 2 MHz over SoapySDR, Qt dependency footprint).
- **Outcome:** Production-grade demod, but we trade open-pluggable for someone else's framework. SDRangel is an active project that could pivot.
- **North-star fit:** Mediocre. Deployability gets messier.

### Option D — SDRplay's SDRConnect (Linux + WebSocket API)

- **What:** Use SDRplay's own server software. Native RSPduo, WebSocket JSON API (v1.0.8, March 2026), multiple VRX demodulators per device.
- **Cost:** ~1-2 weeks integration.
- **Outcome:** Best-in-class for RSPduo, but closed-source. SB5 becomes dependent on SDRplay's roadmap. No native Icecast output (would route through PipeWire). Closed-source conflicts with "deployable" if SB5 ever goes anywhere other than SDRplay hardware.
- **North-star fit:** Strong if Will accepts closed-source dependency; weaker if "deployable" implies portable across SDR ecosystems.

---

## My recommendation

**Option A.** The pattern is proven in our own codebase (op25). ham2mon hands us the AM demod implementation as a starting point. The engineering work that remains is the control plane — schema, dashboard wiring, persistence — which is what SB5 is already good at. We replace the most-broken part of the stack with the most-mature pattern in our stack. 2-4 week cost is real but bought is permanent freedom from the SDRplay wedge problem.

The redesign also makes Phase 2 (auto squelch tracker) and Phase 3 (auto gain) ship cleanly. Today they fight the restart cost. Tomorrow they're just setter calls.

---

## Falsifiable claims worth verifying before committing

1. **ham2mon's `receiver.py` ports to GR 3.10 in less than 3 days.** Verify by cloning, running `./ham2mon.py -d 1 -f 124E6 -r 2E6` on RTL-SDR with GR 3.10 installed. If it requires major rewrite, Option A cost balloons.
2. **`pfb_channelizer_ccf` supports runtime `set_channel_map()` for adding/removing active channels without flowgraph rebuild.** Critical for "operator clicks chip, scan list updates" UX. Verify in [pfb_channelizer_ccf API ref](https://www.gnuradio.org/doc/doxygen-v3.7.10/classgr_1_1filter_1_1pfb__channelizer__ccf.html).
3. **SDRplay RSPduo master/slave mode works under SoapySDR for GR-based access on Linux ARM.** Critical — if it requires SDRplay's proprietary API instead of SoapySDR, ham2mon's `osmocom_source` won't drive both tuners. Verify with a 30-min spike: `gr-osmosdr --source "soapy=0,driver=sdrplay,rspduo_mode=master"` and confirm both tuners stream.
