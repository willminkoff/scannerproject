# Asahi Linux migration — compatibility research

**Date:** 2026-07-26 · **Branch:** `sb3-asahi-research` · **Scope:** read-only research, **no box changes**.
**Question:** is it worth putting Asahi Linux (Fedora Asahi Remix) on Neptune (M1 Mac mini) to run a native Linux SDR stack (op25 + RTL-Airband), replacing the macOS SDRangel + SDRTrunk backend?

---

## Executive summary (verdict + recommendation)

**Verdict: GREEN, with one YELLOW that we can design around.** Every SDR building block the mission needs is available on Fedora aarch64 and runs on Asahi's USB stack. RTL-SDR, HackRF, GNU Radio 3.10, and gr-osmosdr are all stock `dnf` packages on Fedora's primary aarch64 arch. op25 (boatbod) builds on Fedora with a manual apt→dnf dependency translation. Asahi's USB-A ports work for single-function devices (all our SDRs qualify), and — critically — **Asahi has working fan control** via the `macsmc-hwmon` driver, which resolves the exact thermal problem that drove the project *off* Linux on the old T2 Intel mini. Note that Neptune is an **M1**, not the T2 box; the T2 fan issue never applied to it, and Asahi handles the M1 mini fan.

The single YELLOW is **SDRplay on Fedora**: the v3 API ships an aarch64 build (3.15; RSP1B needs ≥3.14) but SDRplay only officially tests on Ubuntu LTS, and `SoapySDRPlay3` isn't packaged for Fedora (build from source). We make that risk **irrelevant** by leading with **RTL-SDR for op25 digital** — which is op25's native, best-supported path anyway — and **RTL-Airband for analog**. The RSPduo/RSP1B become optional wideband extras, not critical-path dependencies. If we never need SDRplay on Linux, the whole migration turns solid green.

**Migration cost:** the SB3 control plane is 6,972 LOC across 33 files; only ~2,300 LOC (5 files + two partial files) is genuinely SDRangel/SDRTrunk-coupled and needs rewriting behind already-existing contracts. ~3,000+ LOC (kill switch, launchd/ownership/state, deploy, reconciler safety+config, HTTP server, wizard/HPDB, profile validator, `sb3.html`) carries over unchanged. Estimate: **≈3 SB3-phase-equivalents** to rebuild the backend-facing surfaces, plus a Phase-0 OS/stack bring-up.

**Recommendation:** **Proceed with the Asahi dual-boot pilot as greenlit** — it is genuinely zero-risk (macOS stays intact as the SDRangel/SDRTrunk + Chrome Remote Desktop fallback; reboot returns you to today's stack in 30 seconds). Lead with RTL-SDR to keep SDRplay off the critical path. **Keep a Raspberry Pi 5 (8 GB) as the documented fallback** — it runs the exact proven Debian op25+RTL-Airband stack out of the box and would be the faster path to first-decode if Asahi's Fedora friction bites. Fastest path from "Asahi installed" to "op25 decoding Cumberland Public Safety" is **one focused evening (~4–6 hrs)**.

---

## 1. Asahi + SDR compatibility (the load-bearing section)

| Component | Verdict | Path on Fedora Asahi (aarch64) | Notes / citations |
|---|---|---|---|
| **RTL-SDR / librtlsdr** | 🟢 GREEN | `dnf install rtl-sdr rtl-sdr-devel soapy-rtlsdr` | Stock Fedora packages; aarch64 is a Fedora *primary* arch. [Fedora SDR wiki], [rtl-sdr Fedora pkg] |
| **HackRF One** | 🟢 GREEN | `dnf` SoapySDR + SoapyHackRF; Fedora lists HackRF as supported | Great Scott arm64 support is mature; single-function USB 2.0 device. [Fedora SDR wiki] |
| **GNU Radio + gr-osmosdr** | 🟢 GREEN | `dnf install gnuradio gr-osmosdr` | GNU Radio **3.10.12** in Fedora 43/44 for aarch64; gr-osmosdr packaged. [gnuradio Fedora pkg], [gr-osmosdr Fedora pkg] |
| **op25 (boatbod)** | 🟢 GREEN (build) | clone + build against Fedora's GNU Radio 3.10; **skip `install.sh`** (apt-based) and map deps to `dnf` | Confirmed buildable on Fedora 37 with manual dep mapping. [op25 GR3.10 thread], [op25 install.sh] |
| **SDRplay v3 API (RSPduo/RSP1B)** | 🟡 YELLOW | run the multiplatform `.run` installer (contains x64+ARM32+**ARM64**); build `SoapySDRPlay3` from source | ARM64 build exists (3.15; RSP1B needs ≥3.14) but **SDRplay tests only on Ubuntu LTS**. Community has run the API on Fedora (as far back as F29). apiService is a systemd unit → fine on Fedora. [SDRplay API], [SDRplay downloads], [SDRplay Fedora forum], [sdr-enthusiasts install script] |
| **Asahi USB stack** | 🟢 GREEN (for our devices) | USB-A functional since kernel 5.16; USB 3 on the TB ports landed recently | **Known gap:** multi-function USB devices (hubs w/ NICs, fancy keyboards) and hub+empty-SD-reader combos misbehave. **All our SDRs are single-function USB 2.0 → unaffected.** Cannot boot from USB (dual-boot uses internal storage — fine). [Asahi M1 feature support], [Asahi USB3 kernel] |
| **Thermal / fan control** | 🟢 GREEN | `macsmc-hwmon` kernel driver exposes fans via hwmon (`fan_control=1`) | **Resolves the blocker that pushed the project to macOS.** (That was the 2018 **T2 Intel** mini; Neptune is **M1**, where Asahi drives the fan.) [Asahi fan control], [macos-backend-migration-scope memory] |

**Bottom line for §1:** no dealbreakers. The only friction is (a) op25 needs a manual Fedora build, and (b) SDRplay-on-Fedora is unofficial. Both are avoidable by using RTL dongles for the core mission.

### USB-specific SDR notes
- Our fleet SDRs (RSPduo, RSP1B, RTL-SDR NESDR/SMArt, HackRF) are all **single-function USB 2.0** — exactly the category Asahi handles. The problematic categories are composite/multi-function devices, which none of ours are.
- Asahi has **no `sdrplay_apiService` contention baggage** — that's a macOS-shared-singleton story. On Linux the apiService is a normal systemd daemon; RTL/HackRF don't use it at all (direct libusb).
- No SDR-specific device-detach reports surfaced for Asahi in this pass; the M1 USB-detach issue we fought on **macOS Tahoe** (RSPduo dropping off the bus, reboot-only recovery) is a macOS/SDRplay-driver interaction, not an Asahi one — worth re-testing but not assumed to carry over.

---

## 2. op25 status

- **Fork:** boatbod (the actively-maintained fork; standard for P25 Phase 2). Default branch targets **GNU Radio 3.10** — which is exactly what Fedora 43 ships, so no GNU Radio version fight. [op25 GR3.10 thread]
- **P25 Phase 2 (TDMA voice):** supported. Enabled with **`-2`** (phase2-tdma) plus **`-w`**; for a **TDMA control channel** add **`--tdma-cc`**. Trunking + Motorola SmartNet/SmartZone fully supported via `multi_rx.py`. [op25 boatbod README]
- **Cumberland Public Safety fit:** trunk 5208 is P25 Phase 2; the Crossville site CCs (453.650 / 460.1125 / 460.2125 / 460.625 MHz) go straight into an op25 `trunk.tsv`. op25 will follow Phase 2 traffic channels and decode voice — the same job SDRTrunk does today, on cheaper hardware.
- **SDR choice for op25:** RTL-SDR is the **recommended, best-documented** path ("op25 can work with a single RTL-SDR dongle"). RSP via SoapySDR is possible but "more complicated to set up" and community guidance is to **avoid RSP for P25 trunking**. **→ Use an RTL dongle for digital.** This is simpler than today's SDRTrunk-on-RSPduo and frees the RSPduo. [rtl-sdr.com op25 P25.2 tutorial], [RadioReference SDRplay+op25]
- **Config overhead:** op25 is TSV/CLI-configured (control-channel list, system TSV, talkgroup allowlist), not GUI — a good match for SB3's config-plane philosophy. HPDB already has every freq/talkgroup we'd populate the TSVs with.
- **Analog is NOT op25's job.** op25 is digital-only. Analog airband/NFM (Crossville CTAF/AWOS/Guard) needs **RTL-Airband** (the tool the original Ubuntu scannerbox used) or a GNU Radio flowgraph. RTL-Airband is C++/CMake, builds cleanly on aarch64, and already pairs with our icecast mount pattern. **Verify item:** confirm RTL-Airband builds on Fedora aarch64 (expected trivial).

---

## 3. SB3 code inventory & migration cost

**Totals:** 33 `.py` files, **6,972 LOC** under `sb3/` (the 683 KB `ui/sb3.html` is served verbatim and is backend-agnostic by construction — it only talks to `/api/*`).

### Needs real rewrite (the backend-coupled core, ~2,300 LOC)
| File | LOC | Why |
|---|---|---|
| `sb3/sdrangel.py` | 418 | 100% SDRangel deviceset/channel/audio REST client (the mutating layer). |
| `sb3/translator.py` | 340 | apply/verify/unload engine emitting SDRangel REST field names. Orchestration *shape* reusable; body isn't. |
| `sb3/sdrtrunk_client.py` | 115 | tails SDRTrunk logs. Replace with an op25 status observer — **preserve the `digital_*` output keys** the UI expects. |
| `sb3/reconciler/observer.py` | 543 | every read routes through SDRangel functions in `backends.py`. |
| `sb3/reconciler/actions.py` | 250 | the only writing module; all five repair handlers drive `SDRangelClient`/`translator`. |
| `backends.py` (SDRangel half) | ~150 of 315 | `sdrangel_devicesets/channels/...`, phantom model. Other half (icecast/launchd/pgrep) is reusable. |
| `routes.py` (write path) | ~300 of 742 | `apply/filter/tune/vfo_mute/volume` + `build_status/heartbeat` PATCH SDRangel. Rest (system/wizard/stubs) agnostic. |

### Reusable skeleton (carries over, ~3,000+ LOC)
Kill switch (`killswitch.py` 466), launchctl settle (`settle.py`), git deploy (`gitdeploy.py`, `update.py`), plist install (`install.py`), ownership boundary (`ownership.py` — edit label strings to op25/rtl-airband), fail-closed State (`state.py`), CLI (`__main__.py`, `bin/sb3-ctl`), reconciler **safety + config** (RateLimiter, FailureCounter, BackendGuard, off-switches), reconciler **classifier taxonomy** (CLEAN/BENIGN/RECOVERABLE/BROKEN policy is pure — only its field names are SDRangel-shaped), profile **validator** (`profile.py` — one-demod, one-keepalive, camp-fit checks), the whole **wizard/HPDB** path (`ui/wizard.py`, `ui/hp_favorites_wizard.py` — RadioReference SQLite, zero backend coupling), the HTTP server shell (`ui/server.py`), and **`sb3.html`**.

### The clean seam
The rest of the code already funnels through two choke points: the read-only `backends.py` observers and the `SDRangelClient` interface. **An op25/RTL-Airband backend primarily replaces those two surfaces plus the `routes.py` write path**, behind the existing `Profile` → apply → observe → classify contracts. That's why the coupling is concentrated, not smeared.

### Cost in "SB3 sub-phase equivalents"
- **Phase 0 (bring-up):** OS install + `dnf` SDR stack + build op25/RTL-Airband + prove each SDR enumerates + op25 decodes Cumberland. (Not code — environment.)
- **Phase A ≈ SB3 Phase 1:** op25/RTL-Airband **control client** + apply/unload engine (replaces `sdrangel.py` + `translator.py` write path).
- **Phase B ≈ SB3 Phase 2:** **observers** — op25 status observer (replaces `sdrtrunk_client.py`) + backend-agnostic `backends.py` rewire + UI `build_status/heartbeat` + write endpoints.
- **Phase C ≈ SB3 Phase 3:** **reconciler** `observer.py` + `actions.py` against op25 state; `profilegen.py` fleet map.

**≈ 3 phases-equivalent (Phase 1+2+3 rebuild of the backend-facing surfaces).** The reused skeleton is what makes it 3 and not 6 — kill switch, deploy, safety, wizard, UI, and validator are done.

---

## 4. Asahi install plan for Neptune (M1 Mac mini, macOS Tahoe)

**Dual-boot, not replace** — keep macOS as the instant fallback (SDRangel + SDRTrunk + Chrome Remote Desktop, all already working) and as the rollback if the pilot stalls.

1. **From macOS Terminal:** `curl https://alx.sh | sh` — the official Asahi installer. It partitions non-destructively and preserves macOS. [Asahi install], [Jeff Geerling M1 mini]
2. **Partition:** minimum 30 GB to boot, ~100 GB to use properly. **Recommend 200 GB** on Neptune (GNU Radio + op25 build trees, recordings, room to breathe). Enter as GB or %. [Asahi partitioning cheatsheet]
3. **Distro:** choose **Fedora Asahi Remix** (the flagship, best-supported; ships GNU Radio 3.10 + SDR packages).
4. **Boot selection:** hold the power button → pick macOS or Asahi. (No USB-boot; Apple Silicon can't, by design — irrelevant to dual-boot.)
5. **Post-install first steps (in order):**
   1. `dnf install gnuradio gr-osmosdr rtl-sdr rtl-sdr-devel soapysdr soapy-rtlsdr` — plug an RTL dongle, `rtl_test` → confirm enumerate.
   2. **op25:** clone boatbod, translate `install.sh` apt deps to `dnf`, build against Fedora's GNU Radio 3.10.
   3. **RTL-Airband:** build from source (CMake); wire to icecast (reuse the two-box mount pattern).
   4. **Fan control:** load `macsmc-hwmon`, set `fan_control=1`, confirm hwmon fan node — validate under a GNU Radio load.
   5. **SDRplay (optional):** run the ARM64 `.run`, start `sdrplay_apiService`, build `SoapySDRPlay3` — only if we want the RSPs on Linux. Skip for the core mission.
   6. Verify each SDR enumerates; run op25 on the Cumberland CCs (`-2 -w --tdma-cc`) → first decode.

---

## 5. Migration risk assessment

- **What's NOT recoverable if the pilot fails?** **Nothing.** Dual-boot leaves macOS + the internal SB6/SDRangel/SDRTrunk stack byte-for-byte intact. Reboot to macOS = today's rig, unchanged. The only cost of a failed pilot is the evening spent and the disk partition (reclaimable).
- **Fastest path "Asahi installed → op25 decoding Cumberland Public Safety":** dnf stack (~30 min) → build op25 with dnf dep-mapping (~1–3 hrs, the main unknown) → write the Cumberland `trunk.tsv` (~30–60 min) → run on an RTL dongle → decode. **≈4–6 hrs / one focused evening**, no surprises.
- **If Asahi USB SDR support has gaps:** (a) stick to **RTL-SDR** (single-function, best-supported) and shelve SDRplay-on-Linux; (b) if the RTLs themselves misbehave on Asahi USB, that's the signal to **fall back to the SBC** (below) and leave Neptune on macOS.
- **Residual unknowns to validate during the pilot:** RTL-Airband on Fedora aarch64 (expected trivial); op25 dnf dep completeness; whether the macOS-era RSPduo USB-detach issue has any Asahi analog (test only if we choose to use SDRplay).

---

## 6. Alternative: dedicated Linux SBC

Instead of Asahi on Neptune, run the scanner on a **dedicated Linux box** and leave Neptune on macOS as the listener/UI/CRD host.

| | **Asahi on Neptune** | **Pi 5 (8 GB) / small x86 (N100) SBC** |
|---|---|---|
| SDR stack | Fedora: op25 needs manual build; SDRplay unofficial | Debian/Ubuntu: op25 `install.sh` **just works**; the *exact* proven scannerbox stack |
| Compute | **M1-class** (strong for GNU Radio) | Pi 5 adequate for a few SDRs; watch USB bandwidth |
| Hardware | **reuses Neptune**, no new box | +1 physical box (~$80–160) |
| Audio path | local | network (icecast over LAN — **we already run this** two-box pattern; cf. philly-exit Pi `rtl_tcp`) |
| Risk | Asahi maturity + Fedora adaptation | **lowest** — battle-tested distro + stack |
| Fallback | macOS on same box (dual-boot) | Neptune macOS untouched |
| Moving parts | fewest (one box) | more (two boxes, LAN audio) |

**Which is actually simpler?** To *first decode*, the **SBC is simpler** — apt-based `install.sh` reproduces the known-good scannerbox stack with no distro translation, and this project **already has** the network-audio and remote-Pi patterns (two-box icecast harness; philly-exit `rtl_tcp`). Asahi is the more *elegant consolidation* (one box, M1 compute, no LAN audio hop) and is **zero-risk to try** because dual-boot preserves macOS — but it costs an extra evening of Fedora dep-mapping and carries Asahi-maturity unknowns.

**Net:** pilot Asahi as greenlit (cheap to attempt, high upside, reversible); if Fedora friction or USB maturity bites after ~2 evenings, pivot to a Pi 5 SBC — same op25+RTL-Airband endpoint, lower risk, at the cost of one more box on the shelf.

---

## Sources

- SDRplay — [API page](https://www.sdrplay.com/api/) · [Downloads](https://www.sdrplay.com/downloads/) · [ARM64 GR source](https://www.sdrplay.com/arm64-gr-source/) · [API on Fedora 29 (forum)](https://www.sdrplay.com/community/viewtopic.php?t=4000)
- [sdr-enthusiasts `install_sdrplay.sh` (arm64 handling)](https://github.com/sdr-enthusiasts/install-libsdrplay/blob/main/install_sdrplay.sh) · [ZR6LSD SDRplay-ARM64](https://github.com/ZR6LSD/SDRplay-ARM64)
- [Fedora SDR wiki](https://fedoraproject.org/wiki/SDR) · [gnuradio 3.10.12 Fedora 43 pkg](https://packages.fedoraproject.org/pkgs/gnuradio/gnuradio/fedora-43.html) · [gr-osmosdr Fedora pkg](https://packages.fedoraproject.org/pkgs/gr-osmosdr/gr-osmosdr/) · [rtl-sdr Fedora pkg](https://packages.fedoraproject.org/pkgs/rtl-sdr/rtl-sdr/fedora-35.html) · [soapy-rtlsdr Fedora pkg](https://packages.fedoraproject.org/pkgs/soapy-rtlsdr/)
- op25 (boatbod) — [apps README (P25.2 options, trunking)](https://github.com/boatbod/op25/blob/master/op25/gr-op25_repeater/apps/README.md) · [install.sh (apt-based)](https://github.com/boatbod/op25/blob/master/install.sh) · [op25 + GNU Radio 3.10 thread](https://forums.radioreference.com/threads/op25-boatbod-with-gnuradio-3-10.452508/) · [rtl-sdr.com P25 Phase 2 tutorial](https://www.rtl-sdr.com/tutorial-on-setting-up-op25-for-p25-phase-2-digital-voice-decoding/) · [RadioReference: SDRplay + op25](https://forums.radioreference.com/threads/sdr-play-and-op25.461171/)
- Asahi Linux — [M1 feature support](https://asahilinux.org/docs/platform/feature-support/m1/) · [FAQ](https://asahilinux.org/docs/project/faq/) · [Partitioning cheatsheet](https://asahilinux.org/docs/sw/partitioning-cheatsheet/) · [USB 3 kernel support](https://www.techpowerup.com/340231/linux-kernel-may-soon-support-usb-3-x-on-apple-m1-and-m2-macbooks-and-desktops) · [Jeff Geerling: Asahi on M1 Mac mini](https://www.jeffgeerling.com/blog/2022/installing-asahi-linux-alpha-on-my-m1-mac-mini/) · [Fan control for Macs on Linux](https://datcu-andrei-2.gitbook.io/)
