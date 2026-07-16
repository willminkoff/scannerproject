# SB3 on Neptune — Architecture Plan

**Author:** Claude (research pass), for Will Minkoff (PO)
**Date:** 2026-07-16
**Status:** DESIGN / RESEARCH. Nothing built, nothing deployed. No box was touched to write this.
**Branch:** `sb3-neptune-plan` (off `origin/main` @ `e2dbb48`)
**Updated 2026-07-16 (serials):** Will confirmed the RSPduo serials — **Neptune =
`180903EF32`, Venus = `1809063632`** (§5.1). `etc/mac/sdr_fleet_policy.json` rev 4.1 has them
**reversed** and is wrong until fixed separately; cleanup survey in §7.5.
**Updated 2026-07-16 (decisions):** ① **`sb3-ctl kill` is a FULL teardown** — controller,
broker, orchestration, all of it; only SDRangel + SDRTrunk survive (§4). ② **Disco moves to a
HackRF One** (✅ on hand), freeing RTL `61108285` for ACARS-only — 5 SDRs now, and the
three-way dongle collision is gone (§3.6, §3.7, §5). ③ **Ground = "anything not Airband and not
digital"** — the catch-all analog role on `56919602`, as switchable sub-profiles (§3.9).
**All role-level and hardware-availability questions are now closed** (§7.1); every remaining
open item is a Phase 0 measurement.

**Method note.** This is a repo-only pass. No REST calls were made against SDRangel or
SDRTrunk on Neptune, Venus, or BreakroomDe; no box state was read or changed. Every claim
below is sourced to a file, a commit, or the brief. Where the repo contradicts itself — and
it does, in three load-bearing places — the contradiction is flagged rather than resolved by
guessing. Those flags are the Phase 0 worklist.

---

## 0. The thesis, in one page

Will wants Neptune (M1 Mac mini, macOS) to become a full SB3 host: **five SDRs**, dual
concurrent P25, airband, a ground role, disco/survey/ACARS, a web UI, icecast per-role
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

Will's 2026-07-16 call sharpens that to its strongest form: **`sb3-ctl kill` tears down the
entire SB3 layer — controller, broker, orchestration — and only SDRangel + SDRTrunk survive,
holding their last-applied config** (§4). No half-alive layer, no policy daemon enforcing
rules for an absent owner. **SB3 owns the SB3 layer; when SB3 leaves, it leaves.** That is the
same "no third state" discipline the SB7 north star is named for, applied to the control plane
itself.

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

**Where that bites is now precise, and it's narrower than it first looked.** With Ground
defined (§3.9), the two analog roles turn out to be opposites:

- **Air fits one window.** Nashville airband is 118.400–119.450 = **1.05 MHz** inside a ~2 MHz
  tuner. Every channel demodulates in parallel — no scanning, no hopping, **no FreqScanner
  dependency at all.** It is strictly better than the chirp design it replaces, which could
  only pass one open channel at a time.
- **Ground is "anything not Air and not digital"** — ~144 to ~470 MHz of territory against
  that same ~2 MHz window. It is **two orders of magnitude too big to multiplex**, so Ground
  sub-profiles are **switchable, not concurrent**, and the wide ones are exactly where
  cross-window hunting is unavoidable.

So the FreqScanner gap costs Air nothing, costs the tight Ground sub-profiles (WX/NOAA,
FRS/GMRS-462, local public-safety clusters) nothing, and costs the wide Ground sub-profiles
(ham 2m, 70cm, mil UHF AM) real capability. **That is a Phase 4 question with a Phase 4
answer** — not a foundation crack. Ship what fits one window first; let the wide bands force
the measurement.

**As of 2026-07-16 every decision this plan needed is made.** Serials confirmed, kill-switch
scope settled, disco's radio chosen and in hand, Ground defined. **What's left is measurement,
not deliberation** — the SDRangel version's REST surface, and Neptune's USB controller map
(§7.6). Phase 0 takes both.

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
| `disco/` (sweep → classify) | ~3k | ❌ **Linux-only today**, + retargeting to HackRF | Two hard blockers: `/run/*` paths everywhere (`sweep.py:20-21`, `classifier.py:136`), and `disco/src/listen.py:124` `SYSTEMCTL_BIN = "/bin/systemctl"`. Env-overridable but nothing in `etc/mac/` sets them; no launchd plist exists. **`classifier.py` is keep-as-is; `sweep.py` moves RTL→HackRF** (§3.6, §3.7). |
| ACARS / VDL2 | — | ❌ **Linux-only** | systemd units only (`systemd/acarsdec.service`, `dumpvdl2.service`). Write JSON to `/run/`, UI tails it. No launchd plist. §3.6. |
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
// chirp/config/airband.json — the real thing, abridged.
// NB: serial 1809063632 here is the MICRO's airband RSPduo (Linux era) — historically
// correct, unrelated to the Neptune/Venus assignment in §5.1. Not a reversal artifact.
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
// (serial = the Micro's airband RSPduo, Linux era. See §5.1 for the Neptune/Venus map.)
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
        ┌────────────────────────────────────────────────────────────────┐
        │  SB3 LAYER  —  dies ENTIRELY on `sb3-ctl kill`  (§4)           │
        │                                                                │
 profiles/*.json ─▶│ loader → translator → reconciler │  sb3-ui         │
        │          │ tuner-broker (leases · policy)   │                 │
        │          │ disco classifier · ACARS · survey │                │
        └──────┬─────────────────────┬─────────────────────┬────────────┘
               │ REST :8091          │ playlist XML +      │ SoapyHackRF /
               │ (live, idempotent)  │ launchctl (restart) │ hackrf_sweep
               ▼                     ▼                     ▼
     ┌───────────────┐      ┌────────────────┐    ┌──────────────┐
  ┌──│   SDRangel    │      │    SDRTrunk    │──┐ │  HackRF One  │ disco +
  │  │   (analog)    │      │  (digital P25) │  │ │ 1 MHz–6 GHz  │ survey
  │  └───────┬───────┘      └────────┬───────┘  │ │  20 MHz IBW  │ (no audio)
  │ RTL×3    │ copyToUDP             │ native   │ └──────────────┘
  │ air      ▼                       │ icecast  │  RSPduo 180903EF32
  │ ground  udp:9998 → ffmpeg        │ broadcast│  dual-tuner: P25 ×2
  │ acars*   │                       │          │
  └──────────┴───────────┬───────────┴──────────┘   * ACARS bypasses SDRangel:
                         ▼                            acarsdec/dumpvdl2 direct
             icecast :8000  (dynamic mounts)
    /neptune-angel.mp3  /neptune-trunk.mp3  /neptune-<role>.mp3
                         │
                         ▼   ◀── survives SB3 death. THIS IS THE INVARIANT.
                       phone
```

**Read the boundary as a contract:** everything below the REST/playlist line keeps running when
everything above it is dead — **including the broker.** SB3 owns the SB3 layer; when SB3
leaves, it leaves entirely (§4.2, Will's call 2026-07-16).

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
3. **The tuner-label string is inconsistent in-repo — in BOTH format and serial** — and must
   be read off the live `View → Tuners` list:
   - `build_sugar_tree_playlist.py`: `"RSPduo SER:180903EF32 Tuner 1"` — ✅ right serial
   - `macos/sdrtrunk/tuner_configuration.json:73`: `"RSPduo Tuner 1 SER#1809063632"` —
     ❌ **wrong serial** (§7.5), *and* a different format.

   This one is functional, not cosmetic: `preferred_tuner` matches on this exact string, so a
   wrong serial means no tuner match and the dual-digital pinning silently fails. **Fix it as
   part of the §7.5 cleanup, then verify the format against the live list.**

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

### 3.6 disco, ACARS, and the survey role — the HackRF resolves the collision

**Both are keep-the-logic, replace-the-plumbing:**

| | Keep | Replace |
|---|---|---|
| **disco** | `classifier.py` (heuristic v0 / ONNX, + ULS/CDBS/HPDB/bandplan enrichment), `training/` | `sweep.py`'s direct `SoapySDR.Device()` → **SoapyHackRF**, broker-leased (§3.7); `/run/*` → `DISCO_STATE_DIR`; `/bin/systemctl` → `ServiceBackend` |
| **ACARS/VDL2** | `ui/wxdata.py` (1,872 lines of decode/enrichment) | systemd units → launchd plists, broker-leased; `/run/*.json` → configurable paths |

**The classifier never touches an SDR** — it consumes `.iq.f32` slice files with a 6-field
filename schema. That's a clean seam: **SB3 owns capture, disco owns classification** — and it
is what makes the RTL→HackRF swap a capture-layer change rather than an ML change.

#### The collision is gone — and that's the real win of the HackRF decision

The prior revision of this doc flagged a **three-way collision** on RTL `61108285`: disco,
ACARS/VDL2, and survey all wanted one dongle, and they cannot coexist. ACARS/VDL2 are
*continuous* by nature — a decoder listening 20% of the time misses 80% of the messages —
while disco and survey want to sweep everywhere. The only answer available was time-slicing
via a broker-enforced mode switch, i.e. **choosing which capability to lose at any moment.**

**Adding the HackRF dissolves that**, and this is a bigger deal than "disco gets a better
radio":

| | Before (3 roles, 1 RTL) | After (HackRF added) |
|---|---|---|
| RTL `61108285` | disco **or** ACARS **or** survey — pick one | **ACARS/VDL2 only, continuous, 24/7** |
| disco + survey | time-sliced against ACARS | **HackRF, always available** |
| Mode switching | required, lossy | **none — no mode concept needed** |
| `scripts/tuner_broker.py` lineage | would have been resurrected | **stays retired** |

**→ The `disco | acars | survey` mode switch is CANCELLED.** Nothing has to be chosen against
anything. ACARS/VDL2 run continuously on `61108285` (~131 MHz and ~136.8 MHz — close enough
that a single wideband capture could plausibly feed both decoders, worth testing in Phase 5);
disco and survey share the HackRF, and *those* two genuinely are the same activity — a sweep
that feeds the classifier is a survey, and a survey with the classifier attached is disco.
**One consumer, two output modes** is a much smaller thing to build than a three-way arbiter.

The broker still leases both devices one-consumer-per-serial — the fleet policy note stands:
*"None of the sounding decoders are broker-integrated yet — wiring the claim-by-serial call in
is a prerequisite before any of them go live."* That work is unchanged; there's just no longer
a contention problem underneath it.

### 3.7 disco on the HackRF — module design

**Device:** Great Scott Gadgets **HackRF One**. 1 MHz – 6 GHz, up to 20 Msps, 8-bit ADC,
half-duplex, USB 2.0 High Speed.

#### Why this is the right radio for disco

- **20 MHz instantaneous bandwidth** vs the RTL's ~2.4–3.2 MHz usable. disco's
  `cluster_planner`-style problem — cover a band by hopping a narrow window — largely
  evaporates: **6–8× fewer dwells for the same coverage.**
- **6 GHz ceiling** vs the RSPduo's 2 GHz and the R820T2's ~1.766 GHz. This is spectrum disco
  literally cannot see today: 2.4 GHz ISM, 5 GHz, most modern telemetry.
- **`hackrf_sweep` is a first-class asset.** GSG ships a dedicated firmware sweep mode that
  covers **1 MHz–6 GHz in about a second**. Nothing in the RTL world compares. For the survey
  role this is not an incremental improvement; it is a different capability.

#### ⚠️ Correcting the rationale: the 8-bit ADC is *not* the tradeoff vs the RTL

The brief notes "8-bit ADC is the tradeoff — acceptable for wide-band classification vs
sensitivity work." **The conclusion is right; the mechanism isn't.** The RTL2832U is *also*
8-bit. Bit depth is a wash — **it is not a regression from the device the HackRF replaces.**
(Against the RSPduo's 14-bit it *is* a real ~36 dB dynamic-range loss, but the RSPduo is
digital-only and never in this path.)

**The real HackRF tradeoffs, worth designing for:**

1. **Noise figure / sensitivity.** This is the actual cost. The HackRF's front end is flat and
   wideband with no LNA by default; the RTL-SDR Blog V4's R828D has a far better NF and a
   built-in LNA. **Expect weaker weak-signal performance** — the brief's instinct ("acceptable
   for wide-band classification vs sensitivity work") is exactly right, just for this reason
   rather than bit depth. **→ Budget an external LNA + the HackRF's bias-tee (`-p 1`) if
   weak-signal classification matters.**
2. **No TCXO by default** (±20 ppm vs the Blog V4's 1 ppm). At 6 GHz that's ±120 kHz of drift.
   Fine for "is there energy here and what shape is it," bad for anything narrowband or
   frequency-precise. **→ Record measured centre, never assume commanded centre.** A GSG
   TCXO board is a cheap fix if it bites.
3. **8-bit + 20 MHz wide = no per-channel AGC.** One strong emitter in the window sets the
   gain floor and desensitizes everything else in it — the classic wideband capture problem.

#### 8-bit dynamic range → classification confidence

This is the one that touches the ML, and it needs to be designed in rather than discovered:

- 8 bits ≈ **48 dB** theoretical SFDR. In a 20 MHz window with a strong local emitter, weak
  signals can land at or below the effective noise floor — **not because they're absent, but
  because the strong one ate the range.**
- The classifier was trained/tuned on **RTL-sourced slices at ~2.4 Msps**. HackRF slices at
  20 Msps with a different front end are **a distribution shift**: different noise floor,
  different NF, different decimation history. **Do not assume the ONNX model transfers.**
- **→ Design requirement:** every slice carries its **measured SNR and the window's peak-to-
  noise ratio** in its metadata, and `classifier.py` **gates confidence on them**. A
  low-confidence classification from a range-starved capture must be reported as
  low-confidence, not as a negative. This is the `icecast_sink` silence-lie (§4.4) wearing a
  different hat: **an instrument that can't see must say so, not report "nothing there."**
- The slice filename schema is already 6 fields (`{tuner}_{freq}_{bw}_{rate}_{ts}_{uid}`) and
  carries `rate` — so RTL-era and HackRF-era slices are **distinguishable on disk.** Use that:
  keep the heuristic v0 path as the HackRF default until the ONNX model is re-tuned on HackRF
  slices (`disco/training/finetune_real.py` exists for exactly this).

#### Capture path — SoapyHackRF, with `hackrf_sweep` as a second mode

| Mode | Tool | Use |
|---|---|---|
| **disco** (classify) | **SoapyHackRF** via `SoapySDR.Device("driver=hackrf,serial=…")` | Matches `sweep.py`'s existing `SoapySDR.Device()` call site — **a driver-string change, not a rewrite.** Emits `.iq.f32` slices exactly as today. |
| **survey** (spectrum) | **`hackrf_sweep`** subprocess | 1 MHz–6 GHz in ~1 s. Power-vs-frequency only, no IQ — feeds the spectrum view, not the classifier. |

**Recommendation: SoapyHackRF for the disco path.** `disco/src/sweep.py:328` already does
`SoapySDR.Device(soapy_args)` and builds the args string at `:492` — swapping
`driver=rtlsdr,serial=…` for `driver=hackrf,serial=…` is the smallest possible change, and it
keeps the slice contract with the classifier intact. `hackrf_transfer` would mean re-plumbing
capture around a subprocess for no gain. **Note SoapyHackRF must be installed into radioconda's
`modules0.8` path** — the same `SOAPY_SDR_PLUGIN_PATH` gotcha the chirp plists document.

**Sweep pattern:** with 20 MHz windows, prefer **fewer, wider dwells** over the RTL's many
narrow ones — but do not run 20 Msps continuously (§5.2: it is at the USB-2 ceiling). Start at
**8–10 Msps sustained** for the classify path and reserve 20 Msps for `hackrf_sweep`'s bursty
retuning survey. Measure before widening.

**Retune settling matters more than it did.** The HackRF's synthesizer needs time after a
retune, and `hackrf_sweep`'s speed comes from accepting dirty edges. For classify-path slices,
**discard the first samples after each retune** or the classifier trains on transients.

### 3.8 Concrete example: "SB3 Profile: Air-Airband-Nashville" → SDRangel

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

### 3.9 The Ground role — DEFINED 2026-07-16, and its fundamental constraint

> ## **Ground = "anything not Airband and not digital."** — Will, 2026-07-16
>
> The catch-all analog role. Public safety VHF/UHF · ham 2m/70cm · marine VHF · business
> VHF/UHF · mil AM UHF ground crew · FRS/GMRS · MURS · WX/NOAA · whatever else turns up.
> **RTL NESDR `56919602` owns it.**

That is a clean definition — Ground is defined by **exclusion**, which makes it open-ended by
design and means the profile system must accommodate bands nobody has thought of yet. Good.
But it collides with one hard physical fact, and that collision is the whole design:

> ### ⚠️ Ground profiles are SWITCHABLE, not concurrent.
>
> `56919602` at 2.048 Msps sees a **~2 MHz instantaneous window** (~2.4 MHz at the RTL's
> practical 2.4 Msps ceiling). Ground's territory spans **~144 MHz to ~470 MHz** — and mil AM
> UHF reaches 400 MHz. That is **two orders of magnitude more spectrum than the window.**
>
> **Air can be one big multiplex. Ground fundamentally cannot.**

Air works as a single camp-mode multiplex because the whole role fits one window: Nashville
airband is 118.400–119.450 = **1.05 MHz**, comfortably inside 2 MHz, so every channel is
demodulated in parallel (§3.4). **Ground has no such luck.** One tuner, one centre, one window
at a time → **one Ground sub-profile active at a time, switched by user selection or profile
priority.**

#### Sub-profiles, and why the band name doesn't decide the mode

```
profiles/ground-nashville-publicsafety-vhf.json    role: ground   } one at a time
profiles/ground-nashville-ham-2m.json              role: ground   } switched by
profiles/ground-frs-gmrs.json                      role: ground   } selection or
profiles/ground-wx-noaa.json                       role: ground   } priority
profiles/ground-marine-vhf.json                    role: ground   }
profiles/ground-mil-uhf-am.json                    role: ground   }  ← note: AM, not NFM
```

**The naïve assumption — "a sub-profile is a band" — is wrong, and it's worth killing now.**
Most named Ground bands **do not fit one window** either:

| Sub-profile | Band span | Fits ~2 MHz? |
|---|---|---|
| WX/NOAA | 162.400–162.550 = **150 kHz** | ✅ trivially |
| FRS/GMRS **462 only** | 462.5500–462.7250 = **175 kHz** | ✅ (proven on Neptune — 15 NFMDemods) |
| FRS/GMRS **462 + 467** | 462.55–467.7125 = **5.16 MHz** | ❌ needs 2 windows |
| MURS | 151.820–154.600 = **2.78 MHz** | ❌ *barely* misses |
| Ham 2m | 144–148 = **4 MHz** | ❌ |
| Marine VHF | 156.05–162.025 = **~6 MHz** | ❌ |
| Public safety VHF | 150.8–162 = **11.2 MHz** | ❌ |
| Public safety UHF | 450–470 = **20 MHz** | ❌ |
| Ham 70cm | 420–450 = **30 MHz** | ❌ |
| **Mil UHF AM ground** | 225–400 = **175 MHz** | ❌❌ |

**→ A Ground sub-profile is defined by its ACTUAL CHANNEL LIST, not by a band.** In practice a
locality's channels of interest cluster tightly — Nashville public-safety VHF might be eight
channels inside 154.0–155.5 (1.5 MHz), which fits fine even though "public safety VHF" as a
band does not. **Whether a sub-profile is camp or hunt is a property you compute, not one you
declare:** run `cluster_planner.plan_clusters()` (§3.4) over the channel list and see how many
clusters come back. One cluster → camp. More than one → hunt.

**The profile schema already handles this** — §3.3's `channels[]` is a list of actual
frequencies, and `mode: camp | hunt` is the answer the planner gives, not an author's guess.
**The loader should compute it and reject a profile whose declared mode contradicts the
planner**, rather than trusting the label. That is `broker/policy.py`'s "don't run on a guess"
stance applied to profiles.

**Ground is multi-modal, not just multi-band.** Mil UHF ground crew (225–400) is **AM**;
everything else here is **NFM**. §3.3's per-profile `demod` block covers it — but it means
"the Ground demod type" is not a thing, and any code that assumes NFM for Ground is wrong.

#### ⚠️ Ground is where §3.4's hunt-mode risk actually lands

This is the most important consequence, and it reframes the plan's biggest open risk:

- **Air never needed hunt mode.** It fits one window. The FreqScanner-not-REST-settable
  problem (§3.4, risk 1) costs Air nothing.
- **Ground needs hunt mode for anything wider than a tight local cluster** — and it is exactly
  the role with no upper bound on span.

**→ Phase 4 is where "can SB3 hunt across windows?" stops being theoretical.** Sequencing that
holds: ship the sub-profiles that **fit one window first** (WX/NOAA, FRS/GMRS-462, tight local
public-safety clusters) — all pure camp mode, all working with what Phase 2 already built —
and let the wide ones (ham 2m, 70cm, mil UHF AM) force the cross-window question with real
measurements behind it. **Most of what Will actually listens to is probably in the first
group**, which is why this ordering is a real strategy and not a stall.

#### Switching cost is real — budget it

A Ground switch is not free: re-PATCH the centre, delete N channels, add M channels — **one at
a time with ~0.4 s delays** (§3.2 landmine 4) — then verify. For 15 channels that is **6+
seconds** of teardown/rebuild, during which `neptune-ground.mp3` has nothing to say.

- **Fine for user-initiated switching.** Will picks a sub-profile; six seconds is nothing.
- **NOT fine as an automatic scan mechanism.** Anything that switches sub-profiles on a timer
  is rebuilding chirp's LO scheduler out of the slowest possible primitive.
- **The keepalive channel matters more here than anywhere** (§3.2 landmine 7): a mount that
  goes silent for 6 s during a rebuild will 404 on icecast's source timeout. **Add the
  keepalive channel FIRST in the rebuild sequence, remove it LAST.**

---

## 4. Kill switch design

### 4.1 The principle

> **Manual override has always won. Encode that, don't fight it.**
>
> **And SB3 owns the SB3 layer — when SB3 leaves, it leaves entirely.** (Will, 2026-07-16.)

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

### 4.2 State ownership — the boundary (DECIDED 2026-07-16)

> **Will's call: `sb3-ctl kill` is a FULL teardown of the SB3 layer — controller, broker,
> orchestration, all of it. Only SDRangel + SDRTrunk stay up, holding their last-applied
> config. SB3 owns the SB3 layer; SB3 leaves.**

This supersedes the earlier recommendation to keep the broker up (§4.5). **Will's call is the
better design, and it's worth saying why rather than just recording it:**

- **The broker was protecting nothing during a kill.** SDRangel and SDRTrunk are GUI apps
  launched by their own LaunchAgents — **they never held leases.** They open devices directly.
  Every actual lease-holder (disco, ACARS, survey, VFO) is SB3-owned and goes down in the kill
  anyway. A live broker with zero consumers and nothing to arbitrate is a daemon holding a
  lock nobody is contending for.
- **A half-alive SB3 layer is a third state**, and the whole program is named for not having
  one (`sb3-northstar`: *"The daemon is either producing real audio from real samples, or it
  has stopped cleanly with a structured diagnostic. There is no third state."*). "SB3 is
  killed but its broker is still up refusing claims" is exactly that third state: a component
  enforcing policy on behalf of a control plane that isn't there.
- **The devices are protected by construction anyway.** While SB3 is gone, SDRangel and
  SDRTrunk already hold their devices *open* — the OS refuses a second open. The broker's real
  value was ordering and rate-limiting **restarts**, and while SB3 is killed there are no SB3
  restarts to order.

```
┌─────────────────────────────────────────────────────────────────────┐
│  THE SB3 LAYER — everything here dies on `sb3-ctl kill`             │
│                                                                     │
│   sb3-ctl · sb3-reconciler · sb3-ui                                 │
│   tuner-broker  (leases, policy, flocks)                            │
│   profile registry + bindings          [on disk, inert while dead]  │
│   disco · ACARS · survey  (lease consumers)                         │
│   route-restoration half of copytoudp-watchdog                      │
└─────────────────────────────────────────────────────────────────────┘
                        ╳  kill severs here  ╳
┌─────────────────────────────────────────────────────────────────────┐
│  THE BACKEND — untouched, keeps producing audio                     │
│                                                                     │
│   SDRangel      devicesets · channels · gain · center · copyToUDP   │
│                 (in RAM — last-applied config, still running)       │
│   SDRTrunk      playlist · aliases · streams · live decode          │
│   icecast       mounts (dynamic)                                    │
│   ffmpeg bridge · tap-arming watchdog · LaunchAgents                │
└─────────────────────────────────────────────────────────────────────┘
```

| State | Owner | On `sb3-ctl kill` |
|---|---|---|
| Profile definitions, bindings, active set | **SB3** | ✅ on disk, **inert** |
| disco classifications, ACARS messages, hit log | **SB3** | ✅ on disk; **collection stops** |
| Web UI | **SB3** | ❌ **gone — by design** |
| Reconciler | **SB3** | ❌ **gone — by design** |
| **Device leases, policy, flocks** | **SB3 (broker)** | ❌ **gone — by design (Will, 2026-07-16)** |
| Deviceset config, channels, gain, center | **SDRangel (RAM)** | ✅ **untouched, still running** |
| copyToUDP tap | **SDRangel (RAM)** | ✅ **untouched** |
| Tap-arming watchdog | **backend plumbing** (§4.4) | ✅ **stays up** |
| Playlist, aliases, streams, live decode | **SDRTrunk (disk+RAM)** | ✅ **untouched** |
| ffmpeg bridge, icecast, mounts | **launchd (independent agents)** | ✅ **untouched** |

**The line is clean because SB3 never holds audio state.** It only *asserts* state onto
backends that then hold it themselves. Kill the asserter and the assertions stand. That is why
this plan is safe to attempt — and it should be protected deliberately, not relied on by luck.

**The one honest cost of the full teardown:** while SB3 is dead, nothing refuses a stray
process that tries to open the RSPduo — and the SIGKILL→dirty-release→**reboot** hazard (§5.4)
is real. In practice SDRTrunk already holds the device open, so a second open fails at the OS
layer before it can do damage. **Accepted risk, and the right trade:** the alternative was a
policy daemon enforcing rules for an absent owner.

### 4.3 Command surface

```
sb3-ctl status              # what SB3 thinks; what the backends actually report; the diff
sb3-ctl kill                # full SB3 teardown. Backends untouched. Audio continues.
sb3-ctl resume              # bring SB3 back; adopt LIVE backend state
sb3-ctl apply <profile>     # one-shot translate+verify, then exit (works while killed)
sb3-ctl diff                # dry run: what would resume change? Prints, changes nothing.
```

**`kill` — ordering matters, and lease consumers must go before the broker:**

```
1. touch $SB3_STATE/killed                          fail-CLOSED sentinel (§4.4)
2. bootout  sb3-reconciler, sb3-ui                  stop asserting first
3. bootout  disco, acars, survey                    LEASE CONSUMERS BEFORE THE BROKER.
                                                    Each runs under `broker.client run`, so a
                                                    clean stop closes the socket = clean
                                                    release. Killing the broker first would
                                                    yank the socket out from under a live
                                                    child — undefined, and exactly the churn
                                                    the broker exists to prevent.
4. bootout  tuner-broker                            SB3's last process. Flocks release on
                                                    process death; lock FILES persist by
                                                    design (leases.py:489-503 — never
                                                    unlinked, to avoid split-inode races).
5. LEAVE RUNNING, always:
     com.scannerproject.sdrangel            ← holds its devices open
     com.scannerproject.sdrtrunk            ← holds the RSPduo, keeps decoding
     com.scannerproject.icecast
     com.scannerproject.neptune-audio-bridge
     com.scannerproject.copytoudp-watchdog  ← tap-arming half only (§4.4)
6. VERIFY every mount still 200. Report. Exit non-zero if any dropped.
```

**Step 6 is the point.** A kill switch that doesn't verify the invariant it exists to protect
is a wish. `sb3-ctl kill` must *prove* audio survived before it reports success.

**Step 3 is the one that will bite if skipped.** `broker/client.py` holds the lease socket for
its child's entire lifetime; the broker dying underneath it is a case nobody has specified.
Stop the children first and the question never arises.

### 4.4 Resume adopts live state — never a snapshot

**The invariant:** `resume` must read what SDRangel/SDRTrunk are *actually doing right now*
and reconcile forward from there. It must never replay a snapshot taken at `kill` time.

Why this is non-negotiable: **the entire reason Will kills the orchestrator is to change
things by hand.** A resume that restores a pre-kill snapshot would clobber precisely the work
the kill existed to enable. That is the `sdrangel-restore.py` bug, reintroduced with extra
steps.

**Resume is the kill sequence in reverse: the broker comes back FIRST** (it is SB3's
foundation — leases must exist before any consumer claims one), and observation happens before
any assertion.

```
sb3-ctl resume:
  1. bootstrap tuner-broker
       - loads sdr_fleet_policy.json (rev 5.0 — hard-fails on a bad policy, exit 3)
       - rebuilds its ledger FROM EMPTY. There is no lease state to recover:
         the ledger was always derived, and `clear_locks_at_boot: true` already
         encodes "a fresh broker trusts nothing it didn't grant itself."
       - stale lock FILES from the last run are expected and harmless — the
         flock, not the file, is the mutex (leases.py:454-503).

  2. assert static reservations for backend-owned devices:
       RSPduo 180903EF32 -> sdrtrunk     |  these two are held OPEN by apps the
       RTL   83241970    -> sdrangel-air |  broker does not supervise. Reserving
       RTL   56919602    -> sdrangel-ground   them stops disco/survey/ACARS from
                                              ever claiming a device already in use.

  3. OBSERVE, do not assert:
       GET /sdrangel + GET /audio           → live SDRangel truth
       read SDRTrunk playlist from disk     → live SDRTrunk truth

  4. for each bound profile:
       route_healthy(profile, live)?
         ✅ → adopt. Log "adopted live state, no change."
         ❌ → the profile and reality have DIVERGED.
              Default: DO NOT CLOBBER. Mark the profile `diverged`,
              surface the diff, reconcile nothing.
              Only `sb3-ctl apply <profile> --force` re-asserts.

  5. bootstrap lease consumers (disco / acars / survey) per the active profile set
  6. rm $SB3_STATE/killed
  7. bootstrap sb3-reconciler, sb3-ui
```

**Steps 3–4 before 7 is the whole design.** The reconciler must not start until divergence has
been observed and classified — otherwise it wakes up, sees a mismatch, and clobbers the human's
work in the first 30-second tick. That is precisely the `sdrangel-restore.py` failure, and
starting the reconciler one step too early reintroduces it perfectly.

**Nothing about the full-teardown decision makes resume harder.** The broker's state was always
derived, never authoritative — it is rebuilt from the policy file plus live claims, and it
already clears locks at boot. That is why the teardown is cheap: **there is no broker state to
lose, because the broker never had any that mattered.**

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

### 4.5 The broker question — DECIDED: full teardown (reasoning trail)

✅ **ANSWERED 2026-07-16 — Will's call: the broker goes down with everything else.** Recorded
in §4.2, which is now the authority. This subsection is kept as the reasoning trail.

**The question was:** under an SDRangel/SDRTrunk backend, what does the broker still
arbitrate — and is that worth keeping alive while SB3 is dead?

**What it still arbitrates (during normal operation):** less than it did, but not nothing.
chirp — its original consumer — retires. SDRangel and SDRTrunk never held leases; they are GUI
apps that open devices directly. That leaves **disco, ACARS, survey, and VFO** as the real
lease consumers. With disco moving to its own HackRF (§3.6), the old three-way RTL collision
mostly dissolves, but the broker still enforces one-consumer-per-serial across the fleet and
rate-limits restarts. **It earns its keep while SB3 is running.**

**What it arbitrates during a kill:** *nothing.* Every lease consumer is SB3-owned and already
stopped. The devices that remain in use are held open by apps the broker doesn't supervise.

**Why the earlier recommendation (keep it up) was wrong:** it treated the broker's static
reservations as protection, but a reservation only binds processes that *ask* — and while SB3
is dead, the only things touching radios are SDRangel and SDRTrunk, which never ask. It bought
no safety and cost a third state. **Will's teardown is cleaner: the SB3 layer is either fully
up or fully down.** Reservations are re-asserted on resume (§4.4 step 2), where they actually
constrain someone.

---

## 5. Hardware / USB topology for Neptune

### 5.1 What Neptune has — serials CONFIRMED by Will, 2026-07-16

> ## ✅ **Neptune RSPduo = `180903EF32`.  Venus RSPduo = `1809063632`.**
>
> **Confirmed by Will, 2026-07-16.** This is the authoritative assignment for this plan.
> It matches the validated Philly decode (`project_neptune_philly_p25_validated`,
> 2026-07-10) and the live Venus SDRangel airband route (`sdrangel-restore.py:35`).

**`etc/mac/sdr_fleet_policy.json` rev 4.1 has the two serials reversed and must be treated as
WRONG until it is fixed separately** (§7.5). It asserts that `180903EF32` left the M1 and that
`1809063632` stayed to carry digital. **Both halves are backwards.** Everything that copied
from rev 4.1 inherited the error.

How the evidence lines up, now that the answer is known:

| Source | Date | Claim | Verdict |
|---|---|---|---|
| `project_neptune_philly_p25_validated` (memory) | 2026-07-10 | Neptune decodes Philly P25 on RSPduo **`180903EF32`** | ✅ **right** |
| `macos/bin/sdrangel-restore.py:35` (Venus `AIRBAND` route) | live | Venus airband = SDRplayV3 **`1809063632`** | ✅ **right** |
| `macos/bin/sdrangel-restore.py:66` (Neptune comment) | live | *"RSPduo 180903EF32 is owned by SDRTrunk (P25), not SDRangel"* | ✅ **right** |
| `etc/mac/sdr_fleet_policy.json` rev 4.1 | 2026-07-08 | `180903EF32` was *"physically UNPLUGGED and taken to a DIFFERENT computer"*; the M1 keeps `1809063632` | ❌ **reversed** |
| `docs/scan-philadelphia.md` | ~2026-07 | *"No RSPduo on Neptune"* — only 2 RTLs | ❌ **stale** (predates the move) |

**The live code was right the whole time; only the policy file was wrong.** That's a useful
signal about which artifacts to trust: `sdrangel-restore.py` runs every 10 minutes on both
boxes and would have failed loudly if its serials were wrong. The policy file is read by the
broker and by humans, and nothing was exercising it hard enough to catch the reversal.

**No Phase 0 serial-reading gate is needed** — the question is answered. Phase 0 still rewrites
the policy to **rev 5.0** (§7.5), but as a *correction to a known-wrong file*, not an
investigation.

**The role map — 5 SDRs, updated 2026-07-16 (HackRF added):**

| # | Device | Serial | Role | Engine | Bandwidth |
|---|---|---|---|---|---|
| 1 | **RSPduo** | `180903EF32` | **Dual digital** — P25 sys 1 (T1) + sys 2 (T2) | SDRTrunk (native API) | ~128 Mbps |
| 2 | **RTL-SDR Blog V4** | `83241970` | **Air** (airband AM) | SDRangel | ~33 Mbps |
| 3 | **RTL NESDR** | `56919602` | **Ground** — *anything not Air, not digital* (§3.9). Switchable sub-profiles, one at a time | SDRangel | ~33 Mbps |
| 4 | **RTL NESDR** | `61108285` | **ACARS/VDL2 only** — freed from disco | acarsdec + dumpvdl2 | ~33 Mbps |
| 5 | **HackRF One** 🆕 | *(32-hex — read at Phase 0)* | **Disco + spectrum survey**. ✅ on hand | SoapyHackRF / `hackrf_sweep` | **~160–320 Mbps** |

**What changed from fleet policy 4.1:**

| Serial | Policy 4.1 role | **Now** | Δ |
|---|---|---|---|
| `83241970` | chirp-airband (was VFO) | Air / airband | same intent |
| `56919602` | sounding (ACARS/VDL2/sonde) | **Ground** (catch-all analog, §3.9) | ⚠️ changed |
| `61108285` | chirp-ground | **ACARS/VDL2 only** | ⚠️ changed (was "disco+ACARS+survey" — HackRF freed it, §3.6) |
| *(new)* | — | **HackRF → disco + survey** | 🆕 **new device** |

**The RTL serials were never in doubt** — the reversal was RSPduo-only. `83241970`,
`56919602`, and `61108285` are consistent across the policy, the brief, and the live scripts.

**VFO has no device in this map.** SB7 gave it `83241970` (the Blog V4), which is now Air. VFO
is not in Will's 5-SDR brief, and the SB7 VFO work is `ecb276b`-blocked on an upstream
SoapyRTLSDR segfault anyway. **Treating it as retired-with-chirp unless Will says otherwise**
— noting SDRangel's own UI is a perfectly good manual VFO, which may be why it stopped mattering.

**HackRF serials** are 32-hex-char strings (not the short RTL/RSP form). Read it at Phase 0 —
`hackrf_info` or `SoapySDRUtil --find="driver=hackrf"` — and pin the policy to it, per the
fleet-wide `"address_by": "serial"` invariant. A single HackRF works fine addressed as
`driver=hackrf` alone, but **don't** — that's the habit that produced the serial-collision bug
(`project_ground_nfm_serial_collision`).

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

1. **Controller separation.** A USB 2.0 high-speed segment is a **shared 480 Mbps domain, and
   the domain belongs to the host CONTROLLER.** The fix for contention is *fewer devices per
   controller*. **This — not TT count — was the Micro's problem**
   (`project_sb6_session_2026_06_18_evening`: 10 SDRs, single xHCI, single 480M domain), and
   the 2026-06-19 retraction is explicit: *"the fix is a powered MULTI-TT USB-3 hub, NOT a
   USB-3 re-cable — RSPduo is USB-2."*
2. **Power.** `powered_hubs_required: true` is in the fleet policy for a reason. Several RTLs
   at ~300 mA each, plus a HackRF at up to ~500 mA, exceeds what a bus-powered hub delivers —
   and brownout presents as intermittent USB errors, i.e. as a *software* bug, for hours.

#### ⚠️ Correction to this doc's previous revision

The first revision of §5.2 said a USB-3 hub "contains a separate internal USB-2 hub, so
plugging it into a USB-3/TB port gives its USB-2 devices their own 480 Mbps domain." **That is
wrong and I'm retracting it.** A USB-3 hub *is* logically two hubs (a USB-3 hub and a USB-2
hub on separate wires) — but the USB-2 half is still **a hub on the host controller's bus**,
not a new bus. It does not manufacture bandwidth.

> **Only a new host CONTROLLER creates a new 480 Mbps domain.** A hub never does.

This is exactly what the Micro's topology proves: the fix there wasn't hubs, it was **five
xHCI controllers** — onboard PCH, two Titan Ridge TB3, and two Fresco FL1100 in the OWC dock.
On a Mac, extra controllers arrive over **Thunderbolt** (a TB dock tunnels PCIe and presents
its own xHCI), never over a plain USB hub.

#### ⚠️ The brief's "M1's 4 USB-2 domains (3 onboard + 2 OWC dock)" is a different machine

The 5-controller topology in `reference_micro_access_and_hpdb_migration` is the **2018 Intel
Mac mini** (host `ScannerBox`, Ubuntu) — the PCI addresses give it away: `00:14.0` Intel
Cannon Lake PCH, `08:00.0`/`7e:00.0` Titan Ridge, `44:00.0`/`45:00.0` Fresco FL1100. **An M1
Mac has no Cannon Lake PCH and no PCI bus enumerated that way.** That topology cannot be
carried over to Neptune by assumption. (The figure is also internally inconsistent — 3 + 2 = 5,
not 4.)

**What Neptune actually has, per the repo** (`sb7-northstar-program.md:27`): *"Mac mini 2021 |
Apple M1, 8 GB RAM, 2× TB/USB4 + 2× USB-A."* Realistically that is **one USB-2 domain shared
by the two USB-A ports**, plus whatever each Thunderbolt port's attached device brings. **Not
5 domains. Probably 1, expandable to 3.** ⚠️ **Unverified — Phase 0 measures it with
`system_profiler SPUSBDataType`.**

**The good news: Will already owns the fix.** The **OWC TB3 dock** (2× Fresco FL1100 = 2
independent xHCI controllers) is the exact hardware that de-stacked the Micro. TB3 docks work
on M1. If it's free now that ScannerBox is out of this arrangement, moving it to Neptune buys
the two extra controllers this layout needs — **for zero dollars.** ❓ **Is the OWC dock
available?** (§7.6)

#### Bandwidth math — 5 SDRs

```
RTL-SDR @ 2.048 Msps × 2 bytes (8-bit I + 8-bit Q)  =  4.10 MB/s  ≈   33 Mbps  (each)
3 RTLs (air + ground + ACARS)                       = 12.3  MB/s  ≈   98 Mbps
RSPduo dual-tuner @ 2 Msps × 2 tuners × 2 bytes     = 16    MB/s  ≈  128 Mbps
HackRF @ 20 Msps × 2 bytes (8-bit I + 8-bit Q)      = 40    MB/s  ≈  320 Mbps  ⚠️
HackRF @ 10 Msps                                    = 20    MB/s  ≈  160 Mbps
                                                      ────────────────────────
practical USB-2 high-speed ceiling (bulk)             ~35-40 MB/s ≈ 280-320 Mbps
```

> ### ⚠️ The HackRF at 20 Msps is AT the USB-2 ceiling, not comfortably under it
>
> 320 Mbps of a ~280–320 Mbps practical bulk ceiling is **100%+ utilization.** This is a
> well-known HackRF characteristic: 20 Msps works on a clean, dedicated bus and drops samples
> the moment anything else shares it — or sometimes anyway. **The brief's instinct that the
> HackRF "needs its own bus" is exactly right, and stronger than it sounds: it needs its own
> CONTROLLER, and even then 20 Msps has no headroom.**
>
> **→ Design for 8–10 Msps sustained** on the disco classify path (160–200 Mbps ≈ 60% — a real
> margin), and reserve 20 Msps for `hackrf_sweep`, whose retune-heavy duty cycle never sustains
> peak anyway. **Dropped samples are the silent-failure mode here** — they corrupt classifier
> input without erroring, which is §3.7's confidence problem arriving through the USB stack.
> Instrument the drop counter and surface it.

**Five SDRs is still an easier problem than the Micro's ten** — but the HackRF alone is worth
~3× a dual-tuner RSPduo in bus terms, so it is not "just one more radio."

### 5.3 Recommended topology — 5 SDRs, 3 controllers

**The requirement: 3 independent USB-2 domains.** The three high-bandwidth consumers cannot
share:

| Domain | Device(s) | Load | of ~300 Mbps |
|---|---|---|---|
| **A** | HackRF (disco/survey) — **alone, non-negotiable** | 160–320 Mbps | **55–100%** |
| **B** | RSPduo `180903EF32` (dual digital) — alone | ~128 Mbps | ~43% |
| **C** | 3 RTLs (air + ground + ACARS) on a powered hub | ~98 Mbps | ~33% |

Neptune natively supplies roughly **one** (the shared USB-A pair). Thunderbolt supplies the
rest — via a dock with its own xHCI, per §5.2.

#### Preferred — using the OWC TB3 dock Will already owns (❓ §7.6)

```
┌─ Thunderbolt / USB4 port 1  →  OWC TB3 dock ────────────────────────┐
│   (2× Fresco FL1100 = TWO independent xHCI controllers)             │
│                                                                     │
│   ├─ Fresco controller #1 ── HackRF One          → DISCO + SURVEY   │
│   │                          ALONE on this domain. 160–320 Mbps.    │
│   │                          Nothing else here. Ever.               │
│   │                                                                 │
│   └─ Fresco controller #2 ── RSPduo 180903EF32   → DUAL DIGITAL     │
│                              ALONE. ~128 Mbps. Dirty-release =      │
│                              reboot (§5.4) → shortest boring path.  │
└─────────────────────────────────────────────────────────────────────┘

┌─ USB-A  →  powered USB-3 hub, own PSU (GL3523 / VL817) ────────────┐
│   (one host controller — the hub does NOT add a domain, §5.2)       │
│                                                                     │
│     port 1 ── RTL 83241970  Blog V4  → AIR      (~33 Mbps)          │
│     port 2 ── RTL 56919602  NESDR    → GROUND   (~33 Mbps, §3.9)    │
│     port 3 ── RTL 61108285  NESDR    → ACARS/VDL2 (~33 Mbps)        │
│     port 4 ── spare (4th RTL → 3rd P25, or waterfall)               │
│                                                                     │
│   ≈98 Mbps of ~300. Comfortable — these three genuinely can share.  │
└─────────────────────────────────────────────────────────────────────┘

┌─ Thunderbolt port 2 ── free (headroom / 2nd dock / display) ───────┐
└─────────────────────────────────────────────────────────────────────┘
```

#### Fallback — no OWC dock

```
TB port 1 ── HackRF direct (USB-C→micro-B)   → own controller, if the TB ports
TB port 2 ── RSPduo direct                     don't share one. ⚠️ VERIFY — this
USB-A     ── powered hub → 3 RTLs              is the assumption to break first.
```

⚠️ **If the two TB ports turn out to share a USB controller, this fallback fails** — the
HackRF and RSPduo would collide at ~450 Mbps on one domain. Then a TB dock (or a second) is a
**hard requirement**, not a nice-to-have. `sb7-northstar-program.md:227` already flagged the
sibling question for USB-A: *"if the two USB-A ports share one controller, consider a second
hub."* **Phase 0 answers it.**

**Why the HackRF is alone and never shares:** at 20 Msps it is ~100% of a domain by itself;
even at 10 Msps it is ~55%. Adding an RTL to its domain is how you get dropped samples — and
dropped samples don't error, they just quietly corrupt what the classifier sees (§3.7).

**Chipsets, ranked** (for the RTL hub — the only place a hub belongs here):

| Chip | Vendor | Notes |
|---|---|---|
| **GL3523** | Genesys Logic | USB 3.1 Gen1 4-port, multi-TT. Best-supported on macOS; the safe default. |
| **GL3520** | Genesys Logic | USB 3.0 4-port, multi-TT. Older sibling, also fine. |
| **VL817** | VIA Labs | USB 3.1 Gen1 4-port, multi-TT. Solid; common in powered hubs. |
| **VL822** | VIA Labs | USB 3.2 Gen2. More than needed; no downside. |

**Buy criteria, in priority order:** (1) **a real external PSU** ≥3 A — matters more than the
chip; (2) per-port power switching if available (lets SB3 power-cycle a wedged NESDR without a
human — §5.4 says that's the recurring failure); (3) one of the chips above; (4) *not* a
monitor/dock hub, which buries the SDRs behind another hop.

**Remember what a hub can and can't do (§5.2):** the hub gives you **ports and power**. It does
**not** give you bandwidth. Only a Thunderbolt-attached controller does.

**Verify on the box** (Phase 0, read-only): `system_profiler SPUSBDataType` —
1. All **5** SDRs appear, each at **480 Mb/s** (12 Mb/s = cable/port fault — the
   `VDL2A001` failure mode already recorded in `reference_micro_access_and_hpdb_migration`).
2. **Count the controllers and map device→controller.** This is the measurement the whole
   layout depends on, and the one nobody has taken on Neptune.
3. Confirm HackRF and RSPduo are on **different** controllers, and that neither shares with
   the RTL hub.

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
   contend for the RSPduo.** Under this plan they don't — the RSPduo is SDRTrunk-only, the
   RTLs are SDRangel (air, ground) and acarsdec/dumpvdl2 (`61108285`), and disco is on the
   HackRF. **No device has two possible owners.** That invariant is worth writing into the
   broker as a hard refusal, not just a convention.

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

- Rewrite `sdr_fleet_policy.json` → **rev 5.0**: Neptune RSPduo = `180903EF32` (confirmed
  2026-07-16, §5.1). The rev-4.1 reversal is a known defect, not an open question. Then work
  the **serial-reversal cleanup list** in §7.5 — the policy is the root, but seven artifacts
  copied from it. `broker/policy.py` hard-fails on a bad policy, so this gates Phase 1.
- One-line confirmation on the box while you're there (cheap, closes it for good):
  `SoapySDRUtil --find="driver=sdrplay"` on each mini, or SDRTrunk `View → Tuners`.
- Root-cause and fix the **two wedged NESDRs**. Power? Enumeration? Tahoe/libusb?
- **MEASURE THE CONTROLLER MAP** — `system_profiler SPUSBDataType`. All **5** SDRs at 480 Mb/s;
  count xHCI controllers; record device→controller. **This is the measurement the layout
  depends on and nobody has taken on Neptune** (§5.2 — the 5-domain figure in circulation is
  the *Intel* box's).
- **Do the two TB ports share a USB controller?** If yes, the no-dock fallback in §5.3 is dead
  and a TB dock becomes mandatory. Answer this before buying anything (§7.6).
- **Is the OWC TB3 dock available?** (§7.6) It's the 2 controllers this layout wants, already
  owned.
- Install the **powered USB-3 hub** for the 3 RTLs; wire per §5.3.
- Read the **exact SDRTrunk tuner-label strings** off `View → Tuners` (§3.5's in-repo
  formats disagree).
- Fix the two stale docs found in this pass: `docs/scan-philadelphia.md` mount names
  (`neptune-digital.mp3` → `neptune-trunk.mp3`) and its "no RSPduo on Neptune" blocker;
  `etc/mac/launchd/neptune/README.md`'s `neptune.mp3`.

**HackRF bring-up** — ✅ **the device is on hand (Will, 2026-07-16); no procurement gate.**
This is hardware *integration*, and it sits at the bottom of Phase 0 because it's the one piece
with no prior art on this box:

- **Verify it enumerates:** `hackrf_info` round-trip on Neptune — board ID, firmware version,
  part ID, and the **serial**. If `hackrf_info` can't see it, nothing downstream matters.
  (`ioreg -p IOUSB` first if it's missing — same discipline as the RSPduo, §5.4: a device off
  the bus makes every software remedy moot.)
- **Pin the serial into the policy** — 32 hex chars, unlike the RTL/RSP short form. Never
  address it as bare `driver=hackrf` (§5.1), even though a single HackRF would work that way.
  That shortcut is how `project_ground_nfm_serial_collision` happened.
- **Install SoapyHackRF.** Try `brew install soapyhackrf` first (a Homebrew formula exists;
  `libhackrf` is definitely available) — **but Homebrew's SoapySDR and radioconda's are
  different installs with different module paths**, and disco runs under radioconda. If the
  brew route doesn't land the module where radioconda's SoapySDR looks, **build from source
  against radioconda's SoapySDR** and install into its `modules0.8`. Same pattern as
  SoapySDRPlay3 (`sb7-northstar-program.md`: *"SoapySDRPlay3 self-built against the universal
  SDRplay 3.15 API"*).
- **Confirm SoapySDR sees it through the right plugin path:**
  `SOAPY_SDR_PLUGIN_PATH=/opt/scannerproject/radioconda/lib/SoapySDR/modules0.8 SoapySDRUtil --find="driver=hackrf"`.
  ⚠️ **A HackRF that `hackrf_info` finds but SoapySDR doesn't is the failure mode to expect** —
  it's the exact `SOAPY_SDR_PLUGIN_PATH` gotcha the chirp plists document ("radioconda's
  SoapySDR has a compiled-in module path that doesn't survive relocation"). Catching it here
  costs an hour; catching it at Phase 5 costs a day of misdiagnosis.
- **First `hackrf_sweep` capture** — end-to-end I/Q proof. A short sweep across a band with
  known occupancy (FM broadcast is the honest choice: unmissable, and if you *don't* see it the
  problem is real). This closes the loop: device → libusb → capture → data on disk.
- **Sustained-rate bench:** capture at **8 / 10 / 16 / 20 Msps** for 60 s each on its own
  controller and **record the drop counter at each.** This sets disco's sample rate with a
  number instead of a guess (§5.2's 320 Mbps-vs-~300 Mbps ceiling), and it will never be
  cheaper to measure than right now, with nothing else competing for the bus.

**✅ Invariant:** All **5** SDRs enumerate at 480 Mb/s and survive a reboot, with **HackRF and
RSPduo on separate xHCI controllers** and neither sharing with the RTL hub — measured, not
assumed. **The HackRF answers `hackrf_info`, is visible to radioconda's SoapySDR as
`driver=hackrf,serial=<32-hex>`, and has produced one real `hackrf_sweep` capture**; its
sustained clean rate is a recorded number. `neptune-angel.mp3` and `neptune-trunk.mp3` are both
live and audible on the phone. `sdr_fleet_policy.json` rev 5.0 matches `ioreg` + the measured
topology, and no artifact in §7.5 still claims Neptune's RSPduo is `1809063632`.
**Nothing regressed — this phase only adds hardware.**

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
- **Full teardown, in the §4.3 order** — consumers → broker last. Even with no lease consumers
  built yet, get the ordering right now; it is the part that's hard to retrofit.
- **`resume` brings the broker back FIRST** and observes before asserting (§4.4).

**✅ Invariant:** `sb3-ctl kill` runs on live Neptune → **the broker and every SB3 process are
gone** (`launchctl print` confirms), while `neptune-angel.mp3` and `neptune-trunk.mp3` stay 200
continuously through the kill, with zero dropped frames; SDRangel channel config is unchanged;
SDRTrunk keeps decoding. `sb3-ctl status` reports accurately while killed. `sb3-ctl resume`
rebuilds the broker ledger from the policy and adopts live state without clobbering.
**Phase 0 invariant still holds.**

---

### Phase 2 — One profile end-to-end (Air, camp mode)

- Profile schema v1 (§3.3) + loader + validator. **Hard-fail on bad config** — inherit
  `broker/policy.py`'s stance: *"A broker running on a guessed policy would be worse than no
  broker: it would look like arbitration while enforcing nothing."* Exit 3, no defaults, no
  guessing.
- Translator: profile → SDRangel deviceset (§3.8's exact sequence). All ten landmines
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

- **Ground is defined (§3.9)** — this is now profile authoring, not discovery. Ship
  `profiles/ground-*.json` on RTL `56919602`, **fits-one-window sub-profiles first**: WX/NOAA
  (150 kHz), FRS/GMRS-462 (175 kHz — already proven on Neptune as 15 NFMDemods), and tight
  local public-safety clusters. All pure camp mode on Phase 2's machinery.
- **Sub-profile switching** — one Ground profile active at a time, user-selected or by
  priority. Budget the rebuild cost (~6 s for 15 channels at 0.4 s/channel) and **add the
  keepalive channel FIRST, remove it LAST**, or the mount 404s mid-switch (§3.9).
- **Profile loader computes camp-vs-hunt** by running `plan_clusters()` over the channel list,
  and **rejects a profile whose declared mode contradicts the planner** (§3.9). Don't trust
  the label.
- **The wide Ground sub-profiles (ham 2m, 70cm, mil UHF AM) are what force Q4** — cross-window
  hunt. Answer it here with measurements, or formally defer them.
- Web UI on the `macos/scannerctl/` skeleton — Flask, mobile-first, `/api/status` unifying
  SDRangel REST + SDRTrunk log scrape. Steal the **route names** from
  `_canonical_scan_api_path()` (`handlers.py:2660`) — Will's own considered API design,
  already written down.
- Per-role mounts (`neptune-ground.mp3`) with dynamic creation.
- **Recover the two lost icecast properties** (§1.6): `<fallback-mount>` + CORS
  `<http-headers>`.
- **Real health**: tap byte-rate AND mount-200 AND ≥1 real hit in the window (§4.4's trap).
- **Decide on cross-window hunt mode with measurements** (§3.4) — or formally defer it.

**✅ Invariant:** Two analog roles (air, ground) + two digital systems run concurrently; four
mounts live; **CPU headroom ≥30%** (§3.2 landmine 10 — the Intel mini died at ~420% on the
channelizer). UI reflects live backend state, never a cache. `sb3-ctl kill` → **UI and broker
both go away, all four mounts stay 200.** **Phase 0–3 invariants still hold.**

---

### Phase 5 — disco on the HackRF + ACARS on its own RTL

**No mode switch to build.** The HackRF dissolved the three-way collision (§3.6) — this phase
is now two independent, concurrent things instead of one arbiter.

**disco + survey → HackRF:**
- Port `disco`: keep `classifier.py` untouched; swap `sweep.py:328`'s
  `SoapySDR.Device("driver=rtlsdr,serial=…")` → `driver=hackrf,serial=…` (§3.7 — a driver
  string, not a rewrite). `/run/*` → `DISCO_STATE_DIR`; `/bin/systemctl` → `ServiceBackend`.
  Broker-leased.
- Sample rate = **the number Phase 0 measured**, not 20 Msps by default (§5.2).
- Discard post-retune transients before slicing (§3.7).
- **Slice metadata carries measured SNR + window peak-to-noise**, and `classifier.py` gates
  confidence on them (§3.7). **This is the phase's real design work** — everything else is
  plumbing.
- **Keep heuristic v0 as the HackRF default.** The ONNX model was tuned on RTL slices at
  2.4 Msps; HackRF slices are a distribution shift. Re-tune via `disco/training/finetune_real.py`
  on HackRF-sourced slices before trusting it. The 6-field filename schema already carries
  `rate`, so the two eras are distinguishable on disk — use it.
- `hackrf_sweep` as the **survey** mode: 1 MHz–6 GHz power-vs-frequency, feeds the spectrum
  view, not the classifier.

**ACARS/VDL2 → RTL `61108285`, continuous:**
- systemd → launchd, broker-leased, configurable output paths.
- **No time-slicing. No mode. Runs 24/7** — which is the only way ACARS is worth running.
- Worth testing: ~131 MHz and ~136.8 MHz are close enough that one wideband capture might feed
  both decoders. If it works, that's a spare RTL. If not, they time-share *within* the sounding
  role — a much smaller problem than the old three-way.

**✅ Invariant:** disco sweeps on the HackRF **while** ACARS decodes continuously on
`61108285` **while** air, ground, and both digital systems run — **five SDRs, all concurrent,
nothing time-sliced against anything.** No dropped-sample warnings from the HackRF at the
configured rate. The broker denies a second claim by name. `sb3-ctl kill` mid-sweep → sweep
stops, ACARS stops, **broker stops**, and **every mount stays 200**. **Phase 0–4 invariants
still hold.**

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

### 7.1 Must be answered before Phase 1 — ✅ ALL CLOSED

> **Every question in this section is answered.** Nothing blocks Phase 1. What remains open in
> this document is **§7.4 (deliberate non-goals)**, **§7.6 (two Phase 0 measurements)**, and
> Q4 below — which is a *measurement*, not a decision, and which Phase 0 takes with a browser.

**Q1 — Which RSPduo is physically on Neptune?** ✅ **ANSWERED 2026-07-16 — Will confirmed:
Neptune = `180903EF32`, Venus = `1809063632`.**
This matched the validated Philly decode and the live `sdrangel-restore.py` routing. The
`sdr_fleet_policy.json` rev 4.1 serial reversal is now a **known defect with a cleanup list**
(§7.5), not an open question. Phase 3 builds against `180903EF32`.

**Q2 — Does the broker stay up through `sb3-ctl kill`?** ✅ **ANSWERED 2026-07-16 — no. Full
teardown.** Will's call: SB3 owns the SB3 layer, and the broker is part of it. My earlier
recommendation (keep it up) was wrong — it protected nothing during a kill and cost a third
state. Recorded in §4.2; reasoning trail in §4.5.

**Q3 — What is the "Ground" role?** ✅ **ANSWERED 2026-07-16 — "anything not Airband and not
digital."** The catch-all analog role on RTL `56919602`. Full design in §3.9; the summary and
what it changed in §7.2. **No role-level unknowns remain in the plan.**

**Q5 — Is the HackRF on hand?** ✅ **ANSWERED 2026-07-16 — yes.** No procurement gate. Phase 0
does bring-up (`hackrf_info` → SoapyHackRF → first `hackrf_sweep` → rate bench), not
acquisition.

**Q4 — Is the FreqScanner REST gap still real?** 🟡 **The one open item — and it is a
measurement, not a decision.**
`docs/scan-philadelphia.md` says 7.25.1 can't set the freq list over REST. **Check the
installed SDRangel version and its Swagger UI at `http://<host>:8091/api/`.** If it's been
fixed upstream, cross-window hunt gets much easier. If not, §3.4's multiplex-only
recommendation stands.

**§3.9 sharpened what this actually costs**, and it's less than the last revision implied:
**Air never needed hunt mode** (118.400–119.450 = 1.05 MHz, one window, pure camp). **Ground
is the only role that needs it** — and even there, the fits-one-window sub-profiles (WX/NOAA,
FRS/GMRS-462, tight local public-safety clusters) work on what Phase 2 already builds. So Q4
doesn't gate Phase 1, 2, or 3; it gates *how far Ground reaches* in Phase 4. **Still the
biggest unknown — it's the difference between "SB3 is a scanner" and "SB3 is a very good
multi-channel receiver" — but it is now a well-fenced one.**

### 7.2 What does Will mean by "Ground"? — ✅ ANSWERED 2026-07-16

> **Ground = "anything not Airband and not digital."** Public safety VHF/UHF, ham 2m/70cm,
> marine VHF, business VHF/UHF, mil AM UHF ground crew, FRS/GMRS, MURS, WX/NOAA, and whatever
> else turns up. RTL NESDR `56919602` owns it.

**Full design in §3.9.** It is the broadest of the four candidates this section previously
guessed at — closest to option (d), "a switchable role covering all of them as profiles" —
with two corrections to that guess:

1. **It's defined by exclusion, so it's open-ended.** Not a fixed list of bands; anything
   analog that isn't Air lands here. The profile system has to accommodate bands nobody has
   named yet.
2. **It isn't NFM-only.** Mil UHF ground crew (225–400 MHz) is **AM**. "The Ground demod type"
   does not exist.

**The consequence is a real design constraint, not a formality:** Ground's territory spans
~144–470 MHz against a ~2 MHz window, so **Ground sub-profiles are switchable, not
concurrent** — Air can be one big multiplex, Ground fundamentally cannot (§3.9). And Ground is
where the FreqScanner/hunt-mode risk (§3.4, risk 1) actually bites, since Air never needed
cross-window hunting and Ground has no upper bound on span.

**No longer blocking.** Phase 4 is now a profile-authoring exercise plus one honest
measurement, and §3.9 sequences it: ship the fits-one-window sub-profiles first (WX/NOAA,
FRS/GMRS-462, tight local clusters — all pure camp mode on what Phase 2 already builds), let
the wide ones force the cross-window question with data behind it.

### 7.3 Risk register

| # | Risk | Sev | Mitigation |
|---|---|---|---|
| 1 | **FreqScanner not REST-settable** → cross-window hunt is hard | 🟠 *(was 🔴)* | **Downgraded now that Ground is defined (§3.9):** Air fits one window and never needs it; tight Ground sub-profiles don't either. Only the WIDE Ground sub-profiles (ham 2m, 70cm, mil UHF AM) are exposed. Multiplex-within-window (§3.4) + ship-narrow-first sequencing. Q4 measures it in Phase 0. |
| 1b | 🆕 **Ground can't multiplex** — ~144–470 MHz of role against a ~2 MHz window; sub-profiles are switchable, not concurrent (§3.9). A ~6 s rebuild per switch, during which the mount can 404. | 🟠 | Switchable sub-profiles, user-selected. Keepalive channel added FIRST / removed LAST in the rebuild. **Never switch on a timer** — that's rebuilding chirp's LO scheduler out of the slowest primitive available. |
| 1c | 🆕 **"A Ground sub-profile is a band" is false** — most named bands don't fit one window (MURS 2.78 MHz, ham 2m 4 MHz, PS-UHF 20 MHz, mil UHF AM 175 MHz). Only actual channel *clusters* fit. | 🟡 | Sub-profiles are channel lists; the loader runs `plan_clusters()` and computes camp-vs-hunt rather than trusting the declared mode (§3.9). |
| 1d | 🆕 **Ground is multi-modal** — mil UHF ground crew (225–400) is **AM**, everything else NFM. Any code assuming "Ground = NFM" is wrong. | 🟡 | Per-profile `demod` block (§3.3) already covers it. Don't add a role-level demod default. |
| 2 | **SDRangel is crash-prone under REST** — bulk channel ops crash it; config lives in RAM; reverts to a stale plist | 🔴 | One-at-a-time + 0.4 s delays + idempotent reconcile. **Proven in `sdrangel-restore.py` — copy it, don't reinvent.** |
| 3 | **RSPduo dirty-release → reboot** | 🔴 | `sb3-ctl` owns SDRTrunk lifecycle: SIGTERM only, 25 s wait, `ioreg` verify. Never SIGKILL. |
| 4 | **CPU** — the Intel mini hit ~420% on SDRangel's channelizer and **killed SDRTrunk**. Neptune is M1/8 GB. | 🔴 | Phase 4 invariant = ≥30% headroom. Measure per-phase, not at the end. Sample rates are a budget. |
| 5 | **Mount-200 is a lie** — ffmpeg encodes silence at full bitrate; the keepalive channel *guarantees* non-silence | 🟠 | Verify tap bytes AND mount AND ≥1 real hit/window (§4.4). |
| 6 | **Fleet policy has the RSPduo serials reversed**, and 7 artifacts copied the error (§5.1, §7.5) | 🟠 | Phase 0 rev 5.0 + the §7.5 cleanup list. Policy is code — `broker/policy.py` hard-fails on it, so a wrong serial there is a Phase 1 boot failure, not a silent drift. |
| 7 | **Both NESDRs wedged after reboot** | 🟠 | Phase 0 root-cause. Per-port-switched hub as a lever. |
| 8 | **Tahoe USB + RTL** — SDRTrunk's README wants the nightly + `libusb --HEAD` on recent macOS. SDRplay's native path is unaffected. | 🟠 | Phase 0 verifies all RTLs at 480 Mb/s post-reboot. **The HackRF is libusb-based too** — same exposure, verify it in the same pass. |
| 8b | 🆕 **HackRF at 20 Msps = ~100% of a USB-2 domain.** Dropped samples don't error; they silently corrupt classifier input. | 🔴 | Own xHCI controller, never shared (§5.3). Design for **8–10 Msps sustained**; 20 Msps only for `hackrf_sweep`. Phase 0 benches the real number. Instrument the drop counter. |
| 8c | 🆕 **Neptune's USB controller count is UNMEASURED.** The "5 domains" figure in circulation is the **Intel** box's (§5.2). If the two TB ports share a controller, the no-dock fallback dies. | 🟠 | Phase 0 `system_profiler SPUSBDataType` — device→controller map. The OWC TB3 dock (already owned?) is the fix (§7.6). |
| 8d | 🆕 **The disco ONNX model was tuned on RTL slices @2.4 Msps.** HackRF slices = different rate, noise floor, and front end — a **distribution shift**, not a drop-in. | 🟠 | Heuristic v0 stays the HackRF default until re-tuned (`disco/training/finetune_real.py`). Slice filenames carry `rate`, so eras are separable on disk (§3.7). |
| 8e | 🆕 **HackRF sensitivity** — flat wideband front end, no LNA, no TCXO (±20 ppm ⇒ ±120 kHz @ 6 GHz). Weaker than the Blog V4 on weak signals. **Not** a bit-depth issue (RTL is also 8-bit). | 🟡 | Accepted for wideband classification (Will's call). External LNA + bias-tee if it bites. Record **measured** centre, never commanded (§3.7). |
| 9 | **Two P25 on one RSPduo is unproven here** — the clean dual-tuner result was on `180903EF32`, and the *simultaneous* two-system case has never run | 🟠 | Phase 3 = the proof. Site spans verified first. Fall back to one system + `preferred_tuner`. |
| 10 | **Phase1-vs-Phase2 decode config** (§3.5) | 🟡 | Live decode. The "metadata-shows-but-audio-silent" symptom is already documented with its remedy. |
| 11 | **Unaliased talkgroups silently don't stream** | 🟡 | Catch-all alias + CI check in the generator. |
| 12 | ~~**disco/ACARS/survey can't share one dongle**~~ | ✅ | **RESOLVED by the HackRF decision (§3.6).** disco+survey → HackRF; ACARS/VDL2 → `61108285` continuously. No mode switch, no time-slicing, nothing chosen against anything. |
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
- **Does not fix the serial reversal.** §7.5 is a survey, not a patch. Out of scope for this
  branch by instruction.

---

### 7.5 Serial-reversal cleanup needed

**Follow-up task. NOT in scope for this branch — nothing below was modified.**

Will confirmed 2026-07-16: **Neptune = `180903EF32`, Venus = `1809063632`** (§5.1).
`etc/mac/sdr_fleet_policy.json` rev 4.1 (2026-07-08) has them reversed, and seven artifacts
copied from it. This is the survey; the fix is its own change.

#### A. Wrong — asserts Neptune's digital RSPduo is `1809063632`

| File | Lines | What it claims |
|---|---|---|
| **`etc/mac/sdr_fleet_policy.json`** ⚠️ **ROOT** | `2`, `5`, `6`, `7`, `8`, `17`, `18`, `23`, `25`, `37`, `42`, `53`, `74`, `76`, `79`, `89` | The whole rev-4.1 narrative: `180903EF32` "removed/relocated" → `retired_devices`; `1809063632` = `RSP-B` / `RSP-DIGITAL`, `role: sdrtrunk-p25`, `mode: DUAL`, USB group `G2`. **Both halves reversed.** `_detection` (line 6) says "Expected serial: 1809063632" — wrong. Fix this first; the rest is downstream. |
| `docs/sb7-northstar-program.md` | `28`, `139`–`142`, `162`–`163`, `175`, `178`–`179`, `254`, `257`–`258` | Hardware table + §4.1 device map + §4.1b plan. Line 163 strikes through `180903EF32` as "REMOVED / RELOCATED". |
| `macos/launchd/com.scannerproject.sdrtrunk.plist` | `9`, `13`–`15`, `19`–`21` | Header comments: "digital tuner = the RSP serial 1809063632"; SDRTrunk "must claim serial 1809063632 through the tuner-broker". |
| `scripts/mac-start-sdrtrunk.sh` | `7`, `10`, `12`, `16`–`17` | "DIGITAL IS ON THE RSP serial 1809063632". |
| **`macos/sdrtrunk/tuner_configuration.json`** | `73`, `89` | `uniqueID: "RSPduo Tuner 1 SER#1809063632"` / `"...Tuner 2 SER#1809063632"`. ⚠️ **Functional, not just prose** — these are the tuner labels `preferred_tuner` matches on. Wrong serial = no tuner match = §3.5's dual-digital pinning silently fails. |
| `tests/test_tuner_broker_policy.py` | `57`, `61`, `67` | Asserts `device_by_serial("1809063632")` and that it's the sole `dual_tuner` device. **The test encodes the wrong fact and will pass against a wrong policy** — update alongside rev 5.0 or it blocks the fix. |
| `ui/sb5.html` | `3404`, `3646` | DIGITAL pane comment + the "Check audio stream" Claude prompt. Cosmetic, but the prompt feeds a diagnostic. |

#### B. Stale — pre-4.1 "both RSPduos on one host" era; re-check when the fleet is rewritten

| File | Lines | Note |
|---|---|---|
| `macos/README.md` | `28`, `29` | `180903EF32 → SDRTrunk` ✅ right for Neptune; `1809063632 → SDRangel` now describes **Venus**, not this host. |
| `docs/macos-backend-migration-scope.md` | `52`, `53` | Same two-RSP mapping. Historical scope doc — arguably leave as the record of what was decided then. |
| `docs/macos-transition-memo.md` | `19`, `24`, `66`, `110`, `123` | Two-RSP era; line 66/110 reference `RSPduo Tuner 1 SER#180903EF32`. |
| `docs/remote-and-stability-plan.md` | `32`, `33` | "RSP-A `180903EF32` → SDRTrunk. RSP-B `1809063632` → SDRangel (70cm/ground)." |
| `ui/reliability.py` | `52`, `53` | Serial→label map. Roles stale (`"RSPduo Digital (op25)"` — op25 retired), but the device→role *intent* happens to match Neptune. |

#### C. Correct — do not touch

| File | Lines | Why it's right |
|---|---|---|
| **`macos/bin/sdrangel-restore.py`** | `35`, `66`, `81`–`85` | `AIRBAND` route `serial="1809063632"` sits in the **Venus** `ROUTES` branch (line 85) — ✅ correct. Line 66's Neptune comment: *"RSPduo 180903EF32 is owned by SDRTrunk (P25), not SDRangel"* — ✅ correct. **The live code had it right the whole time.** |
| `macos/data/analog_scanlists.json` | `2`, `20` | 70cm on "RSP-B SDRplayV3 1809063632" = the Venus/Intel config. ✅ correct for Venus. |
| `macos/sdrtrunk/README.md` | `17` | `disabledTuners` pins `RSPduo Tuner 1 SER#180903EF32`. ✅ correct for Neptune — and note it **disagrees with `tuner_configuration.json` above**, which is itself evidence of the reversal. |
| `scripts/build_sugar_tree_playlist.py` | `24`, `25` | `"RSPduo SER:180903EF32 Tuner 1/2"` ✅ correct device (Nashville/TACN content, but the serial is right). Also the §3.5 worked example. |
| Linux/Micro-era: `chirp/PROGRESS.md`, `chirp/config/*.json`, `tests/test_{config_validator,build_service_config,rspduo_dedicated_exclusion,…}.py`, `snapshots/`, `SB5_Phase0_Spike_Report.md`, `docs/sb6-bringup.md`, `docs/session-handoff-2026-06-1{3,4}.md`, `docs/research-ground-nfm-rtl-fix.md`, `etc/airband-ui.conf`, `etc/scannerbox/`, `broker/client.py`, `scripts/_*_capture.py`, `scripts/recover-sdrplay.sh` | — | `1809063632` = the Micro's **airband** RSPduo. ✅ **Historically accurate — leave alone.** These describe a box that no longer exists in this arrangement; "fixing" them would corrupt the record. |

**Two lessons worth keeping:**

1. **The live code was right; the doc-shaped artifacts were wrong.** `sdrangel-restore.py`
   runs every 10 minutes on both boxes — a wrong serial there fails loudly. The policy file
   is read by the broker and by humans, and nothing exercised it hard enough to catch this.
   **Trust the thing that runs.**
2. **`macos/sdrtrunk/tuner_configuration.json` vs `macos/sdrtrunk/README.md` already
   disagreed** — one says `SER#1809063632`, the other `SER#180903EF32`, in the same
   directory. That contradiction was sitting in the repo, unflagged, and is exactly the
   class of thing §3.5's "read the tuner label off `View → Tuners`" step exists to catch.

### 7.6 Surfaced while writing the HackRF revision — two hardware questions

Both are Phase 0 items, not Phase 1 blockers. Neither changes the design; both change what
gets bought.

**Q5 — Is the OWC TB3 dock available for Neptune?** 🟡
The layout in §5.3 wants **two extra xHCI controllers** (one for the HackRF, one for the
RSPduo). The OWC TB3 dock from the Micro/ScannerBox era has exactly that — **2× Fresco
FL1100** — and it is the hardware that de-stacked the Micro's dual-RSPduo starvation in the
first place (`reference_micro_access_and_hpdb_migration`). TB3 docks work on M1. **If it's
free, this costs nothing and is strictly the best option.** If it's committed elsewhere, the
fallback (§5.3) depends on Q6.

**Q6 — Do Neptune's two Thunderbolt ports share a USB controller?** 🟡
If they do, the no-dock fallback in §5.3 collapses — HackRF (up to 320 Mbps) and RSPduo
(~128 Mbps) would collide on one 480 Mbps domain at ~450 Mbps, which is the Micro's starvation
bug rebuilt on new hardware. **A TB dock then becomes mandatory rather than preferred.**
`sb7-northstar-program.md:227` already asked the sibling question about the USB-A pair and
never answered it. **One `system_profiler SPUSBDataType` answers both.**

> **Neither is a design fork** — the design is the same either way (3 domains, HackRF alone).
> They only decide *what hardware delivers it*. Which is why they're Phase 0 measurements
> rather than §7.1 blockers.

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
| `etc/mac/sdr_fleet_policy.json` | Device map. ⚠️ **rev 4.1 has the RSPduo serials REVERSED — treat as wrong until rev 5.0 lands (§5.1, §7.5)** |
| `macos/data/analog_scanlists.json` | Profile ancestor + the densest SDRangel operational notes in the repo |
| `broker/` | The device-lease design. Survives unchanged. |
| memory `reference_two_box_audio_harness` | Every copyToUDP/keepalive/orphan-ffmpeg landmine |
| memory `project_neptune_philly_p25_validated` | The RSPduo reboot rule + the serial contradiction |
| memory `project_airband_rf_collapse_recurring` | Why §3.3 stores native gain units |
| `chirp/dsp/cluster_planner.py:128` `plan_clusters()` | Pure function, no GR imports — the camp-vs-hunt decision for Ground sub-profiles (§3.9) and the cross-window planner if Q4 forces it (§3.4) |
| `macos/data/analog_scanlists.json:ground_reference` | MURS / FRS-GMRS / 2m channel lists, *"NOT currently deployed"* — seed content for the first Ground sub-profiles (§3.9) |
| `assets/US FRS and GMRS Channels.csv`, `assets/basicnashvilleairband` | Untracked working files in the repo root — likely raw material for Ground/Air profiles. Worth folding into `profiles/` rather than leaving loose. |
| memory `reference_micro_access_and_hpdb_migration` | The 5-controller USB topology — ⚠️ **the INTEL box, not Neptune** (§5.2). Also the source of "domain = controller, not hub" and the OWC dock (§7.6) |
| memory `project_usb2_saturation_reboot_recovery` / `project_sb6_session_2026_06_18_evening` | What USB-2 starvation looks like from the software side, and the 2026-06-19 "USB-3 re-cable won't help" retraction |
| memory `reference_rspduo_serial_assignment` | The confirmed serial map + the rev-4.1 reversal (§5.1, §7.5) |
| `disco/src/sweep.py:328`, `:492` | The `SoapySDR.Device()` call site + args string — the one line that moves RTL→HackRF (§3.7) |
| `disco/training/finetune_real.py` | Re-tuning the classifier on HackRF slices (§3.7, Phase 5) |
