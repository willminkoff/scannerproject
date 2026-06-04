# SB5 Analog Demod Rewrite — Project Plan

**Decision:** Replace rtl-airband with a Python-controlled GNU Radio flowgraph patterned on op25, borrowing DSP code from ham2mon.

**Driver:** rtl-airband's lack of hot config reload forces SIGKILL → SDRplay-wedge cycles on every user action. Working around this is whack-a-mole. The fix is to put the analog demod on the same architectural pattern op25 already uses successfully (long-running flowgraph + JSON command bus + setter calls — no restarts for config changes).

**North star:** SB5 is a fast, rock-solid, enterprise-class deployable tool.

---

## Phases

### Phase 0 — Validate the assumptions (1-2 days) — **COMPLETE 2026-06-03**

Three falsifiable claims have to hold or the plan changes. Spike before committing.

1. **ham2mon's `receiver.py` ports to GR 3.10 cleanly.** — **VALIDATED.** Phase 0 spike confirmed: minor API renames (`firdes` and `window`), 2to3 cleanup, no rewrite needed.
2. **`pfb_channelizer_ccf` supports runtime `set_channel_map()` for adding/removing active channels without flowgraph rebuild.** — **VALIDATED, with caveat.** Spike confirmed runtime channel-map mutation is fragile; design therefore uses the pre-allocated `freq_xlating_fir_filter_ccc` pool pattern (Section 4 of design doc). Polyphase channelizer remains a Phase 5+ option only if channel counts exceed ~32.
3. **SDRplay RSPduo master/slave mode works under SoapySDR for GR-based access on Linux x86.** — **VALIDATED.** Single `gr-osmosdr` process drives both tuners via `soapy=0,driver=sdrplay,rspduo_mode=master` / `=slave`. (Note: Micro is Intel x86, not ARM — original plan text said ARM and was wrong; CPU headroom is plentiful.)

**Exit criteria:** all 3 hold → Phase 1. Any fails → revisit decision. All 3 held → Phase 1 begins.

### Phase 1 — Minimal one-channel proof (3-5 days)

Single AM channel demod via GR flowgraph on Micro. Operator can change frequency / squelch / gain at runtime via a JSON command. Output is raw audio (to local file or fifo, not icecast yet).

**Validates:** the core architectural primitives work on real hardware. No production wiring yet.

### Phase 2 — Multi-channel scanner (3-5 days)

N parallel AM demodulators in one source via `freq_xlating_fir_filter` + per-channel squelch. Scanner control loop assigns active channels. Operator can add/remove channels live, adjust squelch live, no restart.

**Validates:** the multi-channel pattern from ham2mon scales to SB5's needs.

### Phase 3 — Hit detection + Icecast output (2-3 days)

Per-channel squelch-open detection (the hit log writer SB5 already uses), audio publish to `ANALOG.mp3` mountpoint. New code occupies the rtl-airband mountpoint when the new flowgraph runs; rtl-airband still installed but not started.

**Validates:** end-to-end with the existing UI. Can swap rtl-airband for the new demod on a feature flag.

### Phase 4 — Ground band + cutover (1 week)

Replicate Phase 2/3 for the ground band (NFM). Integrate with existing dashboard endpoints. Migrate favorites, presets, AUTO tracker, hit log writer. Retire rtl-airband from the active stack.

**Validates:** full production parity. Phase 1+2+3 of the squelch story all work without the wedge problem.

---

## Branch + naming conventions

- **Branch:** `gr-demod/airband` (feature branch, merges to main only when Phase 4 hits parity). Locked in 2026-06-03.
- **Module name:** `chirp`. Locked in 2026-06-03.
- **Module location:** `chirp/` at repo root (sibling of `disco/`, `ui/`, `scripts/`).
- **Systemd unit names:** `gr-demod@airband.service`, `gr-demod@ground.service` (unit names match the branch for operator continuity; the Python package they exec is `chirp`).

## Architecture decisions — resolved in design doc 2026-06-03

All listed in `SDR_DEMOD_DESIGN_2026-06-03.md`. Summary:

- **Command bus:** UDP JSON (op25 pattern). Resolved.
- **Audio output:** python-shout (libshout direct, in-process). Resolved.
- **DDC strategy:** per-channel `freq_xlating_fir_filter_ccc` with a pre-allocated pool of 32 channels (conservative on Intel x86). Resolved.
- **Process model:** one daemon per band (`gr-demod@airband`, `gr-demod@ground`). Resolved.
- **Channel spacing (v1):** 25 kHz only. 8.33 kHz deferred. Resolved.
- **Mode switching:** mode (`am`|`nfm`) baked at `add_channel` time; no runtime AM↔NFM flip. Resolved.
- **Hit-log retention:** 30 days under `/var/log/chirp/`. Resolved.
- **Mountpoint cutover:** Phase 4 overwrites `/ANALOG.mp3` / `/ANALOG_GROUND.mp3` directly. Resolved.

## Non-goals for this project

- Replacing op25 — it works fine.
- Replacing scanner-vfo — it works fine.
- Building a GUI for the demod — control happens through airband-ui's existing dashboard.
- Supporting non-SDRplay hardware — RTL-SDR support is a bonus if it falls out, not a requirement.

## Acceptance criteria for "done"

- rtl-airband uninstalled from active systemd from Micro.
- All four Phase 1-3 squelch features (presets, AUTO tracker, per-channel sanity, poison-clamp) ship without restart costs.
- Mean-time-between-wedge increases by at least 10x.
- Operator chip click → audible threshold change in under 1 second.
- Phase 3 (auto gain) becomes a straightforward feature add, not an architecture battle.
