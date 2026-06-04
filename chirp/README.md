# chirp

`chirp` is the GNU Radio + Python analog demodulator that replaces rtl-airband
in SB5. It runs as a long-running daemon per band (airband, ground) and exposes
a JSON command bus (UDP, loopback) so the dashboard can retune channels, change
squelch, and add/remove scan slots **without restarting the SDR** — fixing the
SIGKILL → SDRplay-wedge cycle that gates the rtl-airband stack today.

## Why

rtl-airband has no hot config reload. Every operator action triggers a process
restart and a 5–15 s wedge on the SDRplay shared-memory semaphores. Working
around it has been whack-a-mole. The fix is structural: put the analog demod
on the same long-running flowgraph + JSON command bus pattern op25 already
uses successfully. Full rationale, alternatives considered, and architecture
contract are in `SDR_DEMOD_DESIGN_2026-06-03.md` at repo root.

## Project structure

```
chirp/
  README.md                       # this file
  PROGRESS.md                     # nightly overnight log — read top-to-bottom
  __init__.py
  dsp/
    __init__.py
    ham2mon/                      # vendored from madengr/ham2mon @ db9834c (GPL)
      README.md                   # port notes + license
      __init__.py
      receiver.py                 # TunerDemodAM / TunerDemodNBFM hier_blocks (GR 3.10)
      scanner.py                  # control loop (some Phase 1 work deferred)
      LICENSE                     # ham2mon's GPL
  cmd/
    __init__.py
    schema.py                     # UDP JSON command validators (Phase 1 placeholder)
    server.py                     # UDP listener / dispatcher (Phase 1 placeholder)
  config/
    defaults.json                 # default config (Phase 1 starter)
  systemd/
    gr-demod@.service.template    # template — installed in Phase 1/4
  tests/
    __init__.py
    test_imports.py               # sanity: every chirp submodule imports
    fixtures/
      README.md                   # rtl-airband regression fixtures (Phase 2 work)
```

## How to read PROGRESS.md

`chirp/PROGRESS.md` is the overnight log. Each entry is one task by one agent,
in chronological order top-to-bottom. The newest task is at the top of the most
recent entry. The format for each entry is:

- **Goal** — what the task set out to do
- **Done** — bullet list of accomplished items
- **Commits** — SHAs and titles
- **Branch tip** — head SHA after the task
- **Deferred / surfaces for Will** — anything Will should look at on review
- **Next task** — what the next overnight slot picks up

If a task hit a blocker and stopped, that's logged the same way with an explicit
**Blocker** section instead of Done.

## Related docs (repo root)

- [`SDR_DEMOD_DESIGN_2026-06-03.md`](../SDR_DEMOD_DESIGN_2026-06-03.md) — architecture + wire protocol + cutover plan
- [`SDR_DEMOD_PROJECT_PLAN.md`](../SDR_DEMOD_PROJECT_PLAN.md) — phases, branch + naming, acceptance criteria
- [`SDR_DEMOD_DECISION_2026-06-03.md`](../SDR_DEMOD_DECISION_2026-06-03.md) — decision brief: why GR-based replacement vs. alternatives
- [`SB5_Phase0_Spike_Report.md`](../SB5_Phase0_Spike_Report.md) — Phase 0 spike: all 3 critical assumptions validated
- [`SB5_AUDIT_2026-06-03.md`](../SB5_AUDIT_2026-06-03.md) — current-state SB5 audit (the rtl-airband pain catalogued)

## License

Chirp is **GPL** (inherited from the vendored ham2mon DSP code under
`chirp/dsp/ham2mon/`). See `chirp/dsp/ham2mon/LICENSE` for the full text.

This is a change of license stance for the SB5 repo as a whole — flagged in the
2026-06-03 PROGRESS.md entry for Will's awareness. If GPL inheritance is a
problem (e.g. proprietary distribution intent), the alternative is to rewrite
the ham2mon hier_blocks from the GR docs / 3.10 examples; that's a Phase 1/2
decision, not a Phase 0 blocker.

## Status

Pre-Phase-1. Foundation scaffold + ham2mon port land on branch `gr-demod/airband`.
Phase 1 (one-channel AM demod prototype + UDP JSON command bus) is the next
overnight task. Production rtl-airband is **untouched** by this branch and stays
untouched until Phase 4 cutover.
