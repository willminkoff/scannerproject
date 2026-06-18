# SB6 — `probe_rate` audio-flow watchdog gate

**Status:** shipped on branch `sb6-phase1-2-chirp-config-hardfail`, default **OFF**.
**Date:** 2026-06-18
**Files:** `chirp/audio_probe.py` (new), `chirp/dsp/icecast_sink.py`,
`chirp/daemon.py`, `chirp/tests/test_audio_probe_watchdog.py`.

## The bug it fixes

Twice on 2026-06-18 a chirp daemon reported `audio_path_health: live`, active
systemd services, and healthy metrics while its audio branch published **exact
−180 dBFS digital silence** at the Icecast mount:

- **Morning (airband):** post-soft-reboot, the audio branch silently wedged.
  Only diagnosed by capturing `/ANALOG.mp3` and analyzing RMS.
- **Afternoon (ground):** discovered to have been silently wedged since the
  6/16 RTL migration, hidden under a −7 squelch that produced 0 hits.

The defect class: **chirp pings systemd `WATCHDOG=1` even when the audio branch
is producing zeros.** Hits fire, channels open, telemetry stays green — but no
audio reaches the mount, and the daemon never restarts because the watchdog
ping doesn't verify that audio is actually flowing.

## Why the existing telemetry can't catch it

Every pre-existing "is audio healthy" signal is **bytes-based**:

- `icecast_bytes_sent` / `chirp_audio_bytes_published_total`
- the UI's `mount_publishing` heuristic (bytes/sec > 0)

But — as `chirp/dsp/icecast_sink.py` documents in its own module docstring —
**libmp3lame emits perfectly valid MP3 frames for a constant-zero PCM input.**
A wedged branch that feeds the encoder nothing but zeros still produces a
steady MP3 byte stream, so every byte counter keeps climbing and the mount
stays "green". *Measuring bytes cannot detect this wedge.*

The only layer where real audio and the wedge differ is the **PCM amplitude at
the sink input**: an open-squelch channel always carries at least its noise
floor (≈ −60…−40 dBFS), while the wedge is exact `0.0` (−∞ dBFS). That is where
the probe taps.

## Mechanism

```
Mixer ─► IcecastSink.work()  ──peak |sample|──►  AudioFlowProbe.observe_peak()
            (float32 PCM)                              │ records last non-silent ts
                                                       ▼
main loop ── time.monotonic() + audio_path_health ──► AudioFlowProbe.evaluate()
                                                       │
                                          should_ping? ▼
                                    _sd_notify("WATCHDOG=1")  (or withhold)
```

1. **Tap (`IcecastSink.work`)** — one cheap `abs(block).max()` per audio block,
   handed to `AudioFlowProbe.observe_peak(peak)`. The probe records the
   `time.monotonic()` of the last block whose peak exceeded `silence_eps`.
   No-op when the probe is disabled, so the default config pays nothing.

2. **Gate (`AudioFlowProbe.evaluate(now, health)`)** — the main loop calls
   `ChirpFlowgraph.audio_watchdog_status(now)` once per watchdog cycle (~10 s),
   which reads the hit detector's `audio_path_health` and delegates:

   | `audio_path_health` | meaning | decision |
   |---|---|---|
   | `no_open` / `no_live` / `all_muted` | silence is **expected** (nothing keyed, all parked, or gate-muted) | **always ping** |
   | `live` + recent flow | a channel is open and audio is flowing | **ping** |
   | `live` + no flow ≥ grace **and** live ≥ grace | open channel, only −180 dBFS PCM → **wedge** | **withhold ping** |

3. **Restart** — when the ping is withheld, systemd's `WatchdogSec=30` (already
   configured in `gr-demod@.service`) fires and restarts the daemon. The main
   loop also emits an `audio_branch_silent` event + an `STATUS=` line, sets the
   `chirp_audio_branch_silent` gauge to 1, and surfaces it in `get_status`
   under `audio_probe` so the reliability layer / UI can display it directly
   instead of trusting the byte counters that lie.

### Why both `gap ≥ grace` **and** `live_for ≥ grace`

Between transmissions the squelch closes, the mixer goes silent, and
`last_flow` goes stale. When a new transmission opens, `health` flips to `live`
and the gap-since-flow can already be minutes. Gating on the gap alone would
fire a false restart on the **first** transmission after any quiet stretch. The
`live_for` clause (time since `health` last entered `live`) requires the path
to have been continuously live for the full grace window before we trust the
gap, so a freshly-opened channel is never punished for a stale gap. The live
timer resets whenever `health` leaves `live`.

This is the "must not regress legitimate quiet periods" guarantee, regression-
tested in `TestLiveForGuard` and `TestSilenceExpected`.

## Threshold tuning

| Knob | Env | Default | Notes |
|---|---|---|---|
| Enable gating | `CHIRP_AUDIO_PROBE_ENABLED` | `0` (off) | Master switch / rollback. |
| Silence grace | `CHIRP_AUDIO_PROBE_SILENCE_GRACE_S` | `10.0` | Seconds the path may report `live` with no real PCM before it's called wedged. |
| Silence epsilon | `CHIRP_AUDIO_PROBE_SILENCE_EPS` | `1e-4` (≈ −80 dBFS) | float32 peak below this counts as silent. |

- **Grace** is deliberately generous. The observed failures are *permanent*
  silence, so the risk is false positives, not missed wedges. It must exceed
  the longest legitimate `live`-but-silent gap — e.g. a VAD hangover inside a
  transmission (a couple of seconds at most). At `10 s`, detection + the `30 s`
  `WatchdogSec` means a wedge clears in ≲ `40 s` versus *forever* today. Lower
  it for faster detection only after watching `chirp_audio_branch_silent` on
  Micro confirm no false trips.
- **Epsilon** sits in the wide dead band between the wedge (`0.0`, −∞ dBFS) and
  a real open-squelch noise floor (≈ −60…−40 dBFS). Anything in (−120, −70)
  dBFS works; −80 dBFS keeps margin on both sides. Raise it only if a band is
  found to idle above −80 dBFS while genuinely silent.

## Rollback

The behavior is gated behind `CHIRP_AUDIO_PROBE_ENABLED`, default `0`. With it
off (the shipped default), `audio_watchdog_status()` returns
`(True, "probe_disabled")` unconditionally and `observe_peak` is a no-op — i.e.
**byte-for-byte the pre-probe behaviour** of pinging `WATCHDOG=1` every cycle.
To disable after enabling: unset the env (or set `=0`) and restart the daemon
via the safe path (`recover-sdrplay.sh`, per the airband MASTER-restart-wedge
note — a bare `systemctl restart gr-demod@airband` wedges device reopen).

If the gate ever misfires (withholds pings on a healthy branch), the symptom is
a restart loop; the immediate mitigation is `CHIRP_AUDIO_PROBE_ENABLED=0`.

## Validation status

- Pure-probe logic + source-level wiring: 21 tests in
  `chirp/tests/test_audio_probe_watchdog.py`, all green (run anywhere — no
  gnuradio needed).
- End-to-end `IcecastSink.work()` tap + `get_status` surfacing: needs gnuradio,
  so validated on Micro. **Not yet deployed** — surface the diff to Will and
  get a greenlight before enabling on Micro. Validate by: enable on one band,
  confirm `chirp_audio_branch_silent == 0` during normal traffic for a soak
  window, then deliberately wedge (or replay a known-silent capture) and
  confirm the gauge flips to 1 and systemd restarts within ≈ `grace + WatchdogSec`.
```
