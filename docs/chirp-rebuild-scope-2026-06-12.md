# Chirp data path rebuild — stateless-by-default

**Author:** Will Minkoff (drafted with Claude)
**Date:** 2026-06-12 (revised after the input_gate post-mortem + multi-agent code review)
**Status:** Draft for approval

## What changed since the first draft

Today shipped a CPU optimization (`input_gate` — a `blocks.copy` toggled off when a channel parked) that traded 800% CPU for ~3.7 kbps icecast mounts. Internal diagnostics all said "healthy"; the actual audio was starved because every parked channel stopped feeding the downstream `blocks.add_ff` mixer, which is a sync block. The same multi-agent code review surfaced eight other latent defects (RSPduo collision paths, a key-collision in `get_status`, a `RestartSec`/`StartLimit` math break, a false `all_muted` health classifier). The review and the field outcome together changed three things in this scope:

1. **Phase 3 now owns CPU-while-parked correctly.** The savings must come from a topology change (selector or valve + flowgraph lock/unlock, OR a `mute_ff` placed AFTER demod at audio rate, NOT before demod at 2 Msps), not from anything that interrupts sample flow into a sync block. We codify this as a coding standard for the daemon, not just a phase deliverable.

2. **Every phase now has an end-to-end audio gate.** "Looks healthy in journalctl" is not a verification step. A phase is not done until the icecast mount byte rate is measured at >= configured bitrate × 0.8 across both bands for at least one minute under realistic channel load.

3. **A Phase 0 — anti-pattern audit — is added** before any rebuild work, so the same class of bug doesn't ship again under a different name.

## North Star

SB5 should be a software platform where stuff doesn't break — not where we can recover quickly when it breaks. The chirp daemon currently has architectural states where it can be alive but useless ("airband not hitting until I rebooted Micro"). The objective of this rebuild is to eliminate those states by construction.

The daemon must, after this work, satisfy one invariant: **it is either producing real audio from real samples, or it has stopped cleanly with a structured diagnostic. There is no third state.**

## Scope

**In.** Boot contract, source block, LO scheduler, AGC + squelch + priority gate, hit detector, config loading, device ownership. Both bands (airband + ground). End-to-end through the icecast mount.

**Out.** OP25 (different daemon, separate scope if it earns one). RTL-airband legacy code paths. Hardware reliability (UPS, USB hubs — those are bought, not built). Audio character tuning beyond "no regression on Will's ear."

## Seven phases — ~3-4 weeks of focused work

Each phase ships independently behind a feature flag so we can revert if it regresses. Each has its own tests + verification step on the live box before the next phase begins. **And** an end-to-end mp3 mount byte-rate gate (>= configured bitrate × 0.8 for at least one minute under realistic channel load) before the phase is declared complete.

### Phase 0 — Anti-pattern audit (~2 days)

Before writing new code, catalog the bug classes today's review surfaced so the rebuild doesn't ship them under new names.

- **Sync-block starvation.** Every `add_ff`/`multiply_ff` and any other GR sync block in the chirp graph: what is its starvation contract? Which upstream blocks could ever stop emitting? The `input_gate` failure was exactly this — `blocks.copy` with `set_enabled(False)` starved the `blocks.add_ff` mixer. Document the contract per node so this can't happen again.
- **Two-walk drift in diagnostics.** The false `all_muted` health classifier came from counting `muted_count` over a different lock-free walk of `self._slots` than `live_count`/`open_count`. Sweep every diagnostic for the same shape.
- **Key collisions in serialized snapshots.** `get_status` had two contributors both writing `audio_path` — one a string path, one a dict — and the second silently clobbered the first. Audit every serialization point for collisions; document the schema; add a test that fails on collision.
- **systemd interaction math.** Any change to `RestartSec`/`StartLimitBurst`/`StartLimitIntervalSec` requires a comment proving the bound is reachable (`burst × (RestartSec + min_start_time) < interval`). Today's `RestartSec=15` silently disarmed the start-limit circuit breaker. CI should fail if any of the three is changed without the arithmetic comment.
- **Discovery-path exclusion gaps.** The chirp/rtl-airband serial exclusion was applied on the sysfs path but not on the SoapySDR enumerate fallback. Sweep every "discover X from environment" with a "exclude Y" and verify the exclusion applies on all branches.
- **Schema disagreements across reading sites.** `avoid_site_ids` was a list to op25_adapter and a list-or-string to favorites_runtime; only the list form worked in the hard filter. Sweep for other sidecar keys with two parsing implementations.

Output: a list of catalogued instances (file:line) that the rest of the rebuild either fixes in-flight or explicitly defers. No new daemon code. Two days of reading.

Risk: very low. Read-only. Pays for itself by catching the next "clever idea" before it ships.

### Phase 1 — Source contract validation (~3 days)

After `sdrplay_api_Open`, run a 200 ms sample-quality probe before the main demod loop is allowed to start.

Measured: noise floor, variance, DC offset, saturation rate, sample-rate-vs-arrival-rate. The envelope is "what an RSPduo with an antenna connected normally looks like." Anything outside the envelope → abort with structured diagnostic in journalctl.

Behind `CHIRP_SOURCE_VALIDATE=1` initially. Will flip on after we calibrate the envelope.

Outcome: catches the largest class of "alive but useless" failures — sdrplay handing us junk after a partial wedge. Today's reboot most plausibly worked because of this exact symptom; we'll know for sure after Phase 1 ships.

Risk: low. Affects boot path only.

### Phase 2 — Sample-clock LO scheduler (~4–5 days)

Today's LO scheduler is a wall-clock + Python-thread timer. That is exactly the kind of stateful thing that drifts and gets stuck.

Replace with **GNU Radio stream tags + sample-clock dwell**:
- Dwell is `N samples`, not `N seconds`. The RSPduo produces samples at a known rate; GR's scheduler runs at sample rate.
- The hop trigger is a stream tag emitted at sample N. There is no "missed event" because the trigger is built into the sample stream itself.
- The function is pure: `desired_cluster(sample_count, channel_list, dwell_samples) -> cluster_index`. Compare desired vs actual; hop if different.
- Drift is architecturally impossible: the clock is the data.

References worth reading before coding: GR `stream_tag` docs, OP25's tag-based retune patterns, SDRangel's scanning module.

Validation: shadow mode first — new scheduler runs alongside old, decisions compared, divergence alerts emitted. Cut over after 24h of clean shadow.

Property-based tests with `hypothesis`: fuzz channel permutations, cluster boundary cases, sample-rate variations. Catches the edge bugs we wouldn't write a unit test for.

Risk: medium. Scheduler is central to the audio path. Mitigation = shadow mode.

### Phase 3 — Bounded AGC, squelch, priority gate, AND topology-correct park CPU savings (~6-7 days)

Each of these accumulates state today. AGC can latch low on a transient and stay there. Squelch reference can drift. Priority gate can mis-latch on a parked channel. And parked channels still run their full pre-demod FIR chain at 2 Msps — which is where today's 800% CPU comes from.

Each gets a windowed / periodic-re-baseline design:
- **AGC:** hard min/max gain bounds + forced re-baseline every dwell. No accumulation that can survive past one dwell window.
- **Squelch:** reference is the rolling median of the noise floor over the last K samples. No latching.
- **Priority gate:** decision is re-derived each tick from the current channel state, not latched to a "selected" id. The latch was a performance optimization that became a state-correctness bug.
- **Parked-channel CPU savings (this time done right):** the savings come from a topology change, NOT from interrupting sample flow into a sync block. Two viable patterns:
  - (a) **Selector + null_source.** A `blocks.selector` upstream of each channel routes either real samples or a `null_source` into the channel's FIR chain. Switch via flowgraph lock/unlock. Parked channels see zeros (cheap) and downstream sync blocks stay fed.
  - (b) **mute_ff at audio rate.** After the demod step, where the rate is 16 kHz instead of 2 Msps. The pre-demod FIR still burns but the downstream sync contract is trivially honored, and the savings — while smaller — are safe.
  Pattern (a) is the bigger CPU win; pattern (b) is the safer first step. Phase 3 ships (b), then (a) only if soak confirms (b) isn't enough and benchmarking shows (a) is worth the topology complexity.

Audio A/B against current — Will's ear, not my metric. Plus a hard mp3 byte-rate gate: ANALOG.mp3 and ANALOG_GROUND.mp3 must stay >= 25 kbps under realistic channel load (target is 32, we accept silence padding). This is the phase where character-of-sound could shift; needs explicit sign-off before cutover.

Risk: medium-high. Audio quality boundary + GR flowgraph topology change. Mitigation = ship pattern (b) first, gate on mount byte rate, A/B against current.

### Phase 4 — inotify config + atomic channel swap (~3 days)

Daemon watches `/var/lib/chirp/{band}.state.json` via inotify. On change: validate the new config, then swap channels in-place atomically at a scheduler boundary.

Outcome:
- Eliminates the "daemon started before favorite was written" race (one-shot stale load is gone).
- Removes the need to restart the daemon for a favorite change.
- Makes "change profile while driving" safe.

Risk: low. Additive; the daemon still works without inotify if the watcher fails.

### Phase 5 — Leaseable device ownership + atomic boot (~3 days)

**Not** an exclusive lockfile. A lease/return protocol:
- chirp owns its serial by default.
- A registered borrower (disco for extended sweep, OP25 in a future expanded mode, anything we haven't thought of yet) requests a lease via a small JSON API on a Unix socket:
  `request_lease(serial, max_hold_sec, reason) → lease_id`
- chirp pauses the source cleanly, calls `sdrplay_api_Close`, hands the lease, sets a watchdog timer at `max_hold_sec`.
- Borrower attaches, does its work, calls `return_lease(lease_id)`. chirp re-attaches and resumes.
- If the borrower doesn't return by the deadline, chirp force-reclaims and logs the violation. The lease holder is on the hook for cleanup, not chirp.
- `--no-lease` flag for sessions where you want pure chirp uptime guarantees.

This preserves the invariant ("alive iff working") while leaving the door open for disco-style spectrum sweeps to borrow the RSPduo during quiet windows, without us redesigning later.

**Atomic boot:** main loop entry is gated on every invariant passing — device validated, config valid, channels non-empty, source contract satisfied. Otherwise structured exit. Kills the "half-running" failure mode at the door.

Risk: medium. Lease protocol must be carefully designed for the cleanup edge cases. Mitigation = explicit max-hold + force-reclaim + log.

### Phase 6 — Soak + chaos (~4–5 days)

Without this, we don't know we delivered.

**7-day soak.** Box runs untouched. Instrument every silent-stuck symptom we know how to detect: cluster_hop cadence, audio_path_state cadence, icecast mount byte rate, source sample arrival rate, daemon memory RSS over time. Anything anomalous is a bug to file.

**Chaos / fault injection.** Each fault must produce a loud, structured failure → clean restart → resumed operation, not a silent stuck box:
- Block sdrplay samples mid-run (kill `sdrplay_apiService`)
- Corrupt the config file mid-write (atomic-write-violation simulation)
- Skew the system clock
- Mass-mute then unmute the favorite via the UI
- Pull RSPduo USB momentarily

This is non-negotiable. The whole point of the rebuild is "stuff doesn't break"; soak + chaos is how we know.

Risk: this is the phase that surfaces what we didn't plan for. Add up to a week if real issues emerge — that's a feature of the process, not a slip.

## Work model — choose one

1. **Feature-flag incremental on the live box.** Each phase lands behind its flag; we flip it on, watch for a day, flip on the next. Faster iteration, you keep listening through the rebuild.
2. **Parallel branch + swap.** Build all six phases on a branch against a development copy of the daemon, then swap in one window. Cleaner cutover but you lose the live box for one day at swap time.

Recommendation: option 1. The flag-based rollout matches how chirp evolved already (`CHIRP_LO_MAX_CLUSTERS`, `CHIRP_AUDIO_TRACE`, `CHIRP_PRIORITY_GATE_ENABLED`).

## Out-of-scope but flagged

- **OP25 reliability.** Separate scope. Today's `SelectDevice() failed` lesson + split-process pattern is a real lesson and worth a similar treatment for OP25, but it's a different daemon with different timing constraints.
- **UI / handlers.py liveness.** The web layer can fail silently too. Once chirp is bulletproof we'll look at the UI surface.
- **Audio sink path (PipeWire/icecast).** Backpressure starvation is a known anti-pattern. Phase 1 / Phase 6 may surface specific symptoms; if so, file as follow-up.

## What I need from Will before starting

1. Green light on the overall shape.
2. Decision on work model (recommend: feature-flag incremental).
3. Acknowledgement that Phase 6 may add a week if soak surfaces real issues.
4. Anything I'm missing or have wrong.

## Success criteria

At the end of Phase 6:

- 7 days of uptime with no manual intervention.
- Zero "alive but useless" events caught by either the new invariants or the chaos drill.
- The Micro reboot remediation is unused.
- The chirp daemons + OP25 + icecast + disco coexist without race conditions, and disco can borrow an RSPduo on lease without breaking the audio path.
- mp3 mount byte rate stays >= 25 kbps on both analog bands for 95% of the soak window (allowing for brief restarts).
- chirp daemon CPU averages < 400% on each band under normal channel load (the input_gate post-mortem proved the parked-channel cost is real and the rebuild must address it).

That's the bar for "stuff doesn't break."

## Coding standards (binding for this rebuild)

These came out of the 2026-06-12 input_gate failure + the multi-agent review. They apply to every PR in the rebuild and to anything we touch in the daemon afterward.

1. **Don't break a downstream sync-block contract.** Any new GR block that can stop emitting samples must be placed downstream of the last sync block on its path. If you don't know whether a block is sync or async, find out before you wire it.

2. **End-to-end verification, not boundary verification.** A change is not "shipped" until the icecast mount byte rate is measured. Internal diagnostics ("CPU dropped", "events fire") are necessary but never sufficient.

3. **No two-walk diagnostics without a lock.** Either hold a lock across both walks or do one walk over a single snapshot. The `all_muted` false positive came from violating this.

4. **No silent empty results.** A function that resolves an exclusion set, a configuration value, or any other "must have something" answer must log a warning when the result is empty. The chirp-exclusion silent-empty case is exactly the "useful liar" we are trying to eliminate.

5. **systemd timing changes require arithmetic.** Any edit to `RestartSec`, `StartLimitBurst`, or `StartLimitIntervalSec` must include a comment proving the bound is reachable. CI lint enforces this.

6. **One schema per sidecar key.** If a key is parsed by two functions and one accepts more shapes than the other, that's a bug. Share the normalization function or fail fast on the stricter side.

7. **Discovery-path exclusions apply on all branches.** If `_rspduo_usb_serials` filters serial X, the SoapySDR fallback must also filter serial X. Any "filter once, fall back later" pattern is a defect.
