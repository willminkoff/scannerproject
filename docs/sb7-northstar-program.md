# SB7 — "No Third State": program design

**Author:** Claude (senior engineer), for Will Minkoff (PO)
**Date:** 2026-07-04
**Status:** Draft for approval
**Inputs:** full-codebase audit (chirp, ui, monitoring, tests), git-history flaw mining
(2026-06-01 → now), docs/chirp-rebuild-scope-2026-06-12.md, docs/mac-mini-port.md,
all SB6 session handoffs and research docs.

## The north star (unchanged)

> A software platform where stuff doesn't break — not where we can recover quickly
> when it breaks. The daemon is either producing real audio from real samples, or it
> has stopped cleanly with a structured diagnostic. **There is no third state.**
> — docs/chirp-rebuild-scope-2026-06-12.md

The bar (also unchanged): 7 days of untouched uptime, zero "alive but useless"
events, mounts ≥ 0.8× configured bitrate for 95% of the window, every injected fault
producing a loud structured failure → clean recovery.

SB6 did not reach that bar. This document is the honest accounting of why, and the
program that gets there — designed around the hardware actually on hand:

| Asset | Spec |
|---|---|
| Mac mini 2018 | Intel i5-8500B 6-core 3.0 GHz, 16 GB RAM, 4× TB3 (2 buses) + 2× USB-A |
| Mac mini 2021 | Apple M1, 8 GB RAM, 2× TB/USB4 + 2× USB-A |
| 2× SDRplay RSPduo | serials 180903EF32 (RSP-A), 1809063632 (RSP-B) |
| 3× RTL-SDR (attached) | 61108285 (ground), 83241970 (Blog V4, VFO), 56919602 (NESDR, sounding) — verified on the M1, 2026-07-06. A 4th is not currently attached; waterfall is deferred until it is. |

---

## 1. Where SB6 landed — scorecard vs. the rebuild scope

| Rebuild phase | Status | Evidence |
|---|---|---|
| P0 anti-pattern audit | ✅ shipped (catalog + in-flight fixes) | two-walk fix, key-collision fix, StartLimit math fix |
| P1 source-contract validator | ⚠️ built, **never armed** (`CHIRP_SOURCE_VALIDATE` default off, envelope never calibrated) | chirp/dsp/source_validator.py |
| P2 sample-clock LO scheduler | ❌ **not built as spec'd** — scheduler is still wall-clock + Python thread (the drift class the rebuild existed to kill) | chirp/dsp/lo_scheduler.py (`time.time()` tick loop) |
| P2 config hard-fail | ✅ shipped | exit code 3, `chirp_config_load_status` canary |
| P3 global squelch + priority gate | ✅ shipped | commit 80ac180; gate re-derived per tick |
| P3 bounded AGC | ⚠️ bounds shipped, **per-dwell re-baseline missing** (latch class intact) | daemon.py agc params, no dwell reset |
| P3 park-CPU topology | ⚠️ pattern (b) only; pool caps still via live drop-ins, not repo | zz-max-channels.conf on micro |
| P4 inotify config + atomic swap | ❌ **zero code** | — |
| P5 device lease + atomic boot | ❌ **zero code** (boot invariants partial) | — |
| P6 soak + chaos | ❌ **never run** | — |

Also built in SB6 and worth keeping: the probe_rate audio watchdog (e316863 —
**default off, ground band never got its drop-in**), the Prometheus/Grafana/
icecast-exporter stack (f8b3c68 — **no Alertmanager, alerts fire into a UI nobody
watches**), hit-detector JSONL logging, the safe-restart sequencing, the IQ
capture/replay diagnostic harness, pydantic single-schema state.

## 2. Flaw inventory — top to bottom

23 distinct SB6-era flaws were catalogued from docs + git history (16 fixed, 7
open). Full table lives in the audit; what matters for design is the **classes** and
the **open items**.

### Open, unfixed (carried into SB7 as obligations)

1. **RSPduo retune-wedge** — after ~2 h, `sdrplay_api_Update(Tuner_FrF)` →
   `RfUpdateError`; band goes deaf while every internal diagnostic reads healthy.
   Suspected MA/SL retune contention on the shared RSPduo. Workaround only
   (safe-restart ritual). *The single worst "third state" in the product.*
2. **sdrplay_apiService 0x6bed segfault** — deterministic under 2× concurrent
   dual-tuner RSPduos; corrupts live IQ for every consumer. Architectural decision
   was left pending; the interim "fix" was **turning digital off** — i.e. the
   product lost P25 to protect analog.
3. **IcecastSink publish-loop permanent exit** (2026-06-18 ground incident) — one
   transient RTL USB error (-5) kills the publish loop forever; daemon keeps
   scanning, hits keep firing, mount is sourceless. Lies-healthy, all day.
4. **UI "reset radios" ghost units** — `ui/config.py` UNITS defaults still name the
   retired `rtl-airband-*` units; the manual reset path stops real daemons and
   starts ghosts (same defect that caused the 30-min watchdog outage).
5. **Serial-exclusion resolver silent-empty** (P1-8) — resolves to an empty set on
   any read error, exposing chirp's RSPduo to op25. The exact "useful liar" the
   coding standards banned.
6. **Concurrent-restart race** — `restart_rtl_airband()` and `restart_rtl_ground()`
   can both bounce the shared sdrplay daemon with no mutex.
7. **V4 dongle (83241970) USB instability** + BT sink volume reset — hardware/
   platform gremlins, workarounds only.

### The classes (why these keep happening)

- **Safety nets built but never armed.** Source validator, audio probe, alert rules
  — all exist, all default-off or unwired. SB6 *engineered* the north star and then
  didn't turn it on.
- **Restart-to-reconfigure vicious cycle.** Every config change requires a daemon
  restart (Phase 4 never shipped) — and restarts are the #1 wedge trigger
  (apiService churn, MA/SL open races, publish-loop rebuilds). The architecture
  makes its own most dangerous operation routine.
- **The observer shares fate with the observed.** Watchdog, heartbeat, hit pipeline,
  Prometheus — all on the same box as the daemons, reading the same journald, with
  byte counters that lie (lame happily encodes digital silence at full bitrate).
  Peak instance: the reliability watchdog itself caused the worst outage.
- **Dual-tuner fragility.** Every open P0 RF bug traces to RSPduo dual-tuner mode
  (MA/SL) or to the shared closed-source apiService under churn.
- **State sprawl.** Live config scattered over /run, /etc, ~/.local/state,
  ~/scannerproject and live systemd drop-ins; repo deploys don't cover it; sidecar
  schemas parsed differently at different sites (profiles ×3, hp_avoids ×2).
- **No CI.** 124 test files and nothing runs them automatically; the "systemd
  arithmetic" lint the coding standards mandate does not exist; unit files carry
  uncommented RestartSec values today.
- **Mac port stalled at M0.** `SCANNER_SERVICE_BACKEND=launchd` is declared in the
  env file and read by nothing. Everything OS-facing (systemctl, journalctl, /proc,
  wpctl/pw-cat) is still Linux-only. (Also: mac-enable-vnc-tailnet.sh has a
  hardcoded VNC password in the repo — scrub it regardless of path chosen.)

## 3. Design principles for SB7

1. **Eliminate failure classes by construction, not by recovery.** Where hardware
   lets us make a bug *unreachable*, prefer that over any amount of watchdog logic.
2. **Verify from outside the daemon.** No daemon may be the sole judge of its
   own health: "producing real audio" is verified by the on-box prober that
   *listens to the decoded mounts* and pages the phone. (A second-box witness
   was descoped by PO call 2026-07-04 — §5; the residual gap is whole-box
   death, optionally covered by a dead-man ping.)
3. **Arm-by-default.** A safety net that ships default-off is a decoration. Every
   net gets calibrated, flipped on, and CI-guarded in the on position.
4. **Make restarts rare, then make them safe.** Phase-4 live config swap removes
   the routine reason to restart; the broker makes the remaining restarts ordered
   and rate-limited.
5. **One schema, one parser, one state home.** Every sidecar key gets a single
   normalizer; all runtime state lives under one versioned directory that the repo
   can reconstruct.

## 4. Hardware architecture

### 4.1 Device map (reconciled to ACTUAL hardware, 2026-07-06)

Fleet policy v2.1 (`etc/mac/sdr_fleet_policy.json`) is the machine-readable
source of truth; this is the human wiring card. The rule that keeps it safe:
**exactly one dual-tuner RSPduo, and it runs on SDRTrunk's native SDRplay API.**
Only **3 RTLs are physically attached** to the M1 (not 4, as earlier planning
assumed) — that constraint, plus VFO (a role earlier planning missed
entirely), drove the final RTL assignment below.

| Device (serial) | Mode | Role | Fixed / arbitrated |
|---|---|---|---|
| **RSP-A** 180903EF32 | **Dual-tuner** | **SDRTrunk P25** — T1 = MTRTRS (ships first), T2 = 2nd system (Will names it) | fixed |
| **RSP-B** 1809063632 | ST, tuner 1 | chirp **airband** — **LIVE** on `/ANALOG.mp3` | fixed |
| **RTL** 61108285 (stable) | — | chirp **ground** NFM — **LIVE** on `/ANALOG_GROUND.mp3` | fixed, 24/7 |
| **RTL** 83241970 (Blog V4, best RF) | — | **VFO** (`scripts/vfo.py`, manual tune + mini-waterfall → `/VFO.mp3`) | fixed |
| **RTL** 56919602 (reliability TBD) | — | **sounding** (ACARS/VDL2/radiosonde), dedicated, unshared | fixed |
| *(none)* | — | **waterfall** (`/sb5` Live IQ) — **DEFERRED**, not a priority per Will (2026-07-06); no dongle allocated until a 4th RTL arrives | pending hardware |

Both analog bands are **confirmed running** as broker-arbitrated launchd
services (`com.scannerproject.chirp-airband`/`-ground`), verified end-to-end
including clean-shutdown + anti-churn self-heal (§6 build status).

**Digital capacity:** RSP-A dual-tuner = **2 P25 systems simultaneously** (one per
~2 MHz tuner, native SDRplay API — the most reliable digital path on macOS). MTRTRS
ships first on tuner 1; the 2nd system slots onto tuner 2 with no hardware change
(SDRTrunk restart to add). **Growth to a 3rd system** needs a **4th physical RTL**
(none of the 3 attached are spare — see below); the growth mechanism itself
(heterogeneous tuners in one SDRTrunk instance, broker-leased on demand) is
unchanged from the original design. Note the next RTL to arrive has two
competing claims on it (a 3rd P25 system, or un-deferring waterfall) — whoever
gets there first is a call for whenever that RTL actually shows up, not now.

**What still holds by construction:** 0x6bed needs *two* concurrent dual-tuner
RSPduos through one apiService — we run exactly one (RSP-B is ST), so it stays
unreachable. And the ~2 h MA/SL RfUpdateError retune-wedge was an
op25 + SoapySDRPlay3 `mode=MA/SL` phenomenon; SDRTrunk's native dual-tuner is a
vendor-supported mechanism with none of op25's cross-process retune contention,
so that wedge class is gone too.

**The VFO gap (found and closed, 2026-07-06):** earlier planning allocated all
3 RTLs to ground/sounding/waterfall and never accounted for VFO — a genuinely
separate feature (manually-tunable live receiver + its own mini-waterfall,
`scripts/vfo.py`) that on micro had its own dedicated dongle. Will's call, once
the gap surfaced: **VFO reclaims the RTL-SDR Blog V4 (83241970)**, restoring
its original micro-era assignment (chosen there specifically for the Blog V4's
better front-end/SNR — it moved 80000003 → 61108285 → 83241970 over micro's
life for exactly that reason). **Waterfall is deferred** (not a priority per
Will, same conversation) — it has no dongle allocated at all right now, not
even shared; the remaining RTL (56919602) is sounding's, dedicated. When a 4th
RTL arrives, give it to whichever of {waterfall, 3rd-P25} is the priority then;
this doc doesn't need to pre-decide it. "Sounding" is Will's 2026-07-06 name
for the ACARS + VDL2 + radiosonde decoder family (atmospheric-sounding data —
aircraft weather reports plus weather-balloon telemetry; ACARS/VDL2 share the
VHF aviation band and can plausibly run off one wideband capture, radiosonde
is a different UHF band and needs its own dongle or a scheduled time-share).
Sounding itself is not broker-integrated yet (the decoder scripts predate the
broker) — wiring the claim-by-serial call in is a prerequisite before it goes
live on this box.

**Open (only Will can answer):** the 2nd P25 system's name (for tuner 2), and —
before either goes live — a one-time SDRangel span check that each system's
monitored *site* fits ~2 MHz (a single trunked site normally does; statewide
spread is irrelevant since you monitor one site at a time).

**Physical (M1: 2× Thunderbolt + 2× USB-A):** each RSPduo on its own Thunderbolt
port (own controller — the dual-tuner RSP-A especially wants the bandwidth); the
3 RTLs on a powered hub (own PSU) on USB-A. Verify controller grouping on the box
with `system_profiler SPUSBDataType` during SB7.1 — if the two USB-A ports share
one controller, consider a second hub to split the RTLs.

### 4.1b Multi-system digital — REVISED for macOS: SDRTrunk is the engine

**op25 is ruled out on macOS** (researched 2026-07-04): no macOS track record at
all (zero macOS issues in boatbod's tracker; last build attempt 2017), audio
egress is ALSA/Pulse-only, and the SDRplay-via-Soapy chain it would ride has
documented reboot-class failures on macOS. With the box on macOS (§4.3), op25 —
and the entire op25_adapter/ensure-op25-runtime generation path — retires with
micro. (For the record, that path also carried a live latent bug: it emits MA/SL
device args with **no bandwidth set** — scripts/ensure-op25-runtime.py:148 — the
exact aliased-noise config that was the airband root cause, and a plausible
explanation for MTRTRS's chronically weak decode.)

**Digital = SDRTrunk on the box with RSP-A, native SDRplay API.** Verified
2026-07-04: official **osx-x86_64** builds with a bundled JDK (v0.6.1 stable +
active nightlies; the SDRplay dylib-name bug was fixed in 0.6.0), **native
RSPduo dual-tuner support** (two independent ~2 MHz tuners, per-channel
Preferred Tuner pinning), and native Icecast source streaming (continuous
connection, **queued per-call audio** — delayed/serialized, not live).
Because SDRTrunk talks to the SDRplay API directly, it skips the
Soapy/gr-osmosdr chain entirely — on macOS that makes it the *most* proven
RSPduo path, not the fallback (Will already runs it against the RSPduo today).

The plan (updated 2026-07-05 per Will's "≥2 digital systems" requirement —
RSP-A ships **Dual Tuner from launch**, not gated behind the alert stack):
- **Ship first (SB7.5):** SDRTrunk, RSP-A Dual Tuner, **MTRTRS on tuner 1**. The
  2nd system is configured on tuner 2 as soon as Will names it — no hardware
  change, an SDRTrunk restart to add. (Dual-from-launch is safe here: the
  dual-tuner risk that argued for a later gate was op25's cross-process retune
  contention; SDRTrunk manages both tuners natively in one process with none of
  that. Single host keeps the 0x6bed invariant — RSP-B stays ST.)
- **Grow to 3:** add the stable DIGITAL-FLEX RTL (70613472) to the same SDRTrunk
  instance, broker-leased on demand.
- **Constraint to verify once:** each system's monitored *site* must fit a
  tuner's ~2 MHz (~1.5 usable) window — check spans in SDRangel before going
  live (a single trunked site normally fits easily; you monitor one site at a
  time, so statewide spread doesn't matter). Mode/system changes need an
  SDRTrunk restart.
- **Known SDRTrunk gaps we own:** no native tuner-crash recovery (open issue
  #1890) → launchd `KeepAlive` + an alert rule on stream/call cadence;
  alias-list maintenance (unaliased talkgroups silently don't stream) → a
  wildcard catch-all alias per system + a CI check in the playlist generator;
  call-queued audio texture on DIGITAL.mp3 (calls serialize; a Delay +
  max-recording-age govern staleness).

Integration notes: hits/calls for the UI come from SDRTrunk's event/recording
logs instead of op25's JSONL (new, small adapter); the op25-audio-bridge
retires (SDRTrunk streams to Icecast itself).

### 4.2 One box — DECIDED 2026-07-04: single-host deployment, no witness box

Will's call: no second-computer witness; the whole product runs on one Mac mini.

**Host recommendation: the M1 mini, not the Intel.** Will asked "unless it's
easier on the M1" — it is, on the evidence already gathered:
- **Every Mac-side SDR verification so far happened on Apple Silicon + macOS 26**
  (SDRplay API 3.15, SDRangel + RSPduo end-to-end scan 2026-06-21, SDRTrunk +
  RSPduo Sugar Tree) — directly transferable to the M1, none of it to Intel.
- **Years of runway vs. a fall-2027 wall.** The M1 runs macOS 26 today and stays
  supported for years; the 2018 Intel mini caps at Sequoia with security updates
  ending ~fall 2027 and Homebrew sunsetting Intel on the same clock.
- **arm64 is the first-class platform** for Homebrew, SDRTrunk (osx-aarch64
  builds), and radioconda (osx-arm64) alike.
- Faster CPU than the i5-8500B, near-silent, and a fraction of the 24/7 power.

**What the Intel gave up, managed:** (a) *RAM* — 8 GB vs 16 GB. Budget: chirp ×2
(~1–2 GB), SDRTrunk JVM (capped ~1.5 GB), icecast/UI/waterfall (~1 GB),
monitoring (~1 GB) ≈ 5–6 GB. Workable; sounding decoders become opt-in, and the pool
caps stay repo-enforced. (b) *Ports* — 2× TB + 2× USB-A: RSPduo-A → TB1,
RSPduo-B → TB2 (isolated buses, per fleet policy), 4 RTLs on one powered hub on
USB-A (≈40 MB/s aggregate, fine on USB 3). (c) One M1-specific caveat: SDRTrunk's
README warns Tahoe's USB changes can require the nightly build + `libusb --HEAD`
for **RTL** dongles; the SDRplay path (native API) is unaffected, and Will's
2026-06-21 run on macOS 26.5.1 worked — verify during SB7.5.

**The Intel mini becomes the cold spare / bench box** (SDRangel scan rig,
staging for risky upgrades, and the rollback target if the M1 dies). Same
bootstrap provisions it, so promotion is a restore script + cable move.

One-host honesty note: chirp (via SoapySDR) and SDRTrunk (native API) now share
the single `sdrplay_apiService` again, so digital device churn can in principle
touch analog. Containment: exactly one dual-tuner device max (the fleet
invariant, broker-refused), SDRTrunk launches once and stays up (no restart
churn by design), and broker min-restart-interval rate-limits everything else.

### 4.3 Operating system — macOS (unchanged), now on the M1

The macOS-not-Ubuntu call stands and gets simpler on the M1: current macOS,
first-class Homebrew, no Intel sunset clock. (For the record, the original fan
rationale for avoiding Ubuntu-on-Intel was outdated — `t2fanrd` is maintained
and tested on the Macmini8,1, and bare T2 firmware ramps fans autonomously,
worst case throttling — but the single-box M1 plan makes the point moot.)

- **GR stack:** radioconda/conda-forge **osx-arm64** (prebuilt gnuradio 3.10.12,
  gr-soapy in-tree, gnuradio-osmosdr, SoapySDR) — brew's gnuradio formula is
  deprecated (Qt5 EOL) on all arches, so conda is the durable choice either way.
  SoapySDRPlay3 self-built against the universal SDRplay 3.15 API.
- **Known risk to gate:** SDRplay-via-Soapy on Apple Silicon has scattered
  reports of API-service failures (SDR++/SDRangel issues), though Will's own
  GUI sessions ran clean. Headless long-term operation is undocumented territory
  on any Mac → the SB7.1 48 h go/no-go spike runs **on the M1** before any
  hardware migrates. Fallback if it fails: airband on an RTL while we
  re-evaluate (SDRTrunk's native-API path is unaffected either way).
- **Appliance conventions** (verified): auto-login with FileVault OFF (headless
  reboot otherwise stalls at pre-boot unlock), `pmset autorestart 1` +
  never-sleep, HDMI dummy plug for GUI apps over Screen Sharing, and services as
  **LaunchAgents in the logged-in user session, not LaunchDaemons** — USB SDR
  access needs a user session. This amends docs/mac-mini-port.md §3's
  boot-LaunchDaemon design.

Consequence: the mac-mini-port milestones become the critical path — M2
(ServiceBackend/launchd), M3 (supervisor), M4 (JSONL hits + no-op /sys walks),
M5 (audio) are SB7 deliverables. With op25 off the table on macOS (§4.1b),
**SDRTrunk is the digital engine**.

### 4.4 Radio data layer: RadioReference (ADDED 2026-07-04 — Will has a premium account)

The repo already has a location-driven data layer built on the **HomePatrol
database** (data/homepatrol.db + ui/hp_scan_pool.py, hp_favorites_wizard,
travel-mode ZIP push → scan pool). Its weakness: the HPDB was a one-time image
from a Uniden (HPCOPY.zip) — static, aging, and hand-supplemented (Sugar Tree
CSVs, SIC configs, MTEARS lists were all manually curated). A premium
RadioReference account upgrades this into a **live, refreshable data layer** — but
the mechanics matter, so here is what's actually true (verified 2026-07-04):

**The API is SOAP "Web Service 3.1", schema v18, endpoint
`https://api.radioreference.com/soap2` (WSDL `?wsdl&v=latest`, confirmed live).**
Auth per call = the member's premium username/password **plus a free per-app
`appKey`** we request at radioreference.com/account/api. No REST DB API exists;
rate limits are unpublished (→ cache aggressively). `zeep` is the right client
base; no maintained PyPI wrapper exists (~100 lines of our own).

**Correction to earlier assumptions:**
- **The HPDB does NOT come from RR** — it's Uniden's data, delivered by Uniden's
  free Sentinel app, no RR premium needed. So "refresh the HPDB from RR" is off
  the table. Two clean options instead: (a) run **Sentinel** periodically to
  refresh data/homepatrol.db through the existing hpdb pipeline, or (b) **build
  our HPDB-shaped SQLite directly from the RR SOAP API** (`getZipcodeInfo` →
  county → `getCountyFreqsByTag` / `getTrsSites` / `getTrsTalkgroups`), which
  keeps the whole scan-pool/favorites/travel-mode stack unchanged but sourced
  from RR. (b) is the better fit — one credential, scriptable, same schema.
- **SDRTrunk's RR import does NOT solve the alias/streaming problem by itself.**
  It's a genuine win — its Playlist Editor pulls sites, control channels, P25
  WACN/NAC, and "Import All Talkgroups" auto-creates aliases from RR alpha tags,
  needing only Will's username/password (SDRTrunk ships its own embedded appKey).
  **But the import is a one-shot snapshot with no refresh** — talkgroups RR adds
  later stay unaliased (hence unstreamed), and re-importing into an existing
  alias list silently fails (issue #2091) and loses per-alias Stream/Record
  settings. So the plan holds but the reason changes: we keep the wildcard
  catch-all alias per system as the streaming safety net **and** own a periodic
  playlist-refresh path — SDRTrunk will not self-update.

**What we build:**
1. **`rr/` client module (new).** Thin `zeep` wrapper over the v3.1 API — auth
   from env/Keychain (NEVER committed; our own appKey), on-disk response cache
   with TTL (appliance works offline; unpublished rate limits become moot).
   Methods we need: `getZipcodeInfo`, `getCountyFreqsByTag`, `getTrsDetails`,
   `getTrsSites`, `getTrsTalkgroups`, `getUserData` (sub-expiry health check).
2. **HPDB-from-RR builder** feeding the existing pipeline → analog favorites,
   scan pool, and travel-mode for arbitrary ZIPs, on current data.
3. **SDRTrunk playlist generator/refresher** (SB7.5): generate the playlist XML
   from RR data (sites/CC/NAC + aliases), or drive SDRTrunk's import and then
   own the refresh cadence; wildcard catch-all alias per system stays.
4. **Refresh discipline, north-star style:** pulls are periodic + manual, always
   into a *staging* copy, validated (schema + non-empty + sanity diff vs
   last-good) before an atomic swap via the SB7.6 live-config machinery. A failed
   refresh keeps last-good and raises an alert — never a half-written database.

## 5. Alerting without a witness box (descoped by PO call, 2026-07-04)

The second-computer witness is out. What survives on the box itself — this is
most of the value, and it's cheap:

- **The prober runs locally.** Same checks as designed: pull each mount
  (ANALOG.mp3, ANALOG_GROUND.mp3, DIGITAL.mp3) every N minutes, decode the MP3,
  assert byte rate ≥ 0.8× bitrate **and** non-silence RMS (catches
  lame-encodes-zeros, the probe_rate wedge, the sourceless-mount wedge, the
  consumer wedge). Prometheus + Alertmanager + Grafana run on the box too
  (~1 GB, retention trimmed).
- **Phone push stays.** Alertmanager → ntfy/Pushover: critical (no audio 5 min,
  daemon down, config hard-fail) pages; warnings digest. Every SB6 lies-healthy
  incident was a *daemon*-level lie on a healthy box — on-box alerting catches
  that entire class.
- **What's genuinely lost:** whole-box death (kernel panic, PSU, USB-controller
  wedge, network drop) pages nobody — the box can't report its own corpse.
  *Optional 15-minute mitigation, no second computer:* a dead-man's-switch —
  the box curls healthchecks.io (free) every 5 min; the service pushes to
  Will's phone when pings stop. Listed as a checkbox in SB7.4, Will's call.
- **Chaos-drill referee** (SB7.8) becomes the on-box alert stack + Will's phone;
  box-kill drills are verified manually.

## 6. Software program

Each phase ships behind a flag/gate as in SB6; every phase ends with the
end-to-end audio gate (mount byte rate ≥ 0.8× bitrate, both bands, 1 min under
realistic load) — now automated by the on-box prober instead of measured by hand.

### Build status — first code tranche landed 2026-07-05

Committed on `mac-mini-port` (hardware-free work, verified on a Mac dev laptop;
the real gate is the on-box SB7.1 spike). Zero regressions across the pre-existing
suite (baseline-diffed); each piece's own tests green:

- **SB7.0 done:** fleet policy v2 (single-host, 4-RTL, SDRTrunk-digital); VNC
  password scrubbed to an env var; root `conftest.py` so the whole suite runs
  under one `--import-mode=importlib` command (the CI invocation).
- **SB7.2 ServiceBackend (C):** `ui/service_backend.py` — `ServiceBackend` ABC +
  `SystemdBackend` (verbatim current behavior) + `LaunchdBackend`, dispatched by
  `SCANNER_SERVICE_BACKEND`. The ~11 `ui/systemd.py` primitives are now shims over
  it. **P0 #4 fixed** (UNITS ghost names → `gr-demod@airband/ground`, in config +
  the `restart_rtl` NameError + the actions.py reset-radios literals). **P0 #6
  fixed** (sdrplay restart mutex). 133 + 30 tests green.
- **SB7.2 tuner broker (D):** `broker/` — connection-bound leases (socket = lease,
  crash = auto-release), dual-tuner refusal (0x6bed guard), open-gap + anti-churn
  serialization, hot-spare-via-role, `broker denial-with-reason` (retires **P0 #5**
  silent-empty resolver). CLI `run`-wrapper is how SDRTrunk claims RSP-A. 107 tests.
- **SB7.3 IcecastSink (E):** the 6/18 "lies healthy" wedge killed — root cause was
  GR's block-executor calling `stop()` on flowgraph wind-down. Intent flag +
  supervised publish loop + `on_fatal`→exit-code-4 + truthful snapshot/metrics.
  18 tests. (Full SB7.3 net-arming — validators on-by-default, handlers error
  surfacing — still to do.)
- **SB7.1 tooling (F):** `mac-bootstrap.sh` radioconda `--gr` path, `mac-appliance
  -setup.sh`, and the 48 h `mac-spike-chirp-soak.sh` gate. `bash -n` + dry-run OK.

**launchd convention (C↔D seam, binding for the M3 plists):** managed service
units are **KeepAlive=true**, and `LaunchdBackend.stop()` uses `launchctl bootout`
(a bare SIGTERM would let KeepAlive respawn the process and silently defeat the
stop-all→sdrplay-bounce→ordered-start recovery sequence); `start()` re-bootstraps
from `~/Library/LaunchAgents/<label>.plist`. The tuner-broker's own plist is
KeepAlive=true for the same reason. When the gr-demod/icecast/SDRTrunk plists are
authored (M3), they follow this convention.

Not yet built (next): SB7.4 alert stack, SB7.5 SDRTrunk+RR digital, SB7.6 live
config swap + GitHub Actions CI, the SB7.3 remainder, and the `rr/` client (§4.4).

**SB7.0 — Decisions + fleet policy v2 (1 day).**
Host + OS are decided (§4.2/4.3: single M1 mini, macOS). Will still picks:
RTL #4 role, and the second-P25 scope (which two systems, once SDRTrunk goes
dual-tuner). Rewrite `sdr_fleet_policy.json` for the single-host, 4-RTL world. Scrub the hardcoded VNC
password; rotate it.

**SB7.1 — Foundation + the chirp-stack spike (4–6 days + hardware moves).**
Provision the M1 mini: clean macOS, auto-login (FileVault off), pmset
never-sleep + autorestart, HDMI dummy plug, Tailscale, radioconda osx-arm64 +
SoapySDRPlay3 build + SDRplay 3.15 API, brew leaf tools, `/opt/scannerproject`
layout from the existing env file. **Go/no-go spike, before any hardware
migrates:** chirp's flowgraph against RSP-B via conda GR + SoapySDRPlay3 on this
box, 48 h soak with the audio probe on — this is the least-proven link on macOS
(no documented long-term operators) and the whole plan pivots on it. Fallback if
it fails: airband on an RTL while we re-evaluate, or revisit the OS call with
data in hand. In parallel: rtl_eeprom-flash real serials on all 4 RTLs (kills
the serial-collision class forever); migrate the runtime-state set from micro
(digital profiles, dongle assignments, managed controls, favorites) into the
**one state home** `/opt/scannerproject/var` with a repo-committed manifest +
restore script. Micro stays warm as rollback until SB7.8 passes.

**SB7.2 — Service backend + tuner broker (5–6 days).**
The mac-mini-port M2/M3 milestones, now on the critical path: extract the ~10
systemctl primitives behind a `ServiceBackend`, implement the launchd/LaunchAgent
backend (dispatch on `SCANNER_SERVICE_BACKEND`, currently declared-but-unread),
replace the `journalctl -k` segfault probe with stats-freshness + `log show`.
Then the tuner broker as designed in fleet-policy v1: AF_UNIX CLAIM/GRANT, one
flock lease per physical serial (auto-release on crash), open-gap +
min-restart-interval enforcement, dual-tuner invariant refusal (max 1 per host),
hot-spare promotion for a failed RTL. Headless unit tests for every invariant.
All claimants (chirp ×2, SDRTrunk launcher, waterfall, sounding, disco) go through it;
the silent-empty exclusion resolver (open P0 #5) is deleted, replaced by broker
denial-with-reason.

**SB7.3 — Arm the nets + kill the lies (4–5 days).**
- Calibrate the source-validator envelope on live hardware; flip
  `CHIRP_SOURCE_VALIDATE=1` **on by default**; CI-guard the default.
- Flip `CHIRP_AUDIO_PROBE_ENABLED=1` on **both** bands (ground never had it).
- IcecastSink publish loop: auto-reconnect with backoff on transient source error;
  if it cannot re-establish, **structured daemon exit** (no third state). Same
  treatment for the "icecast init fails → silently write to file" fallback.
- Fix UNITS ghost names (open P0 #4); add serialize-restarts mutex (open P0 #6);
  state-file CRC + refuse-to-boot-on-corrupt (with explicit reset flag).
- ui/handlers: stop swallowing icecast/status errors; surface them in heartbeat.

**SB7.4 — Alert stack live (2–3 days).**
§5 in full: prober + Alertmanager + phone push + Grafana dashboards for chirp
and the digital chain (the audit found none exist). Gate: kill -STOP a chirp
daemon and pull a dongle; Will's phone must buzz twice within 5 minutes.

**SB7.5 — Digital restored (SDRTrunk) + local audio (3–5 days).**
SDRTrunk on the box, RSP-A Single Tuner, one P25 system (ladder D3; Dual Tuner D1
follows once the alert stack is proven): LaunchAgent with KeepAlive, playlist
generated/versioned in the repo (wildcard catch-all alias per system), streaming
to the local Icecast DIGITAL mount, a small adapter tailing SDRTrunk's
event/call logs into the UI hit feed (replaces op25 JSONL; op25-audio-bridge
retires). Local sound: the box plays the mounts through speaker/BT via
CoreAudio (mute/volume = commands to a small local player agent, replacing the
wpctl/PipeWire gremlins and the BT-volume-reset bug for good). The UI's
band-mute buttons drive that agent.

**SB7.6 — Live config swap + CI (4–5 days).**
Rebuild Phase 4 as spec'd: inotify (or kqueue) watch on the state files, validate,
atomic in-place channel swap at a scheduler boundary. Result: profile/favorite
changes no longer restart daemons → the wedge-triggering operation becomes rare.
Stand up GitHub Actions: run all 124+ tests, the systemd-arithmetic lint (finally
real), a ghost-unit-name scan, schema-single-parser checks, and flag-default
guards (validators must stay on).

**SB7.7 — Scheduler + AGC debt (2–3 days, deliberately after the alert stack).**
AGC per-dwell re-baseline (close the latch class). LO scheduler: keep wall-clock
*but* the prober now alerts on hop-cadence drift; ship the stream-tag rewrite
only if soak data shows real drift. (Spending a week on stream tags before any
external drift detection exists would repeat SB6's ordering mistake.)

**SB7.8 — Soak + chaos (7–10 days elapsed, low-touch).**
The gate SB6 never ran, unchanged from the rebuild scope, refereed by the
on-box alert stack + Will's phone (box-kill drills verified manually):
7-day untouched soak; chaos drills — kill apiService mid-run, USB-pull the RSPduo
and each RTL, corrupt a state file mid-write, skew the clock, mass mute/unmute
from the UI, drop the network, power-cycle the box. Every fault must end in
recovery-or-clean-stop **and** a page. Then micro is decommissioned.

Rough calendar: **4–5 weeks**, comparable to the original rebuild estimate — but
this time the first two weeks already deliver product (digital back, alerts on).

## 7. Success criteria (the product bar)

1. 7-day untouched soak: zero manual interventions, zero third-state events
   (alert-stack-verified).
2. Airband, ground, **and P25 digital** audible locally and on mounts ≥ 95% of the
   soak window at ≥ 0.8× bitrate with non-silent decoded audio.
3. Every chaos fault → page on Will's phone ≤ 5 min + recovery or clean stop.
4. A favorite/profile/squelch change takes effect with **zero daemon restarts**.
5. The safe-restart ritual and the MA/SL runbook sections are deleted — not
   documented better; *deleted*, because the states they recover from are gone.
6. CI green on every merge, with the armed-by-default flags guarded.

## 8. What SB7 keeps from SB6 (explicitly)

Telemetry-first design and the /metrics schema; hard-fail config with dataclass
defaults; pydantic single-schema state; the probe_rate watchdog (now armed); the
global-squelch UX; split-process per-tuner daemons; the IQ capture/replay
diagnostic harness; the deploy-log discipline. SB6's engineering was largely
sound — SB7's job is to arm it, alert on it, and remove the two structural RF
failure classes the old hardware forced us to live with.

## 9. Risks

| Risk | Mitigation |
|---|---|
| chirp's GR+Soapy+SDRplay chain unproven headless on Intel macOS | SB7.1 go/no-go spike (48 h soak, audio probe armed) **before** hardware migrates; micro stays warm as rollback until SB7.8 passes; fallback = airband-on-RTL or revisit OS with data |
| SDRplay-via-Soapy on Apple Silicon: scattered API-service failure reports | SB7.1 spike gates it; SDRTrunk (native API) unaffected; Intel mini is the warm fallback host |
| SDRTrunk has no native tuner-crash recovery (issue #1890) | LaunchAgent KeepAlive + alert rule on call/stream cadence |
| SDRTrunk alias gap = talkgroups silently not streamed | wildcard catch-all alias per system, CI check in the playlist generator |
| Dual-tuner window: a system's channels may exceed ~2 MHz/tuner | verify spans in SDRangel before D1; fall back to two ST boxes/one system |
| DIGITAL.mp3 becomes queued call audio, not live | accepted texture change (Broadcastify-style); Delay + max-age tuned in SB7.5 |
| 8 GB on the M1 | budget in §4.2 (~5–6 GB); sounding decoders opt-in; JVM heap capped; monitoring retention trimmed |
| Whole-box death pages nobody (no witness) | accepted by PO; optional healthchecks.io dead-man ping (SB7.4 checkbox) |
| Scope creep re-opening the scheduler rewrite early | SB7.7 explicitly gated on prober drift data |

## 10. What I need from Will

1. Confirm the host: M1 mini as the one box (recommended; Intel becomes cold
   spare) — or say the word and everything lands on the Intel instead.
2. The remaining SB7.0 decisions: RTL #4 role; which P25 system ships first in
   D3 (and which pair in D1).
3. Confirmation the 4 physical RTLs on hand match (or replace) the micro pool, so
   SB7.1 can flash serials.
4. Anything missed or wrong.
