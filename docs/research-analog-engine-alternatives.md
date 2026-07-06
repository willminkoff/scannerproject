# Research: Should chirp be replaced as the analog engine?

**Date:** 2026-07-06
**Branch:** `mac-mini-port`
**Status:** Decision made — stay on chirp. Documenting so this doesn't get re-litigated from scratch next time CPU looks scary.
**Question:** chirp (the custom GNU Radio analog daemon) was measured at 250-400% CPU per band on the M1 mini, driving load average to 100-190 and repeatedly killing the ground Icecast mount. Before treating that as a chirp-architecture problem, this asks: is there an off-the-shelf engine that should replace it — SDRangel server, SDRTrunk (already the digital engine), rtl_airband (the incumbent chirp replaced in June), or op25+liquidsoap (a pattern the wider feed community uses)?

---

## TL;DR

| Question | Answer |
|---|---|
| **1. Was the CPU saturation actually architectural?** | **No.** Root-caused live (profiler + code + git archaeology, all agreeing): `daemon.py` prebuilds a full 2 Msps demod branch for every `max_channels` pool slot regardless of how many are claimed — parking only mutes the squelch *downstream* of the front-end FIR stages. Uncapped on the M1 (32/64 slots — the branch diverged before origin/main's ground-cap commit), that's 96 always-hot FIR chains, 409/793 threads, 372%/250-400% CPU. Capped to 12/16 (matching micro's proven production values): threads dropped to the predicted 169/217 exactly, CPU to ~130-200% combined, load average from 100-190 to 30s, and the SDR source went from blocked 88% of the time on downstream buffer space to waiting on the USB device like a healthy real-time pipeline. **A config fix, not a rewrite.**
| **2. SDRangel server (sdrangelsrv)?** | **No.** Capable on paper (REST API, FrequencyScanner does retune-across-a-list, native sdrplayv3 plugin moots the old Soapy 2 MHz objection) — but zero 24/7-appliance precedent anywhere, an open bug that IS the appliance failure mode (#2782: scanner parks on signal forever, silently), a documented SDRplay-on-macOS-ARM silent-stall class (#1917) SDRangel can't recover from, no Icecast egress (needs a new UDP->liquidsoap bridge = new custom code at exactly the seam where the 6/18 all-day dead-air incident lived), and none of chirp's no-third-state contracts transfer.
| **3. SDRTrunk for analog (not just P25)?** | **No — architecturally disqualified, not just risky.** Streaming for *every* channel type, conventional included, is call-based: record-to-completion, queue, serialized playback (issue #1071, live-continuous-streaming request open since **July 2021**). It is explicitly not a scanner — conventional channels must all fit inside one tuner's instantaneous passband simultaneously, no LO hopping ever; covering 18 MHz of airband needs a HackRF-class tuner, not a retune scheduler. And it's a JavaFX GUI with no headless control surface (remote-control request open since **2017**). Keep it exactly where it is: the P25 digital engine.
| **4. rtl_airband (the engine chirp replaced)?** | **No — both original replacement reasons still hold in the current release (v5.2.0, May 2026).** Verified against source: SIGHUP is bound to the same handler as SIGINT — it still *exits*, not reloads; every squelch/gain/preset change is still edit-config -> kill -> cold restart with full SDRplay re-acquisition. SDRplay is still reached only via the same SoapySDR/`sdrplay_apiService` stack with zero recovery logic. Its scan mode is also strictly weaker than chirp's (one channel per device, serial, hardcoded 5 Hz step) and it has its own lies-healthy modes (stats file persists stale on a hang; a wedged input can leave the mount connected-and-silent indefinitely).
| **5. op25 + liquidsoap?** | **Category error.** op25 is a P25/digital decoder; it cannot demodulate AM airband at all. Already ruled out on macOS entirely in the 2026-07-04 SDRTrunk research (zero macOS track record, ALSA/Pulse-only audio egress). The community pairs it with liquidsoap for *digital* feeds — the role SDRTrunk already fills here, and better, on this OS.
| **Bottom line** | **Stay on chirp.** The "what does the feed community run" framing actually validates the decision: the community's own answer for analog airband feeds is rtl_airband, which this project already ran and replaced for cause. ScannerBox's delta over any of these — parallel-pool demod + LO hopping + a live squelch/gain command bus + hit-event feed + no-third-state contracts — is precisely the part none of them ship, which is why chirp exists. Revisit only under the conditional ladder in §4 below, and only if the CPU story gets bad again *after* the cap fix has had a real soak. |

---

## 1. Why this came up

chirp-airband and chirp-ground were each burning 250-400%+ CPU continuously on the M1 mini (8-core, 8 GB) with only 2-3 channels actively claimed per band — nowhere near the configured `max_channels` of 32/64. Load average sat at 100-190. Icecast was killing the ground mount on `source-timeout` every 30-90 seconds because the publish loop couldn't stay inside real time under that load. Before concluding "GNU Radio is too heavy for this box, replace the engine," the CPU was root-caused first — the standing rule being that a decision this size shouldn't be made on top of an unexplained number.

**Root cause (profiler + code + git archaeology, independently, all agreeing):** `chirp/daemon.py` builds one `Channel` object (12 GR blocks -> 12 threads under GR 3.10's thread-per-block scheduler) for every `max_channels` pool slot at flowgraph construction, live or not. `set_parked()` only moves the squelch threshold — the three front-end FIR decimation stages sit *upstream* of the squelch at the full 2 Msps input rate and never stop. A parked slot costs the same as a live one. This is deliberate load-bearing design (the `add_ff` mixer needs every input port fed, and the audio-flow watchdog's block-stall detector depends on parked branches streaming zeros) — it's just uncapped on the Mac. Micro ran this same code capped at `CHIRP_MAX_CHANNELS=12` (airband, committed) and `=24` (ground, live-only drop-in — exactly the SB7 P3 flaw "pool caps still via live drop-ins, not repo"). The `mac-mini-port` branch diverged before origin/main's `77ddce6` baked the ground cap into `ground.json`, so the Mac was running the raw uncapped defaults.

**Fix:** `airband.json` `max_channels` 32->12, `ground.json` 64->16 (+ `vad_enabled=false` on ground, matching airband, removing 16 Python VAD-gate threads doing numpy scoring on mostly-parked zero-streams). Verified live: threads 409/793 -> 169/217 (exact match to the `12*12+25` / `16*12+25` prediction), CPU to ~130-200% combined, load average into the 30s, zero mount drops in 35+ minutes, and — the check that actually matters — a re-profile of the SDR source thread showed it waiting on the USB device again instead of blocked ~88% of the time on buffer backpressure. That's real-time audio restored, not just a smaller CPU number. See commit `9630f53`.

With that fixed, the forcing function for a replatform mostly evaporates — but since the question was already on the table, it got investigated properly rather than dropped.

## 2. Options evaluated

### SDRangel server (sdrangelsrv)

Genuinely closer to viable than the June 2026 brief gives it credit for — every ground the original rejection stood on has shifted:
- "ARM headless build risk" was about aarch64 Linux; MacPorts today ships `sdrangelsrv` with `+server +sdrplay` on macOS (lagging upstream: 7.25.1 vs current 7.27.1).
- "2 MHz SoapySDR cap" is moot — the native `sdrplayv3` plugin bypasses SoapySDR entirely.
- "Qt footprint" doesn't matter on a Mac mini with headroom.
- The FrequencyScanner plugin is capable: it retunes the device center frequency across a list wider than the passband, has per-row AM/NFM demod handoff with threshold/squelch overrides, and Uniden-style hold-on-hit (`WAIT_FOR_END_TX` + retransmission hang). Verified working on this exact RSPduo on 2026-06-21 (`docs/sdrangel-scan-38380.md`).

It still loses on operational grounds:
1. **Zero 24/7-appliance precedent.** Nobody documented runs SDRangel as a scanner-feed appliance; the feed community runs SDRTrunk, OP25+liquidsoap, or rtl_airband.
2. **[f4exb/sdrangel#2782](https://github.com/f4exb/sdrangel/issues/2782)** (open, 2026-06-30): the scanner stops permanently on a signal and the handed-off demod never re-activates. No crash, no exit code — a textbook third state, directly in the critical path.
3. **[f4exb/sdrangel#1917](https://github.com/f4exb/sdrangel/issues/1917)**: SDRplay on Apple Silicon macOS has a documented silent-stall class (device stops delivering samples, revivable only via the SDRplay service ritual). SDRangel has no watchdog, no device-loss reconnect, and doesn't even restore its device configuration on server restart — every restart needs an external reconfigure script before audio flows again.
4. **No Icecast egress at all.** The only audio path out is UDP copy (L16/G722/Opus) to an external bridge (liquidsoap/ffmpeg -> LAME -> Icecast) — two new supervised processes of custom glue at exactly the seam where the 2026-06-18 all-day dead-air incident happened, minus the no-third-state layer chirp wraps around that seam today.
5. **Integration cost is real.** ~40% of the stack survives untouched (Icecast, broker, UI chrome, digital/VFO), but the UDP command bus, the adapter's live squelch/noise-floor semantics, hit JSONL, metrics/alerts, and the entire wedge-detection layer (spurious-stop detection, source-contract validation, structured exit codes, prod-mount refusal) would need rebuilding as external sidecars around a binary that can't be instrumented from inside. Realistic estimate to parity: 4+ weeks.

### SDRTrunk, repurposed for analog

Already the chosen P25 engine here, so "just use it for analog too" was worth checking properly rather than assuming. It is disqualified on three points that are architectural, not maturity issues:

1. **Streaming is call-based for every channel type, conventional included** (verified in `BroadcastConfiguration.java`: a per-call delay plus a max-queue-age purge). Each squelch-gated transmission is recorded to completion, queued, and streamed serially — never a live mix. Real-time/continuous streaming is [DSheirer/sdrtrunk#1071](https://github.com/DSheirer/sdrtrunk/issues/1071), open since **July 2021**, no milestone. Community-reported delay: 4-15 minutes, worse on quiet feeds. `ANALOG.mp3` and `ANALOG_GROUND.mp3` are live mixes today; this would be a fundamentally different (and worse) product.
2. **It is explicitly not a scanner.** Conventional channels are fixed DDC allocations that must all fit inside one tuner's current passband simultaneously — no LO hopping, no retune-across-a-list, for any version including the in-development 0.7.0. An 18 MHz airband list needs a HackRF-class tuner (~20 MHz), not a hopping scheduler.
3. **No control surface.** JavaFX GUI only; headless/remote control has been requested since 2017 ([#212](https://github.com/DSheirer/sdrtrunk/issues/212)) with no progress. The UI's live squelch slider has nothing to talk to.

Also open in 2025-2026: NBFM squelch tails up to 5s after carrier drop ([#1737](https://github.com/DSheirer/sdrtrunk/issues/1737)), squelch settings not persisting ([#2265](https://github.com/DSheirer/sdrtrunk/issues/2265)), no CTCSS tone squelch. SDRTrunk stays exactly where SB7 already put it.

### rtl_airband (the incumbent)

The real head-to-head, since this is what chirp replaced in June and what the wider community actually runs for this exact use case. Checked against current `main` (v5.2.0, 2026-05-22) rather than trusting the June writeup to still be true:

- **Still restart-only.** `sighandler()` in `src/rtl_airband.cpp` binds SIGHUP to the same exit path as SIGINT/SIGTERM. No reload, no control socket, no API — grep of the current source confirms no reconfig code path exists anywhere. Every squelch/gain/preset change is still config-edit -> kill -> cold restart with full device re-acquisition. The original replacement rationale is unchanged.
- **SDRplay still rides the same unmanaged stack.** Input drivers are `rtlsdr`/`mirisdr`/`soapysdr`/`file` only — SDRplay is reachable exclusively via SoapySDR + SoapySDRPlay3, untested in rtl_airband's own CI, with no recovery logic if `sdrplay_apiService` wedges.
- **Scanning is strictly weaker than chirp's.** Scan mode allows exactly one channel per device, stepping serially at a hardcoded 5 Hz with a hardcoded 2s post-signal hang, no dwell/priority config (feature request open since Feb 2026, zero replies; the maintainer's own 2023 "virtual devices" scan redesign is still unimplemented). chirp's parked pool demodulates every in-window channel simultaneously *and* LO-hops.
- **Its own lies-healthy modes exist:** no icecast-connection-state metric in the stats file; the stats file persists stale on disk if the process hangs; a wedged input that doesn't cleanly transition to a failed state can leave the mount connected-and-silent indefinitely.
- Credit where due: a real 10-second Icecast reconnect loop and a Prometheus-format stats file — both of which chirp already matches or beats post-SB7.3.
- Project health: single maintainer, ~annual releases, 2026 priorities are test infrastructure, not features. Notable friction: Broadcastify's own submitted PR for call-based output was declined and the author threatened to fork.

### op25 + liquidsoap

Not evaluated in depth — it's a category error. op25 decodes P25/digital only; it has no AM demodulator and cannot serve the airband/ground analog role under any configuration. It was also already ruled out on macOS entirely in the 2026-07-04 SDRTrunk research (zero macOS track record, ALSA/Pulse-only audio egress, last known build attempt 2017). The pattern is real in the feed community, but for *digital* trunking feeds — the role SDRTrunk fills here already, natively, on the OS this project actually runs.

## 3. Recommendation

**Stay on chirp.** None of the four alternatives clear the bar, and the original forcing function (unexplained CPU) turned out to be a two-line config fix rather than an architecture problem. The "what does everyone else run" framing, taken seriously, argues *for* the current design: the community's answer for analog scanner feeds is rtl_airband, this project already ran it and replaced it for reasons that independently re-verified as still true, and ScannerBox's actual differentiators — parallel-pool demod, LO hopping across a list wider than one tuner's passband, a live command bus the UI drives squelch/gain through, structured hit events, and the no-third-state reliability contracts (armed audio probe, spurious-stop detection, structured exit codes) — are exactly the parts no off-the-shelf engine ships. Swapping engines was never going to avoid rebuilding those; it would just mean rebuilding them around a black box instead of inside code this project already owns and understands.

## 4. What would change this answer

Not "never revisit" — a conditional ladder, in order:

1. **If chirp CPU becomes untenable again** after the pool-cap fix has had a real soak (days, not minutes) — go to a GR-side fix first: replace the per-channel `freq_xlating_fir_filter` duplication with a single polyphase channelizer (`gr.filter.pfb.channelizer_ccf`) feeding lightweight per-channel back-ends. This preserves every reliability contract in chirp today (the icecast sink, the audio probe, the source validator, the structured exit codes) at a fraction of the integration cost any external engine would demand. Rated medium risk (uniform-grid channel constraint, real refactor) but it's still fixing the load-bearing code, not replacing it.
2. **Only if GR itself proves architecturally hopeless on this hardware** — run a real 72-hour `sdrangelsrv` bench soak (MacPorts `+server +sdrplay`, on the Intel mini so it doesn't touch production) against a hard adoption bar, all four required: zero silent device stalls in 72h; `#2782` fixed or not reproducible on whatever version is installable; continuous listener audio verified through the scanner's mute-while-scanning window; total CPU meaningfully under the capped-chirp number measured today. If it clears every bar, migrate ground first (RTL-SDR, simpler, lower stakes) behind a UDP-protocol shim so `ui/` stays untouched, with chirp retained on airband as the rollback. Never a big-bang swap.
3. **SDRTrunk-for-analog and rtl_airband are not on this ladder.** The first is architecturally incompatible with live streaming and wide-list scanning (not a maturity gap that a future release closes); the second is the thing already replaced, for reasons this research re-confirmed still hold.

**One action that pays off regardless of which rung is ever reached:** the external Icecast-mount audio prober (decode the actual public stream, measure RMS from outside the daemon, no restart authority yet) — already an SB7.4 line item, and the one piece of work that would have caught both the 2026-06-18 wedge and today's degraded-audio state, independent of what produces the audio.
