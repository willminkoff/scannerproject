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
