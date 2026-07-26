# Asahi Linux migration — compatibility research (RSP-first)

**Date:** 2026-07-26 · **Branch:** `sb3-asahi-research` · **Scope:** read-only research, **no box changes**.
**Question:** is it worth putting Asahi Linux on Neptune (M1 Mac mini) to run a native Linux SDR stack — **op25 on the RSPduo + chirp on the RSP1B** — replacing the macOS SDRangel + SDRTrunk backend?

> **Rev 2 (RSP-first).** This supersedes the first pass, which led with RTL-SDR. Will's actual hardware plan puts the **SDRplay RSPs as the primary radios**; the RTLs are relegated to ACARS/VDL2/spares. The role model, distro recommendation, and install plan below are re-centered on the RSPs. The structural code analysis (LOC, ~3-phase estimate) is preserved. See **§7 "Why the first pass hedged to RTL"** for the honest accounting.

---

## Corrected role model (Will's fleet plan)

| Radio | Serial | Role on Asahi | Engine |
|---|---|---|---|
| **RSPduo** | `180903EF32` | **Two concurrent trunked P25 systems** — Tuner 1 + Tuner 2 | **op25, dual-instance** (one op25 process per tuner) |
| **RSP1B** | `2405265A60` | **Analog scanning** (airband/NFM) — proven on Ubuntu Scannerbox | **chirp** (SB3-era GNU Radio analog engine, already in-repo) |
| **HackRF One** | `c66c63dc35742683` | Disco / spectrum survey | (unchanged) |
| **RTL ×3** | `95339533` / `61108285` / `56919602` | ACARS, VDL2, or spares | dump1090/acarsdec/dumpvdl2 |

The RSPs are 14-bit, low-drift, well-filtered receivers — materially better than the 8-bit RTL dongles for both trunked-P25 sensitivity and clean analog. Building the stack around them is the right capability call; the cost is more upfront integration (SoapySDRPlay3, a dual-tuner pilot, the chirp port), which is one-time.

---

## Executive summary (verdict + recommendation)

**Verdict: viable, but the RSP-first path front-loads real integration work that the RTL path didn't.** The good news is the two decode engines already exist and were proven on Linux: **op25** (boatbod, the standard P25 Phase 2 decoder) and **chirp** — which is *in this repo* (`chirp/`) as "the GNU Radio + Python analog demodulator that replaces rtl-airband in SB5," purpose-built around the SDRplay. The macOS era swapped chirp+op25 out for SDRangel+SDRTrunk; **Asahi essentially reverts the backend to chirp+op25 and re-points the SB3 control plane at them.**

The two things that need proving, in priority order:

1. **SDRplay v3 API on Asahi.** The API ships generic aarch64 Linux ELF binaries (3.15; RSP1B needs ≥3.14) as a self-extracting `.run` with a systemd `sdrplay_apiService`. It is officially **tested only on Ubuntu LTS**. On **Fedora** it's community-run (as far back as F29) but unofficial; `SoapySDRPlay3`/`gr-osmosdr-sdrplay3` build from source. On **Ubuntu Asahi** it is the vendor-tested userland → lowest risk.
2. **op25 dual-instance on the RSPduo's two tuners.** `multi_rx.py`'s multi-dongle support is documented for P25 *conventional*, not trunked — so two concurrent *trunked* systems means **two op25 processes, one per tuner**. `SoapySDRPlay3` (fventuri) supports the RSPduo in all modes including dual-tuner, but **the specific combo — two concurrent op25 trunking instances each owning one RSPduo tuner, on Asahi — is assembled from parts, not a turnkey recipe. It needs a pilot.** Note the RSPduo caps each tuner at **2 MHz** in dual-tuner mode (vs 10 MHz single); op25 follows wide systems by retuning the tuner, same as an RTL, so this is a latency/verify concern, not a hard blocker.

**Distro recommendation: default to Ubuntu Asahi for an RSP-first build.** SDRplay's API, op25's `install.sh`, and chirp's `requirements.txt` are all Ubuntu/apt-targeted upstream — on Ubuntu Asahi they install as intended, with the only risk being that Ubuntu Asahi is a community remix rather than the Asahi flagship (Fedora Asahi Remix). For a **headless** scanner box the platform-maturity gap is small — the load-bearing parts (kernel, USB, WiFi/Ethernet, **fan control via `macsmc-hwmon`**) are the shared Asahi core, not the desktop polish where Fedora leads. Keep **Fedora Asahi Remix 43** (the most mature platform, GNU Radio 3.10 in-repo) as the fallback if Ubuntu Asahi's platform integration disappoints.

**Migration cost:** unchanged structurally — ~2,300 of 6,972 SB3 LOC is backend-coupled; ~3,000+ LOC skeleton reuses. **≈3 SB3-phase-equivalents**, and the RSP-first path is arguably *cheaper on the analog side* because chirp (and an existing chirp client, `ui/chirp_client.py`) already exist — analog becomes a **port-back**, not a rewrite.

**Recommendation:** proceed with the **dual-boot Asahi pilot** (zero-risk: macOS + SDRangel/SDRTrunk stay intact as instant fallback). Sequence: prove SDRplay on the chosen distro → stand up **one** op25 trunking instance on RSPduo Tuner 1 against **Cumberland Public Safety** (tonight's real target) → add the second instance and pilot dual-tuner → port chirp for RSP1B analog. If SDRplay or the dual-tuner pilot fights on Asahi, fall to the **Pi 5 hybrid** (§8): Pi runs op25+RSPduo, RSP1B stays on macOS SDRangel, Neptune becomes hybrid.

---

## 1. SDR compatibility on Asahi (RSP-first)

| Component | Verdict | Path | Notes / citations |
|---|---|---|---|
| **SDRplay v3 API** (RSPduo, RSP1B) | 🟡→🟢 | `.run` installer (contains x64+ARM32+**ARM64**) + systemd `sdrplay_apiService` | Generic aarch64 ELF; **Ubuntu-tested**, community-run on Fedora. Tier the risk (below). [SDRplay API], [SDRplay downloads], [SDRplay Fedora forum] |
| **SoapySDRPlay3** (RSPduo all modes) | 🟢 (build) | clone + cmake + make against the API; needs **API ≥3.15** | fventuri's fork explicitly supports **RSPduo all modes + RSPdx**. Build on aarch64 is standard. [SoapySDRPlay3], [fventuri SoapySDRPlay3] |
| **gr-osmosdr (SDRplay 3.x)** | 🟢 (build) | fventuri's `sdrplay3` branch, or gr-osmosdr's `soapy` source over SoapySDRPlay3 | This is what **chirp** uses (`osmosdr.source`). [fventuri gr-osmosdr] |
| **op25 (boatbod)** | 🟢 | GNU Radio 3.10; `-2 -w --tdma-cc` for P25.2; SoapySDRPlay source (`IFGR:x,RFGR:y` gains) | On Ubuntu Asahi `install.sh` just works; on Fedora, map apt→dnf. [op25 README], [op25 GR3.10 thread] |
| **chirp** (analog, RSP1B) | 🟢 | Python + GNU Radio 3.10 + gr-osmosdr; **already in-repo** | Designed around SDRplay; ports back to Linux. See §3. |
| **RTL-SDR / HackRF** | 🟢 | stock `dnf`/`apt` packages both distros | For ACARS/VDL2/disco. Single-function USB. [Fedora SDR wiki] |
| **Asahi USB stack** | 🟢 | USB-A since kernel 5.16; single-function devices fine | RSPduo/RSP1B/RTL/HackRF all single-function USB 2.0 → unaffected by the multi-function-device gap. No USB-boot (dual-boot uses internal). [Asahi M1 feature support] |
| **Fan / thermal** | 🟢 | `macsmc-hwmon` (`fan_control=1`) | Resolves the blocker that pushed the project off Linux — and that was the **T2 Intel** mini; Neptune is **M1**, where Asahi drives the fan. [Asahi fan control] |

### SDRplay-on-Asahi — the three tiers (answering the v1 hedge directly)

1. **Tier 1 — Fedora Asahi Remix + the generic arm64 `.run`.** The binaries are distro-neutral glibc ELF; the apiService is a normal systemd unit. Community has run the API on Fedora since F29. Expect to install the `.run`, enable the service, then **build** `SoapySDRPlay3` + `gr-osmosdr` from source (fventuri). Risk: unofficial, possible glibc/udev/path fiddling.
2. **Tier 2 — Ubuntu Asahi.** SDRplay's **Ubuntu-LTS-tested** build runs as the vendor intends; op25/chirp apt scripts work natively. **This is the lowest-friction base for an RSP-first stack** and, given the RSPs are primary, is a strong argument to make Ubuntu Asahi the *default*, not the fallback.
3. **Tier 3 — Pi 5 hybrid.** If Asahi (either distro) fights the RSPs, a Pi 5 runs the battle-tested Debian SDRplay+op25 stack; RSP1B stays on macOS SDRangel; Neptune is hybrid. (§8)

---

## 2. op25 + RSPduo dual-tuner — documented, or pilot?

**Answer: assembled from documented parts, but the exact configuration needs a pilot — no turnkey recipe exists.**

- **Two trunked systems ⇒ two op25 instances.** `multi_rx.py` does multi-system/multi-channel concurrent operation, but its *multiple-dongle* support is documented for **P25 conventional, YSF, DMR, DStar — not trunked**. For two concurrent **trunked** P25 systems the reliable pattern is **one op25 process per tuner**, which is exactly Will's "dual-instance" plan. [op25 multi_rx thread], [op25 issue #184]
- **RSPduo dual-tuner = 2 MHz per tuner** (vs 10 MHz single-tuner). A trunk system whose channels span >2 MHz (e.g. Cumberland's Crossville site spans ~7 MHz) is still decodable because op25 **retunes** the tuner to follow control→traffic grants (same as a 2.4 MHz RTL does); the 2 MHz is instantaneous capture width, not a coverage ceiling. Verify: retune latency / missed call-starts under dual-tuner mode. [SDRplay RSPduo]
- **The unproven link:** two SoapySDRPlay3 clients each opening one tuner of the *same* RSPduo in dual-tuner mode, each driving an independent op25 trunking instance, on Asahi. SoapySDRPlay3 supports RSPduo dual-tuner (fventuri), and running two op25 instances is routine — but **this precise stack is not documented end-to-end. Pilot it: first one instance on Tuner 1 (Cumberland), then add Tuner 2.**
- **Historical caveat:** community guidance has long been "avoid RSP for P25 trunking — little software supports it" — that reflects tooling convenience, not a hardware limit. op25+SoapySDRPlay does work; it's just less trodden than op25+RTL. [RadioReference SDRplay+op25]

---

## 3. chirp — the analog engine already exists (in this repo)

`chirp/` is present and substantial (`daemon.py` ~95 KB, `dsp/`, `cli.py`, `metrics.py`, `audio_probe.py`, full `tests/`). From `chirp/README.md`:

> "chirp is the GNU Radio + Python analog demodulator that **replaces rtl-airband in SB5**. It runs as a long-running daemon per band (airband, ground) and exposes a **JSON command bus (UDP, loopback)** so the dashboard can retune channels, change squelch, and add/remove scan slots **without restarting the SDR**."

- **Framework:** Python + **GNU Radio 3.10** + **gr-osmosdr** (`osmosdr.source`); its probe log shows `gr-osmosdr 0.2.0.0 / gnuradio 3.10.9.2`. Deps in `requirements.txt` are apt-style (`gnuradio python3-gi python3-numpy`).
- **Built for the SDRplay:** the entire README/PROGRESS is SDRplay-wedge lore (`sdrplay_apiService` recovery, shared-memory semaphores). It is the RSP1B analog engine Will wants, and it already implements the hot-retune JSON bus that SB3's control plane can drive.
- **Porting cost:** low. It ran on Ubuntu Scannerbox; on Asahi it needs GNU Radio 3.10 (present both distros) + gr-osmosdr with SDRplay (SoapySDRPlay3 or fventuri's sdrplay3 branch — the same build as op25). **Analog is a port-back, not a rewrite.**
- **Control client already exists:** `ui/chirp_client.py` + `ui/chirp_adapter.py` (SB5/SB6 airband-ui) speak chirp's JSON bus. SB3 can adopt/adapt that instead of writing an analog client from scratch — which **reduces** the rewrite below on the analog side.

---

## 4. SB3 control-plane inventory & migration cost (preserved from rev 1)

**Totals:** 33 `.py`, **6,972 LOC** under `sb3/` (683 KB `ui/sb3.html` served verbatim, backend-agnostic).

**Needs real rewrite (backend-coupled, ~2,300 LOC):** `sdrangel.py` (418, the mutating REST client) → op25/chirp control clients; `translator.py` (340, apply/verify/unload) → op25 TSV + chirp JSON-bus apply; `sdrtrunk_client.py` (115, log observer) → op25 status observer (**preserve `digital_*` output keys**); `reconciler/observer.py` (543) + `reconciler/actions.py` (250) → op25/chirp state + repairs; SDRangel halves of `backends.py` (~150/315) and the `routes.py` write path (~300/742).

**Reuses unchanged (~3,000+ LOC):** kill switch (`killswitch.py` 466), launchctl settle, git deploy/update, plist install, ownership boundary (edit label strings to op25/chirp), fail-closed `state.py`, CLI, reconciler **safety+config** and **classifier taxonomy** (CLEAN/BENIGN/RECOVERABLE/BROKEN policy is pure), profile **validator**, the entire **wizard/HPDB** path, the HTTP server shell, and `sb3.html`.

**Cost: ≈3 SB3-phase-equivalents** — Phase A (op25+chirp control clients / apply engine), Phase B (observers + UI status/write endpoints), Phase C (reconciler observer+actions + profilegen fleet map). The clean seam is the `backends.py` observer boundary + the `SDRangelClient` interface — the two choke points everything already funnels through. **RSP-first lowers analog cost:** chirp + `ui/chirp_client.py` already exist, so Phase A's analog half is adaptation, not greenfield.

---

## 5. Install plan for Neptune (M1 Mac mini) — RSP-first sequence

**Dual-boot, keep macOS** as instant fallback (SDRangel + SDRTrunk + Chrome Remote Desktop). Reboot = today's rig.

1. **Pick the distro by the SDRplay test.** Default **Ubuntu Asahi** (vendor-tested SDRplay, apt-native op25/chirp). If its M1-mini platform integration disappoints on a headless box, use **Fedora Asahi Remix 43**. Install via `curl https://alx.sh | sh` (Ubuntu Asahi has its own installer flow at ubuntuasahi.org); **partition ≥200 GB** for GNU Radio/op25 build trees + recordings. [Asahi install], [Ubuntu Asahi], [Asahi partitioning]
2. **SDRplay v3 API** — run the arm64 `.run`, enable `sdrplay_apiService`, confirm `sdrplay_apiService --version` and that `SoapySDRUtil --find` sees both RSPs. (Tier-1 Fedora / Tier-2 Ubuntu per §1.)
3. **SoapySDRPlay3 + gr-osmosdr** (fventuri) from source; `SoapySDRUtil --probe` the RSPduo in **dual-tuner** mode.
4. **op25** (boatbod) — Ubuntu: `install.sh`; Fedora: apt→dnf mapping against GNU Radio 3.10. Build JMBE/imbe vocoder.
5. **First decode target — Cumberland Public Safety on RSPduo Tuner 1** (tonight's real use case): write the op25 `trunk.tsv` from HPDB (CCs 453.650 / 460.1125 / 460.2125 / 460.625, P25.2, `--tdma-cc`), run one op25 instance → verify Crossville PD (TG 300) / Sheriff (600) / EMS (200) / Fire (400).
6. **Add op25 instance #2 on Tuner 2** — pilot the dual-tuner concurrency (§2). Second trunked system of Will's choice.
7. **chirp for RSP1B analog** — port-back (§3): GNU Radio 3.10 + gr-osmosdr-sdrplay, start the airband daemon, verify Crossville CTAF/AWOS/Guard, wire to icecast (`neptune.mp3`).
8. **RTLs** → ACARS/VDL2/disco as spares.

---

## 6. Risk assessment

- **What's NOT recoverable if the pilot fails? Nothing.** Dual-boot leaves macOS + SDRangel/SDRTrunk intact; reboot restores today's rig. Cost of failure = the evenings spent + a reclaimable partition.
- **Fastest path "Asahi installed → op25 decoding Cumberland":** SDR stack + SDRplay + SoapySDRPlay3 build (~2–4 hrs, the SDRplay build is the main unknown) → op25 build (Ubuntu: ~1 hr; Fedora: +dep-mapping) → Cumberland `trunk.tsv` → single-instance decode. **≈one focused day** (longer than the RTL path's evening, because of the SoapySDRPlay3/API build).
- **Biggest technical risk: the dual-tuner op25 pilot (§2)** — unproven end-to-end. Mitigate by validating single-instance first; if dual-tuner is flaky, run **one trunked system per physical radio** (RSPduo single-tuner 10 MHz for the priority system; a second SDR for the second system).
- **Second risk: SDRplay-on-Fedora friction** — mitigated by defaulting to **Ubuntu Asahi**.
- **Validate during pilot:** chirp on Asahi gr-osmosdr-sdrplay; whether the macOS-era RSPduo USB-detach pathology has any Asahi analog (it was a macOS/driver interaction, not assumed to carry).

---

## 7. Why the first pass hedged to RTL (honest accounting)

Rev 1 led with RTL-SDR for two reasons, and both were about *my* integration convenience, not Will's mission:

1. **op25's documentation is RTL-centric.** Its tutorials, `install.sh`, and example `--args 'rtl'` all assume RTL dongles; RSP-via-SoapySDR is repeatedly called "more complicated," and community guidance is "avoid RSP for P25 trunking." Leading with RTL meant walking a paved road.
2. **It sidestepped the SDRplay-on-Fedora question** — the one genuine unknown — instead of confronting it.

That optimized for path-of-least-resistance, which is the wrong objective here. Will has **two 14-bit RSPs**; RTL-first would leave the better radios on the shelf and build the system around 8-bit dongles. The correct call is RSP-first, and the price is honest: **more upfront integration** (build SoapySDRPlay3, pilot the dual-tuner op25 config, port chirp) and **choosing the distro to make SDRplay easy (Ubuntu Asahi)** rather than choosing the distro that's most polished (Fedora). The RTLs still have a role — ACARS/VDL2/disco — just not as the primary decoders.

---

## 8. Fallback: Pi 5 hybrid (tier 3)

If Asahi fights the RSPs, don't abandon the RSP-first plan — **relocate it**:

- **Pi 5 (8 GB)** runs the battle-tested Debian **SDRplay + op25** stack (apt-native, zero distro friction) with the **RSPduo** for the two trunked systems.
- **RSP1B stays on Neptune/macOS SDRangel** for analog (today's working setup) — no regression.
- Neptune becomes the **hybrid listener/UI/CRD host**; Pi audio reaches it over the LAN via the **icecast pattern this project already runs** (two-box Venus/Neptune harness; philly-exit Pi `rtl_tcp`).

| | **Asahi on Neptune (RSP-first)** | **Pi 5 hybrid** |
|---|---|---|
| SDRplay | Ubuntu Asahi vendor-tested / Fedora unofficial | Debian, fully proven |
| op25 install | Ubuntu apt / Fedora dep-map | apt `install.sh`, just works |
| Compute | M1-class (strong for GNU Radio) | adequate; watch USB bandwidth on the RSPduo |
| Hardware | reuses Neptune | +1 box (~$80–120) |
| Analog | chirp on RSP1B (port-back) | RSP1B stays macOS SDRangel |
| Risk | Asahi maturity + dual-tuner pilot | lowest — proven stack |
| Reversibility | dual-boot | Neptune untouched |

**Simpler to first-decode:** the Pi 5. **More elegant / consolidated / better compute:** Asahi on Neptune. Since dual-boot makes Asahi zero-risk to attempt, pilot it; if the SDRplay build or dual-tuner concurrency disappoints after ~2 evenings, move the RSPduo to a Pi 5 and keep Neptune's RSP1B analog exactly as it is today.

---

## Sources

- SDRplay — [API](https://www.sdrplay.com/api/) · [Downloads](https://www.sdrplay.com/downloads/) · [RSPduo (dual-tuner 2 MHz / single 10 MHz)](https://www.sdrplay.com/rspduo/) · [API on Fedora (forum)](https://www.sdrplay.com/community/viewtopic.php?t=4000)
- SoapySDRPlay3 / gr-osmosdr — [pothosware SoapySDRPlay3](https://github.com/pothosware/SoapySDRPlay3) · [fventuri SoapySDRPlay3](https://github.com/fventuri/SoapySDRPlay3) · [fventuri gr-osmosdr (SDRplay 3.x, RSPduo all modes)](https://github.com/fventuri/gr-osmosdr)
- op25 (boatbod) — [apps README (P25.2 `-2 -w --tdma-cc`, trunking)](https://github.com/boatbod/op25/blob/master/op25/gr-op25_repeater/apps/README.md) · [multi_rx multi-dongle limits (RadioReference)](https://forums.radioreference.com/threads/true-trunk-tracking-with-op25-multi_rx-py-and-2-sdr-dongles.425300/) · [multi_rx single-dongle trunk (issue #184)](https://github.com/boatbod/op25/issues/184) · [SDRplay + op25 (RadioReference)](https://forums.radioreference.com/threads/sdr-play-and-op25.461171/)
- Asahi / distros — [Fedora Asahi Remix](https://asahilinux.org/fedora/) · [Fedora Asahi Remix 43 release](https://www.linuxteck.com/fedora-asahi-remix-43-apple-silicon/) · [Ubuntu Asahi](https://ubuntuasahi.org/) · [Ubuntu Asahi deep-dive](https://linuxvox.com/blog/ubuntu-asahi/) · [M1 feature support](https://asahilinux.org/docs/platform/feature-support/m1/) · [Partitioning cheatsheet](https://asahilinux.org/docs/sw/partitioning-cheatsheet/) · [Fan control for Macs on Linux](https://datcu-andrei-2.gitbook.io/) · [Jeff Geerling: Asahi on M1 mini](https://www.jeffgeerling.com/blog/2022/installing-asahi-linux-alpha-on-my-m1-mac-mini/)
- Fedora SDR packages — [Fedora SDR wiki](https://fedoraproject.org/wiki/SDR) · [gnuradio 3.10.12 (F43)](https://packages.fedoraproject.org/pkgs/gnuradio/gnuradio/fedora-43.html) · [gr-osmosdr](https://packages.fedoraproject.org/pkgs/gr-osmosdr/gr-osmosdr/)
- In-repo — `chirp/README.md`, `chirp/PROGRESS.md`, `ui/chirp_client.py`, `ui/chirp_adapter.py`
