# SB3 on Neptune — Architecture Plan

**Author:** Claude (research pass), for Will Minkoff (PO)
**Date:** 2026-07-16
**Status:** DESIGN / RESEARCH. Nothing built, nothing deployed. No box was touched to write this.
**Branch:** `sb3-neptune-plan` (off `origin/main` @ `e2dbb48`)

**Method note.** This is a repo-only pass. No REST calls were made against SDRangel or
SDRTrunk on Neptune, Venus, or BreakroomDe; no box state was read or changed. Every claim
below is sourced to a file, a commit, or the brief. Where the repo contradicts itself — and
it does, in three load-bearing places — the contradiction is flagged rather than resolved by
guessing. Those flags are the Phase 0 worklist.

---

## 0. The thesis, in one page

Will wants Neptune (M1 Mac mini, macOS) to become a full SB3 host: four SDRs, dual
concurrent P25, airband, a ground role, disco/ACARS/survey, a web UI, icecast per-role
mounts, and profiles — with **SDRangel + SDRTrunk as the backend** rather than a Mac port of
chirp/op25.

That decision is right, and it is bigger than it looks. It changes what SB3 *is*:

> **SB3 stops being a radio and becomes a control plane.**

Today SB3 owns the samples. `chirp` opens the SDR, runs a GNU Radio flowgraph, demodulates,
squelches, encodes MP3, and pushes it to icecast — all in one process it fully controls
(§2). Tomorrow SB3 owns none of that. SDRangel owns the analog samples; SDRTrunk owns the
digital ones. SB3's job becomes: **translate a profile into backend configuration, verify the
backend actually did it, and get out of the way.**

That reframing drives everything else in this doc, and it has one sharp consequence that
deserves to be stated before the details:

> **SB3 must be a strictly optional component.** Everything that produces audio must keep
> producing audio when SB3 is dead. This is not a nice-to-have kill-switch feature bolted on
> at Phase 5 — it is an architectural constraint that has to be true from Phase 1, because it
> is the only thing that makes the whole plan safe to attempt on a box Will actually listens
> to.

The good news: the repo is already most of the way there, and doesn't know it. The
Neptune/Venus audio harness on `origin/main` (`macos/bin/`, §3.1) is *already* an
SB3-shaped orchestrator — host-aware, idempotent, REST-driven, self-healing, and completely
separable from the apps it drives. It is 400 lines of shell and Python that nobody called
"SB3." The plan below is largely: **generalize what already works, and delete what can't
follow us to macOS.**

The bad news is concentrated in one place, and it is worth knowing now rather than at
Phase 3: **SDRangel's FreqScanner frequency list cannot be set over REST** in the version
in play (§7.1). Scanning — the thing a scanner does — is the one control surface the REST
API does not expose. Every other risk in this document is a scheduling problem. That one is
a design problem, and it is the reason §3.4 proposes hunt-mode be built out of multiplexed
demods rather than FreqScanner.

---

## 1. Repo inventory of SB3

### 1.1 The branch situation (resolve this first — it is not what it looks like)

```
git merge-base origin/main mac-mini-port  = a725133
git log origin/main..mac-mini-port | wc -l = 0      # mac-mini-port is 0 AHEAD
git log mac-mini-port..origin/main | wc -l = 45     # and 45 BEHIND
```

**`mac-mini-port` is fully merged into `origin/main` and is a strict ancestor of it.** The
merge commits are explicit: `ea41b18 Merge branch 'mac-mini-port'` and `5c261f0 Merge
origin/mac-mini-port: SB7.1-SB7.7 provisioning + VFO wiring`.

This matters because the brief was written from a `mac-mini-port` checkout, where
`macos/bin/`, `etc/mac/launchd/neptune/`, and `etc/mac/sdrtrunk/philadelphia-p25.xml` **do
not exist**. All the Neptune/Venus SDRangel work is main-only. A grep for `neptune-angel.mp3`
on `mac-mini-port` returns nothing but false positives from the HomePatrol database dumps
(`assets/HPDB/New Jersey.hpd:2462` has a municipality called Neptune).

**→ All SB3-on-Neptune work happens off `origin/main`. There is nothing to merge back from
`mac-mini-port`.** This doc's branch is cut from `origin/main` accordingly.

### 1.2 Component-by-component portability

| Component | LOC/size | Verdict | Why |
|---|---|---|---|
| `broker/` (server, client, leases, policy) | ~1.5k | ✅ **Already macOS-native** | Pure stdlib: AF_UNIX, `fcntl.flock`, `os.replace`. `leases.py:583` names APFS explicitly. Plist exists + is complete. **`git diff mac-mini-port origin/main -- broker/` = zero files changed.** This is the single best asset in the repo for the new plan. |
| `macos/bin/` (SDRangel harness) | ~400 | ✅ **Live and proven** | Host-aware, idempotent, REST-driven, self-healing. Running on both minis today. §3.1. |
| `ui/service_backend.py` | ~350 | ✅ **Ported** | `ServiceBackend` ABC; `LaunchdBackend` does `launchctl kickstart -k` / `bootout` / `print`. Dispatch on `SCANNER_SERVICE_BACKEND=launchd`. |
| `ui/chirp_client.py`, `chirp_adapter.py` | ~600 | ⚠️ **Portable but pointless** | Stdlib-only UDP by design — but they talk to chirp, which retires. Dead on arrival. |
| `chirp/` (GNU Radio analog engine) | ~8k | ❌ **RETIRING** (Will's call) | Runs on macOS via radioconda, but Will's decision is not to port it. §2.1. |
| `disco/` (sweep → classify) | ~3k | ❌ **Linux-only today** | Two hard blockers: `/run/*` paths everywhere (`sweep.py:20-21`, `classifier.py:136`), and `disco/src/listen.py:124` `SYSTEMCTL_BIN = "/bin/systemctl"`. Env-overridable but nothing in `etc/mac/` sets them; no launchd plist exists. §3.6. |
| ACARS / VDL2 | — | ❌ **Linux-only** | systemd units only (`systemd/acarsdec.service`, `dumpvdl2.service`). Write JSON to `/run/`, UI tails it. No launchd plist. §3.7. |
| `ui/` (airband-ui) | **10,175 lines in one handler class** | ❌ **Do not port** | §1.5. |
| `ui/band_mute.py`, `audio_leveler.py`, `vlc.py` | ~1k | ❌ **Linux-only** | `wpctl`, `pactl`, `$XDG_RUNTIME_DIR/pipewire-0`. `ui/handlers.py:9744` hardcodes `alsa_output.pci-0000_00_1f.3.analog-stereo`. |
| `ui/system_stats.py`, `reliability.py` | ~1k | ❌ **Linux-only** | `/sys/bus/usb/devices`, `/sys/class/{hwmon,thermal,net}`. `ui/actions.py:104` hardcodes `/dev/bus/usb/003/002`. |
| `systemd/` (36 units) | — | ❌ **Retires** | Replaced by launchd. |
| `op25` + `ui/op25_adapter.py` | 3,570 | ❌ **Retired** | `sdr_fleet_policy.json:2`: "op25 is retired (no macOS path)". Researched 2026-07-04: zero macOS track record, ALSA/Pulse-only egress. |
| `scripts/tuner_broker.py` | — | ❌ **Retired, and confusingly named** | Unrelated to `broker/`. Polls `/run/scannerproject/broker/mode.json`, swaps *systemd units*. Different schema, different job. Delete it to stop the name collision. |
| `scripts/ensure-digital-runtime.py` | 1,169 | ⚠️ **Generalize, don't rewrite** | A full SDRTrunk playlist-synthesis engine: `_sync_source_configuration()`, `_sync_alias_broadcast_channels()`, `_sync_stream_configuration()`, `_preferred_tuner_target()`. Written for RTL dongles, but this is exactly the machinery the dual-RSPduo-tuner plan needs. §3.5. |

**The headline:** the two things worth keeping are the ones nobody planned as the future —
`broker/` and `macos/bin/`. The 10k-line UI and the 8k-line DSP engine both retire.

### 1.3 What a "profile" looks like in code — the honest answer

The brief asks to "confirm profiles handle both hunt-mode (chirp) and camp-mode (fixed-freq)
airband." I can't confirm that, because **it isn't true, and the terms don't exist.**

`grep -riE "hunt|camp"` across all `.py/.json/.md/.html` returns zero hits (only
"Huntingdon" and "Ft. Campbell" as radio labels). More importantly:

> **chirp has no profile concept at all.** Its channel list is not in any config file. It is
> pushed at runtime over the UDP command bus (`add_channel`) and persisted to
> `state.json`. There is no file you can read to learn what chirp is listening to.

There are **four** unrelated things in this repo wearing the word "profile," and only one of
them is close to what Will means:

**(a) `chirp/config/{airband,ground,defaults}.json` — per-band daemon tunables. Not a profile.**
One file per band, selected by `CHIRP_BAND`. Precedence is env > json > dataclass default
(`daemon.py:371-383`). Flat scalars plus one nested `sdr` block:

```jsonc
// chirp/config/airband.json — the real thing, abridged
{ "version": 1, "band": "airband", "pool_mode": "am",
  "cmd_port": 7400, "source_samp_rate": 2000000.0, "audio_rate": 16000.0,
  "am_agc_enabled": false, "am_fixed_gain": 10.0,
  "lo_dwell_sec": 3.0, "scan_hold_enabled": true, "scan_hold_hang_sec": 2.0,
  "scan_hold_max_sec": 30.0, "priority_gate_enabled": true,
  "global_squelch_dbfs": -56.0,
  "sdr": { "device_args": "soapy=,driver=sdrplay,serial=1809063632,rspduo_mode=ST,rspduo_tuner=1",
           "center_freq_hz": 127500000.0, "gain_db": 32.8, "gain_mode_auto": false,
           "bandwidth_hz": 1536000.0, "antenna": "Tuner 1 50 ohm" } }
```

Note: **no frequency list.** That is the point.

**(b) `profiles/profile_metadata.json` — a systemd unit teardown contract.** Schema:

```jsonc
{ "schema_version": 1,
  "profiles": { "<id>": { "requires_stop": ["unit..."], "starts": ["unit..."],
                          "claims_serials": ["serial..."] } } }
```

Six ids (`acars`, `radiosonde`, `hp3_favorites_airband`, …). It says **nothing about
frequencies** — it is pure service orchestration for the retired rtl-airband era. The
frequency content lived in `.conf` files under `/usr/local/etc/airband-profiles`, merged by
`combined_config.py` into libconfig format.

**(c) `profiles/trunking/p25_profile.json` — a 9-line unused template.** Placeholder control
channel `451.0000`, empty voice list, no loader references it. Dead.

**(d) `macos/data/analog_scanlists.json` — ⭐ the actual ancestor of the SB3 profile.**
This is the one that matters. It is the only file in the repo that describes *what to listen
to* in a backend-agnostic way, and it is already written against SDRangel:

```jsonc
{ "airband": {
    "_device": "RTL-SDR 83241970 (970 dongle), center 118.925 MHz, 2.4 Msps, AM, 8 kHz channels",
    "_mode": "AM",
    "deployed": [ {"mhz": 118.400, "label": "Nashville App/Dep East"},
                  {"mhz": 118.600, "label": "BNA Tower"}, … ],
    "candidates_from_hpdb": [ {"mhz": 124.750, "label": "Nashville Final Approach"}, … ] },
  "70cm": { "_device": "RSP-B SDRplayV3 1809063632, center 446.1 MHz, 8 Msps, bandwidthIndex 7",
            "_mode": "NFM", "simplex": [...], "repeaters": [ {"output_mhz": 442.750, "pl": 100.0, …} ] },
  "ground_reference": { "_note": "NOT currently deployed…", "MURS": [...], "FRS_GMRS_simplex": [...] } }
```

Its `_comment` field is also the single densest source of SDRangel operational truth in the
repo — see §3.2.

**→ Conclusion: the SB3 profile does not exist yet and must be designed (§3.3).**
`analog_scanlists.json` is the seed. This is a feature, not a setback: there is no legacy
profile schema to stay compatible with, because the thing Will calls a profile was never
written down.

### 1.4 Hunt mode vs camp mode — what the mechanisms are actually called

The concepts are real; the names aren't. In chirp they are:

| Will's term | Actual mechanism | Where |
|---|---|---|
| hunt / scan | **LO cluster rotation** — greedy 1-D bin-pack of channels into `iq_bw`-wide clusters, round-robin retune on a dwell | `chirp/dsp/cluster_planner.py:128`, `lo_scheduler.py:333` |
| stop-on-hit | **scan-hold** — latch the LO while squelch is open; hang 2.0 s, cap 30.0 s | `lo_scheduler.py:386-454` |
| camp / fixed | **single-cluster degenerate case** — `if len(clusters) == 1: return` and it never rotates | `lo_scheduler.py:367-369` |

So camp mode is not a mode. It is what happens when all your channels fit in one 2 MHz
window and the scheduler has nowhere to hop. **That is a genuinely good idea and SB3 should
keep it** — see §3.4, where it falls out of the SDRangel design for free, because SDRangel
demodulates every channel in the window simultaneously and "camping" is just "your window
covers everything."

The honest cost of hunt mode is documented at `chirp/SCAN_HOLD_DESIGN.md`: *"While held,
every other cluster is parked and **deaf**."* Parking works by slamming squelch to
`PARKED_SQUELCH_DBFS = 0.0` (`daemon.py:143`) — 0 dBFS, i.e. gate closed unless the input is
louder than full scale.

### 1.5 Web UI stack — and why it doesn't come with us

`ui/` is **not** Flask or FastAPI. It is raw stdlib `http.server`:

- `ui/app.py:137` — `ThreadedHTTPServer(("0.0.0.0", 5050), Handler)`, `ThreadingMixIn`.
- `ui/handlers.py` — **one `BaseHTTPRequestHandler` subclass, 10,175 lines.** Routing is a
  linear `if p == "/api/..."` chain, not a table. `do_GET` starts at line 5855; `do_POST` at
  7606.
- **No template engine.** HTML is `open()`ed and returned as bytes.
- **Three coexisting UIs**: `/sb3` (`sb3.html`, 15k+ lines), `/sb5` (current prod), `/hp3`
  (the only real SPA — React 18 imported from `https://esm.sh/react@18` at runtime, no
  bundler). `handlers.py:5880-5894` documents SB3 as **un-retirable** because SB5 lacks the
  favorites wizard and band-scan tile.
- **Both SSE and WebSocket**: `/api/stream` is `text/event-stream`; `/ws/spectrum` is a
  **hand-rolled RFC 6455 implementation** inside the HTTP handler (`ui/ws_spectrum.py:154`
  `compute_accept_key`, `:177` `encode_frame`).
- ~90 GET routes, ~60 POST routes, plus an alias layer (`handlers.py:2660`
  `_canonical_scan_api_path()`) that rewrites a preferred `/api/scan/*` surface onto legacy
  handlers.

**Verdict: do not port.** A 10k-line handler class with a linear route chain, three
overlapping UIs, and a hand-rolled WebSocket is not an asset. It is also shot through with
the Linux-only calls in §1.2.

**Take instead:** `macos/scannerctl/` — a Flask skeleton that already has the right shape
(`app.py`, ~80 lines: `/api/status` unifying SDRangel REST + SDRTrunk logs, `/api/scan/<onoff>`,
`/api/squelch`, `/api/digital/restart`) and a launchd plist. Its own docstring is accurate
about its state: *"STATUS: skeleton. Routes + client wiring are real; the actual SDRangel
field names and SDRTrunk levers need confirming against running instances."*

The `/api/scan/*` alias layer in the old UI is worth reading before designing the new route
surface — it is Will's own considered opinion, expressed in code, about what the API *should*
have been called.

### 1.6 Icecast pattern

**Mounts today (per role, one source each):**

| Role | Mount | Source mechanism |
|---|---|---|
| Neptune analog | `/neptune-angel.mp3` | SDRangel copyToUDP → `udp:9998` → ffmpeg → icecast |
| Neptune digital | `/neptune-trunk.mp3` | **SDRTrunk's own icecast broadcaster** (no ffmpeg) |
| Venus analog | `/venus-angel.mp3` | same as Neptune analog |
| *(chirp-era, retiring)* | `/ANALOG.mp3`, `/ANALOG_GROUND.mp3`, `/DIGITAL.mp3`, `/VFO.mp3` | libshout from `chirp/dsp/icecast_sink.py` |

Renamed by `162423d` (`{neptune,venus}.mp3` → `*-angel.mp3`, `neptune-digital` →
`neptune-trunk`).

**Config generation: none, and that's deliberate.** Three inconsistent sources exist:

1. `icecast/icecast.xml.example` — hand-maintained Linux reference. Has per-mount
   `<fallback-mount>` + `<fallback-override>1` to survive source restarts, and a
   `<http-headers>` CORS block the browser player needs for Web Audio.
2. `scripts/mac-bootstrap.sh:195-221` — **generates** `$PREFIX/etc/icecast.xml` by heredoc,
   only if absent. **No `<mount>` blocks at all**, and **no CORS block.**
3. `scripts/cutover_ma_sl_split.sh:97-130` — an XML-aware Python patch (explicitly not sed).

The Mac path relies on **implicit dynamic mount creation** — the mount springs into
existence from ffmpeg's connect URL. As the two-box notes put it: *"renaming = edit the
bridge script's target only (no icecast restart)."*

This is genuinely elegant and SB3 should keep it. But **the Mac deployment has silently lost
two properties** the Linux config had, and both should be recovered before SB3 leans harder
on icecast:

- **No `<fallback-mount>`** → a source restart 404s the mount instead of falling back.
- **No CORS `<http-headers>`** → an embedded browser player can't use Web Audio.

---

## 2. How SB3 currently talks to radios

### 2.1 The chain: GNU Radio → gr-osmosdr → (SoapySDR | native rtl)

`chirp/daemon.py:567` — `class ChirpFlowgraph(gr.top_block)`. Acquisition is
**`osmosdr.source`** (`chirp/dsp/source_sdr.py:147`), and gr-osmosdr is a hard requirement:

```python
# chirp/dsp/source_sdr.py:133-136
if osmosdr is None:
    raise RuntimeError("osmosdr (gr-osmosdr) is not importable; cannot use sdr source")
```

Note that **not everything in the repo agrees on the abstraction**. Three different SDR
access methods coexist:

| Consumer | Method |
|---|---|
| `chirp/` | GNU Radio `osmosdr.source` (which itself dispatches to Soapy *or* native rtl) |
| `scripts/vfo.py`, `disco/src/sweep.py` | **`import SoapySDR` directly** — `SoapySDR.Device(soapy_args)` |
| SDRTrunk | **Native SDRplay API** — skips Soapy/osmosdr entirely |
| SDRangel | Its own in-app device plugins |

**There is no unifying "device" abstraction in SB3 today.** The nearest thing is a string.

### 2.2 The "device" concept — an opaque driver-args string

There is no `Device` class. The device is `SdrSourceConfig.device_args: str`
(`source_sdr.py:102`), passed verbatim to osmosdr. Two incompatible syntaxes are live:

```jsonc
// SDRplay RSPduo via SoapySDRPlay3 — note rspduo_* keys, NOT osmosdr's mode/tuner
"device_args": "soapy=,driver=sdrplay,serial=1809063632,rspduo_mode=ST,rspduo_tuner=1"
// RTL via osmosdr's NATIVE backend — NOT soapy=,driver=rtlsdr (empirical, M1, 2026-07-06)
"device_args": "rtl=61108285"
```

**Identification is always by serial, never index or USB position.** This is a fleet-wide
invariant (`sdr_fleet_policy.json`: `"address_by": "serial"`) and it is the single most
durable lesson in the repo — see `project_ground_nfm_serial_collision` (a "structural
−180 dBFS NFM bug" that was actually two consumers on one dongle).

**SB3's real device abstraction is `broker/`, not anything in chirp.** And its central
design decision is worth quoting, because §4 depends on it:

```python
# broker/server.py:22-30
#   a lease is bound to the client connection; the socket staying open IS the lease.
```

One lease per connection. The connection closing for *any* reason — clean exit, crash,
SIGKILL — auto-releases after a 0.5 s grace. No reaper, no stale-lease third state. For
non-Python consumers there's a subprocess wrapper that holds the socket for the child's
whole lifetime:

```bash
python -m broker.client run --serial 180903EF32 --consumer sdrtrunk --reason p25 -- /opt/sdrtrunk/bin/sdr-trunk
```

Denials are specific and name the holder (`leases.py:339-348` — the comment says *"this line
IS the product"*). Ten stable denial codes. A belt-and-braces `fcntl.flock` per serial backs
the in-memory ledger.

**This is the best-designed component in the repo and it survives the migration unchanged.**

### 2.3 Audio egress

chirp: `Mixer (float32 @16 kHz) → IcecastSink → int16 → lame subprocess pipe → libshout →
icecast`. Notable: `work()` **never returns −1** (`icecast_sink.py:~84`) because returning
−1 marks the block done and propagates done-ness — the 2026-06-18 wedge. And the honest
hazard at `icecast_sink.py:35-37`: **lame emits valid MP3 frames for constant-zero PCM**, so
a byte-rate health check reads "healthy" on total silence. That lie recurs in the SDRangel
design (§4.4) and must be designed against again.

The UDP in chirp is the **command bus**, not audio: `127.0.0.1:7400` (airband) / `7401`
(ground), asyncio datagrams, pydantic-validated envelope `{v,id,cmd,args}`, ~12 verbs.

---

## 3. Design proposal — SB3 as SDRangel orchestrator

### 3.0 The shape, in one diagram

```
                      ┌──────────────────────────────────────────┐
                      │  SB3 CONTROL PLANE  (optional, killable) │
                      │                                          │
   profiles/*.json ──▶│  profile loader → translator → reconciler│
                      │  disco classifier · ACARS · web UI       │
                      └───────┬──────────────────────┬───────────┘
                              │ REST :8091           │ playlist XML + launchctl
                              │ (live, idempotent)   │ (config + restart)
                              ▼                      ▼
                      ┌───────────────┐      ┌────────────────┐
   ┌──────────────────│   SDRangel    │      │    SDRTrunk    │──────────────┐
   │  RTL×3           │  (analog)     │      │  (digital P25) │  RSPduo      │
   │                  └───────┬───────┘      └────────┬───────┘  dual-tuner  │
   │                          │ copyToUDP             │ native icecast       │
   │                          ▼                       │ broadcaster          │
   │                    udp:9998 → ffmpeg             │                      │
   │                          │                       │                      │
   └──────────────────────────┴───────────┬───────────┴──────────────────────┘
                                          ▼
                              icecast :8000  (dynamic mounts)
                       /neptune-angel.mp3   /neptune-trunk.mp3   /neptune-<role>.mp3
                                          │
                                          ▼   ◀── survives SB3 death. This is the invariant.
                                        phone
```

**Read the dashed boundary as a contract:** everything below the REST/playlist line keeps
running when everything above it is dead.

### 3.1 What already exists (and is better than you'd expect)

`macos/bin/` on `origin/main` is a working, deployed, self-healing SDRangel orchestrator.
**It is the prototype for SB3's translator layer, and it should be read before any new code
is written.**

`sdrangel-restore.py` — host-aware idempotent config reconciler:

```python
HOST = socket.gethostname().lower()
if "m1mini" in HOST or "neptune" in HOST:
    HOST_LABEL, ROUTES = "Neptune (m1mini)", [SKYWARN_2M]
else:
    HOST_LABEL, ROUTES = "Venus (macmini)", [AIRBAND, NFM]
```

Its safety property is elegant: **a wrong host match is a safe no-op**, because
`find_or_assign()` only acts on serials SDRangel actually enumerates. The other box's routes
simply time out on `wait_device()`. That property — *reconcile only what you can prove is
yours* — should be carried into SB3's translator verbatim.

`route_healthy()` is the idempotence check, and it's the right list: serial + center (±5 kHz)
+ channel count + `state=="running"` + gain + channel volume (±0.01). Gain comparison
tolerates the RTL's snap to the nearest supported step (`abs(cur-v) > 6`; 400 reads back as
402); SDRplay IF/LNA compare exactly.

`copytoudp-watchdog.sh` — a 30 s loop that runs the restore when **the SDRangel pid changes**
(restart detected), on first pass, or every 20th cycle (~10 min safety sweep). Then
independently re-arms copyToUDP.

**The architectural insight buried in that script:** *the tap lives on the audio device, not
on a deviceset* — so it must be re-armed independently of the route restore. SB3 inherits
this split.

### 3.2 The SDRangel REST surface — and its landmines

Base: `http://127.0.0.1:8091/sdrangel`.

| Method | Path | Purpose |
|---|---|---|
| GET | `` (root) | instance summary → `{devicesetlist:{deviceSets:[…]}}` |
| GET | `/devices?direction=0` | enumerate → `{devices:[{serial,…}]}` |
| GET | `/deviceset/{i}` | `{samplingDevice:{serial,centerFrequency,state}, channels:[…]}` |
| PUT | `/deviceset/{i}/device` | `{"hwType":"RTLSDR"\|"SDRplayV3","serial":"<sn>","direction":0}` |
| POST / DELETE | `/deviceset/{i}/device/run` | start / stop |
| PATCH | `/deviceset/{i}/device/settings` | `{"deviceHwType":…,"rtlSdrSettings":{…}}` |
| POST / DELETE | `/deviceset/{i}/channel[/{c}]` | add / remove demod |
| PATCH | `/deviceset/{i}/channel/{c}/settings` | `{"channelType":…,"AMDemodSettings":{…}}` |
| GET / PATCH | `/audio[/output/parameters]` | the copyToUDP tap |

**Landmines — every one of these is written in blood in `analog_scanlists.json:_comment`,
`sdrangel-restore.py`, and `reference_two_box_audio_harness`. SB3's translator must encode
all of them:**

1. **SDRangel keeps working config in RAM and reverts to a stale on-disk plist on
   crash/restart.** Gain, centers, and channels vanish. *This is the entire reason the
   restore tooling exists.*
2. **SDRangel drops ALL channels on restart.** Confirmed on Venus: DS0 came back `running`
   with 0 channels. After any bounce you must re-`POST /device/run` **and** re-add every
   channel.
3. **PATCH only a RUNNING device.** A pre-run PATCH on a freshly-reloaded device is
   *silently ignored*.
4. **Add/delete channels ONE AT A TIME with ~0.4 s delays.** Rapid bulk ops crash SDRangel.
5. **`copyToUDP` will not emit from a plain REST arm.** Setting `copyToUDP:1` sets the flag
   but does not start the sender thread → 0 bytes on 9998 → mount 404. **You must toggle
   0→1.** This one cost a very long session.
6. **`audioDeviceName` must be the EXACT tap device name** — on Neptune `"Mac mini Speakers"`
   (audio idx0), *not* `"System default device"`, even though idx0 *is* the default. Wrong
   name = 0-byte tap = mount 404.
7. **A silent band = a dead mount.** copyToUDP sends nothing when all channels are gated →
   icecast source-timeout → 404. **Every route needs a low-volume always-open keepalive
   channel** (`squelch -100, volume 0.3-0.4`).
8. **A combined device-settings PATCH + `device/run` can leave the center wrong** (Venus:
   118.293 instead of 118.500). Re-PATCH `centerFrequency` alone, then verify.
9. **Orphan ffmpeg holding udp:9998** = the sneaky mount-404. Fix is a *targeted* kill
   (`pkill -f 'ffmpeg.*udp://127.0.0.1:9998'`), never blanket.
10. **CPU is a real constraint.** Running airband + 70cm + digital together on the Intel mini
    pegged SDRangel's 8 MHz channelizer at ~420% CPU, load hit ~17, and **SDRTrunk died.**
    Neptune is an M1 with 8 GB — faster, but this is a budget to respect, not ignore.

**A caution on `macos/clients/sdrangel_client.py`:** it carries its own warning — *"written
from the documented API shape BEFORE testing against a live instance."* It calls `GET
/devicesets` (plural); the code that actually runs reads the list from the instance root.
**Treat `sdrangel-restore.py` as the authority on real endpoint shapes.**

### 3.3 The SB3 profile — proposed schema

A profile is **a role's complete listening intent, backend-agnostic**. SB3 translates; it
does not store backend state.

```jsonc
// profiles/air-airband-nashville.json
{
  "schema_version": 1,
  "id": "air-airband-nashville",
  "role": "air",                       // air | ground | digital | disco | sounding
  "label": "Nashville Airband",
  "engine": "sdrangel",                // sdrangel | sdrtrunk
  "mode": "camp",                      // camp | hunt   (see §3.4)

  "device": { "serial": "83241970", "hw_type": "RTLSDR" },

  "rf": {
    "center_hz": 118500000,            // camp: explicit. hunt: computed by the planner.
    "sample_rate_hz": 2048000,
    "gain": 297,                       // SDRangel native units (29.7 dB). NOT chirp's dB.
    "agc": false,
    "dc_block": true
  },

  "demod": { "type": "AMDemod", "rf_bandwidth_hz": 8000,
             "squelch_dbfs": -55, "volume": 3.0 },

  "channels": [
    { "mhz": 118.400, "label": "Nashville App/Dep East" },
    { "mhz": 118.600, "label": "BNA Tower" },
    { "mhz": 119.450, "label": "Nashville Approach" }
  ],

  // Landmine #7. Not optional. SB3 injects one if absent and says so in the log.
  "keepalive": { "mhz": 118.800, "squelch_dbfs": -100, "volume": 0.3 },

  "audio": { "tap_device": "@idx0", "mount": "neptune-angel.mp3" }
}
```

**Design decisions, and why:**

- **`gain` is in SDRangel native units, not dB.** The temptation is to abstract it. Do not.
  `project_airband_rf_collapse_recurring` is a ten-hour incident caused by exactly one gain
  abstraction mapping wrong (chirp applied its gain value as SDRplay IFGR *reduction* —
  inverted, higher = quieter). **Store what the backend takes. Let the UI label it.**
- **`tap_device: "@idx0"` resolves dynamically at apply time**, not a literal string. This is
  the `fix-{venus,neptune}-angel.sh` divergence made safe: Neptune's script hardcodes
  `"Mac mini Speakers"` and Venus's hardcodes `"System default device"`, and
  `route_healthy()` **never checks `audioDeviceName`** — so nothing reconciles it. A latent
  bug today; a resolved indirection tomorrow.
- **`keepalive` is a first-class field**, because it is a first-class failure mode.
- **No `deviceset` index in the profile.** SB3 resolves serial → deviceset at apply time,
  exactly like `find_or_assign()`. Deviceset indices are runtime facts, not config.
- **Profiles are per-role, not per-box.** Host-awareness lives in a separate binding map, so
  a Nashville airband profile can run on either mini.

### 3.4 Hunt mode vs camp mode on SDRangel — the design problem

This is where the plan gets interesting, and where §7.1's risk bites.

**Camp mode is nearly free.** SDRangel demodulates every channel in the window
simultaneously — it's a channelizer, not a scanner. So "camp" = one deviceset, one center,
N `AMDemod` channels at `inputFrequencyOffset = (target - center)`, all routed to the tap.
**This is strictly better than chirp's camp mode**, because chirp's priority gate passes only
one open channel at a time, whereas SDRangel genuinely receives them all in parallel. Will's
existing Venus and Neptune airband setups already work exactly this way.

**Hunt mode is the problem.** SDRangel has a `FreqScanner` channel that does what chirp's LO
scheduler does. But:

> **`docs/scan-philadelphia.md`: the FreqScanner CSV is imported via the SDRangel GUI —
> 7.25.1 can't set the freq list over REST.**

And independently, from the FRS/GMRS work (2026-07-14):

> **"Multiplex chosen over FreqScanner (FreqScanner won't auto-resume from a quiet parked
> channel)."**

Two independent findings, both against FreqScanner. **→ Recommendation: build hunt mode out
of multiplexed demods, not FreqScanner.** Concretely:

- **Within one window (~2 MHz for RTL at 2.048 Msps): don't hunt. Multiplex.** Instantiate a
  demod per channel. This covers airband (118–119.5 = 1.1 MHz) and the whole 462 MHz FRS/GMRS
  block (15 NFMDemods, already proven on Neptune). No scanning required.
- **Across windows (channels that don't fit one span): SB3 retunes the deviceset centre on a
  dwell** — reimplementing chirp's `cluster_planner.py` + `lo_scheduler.py` logic in the
  control plane, driving `PATCH /device/settings {centerFrequency}` + re-offsetting each
  demod. `cluster_planner.plan_clusters()` is a **pure function with no GR imports** and
  `lo_scheduler.step()` is a **pure state machine driven by injected callbacks**
  (`retune_to`, `park_channels`, `is_open`). **Both port to the SB3 control plane almost
  verbatim.** That is a genuinely lucky piece of prior design.

**But be honest about the cost:** cross-window hunting at ~1 Hz over REST, against an API
whose own tooling PATCHes with 0.4 s delays and 3× retries, is a materially worse scanner
than chirp's in-flowgraph LO scheduler. **Recommendation: ship camp/multiplex first
(Phase 2), and treat cross-window hunt as a Phase 4 question we answer with measurements, not
optimism.** For most of Will's actual roles — airband, FRS/GMRS, SKYWARN — multiplex within
one window is sufficient and strictly better.

### 3.5 Where SDRTrunk fits — the dual-digital case

**The asymmetry is the central shape of this integration**, and `macos/clients/sdrtrunk_client.py`
states it better than I can:

> *"SDRTrunk is a Java GUI app. Unlike SDRangel, it has **no rich runtime REST API**… READ
> live decode state → tail/scrape SDRTrunk logs. CHANGE what's monitored → rewrite the
> playlist XML + restart via launchctl. There is NO 'set squelch on channel X at runtime over
> HTTP' like SDRangel."*

| | SDRangel | SDRTrunk |
|---|---|---|
| **Write** | live REST, idempotent reconcile | rewrite playlist XML + graceful restart |
| **Read** | REST GET | scrape event logs / call CSV |
| **Audio** | copyToUDP → ffmpeg → icecast | native icecast broadcaster |
| **SB3 cadence** | continuous (30 s reconcile) | rare (config change only) |

**Dual P25 on one RSPduo — the mechanism.** Two `<channel>` elements, each pinned to a
different tuner via `preferred_tuner=`. The repo already has a worked example in
`scripts/build_sugar_tree_playlist.py` (analog T1 / TACN P25 T2):

```python
TUNER1 = "RSPduo SER:180903EF32 Tuner 1"
TUNER2 = "RSPduo SER:180903EF32 Tuner 2"
<source_configuration type="sourceConfigTunerMultipleFrequency"
                      preferred_tuner="{esc(TUNER2)}" source_type="TUNER_MULTIPLE_FREQUENCIES">
```

Playlist schema (from `etc/mac/sdrtrunk/philadelphia-p25.xml`):

```xml
<playlist version="4">
  <stream type="icecastHTTPConfiguration" name="neptune-digital" enabled="true" format="MP3"
          host="127.0.0.1" port="8000" mount_point="/neptune-trunk.mp3"
          user_name="source" password="…" bit_rate="16"/>
  <alias list="Philadelphia P25" name="All P25 Voice">
    <id type="talkgroupRange" min="1" max="65535" protocol="APCO25"/>   <!-- matcher -->
    <id type="broadcastChannel" channel="neptune-digital"/>              <!-- router  -->
  </alias>
  <channel name="Philadelphia P25 (PPD/PFD)" order="1" enabled="true">
    <source_configuration type="sourceConfigTunerMultipleFrequency"
                          frequency_rotation_delay="200" source_type="TUNER_MULTIPLE_FREQUENCIES">
      <frequency>853312500</frequency> <!-- … 8 control channels … -->
    </source_configuration>
    <decode_configuration type="decodeConfigP25Phase1" modulation="CQPSK"
                          traffic_channel_pool_size="20"/>
    <alias_list_name>Philadelphia P25</alias_list_name>
  </channel>
</playlist>
```

**Three things that will bite:**

1. **Streaming is per-alias.** Audio flows only for talkgroups matched by an alias whose
   `list` equals the channel's `<alias_list_name>` **and** which carries a `broadcastChannel`
   id. The catch-all `talkgroupRange 1–65535` is what makes all voice stream. **Unaliased
   talkgroups silently don't stream** — this needs a CI check in the generator, not a
   comment.
2. **`disabledTuners` is the current single-tuner pin.** `macos/sdrtrunk/README.md` pins to
   one tuner because *"SDRTrunk auto-opens every tuner by default; two concurrent dual-tuner
   RSPduos collapse the USB isochronous stream."* Read that carefully: the warning is about
   **two RSPduos**, not two tuners of one. Going dual means emptying `disabledTuners` — which
   the fleet policy explicitly plans for. **It does not violate the invariant.**
3. **The tuner-label string format is inconsistent in-repo** and must be read off the live
   `View → Tuners` list:
   - `build_sugar_tree_playlist.py`: `"RSPduo SER:180903EF32 Tuner 1"`
   - `macos/sdrtrunk/tuner_configuration.json`: `"RSPduo Tuner 1 SER#1809063632"`

**Don't hand-write the two-system playlist.** `scripts/ensure-digital-runtime.py` (1,169
lines) already implements `_sync_source_configuration()`, `_sync_tuner_configuration()`,
`_preferred_tuner_target()`, `_sync_alias_broadcast_channels()`, and
`_sync_stream_configuration()` — the exact "dedicated tuner per system, no time-slicing"
model this needs. It was written for RTL dongles keyed by `preferred_tuner_serial`.
**Generalizing it to RSPduo tuner labels is a smaller and safer job than writing a new
generator.**

**Operational discipline (non-negotiable, from two validated memories):**
- **Stop SDRTrunk with SIGTERM. Never SIGKILL.** A dirty release drops the RSPduo off the USB
  bus entirely (`ioreg` count = 0) and **only a reboot recovers it.** Reseating does not work.
- **Wait ~25 s after stop** for the apiService to release the tuner, or SDRTrunk comes up
  `Discovered [0]`.
- **These two rules are why `sb3-ctl` must own SDRTrunk's lifecycle** (§4) — a naive
  `launchctl bootout` is a footgun.

**Phase 1 vs Phase 2 discrepancy to resolve:** `docs/scan-philadelphia.md` says Philadelphia
is *"P25 Phase II simulcast"*, but the playlist uses `decodeConfigP25Phase1`. This may be
deliberate (a Phase II system's CC is Phase 1, and CC + Phase-1 grants still yield audio) or
a copy-forward from `mtrtrs-playlist.xml`. `build_sugar_tree_playlist.py` uses
`decodeConfigP25Phase2` for TACN. **Verify against a live decode** — and note the Neptune
memory already flags *"intermittent SYNC LOSS on some voice follows"* with the exact remedy:
*"if metadata-shows-but-audio-silent, some voice channels are Phase 2 → flip that channel to
decodeConfigP25Phase2."* That symptom has probably already been observed.

### 3.6 disco, ACARS, and the survey role

Both are **keep-the-logic, replace-the-plumbing**:

| | Keep | Replace |
|---|---|---|
| **disco** | `classifier.py` (heuristic v0 / ONNX, + ULS/CDBS/HPDB/bandplan enrichment), `training/` | `sweep.py`'s direct `SoapySDR.Device()` → SB3 leases the RTL via `broker/` and drives it; `/run/*` → `DISCO_STATE_DIR`; `/bin/systemctl` → `ServiceBackend` |
| **ACARS/VDL2** | `ui/wxdata.py` (1,872 lines of decode/enrichment) | systemd units → launchd plists, broker-leased; `/run/*.json` → configurable paths |

**The classifier never touches an SDR** — it consumes `.iq.f32` slice files with a 6-field
filename schema. That's a clean seam: SB3 owns capture, disco owns classification.

**The three-way collision on RTL `61108285` is real and unresolved.** Will's brief assigns it
"Disco + ACARS + spectrum survey." Those are three consumers of one dongle:

- ACARS wants ~131 MHz continuously (`acarsdec -o 4 … 131.550 130.025 130.450 131.125`).
- VDL2 wants ~136.8 MHz continuously.
- disco wants to sweep everywhere.
- Survey wants to sweep everywhere.

**These cannot run simultaneously.** ACARS/VDL2 are *continuous* by nature — an ACARS
decoder that only listens 20% of the time misses 80% of the messages. The old Linux answer
was `scripts/tuner_broker.py` swapping systemd units on a mode flag, which is exactly the
time-slicing this implies.

**→ Recommendation:** make this an explicit, profile-selected, broker-arbitrated **mode**
(`disco` | `acars` | `survey`), not a background time-share. One at a time, Will picks, SB3
enforces via the lease. Note the fleet policy already concedes the shape:
`"None of the sounding decoders are broker-integrated yet — wiring the claim-by-serial call
in is a prerequisite before any of them go live."`

### 3.7 Concrete example: "SB3 Profile: Air-Airband-Nashville" → SDRangel

Given the profile in §3.3, SB3's translator emits this sequence. **The ordering is not
stylistic — every step encodes a landmine from §3.2.**

```
 0. broker: claim serial 83241970, consumer "sb3-air", reason "airband"
      └─ denied → log the holder by name, abort. Never open a device we don't hold.

 1. GET /sdrangel                       → deviceSets[]  (authority: instance root, not /devicesets)
 2. GET /sdrangel/devices?direction=0   → confirm 83241970 is enumerated
      └─ absent → abort the ROUTE, not the run.        [landmine: safe no-op on wrong host]

 3. resolve deviceset: prefer DS0; else scan indices for a free/matching one
 4. if DS0 already has a different device:  DELETE /deviceset/0/device/run
    PUT  /deviceset/0/device   {"hwType":"RTLSDR","serial":"83241970","direction":0}
    sleep 3.0                                            [settle]

 5. POST /deviceset/0/device/run                         [#3: PATCH only a RUNNING device]

 6. PATCH /deviceset/0/device/settings
      {"deviceHwType":"RTLSDR","direction":0,
       "rtlSdrSettings":{"centerFrequency":118500000,"devSampleRate":2048000,
                         "gain":297,"agc":0,"dcBlock":1,"log2Decim":0}}
    verify centerFrequency within ±5 kHz; up to 3 retries  [#8: center can silently not stick]
    └─ if still wrong → re-PATCH centerFrequency ALONE, then re-verify.

 7. reconcile channels — ONE AT A TIME, 0.4 s apart      [#4: bulk ops crash SDRangel]
    tap = resolve("@idx0") from GET /audio  →  "Mac mini Speakers"   [#6: EXACT name]

      ch0  118.400  offset -100000   AMDemod  rfBW 8000  sq -55   vol 3.0   audio=tap
      ch1  118.600  offset +100000   AMDemod  rfBW 8000  sq -55   vol 3.0   audio=tap
      ch2  119.450  offset +950000   AMDemod  rfBW 8000  sq -55   vol 3.0   audio=tap
      ch3  118.800  offset +300000   AMDemod  rfBW 8000  sq -100  vol 0.3   audio=tap  ← KEEPALIVE
                                                                            [#7: else mount 404s when quiet]

 8. arm the tap — TOGGLE, do not just set                [#5: a plain arm does NOT start the sender]
    PATCH /audio/output/parameters {"index":0,"copyToUDP":0}
    sleep 0.5
    PATCH /audio/output/parameters {"index":0,"copyToUDP":1,"udpAddress":"127.0.0.1",
                                    "udpPort":9998,"udpUsesRTP":0,"udpChannelMode":2,
                                    "udpChannelCodec":0,"sampleRate":48000}

 9. pkill -f 'ffmpeg.*udp://127.0.0.1:9998'              [#9: TARGETED. never blanket]
    measure udp:9998 > 0 bytes / 3 s with the port briefly free
    launchctl kickstart -k gui/$UID/com.scannerproject.neptune-audio-bridge

10. VERIFY, and mean it:
      - udp:9998 carrying bytes
      - GET /admin/stats  → mount neptune-angel.mp3 present, source connected
      - HTTP 200 on the mount, sustained 3× at 3 s
    └─ any failure → structured diagnostic, profile marked FAILED. No third state.
```

**Idempotence.** Re-running is a no-op when `route_healthy()` passes: serial + center (±5 kHz)
+ channel count + `state=="running"` + gain (±6, for the RTL step-snap) + volume (±0.01).
**Add `audioDeviceName` to that list** — its absence is the latent Neptune/Venus bug in §3.3.

---

## 4. Kill switch design

### 4.1 The principle

> **Manual override has always won. Encode that, don't fight it.**

The brief asks to "match how manual override has always won in tonight's fights." The repo
records exactly why that instruction exists:

> *"`sdrangel-restore.py` silently REVERTS any manual SDRangel config back to the host's
> route set every ~10 min + on SDRangel restart. Manual airband tuning gets clobbered within
> minutes. The pause-gate marker `.sdrangel-restore-paused` is unreliable (went MISSING
> across reboots → restore active again). For a manual session, `launchctl bootout
> gui/$UID/com.scannerproject.copytoudp-watchdog` (fully stop it) is the reliable way."*
> — `reference_two_box_audio_harness`

**Read that as a design review.** The existing orchestrator has a kill switch. It's a marker
file, it's unreliable, it fails *open* (a missing marker = clobber resumes), and the only
trustworthy way to stop the thing is to kill the process. Will already discovered the right
answer empirically and has been using it. `sb3-ctl kill` is that discovery, made official
and made safe.

**So the kill switch is not a feature of SB3. It is the definition of SB3.** SB3 is *the set
of processes you can bootout without losing audio.* If a component can't be killed, it isn't
part of SB3 — it's part of the backend.

And the failure direction must invert: **a marker file that fails open is a bug; SB3 must
fail closed.** If SB3 can't prove it should be reconciling, it doesn't reconcile.

### 4.2 State ownership — the actual boundary

| State | Owner | Survives `sb3-ctl kill`? |
|---|---|---|
| Profile definitions (`profiles/*.json`) | **SB3** | ✅ on disk, inert |
| Profile→device binding, active profile set | **SB3** | ✅ on disk, inert |
| disco classifications, ACARS messages, hit log | **SB3** | ✅ on disk; **collection stops** |
| Web UI | **SB3** | ❌ **goes away — by design** |
| **Deviceset config, channels, gain, center** | **SDRangel (RAM)** | ✅ **untouched, keeps running** |
| **copyToUDP tap** | **SDRangel (RAM)** | ✅ **untouched** |
| **Playlist, aliases, streams** | **SDRTrunk (disk+RAM)** | ✅ **untouched** |
| **ffmpeg bridge, icecast, mounts** | **launchd (independent agents)** | ✅ **untouched** |
| Device leases | **broker** | ⚠️ **§4.5 — the hard question** |

**The line is clean because of an accident of the existing design:** SB3 never holds audio
state. It only *asserts* state onto backends that then hold it themselves. Kill the asserter
and the assertions stand. That is why this plan is safe to attempt, and it's worth
protecting deliberately rather than relying on it by luck.

### 4.3 Command surface

```
sb3-ctl status              # what SB3 thinks; what the backends actually report; the diff
sb3-ctl kill                # stop reconciling. Backends untouched. Audio continues.
sb3-ctl resume              # adopt LIVE state, then reconcile forward
sb3-ctl apply <profile>     # one-shot translate+verify, then exit (works while killed)
sb3-ctl diff                # dry run: what would resume change? Prints, changes nothing.
```

`kill` in detail — ordering matters:

```
1. touch  $SB3_STATE/killed              (fail-CLOSED sentinel; §4.4)
2. launchctl bootout gui/$UID/com.scannerproject.sb3-reconciler
3. launchctl bootout gui/$UID/com.scannerproject.sb3-ui
4. LEAVE RUNNING, always:
     com.scannerproject.sdrangel
     com.scannerproject.sdrtrunk
     com.scannerproject.icecast
     com.scannerproject.neptune-audio-bridge
     com.scannerproject.copytoudp-watchdog      ← see §4.4
     com.scannerproject.tuner-broker            ← see §4.5
5. verify all mounts still 200. Report. Exit non-zero if any dropped.
```

**Step 5 is the point.** A kill switch that doesn't verify the invariant it exists to protect
is a wish. `sb3-ctl kill` must *prove* audio survived before it reports success.

### 4.4 Resume adopts live state — never a snapshot

**The invariant:** `resume` must read what SDRangel/SDRTrunk are *actually doing right now*
and reconcile forward from there. It must never replay a snapshot taken at `kill` time.

Why this is non-negotiable: **the entire reason Will kills the orchestrator is to change
things by hand.** A resume that restores a pre-kill snapshot would clobber precisely the work
the kill existed to enable. That is the `sdrangel-restore.py` bug, reintroduced with extra
steps.

```
sb3-ctl resume:
  1. GET /sdrangel + GET /audio      → observe LIVE truth
  2. read SDRTrunk playlist from disk → observe LIVE truth
  3. for each bound profile:
       route_healthy(profile, live)?
         ✅ → adopt. Log "adopted live state, no change."
         ❌ → the profile and reality have DIVERGED.
              Default: DO NOT CLOBBER. Mark the profile `diverged`,
              surface the diff, and reconcile nothing.
              Only `sb3-ctl apply <profile> --force` re-asserts.
  4. rm $SB3_STATE/killed
  5. bootstrap the reconciler
```

**Divergence defaults to "the human is right."** SB3 reports the diff and waits. This is
the opposite of today's every-10-minutes clobber, and it is the whole behavioural point of
the redesign.

**The `copytoudp-watchdog` question.** It re-arms the tap and runs the restore. Under SB3 it
must **split in two**:

- **Tap-arming** (`copyToUDP != 1 → toggle 0→1`) is **audio plumbing, not policy**. It stays
  running through a kill — it's what keeps the mount alive across an SDRangel crash. Killing
  it would violate the core invariant.
- **Route restoration** (`sdrangel-restore.py` on pid-change) is **policy** and moves into
  the SB3 reconciler, where `killed` gates it.

That split is Phase 1's real work, and it maps exactly onto the boundary in §4.2: the tap is
audio, the routes are intent.

**The health-check trap, inherited.** `icecast_sink.py:35-37` warns that lame emits valid MP3
frames for constant-zero PCM — so byte-rate reads healthy on total silence. **The same lie
exists in the SDRangel chain**: ffmpeg will happily encode a silent-but-present UDP stream at
full bitrate forever. A keepalive channel (§3.2 #7) makes this *worse*, because it guarantees
non-silence. **→ SB3's verify must check the tap byte-rate AND the mount AND at least one
real hit within a window.** Mount-200 is necessary, not sufficient. This is `project_probe_rate_watchdog_deploy`
and `project_ground_icecast_publish_loop_wedge` in a new costume, and it will be missed
unless it's designed for now.

### 4.5 The broker question — the one genuinely hard call

If `sb3-ctl kill` stops the broker, leases evaporate and the invariant is safe but the
protection is gone. If it leaves the broker running, the protection holds — but **SDRangel
and SDRTrunk are GUI apps launched by their own LaunchAgents; they do not hold broker
leases.** They open devices directly. Today the broker leases devices to *chirp*, which is
retiring.

**So under the SDRangel/SDRTrunk backend, what does the broker actually arbitrate?**

Honest answer: **less than it did, but not nothing.** The remaining contenders for a lease
are disco, ACARS/VDL2, survey, and VFO — the three-way collision in §3.6 is *precisely* a
lease problem, and it's the strongest remaining case for keeping the broker.

**→ Recommendation: keep the broker running through a kill, and treat SDRangel/SDRTrunk as
holding *static reservations* rather than dynamic leases.** SB3 asserts on startup that the
RSPduo serial is reserved for SDRTrunk and RTL `83241970` for SDRangel-air; the broker then
refuses to grant those serials to disco/ACARS/survey. The reservation outlives SB3 because
it's the broker's own state, and the broker is a 1.5k-line stdlib daemon that has never been
the thing that broke.

**This needs Will's confirmation before Phase 1** (§7). It is the one design question where I
can construct a defensible argument for either answer.

---

## 5. Hardware / USB topology for Neptune

### 5.1 What Neptune has (and the contradiction that has to be resolved first)

**The repo contradicts itself about which RSPduo is on Neptune, and the fleet policy is
almost certainly stale.**

| Source | Date | Claim |
|---|---|---|
| `etc/mac/sdr_fleet_policy.json` rev 4.1 | 2026-07-08 | RSP `180903EF32` was **"physically UNPLUGGED and taken to a DIFFERENT computer."** The M1 keeps `1809063632`. |
| `docs/scan-philadelphia.md` | ~2026-07 | **"No RSPduo on Neptune"** — only 2 RTLs (`61108285`, `83241970`). |
| `project_neptune_philly_p25_validated` (memory) | **2026-07-10** | **Neptune decodes Philly P25 on RSPduo `180903EF32`.** 1,756 msgs, real call events. |
| `sdrangel-restore.py` Venus route | live | Venus airband = SDRplayV3 **`1809063632`**. |

**Reading these together: the policy has it backwards.** `180903EF32` went *to* Neptune (and
is validated decoding there, two days after the policy was written); `1809063632` went to
Venus. The policy's "different computer" was Venus, and it named the wrong device as the one
that moved.

**→ Phase 0 gate: read the actual serial off the box** (`SoapySDRUtil --find="driver=sdrplay"`,
or SDRTrunk `View → Tuners`) **and rewrite `sdr_fleet_policy.json` to rev 5.0.** Do not build
against a policy file that contradicts a validated decode. Note the policy's own instruction
already anticipates this: *"CONFIRM ON THE BOX which RSP is present before trusting this."*

**The RTL role assignment is also changing.** Will's brief reassigns two of three:

| Serial | Hardware | Fleet policy 4.1 role | **Will's brief** | Δ |
|---|---|---|---|---|
| `83241970` | RTL-SDR Blog V4 | chirp-airband (was VFO) | **Air / airband** | same intent |
| `56919602` | NESDR SMArt v5 | sounding (ACARS/VDL2/sonde) | **Ground — TBD** | ⚠️ **changed** |
| `61108285` | NESDR | chirp-ground | **Disco + ACARS + survey** | ⚠️ **changed** |

The Blog V4 keeps airband, which is right — it was chosen for airband historically
*specifically* for its better front-end/SNR (R828D + TCXO), and it moved
`80000003 → 61108285 → 83241970` over the Micro's life for exactly that reason.

**Both NESDRs are known-wedged.** From `reference_two_box_audio_harness`: *"Neptune reboots
leave SDRTrunk stopped + the 2 NESDRs off-bus."* That is the Phase 0 work item.

### 5.2 The hub — with an honest correction to the brief

The brief asks for multi-TT hub recommendations (GL3520/3523, VL817/822). Those are the right
chips. **But the stated reason is off, and it's worth getting right before money is spent:**

> **Multi-TT does not help RTL-SDRs or the RSPduo.** A Transaction Translator only exists to
> bridge **full-speed (12 Mbps) and low-speed** devices onto a high-speed bus. Every SDR here
> — RTL-SDR and RSPduo alike — is a **USB 2.0 high-speed (480 Mbps)** device. High-speed
> traffic **bypasses the TT entirely.** A single-TT hub and a multi-TT hub are identical for
> a bag of RTL-SDRs.

Multi-TT is still worth specifying (it correlates with better-engineered hubs and helps if a
full-speed device shares the hub), but **it is not the mechanism that fixes bandwidth.** The
two mechanisms that actually matter:

1. **Bus/controller separation.** A USB 2.0 high-speed segment is a **shared 480 Mbps
   domain**. Every USB-2 device behind one hub shares one. The fix for contention is *fewer
   devices per domain*, not a fancier TT. **This — not TT count — was the Micro's problem**
   (`project_sb6_session_2026_06_18_evening`: 10 SDRs, single xHCI, single 480M domain), and
   the 2026-06-19 retraction is explicit: *"the fix is a powered MULTI-TT USB-3 hub, NOT a
   USB-3 re-cable — RSPduo is USB-2."* The value of a **USB-3** hub here is subtle and real:
   it contains a *separate internal USB-2 hub*, so plugging it into a USB-3/TB port gives its
   USB-2 devices **their own 480 Mbps domain**, distinct from anything on other ports.
2. **Power.** `powered_hubs_required: true` is in the fleet policy for a reason. Three RTLs
   at ~300 mA each plus an RSPduo exceeds what a bus-powered hub delivers, and RTL brownout
   presents as intermittent USB errors — i.e. as a *software* bug, for hours.

**Bandwidth math (why this is comfortable on Neptune):**

```
RTL-SDR @ 2.048 Msps × 2 bytes (8-bit I + 8-bit Q)  =  4.10 MB/s  ≈  33 Mbps
3 RTLs                                              = 12.3 MB/s   ≈  98 Mbps
  … of a shared 480 Mbps domain (~280–320 Mbps practical)  →  ~30-35% utilized.  Fine.

RSPduo dual-tuner @ 2 Msps × 2 tuners × 2 bytes (14-bit → 16)  ≈  16 MB/s  ≈ 128 Mbps
  … on its OWN domain.  Fine.
```

**Four SDRs on an M1 is a fundamentally easier problem than ten on the Micro.** The Micro's
starvation does not automatically recur here. Do not over-buy.

### 5.3 Recommended topology

Neptune (M1 mini) has **2× Thunderbolt/USB4 + 2× USB-A**. The USB-A ports very likely share
one controller — `sb7-northstar-program.md` flags this: *"if the two USB-A ports share one
controller, consider a second hub to split the RTLs."*

```
┌─ Thunderbolt / USB4 port 1 ─────────────────────────────────┐
│   RSPduo  (180903EF32 — VERIFY, §5.1)   DIRECT, no hub      │
│   dual-tuner, SDRTrunk, native SDRplay API                   │
│   → own controller, own 480 Mbps domain, ~128 Mbps           │
└──────────────────────────────────────────────────────────────┘

┌─ Thunderbolt / USB4 port 2 ─────────────────────────────────┐
│   Powered USB-3 hub, own PSU  (GL3523 / VL817)              │
│   → its internal USB-2 hub = a SEPARATE 480 Mbps domain      │
│                                                              │
│     port 1 ── RTL 83241970  Blog V4   → AIR (airband)        │
│     port 2 ── RTL 56919602  NESDR     → GROUND (TBD, §7.2)   │
│     port 3 ── RTL 61108285  NESDR     → DISCO/ACARS/SURVEY   │
│     port 4 ── (spare: 4th RTL = 3rd P25 or waterfall)        │
│                                                              │
│   3 RTLs ≈ 98 Mbps of ~300 Mbps practical. Comfortable.      │
└──────────────────────────────────────────────────────────────┘

┌─ USB-A ×2 ──────────────────────────────────────────────────┐
│   Keyboard / dummy-HDMI dongle / nothing SDR.                 │
│   Deliberately unused for radio: keeps SDRs off a possibly-   │
│   shared controller, and off the same domain as anything      │
│   hot-plugged.                                                │
└──────────────────────────────────────────────────────────────┘
```

**Why the RSPduo gets its own port, not a hub port:** it's the highest-bandwidth device
(~128 Mbps dual-tuner), it's the one whose dirty-release costs a **reboot**, and it's the one
SDRTrunk holds for days. Give it the shortest, most boring path to the host.

**Chipsets, ranked:**

| Chip | Vendor | Notes |
|---|---|---|
| **GL3523** | Genesys Logic | USB 3.1 Gen1 4-port, multi-TT. Best-supported on macOS; the safe default. |
| **GL3520** | Genesys Logic | USB 3.0 4-port, multi-TT. Older sibling, also fine. |
| **VL817** | VIA Labs | USB 3.1 Gen1 4-port, multi-TT. Solid; common in powered hubs. |
| **VL822** | VIA Labs | USB 3.2 Gen2. More than needed; no downside. |

**Buy criteria, in priority order:** (1) **a real external PSU** ≥3 A — this matters more
than the chip; (2) per-port power switching if available (lets SB3 power-cycle a wedged
NESDR without a human — and §5.4 says that's the recurring failure); (3) one of the chips
above; (4) *not* a monitor/dock hub, which buries the SDRs behind another hop.

**Verify on the box** (Phase 0, read-only): `system_profiler SPUSBDataType` — confirm each
RTL enumerates at **480 Mb/s** (not 12 Mb/s — that would mean a cable/hub fault), confirm the
hub's internal USB-2 hub is on a different controller than the RSPduo, and confirm all four
serials appear.

### 5.4 The known Neptune hardware failure modes (design against these)

Three, all validated, all cheap to design for and expensive to discover:

1. **RSPduo dirty-release → device drops off the USB bus.** `ioreg -p IOUSB -l | grep -c
   180903EF32` = 0. **Reseating does not fix it. Only a reboot does.** Cause: SIGKILL to
   SDRTrunk, or an ungraceful release. **→ `sb3-ctl` must own SDRTrunk's lifecycle and only
   ever SIGTERM + wait 25 s** (§3.5). Check `ioreg` **before** attempting any software
   recovery — if the count is 0, every software remedy is moot and you're just burning time.
2. **Both NESDRs off-bus after reboot.** Recurring. Phase 0 must root-cause: power (hub PSU),
   enumeration order, or the Tahoe USB changes SDRTrunk's README warns about for **RTL**
   dongles (needs the nightly build + `libusb --HEAD`; the SDRplay native path is unaffected).
3. **RSPduo apiService-wedged for SDRangel after SDRTrunk releases it.** Device report shows
   `deviceType:"Unknown"`, `state=error`, all channel power −120/−150 dB. Re-PUT and reseat
   don't fix it; a reboot does. **→ Structural mitigation: never let SDRangel and SDRTrunk
   contend for the RSPduo.** Under this plan they don't — the RSPduo is SDRTrunk-only and all
   three RTLs are SDRangel/disco. **That invariant is worth writing into the broker as a hard
   refusal**, not just a convention.

---

## 6. Phased plan

**Every phase carries a non-regression invariant, stated as something you can verify, not
something you can feel.** A phase is not done until its invariant is demonstrated *and* the
prior phases' invariants still hold.

**The governing invariant, true from Phase 1 onward and never allowed to lapse:**

> **`sb3-ctl kill` → SDRangel and SDRTrunk keep producing audio; every live mount stays 200;
> no config is lost.** Verified at the end of every phase, not just the one that builds it.

---

### Phase 0 — Hardware layer + ground truth

**Nobody writes SB3 code in this phase.**

- Read the **actual RSPduo serial** off Neptune (`SoapySDRUtil --find="driver=sdrplay"` /
  SDRTrunk `View → Tuners`). Rewrite `sdr_fleet_policy.json` → **rev 5.0** to match physical
  truth. Resolve §5.1.
- Root-cause and fix the **two wedged NESDRs**. Power? Enumeration? Tahoe/libusb?
- Install the **powered USB-3 hub**; wire per §5.3.
- `system_profiler SPUSBDataType`: all 4 SDRs, each at 480 Mb/s, controller grouping recorded.
- Read the **exact SDRTrunk tuner-label strings** off `View → Tuners` (§3.5's in-repo
  formats disagree).
- Fix the two stale docs found in this pass: `docs/scan-philadelphia.md` mount names
  (`neptune-digital.mp3` → `neptune-trunk.mp3`) and its "no RSPduo on Neptune" blocker;
  `etc/mac/launchd/neptune/README.md`'s `neptune.mp3`.

**✅ Invariant:** All 4 SDRs enumerate at 480 Mb/s across two USB-2 domains and survive a
reboot. `neptune-angel.mp3` and `neptune-trunk.mp3` are both live and audible on the phone.
`sdr_fleet_policy.json` matches `ioreg`. **Nothing regressed — this phase only adds hardware.**

---

### Phase 1 — Kill-switch scaffolding

**The kill switch is built FIRST, before there's anything to kill. That is deliberate:** it
is the safety harness for every later phase, and building it last is how you find out at
Phase 4 that the boundary was never real.

- `sb3-ctl` skeleton: `status`, `kill`, `resume`, `diff`. **`status` and `kill` are the real
  deliverables**; `resume` is a no-op adopt at this stage (there's no reconciler yet).
- Split `copytoudp-watchdog.sh` (§4.4): **tap-arming stays** as an independent agent that
  survives a kill; **route-restoration** moves into a new (empty) `sb3-reconciler` agent
  that `killed` gates.
- Fail-**closed** sentinel: `$SB3_STATE/killed` missing ≠ permission to reconcile. Positive
  state required. (This inverts the `.sdrangel-restore-paused` bug.)
- `sb3-ctl kill` **verifies mounts stayed 200** and exits non-zero if not.
- Decide the **broker question** (§4.5) with Will and encode the answer.

**✅ Invariant:** `sb3-ctl kill` runs on live Neptune → `neptune-angel.mp3` and
`neptune-trunk.mp3` stay 200 continuously through the kill, with zero dropped frames; SDRangel
channel config is unchanged; SDRTrunk keeps decoding. `sb3-ctl status` reports accurately
while killed. **Phase 0 invariant still holds.**

---

### Phase 2 — One profile end-to-end (Air, camp mode)

- Profile schema v1 (§3.3) + loader + validator. **Hard-fail on bad config** — inherit
  `broker/policy.py`'s stance: *"A broker running on a guessed policy would be worse than no
  broker: it would look like arbitration while enforcing nothing."* Exit 3, no defaults, no
  guessing.
- Translator: profile → SDRangel deviceset (§3.7's exact sequence). All ten landmines
  encoded, including the keepalive injection and the 0→1 tap toggle.
- `route_healthy()` idempotence — **plus `audioDeviceName`**, closing the latent bug.
- Reconciler: 30 s loop, pid-change detection, gated on `killed`.
- Ship `profiles/air-airband-nashville.json` driving RTL `83241970` → `neptune-angel.mp3`.
- Retire `sdrangel-restore.py`'s Neptune route (SB3 now owns it). **Leave the file** — Venus
  still uses it.

**✅ Invariant:** SDRangel airband on RTL `83241970` streams to `neptune-angel.mp3` while the
SB3 profile system runs. Reconciling twice changes nothing (idempotent). `sb3-ctl kill`
mid-stream → **audio continues, unbroken**. `sb3-ctl resume` after Will hand-retunes SDRangel
→ **reports `diverged`, changes nothing.** **Phase 0–1 invariants still hold.**

---

### Phase 3 — Dual digital (SDRTrunk, two P25 systems, one RSPduo)

- Generalize `scripts/ensure-digital-runtime.py` from RTL serials → RSPduo tuner labels
  (§3.5). Do not write a new generator.
- Empty `disabledTuners`; two `<channel>` elements with `preferred_tuner=` on tuner 1 / 2.
- Verify each system's **site span fits one tuner window** (~1.5–2 MHz — the repo disagrees
  with itself; Philadelphia's 8 CCs span 525 kHz and fit either way).
- Resolve **Phase1 vs Phase2 decode config** against a live decode (§3.5).
- CI check: **every system has a catch-all `talkgroupRange` alias with a `broadcastChannel`
  id**, or unaliased talkgroups silently don't stream.
- `sb3-ctl` owns SDRTrunk lifecycle: **SIGTERM only, wait 25 s, `ioreg`-verify the RSPduo
  came back** before relaunch.
- Second mount: `neptune-trunk.mp3` (system 1) + a second stream/mount for system 2.

**✅ Invariant:** Two P25 systems decode concurrently on one RSPduo, each on its own tuner,
each streaming to its own mount, sustained ≥1 h. Airband is **unaffected** — `neptune-angel.mp3`
stays 200 throughout. `sb3-ctl kill` → both digital systems keep decoding and streaming.
**Phase 0–2 invariants still hold.**

---

### Phase 4 — Ground + web UI

- Answer §7.2 (**what is the Ground role?**) and ship `profiles/ground-*.json` on RTL
  `56919602`.
- Web UI on the `macos/scannerctl/` skeleton — Flask, mobile-first, `/api/status` unifying
  SDRangel REST + SDRTrunk log scrape. Steal the **route names** from
  `_canonical_scan_api_path()` (`handlers.py:2660`) — Will's own considered API design,
  already written down.
- Per-role mounts (`neptune-ground.mp3`) with dynamic creation.
- **Recover the two lost icecast properties** (§1.6): `<fallback-mount>` + CORS
  `<http-headers>`.
- **Real health**: tap byte-rate AND mount-200 AND ≥1 real hit in the window (§4.4's trap).
- **Decide on cross-window hunt mode with measurements** (§3.4) — or formally defer it.

**✅ Invariant:** Three analog roles (air, ground, disco-idle) + two digital systems run
concurrently; five mounts live; **CPU headroom ≥30%** (§3.2 landmine 10 — the Intel mini died
at ~420% on the channelizer). UI reflects live backend state, never a cache. `sb3-ctl kill`
→ UI goes away, **all five mounts stay 200.** **Phase 0–3 invariants still hold.**

---

### Phase 5 — disco / ACARS / survey (the shared-dongle role)

- Port `disco`: keep `classifier.py`, replace `sweep.py`'s direct Soapy open with a
  broker-leased SB3-driven capture; `/run/*` → `DISCO_STATE_DIR`; `/bin/systemctl` →
  `ServiceBackend`.
- ACARS/VDL2 → launchd, broker-leased, configurable output paths.
- **Explicit modes** on RTL `61108285` — `disco` | `acars` | `survey`, one at a time,
  broker-enforced (§3.6). **Not a background time-share.**
- Spectrum survey as a disco mode.

**✅ Invariant:** Switching `61108285` between disco/acars/survey never disturbs air, ground,
or digital. The broker denies a second claim by name. `sb3-ctl kill` mid-sweep → the sweep
stops, the lease releases (socket closes), **and every other mount stays 200.**
**Phase 0–4 invariants still hold.**

---

### Phase 6 — Soak + chaos

The phase SB6 planned and **never ran** — `sb7-northstar-program.md` scores it "❌ never run,"
and the whole point of that document is that this is *why* SB6 missed its bar.

- **7 days untouched.** Zero "alive but useless" events. Mounts ≥0.8× configured bitrate for
  95% of the window.
- Injected faults, each producing a **loud structured failure → clean recovery**: yank an
  RTL; SIGKILL SDRangel; SIGTERM SDRTrunk; kill the ffmpeg bridge; kill icecast; fill the
  disk; `sb3-ctl kill` at a random moment.
- **Arm every safety net.** The SB6 lesson, in its own words: *"A safety net that ships
  default-off is a decoration."* SB6 built the source validator, the audio probe, and the
  alert rules — and shipped all three off.

**✅ Invariant:** 7 days, zero unplanned mount outages > 30 s, zero silent failures. Every
injected fault produced a structured diagnostic. **All prior invariants still hold.**

---

## 7. Risks + open questions

### 7.1 Must be answered before Phase 1

**Q1 — Which RSPduo is physically on Neptune?** 🔴 **Blocking.**
The fleet policy says `1809063632`; a validated decode two days later says `180903EF32`
(§5.1). Everything digital keys off this. **Read it off the box. Rewrite the policy to rev
5.0.** Cost of getting it wrong: Phase 3 builds against a device that isn't there.

**Q2 — Does the broker stay up through `sb3-ctl kill`?** 🔴 **Blocking, Will's call.**
§4.5. I recommend **yes** — reservations outlive the control plane, and the disco/ACARS/survey
collision is a genuine lease problem. But the broker's remaining job shrinks a lot when
SDRangel/SDRTrunk open devices themselves, and there's a defensible argument for retiring it
to a static policy file. **This is a real fork, not a formality.**

**Q3 — What is the "Ground" role?** 🟡 **Blocking Phase 4, not Phase 1.** §7.2.

**Q4 — Is the FreqScanner REST gap still real?** 🟡
`docs/scan-philadelphia.md` says 7.25.1 can't set the freq list over REST. **Check the
installed SDRangel version and its Swagger UI at `http://<host>:8091/api/`.** If it's been
fixed upstream, hunt mode gets much easier. If not, §3.4's multiplex-only recommendation
stands and cross-window hunt is a Phase 4 measurement problem. **This is the single biggest
unknown in the plan** — it's the difference between "SB3 is a scanner" and "SB3 is a very good
multi-channel receiver."

### 7.2 What does Will mean by "Ground"?

**Flagged in the brief as TBD, and it genuinely is — the repo offers four different answers:**

| Candidate | Evidence | Band |
|---|---|---|
| **NFM ground/mil-air**, the chirp `ground` band | `chirp/config/ground.json`: center **138.05 MHz**, NFM, 64 channels | ~136–174 |
| **FRS/GMRS road scanner** | Already built and working on Neptune RTL `83241970` — 15 NFMDemods @ 462.450 center. The brief says "replacing the current ad-hoc SDRangel + FRS/GMRS setup." | 462/467 |
| **MURS / 2m / 70cm ham** | `analog_scanlists.json:ground_reference` — *"NOT currently deployed"*, MURS + FRS/GMRS + 2m simplex | mixed |
| **SKYWARN 2m** | Neptune's *current* live SDRangel route: 147.360 Philly SKYWARN, 146.520, 147.030 | ~147 |

**These need different centers and don't share a window.** 138 MHz vs 147 MHz vs 462 MHz is
three separate profiles, not one role — and no single RTL covers them simultaneously.

**My read of the brief:** "replacing the current ad-hoc SDRangel + FRS/GMRS setup" strongly
suggests **FRS/GMRS is the thing being *replaced by* SB3**, i.e. it becomes a proper profile.
And SKYWARN is what Neptune runs *today*. So Ground is plausibly **"FRS/GMRS + SKYWARN, as
switchable profiles on `56919602`"** — which the profile system handles natively (one role,
several profiles, one active).

**But this is inference and I'm not going to build on it.** ❓ **Will: does "Ground" mean
(a) NFM ground/mil-air ~138 MHz like the old chirp band, (b) FRS/GMRS road scanner, (c)
SKYWARN/ham 2m, or (d) "a switchable NFM role" that covers all of them as profiles?** If (d),
Phase 4 is a profile-authoring exercise and the design is already done.

### 7.3 Risk register

| # | Risk | Sev | Mitigation |
|---|---|---|---|
| 1 | **FreqScanner not REST-settable** → real hunt mode is hard | 🔴 | Multiplex-within-window covers most roles (§3.4). Cross-window = Phase 4 measurement. Q4 first. |
| 2 | **SDRangel is crash-prone under REST** — bulk channel ops crash it; config lives in RAM; reverts to a stale plist | 🔴 | One-at-a-time + 0.4 s delays + idempotent reconcile. **Proven in `sdrangel-restore.py` — copy it, don't reinvent.** |
| 3 | **RSPduo dirty-release → reboot** | 🔴 | `sb3-ctl` owns SDRTrunk lifecycle: SIGTERM only, 25 s wait, `ioreg` verify. Never SIGKILL. |
| 4 | **CPU** — the Intel mini hit ~420% on SDRangel's channelizer and **killed SDRTrunk**. Neptune is M1/8 GB. | 🔴 | Phase 4 invariant = ≥30% headroom. Measure per-phase, not at the end. Sample rates are a budget. |
| 5 | **Mount-200 is a lie** — ffmpeg encodes silence at full bitrate; the keepalive channel *guarantees* non-silence | 🟠 | Verify tap bytes AND mount AND ≥1 real hit/window (§4.4). |
| 6 | **Fleet policy contradicts reality** (§5.1) | 🟠 | Phase 0 rev 5.0. Policy is code — `broker/policy.py` hard-fails on it. |
| 7 | **Both NESDRs wedged after reboot** | 🟠 | Phase 0 root-cause. Per-port-switched hub as a lever. |
| 8 | **Tahoe USB + RTL** — SDRTrunk's README wants the nightly + `libusb --HEAD` on recent macOS. SDRplay's native path is unaffected. | 🟠 | Phase 0 verifies all 3 RTLs at 480 Mb/s post-reboot. |
| 9 | **Two P25 on one RSPduo is unproven here** — the clean dual-tuner result was on `180903EF32`, and the *simultaneous* two-system case has never run | 🟠 | Phase 3 = the proof. Site spans verified first. Fall back to one system + `preferred_tuner`. |
| 10 | **Phase1-vs-Phase2 decode config** (§3.5) | 🟡 | Live decode. The "metadata-shows-but-audio-silent" symptom is already documented with its remedy. |
| 11 | **Unaliased talkgroups silently don't stream** | 🟡 | Catch-all alias + CI check in the generator. |
| 12 | **disco/ACARS/survey can't share one dongle** (§3.6) | 🟡 | Explicit broker-enforced modes. Not a time-share. |
| 13 | **Icecast on Mac lost fallback-mount + CORS** (§1.6) | 🟡 | Phase 4 restores both. |
| 14 | **Two "tuner broker"s** — `broker/` vs `scripts/tuner_broker.py`, unrelated, different schemas | 🟡 | Delete the script in Phase 0. Name collisions cost hours. |
| 15 | **`sdrangel_client.py` is unvalidated** — its own docstring says so; calls `GET /devicesets` where the working code reads the instance root | 🟡 | `sdrangel-restore.py` is the authority. Validate against live Swagger before trusting any field name. |
| 16 | **Repo lives at a TCC-protected path.** `~/Documents` hangs headless launchd processes on the consent check with no UI to answer. | 🟡 | Deploy base is `~/scannerproject/`, never the git clone. Already established convention — keep it. |

### 7.4 What this plan deliberately does not do

- **Does not port chirp.** Will's call. ~8k lines retire. The two genuinely portable pieces —
  `cluster_planner.py` (pure function) and `lo_scheduler.py` (pure state machine) — get
  lifted into the control plane *if* cross-window hunt survives Phase 4.
- **Does not port `ui/`.** 10k lines in one handler class, three overlapping UIs, a
  hand-rolled WebSocket. `macos/scannerctl/` is the seed instead.
- **Does not touch Venus.** `sdrangel-restore.py` keeps its Venus routes and keeps running
  there. Neptune's route retires from it in Phase 2. Venus is the control group — if SB3
  breaks Neptune, Venus proves it was SB3.
- **Does not build a second-box witness.** Descoped by PO call 2026-07-04.
- **Does not run any of this.** This document is research. Phase 0 is the first thing that
  touches hardware.

---

## 8. Cross-references

| Source | What it gives |
|---|---|
| `docs/macos-backend-migration-scope.md` @ `05e5dcd` | The original decision + the two mobile paths (thin UI / Claude-drives-the-box) |
| `docs/sb7-northstar-program.md` | "No third state"; the flaw classes; why SB6 missed its bar. **The most important prior art here.** |
| `docs/scan-philadelphia.md` | Philly P25 + the FreqScanner-REST gap. ⚠️ stale mounts + stale RSPduo blocker |
| `macos/bin/sdrangel-restore.py` | **The prototype for SB3's translator.** Authority on real REST shapes. |
| `macos/bin/copytoudp-watchdog.sh` | The tap-vs-route split (§4.4) |
| `macos/clients/sdrtrunk_client.py` | The best statement of the SDRangel/SDRTrunk asymmetry |
| `scripts/ensure-digital-runtime.py` | The playlist generator to generalize, not rewrite |
| `scripts/build_sugar_tree_playlist.py` | Worked two-tuner `preferred_tuner` example |
| `etc/mac/sdr_fleet_policy.json` | Device map. ⚠️ **contradicts reality — rev 5.0 in Phase 0** |
| `macos/data/analog_scanlists.json` | Profile ancestor + the densest SDRangel operational notes in the repo |
| `broker/` | The device-lease design. Survives unchanged. |
| memory `reference_two_box_audio_harness` | Every copyToUDP/keepalive/orphan-ffmpeg landmine |
| memory `project_neptune_philly_p25_validated` | The RSPduo reboot rule + the serial contradiction |
| memory `project_airband_rf_collapse_recurring` | Why §3.3 stores native gain units |
