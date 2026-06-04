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


## Phase 4c — dashboard integration behind a feature flag

The Phase 4c task wires the existing airband-ui dashboard endpoints to
talk to chirp daemons via the UDP JSON command bus when a single
feature flag is on.  Default behavior is unchanged — production keeps
using the rtl-airband path until the flag is flipped.

### The feature flag

```
SB5_USE_GR_DEMOD=true   # chirp path
SB5_USE_GR_DEMOD=false  # rtl-airband path (default)
```

When `true`, four operator endpoints proxy to chirp daemons (airband on
`127.0.0.1:7400`, ground on `127.0.0.1:7401`) instead of writing
`rtl_airband.conf` + invoking `safe_restart_rtl_airband`:

| Endpoint                              | rtl-airband path                                              | chirp path                                                  |
| ---                                   | ---                                                            | ---                                                          |
| `POST /api/airband/squelch_preset`    | compute thresholds + write `rtl_airband.conf` + pending_restart | read noise floor from `chirp.get_status`, push `set_squelch` per channel (no restart) |
| `POST /api/airband/squelch_auto`      | persist flag; tracker writes/restarts                          | persist flag; tracker pushes `set_squelch` via chirp         |
| `POST /api/hp/state/activate`         | save HPState; awaits operator restart                          | save HPState + `reset` chirp + batch `add_channel` (sub-second) |
| `POST /api/sitrep/action reset_radios` | `safe_restart_rtl_airband` (5–15 s; SDRplay handle recovery)   | `reset` both chirp daemons (sub-second; no SDR teardown)     |

### Flipping the flag for testing

The flag is read at airband-ui startup (and on every endpoint call —
the module re-probes per-cycle, no daemon reload needed for the value
to take effect).  To flip:

```
# Inline test:
sudo systemctl edit airband-ui.service
# Add under [Service]:
#   Environment=SB5_USE_GR_DEMOD=true
sudo systemctl restart airband-ui.service

# Quick test from a shell:
SB5_USE_GR_DEMOD=true python3 -m ui.app
```

### Rollback procedure (≤30 s)

The chirp path is purely additive — production rtl-airband config is
NOT touched while the flag is on (the chirp daemons run alongside, the
tracker just stops writing to `rtl_airband_*_<band>.conf`).

To revert:

```
sudo systemctl edit airband-ui.service     # remove the SB5_USE_GR_DEMOD line
sudo systemctl restart airband-ui.service  # back to rtl-airband path
```

Total downtime: airband-ui restart only (≈3 s).  rtl-airband, op25,
icecast, and Discovery are NOT touched by the flag flip.

### State migration runbook

When you're ready to start chirp daemons (Phase 4d), use the
one-shot migration script to pre-populate the daemon state files
with the operator's active favorite + current preset thresholds:

```
# 1) Dry-run first to see what would change (default mode).
python3 chirp/scripts/migrate_state.py

# Example output:
#   chirp/migrate_state — mode=DRY-RUN
#     hp_state     : /home/ubuntu/scannerproject/data/hp_state.json
#     controls     : /home/ubuntu/scannerproject/profiles/managed_analog_controls.json
#     state dir    : /var/lib/chirp
#     bands        : airband, ground
#
#   [airband]
#     active favorite : SIC
#     channels (planned): 20, preset=balanced
#     target path     : /var/lib/chirp/airband.state.json
#     existing match  : False
#     NEW FILE — 20 channels, preset=balanced
#   ...

# 2) When the dry-run looks right, --apply to mutate disk.
sudo python3 chirp/scripts/migrate_state.py --apply

# Idempotent: a second --apply against unchanged inputs is a no-op.
```

The script is **safe to run repeatedly**.  Inputs:

- `data/hp_state.json` — favorites + per-band activation flags.
- `profiles/managed_analog_controls.json` — per-band preset + threshold metadata.

Outputs (`/var/lib/chirp/`):

- `airband.state.json` — chirp.state.ChirpState shape, mode=am.
- `ground.state.json` — chirp.state.ChirpState shape, mode=nfm.

Both written atomically (tmp file in same dir → fsync → rename).

### Audit log

Every chirp call from airband-ui appends one JSON line to
`~/.cache/airband-ui/chirp_client.jsonl` (path overridable via
`CHIRP_CLIENT_LOG_PATH`).  Rotates at 1 MiB.  Useful for operator
forensics when a channel goes quiet — the log captures req_id, args,
status, error, elapsed_ms.

### What does NOT happen until cutover

This phase is dashboard-side only.  The chirp daemons are NOT started
by anything in Phase 4c.  Phase 4d (cutover) starts the daemons, runs
the state migration, and flips the flag.

### Tests

A representative slice (run on `gr-demod/airband` branch):

```
python3 -m pytest chirp/tests/ ui/tests/ -m 'not slow' -q
# 209 passed, 4 deselected
```
