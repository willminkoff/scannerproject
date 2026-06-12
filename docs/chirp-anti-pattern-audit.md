# Chirp anti-pattern audit — Phase 0

**Author:** Will Minkoff (drafted with Claude)
**Date:** 2026-06-12
**Status:** Phase 0 of the chirp rebuild (see `chirp-rebuild-scope-2026-06-12.md`).
**Approach:** read-only. No daemon code changes in Phase 0. Each finding is tagged with the rebuild phase that fixes it, or "defer" if it's outside scope.

## Why this exists

Today's input_gate failure plus the multi-agent code review surfaced six bug classes that share a property: **the daemon can be alive and look healthy in the journal while not doing its job**. That's the "alive-but-useless" failure mode the rebuild is supposed to eliminate by construction. Before writing new code, this audit walks the existing daemon for every instance of each class, so the rebuild doesn't ship the same patterns under new names.

The six classes:

1. Sync-block starvation
2. Two-walk drift in diagnostics
3. Key collisions in serialized snapshots
4. systemd interaction math
5. Discovery-path exclusion gaps
6. Schema disagreements across reading sites

Each section: the pattern signature, current instances (file:line), and the phase that will fix or has fixed it.

---

## Class 1 — Sync-block starvation

**Signature.** A GR sync block (`add_ff`, `multiply_ff`, sample-aligned probe, etc.) waits for input on every port before emitting anything. Any upstream block that stops emitting samples while still being wired into the graph starves the sync block silently — output stops, no error.

**Why it's an "alive but useless" pattern.** The Python process stays running, the GR scheduler doesn't fault, the journal shows nothing wrong. The only symptom is that the downstream sink (icecast, file) stops receiving frames. Today's input_gate experiment was exactly this; ANALOG.mp3 dropped from 32 kbps target to ~3.7 kbps, scratching together silence-padding only.

**Current sync-block instances in chirp.**

| File:line | Block | Inputs | Risk surface |
|---|---|---|---|
| `chirp/dsp/mixer.py:54` | `blocks.add_ff()` | N (one per channel pool slot) | High — every channel sits behind this. Any block on any channel's path that stops emitting (`copy.set_enabled(False)`, `head` after exhaustion, disconnected sink) starves the band. |
| `chirp/dsp/mixer.py:55` | `blocks.multiply_const_ff` | 1 | Low — single-input sync block consuming whatever the adder emits. |
| `chirp/dsp/channel.py:253` | `blocks.multiply_const_ff` (nfm_audio_gain) | 1 | Low. Constant toggle, never stops sample flow. |
| `chirp/dsp/channel.py:290` | `blocks.multiply_const_ff` (audio_trim) | 1 | Low. |
| `chirp/dsp/channel.py:299` | `blocks.multiply_const_ff` (priority_gate latch) | 1 | Low. Mutes by zeroing, doesn't stop emission. |

**Known historical bug:** the `input_gate = blocks.copy(...)` experiment (commit 3bad8bd, reverted 8b30035) inserted a block upstream of each channel whose `set_enabled(False)` consumed samples without emitting → the `add_ff` mixer starved → entire band silent. The revert restored audio. Phase 3 will revisit parked-channel CPU savings using a topology that does NOT break this contract.

**Fix path.**
- **Phase 0 (this doc):** catalog the sync-block contract per node above. Done.
- **Phase 3:** any CPU-savings work for parked channels MUST go either (a) downstream of the audio rate (`mute_ff` after demod) where the sync contract is trivial, or (b) at the topology level (selector + null_source under flowgraph lock/unlock) where the contract is satisfied by construction. The `blocks.copy.set_enabled(False)` pattern is permanently banned for any block upstream of a sync block.

---

## Class 2 — Two-walk drift in diagnostics

**Signature.** A function walks a shared state collection (e.g. `self._slots`) once to compute one set of values, then walks it again to compute related values, without holding a lock across both walks. Slot membership and per-slot state can change between walks. Counters disagree with themselves; classifiers fire false positives.

**Why it's "alive but useless".** A diagnostic that lies. The audio path is fine but the health field says `all_muted`. The dashboard says one thing, the journal says another. Operator can't trust either.

**Current instances.**

| File:line | Status | Notes |
|---|---|---|
| `chirp/hit_detector.py:_tick()` (around old line 290) | **Fixed today (P1-9, commit 8c7595f).** | `parked_count` was tracked in a second lock-free walk of `self._slots`; `muted_count` likewise. A slot erroring in walk 1 could appear in walk 2 → `muted_count >= live_count` with `open_count >= 1` → false `all_muted`. Fix: `parked_count` tracked during walk 1, `muted_count` counted over `live_channels` (the same set as `live_count`). Single walk, consistent counters. |
| `chirp/daemon.py:_cmd_get_status()` lines 1038, 1081 | OK — both walks inside `with self._lock:`. | Maintenance trap: a refactor that hoists `pool_free` out of the locked block would re-introduce the drift. Phase 0 standing rule: "no two walks under a lock should be split across lock boundaries without an explicit comment." |
| `chirp/daemon.py:_get_plan_channels()` line 1148 | OK — single walk under `self._lock`. | Document the contract. |
| `chirp/daemon.py:_scheduler_retune_to()` line 1189 | OK — single walk under `self._lock`. | Document. |

**Pattern to watch in future code.** Any time diagnostics computes counters over multiple iterations of a state set, all iterations must operate on a snapshot captured under one lock, OR the function must be written to tolerate the race (count from a single in-walk set, like the fixed `_tick`).

**Fix path.**
- **Phase 0 (this doc):** done.
- **Phase 3:** the AGC/squelch/priority-gate rework will produce its own diagnostics. They must follow the single-walk pattern or hold the daemon lock.
- **Standing rule (in scope doc):** rule 3 — "No two-walk diagnostics without a lock."

---

## Class 3 — Key collisions in serialized snapshots

**Signature.** A function builds a dict (`data = {...}`) across many lines with many contributors writing keys. Nothing prevents `data["foo"] = X` followed later by `data["foo"] = Y`. The second write silently clobbers the first. If the types differ, downstream consumers crash or misbucket.

**Why it's "alive but useless".** The snapshot LOOKS right but a field is wrong (or missing, or the wrong type). External tools (the chirp-audio-path-probe, the dashboard, any future watchdog) silently degrade.

**Current instances.**

| File:line | Status | Notes |
|---|---|---|
| `chirp/daemon.py:_cmd_get_status` lines 1079 + (was 1116) | **Fixed today (P1-5, commit 8c7595f).** | `data["audio_path"]` set to `str(self._audio_out_path)` at 1079; clobbered by the per-tick diagnostics dict at 1116. Renamed to `audio_path_state`. Probe script reads new key with fallback. |
| `chirp/daemon.py:_cmd_get_status` `data` dict | Latent risk. ~30 keys built across ~70 lines, contributions from `_cfg`, slot snapshots, icecast snapshot, LO scheduler snapshot, priority gate, audio_path_state. | No collision detection. A future contributor wires in `data["thing"]` without realizing some other branch also uses `"thing"`. |
| `chirp/dsp/channel.py:Channel.snapshot()` | Inspected; no collisions visible. | Single function, ~10 keys, no merge. |
| `chirp/dsp/lo_scheduler.py:LoScheduler.snapshot()` | Inspected; OK. | Snapshot is a flat dict built in one place. |
| `chirp/icecast.py:IcecastSink.snapshot()` | Inspected; OK. | Same. |

**Recommended phase-0 mitigation (still no daemon code).** Add a CI lint or a small unit test that builds a representative `get_status` payload from a mock daemon and asserts no key is set twice. The lint would fail PRs that re-introduce the collision pattern. Defer the *implementation* of this lint to Phase 4 (when the test harness is being touched anyway).

**Fix path.**
- **Phase 0 (this doc):** instance catalogued.
- **Phase 4:** add the collision-detection test as part of the get_status / inotify config work.
- **Standing rule:** any new top-level key in a snapshot dict requires a comment naming the owner, and key choice must be checked against the documented schema.

---

## Class 4 — systemd interaction math

**Signature.** A change to one of `RestartSec` / `StartLimitBurst` / `StartLimitIntervalSec` (or `WatchdogSec`) without checking the arithmetic against the others. The restart limiter becomes mathematically unreachable; a wedged process restart-loops forever, each retry re-poking the wedge.

**Why it's "alive but useless".** The service shows `activating`/`auto-restart` forever. systemd never gives up. The wedged downstream (sdrplay_apiService) never gets a chance to recover.

**Current instances.**

| File | Values | Math | Status |
|---|---|---|---|
| `chirp/systemd/gr-demod@.service.template` | `RestartSec=15`, `StartLimitBurst=10`, `StartLimitIntervalSec=60` | 10 attempts × (15 s + ~5 s minimum start) = 200 s, vastly exceeds the 60 s window. Limit is unreachable. The "Bounded by StartLimitBurst/Interval below" comment is **false**. | **P1-7 — open**. |
| `systemd/scanner-digital-op25.service.d/10-after-chirp.conf` | adds `After=` only | no timing change | OK. |
| `chirp/systemd/gr-demod@airband.service.d/zz-lo-clusters.conf` | env only | no timing change | OK. |
| `chirp/systemd/gr-demod@*.service.d/zz-audio-trace.conf` | env only | no timing change | OK. |

**Fix path.**
- **Phase 0 (this doc):** identified.
- **Phase 5 (boot atomicity + lease):** the right structural fix is to replace the sleep-tuned back-off with an `ExecStartPre` readiness probe on `sdrplay_apiService` so chirp only starts when sdrplay is actually healthy. Until then, the immediate fix is `StartLimitIntervalSec=180` (or `StartLimitBurst=4`) so the math closes. Land the arithmetic fix as a small PR alongside Phase 1 — that PR has CI coverage for the unit anyway.
- **Standing rule (rule 5):** "systemd timing changes require arithmetic."

---

## Class 5 — Discovery-path exclusion gaps

**Signature.** A discovery function (find serials, find devices, find configs) takes a primary path and a fallback path. The primary applies a filter; the fallback does not. When the primary returns empty for any reason, the fallback hands out the excluded item.

**Why it's "alive but useless".** The system runs but uses the wrong device / config / serial / etc. Probably won't fail immediately; the symptoms surface as race conditions, hardware conflicts, or "why is OP25 stealing chirp's RSPduo?"

**Current instances.**

| File:line | Status | Notes |
|---|---|---|
| `ui/favorites_runtime.py:_rspduo_tuner_ids()` sysfs vs SoapySDR fallback | **Fixed today (P0-3, commit 07fd949).** | Exclusion was applied in `_rspduo_usb_serials` (sysfs) but not on the SoapySDR fallback. Now applies on both. |
| `ui/favorites_runtime.py:_chirp_dedicated_rspduo_serials()` | **Latent (P1-8) — open.** | Falsely claims to honor `CHIRP_CONFIG_DIR`; chirp's loader doesn't read that env var. Misses `CHIRP_SDR_DEVICE_ARGS` overrides entirely. Silent empty when working directory is wrong. `airband-ui.service` runs as `/opt/airband-ui` with no `WorkingDirectory`, so the repo-relative fallback resolves only if `/opt/airband-ui` is the checkout. All read errors are swallowed (`except: continue`); an empty exclusion set produces zero log output. |
| `ui/favorites_runtime.py:_rtl_airband_dedicated_rspduo_serials()` | Inspected briefly; same pattern. | Likely has analogous gaps but used less often now that chirp owns the analog bands. Audit during Phase 5 (device ownership). |

**Fix path.**
- **Phase 0 (this doc):** P0-3 closed today, P1-8 catalogued.
- **Phase 1 (source contract validation):** the `_chirp_dedicated_rspduo_serials` resolver gets honest. Honor `CHIRP_SDR_DEVICE_ARGS`. Emit a WARNING when the exclusion resolves empty. Either make chirp's loader actually read `CHIRP_CONFIG_DIR` or remove the stale comment and document `CHIRP_RSPDUO_SERIAL` as the required deployment setting on the Micro — and set it in `/etc/airband-ui.conf` as part of the deploy. Add unit tests (config-file path, env override, unreadable file → empty set + warning).
- **Standing rule (rule 7):** "Discovery-path exclusions apply on all branches."

---

## Class 6 — Schema disagreements across reading sites

**Signature.** The same sidecar key (or config field) is read by two different functions that disagree on what shapes they accept. One accepts list-only; the other accepts list-or-string. The feature works on the lenient side and silently no-ops on the strict side.

**Why it's "alive but useless".** The setting LOOKS like it's applied. The dashboard or one code path honors it. The other code path doesn't, and the operator can't tell why their override isn't sticking.

**Current instances.**

| Key | Reading sites | Status |
|---|---|---|
| `avoid_site_ids` (under `site_policy`) | `ui/op25_adapter.py:_norm_site_list` (~line 505) — accepts list AND `"758, 759"` string. `ui/favorites_runtime.py:_apply_avoid_site_ids` (~line 1132) — requires `isinstance(raw, list)`; string form silently skipped. | **P1-10 — open.** |
| (Other potential collision points.) Sweep deferred until Phase 4 (config + inotify), where every config-reading site gets touched anyway. | TBD | defer. |

**Fix path.**
- **Phase 0 (this doc):** P1-10 catalogued.
- **Phase 4 (inotify config):** extract `_norm_site_list` into a shared helper. Use it in `_apply_avoid_site_ids`. Add a test with `"site_policy": {"avoid_site_ids": "S1, S2"}` asserting the sites are dropped from systems.json. Sweep all other sidecar keys for similar disagreements while the config code is being touched.
- **Standing rule (rule 6):** "One schema per sidecar key."

---

## P2 cleanups (also catalogued for completeness)

These came out of the review; not bug classes, but housekeeping that touches the same files Phase 1-4 will edit. Fold opportunistically.

- `chirp/daemon.py:1119` — the hand-typed 7-key fallback dict in the `except` of `audio_path_state` duplicates the snapshot schema owned by `HitDetector.__init__` and invents `audio_path_health="unknown"`, which is intentionally outside the documented enum so probe scripts can bucket "snapshot failed" separately. Documented now, but the duplication remains a maintenance trap. Phase 3 should make `HitDetector` own a default constant and have `get_status` import it.
- `ui/favorites_runtime.py` ~lines 1524-1534 and ~1598-1607 — both do the relative/absolute import dance and parse the same `op25_system_config.json` per sync. Hoist one read; share between the avoid-site filter and the tuner-cap path. Phase 4 candidate.
- `tests/test_favorites_runtime_rspduo_discovery.py:135` claimed single-process MA/SL was "the safe pattern proven by chirp" — wrong on both counts. **Already fixed during the OP25 dual-trunk work.** Re-verify during Phase 5.
- Stale comments at `ui/favorites_runtime.py` ~1589 and ~1623 still describe the inverted-away "Tuner 1 only" cap. Fold into Phase 5.

---

## What this audit does NOT cover (and why)

- **OP25 daemon internals.** Out of scope per the rebuild doc. The OP25-adjacent fixes from the review are landed; deeper OP25 reliability work is its own scope.
- **PipeWire / icecast sink backpressure.** Flagged in the rebuild doc as a follow-up. Phase 6 (soak + chaos) will surface specific symptoms if they exist; only then file as a follow-up.
- **UI / airband-ui.py / handlers.py** beyond `_chirp_dedicated_rspduo_serials` and the `_apply_avoid_site_ids` schema disagreement. UI liveness is a separate scope.
- **Hardware/kernel layer.** Per the rebuild doc, anything below userspace cannot be audited from inside the daemon.

---

## Phase 1 handoff

Phase 1 (source contract validation) should reference:
- Class 1 above to confirm the source block change does not break any sync-block contract.
- Class 5's P1-8 finding — the exclusion-set work belongs here.

Phase 0 deliverable complete. Total reading: ~half a day. Standing rules now live in `chirp-rebuild-scope-2026-06-12.md`.
