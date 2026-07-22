# Research: Two SDRangel instances on Neptune (VFO gets its own mount)

**Date:** 2026-07-21 · **Branch:** `sb3-neptune-vfo` · **Status:** research pass — read-only probes + this doc. Nothing built.

## Why this exists

A single SDRangel process on Neptune sustains **exactly one** working `copyToUDP` tap — the virtual **"System default device" (idx -1)** — and Air already owns it (→ :9998 → `neptune-analog.mp3`). This is now **measured, not theorized**:

- 2026-07-21 Ground load: routed NFM channels to the named **BlackHole 2ch** device (idx 2), armed its `copyToUDP` → mount **404** (no audio at all), and the multi-DS teardown **wedged SDRangel's REST**.
- 2026-07-21 drainer test (Option B): one channel on DS0 → BlackHole 2ch, `copyToUDP` on idx 2 → :9997, measured with a UDP socket. **0 bytes without a drainer, 0 bytes with an `ffmpeg avfoundation` drainer.** Draining did nothing; the channel-op wedged REST again.

**Conclusion:** BlackHole is not a viable second sink on this box. Will's requirement is explicit — **VFO must have its own mount** — so the only remaining path is a **second SDRangel process** with its own audio engine and its own idx -1 tap.

## Feasibility: YES (with one untested load-bearing assumption)

SDRangel's CLI (`--help` on Neptune, v7.x) supports what's needed:

| Option | Use |
|---|---|
| `-p, --api-port <port>` | Second instance REST on **8092** (primary keeps 8091). ✓ |
| `-a, --api-address <address>` | Bind REST to loopback. |
| `--scratch` | **Start from scratch, no config load** — ideal for a REST-driven instance. ✓ |

- **Device claiming is a non-issue for the RTL.** The RTL (`95339533`) is **librtlsdr / USB-direct — no `sdrplay_apiService` involved** (that daemon is SDRplay-only). Each SDRangel opens its own RTL by serial via an explicit `PUT /deviceset/N/device`. Enumeration (`GET /devices`) is read-only and does not claim. So as long as serials are **partitioned** — VFO instance binds only `95339533`, the primary never touches it — the two instances cannot fight over hardware. (The primary drives the RSP1B `2405265A60`; the VFO instance drives the RTL. Neither opens the other's device.)

### ⚠️ The one load-bearing unknown: two idx -1 audio taps

The entire premise is that a **second SDRangel *process*** gets its **own** sustaining "System default device" tap. This is **plausible but UNTESTED**:

- **Why plausible:** the Part 2 failure was that *one* process can't drive *two* taps (idx -1 works, a second named device doesn't). A separate process has its own Qt audio engine and its own idx -1. This is the same shape as fleet-policy rev 5.2's "**one RSP per process** is fine" — the coupling is *intra-process*, not global.
- **Why not certain:** CoreAudio is multi-client (multiple apps can output to the same physical device), and each SDRangel's `copyToUDP` is its own thread tapping its own audio buffer — so two instances taping "System default device" to **different UDP ports** (:9998 and :9997) *should* be independent. But "should" isn't "measured," and this is exactly the kind of audio-plumbing assumption that has bitten twice.

**This must be the FIRST thing tested, before any build (see Pass 1).** If a second SDRangel's idx -1 tap does not sustain to :9997, multi-instance is dead too and the separate-mount requirement can't be met on this box.

## Config isolation

SDRangel stores config in a **single shared QSettings plist**: `~/Library/Preferences/com.f4exb.SDRangel.plist` (org `f4exb`, app `SDRangel`). Two instances share it and **clobber it last-writer-wins on exit**. Options:

1. **`--scratch` for the VFO instance** (recommended for the trial): it never *loads* config, so it starts clean and is driven entirely by REST (sb3 applies its profile each launch). It still *writes* on exit, but neither instance depends on persisted config — Neptune's primary is already effectively REST-driven (after every restart its device set comes up stale and Air must be re-loaded via sb3; see the recovery pattern). So the shared write is low-impact.
2. **Separate `.app` bundle with a distinct `CFBundleIdentifier`** (cleanest, if we productionize): copy `SDRangel.app` → `SDRangel-VFO.app`, edit `CFBundleIdentifier` to `com.f4exb.SDRangel-vfo`, and QSettings lands in a *separate* plist. Costs ~disk for a duplicate bundle; fully isolates config. Recommend this only after Pass 1 proves the audio path.

Qt's config path is keyed by bundle/org on macOS and is **not** redirectable per-instance via a CLI flag or `XDG_CONFIG_HOME`, so those two are the realistic options.

## sb3 orchestration change (moderate — the foundation is already there)

- **`sb3/sdrangel.py`** — `SDRangelClient.__init__(base=BASE)` **already accepts a base URL** (line 48). The write path is ready; the translator just needs to pass it.
- **`sb3/translator.py`** — lines 116 & 271 create `SDRangelClient(execute=…, emit=…)` with the default base. Change to `base=prof.rest_url`. Also `resolve_audio_tap()` (called during apply) reads audio from `backends` → must target the right instance.
- **`sb3/backends.py`** — the read functions (`sdrangel_devicesets`, `sdrangel_audio_outputs`, `resolve_audio_tap`, `sdrangel_deviceset_detail`) hardcode `SDRANGEL_REST` (8091). Add a `base` parameter (default `SDRANGEL_REST`) so VFO status/tap reads can hit 8092.
- **Profile schema** — add optional `rest_url` (default `http://127.0.0.1:8091/sdrangel`). The VFO profile sets `http://127.0.0.1:8092/sdrangel`.
- **`sb3/ui/routes.py`** — `build_status` queries the VFO instance (8092) for `vfo_status` / `vfo_device_online`, mirroring the existing air/ground fields. Write handlers (`tune`/`squelch`/`volume`) grow a `target=vfo` branch that instantiates the client with the VFO base.

## Ownership diagram update

```
com.scannerproject.sdrangel       BACKEND  — primary   (REST 8091, RSP1B 2405265A60 → Air/analog)
com.scannerproject.sdrangel-vfo   BACKEND  — secondary (REST 8092, RTL 95339533 → VFO)
```

- Both are **BACKEND** (survive `kill`, like the primary). Add `com.scannerproject.sdrangel-vfo` to `ownership.BACKEND` (fail-closed `classify()` will otherwise block kill/resume — same bug class as the ground-bridge miss).
- `GUARDED_MOUNTS += "neptune-vfo.mp3"`.
- `kill` never touches either SDRangel; the VFO bridge (backend) + mount are what the invariant guards.

## Failure modes to flag

1. **Dual idx -1 tap collision** — the load-bearing test (Pass 1). Everything hinges on it.
2. **Config clobber** (shared QSettings) — mitigate via `--scratch` or a separate bundle.
3. **Device double-claim** — low risk (RTL is by-serial `PUT`; partition serials), but a stray persisted config on either instance could auto-bind the wrong device on startup. `--scratch` removes that risk for the VFO instance.
4. **CPU** — a second SDRangel adds analog demods + its own audio engine. Neptune (M1) has headroom (Air on RSP1B sits ~3–70% depending on channels), but a VFO doing wideband + waterfall could add real load. Monitor; the primary must not starve.
5. **KeepAlive + stale-enum** — the VFO instance will hit the same "unclean exit → device set stale → needs a gated reload" pattern the primary does. sb3's gated-load (only bind if the device enumerates; second restart if not) already handles this and must apply to the VFO instance too.
6. **Two GUI windows** in the Aqua session (both are GUI apps launched `LimitLoadToSessionType=Aqua`) — cosmetic CRD clutter; consider a headless/offscreen option if it matters.

## Estimated passes to build + deploy + test

| Pass | Work | Gate |
|---|---|---|
| **1 — Audio proof (GO/NO-GO)** | Manually launch a 2nd SDRangel (`--scratch -p 8092`), bind RTL `95339533`, one channel → System default device, `copyToUDP` → :9997, socket-listen. Does the second process's idx -1 tap **sustain**? | If NO → multi-instance is dead; separate VFO mount is not achievable on this box → back to Will. |
| **2 — Build** (if Pass 1 = GO) | Launchd `com.scannerproject.sdrangel-vfo` (or bundle copy); sb3 code (`backends` base param, profile `rest_url`, `translator`, `routes` vfo fields + `target=vfo` writes, `ownership` BACKEND + GUARDED_MOUNTS); `profiles/vfo.default.json`; `neptune-vfo-bridge.sh` + plist (:9997 → `neptune-vfo.mp3`); tests. | — |
| **3 — Deploy + test** | Deploy branch; load Analog + VFO concurrently; 30s sustain all mounts (trunk + analog + vfo); kill-with-all-profiles; UI VFO tab. | Trunk 200 throughout; no wedge. |

**~3–4 sessions total, Pass 1 the gate.** Pass 1 is cheap (~30 min, one manual second instance + a socket listen) and decisive — it should be run before committing to the ~2 sessions of build. Recommend Will greenlight **Pass 1 only** next, then re-scope.

## Open question for Will

Pass 1 (the audio proof) requires **launching a second SDRangel process on Neptune** — that's beyond "read-only," but it's non-destructive (a `--scratch` instance on 8092 that binds only the RTL; the primary's RSP1B/Air is untouched, trunk untouched). It can be torn down cleanly (`launchctl`/`kill` the test instance). **Recommend running Pass 1 as the next step** — it's the single fact that decides whether the whole VFO-own-mount plan is even possible.
