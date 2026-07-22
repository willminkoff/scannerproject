# Research: Two `sdrplay_apiService` instances, one per RSPduo

**Date:** 2026-06-18
**Branch:** `sb6-phase1-2-chirp-config-hardfail` (not pushed)
**Status:** First-pass research — doc + forum + GitHub reading only. No changes to the Micro. Micro-side confirmations are listed explicitly and gated on Will's greenlight.
**Question:** Can we run two instances of `sdrplay_apiService`, one bound to each RSPduo, so that if one wedges the other RSPduo keeps running? (Failure-isolation requirement for the mobile/in-car use case.)

---

## TL;DR

| Question | Answer |
|---|---|
| **1. Vendor officially supports multiple `apiService` instances?** | **No.** No SDRplay documentation, install script, or forum post describes or endorses running two service instances. Every documented setup runs exactly one. |
| **2. Feasible via workaround?** | **Theoretically yes, unproven and fragile.** The API v3.x service↔library IPC is POSIX **shared memory** (`/dev/shm`, `shm_open`) with a **singleton/global-mutex** design. Two naked instances on one host will collide on the fixed shm names. The only plausible isolation is **two containers / mount-namespaces**, each with a *private* `/dev/shm` and only one RSPduo's USB node passed in. No one in the community has documented doing this. |
| **3. Client-side requirements** | Heavy. The library talks to its service over `/dev/shm`, so **each client (chirp gr-osmosdr, op25 SoapySDRPlay3) must run in the same namespace as its service** — i.e. inside the same container. There is **no client-side "pick which service/socket" knob**; device selection is by serial only, and that selection happens *within* one service. |
| **4. Community precedent?** | **None found** for multi-instance. Docker community pattern is the opposite: **one host service**, `/dev/shm` shared *into* client containers. |
| **5. Can we skip the daemon (libsdrplay_api direct)?** | **No.** API v3.x requires the running service; the library crashes/fails without it (unlike the old API 2.x, which talked to hardware directly). |
| **6. Recommended path** | **Do not build multi-instance first.** Run a cheap **empirical coupling test** on the Micro (greenlit, read-only-ish): pull one RSPduo, watch whether the other's stream survives a single shared service. The SB6 ST+DT topology + USB-3 move already removes the *wedge* failure mode that has actually been biting. Only if the empirical test proves hard coupling *and* that's unacceptable do we prototype the two-container isolation off-box. |

---

## REVISED 2026-07-21 — multi-instance is UNNECESSARY; one service already serves many clients/devices

**The question this doc asks (two `apiService` instances) is moot for the actual need.** Measured on both macOS boxes (`lsof`, 2026-07-21):

- **SDRTrunk and SDRangel each link the SAME system library** `/Library/SDRplayAPI/3.15.1/lib/libsdrplay_api.so.3.15`. Neither bundles its own bindings; neither bypasses the daemon. *(This confirms TL;DR row 5 — the daemon is required — and disproves the "SDRTrunk is direct-linked" hypothesis.)*
- **Both reach `sdrplay_apiService` over POSIX shared memory**, not TCP: `Glbl\sdrSrvComShMem`, `Glbl\sdrSrvComMtx`, `Glbl\sdrSrvCmdSema`, `Glbl\sdrSrvRespSema`. A TCP-client probe shows **zero** clients and misleadingly looks like a bypass — do not use TCP as the test.
- **The IPC carries PER-DEVICE sub-channels**: `Glbl\sdrSrvDv00_mCmdMap`, `…Dv00_mCmdSem`, `…Dv00_mDataMap`, `…Dv00_mDataRSem` (and `Dv01` for a second device). **The service is multi-client *and* multi-device by design.**

**Live proof (Neptune, at time of writing):** SDRTrunk (RSPduo `180903EF32`, per-device `Dv00` open, 23 `sdrSrv` handles) and SDRangel (global channel, 4 handles) have been **concurrent `apiService` clients for 35+ hours** with `neptune-trunk.mp3` and `neptune-air.mp3` both at 200. Independently, Will ran **SDRTrunk on one RSP + SDRangel on the other RSP on Venus 2026-07-20** with no crash.

### The real constraint
> **One RSP per client PROCESS is fine. Two RSPs inside ONE process is not.**

The `0x6bed` segfault / corrupt-IQ history belongs to the **one-process-two-RSPs** mode — a single SDRangel instance driving two RSP devicesets is exactly what crashed on Venus 2026-07-21 — and to rapid concurrent open/close churn. It does **not** come from two separate processes sharing the service.

### Consequences
- **No container-isolated `apiService` work is needed** to run SDRTrunk on the RSPduo and SDRangel on an RSP1B simultaneously on one host. The two-container prototype in this doc can stay shelved.
- `etc/mac/sdr_fleet_policy.json` **rev 5.2** withdraws rev 5.1's blanket *"two RSPs on one host is toxic"* (which was inferred, not measured) and encodes the one-RSP-per-process rule instead.
- Failure *isolation* (the original motivation — one RSPduo wedging the other) is a **separate** question this revision does not answer; it remains untested.

---

## 1. How the SDRplay API v3.x is actually wired

The chain is:

```
application  ──>  libsdrplay_api.so  ──(POSIX shared memory in /dev/shm)──>  sdrplay_apiService  ──(USB)──>  RSP hardware
(chirp/op25)      (linked into client)        singleton daemon, global mutex
```

Verified facts:

- **IPC is POSIX shared memory, not a TCP port.** The library calls `shm_open` against segments under `/dev/shm`. This is visible in the wild as failures like `libsdrplay_api...: shm_open: No such file or directory` ([sdrtrunk #1644](https://github.com/DSheirer/sdrtrunk/issues/1644)). The Docker community pattern confirms it the other way: containers must **volume-mount `/dev/shm`** (and `/dev/bus/usb`) to reach a host-run service ([herrameise/sdrplay-api-linux-docker](https://github.com/herrameise/sdrplay-api-linux-docker), [openwebrx groups.io](https://groups.io/g/openwebrx/topic/sdrplay_with_docker_on_ubuntu/84113152)).
  - ⚠️ The "port 1234 / `-a` / `-p`" results that come up in searches are about the **RSP TCP Server** (`rsp_tcp`), a *separate* remote-streaming app, **not** `sdrplay_apiService`. Do not conflate them. ([RSP TCP Server guide](https://www.sdrplay.com/docs/SDRplay_RSP_TCP_Server_Guide.pdf), [SDRplay/RSPTCPServer](https://github.com/SDRplay/RSPTCPServer))
- **The service is a singleton with a global lock.** Community fixes for a stuck service are "send SIGTERM to `sdrplay_apiService`" / "`sudo systemctl restart sdrplay`," and SoapySDRPlay3 had to add finer-grained locking around a **global mutex** in the client path ([SoapySDRPlay3 PR #62 "Lock fix"](https://github.com/pothosware/SoapySDRPlay3/pull/62)). This is consistent with a one-service-owns-everything design.
- **The documented service launch takes no arguments.** Every install (vendor + community) is literally `ExecStart=/opt/sdrplay_api/sdrplay_apiService` with no flags, no port, no device filter ([SDRplay API install instructions](https://websdr.oh2lak.radio/SDRPlay-API/), [developnsolve install guide](https://www.developnsolve.com/linux/how-to-install-sdrplay-api-from-cli-on-linux), [ZR6LSD/SDRplay-x64](https://github.com/ZR6LSD/SDRplay-x64)).
  - ⚠️ **Not yet verified on the Micro:** whether the installed `sdrplay_apiService` binary exposes any *undocumented* `--help` flags or env vars (e.g. a shm-namespace or device-serial argument). This is the single most useful local check and is low-risk (see §7).
- **Device model: one service, many devices, client picks by serial.** A single service enumerates *all* connected RSPs (`sdrplay_api_GetDevices`); each client calls `sdrplay_api_SelectDevice` with the serial it wants. So **one service already handles both RSPduos as independent device handles** — that's the supported multi-device path ([astronomy.me.uk: two RSPduo in SDRuno](https://www.astronomy.me.uk/running-two-separate-sdrplay-rspduo-devices-in-sdruno), [SDRplay community](https://www.sdrplay.com/community/viewtopic.php?t=4913)). The coupling Will is worried about is **not** the device handles — it's that they live behind **one process**.
- **API v3.x requires the running service; you cannot use the library alone.** With the service stopped, clients crash/fail to init ([sdrtrunk #1511 "Crash can occur with SDRPlay API in place, but service stopped"](https://github.com/DSheirer/sdrtrunk/issues/1511), [SDR++ #956 "make sure the service is running"](https://github.com/AlexandreRouma/SDRPlusPlus/issues/956)). The old **API 2.x** library *did* talk to hardware directly with no service, but 3.x moved everything behind the daemon. So "libsdrplay_api direct, no daemon" is **not an option on 3.x**.

**What this means for the coupling question:** the two RSPduos are already logically independent *inside* the one service. The shared point of failure is the single `apiService` **process** and its single `/dev/shm` namespace. If that process wedges, both drop. That — and only that — is what a second instance would decouple.

---

## 2. Is multiple-instance feasible via workaround?

**Naked (two `sdrplay_apiService` on the same host, same `/dev/shm`): no.** They use fixed shm segment names and a singleton/global-lock model. Two will collide. There is no documented port/socket/namespace argument to separate them. (Strongly implied by all evidence above; the exact shm names are the one thing left to confirm on the binary — §7.)

**Isolated (two containers or two mount-namespaces): plausible but unproven.** The mechanism would be:

1. Two containers, each with a **private `/dev/shm`** (default for a container — its `/dev/shm` is its own tmpfs unless you share the host's). Private `/dev/shm` ⇒ the fixed shm names don't collide because they live in different filesystems.
2. Each container is passed **only one RSPduo's USB node** (`--device=/dev/bus/usb/<bus>/<dev-of-RSPduo-A>` for container A, the other for container B), or restricted via a cgroup device rule, so each service enumerates exactly one device.
3. **Each container runs its own `sdrplay_apiService` *and* its own clients** (chirp stack in container A, op25 stack in container B), because the client reaches the service through that container's private `/dev/shm`.

This is the *only* design that satisfies the requirement, and it is **not documented by anyone**. The community Docker images (herrameise, f4fhh) all do the opposite — single host service, shared `/dev/shm` into thin client containers — precisely because that's the known-working pattern. So this is a build-and-prove-it path, not a follow-a-recipe path.

**Hard caveats even if it works:**
- **USB device→bus/dev mapping is not stable across replug.** `--device=/dev/bus/usb/001/004` breaks when the device re-enumerates (exactly the mobile/vibration scenario we're isolating *for*). Would need udev rules that pin a stable symlink per RSPduo serial and pass *that* — adds moving parts.
- **One USB controller is still shared.** Per the 2026-06-17 finding, the real production killer has been **USB 2.0 bus saturation**, not the API broker. Two services on the same physical USB 2 bus still starve each other. Namespace isolation does nothing for bandwidth. (See `project_sb6_phase1_2_and_voice_pulse_rootcause` / the SB6 plan's USB-topology work item.)
- **Forward-compat risk.** Anything relying on undocumented shm/namespace behavior can break on any SDRplay API point release (3.15 → 3.16…). SDRplay has a history of churn here (3.07/3.08/3.12/3.14/3.15) and macOS/Linux behavioral differences ([SDR++ #1312 Debian 3.12 vs 3.14](https://github.com/AlexandreRouma/SDRPlusPlus/discussions/1312)).

---

## 3. Client-side requirements

- **No "choose your service" knob exists.** Neither gr-osmosdr's sdrplay/soapy path nor SoapySDRPlay3 expose a service-endpoint argument. Device args are serial/label only (`soapy=...,driver=sdrplay,serial=...` style), and that selection is resolved *inside* a single service via `GetDevices`/`SelectDevice`. So you cannot tell client A "use service A" by config — you separate them only by putting them in **different namespaces with different `/dev/shm`**.
- **Therefore clients move into the container.** In the isolated design, chirp's gr-demod daemons and op25's multi_rx must run inside the same namespace as their device's service. That's a real restructuring of the systemd unit layout (units become container-scoped), and it changes how the cmd-bus, icecast, and `/dev/shm`-based IQ ring (SB6 Phase 4) are wired.

---

## 4. Community precedent

- **Multiple `apiService` instances:** none found. Searches for "multiple apiService," "two instances," "multi-instance" return only **multiple *application* instances against one service** (e.g. two SDRuno windows, one per RSPduo — [astronomy.me.uk](https://www.astronomy.me.uk/running-two-separate-sdrplay-rspduo-devices-in-sdruno)).
- **Two RSPs on one service, real-world reliability:** widely reported as workable but with a strict **master-before-slave start ordering** and a known tendency for the *service* to "decide unpredictably to shut down" and drop all radios at once ([SDR-Radio groups.io "Another SDRPlay API Problem Variant"](https://sdr-radio.groups.io/g/main/topic/another_sdrplay_api_problem/101457815), [SDR++ #1558](https://github.com/AlexandreRouma/SDRPlusPlus/issues/1558)). This is exactly the shared-broker coupling Will named — confirmed as a real failure mode, but it's a *service wedge*, not "one device drop kills the other."
- **Docker:** [herrameise/sdrplay-api-linux-docker](https://github.com/herrameise/sdrplay-api-linux-docker), [f4fhh/sdrplay_container](https://github.com/f4fhh/sdrplay_container) — all single-host-service, shared `/dev/shm`. None isolate per device.

---

## 5. Risks summary

| Risk | Severity | Note |
|---|---|---|
| Fixed shm names collide between instances | **High / blocking for naked instances** | Forces the container/namespace route. |
| Undocumented behavior breaks on API update | **High** | SDRplay churns the API; closed-source; no support if it breaks. |
| USB bus bandwidth still shared | **High** | The actual recent production killer. Namespacing the broker doesn't help bandwidth. |
| USB device→/dev path instability on replug | **Medium** | The mobile scenario we're isolating for is also what breaks `--device` pinning. Needs serial-pinned udev symlinks. |
| Client stacks must move into containers | **Medium** | Real restructuring of systemd/cmd-bus/IQ-ring wiring. |
| Master/slave start-ordering still applies per device | **Low/Medium** | Each DT device still needs MA-before-SL; ST device is simpler. |

---

## 6. Recommended path forward

**Ranked, evidence-first, reversible — matching the plans-first / prove-don't-assume workflow.**

### Step 0 (do this first, cheap): empirically test whether one service actually couples the two RSPduos on a *device* failure.
The whole premise is "if one SDR falls out, the other must survive." We have **not** established that a single `apiService` fails to deliver that. Two distinct failure modes:
- **Service wedge** (the documented one) → takes down *both*. Real, but addressed structurally by SB6 (ST+DT topology removes the two-DT wedge; USB-3 move removes the bandwidth wedge; a readiness-probe supervisor — SB6 Phase 3/4 — restarts the *service* fast).
- **Single device USB drop / replug** (the mobile-vibration case) → **unknown whether it crashes the service for the other device.** This is the test that actually decides whether multi-instance is even needed.

**Test (greenlit, on the Micro, low blast radius):** with both RSPduos streaming through the single service, physically unplug RSPduo A (or `echo 0 > .../authorized` on A's USB node) and watch whether RSPduo B's stream survives and whether the service recovers when A returns. If B survives a clean single-device removal, the isolation requirement is **already met for the realistic mobile failure**, and multi-instance buys little.

### Step 1: confirm the binary's surface (read-only, see §7).
`sdrplay_apiService --help`, `--version`, `strings` for shm names and any env vars. Either confirms there's no separation knob (expected) or surfaces an undocumented one (would change everything).

### Step 2: only if Step 0 shows hard coupling AND it's unacceptable — prototype the two-container isolation **off the production Micro** (spare box / VM with the two RSPduos, or a maintenance window). Success criterion: kill container A's service, verify container B keeps streaming. Reversible by definition (containers are additive; production path untouched until proven).

**What I would *not* do:** build naked dual instances (will collide), or chase libsdrplay_api-direct (doesn't exist on 3.x).

**Bigger-picture honest take:** the SB6 plan already chose **one ST device (airband) + one DT device (digital)** specifically to kill the two-DT-mode wedge, and the 2026-06-17 work pinned the *actual* recurring failure to **USB 2.0 bandwidth**, fixed by moving airband to USB 3. Between those two, the historically-observed "both go down together" events are largely addressed without touching the broker. Multi-instance is a real isolation upgrade *in principle*, but it's the highest-complexity, lowest-precedent option, and it doesn't address the bandwidth coupling that's been doing the actual damage. Earn it with Step 0 before spending the complexity.

---

## 7. If/when greenlit — low-risk Micro confirmations (read-only-ish)

These don't prototype anything; they just resolve the open factual questions. None restart the service except where noted.

```bash
# 1. Exact API version in production
cat /opt/sdrplay_api/*.txt 2>/dev/null; ls -l /usr/local/lib/libsdrplay_api.so*

# 2. Does the binary expose any separation knobs? (does NOT start the daemon if --help exits)
/opt/sdrplay_api/sdrplay_apiService --help   2>&1 | head -40
/opt/sdrplay_api/sdrplay_apiService --version 2>&1 | head -5

# 3. What shm segments does the running service use? (confirms fixed-name collision hypothesis)
ls -l /dev/shm | grep -i sdr
strings /opt/sdrplay_api/sdrplay_apiService | grep -iE 'shm|/dev/shm|SDRPLAY_|PORT|socket' | sort -u

# 4. Any env vars the service reads
strings /opt/sdrplay_api/sdrplay_apiService | grep -E '^[A-Z][A-Z0-9_]{3,}$' | sort -u
```

⚠️ Item 2 runs the binary. If `--help` is unrecognized, the binary may instead try to *start* a second service — so run it only when the production service can tolerate a momentary second-process attempt, or run it in a throwaway namespace. Safer alternative for item 2: inspect with `strings ... | grep -- '--'` first to see if any flag strings exist before executing.

---

## Sources

- [SDRplay API page](https://www.sdrplay.com/api/)
- [SDRplay API Specification v3.14 (PDF)](https://www.sdrplay.com/docs/SDRplay_API_Specification_v3.14.pdf) — fetched but PDF text not machine-extractable in this pass; architecture cross-checked against community sources
- [RSP TCP Server User Guide (PDF)](https://www.sdrplay.com/docs/SDRplay_RSP_TCP_Server_Guide.pdf) — the *separate* `rsp_tcp` app (port 1234), not `apiService`
- [SDRplay/RSPTCPServer (GitHub)](https://github.com/SDRplay/RSPTCPServer)
- [sdrtrunk #1644 — shm_open: No such file or directory](https://github.com/DSheirer/sdrtrunk/issues/1644)
- [sdrtrunk #1511 — crash with API installed but service stopped](https://github.com/DSheirer/sdrtrunk/issues/1511)
- [SDR++ #956 — "make sure the service is running"](https://github.com/AlexandreRouma/SDRPlusPlus/issues/956)
- [SDR++ #1558 — could not select RSP device](https://github.com/AlexandreRouma/SDRPlusPlus/issues/1558)
- [SDR++ #1312 — Debian sid, API 3.12 vs 3.14](https://github.com/AlexandreRouma/SDRPlusPlus/discussions/1312)
- [SoapySDRPlay3 PR #62 — Lock fix (global mutex)](https://github.com/pothosware/SoapySDRPlay3/pull/62)
- [astronomy.me.uk — running two RSPduo in SDRuno (two app instances, one service)](https://www.astronomy.me.uk/running-two-separate-sdrplay-rspduo-devices-in-sdruno)
- [SDRplay community — two RSP, master/slave start ordering](https://www.sdrplay.com/community/viewtopic.php?t=4913)
- [SDR-Radio groups.io — service unpredictably drops all radios](https://sdr-radio.groups.io/g/main/topic/another_sdrplay_api_problem/101457815)
- [herrameise/sdrplay-api-linux-docker](https://github.com/herrameise/sdrplay-api-linux-docker) — single host service, shared /dev/shm
- [f4fhh/sdrplay_container](https://github.com/f4fhh/sdrplay_container)
- [openwebrx groups.io — SDRPlay with Docker on Ubuntu](https://groups.io/g/openwebrx/topic/sdrplay_with_docker_on_ubuntu/84113152) (mounts /dev/shm + /dev/bus/usb)
- [SDRplay API Linux install instructions (oh2lak)](https://websdr.oh2lak.radio/SDRPlay-API/)
- [developnsolve — install SDRplay API from CLI](https://www.developnsolve.com/linux/how-to-install-sdrplay-api-from-cli-on-linux)
- [ZR6LSD/SDRplay-x64 install script](https://github.com/ZR6LSD/SDRplay-x64)
</content>
</invoke>
