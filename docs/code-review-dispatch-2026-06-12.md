# Dispatch work order — code-review fixes (2026-06-12 working tree)

**Scope:** uncommitted changes on `main` (chirp input-gate park optimization, audio-path
tracing, RSPduo Tuner-2 unlock, avoid_site_ids hard filter, systemd RestartSec bump).
A 7-angle multi-agent review with per-finding adversarial verification confirmed 13
defects. This doc is self-contained: each item has the problem, the fix spec, and
acceptance criteria. No other context is required.

**Deploy gate:** items 1–4 are P0. Do NOT deploy the working tree to the Micro until
1–4 are fixed. Item 1 is a total-silence regression on the analog bands; items 2–4
each recreate the sdrplay MA/SL collision this change set was written to eliminate.

**Background invariant (read first):** OP25 and chirp must never open the same
physical RSPduo concurrently, and a single Python process can never open Master+Slave
on one RSPduo through gr-osmosdr (the second `osmosdr.source` raises
`SelectDevice() failed`; a botched open can wedge `sdrplay_apiService` system-wide).
The working pattern is **split-process** MA/SL with `OP25_RSPDUO_LAUNCH_GAP_SEC`
serialising the opens — this is how chirp's `gr-demod@airband` + `gr-demod@ground`
share one RSPduo.

---

## P0-1 · input_gate park silences the ENTIRE band — rework or revert

- **Files:** `chirp/dsp/channel.py` (~line 383 `set_parked`; gate built ~line 192,
  wired ~line 305), `chirp/daemon.py:656` (channel→mixer wiring),
  `chirp/dsp/mixer.py:54` (`blocks.add_ff`).
- **Problem:** every channel feeds one port of a shared `blocks.add_ff`. `add_ff` is a
  GR **sync block**: it produces output only when ALL input ports have samples. The new
  `input_gate` is a `blocks.copy`; `set_enabled(False)` consumes input and emits
  **nothing**, so the first parked channel starves the adder permanently → the whole
  band's audio sink (file/icecast) goes silent. The LO scheduler parks channels
  routinely in any multi-cluster plan, so this fires almost immediately in production.
  The pre-change design was safe because parked channels kept streaming
  squelch-closed **zeros**, keeping every adder port fed.
- **Fix spec (pick one):**
  - (a) **Revert** the input_gate entirely (constructor block, wiring, `set_parked`
    calls, `input_gated` snapshot field, and the two new tests in
    `chirp/tests/test_lo_scheduler.py`). Accept the FIR burn for now.
  - (b) Replace with a construct that emits zeros when parked (keeps the adder fed)
    — e.g. multiply-by-zero const toggled on park, placed AFTER the demod where the
    rate is audio not 2 Msps, or `blocks.mute_ff` semantics.
  - (c) Keep CPU savings via real topology change (valve + null_source/selector with
    flowgraph lock/unlock) — larger change, only if (b) is insufficient.
- **Acceptance:** a test that builds a daemon-shaped graph (or minimal
  source→2 channels→AudioMixer→probe), parks one channel, runs the graph, and asserts
  the mixer still produces samples. Existing lo_scheduler tests still pass.
- **Also fix while there (P1-6, same function):** parked channels freeze with
  `squelch_open=True` if parked mid-transmission — the gate stops samples before the
  squelch slam can take effect, and GR squelch state only updates in `work()`. Make
  `get_squelch_open()` return `False` when `self._is_parked` (single source of truth);
  then delete the `input_gated` drift-detector field and correct the two
  "order matters" comments in `set_parked` (they describe the opposite of what GR
  guarantees). This holds regardless of which fix option above is chosen.

## P0-2 · Single system + single RSPduo generates in-process MA+SL (crash loop)

- **Files:** `scripts/ensure-op25-runtime.py` (`_detect_traffic_dongle` ~line 415–422,
  split condition ~line 350, follower-skip ~line 400, `_select_rspduo_modes`
  ~line 182–189), allocator strategy `ui/dongle_allocator.py:140–149`.
- **Problem:** `_rspduo_tuner_ids` now always emits Tuner 2, so with one system and one
  RSPduo the `single_system` strategy puts `RSPduo Tuner 2 SER#X` into
  `traffic_pool`; `_detect_traffic_dongle` returns `pool[0]` with no RSPduo check;
  `_select_rspduo_modes` sees both tuners and assigns MA+SL; and
  `_build_runtime_process_plans` only splits when there are ≥2 RSPduo **anchors** —
  the RSPduo-follower skip lives only in that branch. Net: one `multi_rx.py` with
  `mode=MA` control + `mode=SL` traffic on the same physical device →
  `SelectDevice() failed` → status=1 restart loop, possible sdrplay wedge. This is the
  most common deployment shape (one favorite, one RSPduo).
- **Fix spec:** never allow a same-physical-serial RSPduo Tuner 2 to be a traffic
  follower **in the same process** as its Tuner 1. Either skip RSPduo tuners in
  `_detect_traffic_dongle` (traffic followers should prefer RTL serials), or hoist a
  same-box Tuner-2 follower into its own plan exactly like the ≥2-anchor branch does.
- **Acceptance:** new test in `tests/test_ensure_op25_runtime_rspduo_args.py`: one
  system, dongle map with both tuners of one serial available, assert the generated
  plan set never co-locates Tuner 1 (MA) and Tuner 2 (SL) of the same serial in one
  process.

## P0-3 · SoapySDR fallback bypasses the chirp/rtl-airband serial exclusion

- **File:** `ui/favorites_runtime.py` `_rspduo_tuner_ids` (~line 459–482).
- **Problem:** the exclusion set (`_rtl_airband_dedicated_rspduo_serials() |
  _chirp_dedicated_rspduo_serials()`) is applied only inside `_rspduo_usb_serials`
  (sysfs path). When that returns `[]` — which happens **precisely when the only
  attached RSPduo is chirp's and got filtered out**, and also during the boot-time USB
  enum race — the code falls through to `SoapySDR.Device.enumerate`, which applies NO
  exclusion and now emits BOTH tuners of the excluded box. OP25 then races chirp on
  `sdrplay_api_Open` against the same physical device.
- **Fix spec:** apply the same union exclusion set to the fallback's serial list
  before `_expand_rspduo_ids`. Additionally (recommended): make `_rspduo_usb_serials`
  distinguish "no devices attached" from "devices attached but all excluded" (e.g.
  return a sentinel or have the caller re-check the exclusion) so the Soapy fallback
  doesn't fire at all in the all-excluded case — Soapy enumeration is also documented
  in-file as hang-prone on the Micro.
- **Acceptance:** extend `tests/test_favorites_runtime_rspduo_discovery.py`: with the
  sysfs probe returning [] and the fake Soapy enumerating serial X, patch the chirp
  exclusion to {X} and assert `_rspduo_tuner_ids()` returns [].

## P0-4 · OP25_RSPDUO_SPLIT_PROCESSES=0 escape hatch now produces a broken config

- **Files:** `ui/favorites_runtime.py` (~line 440–456, gate removed),
  `scripts/ensure-op25-runtime.py:350` (flag still honored, default "1"),
  `tests/test_ensure_op25_runtime_rspduo_args.py` `test_split_disabled_keeps_everything_in_one_process`.
- **Problem:** the removed `allow_dual` gate was the only thing keeping Tuner 2 out of
  the pool when split=0. Now split=0 still gets two systems assigned to Tuner 1 +
  Tuner 2 of one box, and the planner collapses them into ONE process with MA+SL
  device args → guaranteed `SelectDevice()` failure. Pre-change, split=0 gracefully
  capped to one system per box.
- **Fix spec:** make the layers agree on what split=0 means. Recommended: in the
  split=0 single-process path of `_build_runtime_process_plans` (or in
  `_select_rspduo_modes`), drop any same-box Tuner-2 system with a logged warning so
  the output degrades to the old one-system-per-box behavior. Alternative: have
  `_rspduo_tuner_ids`/`_available_digital_tuner_count` consult the flag again.
- **Acceptance:** update the escape-hatch test to assert the safe degraded behavior
  (one process, ONE system, no MA+SL pair in a single plan), not the broken one.

---

## P1 — fix before/with the next deploy

## P1-5 · get_status `audio_path` key collision

- **File:** `chirp/daemon.py` (~line 1116 clobbers the string set at ~line 1079).
- **Problem:** `data["audio_path"]` (audio output file path, str) is unconditionally
  overwritten by the new diagnostics dict — the path silently disappears and the
  field changes type.
- **Fix spec:** rename the new key to `audio_path_state` (matches the
  `audio_path_state` event name). Update `scripts/chirp-audio-path-probe.py` (~line
  121) which reads the dict shape, and the systemd drop-in README if it mentions it.
- **Acceptance:** get_status returns both `audio_path` (str) and `audio_path_state`
  (dict).

## P1-6 · Frozen-open squelch on parked channels

- Folded into **P0-1** above (same function, same fix window). Listed here so it
  isn't lost if P0-1 is resolved by revert: even with the gate reverted this is
  moot, but if any gate variant is kept, `get_squelch_open()` must return False
  while parked.

## P1-7 · RestartSec=15 disarms the systemd restart circuit-breaker

- **File:** `chirp/systemd/gr-demod@.service.template` (RestartSec line ~58,
  `StartLimitBurst=10` / `StartLimitIntervalSec=60` at ~lines 74–75).
- **Problem:** 10 attempts spaced ≥15 s span ≥135 s, so >10 starts can never land in a
  60 s window — the start limit is mathematically unreachable, a permanently wedged
  sdrplay state restart-loops forever (each attempt re-poking the wedged daemon), and
  the new comment "Bounded by StartLimitBurst/Interval below" is false.
- **Fix spec:** raise `StartLimitIntervalSec` to ≥180 (or lower `StartLimitBurst` to
  ≤4) so the bound is reachable with RestartSec=15; correct the comment. Note both
  drop-in dirs (`gr-demod@airband.service.d/`, `gr-demod@ground.service.d/`) were
  checked — nothing overrides these values. Longer-term (optional, separate change):
  replace the sleep-tuned backoff with an `ExecStartPre` readiness probe on
  `sdrplay_apiService`.
- **Acceptance:** arithmetic in a comment next to the values showing the limit is
  reachable (burst × (RestartSec + min start time) < interval).

## P1-8 · Chirp serial exclusion can be silently empty on the deployed box

- **File:** `ui/favorites_runtime.py` `_chirp_dedicated_rspduo_serials` (~line 215–280).
- **Problems (all confirmed):**
  1. The comment "Mirror the band loader's path discovery so a CHIRP_CONFIG_DIR
     override … is honoured" is false — `chirp/daemon.py:load_config` resolves
     `Path(__file__).parent / "config"` and never reads `CHIRP_CONFIG_DIR`; the env
     var exists nowhere else in the repo.
  2. The daemon supports `CHIRP_SDR_DEVICE_ARGS` env override of device_args
     (`chirp/daemon.py` ~line 420), invisible to this function — a drop-in override
     would desync the exclusion from what chirp actually holds.
  3. `airband-ui.service` runs `/opt/airband-ui/airband_ui.py` with no
     `WorkingDirectory`; the repo-relative fallback resolves only if /opt/airband-ui
     is the checkout. All read errors are swallowed (`except: continue`), and an
     empty exclusion set produces zero log output.
  4. No test covers the function.
- **Fix spec:** (a) honor `CHIRP_SDR_DEVICE_ARGS` if set in the environment; (b) log a
  WARNING when the exclusion set resolves empty (one line, once per call is fine);
  (c) either make chirp's loader actually read `CHIRP_CONFIG_DIR` or fix the comment
  and document `CHIRP_RSPDUO_SERIAL` as the required deployment setting on the Micro
  — and set it in `/etc/airband-ui.conf` as part of the deploy; (d) add unit tests
  (config-file path, env override, unreadable file → empty set + warning).
- **Acceptance:** tests pass; deploy checklist includes `CHIRP_RSPDUO_SERIAL` in
  `/etc/airband-ui.conf`.

## P1-9 · `all_muted` health (the declared bug signature) fires falsely

- **File:** `chirp/hit_detector.py` `_tick` (diagnostics walk ~line 292–315).
- **Problem:** `muted_count` is counted over a second lock-free walk of `self._slots`,
  a different set than `live_count`/`open_count` from the first walk (which drops
  slots whose squelch/level reads raised; park state can also flip between walks —
  no lock is held, contradicting the module docstring). A stale-muted erroring slot
  → `muted_count >= live_count` with `open_count ≥ 1` → `health="all_muted"` while
  audio flows.
- **Fix spec:** count `muted_count` (and `parked_count`) during/over the same set the
  first loop built (`live_channels`), eliminating the second O(N) walk per tick.
  Remove the unreachable inner `try/except: pass` around the `getattr` bool. Fix the
  module docstring's false claim that the daemon lock is taken.
- **Acceptance:** unit test: one healthy open unmuted channel + one slot whose
  `get_squelch_open` raises while `is_priority_muted=True` → health must be "live",
  not "all_muted".

## P1-10 · `avoid_site_ids` string form silently ignored by the hard filter

- **Files:** `ui/favorites_runtime.py` `_apply_avoid_site_ids` (~line 1132–1134) vs
  `ui/op25_adapter.py` `_norm_site_list` (~line 505–521).
- **Problem:** op25_adapter accepts both list and `"758, 759"` string forms for the
  soft −80 penalty; the new hard exclusion requires `isinstance(raw, list)` and
  silently skips strings. Same sidecar key, two accepted syntaxes, and the feature's
  primary mechanism (dropping the site before systems.json/trunk.tsv) silently
  doesn't apply for the string form.
- **Fix spec:** reuse op25_adapter's normalization — extract `_norm_site_list` (or
  import it) and use it in `_apply_avoid_site_ids`.
- **Acceptance:** test with `"site_policy": {"avoid_site_ids": "S1, S2"}` asserting
  the sites are dropped.

---

## P2 — cleanups (fold into the PRs above, no separate work)

- **Fallback dict schema drift** — `chirp/daemon.py` ~line 1119: the hand-typed 7-key
  fallback duplicates the snapshot schema owned by `HitDetector.__init__` and invents
  `audio_path_health="unknown"`, which is not in the documented enum
  (live/all_muted/no_open/no_live) — `chirp-audio-path-probe.py` would mis-bucket it.
  The except path is effectively unreachable (snapshot is `dict(self._audio_path)`,
  always initialized; hit_detector exists before the cmd server starts). Drop the
  try/except, or fall back to a HitDetector-owned default constant.
- **Duplicate sidecar read** — `ui/favorites_runtime.py` ~lines 1524–1534 and
  ~1598–1607 both do the relative/absolute import dance and parse the same
  `op25_system_config.json` per sync. Hoist one read; share between the avoid-site
  filter and the tuner-cap path.
- **Contradictory/stale docs (maintenance trap)** —
  `tests/test_favorites_runtime_rspduo_discovery.py:135` claims single-process MA/SL
  is "the safe pattern proven by chirp": wrong on both counts (it fails with
  `SelectDevice()`; chirp is split-process). Stale comments at
  `ui/favorites_runtime.py` ~1589 ("never a same-box Tuner 2") and ~1623 ("Tuner 1
  only") still state the inverted-away invariant. Align all with the split-process
  model stated in `scripts/ensure-op25-runtime.py:327–336`.
- **Test boilerplate** — `tests/test_ensure_op25_runtime_rspduo_args.py`
  `test_split_disabled_…` hand-rolls env save/restore; use
  `mock.patch.dict(os.environ, {...})` like the sibling tests added in the same diff.
- **Dead-ish review note** (verified NOT a bug, do not "fix"): `_apply_avoid_site_ids`
  running after `_normalize_digital_pool` does NOT leak avoided control channels into
  what OP25 reads — `profile_editor.save_digital_editor_payload` rebuilds
  control_channels.txt from the filtered systems.json. Only the result dict's
  `talkgroup_count`/`control_channel_count` are stale pre-filter values; patch those
  counts if convenient, nothing more.

---

## Verification (run after fixes)

```bash
# Unit tests touched by this work
python3 -m pytest chirp/tests/test_lo_scheduler.py -q
python3 -m pytest tests/test_ensure_op25_runtime.py tests/test_ensure_op25_runtime_rspduo_args.py -q
python3 -m pytest tests/test_favorites_runtime_rspduo_discovery.py tests/test_favorites_runtime_tuner_cap.py -q

# Full suites
python3 -m pytest chirp/tests -q
python3 -m pytest tests -q
```

Hardware smoke (on the Micro, after deploy): park/unpark a cluster and confirm the
band's icecast mount keeps producing audio (P0-1); run one favorite with one RSPduo
and confirm the generated `multi_rx.json` never pairs Tuner 1 + Tuner 2 of one serial
in a single process (P0-2); confirm `get_status` shows both `audio_path` (path) and
`audio_path_state` (dict) (P1-5).

## Suggested PR slicing

1. **PR A (P0, ship first):** items 2, 3, 4 — all in `ui/favorites_runtime.py` +
   `scripts/ensure-op25-runtime.py`, same hazard (RSPduo collision), test together.
2. **PR B (P0):** item 1 (+6) — chirp DSP gate rework or revert.
3. **PR C (P1):** items 5, 7, 8, 9, 10 + P2 cleanups.
