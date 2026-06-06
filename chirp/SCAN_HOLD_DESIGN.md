# Scan-and-Hold for the chirp LO scheduler

**Status:** design / not yet implemented · **Author:** 2026-06-05 · **Owner:** chirp analog

## Problem

chirp's `LoScheduler` (`chirp/dsp/lo_scheduler.py`) rotates the SDR local
oscillator between frequency clusters on a **fixed timer** — currently
`lo_dwell_sec` (was 60s, now 3s). The hop decision is purely time-based
(`lo_scheduler.py:332-338`):

```
elapsed = now - self._cluster_dwell_start_ts
if elapsed >= self._dwell_s:
    hop to next cluster
```

There is **no awareness of activity**. The LO leaves a cluster the instant the
timer expires, even mid-transmission. Consequences:

- A transmission longer than `dwell` is **chopped** — heard for ~`dwell`
  seconds, then the LO moves on and won't return to that cluster for
  `dwell × (n_clusters − 1)` (~15s on airband at 3s/6 clusters).
- Short bursts that land while a cluster is *parked* are missed entirely.

The 3s dwell traded "missed entirely" for "chops long TX." Neither matches the
operator's expectation, which is **classic scanner behavior**: hear one
transmission at a time, stay on it until it ends, then resume scanning. This is
what a Uniden does and what RTLSDR-Airband calls "scan mode." chirp does not do
it because it demods *all* channels in the active cluster simultaneously and
rotates on a clock — a deliberate multichannel design, not a missing toggle.

## Goal

Make the scheduler **activity-aware**: sweep when quiet, **stop on an active
channel, hold until the TX ends (+ a short hang), then resume** — while keeping
everything chirp bought us over RTLSDR-Airband (hot config reload, no
SDR-restart wedge, the SB5 UI/command bus). Anti-starvation guards prevent a
stuck carrier from holding forever.

## Behavior — three-state loop

```
            squelch opens on a live channel
   SCANNING ───────────────────────────────► HOLDING
      ▲                                          │
      │ hang expires, no reopen        squelch closes
      │                                          ▼
      └──────────────── HANG ◄───────────────────┘
                         │  reopen → HOLDING
                         │  max_hold exceeded → forced resume (+ optional lockout)
```

- **SCANNING** — rotate clusters every `scan_dwell_sec`. All channels in the
  live cluster demodulate, listening for activity.
- **HOLDING** — on the first live-channel squelch-open, stop rotating; stay on
  this cluster until the TX ends or `max_hold_sec` is hit.
- **HANG** — when squelch closes, linger `hang_time_sec`. Reopen → back to
  HOLDING (catches the reply). Quiet through hang → SCANNING, advance to the
  next cluster.

This is exactly the scan → stop-on-active → hold → delay → resume loop of a
hardware scanner.

## Config (per-band JSON, env-overridable — same path as `lo_dwell_sec`)

| Key | Proposed default | Meaning |
|---|---|---|
| `scan_dwell_sec` | **2.0** | sweep speed when quiet (per-cluster listen window). Floor ~1.5s — see Warmup. Replaces `lo_dwell_sec`. |
| `hang_time_sec` | **2.0** | linger after TX ends before resuming (scanner "delay"). |
| `max_hold_sec` | **90.0** | anti-starvation cap; force resume even if still active. |
| `solo_on_hold` | **true** | park the cluster's other channels during a hold → strict one-at-a-time (Phase A2). |
| `hold_lockout_sec` | **8.0** | after a `max_hold` forced resume, skip that channel for this long (anti-stuck) (Phase A3). |

Env overrides: `CHIRP_SCAN_DWELL_SEC`, `CHIRP_HANG_TIME_SEC`,
`CHIRP_MAX_HOLD_SEC`, `CHIRP_SOLO_ON_HOLD`, `CHIRP_HOLD_LOCKOUT_SEC`.
`lo_dwell_sec` / `CHIRP_LO_DWELL_SEC` is retained as a deprecated alias mapping
to `scan_dwell_sec` so the current 3s deploy keeps working.

## The activity signal

`LoScheduler` is already callback-driven (`retune_to`, `park_channels`,
`unpark_channels`, `emit_event`, `get_channels`). Add **one injected read**:

```
is_open(channel_id) -> bool
```

backed by the same per-channel squelch-open state the `HitDetector` polls at
5 Hz (`hit_detector.py`; `Channel` already tracks squelch + exposes
`get_signal_level_dbfs()`). `step()` evaluates `is_open` only for the **live**
cluster's IDs — parked channels are squelch-slammed and irrelevant. Keeps the
scheduler decoupled from the DSP, consistent with the existing callback design.

## `step()` redesign (replaces `lo_scheduler.py:332-338`)

Per tick (~`tick_s`, 250ms), under the existing `daemon_lock + self._lock`:

- **SCANNING:**
  - any live `is_open` → **HOLDING** (record `held_channel`, `hold_start_ts`;
    if `solo_on_hold`, park all live IDs except `held_channel`).
  - else `elapsed ≥ scan_dwell` → hop to next cluster (current logic).
- **HOLDING:**
  - still open and `hold_elapsed < max_hold` → stay.
  - closed → **HANG** (`hang_start_ts`).
  - `hold_elapsed ≥ max_hold` → forced **HANG** + add `held_channel` to a
    time-boxed lockout set (`hold_lockout_sec`).
- **HANG:**
  - `is_open` again → **HOLDING**.
  - `hang_elapsed ≥ hang_time` → unpark the full cluster (if soloed),
    **SCANNING**, advance to next cluster.

Cluster rotation still advances `(_current_cluster_idx + 1) % len(plan)`; hold
just suspends the advance. The lockout set is consulted in SCANNING so a
just-force-released channel doesn't instantly re-grab the hold.

## True "one at a time" — `solo_on_hold`

chirp normally mixes all live cluster channels. To guarantee a single TX in the
ears: on entering HOLDING, park every live channel **except** the trigger; on
resume, unpark them. Tradeoff — while soloed, a *sibling* in the same cluster
that starts up is not heard until the next sweep. That is exactly scanner
behavior (a Uniden is deaf to all else while stopped). If two open in the same
tick, pick the strongest by `get_signal_level_dbfs()`. Gated by the toggle so we
can A/B "solo" vs "hear everything active in the cluster."

## Anti-starvation & the scan-rate floor (honest limits)

- **Stuck carrier / very long TX:** `max_hold_sec` forces a resume; the
  `hold_lockout_sec` skip prevents an immediate re-hold loop.
- **Warmup floor:** after each retune there is a ~1s settle (`warmup_s` in
  `hit_detector.py`, `daemon.py` resets `claimed_at` on unpark) where squelch is
  unreliable. So `scan_dwell` can't go far below ~1.5–2s, and detection on a
  freshly-tuned cluster lags ~1s. chirp sweeps a cluster every ~2s vs a Uniden's
  ~100ms because each "channel" here is a **2 MHz LO retune**, not a synthesizer
  hop. **Open validation:** can HOLD be triggered off raw squelch *during*
  warmup (faster) while still suppressing only the warmup *hit-report* flag? If
  yes, `scan_dwell` can drop. Decouple "hold-trigger" (always live) from
  "hit-report warmup flag" (cosmetic).

## Edge cases

- **Single-cluster plan:** never rotates (already short-circuited at
  `lo_scheduler.py:328-330`); scan-and-hold is a no-op — it hears everything.
- **Plan recompute mid-hold** (operator adds/removes a channel → invalidate
  event): abort the hold cleanly, unpark, reset to SCANNING, re-plan.
- **All quiet:** behaves as a fast metronome at `scan_dwell`.
- **Held channel removed by operator mid-hold:** treat as TX-end → HANG/resume.

## Observability

- New events via `emit_event`: `hold_start` (cluster, channel, trigger level)
  and `hold_end` (duration, end-reason `tx_ended` | `max_hold`). Logged to the
  hit JSONL alongside `cluster_hop`.
- SB5 player/scan card surfaces "HOLDING 121.500" so it reads like a scanner.
- `get_status().lo_scheduler` gains `state` (scanning/holding/hang),
  `held_channel_id`, `hold_elapsed_sec`.

## Phasing (discrete commits, tests per phase)

- **Phase A1 — core state machine (MVP).** SCANNING/HOLDING/HANG, `is_open`
  callback wired in `daemon.py`, `step()` redesign, `scan_dwell_sec` /
  `hang_time_sec` / `max_hold_sec` config (+ `lo_dwell_sec` alias). `solo` OFF
  (all cluster members live during hold). Delivers stop-on-active + hold + hang
  + resume + max_hold. Tests extend `chirp/tests/test_phase4pre.py` (fake clock,
  scripted `is_open` sequences asserting hold/resume/hang/max_hold).
- **Phase A2 — `solo_on_hold`.** Park siblings during hold for true
  one-at-a-time; strongest-signal tiebreak. Tests: two-open-same-tick, sibling
  starts during solo (not heard until resume).
- **Phase A3 — anti-stuck + scan-rate.** `hold_lockout_sec` after forced
  resume; warmup-era hold-trigger optimization (validate, then lower
  `scan_dwell`). Tests: stuck-carrier doesn't re-hold; lockout expiry.
- **Phase A4 — observability + UI.** `hold_start`/`hold_end` events,
  `get_status` state fields, SB5 "HOLDING" display, config knobs in the UI.
- **Phase A5 (future) — priority channels.** Periodically peek a priority
  channel even while holding (interrupt to it if active). Mirrors ham2mon's
  priority list.

## Open questions (proposed defaults above; confirm before A1)

1. `scan_dwell` — ship 2.0 (safe) or push ~1.5 and validate warmup detection?
2. `solo_on_hold` default — strict one-at-a-time (proposed true), or keep all
   cluster members live and only narrow if it's noisy?
3. `hang_time` — 2.0s (typical scanner delay) ok?

## Deploy note

Config-only knobs land in `chirp/config/<band>.json` (read from the repo
checkout — `git pull` is the deploy). Code changes ship via the same pull +
**sequential `gr-demod@airband` then `gr-demod@ground` restart**, in MA→SL
order (master first; stop the SL slave first if the MA master must re-acquire) —
per the 2026-06-05 reboot-recovery lesson, an out-of-order restart can wedge the
shared RSPduo/sdrplay API.

## Reference: code anchors

- `chirp/dsp/lo_scheduler.py` — `step()` (`:310-338`), `_apply_cluster`,
  `_park_channels`, dwell-start ts (`:175`), `dwell_s` (`:153`), guards (`:140`).
- `chirp/hit_detector.py` — 5 Hz squelch poll, warmup gating, parked-skip.
- `chirp/daemon.py` — scheduler wiring (`:556` dwell), `_scheduler_unpark_channels`
  warmup reset (`:1020-1030`), `_cmd_set_squelch`.
- `chirp/dsp/channel.py` — `set_parked` / `_PARKED_SQUELCH_DBFS`,
  `get_signal_level_dbfs`, squelch block.
- `chirp/tests/test_phase4pre.py` — scheduler test harness (fake clock,
  injected callbacks).
