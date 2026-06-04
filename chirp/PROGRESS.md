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
- _this commit_ — chirp(phase1): PROGRESS.md Phase 1 entry

**Branch tip:** _filled in by the next commit; current pre-PROGRESS tip is
`5075d0ed92ae3cba9fd6b170a618606615f3aa71`._

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
