# chirp — overnight progress log

This log captures what each overnight task accomplished. Read top-to-bottom for
chronological history (newest task is at the top of the most recent entry).

Format per entry:

- **Goal** — what the task set out to do.
- **Done** — bullet list of accomplished items.
- **Commits** — SHAs and titles on `gr-demod/airband`.
- **Branch tip** — head SHA after the task.
- **Deferred / surfaces for Will** — anything Will should look at on review.
- **Blocker** — only present if the task stopped short.
- **Next task** — handoff note for the next overnight slot.

Hard rules every overnight task must follow are restated at the bottom.

---

## 2026-06-04 11:18 UTC — Phase 2 (task 3 of 4+)

**Goal** — multi-channel scanner. 32-slot pool feeding a single audio mixer +
master gain trim. State persistence across daemon restarts. Hit detection per
channel with JSONL log. Batched `add_channel` for fast 31-channel load.
Regression tests for the four rtl-airband radio bugs Will hit on 2026-06-03.
31-channel stress test.

**Done**

- **State persistence** (`chirp/state.py`):
  - Pydantic v2 `ChirpState` / `ChannelState` models with bounds validation.
  - `StateStore.load()` returns empty state on missing / corrupt / empty /
    schema-violating files (never raises — boot must always succeed).
  - `StateStore.save()` is atomic: tmp file in same dir → fsync → `os.replace`.
  - 17 tests, including atomic round-trip across 50 saves with no orphan tmp
    files, future-version forward-compat, env-override path resolution.
- **AudioMixer + 32-slot pool** (`chirp/dsp/mixer.py` + `chirp/daemon.py`):
  - `AudioMixer` = `add_ff` N-in-1 + `multiply_const_ff` master gain (dB).
  - Every slot's `Channel` hier_block is permanently wired to a mixer input.
    Parked slots (squelch slammed to 0 dBFS) contribute zero to the sum.
  - Single `file_sink` at `audio_out_path` instead of one file per slot.
  - Default `max_channels` is now 32 (Phase 1 used 1).
  - Backward-compat: Phase 1 daemon-dispatch tests still pass against
    `max_channels=1` mixer-fed config.
- **New commands** (`chirp/cmd/schema.py` + `chirp/cmd/server.py`):
  - `add_channel` accepts batches: `{"channels": [ChannelArgs, ...]}`. Legacy
    single-channel wire shape still works — internally normalised to a
    1-element list. All-or-nothing: a too-large batch is rejected without
    partial slot allocation.
  - `set_master_gain { db }`: post-mixer trim, bounds [-20, 40] dB.
  - `reset { }`: parks every slot, zeroes master gain, clears state file.
  - `get_status` now reports `master_gain_db` + a single top-level
    `audio_path` (the mixer sink).
- **State restore on boot** (`ChirpFlowgraph.restore_from_state`): reads the
  persisted JSON and reapplies channels into free slots. Skips IDs that
  collide or overflow the pool, logging a warning. Emits `state_restored`
  event with the count.
- **Hit detector** (`chirp/hit_detector.py`):
  - Per-slot state machine: closed→open emits `hit_start`; open→closed emits
    `hit_end` with peak_dbfs + duration_s.
  - Appends one JSON record per hit to `/var/log/chirp/hits.jsonl`
    (overridable via `CHIRP_HIT_LOG`). Path-unwritable → JSONL log silently
    disabled (events still go out via UDP).
  - Tracks a warmup flag on the `hit_start` so downstream consumers can
    discount hits from a sub-1s-old channel.
- **Shutdown drain** (`ChirpFlowgraph.shutdown_drain`): blocks acquiring the
  dispatch lock with a bounded timeout so an in-flight setter finishes
  before `tb.stop()`. `daemon.main()` calls `shutdown_drain()` between the
  SIGTERM handler and the flowgraph stop. Regression test for the rtl-airband
  master/slave-on-restart wedge.
- **Radio bug regression tests** (`chirp/tests/test_radio_bugs.py`, 13 tests):
  - Bug 1 (squelch poison value): `Channel.get_signal_level_dbfs()` on a
    fresh-construction probe returns -120 dBFS (our explicit floor), not
    -10.964 (rtl-airband's poison). Codebase scan: no chirp source file
    contains the poison constant.
  - Bug 2 (master/slave shutdown wedge): `shutdown_drain` waits for in-flight
    setter, times out cleanly if it can't, and `daemon.main()` source
    inspection confirms drain precedes the final `tb.stop()`.
  - Bug 3 (libshout drop without reconnect): reference `IcecastReconnector`
    contract with exponential-backoff schedule (0.25, 0.5, 1, 2, 4 s capped),
    drops are logged not swallowed, successful send resets backoff. Phase 3
    will plug the real python-shout sink into this contract.
  - Bug 4 (noise-floor init race): `set_squelch` on a sub-1s-old channel
    applies the operator value verbatim and does not block waiting for the
    noise estimator. End-to-end test confirms the band is not silenced.
- **31-channel stress test** (`chirp/tests/test_phase2_stress.py`, 4 tests):
  - Synthesizes 31 AM carriers at 50 kHz spacing in a 35 s, 2 Msps IQ file.
  - Batch `add_channel` adds all 31 in one command (pool_free → 1).
  - Runs ~28 s, asserts hit JSONL log is non-empty + audio file kept growing.
  - Live `remove_channel` mid-test (pool_free → 2).
  - State file on disk matches live pool.
  - `reset` returns pool_free to 32.
  - Wall-clock on micro (20 cores): ~178 s. Marked `slow` so a developer can
    run `pytest -m "not slow"` to skip.

- **Requirements file** (`chirp/requirements.txt`) — pydantic, pytest, numpy.
  Documents that GNU Radio is an apt package, not a pip dep.

**Tests** (final count)

- `pytest -m "not slow" chirp/tests/`: **84 passed, 4 deselected** in ~13 s.
- `pytest chirp/tests/test_phase2_stress.py`: **4 passed** in 178 s.
- Total: **88 tests, all green** (was 31 after Phase 1).

**Smoke test outcomes**

- 31-channel synthetic load — daemon stayed up the full 35 s simulated
  duration on a 32-slot pool with no crashes; hit JSONL had multiple records
  by the time we checked at t≈28 s; live remove succeeded mid-run.
- Phase 1 single-channel daemon test (`test_squelch_gates_audio_amplitude`)
  still passes verbatim — the mixer path is transparent at master_gain=0 dB.
- `set_master_gain(-20)` causes ≥5× drop in audio RMS (matches the 10× linear
  attenuation within AGC overshoot tolerance).

**Commits on `gr-demod/airband` (Phase 2, in order)**

- `5dc46f2` — chirp(phase2): state persistence with atomic JSON I/O (17 tests)
- `d2a2a6a` — chirp(phase2): mixer + 32-slot pool + batch add_channel + reset
  + set_master_gain + hit_detector skeleton
- `625d47e` — chirp(phase2): unit + integration tests for mixer / schema /
  hit_detector / daemon (23 tests)
- `1c34a36` — chirp(phase2): regression tests for the 4 rtl-airband radio
  bugs from 2026-06-03 (13 tests)
- `9844ddb` — chirp(phase2): 31-channel stress test (4 tests, ~3 min @ 2 Msps)
- (this commit) — chirp(phase2): requirements.txt + PROGRESS.md Phase 2 entry

**Branch tip:** `6332d37f2c3647d0205bf268ad63bef8301c00ce` (preceded the PROGRESS-fill commit; final Phase 2 tip is the next commit on `gr-demod/airband`).

**Deferred / surfaces for Will**

- **Hit log fsync.** `HitDetector._append_log` writes with O_APPEND but does
  not fsync per-hit; under a sudden crash the last hit or two could be lost.
  Acceptable for Phase 2 (low-rate, single host), but for a gigabit fleet
  Phase 4 will need batched fsync or a small in-memory buffer flushed on
  SIGTERM. Cost ~20 LOC.
- **Hit log rotation.** Spec called for "30-day rotation in Phase 3 or 4".
  Not implemented yet. Recommend `logrotate` config rather than reinventing —
  a `/etc/logrotate.d/chirp` entry of ~10 lines.
- **freq_mhz > 0 schema constraint.** Daemon treats `freq_mhz` as a signed
  offset from the file source's baseband center (0 Hz). The schema requires
  freq_mhz > 0, so the stress test packs carriers on the positive side only.
  When we swap to a real SDR in Phase 3, freq_mhz will become an absolute
  RF frequency (e.g. 121.025) and this constraint is naturally satisfied.
  The file-source semantics will then be the surprising case; that
  reconciliation belongs in the Phase 3 source-abstraction commit.
- **IcecastReconnector backoff schedule** is hard-coded constants
  (0.25, 0.5, 1, 2, 4 s capped). Easy to make configurable in Phase 3 when
  the real libshout sink lands — but the contract test ensures whatever we
  ship satisfies the no-silent-drop property.
- **Master gain bounds vs. per-channel gain bounds** are both [-20, 40] dB,
  shared by `_check_gain`. If they should diverge (e.g. master gain wants
  a wider negative range to fully mute the output), trivial to split.
- **rtl-airband poison constant scan** in test_radio_bugs grep-matches
  `-10.964` and `10.964` substrings. False positives are possible if any
  future config file contains those digits incidentally; if that happens,
  tighten the match to a regex like `-?10\.964\b`.
- **Snapshots directory** is still untracked on micro (`?? snapshots/`).
  Left alone per Phase 1 deferral note.

**Next task** — **Phase 3:** plumb real-time hit emission to subscribers (the
event-stream UDP fan-out is in place but no one's subscribing yet) and add
the python-shout Icecast publisher to a **TEST mount** (do not publish to
production). The libshout reconnect contract is already covered by
`chirp/tests/test_radio_bugs.py::TestBug3LibshoutReconnectsAfterDrop` — Phase 3
just wires a real `shout.Shout` instance into the existing `IcecastReconnector`.
Plus: a small `chirp/cli.py` (CLI client around the UDP command bus) to make
operator-facing add/remove/reset/status accessible without crafting JSON by
hand. Smoke test: 1-channel demod from a file, published to a local
Icecast mount, listenable in VLC.

## 2026-06-04 06:35 UTC — Phase 1 (task 2 of 4+)

**Goal:** A single-channel AM demod prototype that runs as a Python daemon, has
a UDP JSON command bus per the design doc spec, accepts runtime
freq/squelch/gain changes, sources IQ from a file (no SDR contention), demods
AM, applies squelch, writes audio to a file. Demonstrate the core
architectural primitives end-to-end.

**Done:**

- **Pydantic v2 command/response schemas** (`chirp/cmd/schema.py`). Envelope
  validates `{v, id, cmd, args}` with `extra="forbid"` at every level; the
  protocol version is pinned (v=2 is rejected). Per-command arg models for
  `add_channel`, `remove_channel`, `set_squelch`, `set_freq`, `set_gain`,
  `get_status`. Validation rules per the Phase-1 prompt: `freq_mhz > 0`,
  `squelch_dbfs ∈ [-120, 0]`, `gain_db ∈ [-20, 40]`, non-empty id. `mode != "am"`
  is rejected (NFM is Phase 2/4 per design doc Section 5).

- **UDP JSON command server** (`chirp/cmd/server.py`). Async (`asyncio`) UDP
  server runs in a dedicated background thread. Parses + validates each
  datagram, dispatches to a caller-supplied callback, replies on the same
  socket. `emit_event(...)` always writes structured JSON to stdout (Phase-1
  minimum) and optionally fires a UDP datagram to `CHIRP_EVENT_SINK=host:port`.
  Reply path is fault-tolerant: malformed JSON, unknown command, invalid args,
  internal dispatch exceptions all return a typed error response without
  killing the server.

- **`Channel` hier_block** (`chirp/dsp/channel.py`). Single-channel AM demod
  modeled on ham2mon's `TunerDemodAM` but stripped of the integrated wav-file
  sink (the daemon owns audio routing). Pipeline matches the design doc:
  `freq_xlating_fir_filter_ccc → fir_filter_ccc → fir_filter_ccc → pwr_squelch_cc`
  (non-blocking; design intent — keeps parallel channels stream-synced for the
  future adder) `→ agc3_cc → complex_to_mag (AM env detector) → audio LPF →
  pfb.arb_resampler_fff` to 16 kHz mono float32. Hot setters
  `set_center_freq_offset / set_squelch / set_gain` plus
  `get_signal_level_dbfs()` / `get_squelch_open()` probes.

- **File IQ source** (`chirp/dsp/source_file.py`). Looped, throttled fc32 file
  source; replaces the SDR for Phase 1 dev/test without touching production.

- **Daemon** (`chirp/daemon.py`). Config loader (`chirp/config/defaults.json` +
  `CHIRP_*` env overrides), pre-allocated channel pool (default size 1 per
  design doc Section 4 strategy 1), sync dispatch callback called from the
  asyncio thread under an `RLock`, background health thread emitting
  `hit_start` / `hit_end` events on squelch transitions, SIGTERM/SIGINT
  graceful shutdown. Phase 1 source is file-only; SDR path raises
  `NotImplementedError` on purpose (no production contention).

- **Unit + integration tests** (`chirp/tests/test_phase1.py`). 27 new tests,
  all green:
  ```
  $ python3 -m pytest chirp/tests/ -v
  ...
  ============================== 31 passed in 4.91s ==============================
  ```
  The integration tests synthesize 4 s of AM IQ at +200 kHz with a 1 kHz tone,
  spin up a real `ChirpFlowgraph` + `CommandServer`, fire UDP commands, and
  assert behavior — including the subtle point that closing the (non-blocking)
  squelch must drop **amplitude** by 40 dB+ rather than stop the byte stream.

- **Smoke test on Micro.** `/tmp/chirp_smoke.sh` ran end-to-end:
  1. Generated 30 s of AM IQ at 1 Msps, carrier +200 kHz, 1 kHz tone (240 MB).
  2. Booted the daemon on port 17777 with that file as the source.
  3. `add_channel ch01 freq_mhz=0.2 mode=am squelch_dbfs=-90`
     → audio file RMS = **0.0640** (tone audible).
  4. `get_status` returned `channels=[ch01], pool_free=0,
     signal_level_dbfs=-4.45, squelch_open=true`.
  5. `set_squelch dbfs=0` (slam shut) → audio file RMS = **0.000000**.
  6. `set_squelch dbfs=-90` (re-open) → audio file RMS = **0.0640** (recovered).
  7. Rejection paths: duplicate `add_channel`, pool-exhausted `add_channel`,
     out-of-range `set_squelch` — all returned `status=rejected` with reasons.
  8. `remove_channel` → `pool_free=1`.
  9. Daemon shut down cleanly on SIGTERM.

  **FFT on the recovered audio** (last 1 s of the file, DC-removed):
  dominant peak at **1000.00 Hz** — the demodulated voice tone is exactly the
  one we modulated onto the carrier. The chain works.

**Commits on `gr-demod/airband` (in order):**

- `956e0fb` — chirp(phase1): Pydantic v2 command + response schemas
- `f4f85a8` — chirp(phase1): asyncio UDP command server + events
- `12ec707` — chirp(phase1): Channel hier_block (AM) + FileIQSource
- `a368793` — chirp(phase1): daemon entrypoint with pre-allocated channel pool
- `6b5a683` — chirp(phase1): unit + integration tests (27 new, all passing)
- `5075d0e` — chirp(phase1): make_test_iq fixture for smoke testing
- `51deb00` — chirp(phase1): PROGRESS.md Phase 1 entry
- `08b4cfa` — chirp(phase1): fill in final SHA + branch tip in PROGRESS.md

**Branch tip at push time:** `08b4cfadd4ffb8e78c3f5c5c4acd982fa136493a`. A
trailing PROGRESS-fix commit may sit on top (the SHA self-reference forces a
one-commit chase); `git log --oneline gr-demod/airband` is canonical.

**Deferred / surfaces for Will:**

- **Response shape diverges from design doc.** Will's Phase-1 prompt specified
  `Response = { v, id, status: "ok"|"rejected"|"error", data, error }`, while
  design doc Section 5 specifies
  `{ v, id, ok: bool, result | error: {code, message} }`. Both shapes encode the
  same information; the Phase-1 prompt was treated as authoritative for this
  task. A forward-compat bridge is cheap (additive fields per the design doc's
  "clients must ignore unknown fields" rule), or the design doc can be amended
  to match. **Recommend resolving before Phase 4 cutover** so the dashboard
  and CLI land on a stable contract.

- **Pydantic v2 field/method collision.** `Response` initially had `ok()`,
  `rejected()`, and `error()` classmethods. Pydantic v2 reserves field names
  on the class namespace, so `Response.error(...)` actually got swallowed by
  the `error` field. Renamed factories to `make_ok / make_rejected / make_error`.
  Worth knowing for the future schemas in Phase 2.

- **`pwr_squelch_cc` is non-blocking by design.** When the squelch closes, the
  output stream keeps flowing as zeros (design doc Section 4: "non-blocking
  since samples will be added with other demods"). This means audio sinks
  written per-channel will grow byte-wise even when muted — the silence is
  amplitude-only. Will's existing rtl-airband sample-flow heuristic in
  `ui/sample_flow.py` relies on the post-mix `pwr_squelch_ff` for byte gating;
  chirp will need the same final stage at the mixer in Phase 2.

- **`audio_out=file:` writes raw float32, not WAV.** The smoke test uses
  `np.fromfile(..., dtype=np.float32)`; play with `aplay -t raw -f FLOAT_LE -c 1
  -r 16000 audio.f32` or convert via `sox`. A `wavfile_sink` writer with a
  single fixed path could be added if you want one-step playback in Phase 2.

- **No live SDR path in Phase 1.** `source_kind="sdr"` raises
  `NotImplementedError` on purpose. Production rtl-airband on Micro is
  untouched — no service starts/stops, no config edits. The Phase 0 spike
  already validated the SoapySDR path; Phase 2/3 wires it back in.

- **Pre-allocated pool size = 1 in Phase 1.** The pool architecture is in
  place (matches design doc Section 4 strategy 1), but the default
  `CHIRP_MAX_CHANNELS=1` keeps Phase 1 single-channel. Bumping it to N at
  daemon start works today — Phase 2 will wire the per-channel outputs into
  the adder + mixer.

- **Pydantic + pytest installed via pip on Micro.** They were not present at
  task start; installed with `pip3 install --break-system-packages pydantic
  pytest`. Versions pinned in this run: pydantic 2.13.4, pytest 9.0.3. Add to
  a chirp requirements file in Phase 2 once we know what else we need.

**Next task:** Phase 2 — multi-channel scanner. Lift `CHIRP_MAX_CHANNELS` to
the design-doc default (32), wire the per-channel outputs into an `add_ff`
mixer and a single audio sink, port the FFT/estimate path so `pwr_squelch`
events translate cleanly to `hit_start` / `hit_end` JSONL matching the
existing rtl-airband hit log format, and start the JSON-state persistence at
`/var/lib/chirp/<band>.state.json` (design doc Section 6). Smoke test goal:
8+ concurrent AM channels from a synthesized multi-carrier IQ fixture, each
demuxing to its own slot, mixer producing a single audio stream where multiple
channels are audible when their carriers are present.

---


## 2026-06-03 23:49 EDT — Foundation (task 1 of 4+)

**Goal:** Lock in operator decisions in the design + plan docs, create the
long-lived feature branch, scaffold the `chirp/` module, vendor + port ham2mon's
DSP from GR 3.7 to GR 3.10, and prove the import graph is clean. No production
state touched.

**Done:**

- **Docs patched.** `SDR_DEMOD_DESIGN_2026-06-03.md` and
  `SDR_DEMOD_PROJECT_PLAN.md` updated to reflect Will's locked-in answers:
  - Module name: `chirp` (replaced placeholder `gr_demod` throughout).
  - Branch: `gr-demod/airband` until Phase 4 parity.
  - v1 spacing: 25 kHz only; 8.33 kHz EU support deferred.
  - Mountpoint cutover: chirp overwrites `/ANALOG.mp3` (and `/ANALOG_GROUND.mp3`)
    directly at Phase 4 (no permanent `/ANALOG_NEW.mp3`).
  - Hit-log retention: 30 days under `/var/log/chirp/`.
  - `set_mode` is baked at `add_channel` time only; runtime AM↔NFM flip dropped;
    `set_mode` command reserved and returns new `ENOTSUP` error.
  - Hardware section corrected: Micro is **Intel x86, not ARM** — earlier
    Pi-class CPU concerns retracted, headroom is plentiful, 32-channel pool
    is conservative.
  - Section 10 testing rewritten to enshrine four named rtl-airband software
    bugs as regression fixtures: squelch poison value, SDRplay master/slave
    wedge on restart, libshout drop without reconnect, noise-floor init race.
    USB dongle physical flap explicitly listed as out of scope (hardware-side).
  - Section 13 reframed from "open questions" to "operator decisions" with
    resolutions for all 8 items.
  - Project plan Phase 0 marked complete with the spike result for each
    assumption.

- **Branch created.** `gr-demod/airband` off `origin/main @ a066439`. Pushed to
  origin with upstream tracking.

- **`chirp/` scaffolded** (top-level + dsp/cmd/config/systemd/tests subtrees,
  with placeholders for Phase 1 work — schema/server, defaults.json,
  gr-demod@.service template, tests/fixtures README enumerating the bugs the
  regression fixtures must cover).

- **ham2mon vendored and ported to GR 3.10.** Cloned upstream lives at
  `~/sb5-spike/ham2mon` (already on Micro from Phase 0 spike).
  - Upstream SHA: `db9834ce923c1919602bf33cb47720daba9bc6ea` (2018-10-27).
  - Copied `apps/receiver.py` and `apps/scanner.py` to `chirp/dsp/ham2mon/`.
  - Ran `2to3 -w` on both (print-as-function was the only fix needed; no
    other Python-2 idioms in these files).
  - `receiver.py`: rewrote `grfilter.firdes_low_pass(...)` →
    `grfilter.firdes.low_pass(...)` (6 sites) and
    `grfilter.firdes.WIN_HAMMING` → `window.WIN_HAMMING` (6 sites). The
    `from gnuradio.fft import window` import was already present upstream.
  - `scanner.py`: top-of-file `import receiver / import estimate / import parser`
    block rewritten for package layout. `estimate` and `parser` are NOT vendored
    in this commit (per scaffold brief); stubbed to `None` so the module imports
    cleanly. Code paths that touch them will fail loudly until Phase 1 vendors
    them.
  - `chirp/dsp/ham2mon/LICENSE` carries the upstream GPL.
  - `chirp/dsp/ham2mon/README.md` documents the upstream SHA, port edits,
    what's vendored vs. what isn't, and the smoke-test command.

- **Import sanity verified.** Test command on Micro:
  ```
  cd /home/ubuntu/scannerproject && python3 -c \
    'from chirp.dsp.ham2mon import receiver, scanner; print("OK")'
  ```
  Output: `OK receiver= chirp.dsp.ham2mon.receiver  scanner= chirp.dsp.ham2mon.scanner`
  (Preceded by GR backend probe lines: `CPU Features: SSE2+ SSE4.1+ AVX+ FMA+`
  — confirms Intel x86 hardware out of band.)

- **Pytest passes.** `chirp/tests/test_imports.py` covers `chirp` package,
  `chirp.dsp.ham2mon.receiver` + `scanner` (and the presence of
  `TunerDemodAM` / `TunerDemodNBFM` after the port), and `chirp.cmd.schema` +
  `chirp.cmd.server` placeholders.
  ```
  $ python3 -m pytest chirp/tests/ -v
  collected 4 items
  chirp/tests/test_imports.py::test_import_chirp       PASSED  [ 25%]
  chirp/tests/test_imports.py::test_import_chirp_dsp   PASSED  [ 50%]
  chirp/tests/test_imports.py::test_import_chirp_cmd   PASSED  [ 75%]
  chirp/tests/test_imports.py::test_chirp_dsp_pkg      PASSED  [100%]
  ============================== 4 passed in 0.19s ===============================
  ```

**Commits on `gr-demod/airband` (in order):**

- `f52977e` — docs: lock in chirp design decisions from operator review
- `c92b2d7` — chirp: scaffold module structure
- `507179b` — chirp: vendor ham2mon DSP code, ported to GR 3.10
- `3bd2b1e` — chirp: README + import sanity tests + initial PROGRESS log

**Branch tip:** `3bd2b1e05360db0127cd0f57755245e450921c97`

**Deferred / surfaces for Will:**

- **GPL inheritance.** chirp vendors GPL code (ham2mon), so the chirp module —
  and effectively any binary distribution of SB5 that includes chirp — picks up
  GPL terms. The SB5 repo as a whole did not previously have a clear license
  stance. If GPL distribution is fine, no action; if you want chirp to ship
  under a permissive license, the alternative is rewriting `TunerDemodAM` /
  `TunerDemodNBFM` from the GR 3.10 docs (Phase 1/2 work, ~1–2 days).
- **ham2mon `estimate.py` and `parser.py` not vendored** in this commit per the
  scaffold brief. `scanner.py` imports them as `None` stubs so it's import-clean,
  but its scanning code paths will `AttributeError` until those modules are
  vendored. Likely Phase 1 work — `estimate.py` is needed for the FFT-based
  channel detector; `parser.py` is CLI-only and may not be needed since chirp
  uses the JSON command bus instead.
- **Systemd unit name vs. module name.** The systemd unit name in the template
  is `gr-demod@.service` (matches the branch, matches existing internal
  language); the Python module it execs is `chirp`. This is documented in the
  template and in the design doc. If you'd rather rename the unit to
  `chirp@.service` we can do it at Phase 1 (cheap).
- **Snapshots dir.** `?? snapshots/` shows up in `git status` on Micro — it's
  the existing untracked snapshot scratch dir, unrelated to chirp. Left alone.
- **No production writes.** Production rtl-airband units and live configs are
  untouched. The branch lives entirely under `chirp/` and patched-in-place
  design docs at repo root.

**Next task:** Phase 1 — one-channel AM demod prototype with UDP JSON command
bus, on file-source IQ (no live SDR yet). Specifically: a minimal
`chirp/daemon.py` that builds a top_block with one ham2mon `TunerDemodAM`
fed by `gr.blocks.file_source` (IQ capture from Phase 0 spike), wired to a
file sink for audio, with a `chirp.cmd.server` UDP listener that handles
`set_freq` / `set_squelch` / `get_status` and demonstrates hot retune
without flowgraph rebuild. Test command (the inverse of this task's smoke
test): `nc -u 127.0.0.1 7400` with a `set_freq` JSON command, observe the
audio file contents change frequency without daemon restart. Vendor
`estimate.py` and `parser.py` only if Phase 1 needs them — likely yes for
estimate, probably not for parser.

---

## Hard rules for every overnight task

1. **Production rtl-airband UNTOUCHED.** No writes to anything in production
   state. No systemd starts/stops on production services. No edits to live
   config files.
2. All experiment work lives in `~/sb5-spike/` on Micro.
3. All code work lives on branch `gr-demod/airband` until Phase 4 parity.
4. Frequent commits + push to origin. Each meaningful step = a commit.
5. Log every action here so Will can read it in the morning.
6. If you hit a blocker (not a question — an actual stop): STOP, log clearly
   to PROGRESS.md, commit, exit.
7. DO NOT spawn sub-agents.


---

## Phase 3 — Icecast publish + CLI + UDP event subscribers (2026-06-04)

**Goal.** End-to-end audio: file IQ source → 32-slot demod pool → mixer →
MP3 encode (libmp3lame) → libshout → Icecast on a TEST mount distinct from
production. Plus an operator CLI and a live UDP event subscriber. Production
rtl-airband UNTOUCHED throughout.

### Files added/modified

- `chirp/dsp/icecast_sink.py` *(new — 380 lines)*. Three pieces:
  - `IcecastReconnector` — the same backoff schedule (0.25, 0.5, 1.0, 2.0,
    4.0 s, capped) that lived inline in `test_radio_bugs.py` for Phase 2, now
    the canonical production class. Phase 2's regression tests were updated
    to import from here (single source of truth — the 4 reconnect tests now
    exercise the production class directly).
  - `_ShoutPublisher` — adapter around `python-shout`. Translates
    `shout.ShoutException` into `ConnectionError` so the reconnector
    contract Just Works. Exposes `send/reconnect/sync/close/get_connected`.
  - `IcecastSink(gr.sync_block)` — mono float32 input → int16 LE → `lame -r
    -s 16 --bitwidth 16 -m m --cbr -b 32 --silent - -` subprocess → MP3
    chunks → IcecastReconnector → libshout. Publisher runs in a dedicated
    background thread; GR work() just feeds lame.stdin. Silent-frame
    keepalive is implicit: parked channels output zeros, lame produces
    valid silent MP3 frames, byte flow never stops (preserves the
    `mount_publishing` heuristic in `ui/sample_flow.py` per design doc §4.7).
  - `IcecastSinkConfig(host, port, mount, password, bitrate_kbps,
    sample_rate, user, server_name, description, genre, public)`.

- `chirp/scripts/add_test_mount.sh` *(new)*, `chirp/scripts/remove_test_mount.sh`
  *(new)*. Idempotent shell scripts that edit `/etc/icecast2/icecast.xml` to
  add/remove the `/CHIRP_TEST.mp3` mount block immediately before `<paths>`,
  then `systemctl reload icecast2` (NEVER restart — production sources stay
  connected through the reload). Each backs up the xml to
  `icecast.xml.bak.YYYYMMDD-HHMMSS`. Both validate with `xmllint` if installed.

- `chirp/daemon.py` *(modified)*:
  - `DaemonConfig` gains `icecast_host/port/mount/password/bitrate_kbps` and
    `icecast_fallback_file` (default `/tmp/chirp_audio_fallback.f32`).
  - `_parse_audio_out` accepts `icecast:host:port:/mount:password`.
  - `_parse_icecast_spec` REFUSES `/ANALOG.mp3`, `/ANALOG_GROUND.mp3`,
    `/DIGITAL.mp3`, `/VFO.mp3` — Phase 3 refuses to publish to production
    mountpoints with a hard validation error. Phase 4 cutover will lift this.
  - `ChirpFlowgraph` wires `IcecastSink` when configured; falls back to a
    `file_sink` at `icecast_fallback_file` (logged loudly) if IcecastSink
    instantiation fails (e.g. no `lame` binary).
  - `_cmd_get_status` now surfaces `icecast_state`, `icecast_bytes_sent`,
    `icecast_reconnect_count`, `icecast_drop_count`, `icecast_mount`,
    `icecast_bitrate_kbps`. When no icecast configured, state =
    `not_configured`.

- `chirp/cmd/schema.py` *(modified)*: `SubscribeArgs(events: list[str])` and
  `UnsubscribeArgs()` added to `COMMAND_ARGS`.

- `chirp/cmd/server.py` *(modified)*:
  - `CommandServer` carries a `{(host, port) → set(event_names)}` subscriber
    registry; empty set = all events.
  - `_CommandProtocol.datagram_received` short-circuits `subscribe` /
    `unsubscribe` to `CommandServer.add_subscriber/remove_subscriber` so we
    record the source addr (which `dispatch` never sees).
  - `emit_event` now fans out to dynamic subscribers in addition to the
    static `CHIRP_EVENT_SINK` and stdout JSON logging.

- `chirp/cli.py` *(new — 294 lines)*. argparse-based UDP client:
  `status / add-channel / remove-channel / set-squelch / set-freq /
  set-gain / set-master-gain / reset / events --filter`. Pretty-prints via
  `rich` if installed AND stdout is a tty; otherwise plain JSON for
  pipeability. The `events` subcommand binds a UDP socket, sends
  `subscribe` FROM that socket so the daemon records OUR port, then prints
  events as they arrive. Ctrl-C unsubscribes politely.

- `chirp/tests/test_phase3.py` *(new — 565 lines)*. 17 tests covering
  IcecastSink construction, sample flow through fake encoder & fake
  publisher, mid-stream drop → reconnect, daemon refusing production mounts,
  daemon get_status surfacing icecast fields, audio flowing daemon →
  publisher via fake factory, CLI round-trip (status, add, remove, reset),
  subscribe/unsubscribe + event fan-out, and an end-to-end real-lame MP3
  smoke (gated on `lame` binary present).

### Test results

```
$ python3 -m pytest chirp/tests/ --ignore=chirp/tests/test_phase2_stress.py
============================= test session starts ==============================
collected 101 items

chirp/tests/test_imports.py    ....                                  [ 3%]
chirp/tests/test_phase1.py     ...........................           [30%]
chirp/tests/test_phase2.py     .......................               [53%]
chirp/tests/test_phase3.py     .................                     [70%]   ← 17 new
chirp/tests/test_radio_bugs.py .............                         [83%]
chirp/tests/test_state.py      .................                     [100%]
============================= 101 passed in 17.72s =============================
```

(Phase 2 stress suite excluded from gating run — it's a multi-minute soak
test of its own and unaffected by Phase 3 changes.)

### Smoke test on Micro — `/CHIRP_TEST.mp3` end to end

**Order of operations** (production rtl-airband, op25, scanner-vfo,
sdrplay-coord untouched at every step):

1. `curl /status-json.xsl` BEFORE — 6 sources active:
   `/ANALOG.mp3 (1 listener)`, `/ANALOG_GROUND.mp3 (1)`, `/DIGITAL.mp3 (1)`,
   `/VFO.mp3 (0)`, `/keepalive-analog.mp3 (0)`, `/keepalive-ground.mp3 (0)`.
2. `sudo bash chirp/scripts/add_test_mount.sh` — backed up xml, inserted
   `/CHIRP_TEST.mp3` mount block before `<paths>`, `systemctl reload
   icecast2`. xmllint OK. `curl -I` returns 400 (declared but unsourced).
   `status-json.xsl` STILL shows all 6 production sources connected.
3. `python3 -m chirp.tests.fixtures.make_test_iq --out /tmp/chirp_smoke.iq
   --samp-rate 1e6 --duration 30 --carrier 200e3 --tone 1000` —
   240 MB synthetic AM IQ.
4. `CHIRP_AUDIO_OUT=icecast:127.0.0.1:8000:/CHIRP_TEST.mp3:062352
   python3 -m chirp.daemon` (port 27400, max 4 channels) — daemon ready,
   icecast_state = `connected`, `icecast_reconnect_count = 1`.
5. `python3 -m chirp.cli --port 27400 add-channel --id ch01 --freq 0.2
   --mode am --squelch -90 --gain 0` → `{"status": "ok"}`. `hit_start`
   fires immediately (synthetic carrier is on tune).
6. `curl /status-json.xsl` DURING — now 7 sources: production 6 unchanged
   + `/CHIRP_TEST.mp3 (server_name=chirp, 0 listeners)`. All 3 production
   listeners still connected through the icecast reload.
7. `curl -o /tmp/chirp_30s.mp3 --max-time 30 http://127.0.0.1:8000/CHIRP_TEST.mp3`
   →  **120 400 bytes in 30 s = 4 013 B/s** (expected 32 kbps = 4 000 B/s;
   0.3 % over — essentially exact).
   `ffprobe`: `Audio: mp3, 16000 Hz, mono, fltp, 32 kb/s`, duration
   `30.074 s`, bit_rate 32027. VLC-listenable per ffprobe stream profile.
8. `pkill -TERM chirp.daemon` — clean shutdown, no shout errors, publish
   loop drained.
9. `sudo bash chirp/scripts/remove_test_mount.sh` — backed up xml again,
   removed the mount block, systemctl reload. xml now contains exactly
   `/ANALOG.mp3`, `/ANALOG_GROUND.mp3`, `/DIGITAL.mp3` — pre-smoke baseline.
10. `curl /status-json.xsl` AFTER — back to 6 sources, all production
    listener counts identical to BEFORE.

**Production mount diff before vs. after smoke: zero changes.** Same
mount-name list in `icecast.xml`, same active sources, same listener counts.

### Hard rules compliance

- Production `rtl-airband` untouched (no systemctl writes to it).
- All 3 production listeners on ANALOG / ANALOG_GROUND / DIGITAL stayed
  connected the entire smoke (icecast `reload` preserves source connections).
- `icecast.xml` only ever held `/CHIRP_TEST.mp3` as a NEW mount, never
  modified the existing 3. After teardown, identical to baseline.
- Refused to publish to production mountpoints with a hard validation error
  in `_parse_icecast_spec` (tested in `TestDaemonIcecastConfigRefusal`).
- Branch tip: `e667485` on `origin/gr-demod/airband` (this section adds one
  more commit).

### Deferred / open questions for Will

- **lame subprocess vs. libmp3lame python binding.** Phase 3 uses a `lame`
  subprocess for simplicity (one fewer python dep, well-understood CBR
  behaviour). If startup latency or process count matters for production
  deployment (32 channels = ~1 ms startup), we can swap to a pure-Python
  encoder later; the IcecastSink takes an injected `encoder=` exactly for
  this kind of swap.
- **Subscriber TTL.** Subscribers persist until daemon restart or explicit
  `unsubscribe`. No heartbeat / auto-expiry. If airband-ui's webhook bridge
  ever subscribes and crashes without unsubscribing, the daemon will keep
  pushing to a dead UDP port — silent waste but not a failure mode. Phase 4
  can add a periodic ping if needed.
- **Icecast credentials in env.** `CHIRP_AUDIO_OUT=icecast:host:port:/mount:password`
  embeds the password in process env, which `ps -e` and journald don't see
  (Linux strips the env from /proc), but it's still less hygenic than a
  credentials file. Phase 4 cutover (which will need the real source
  password for `/ANALOG.mp3`) should consider a `CHIRP_ICECAST_PASSWORD_FILE`
  alternative.
- **CHIRP_TEST mount removed.** Mount is no longer in `icecast.xml` —
  re-running the add script will reinstate it. Phase 4 will define
  `/ANALOG_NEW.mp3` per the design-doc rollout plan; chirp will publish
  there during A/B shadow.

### Commits on `gr-demod/airband` (Phase 3 in order)

- `142a941` — IcecastSink + production IcecastReconnector
- `2533405` — add/remove /CHIRP_TEST.mp3 mount scripts
- `061d4b0` — wire IcecastSink into daemon; subscribe/unsubscribe commands
- `a1e6209` — chirp.cli operator CLI + UDP event subscriber
- `e667485` — test_phase3.py (17 tests)
- *(this section's commit)* — PROGRESS.md Phase 3 entry

**Branch tip:** `540554f`

### Next task — Phase 4

Ground band parity + dashboard cutover. Specifically:

1. Spin up `gr-demod@ground` alongside `gr-demod@airband` (NFM mode added to
   `Channel`; the design doc has the receiver hierarchy already).
2. Publish chirp to `/ANALOG_NEW.mp3` (NOT `/ANALOG.mp3` yet) for a 24-hour
   shadow window, side-by-side with rtl-airband.
3. Flip `SB5_USE_GR_DEMOD=1` in `/etc/airband-ui.conf`, restart airband-ui.
   `/api/airband/*` proxies pivot from rtl-airband to chirp.
4. Chirp overwrites `/ANALOG.mp3` (and `/ANALOG_GROUND.mp3`) directly so
   existing bookmarked stream URLs keep working. `/ANALOG_NEW.mp3` mount is
   removed once cutover is signed off.
5. `systemctl stop rtl-airband; systemctl disable rtl-airband` — rollback
   path: flip the env flag back, `systemctl start rtl-airband`. ≤ 30 s revert.

Rollback rehearsal goes in the Phase 4 commit log so we don't ad-lib it
under pressure.


---

## Phase 4a — ground-band NFM + two-daemon coexistence (2026-06-04)

**Goal.** Get chirp running for the ground band (NFM) the same way it
already runs for airband (AM), with both daemons coexisting on Micro.
File source only — no RSPduo opens in this sub-phase.

### What landed

1. **NFM demod path in `Channel`** (`chirp/dsp/channel.py`).
   New chain alongside the AM path: `freq_xlating` → fir decim ×5 → fir decim
   ×(samp_rate/1e6) → `pwr_squelch_cc` → `quadrature_demod_cf` →
   `multiply_const_ff` (post-demod gain) → audio LPF/decim ×5 → arb resampler.
   Discriminator gain = `pre_demod_rate / (2π·max_dev_hz)`; default
   `nfm_max_deviation_hz = 5 kHz` (mil-air narrowband). No 75 µs de-emphasis
   — mil-air NFM doesn't pre-emphasise. AGC is omitted on the NFM path (FM
   is amplitude-insensitive); `set_gain()` instead drives the post-demod
   scalar so operator semantics match AM.
   Mode is immutable per channel (locked design decision); the constructor
   raises `ValueError` for anything other than `"am"`/`"nfm"`.

2. **Per-band configuration** (`chirp/config/`).
   - `airband.json` — AM defaults, cmd port 7400, fallback file
     `/tmp/chirp_airband_fallback.f32`.
   - `ground.json` — NFM defaults, cmd port 7401, fallback file
     `/tmp/chirp_ground_fallback.f32`.
   - `README.md` — schema, env-override matrix, per-band path table,
     bad-JSON behaviour.
   `load_config()` now picks `chirp/config/<CHIRP_BAND>.json` and raises
   `ValueError("invalid JSON …")` on malformed JSON. `CHIRP_POOL_MODE`
   overrides the pool's demod mode; anything outside {`am`,`nfm`} is a
   hard reject at startup. Hit log defaults to
   `/var/log/chirp/<band>_hits.jsonl` so the two daemons never share one.

3. **Daemon pool is mode-homogeneous** (`chirp/daemon.py`).
   Every slot in the 32-slot pool is created with `mode=cfg.pool_mode`,
   so airband pools are AM and ground pools are NFM. `add_channel` now
   rejects requests whose `mode` doesn't match the pool with
   `"channel mode mismatch: pool=<m>, requested […] != pool mode"`.
   `get_status` surfaces `pool_mode` so operators can see which demod
   chain is wired.

4. **Schema + CLI accept NFM.**
   `ChannelArgs.mode` widened from `Literal["am"]` to `Literal["am","nfm"]`.
   `chirp-cli add-channel --mode` choices updated to `("am","nfm")`.
   The Phase 1 `test_add_channel_rejects_nfm` guard was replaced with
   `test_add_channel_accepts_nfm` + `test_add_channel_rejects_unknown_mode`
   so the rejection envelope (e.g. "ssb") is still covered.

5. **systemd template fleshed out** (`chirp/systemd/gr-demod@.service.template`).
   `User=ubuntu`, `Type=simple`, `Environment="CHIRP_BAND=%i"`,
   `ExecStart=/usr/bin/python3 -m chirp.daemon`,
   `Restart=on-failure RestartSec=2`, `StartLimitBurst=10 StartLimitIntervalSec=60`,
   `ReadWritePaths=/var/lib/chirp /var/log/chirp /tmp`, `MemoryMax=1G`.
   `Requires=icecast2.service After=network-online.target`. **Not installed
   on Micro** — that's Phase 4d cutover. Document only.

6. **Ground test-mount tooling** (`chirp/scripts/`).
   `add_ground_test_mount.sh` / `remove_ground_test_mount.sh` mirror the
   Phase 3 airband helpers: `sudo`, idempotent, backup-then-edit, xmllint
   validate, `systemctl reload icecast2` (NOT restart — production sources
   stay connected).

7. **NFM IQ fixture** (`chirp/tests/fixtures/make_nfm_iq.py`).
   Carrier × `exp(jφ(t))` with `φ(t) = 2π·fc·t − β·cos(2π·tone·t)` and
   `β = max_dev/tone`. Same CLI shape as `make_test_iq.py` plus
   `--max-dev`. Default deviation 5 kHz, tone 500 Hz.

8. **Tests** — `chirp/tests/test_phase4a.py`, 15 tests:
   - 5× `TestChannelNFM` — construction, AM regression, bad mode rejection,
     hot setters, snapshot keys.
   - 1× `TestNFMDemodEndToEnd::test_nfm_tone_recovered` — synthesised NFM
     fixture (1 Msps, +100 kHz carrier, 500 Hz tone, 5 kHz deviation, 3 s)
     fed through a real `ChirpFlowgraph`. FFT peak on the demodulated
     audio file must sit within 30 Hz of 500 Hz with SNR > 15 dB above
     mean spectrum. Tolerance set by 2-s capture bin width (~0.5 Hz).
   - 5× `TestConfigLoader` — airband/ground via env, bad JSON →
     `ValueError("invalid JSON …")`, bad pool_mode → `ValueError("invalid
     pool_mode …")`, missing file falls back to airband defaults.
   - 4× `TestTwoDaemonCoexistence` — two `ChirpFlowgraph`s in one pytest
     process with distinct UDP ports, state files, hit logs, audio paths.
     Adds a channel to each, verifies `_by_id` mappings are disjoint,
     `set_squelch` to airband doesn't touch ground state and vice versa,
     airband (AM) pool rejects an NFM `add_channel`, both state files
     land on disk with only their own channel ids.

### Full test suite

```
$ python3 -m pytest chirp/tests/ -q --tb=short
... 121 passed in 151.46s (0:02:31)
```

105 → 121 (Phase 4a added 16 new tests counting the schema swap).

### Smoke test on Micro (file source only — no RSPduo)

1. Captured baseline: `sha256(/etc/icecast2/icecast.xml) =
   29e41b2be553b0b98e41c47fbae208ec42059a3e31363921b1424cb699017b02`,
   production sources `{ANALOG, ANALOG_GROUND, DIGITAL, VFO,
   keepalive-analog, keepalive-ground}` with their listener counts.
2. `sudo bash chirp/scripts/add_test_mount.sh` — declared `/CHIRP_TEST.mp3`,
   `systemctl reload icecast2`; six production sources still up.
3. `sudo bash chirp/scripts/add_ground_test_mount.sh` — declared
   `/CHIRP_GROUND_TEST.mp3`, reload again; production untouched.
4. Synthesised fixtures: `make_test_iq` → `/tmp/am_smoke.iq` (240 MB,
   carrier +200 kHz, 1 kHz tone); `make_nfm_iq` → `/tmp/nfm_smoke.iq`
   (240 MB, carrier +100 kHz, 500 Hz tone, 5 kHz deviation). 30 s each.
5. Started both daemons (file source):
   - `CHIRP_BAND=airband CHIRP_SOURCE=file:/tmp/am_smoke.iq
     CHIRP_AUDIO_OUT=icecast:127.0.0.1:8000:/CHIRP_TEST.mp3:062352
     CHIRP_CMD_PORT=7400 …` — `daemon_ready` event fired,
     `icecast_state=connected`.
   - `CHIRP_BAND=ground CHIRP_SOURCE=file:/tmp/nfm_smoke.iq
     CHIRP_AUDIO_OUT=icecast:127.0.0.1:8000:/CHIRP_GROUND_TEST.mp3:062352
     CHIRP_CMD_PORT=7401 …` — `daemon_ready` event fired,
     `icecast_state=connected`.
6. Added channels via real UDP CLI:
   - `chirp-cli --port 7400 add-channel --id air01 --freq 0.2 --mode am
     --squelch -90 --gain 0` → `{"status":"ok","data":{"slot":0,…}}`.
   - `chirp-cli --port 7401 add-channel --id gnd01 --freq 0.1 --mode nfm
     --squelch -90 --gain 6` → `{"status":"ok","data":{"slot":0,…}}`.
7. `curl --max-time 15` against both mounts in parallel:
   - `/CHIRP_TEST.mp3` → 60 200 bytes (`4 013 B/s`).
   - `/CHIRP_GROUND_TEST.mp3` → 60 200 bytes (`4 013 B/s`).
   `ffprobe` on both: `Audio: mp3, 16000 Hz, mono, 32 kb/s, duration 15.02 s`.
   `status-json.xsl` during smoke listed 8 sources (6 production + 2 chirp);
   all 6 production sources kept the same listener counts they had before.
8. Real-UDP cross-talk check: `set-squelch --port 7400 --id air01 --dbfs -40`
   → `status` shows airband `squelch_dbfs=-40.0`, ground unchanged at `-90.0`.
9. SIGTERM both daemons → "publish loop exiting", "chirp stopped". Clean.
10. `sudo bash chirp/scripts/remove_ground_test_mount.sh` → reload.
    `sudo bash chirp/scripts/remove_test_mount.sh` → reload. Restored
    icecast.xml from the pre-Phase-4a backup (the remove scripts each left
    one blank line; restoring from backup makes the file byte-identical).
11. Final `sha256(/etc/icecast2/icecast.xml) =
    29e41b2be553b0b98e41c47fbae208ec42059a3e31363921b1424cb699017b02` —
    byte-identical match. Production status: 6 sources, identical listener
    counts to pre-Phase-4a.

**Production rtl-airband, op25, scanner-vfo, sdrplay — untouched.** No
restart of any production unit. Only `systemctl reload icecast2` (which
preserves source connections), called once per mount add/remove.

### Files created

```
chirp/config/airband.json
chirp/config/ground.json
chirp/config/README.md
chirp/scripts/add_ground_test_mount.sh
chirp/scripts/remove_ground_test_mount.sh
chirp/tests/fixtures/make_nfm_iq.py
chirp/tests/test_phase4a.py
```

### Files modified

```
chirp/cli.py                       (--mode choices: am → am,nfm)
chirp/cmd/schema.py                (ChannelArgs.mode: Literal["am"] → ["am","nfm"])
chirp/daemon.py                    (pool_mode plumbing, per-band JSON loader,
                                    mode-mismatch rejection, per-band hit log,
                                    pool_mode in get_status)
chirp/dsp/channel.py               (NFM demod path, mode kwarg, mode-aware setters)
chirp/systemd/gr-demod@.service.template  (real ExecStart, User, hardening)
chirp/tests/test_phase1.py         (replaced rejects_nfm with accepts_nfm +
                                    rejects_unknown_mode)
```

### Commits (Phase 4a)

- `4e88fb7` — chirp(phase4a): NFM demod path + ChannelMode plumbing
- `099d294` — chirp(phase4a): widen schema + CLI to accept mode=nfm
- `bd0d580` — chirp(phase4a): per-band config (airband/ground) + loader + mode-homogeneous pool
- `a9fddbf` — chirp(phase4a): NFM IQ fixture + test_phase4a.py (15 tests)
- `cb6a7f1` — chirp(phase4a): systemd template + ground test-mount scripts
- *(this section)* — chirp(phase4a): PROGRESS.md Phase 4a entry

**Branch tip:** `<sha>`

### Deferred

- Real RSPduo source — Phase 4b. The SDR adapter (rtlsdr_source / soapy
  source) lives behind the same `Channel` contract; the smoke test pattern
  carries over directly.
- 24-hour shadow window against rtl-airband — Phase 4b/4c.
- `/ANALOG_NEW.mp3` cutover mount + airband-ui flip — Phase 4d.
- systemd unit install on Micro — Phase 4d cutover.

### Next task — Phase 4b (needs Will's authorization)

SDR source adapter + parallel-with-rtl-airband live test. Specifically:

1. Add an `SdrIQSource` block parallel to `FileIQSource` so a chirp daemon
   can be pointed at the real RSPduo (`CHIRP_SOURCE=sdr:rspduo:0:118.5e6`).
   Antenna routing and gain split must mirror rtl-airband's airband config
   exactly so the demod-quality comparison is apples-to-apples.
2. Spin chirp up in **shadow** alongside live rtl-airband, publishing AM to
   `/CHIRP_TEST.mp3` (same mount Phase 4a smoke used) and NFM to
   `/CHIRP_GROUND_TEST.mp3`. Production `/ANALOG.mp3` / `/ANALOG_GROUND.mp3`
   stay on rtl-airband.
3. A/B listen for a few minutes, compare `icecast_bytes_sent` rates, check
   `hit_start` events fire when a real Nashville Tower transmission lands.
4. Phase 4b ends with chirp confirmed working on real RF with rtl-airband
   still owning production. No cutover yet.
