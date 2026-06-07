# Scan-Hold for the chirp LO scheduler

**Status:** design — no implementation · **Date:** 2026-06-05 · **Owner:** chirp analog

## 1. Problem

`LoScheduler` rotates the SDR local oscillator between frequency clusters on a
fixed `lo_dwell_sec` (now 3s) with **no awareness of in-progress traffic**. The
hop is time-only (`chirp/dsp/lo_scheduler.py:332-338`): when
`elapsed >= dwell_s`, it advances to the next cluster, period. A transmission
that begins mid-dwell is **cut off** the instant the LO hops away — the operator
hears a fragment, then that cluster goes dark for `dwell × (n_clusters − 1)`
(~15s on a 6-cluster airband plan) before it is revisited.

A scanner user expects the opposite: the radio **stops on a live channel and
stays there until the exchange ends**, then resumes scanning. That
"hold-on-hit" latch — standard on any Uniden and on RTLSDR-Airband's scan mode —
is what chirp lacks today.

## 2. Audit of current code paths (the hooks we can leverage)

- **Dwell timing & hop decision** — `chirp/dsp/lo_scheduler.py`. `step()` runs
  every `tick_s` (~250ms) under `daemon_lock + self._lock`; the time-only hop is
  `:332-338`; the dwell-start timestamp is `:175`; `dwell_s` is `:153`; the
  `dwell_s > 0` guard is `:140`. This `step()` is the **single place** a hold
  must intercept the advance.
- **Per-channel squelch-open detection** — `chirp/hit_detector.py` polls each
  live slot's `Channel.get_squelch_open()` at 5 Hz and emits `hit_start` /
  `hit_end` on closed→open / open→closed transitions (`:4-7`). It already
  **skips parked channels** (`:149`). This is precisely the activity signal
  scan-hold needs.
- **Parking** — `chirp/dsp/channel.py` `set_parked` slams squelch shut on
  off-cluster channels, so **only the active cluster can ever report open**.
- **Callback pattern** — the scheduler is already decoupled via injected
  callbacks (`retune_to`, `park_channels`, `unpark_channels`, `emit_event`,
  `get_channels`). Scan-hold adds one read of the same shape — `is_open(id)` —
  backed by `Channel.get_squelch_open()`, evaluated only for the live cluster's
  IDs. No new coupling.

So the hooks exist: read squelch-open on the live IDs, and conditionally
suspend the `:332-338` advance.

## 3. Proposed feature

- **Trigger.** In `step()`, before the time-hop, check `is_open()` for the
  active cluster's live channels. Any open → **engage hold** (suspend rotation;
  record `hold_start_ts`, the triggering channel, `extended_n_times = 0`).
- **Release.** When **every** channel in the cluster has been continuously
  closed for `scan_hold_hang_sec` (proposed default **2s**), release and advance
  to the next cluster.
- **Cap.** `scan_hold_max_sec` (proposed default **45s**, range 30–60) bounds
  total time on one cluster so a constantly-busy cluster can't starve the rest.
  On cap, force-release and advance.
- **Concurrency — honest trade-off.** While held, every other cluster is parked
  and **deaf**. A hit on another cluster during a hold is missed — unavoidable
  with one front end (same as any single-receiver scanner). The cap bounds the
  worst-case starvation window.
- **Multiple opens within the hold.** A re-open on the held channel, **or** a
  *new* channel in the same cluster opening during the hold, **resets the hang
  timer** (extends the hold) and increments `extended_n_times`. Retransmissions
  and back-and-forth exchanges keep the hold alive.
- **Band-mute interaction.** ⚠️ Today the chirp daemon has **no mute concept** —
  band-mute is a UI/PipeWire control on the VLC sink-input *downstream* of
  icecast (confirmed: no `mute` references in `daemon.py` / `lo_scheduler.py` /
  `hit_detector.py`). So scan-hold cannot honor mute without plumbing mute state
  into the daemon. Proposed rule: a squelch-open on a **band-muted** channel
  should **not** trigger or extend a hold (latching on audio nobody hears is
  pointless); if *all* of a cluster's channels are muted, never hold it. This
  requires the UI to push a per-band (or per-channel) "muted" flag to the daemon
  — see Open Questions.
- **Phase 5 (proportional dwell) interaction.** Phase 5 (`PROGRESS.md:2724`,
  "Phase 5 will add proportional dwell") weights each cluster's **long-run**
  dwell by recent activity. Scan-hold is an **instantaneous** latch on the
  **current** hit. They **compose**: proportional dwell sets the base scan rate
  per cluster (long-run fairness); scan-hold transiently overrides it while a TX
  is live. Build scan-hold first; Phase 5 later only tunes the rate it falls
  back to between holds. No conflict — different time horizons.
- **Telemetry.** New events via `emit_event`: `scan_hold_engaged`
  (`cluster_idx`, trigger `channel_id`, `level_dbfs`) and `scan_hold_released`
  with `held_for_sec`, `extended_n_times`, `released_reason: hang | max_cap`.
  Logged to the hit JSONL alongside `cluster_hop`, and surfaced in
  `get_status().lo_scheduler` (`state`, `held_channel_id`, `hold_elapsed_sec`).

## 4. Open questions for Will

1. `scan_hold_hang_sec` default — **2s** ok?
2. `scan_hold_max_sec` default — **30, 45, or 60s**?
3. **Band-mute:** do band-muted channels participate in holds? Worth plumbing
   mute state into the daemon, or scope "muted" to the whole-band case only
   (simpler)?
4. **Per-band toggle** — independent `scan_hold_enabled` in `airband.json` /
   `ground.json`, or one global setting?
5. **Priority logic** — if two clusters have hits at a hop-decision moment, does
   the current cluster always win (stay), or do we honor a priority-channel
   list that can preempt?

## 5. Test plan

Extend `chirp/tests/test_lo_scheduler.py` (its fake clock + injected callbacks
are the template). Unit cases: hold engages on open; holds through a closed-blip
shorter than `hang`; releases after `hang`; re-open extends + bumps
`extended_n_times`; a *new* channel in the cluster extends; `max_cap`
force-release and advance; parked / other clusters never trigger a hold;
band-mute bypass (once the flag is plumbed). Runtime on Micro after deploy:
watch `scan_hold_engaged` / `scan_hold_released` in the `gr-demod@*` journals,
confirm `held_for_sec` tracks real TX lengths, and that normal `cluster_hop`
cadence resumes promptly after each release.

## 6. Sequencing (no big-bang)

1. **Design sign-off** — this doc + answers to §4.
2. **Implement + unit tests** behind a default-**OFF** `scan_hold_enabled`.
3. **Live shadow** — a log-only mode that emits `scan_hold_engaged/released`
   events describing what it *would* have held, **without** actually suspending
   rotation. Run a session on Micro and compare against real traffic / the
   Uniden before changing behavior.
4. **Cutover** — flip `scan_hold_enabled` on per band, restart in **MA→SL order**
   (master first; if the MA master must re-acquire, stop the SL slave first —
   per the 2026-06-05 RSPduo/sdrplay wedge lesson), and watch telemetry.
