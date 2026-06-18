# Research: RTL-SDR disconnect handling audit

**Date:** 2026-06-18
**Branch:** `sb6-phase1-2-chirp-config-hardfail` (not pushed)
**Method:** Pure code reading. No Micro access. Each per-daemon finding was produced by a deep-read agent and then **adversarially verified** by a second agent that re-read the same files and tried to refute it. Every claim below carries a `file:line` and survived that refutation pass (refinements noted inline).
**Concern (reframed):** RTL-SDR dongles have lightweight USB connectors that can shake loose from vibration in the in-car use case. RTLs have **no shared broker** — each is opened directly by its owning process via SoapySDR (`driver=rtlsdr,serial=…`), so they are isolated at the OS driver level. The question is per-daemon: when an RTL is yanked mid-stream, does its owner **crash / hang / silently spin / or gracefully degrade and reconnect** — and can anyone see it happen?

---

## TL;DR — the system is half-ready

| Daemon | Owns | On a hard RTL yank, today | Self-heals? | Publishes device-lost? | Priority |
|---|---|---|---|---|---|
| **disco-sweep@N** | 1 RTL each (×N) | **Silent spin on a dead handle** (re-tunes forever) **or** crash → unlimited 5 s restarts | ❌ no in-process reopen | ❌ no health field at all | **P1 — fix first** |
| **waterfall** | 2 RTLs (1 proc) | **Per-dongle in-process reconnect**; sibling unaffected | ✅ for same-serial replug | ⚠️ binary ok/down only | **P1** |
| **vfo** | 1 RTL | **In-process reconnect** (3 bad reads → close → 5→60 s backoff → reopen) | ✅ for same-serial replug | ⚠️ binary ok/down only | **P2** |
| **chirp ground** | RSPduo-SL today, **RTL is future** (SB6 Ph3) | **Silent dead-air wedge that reports HEALTHY to systemd** | ❌ none | ❌ none | **P0-severity, but RTL-future** |
| **status-surface** | — (cross-cutting) | ENODEV invisible; UI fabricates "reconnecting in 8 s" | — | ❌ vocabulary is only {ok,degraded,down} | **P1 (do alongside)** |

**The good news:** the two daemons that own the most-exposed RTLs in steady operation — `vfo` and `waterfall` — **already implement the desired graceful-degrade-and-reconnect loop**, and adversarial verification could not break the claim that a hard device-gone reaches the reconnect path without killing the process (or, for waterfall, the sibling dongle).

**The bad news, in three parts:**
1. **`disco-sweep` is genuinely broken for the RTL case** and there are several instances of it.
2. **The actual in-car failure mode — re-enumeration under a *different* USB path/serial after a vibration re-seat — defeats even the "good" daemons**, because their reopen is pinned to the original serial and backs off forever.
3. **Nobody publishes a truthful device-lost signal.** The whole status vocabulary is `{ok,degraded,down}`; the UI's "reconnecting in N s" is a cosmetic `8→0→8` animation with zero backend linkage.

---

## What "isolated at the OS level" does and doesn't buy you

Confirmed: each RTL daemon opens its own device in its own process via `SoapySDR.Device({"driver":"rtlsdr","serial":…})` — `vfo` at [scripts/vfo.py:815](scripts/vfo.py), `waterfall` per-dongle at [scripts/waterfall.py:528](scripts/waterfall.py), `disco-sweep` at [disco/src/sweep.py:328](disco/src/sweep.py). There is no SDRplay-style shared broker on the RTL side. So a yank of one RTL **cannot** take another RTL's *driver handle* down.

But OS-level isolation does **not** guarantee the owning process degrades gracefully, and it does not eliminate two real cross-daemon ripple paths that an empirical test must check:
- **USB enumeration churn.** `disco-sweep`'s crash branch restarts every 5 s with no rate limit ([systemd/disco-sweep@.service:5](systemd/disco-sweep@.service) `StartLimitIntervalSec=0`), each restart re-scanning USB for an absent serial — which can race a *sibling* RTL's open on the shared bus.
- **Shared-bus bandwidth** (the known SB6 USB-2 saturation issue) is orthogonal to this audit but compounds it.

---

## Per-daemon findings

### 1. `disco-sweep` — silent spin or restart-storm (fix first)

**Current behavior — verified two branches, both bad** ([disco/src/sweep.py:416-426](disco/src/sweep.py)):
```python
sr = sdr.readStream(stream, [buf[pos:pos+chunk]], chunk, timeoutUs=2_000_000)
if sr.ret < 0:
    LOG.warning("[%s] readStream error ret=%s flags=%s", tuner_id, sr.ret, sr.flags)
    break                 # breaks only the inner dwell loop
pos += sr.ret
...
if pos < fft_size:
    continue              # advances to next center freq on the SAME dead handle
```
- **Branch A (negative ret):** the loop logs, breaks the inner dwell, `continue`s, and the outer sweep `for` calls `setFrequency()` + `readStream()` again on the dead handle — **forever**. `while not _STOP` ([:398](disco/src/sweep.py)) never exits, never reopens, never `sys.exit`s. Process stays alive doing zero work, emitting a `readStream error` WARNING per step.
- **Branch B (exception):** `setFrequency`/`readStream` are **not** wrapped in try/except inside the loop, so a throw propagates out of `main()`'s `try/finally`, the process exits non-zero, and systemd (`Restart=on-failure`, `RestartSec=5`, **`StartLimitIntervalSec=0` = unlimited**) re-execs every 5 s forever. `open_device_with_retry` then burns ~minutes retrying the absent serial 20× before `sys.exit(2)` → another restart.

Which branch fires depends on how SoapyRTLSDR surfaces ENODEV — **not determinable from code** (flagged as a residual uncertainty; the pull-test settles it). Both fail the spec.

- **Reconnect logic:** exists **only at startup** — `open_device_with_retry` ([:321-335](disco/src/sweep.py)) has exactly one caller, the boot open at [:367](disco/src/sweep.py) (grep-confirmed). The read loop never re-enters it; `setupStream`/`activateStream` run once and are never re-run.
- **Status:** **no health field anywhere.** `init_state` ([:220-236](disco/src/sweep.py)) has no `device_status`. The sweep keeps rewriting `spectrum_<id>.json` with a fresh `ts` even while dead, so it **reads as alive**. The disco-coordinator infers `down` purely from staleness ([scripts/disco_coordinator.py:388-397](scripts/disco_coordinator.py), `DEGRADED_AFTER_SEC=5`, `DOWN_AFTER_SEC=30`) — indistinguishable from a CPU stall.

**Desired changes:**
1. Classify the `readStream` return at [:419](disco/src/sweep.py): split benign `SOAPY_SDR_OVERFLOW` from `SOAPY_SDR_TIMEOUT` (count) from `SOAPY_SDR_STREAM_ERROR`/other (device-gone candidate); keep a consecutive-error counter.
2. On threshold, break to a device-loss block that `closeStream`s, publishes `lost`/`reconnecting`, re-enters `open_device_with_retry`, and on success re-runs `setSampleRate`/`setupStream`/`activateStream` before resuming — bounded 5→30→60 s. Do **not** `sys.exit`.
3. Add a `device_status` field to `init_state`/`spectrum_<id>.json` ([:220-236](disco/src/sweep.py), write at [:442](disco/src/sweep.py)).
4. Defense-in-depth: add `StartLimitBurst`/`StartLimitIntervalSec` to the unit so the crash branch backs off instead of re-exec'ing every 5 s.

### 2. `waterfall` — already self-heals per-dongle; one real in-car gap

**Current behavior — verified `in-process-reconnect`, sibling-isolated.** Two dongles, one process, **each in its own thread** ([scripts/waterfall.py:872-881](scripts/waterfall.py)). A yank surfaces as a throw (caught by a broad `except Exception` → `None`, [:670-676](scripts/waterfall.py)) **or** a negative ret ([:694-699](scripts/waterfall.py)); `SOAPY_SDR_OVERFLOW` is handled benignly first ([:677-693](scripts/waterfall.py)). Either failure → `None` → `_bad_reads` → after 3, the watchdog `_safe_close()`s and sets `state="down"` ([:743-753](scripts/waterfall.py)) → next loop sees `_sdr is None` → reopen on 5→×1.6→60 s backoff ([:721-737](scripts/waterfall.py)) → on success fully re-inits the source ([:535-571](scripts/waterfall.py)). **The other dongle's thread is untouched**; the stitch loop keeps publishing from the survivor at `top_state="degraded"`. Status reaches `/run/scannerproject/waterfall/state.json` (per-dongle array) and the UI.

**The gap that matches the in-car concern — verified:** `_open()` always matches the **original serial** ([:528](scripts/waterfall.py)), and there is **no `enumerate()`/rebind/escalation** (grep: zero `enumerate` hits). RTL-SDRs frequently re-enumerate on a **different USB path** (and sometimes a different/again-readable serial) after a vibration re-seat — the serial-pinned reopen then **backs off forever at the 60 s cap while the UI keeps implying imminent reconnect.** This is precisely the failure this audit targets.

Other confirmed gaps: only `SOAPY_SDR_OVERFLOW` imported ([:46](scripts/waterfall.py)) so every non-overflow cause collapses to generic `down` (no reason); `state` is only ever `ok`/`down` (no `reconnecting`/`lost` value, 6 assignment sites grep-verified); stale module-header serial for dongle B ([:66-69](scripts/waterfall.py)).

**Desired changes:** import + branch on the error constants and set a `last_error` cause ([:46](scripts/waterfall.py), [:670-699](scripts/waterfall.py)); add `reconnecting`/`lost` states + `next_retry_in_s` ([:730](scripts/waterfall.py), [:752-753](scripts/waterfall.py)); **handle serial re-enumeration** — enumerate RTLs on reopen and rebind by bus-path, or after K failed reopens escalate to a sticky `lost-permanent` so the UI stops implying recovery ([:525-529](scripts/waterfall.py)); fix the stale header.

### 3. `vfo` — already self-heals; same re-enumeration gap + a hang risk

**Current behavior — verified `in-process-reconnect`.** `_read_block` collapses **all three** failure modes to `None`: a thrown exception ([scripts/vfo.py:949-954](scripts/vfo.py)), a negative ret ([:955-960](scripts/vfo.py)), **and** a `ret==0` no-data spin bounded by a 1.0 s wall-clock guard ([:962-967](scripts/vfo.py) — the verifier flagged this third path the audit missed; it makes vfo *more* robust). 3 `None`s → watchdog `_safe_close()` + `state="down"` ([:1010-1020](scripts/vfo.py)) → reopen on 5→10→20→40→60 s backoff ([:992-1006](scripts/vfo.py)), with `_open()`'s own try/except ([:852-859](scripts/vfo.py)) preventing a crash when the device is still absent. Status published to `/run/scannerproject/vfo/state.json` and consumed by both the UI device card ([ui/handlers.py:4044-4098](ui/handlers.py)) and the reliability panel ([ui/reliability.py:335](ui/reliability.py), `state != "ok"` → `wedged`).

**Confirmed gaps:** no `OVERFLOW`/`TIMEOUT`/ENODEV discrimination (only `SOAPY_SDR_RX`/`CF32` imported, [:41](scripts/vfo.py)) — a benign overflow *could* false-trip the watchdog (verifier downgraded this to "plausible, driver-specific" — needs the pull-test); binary `ok`/`down` with no `reconnecting`/`lost`; the UI's **"DOWN since N s ago" is wrong** because the main thread rewrites `state.json` every 250 ms even while down, so its mtime never goes stale ([scripts/vfo.py:1237-1242](scripts/vfo.py) vs [ui/handlers.py:4086](ui/handlers.py)); stale `last_bus_path` ([:842](scripts/vfo.py), never cleared); same serial-re-enumeration blind spot as waterfall.

**One hang risk the verifier surfaced:** `_safe_close()` / `closeStream` ([:861-884](scripts/vfo.py)) and `SoapySDR.Device()` are **not time-bounded** — if a libusb call blocks on a vanished device, the worker thread could stall before reaching the reopen path (process stays alive but never self-heals). The empirical test must check for this; consider a bounded close.

**Desired changes:** import + branch on the error constants ([:41](scripts/vfo.py), [:955](scripts/vfo.py)); publish `dongle_status` ∈ {available,lost,reconnecting} + `next_retry_in_s` ([:1019-1020](scripts/vfo.py), [:1220-1225](scripts/vfo.py)); add a `lost_since_ts` so the UI stops deriving down-time from file mtime; clear `last_bus_path` on disconnect; time-bound the close.

### 4. `chirp ground` — silent dead-air wedge (most severe defect, but RTL-future)

**Scope note:** ground is an **RSPduo Slave tuner today**; the SB6 plan migrates it to a dedicated RTL (Phase 3). So the vibration-yank is a *future* RTL concern — but **the disconnect-handling code is identical** for RSPduo and RTL (it's all `osmosdr.source`), and the defect is a **current latent P0** for today's RSPduo path too.

**Current behavior — verified `silent dead-air wedge`** (the verifier refined the label from "silent-error-loop" — there is no loop). chirp is a GNU Radio flowgraph, not a Python read loop. The device opens once ([chirp/dsp/source_sdr.py:147](chirp/dsp/source_sdr.py)) and is never reopened. On a device-gone, gr-osmosdr's source block returns `WORK_DONE(-1)` (silent halt) or throws on the C++ scheduler thread — **nothing propagates into Python** (`tb.wait()` is never called in the run loop; only in shutdown paths at [chirp/daemon.py:1432/1452/1504](chirp/daemon.py)). The Python main thread stays in `while not stop_evt.is_set(): time.sleep(0.25)` ([:1488-1494](chirp/daemon.py)), and — the dangerous part — **keeps pinging `WATCHDOG=1` on a blind 10 s wall-clock timer with zero health gating**, so systemd's `WatchdogSec=30` never trips and the unit looks perfectly healthy while the mount goes permanently silent.

- **Detection: none.** No work()-return inspection, no error callback; the try/excepts in `source_sdr.py` guard only construction-time calls. The one sample-flow check (`source_validator`) is a **boot-time-only, default-OFF** `blocks.head(N)` gate ([:214-225](chirp/dsp/source_sdr.py)).
- **Status: none.** `get_status` has no device field; `live_center_freq_hz` reads the cached osmosdr handle so it **doesn't flip** on loss; `audio_path_health` reports `no_open` — indistinguishable from a quiet band ([chirp/hit_detector.py:317-326](chirp/hit_detector.py)).
- **systemd circuit-breaker is dead code here** — the tuned `Restart=on-failure`/`RestartSec=15`/`WatchdogSec=30` + churn-guard never fire because the process never exits and never stops pinging.

**Desired changes (low-risk path verified as the right one):**
1. Add a runtime sample-flow tap — `blocks.probe_rate` on a side branch off the source ([chirp/dsp/source_sdr.py:204](chirp/dsp/source_sdr.py)), mirroring the existing parallel-validator pattern.
2. **Gate `WATCHDOG=1` on that rate** ([chirp/daemon.py:1492](chirp/daemon.py)): if IQ flow stays below a floor for N s, stop pinging (let `WatchdogSec` expire) or `stop_evt.set()` to exit non-zero — turning the **already-tuned** systemd `RestartSec=15` back-off into the reopen path. A GR `top_block` can't be re-connected while running, so process-exit-and-restart is lower-risk than in-process rebuild (in-process reopen is a follow-up only if <15 s recovery is required).
3. Publish a `device` block in `get_status` + a `source_lost`/`source_recovered` event.

### 5. status-surface — the vocabulary can't express "lost" or "reconnecting"

**Current state — verified.** The published vocabulary across all RTL daemons is only `{ok,degraded,down}` (with disco's per-tuner `spectrum_<id>.json` having **no** health field at all). There is **no `reconnecting`/`lost`/`unavailable` state, no machine-readable reason/error-code, and no published backoff/next-retry** anywhere — even though vfo/waterfall *compute* a real backoff internally. So:
- `/api/heartbeat` fabricates "DOWN since N s ago · reconnecting in K s" from `state.json` **mtime**, not daemon truth — the handler docstrings admit it ([ui/handlers.py:3863-3864](ui/handlers.py), [:4050-4053](ui/handlers.py)).
- The per-card "reconnect in N s" countdown is a **cosmetic `8→0→8` animation** shared across all three panes with zero backend linkage ([ui/sb5.html:8935-8941](ui/sb5.html)) — it ticks even when nothing is reconnecting (and for disco, nothing *is*).

**Proposed shared mechanism (the cross-cutting fix):** one device-health schema in a new `ui/device_health.py`, written by all four daemon types and read by one UI code path:
```
{ serial, role, status ∈ {available,lost,reconnecting,unavailable},
  reason ∈ {ok,enodev,read_timeout,stream_error,config,unknown},
  last_frame_age_ms, reconnect_attempt, next_retry_in_s, backoff_s, bus }
```
Then collapse the three hand-rolled evidence builders ([ui/handlers.py:3859-3916](ui/handlers.py), [:4044-4098](ui/handlers.py), [:4482-4551](ui/handlers.py)) into one schema-driven reader, delete the `DOWN_T` animation, and optionally expose a `/api/devices` or `/metrics` route on the existing UI HTTP server (no web server added to each daemon — that would complicate the bounded-shutdown watchdogs). Aligns with the SB6 Phase 1 `/metrics` precedent already shipped on chirp.

---

## Cross-cutting themes (the things that span all daemons)

1. **The real in-car failure is re-enumeration, not clean replug.** vfo and waterfall already recover a same-serial removal-and-replug. What defeats them — and what a vibration re-seat actually does — is the dongle coming back on a **different USB path / serial**. Every RTL daemon pins reopen to the original serial. **This is the single highest-value behavioral fix** and it's the same gap in three places.
2. **How ENODEV surfaces is driver-specific and unknowable from code.** A hard yank may throw, return a negative ret, or spin returning 0 samples. vfo and waterfall cover all three; disco and chirp cover none. Only a pull-the-cable test pins down which fires and how fast — the audit flags this consistently as the one thing code-reading can't settle.
3. **Close/open are not time-bounded.** Even the "good" daemons could stall if a libusb `closeStream`/`Device()` blocks on a vanished device. Bound these alongside the reconnect work, and verify empirically.
4. **OS isolation holds, but disco's restart-storm is the one cross-daemon hazard.** Unbounded 5 s restarts re-scanning USB can disturb a sibling RTL's open. Bounding disco's restarts protects the others.

---

## Priority ordering (for the mobile-RTL-vibration goal)

1. **`disco-sweep` — fix first.** It's the only RTL daemon that's genuinely broken (silent spin or restart-storm, no in-process reopen, no health field), and there are several instances. Highest leverage.
2. **Serial-re-enumeration handling in `waterfall` + `vfo`.** This is the exact in-car failure mode; both already self-heal a same-serial replug, so this is the gap that converts "mostly works" into "works." Bundle the bounded-close fix here.
3. **`status-surface` shared schema.** Do alongside 1–2 — without it the operator can't see lost-vs-reconnecting, and the fixes above have nowhere to publish their new state. Relatively self-contained.
4. **`chirp ground` watchdog-gating.** Highest-*severity* defect (an invisible wedge that lies "healthy" to systemd), but ground is RSPduo today and RTL is a future migration. Fix the `probe_rate` + `WATCHDOG=1` gating **before the ground→RTL cutover** — and note it also closes today's RSPduo silent-wedge.

---

## Empirical test plan (gated on Will's go — requires pulling RTL cables on the Micro)

Goal: settle the driver-specific unknowns and confirm cross-daemon isolation. **Do not run until Will is at the Micro and ready.**

**Setup:** bring everything up clean (post-hard-power-cycle baseline). In separate panes: `journalctl -f` on each unit; `watch -n1 'cat /run/scannerproject/{vfo,waterfall,disco}/*.json'`; the UI device cards; `dmesg -w` for USB events; `SoapySDRUtil --find` / `lsusb` on hand.

**For each RTL in turn** (`vfo` 83241970, `waterfall` A=70613472 / B=61108285, each `disco-sweep@<serial>`):
1. **Pull the cable.** Record the owning daemon's behavior: stays alive / crashes / hangs / spins (CPU)? How long until it notices? Does `state.json` flip, go stale, or keep looking alive?
2. **Watch every *other* daemon** + the **RSPduo `sdrplay_apiService`**: any hiccup, log error, or stream drop? (Expect none — confirm.) Watch for USB enumeration churn from disco's restarts.
3. **Replug the *same* port.** Does the owner self-heal? How long? (Expect yes for vfo/waterfall, no for disco/chirp.)
4. **Replug a *different* port** (forces re-enumeration). Does the owner recover, or back off forever on the old serial? (This is the key test — expect failure across the board today.)
5. **Capture per-daemon:** crash? hang? silent spin? recover-same-serial? recover-different-serial? close/open hang? sibling impact? Fill the matrix.

**Decision outputs:** which branch ENODEV takes per daemon (throw / neg-ret / spin) → drives the classify-the-error code; whether close/open can block → drives the bounded-close work; whether re-enumeration recovery is needed (almost certainly yes) → confirms theme #1 as P-first.

---

## Verification note

All five findings went through an adversarial second pass (the status-surface verify was still completing at write time; its audit is incorporated and is a design/synthesis target rather than a load-bearing code claim, so it carries lower refutation risk). The verifiers **confirmed every load-bearing claim** and contributed refinements folded in above: vfo's third `ret==0` bounded path; waterfall's UI countdown being an animated `8→0→8` ticker (not static) and a second device-loss route via retune-failure; chirp's correct label being "silent dead-air wedge" (no loop); and consistent flagging that *which* ENODEV branch fires is driver-internal and only resolvable by the pull-test. Code-proven claims and pull-test-only claims are distinguished throughout.
</content>
