# Asahi Linux migration — stability-hardened plan (rev 4)

**Date:** 2026-07-26 · **Branch:** `sb3-asahi-research` · **Scope:** read-only research, **no box changes**.
**Target:** **Ubuntu Server 24.04 LTS (Asahi)** on Neptune (M1 Mac mini), running a hardened Linux SDR stack, replacing macOS SDRangel + SDRTrunk-on-macOS.

> **Rev 4 — stability first.** Same locked stack as rev 3 (SDRTrunk headless / chirp / disco / 3× RTL), re-engineered so every subsystem is isolated, supervised, observable, rolls back cleanly, and is **burned in before the next one is added**. The headline gate: the reconciler stays **PASSIVE for ≥168 h (7 days)** and only graduates to ACTIVE after clean drift logs. Prior revs 1–3 are superseded.

---

## What's different in rev 4 (the one-page "why this is stable now")

| Dimension | rev 3 | **rev 4 (hardened)** |
|---|---|---|
| Base OS | "Ubuntu Asahi" (unversioned) | **Ubuntu Server 24.04 LTS Asahi** — headless, LTS, no desktop |
| Identity | ambiguous (macOS root/HOME pain) | **dedicated non-root `scannerproject` user**; no root services |
| Config location | `~/SDRTrunk/`, user home | **`/etc/scannerproject/`** (config) + `/var/lib/scannerproject/` (runtime) + `/var/log/scannerproject/` (logs, rotated) |
| Process model | "run it" | **one systemd unit per subsystem**, `Restart=on-failure`, `After/Wants/Requires` graph, crash-isolated |
| SDR readiness | assumed | **`sdrplay-apiService` unit + udev rules + a oneshot enumeration gate** that blocks decoders until both RSPs are present |
| Headless X | Xvfb (`xvfb-run`) | **separate `Xvfb :99` unit** SDRTrunk depends on (not `xvfb-run`; **not xpra** — see §3); survives SDRTrunk restarts |
| Audio | ad-hoc bridge | **`neptune-mixer` ffmpeg systemd unit** + apt icecast2; per-role mounts as fallback |
| Reconciler | "PASSIVE then ACTIVE" | **enforced 168 h PASSIVE**, `sb3-ctl reconciler graduate` with prerequisite checks |
| Rollout | "one evening" | **staged A–F, 48 h burn-in between each subsystem**, each stage a durable checkpoint |
| Rollback | "reboot to macOS" | reboot to macOS **+ per-service `systemctl stop` isolation + wipeable partition + Pi 5 backing plan** |
| Observability | logs somewhere | **journald per unit + `/var/log/scannerproject/` rotated + `/healthz` per service from day 1** |

**Net:** rev 3 proved the stack is *possible*; rev 4 makes it *operable unattended*. Nothing is chained to a risky neighbor; every stage has a known-good checkpoint and a stop button.

---

## Stability principles → concrete design

1. **Minimize failure modes / every step reversible** → systemd `systemctl stop <unit>` isolates any subsystem; macOS dual-boot is the whole-rig rollback; the Asahi partition is wipeable from macOS Disk Utility.
2. **LTS + stable branches** → Ubuntu **24.04 LTS**, **Temurin JDK 21 LTS**, SDRTrunk stable release, GNU Radio 3.10 from the LTS repo, op25 stable branch pre-staged.
3. **Test in isolation before combining** → the burn-in ladder (§8): each SDR/decoder is proven alone for 48 h before the next joins.
4. **Durable known-good checkpoints** → each of the 10 checkpoints is a state you can return to; config is version-controlled and deployed, not hand-edited live.
5. **Persistent config in `/etc`** → `/etc/scannerproject/{sdrtrunk,chirp,disco,rtl,audio}/`; runtime writable state in `/var/lib/scannerproject/`; **no service reads/writes a user home.**
6. **systemd hardening** → `Restart=on-failure`, `RestartSec`, `After/Wants/Requires`, `TimeoutStopSec`, `WatchdogSec` where supported, resource caps.
7. **Dedicated non-root user** → `scannerproject` (in `plugdev`/`dialout` + a `sdr` group for udev-owned devices). Kills the root/HOME confusion permanently.
8. **Observability from day 1** → journald per unit, `/var/log/scannerproject/*.log` with `logrotate`, a `/healthz` on the UI and a per-decoder liveness probe.
9. **Isolation / no cascade** → decoders use `Wants/After` (soft) for ordering, `Requires` only on their true hard dep (`sdrplay-apiService`); a chirp crash cannot stop SDRTrunk; the mixer degrades to whatever mounts are live.

---

## Locked stack & role model (unchanged)

| Radio | Serial | Role | Engine | systemd unit |
|---|---|---|---|---|
| RSPduo | `180903EF32` | 2 concurrent trunked P25 (Tuner 1+2) | **SDRTrunk headless** | `sdrtrunk.service` |
| RSP1B | `2405265A60` | Analog (airband/NFM) | **chirp** (in-repo) | `chirp-airband.service` |
| HackRF | `c66c63dc35742683` | Disco / RF classifier | **disco** (in-repo) | `disco-*.service` |
| RTL SMArTee (bias-tee) | `95339533` | Radiosonde | radiosonde_auto_rx | `radiosonde-autorx.service` |
| RTL SMArt | `61108285` | ACARS | acarsdec (f00b4r0) | `acarsdec.service` |
| RTL SMArt | `56919602` | VDL2 | dumpvdl2 (szpajder) | `dumpvdl2.service` |

Coherence + engine details are unchanged from rev 3 (§SDRTrunk-headless is the one pilot risk; disco needs a HackRF classifier re-validation). This rev is about *how* they run, not *what* they are.

---

## 1. Distro — final recommendation

**Use Ubuntu Server 24.04 LTS (Asahi). Honest tradeoff below.**

- **There is no 22.04 Asahi.** Ubuntu Asahi ships **24.04 LTS** (Desktop *and* **Server**) for Apple Silicon — the Asahi kernel requires a newer base than 22.04. 24.04 is itself an LTS (support to 2029), so the "LTS over cutting-edge" principle is satisfied at 24.04. Prefer the **Server** image: no desktop/GPU stack to destabilize a headless box. [Ubuntu Asahi], [Ubuntu Asahi deep-dive]
- **Ubuntu vs Fedora Asahi Remix — the tradeoff:**
  - **Ubuntu Asahi (recommended):** SDRplay API is **vendor-tested on Ubuntu LTS** (out-of-box `.run`), op25/acarsdec/dumpvdl2/radiosonde_auto_rx/chirp are all **apt-native or apt-dep** — the entire SDR userland installs as upstream intends. Downside: Ubuntu Asahi is a **community remix** (built on Asahi's kernel/drivers), not the Asahi *flagship*, so kernel/platform updates trail Fedora's.
  - **Fedora Asahi Remix 43 (fallback):** the **upstream flagship** — best-maintained kernel/platform, longest Apple-Silicon track record, GNU Radio 3.10 in-repo. Downside for *this* stack: SDRplay is **unofficial on Fedora**, and every SDR tool needs apt→dnf translation and more from-source builds — more moving parts, against principle #1.
- **Verdict:** for an **RSP-centric, stability-first** box, minimizing SDR-userland friction (Ubuntu) outweighs platform-update freshness (Fedora), *because* the load-bearing radios are SDRplay and SDRplay is Ubuntu-tested. Keep Fedora Asahi Remix 43 as the fallback if Ubuntu Asahi's platform integration on the M1 mini proves rough during Checkpoints 1–2.

---

## 0/2. Foundation + SDRplay API hardening

**User & filesystem (Phase C):**
```
useradd -r -m -d /var/lib/scannerproject -G plugdev,dialout scannerproject
install -d -o scannerproject /etc/scannerproject/{sdrtrunk,chirp,disco,rtl,audio}
install -d -o scannerproject /var/lib/scannerproject /var/log/scannerproject
```
`logrotate` drop-in `/etc/logrotate.d/scannerproject` (daily, rotate 14, compress, copytruncate).

**udev — deterministic device ownership** (`/etc/udev/rules.d/70-scannerproject-sdr.rules`):
```
# RTL-SDR (RTL2832U) → scannerproject-owned, stable per-serial symlink
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", \
  MODE="0660", GROUP="scannerproject", SYMLINK+="rtl-$attr{serial}"
# HackRF One
SUBSYSTEM=="usb", ATTRS{idVendor}=="1d50", ATTRS{idProduct}=="6089", \
  MODE="0660", GROUP="scannerproject"
# SDRplay RSPs are claimed by sdrplay_apiService; its .run installs 66-mirics.rules.
```
Reload: `udevadm control --reload && udevadm trigger`. Per-serial symlinks make the ACARS/VDL2/sonde units bind the *right* dongle regardless of USB port order.

**`sdrplay-apiService.service`** — the vendor `.run` installs a service; wrap/verify it as:
```
[Unit]
Description=SDRplay API service
After=network.target
[Service]
Type=simple
ExecStart=/usr/local/bin/sdrplay_apiService
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
```

**`sdr-enum-gate.service`** (oneshot readiness gate — decoders `After`/`Requires` this):
```
[Unit]
Description=Verify RSPduo + RSP1B enumerate before decoders start
After=sdrplay-apiService.service
Requires=sdrplay-apiService.service
[Service]
Type=oneshot
RemainAfterExit=yes
# exits non-zero (blocking dependents) if either serial is missing
ExecStart=/usr/local/bin/sdr-enum-gate.sh 180903EF32 2405265A60
[Install]
WantedBy=multi-user.target
```
`sdr-enum-gate.sh` = `SoapySDRUtil --find | grep -q <serial>` for each; retries N times with backoff. This is the **health check that stops a half-enumerated bus from cascading** into SDRTrunk/chirp failures.

---

## 3. SDRTrunk hardening (the load-bearing subsystem)

- **Java: Temurin JDK 21 LTS** (SDRTrunk's official target; releases also bundle a JRE). Pin it; don't float to 22-beta. [SDRTrunk Getting Started], [JDK 21]
- **Headless display: Xvfb as a *separate* unit — not `xvfb-run`, not xpra.**
  - **Why not xpra:** xpra runs a compositing WM *on top of Xvfb* to add attach/detach — a feature a 24/7 decoder never uses, so it's pure added failure surface. xpra has a documented runaway failure mode (Xorg processes climbing to ~1800 in 6 h, host blocked). For an always-on service, fewer moving parts wins. [xpra man], [xpra runaway issue #4250]
  - **Why a separate `Xvfb :99` unit (not `xvfb-run`):** `xvfb-run` ties the display's lifetime to a single process; when SDRTrunk restarts (`Restart=on-failure`) it would orphan/re-spawn the display. A standing `Xvfb :99` unit lets SDRTrunk reconnect to the same display across restarts, and lets an operator `x11vnc -display :99` to peek on demand without changing the service.
  - Optional upgrade: **`Xorg -config dummy` (Xdummy)** instead of Xvfb — better RandR/memory behavior for long-running JavaFX; keep as a tuning option if Xvfb shows memory creep.
```
# xvfb@.service  (enable: xvfb@99)
[Service]
ExecStart=/usr/bin/Xvfb :%i -screen 0 1280x1024x16 -nolisten tcp
Restart=on-failure
RestartSec=5

# sdrtrunk.service
[Unit]
After=network-online.target sdrplay-apiService.service sdr-enum-gate.service xvfb@99.service
Wants=network-online.target xvfb@99.service
Requires=sdrplay-apiService.service sdr-enum-gate.service
[Service]
User=scannerproject
Environment=DISPLAY=:99 HOME=/var/lib/scannerproject/sdrtrunk
ExecStart=/opt/sdrtrunk/bin/sdr-trunk
Restart=on-failure
RestartSec=30
TimeoutStopSec=45          # SDRTrunk ~6 s clean exit; generous, never SIGKILL early
```
- **Playlist lives in `/etc/scannerproject/sdrtrunk/playlist/default.xml`** (canonical, deployed by SB3). SDRTrunk works on a copy in `$HOME=/var/lib/scannerproject/sdrtrunk` (it rewrites its playlist on exit — the copy-on-deploy pattern we already use avoids clobbering the canonical source). `tuner_configuration.json` (RSP1B blacklist / RSPduo Tuner1+2) likewise deployed from `/etc`.
- **Restart isolation:** a SDRTrunk crash → `Restart=on-failure` after 30 s, reconnects to the standing Xvfb, re-reads the playlist. No root, no Aqua session, no `HOME=/var/root` — **the entire macOS restart saga is gone.**
- **48 h unattended pilot (Checkpoint 4):** run SDRTrunk alone under Xvfb, rotate stop/start cycles, watch RSS/`journalctl` for JavaFX leaks. **If it degrades over 48 h → swap to the pre-staged op25** (native headless; the digital decoder is the only thing that changes — playlist/observer/mixer are decoder-agnostic).

---

## 4. chirp / disco / RTL tools — each an isolated unit

Every decoder is its own `Restart=on-failure` unit; none `Requires` another. Ordering via `After`/`Wants` only.

- **`chirp-airband.service`** — `User=scannerproject`, `Requires=sdrplay-apiService sdr-enum-gate`, config `/etc/scannerproject/chirp/`, JSON bus on loopback, `Restart=on-failure`, `RestartSec=15`. chirp already self-heals the `sdrplay_apiService` wedge (its whole design) — surface that via journald + a `/healthz`.
- **`disco-sweep.service` / `disco-classifier.service`** — HackRF via SoapySDR `driver=hackrf`; no SDRplay dependency, so **fully isolated from the RSP chain** (a HackRF hiccup can't touch digital/analog). Retarget + **classifier re-validation on 8-bit HackRF captures is a Checkpoint-7 line item**, not a blocker.
- **`acarsdec.service`** (RTL `61108285`), **`dumpvdl2.service`** (RTL `56919602` — validate the IQ-artifact-history dongle first), **`radiosonde-autorx.service`** (RTL `95339533` + bias-tee). Each binds its dongle by the udev per-serial symlink, `Restart=on-failure`, own log in `/var/log/scannerproject/`. All three are independent leaf services — any can crash without touching the others.

---

## 5. Audio pipeline as proper Linux services

- **`icecast2`** from apt (well-known, stable); config + source password in `/etc/scannerproject/audio/` (mode 0640, `scannerproject`-owned).
- **`neptune-mixer.service`** — the resilient ffmpeg supervisor (already written for macOS) ported to a unit:
```
[Unit]
After=icecast2.service
Wants=icecast2.service chirp-airband.service sdrtrunk.service
[Service]
User=scannerproject
ExecStart=/usr/local/bin/neptune-mixer.sh    # mixes whichever source mounts are live
Restart=on-failure
RestartSec=10
```
- **One mount `neptune.mp3`** mixing analog + digital; **fallback:** if the mixer struggles under load, drop to **per-role mounts** (`neptune-analog.mp3`, `neptune-trunk.mp3`) and let the client pick — no mixer in the hot path. (`Wants` not `Requires` on the decoders → the mixer runs and degrades gracefully whether or not a given source is up.)

---

## 6. systemd dependency graph (isolation-first)

```
network-online.target
        │
sdrplay-apiService.service ──Requires──► sdr-enum-gate.service (oneshot)
        │                                      │
        │                        ┌─────────────┼───────────────┐
        ▼                        ▼             ▼               (Requires apiService+gate)
   xvfb@99.service        chirp-airband   sdrtrunk.service
        │                  .service            │
        └────Wants/After────────┐              │
                                ▼              ▼
                        (independent leaves; Restart=on-failure each)
   disco-*.service   acarsdec.service   dumpvdl2.service   radiosonde-autorx.service
        │
icecast2.service ──Wants──► neptune-mixer.service ──Wants──► {chirp, sdrtrunk}
sb3-ui.service (/healthz)   sb3-reconciler.service (PASSIVE, §7)
```
**Rule:** `Requires` only points at a true hard dependency (the SDRplay API + enum gate). Everything else is `Wants`/`After` (ordering, not coupling). Result: **no decoder can take down another; the mixer and UI survive any single decoder crash.**

---

## 7. Reconciler — enforced 168 h PASSIVE → graduate

- The reconciler starts and **stays PASSIVE for ≥168 h (7 days)** on the new backend (config off-switch + a graduation gate). It observes and logs drift (CLEAN/BENIGN/RECOVERABLE/BROKEN) but **takes no action**.
- **`sb3-ctl reconciler graduate`** — refuses unless prerequisites pass: (a) ≥168 h of PASSIVE uptime, (b) drift log **0 BROKEN / 0 emergency-pauses**, (c) RECOVERABLE events all explained, (d) backend PIDs stable across the window. Only then does it flip to ACTIVE, and even then with the existing cascade brakes (rate limiter, failure-counter quarantine, BackendGuard emergency-pause). The classifier taxonomy + safety machinery are **reused unchanged** from the current reconciler.
- This is the single most important stability rule: **automation earns ACTIVE by a week of clean observation, not by assertion.**

---

## 8. Burn-in ladder — 10 durable checkpoints (each a known-good state)

| # | Checkpoint | Gate to pass | Rollback |
|---|---|---|---|
| 1 | **Ubuntu boots reliably** | 3 clean unattended boots to Ubuntu default | reboot macOS |
| 2 | **All SDRs enumerate** | 5 devices present every boot for **24 h** | `systemctl stop`; reseat; macOS |
| 3 | **SDRplay apiService stable** | 24 h of looped open/close cycles, no wedge | restart unit; reboot |
| 4 | **SDRTrunk headless survives** | **48 h** unattended + stop/start rotation, no leak | swap to op25 |
| 5 | **Cumberland CC locks + decodes** | reliable lock on RSPduo Tuner 1 (Cumberland; verify vs TACN/Metro) | edit playlist; op25 |
| 6 | **chirp analog** | RSP1B airband proven, `neptune.mp3` audio | `systemctl stop chirp` |
| 7 | **disco HackRF** | sweep + **classifier re-validated** on HackRF samples | disable disco units |
| 8 | **RTL tools** | acarsdec / dumpvdl2 / radiosonde each proven **alone** | stop the one unit |
| 9 | **Reconciler PASSIVE** | **168 h**, 0 false positives / 0 BROKEN | stays passive |
| 10 | **Reconciler ACTIVE** | `sb3-ctl reconciler graduate` prerequisites met | `reconciler passive --execute` |

Each row is only entered after the previous is durable. **48 h burn-in between subsystems in Phase E** — no chaining two unproven things.

---

## 9. Install sequence (phased, paced) + time

- **Phase A — prep (30 min):** back up macOS state; stage the op25 fallback tarball; confirm Cumberland `default.xml` + `tuner_configuration.json` in `/etc` layout.
- **Phase B — Asahi install (60–90 min):** `curl` Ubuntu Asahi installer; **partition ≥200 GB**; **set Ubuntu Server as default boot** so the first reboot lands in Ubuntu unattended. → **Checkpoint 1.**
- **Phase C — base setup (45 min):** create `scannerproject` user + `/etc` `/var/lib` `/var/log` tree; **install Tailscale first** (get on the tailnet before touching SDR — remote hands from anywhere); apt base + GNU Radio 3.10 + icecast2 + Xvfb + Temurin JDK 21; udev rules; SDRplay API `.run` + its service. → **Checkpoints 2–3.**
- **Phase D — first decode (90 min):** SDRTrunk unit + Xvfb unit; deploy Cumberland playlist; lock CC on RSPduo Tuner 1. → **Checkpoint 5. First real decode ~4 h elapsed.**
- **Phase E — roll out the rest, one subsystem per session, 48 h burn-in between each:** Tuner 2 second system → chirp → disco (+ classifier validation) → acarsdec → dumpvdl2 → radiosonde_auto_rx. → **Checkpoints 4/6/7/8.** Deliberately **multi-session over ~2 weeks**, not one evening.
- **Phase F — SB3 code migration (weeks):** the ≈2-phase-equivalent control-plane port (§10), landing the reconciler PASSIVE → **Checkpoints 9–10.**

**Time:**  ~**4 h hands-on to first Cumberland decode**; Phase E paced over ~2 weeks (48 h burn-ins); **reconciler ACTIVE no sooner than 168 h of clean PASSIVE**, realistically **~2–3 weeks calendar** once the backend is stable. Fast to *useful*, slow to *fully autonomous* — on purpose.

---

## 10. Migration cost (unchanged from rev 3: ≈2 SB3-phase-equivalents)

33 `.py` / 6,972 LOC in `sb3/`; SDRTrunk observer (`sdrtrunk_client.py`) **reused as-is**, chirp + `ui/chirp_client.py` + disco **already exist**. Rewrite concentrates on control adapters (digital = deploy `default.xml` + `systemctl restart`; analog = chirp JSON bus), the apply engine, and the reconciler retarget. **Add to the estimate:** translate the launchd plists → the hardened systemd units above (mechanical) and add the three RTL-tool hit-tailers + `/healthz` probes. Still **≈2 phases**; the hardening is unit files + scripts, not new control logic.

---

## 11. Backing plan — Pi 5 (unchanged)

If the Asahi stack proves unstable after Phase D despite the hardening, relocate the RSP digital+analog stack to a **Pi 5 (8 GB)** on proven Debian, keep RSP1B analog on macOS if needed, and aggregate over the LAN via the icecast pattern this project already runs. Neptune stays on macOS. Zero data loss, one box added.

---

## Sources

- Distro — [Ubuntu Asahi (24.04 Desktop + Server)](https://ubuntuasahi.org/) · [Ubuntu Asahi deep-dive](https://linuxvox.com/blog/ubuntu-asahi/) · [Fedora Asahi Remix 43](https://www.linuxteck.com/fedora-asahi-remix-43-apple-silicon/) · [Asahi M1 feature support](https://asahilinux.org/docs/platform/feature-support/m1/) · [Asahi fan control (macsmc-hwmon)](https://datcu-andrei-2.gitbook.io/)
- SDRTrunk — [Getting Started (Java 21)](https://github.com/DSheirer/sdrtrunk/wiki/Getting-Started) · [headless issue #92](https://github.com/DSheirer/sdrtrunk/issues/92) · [JDK 21 LTS](https://openjdk.org/projects/jdk/21/)
- Headless X — [xpra man page](https://manpages.ubuntu.com/manpages/xenial/man1/xpra.1.html) · [xpra runaway-process issue #4250](https://github.com/Xpra-org/xpra/issues/4250)
- SDRplay / SoapySDRPlay3 — [SDRplay API](https://www.sdrplay.com/api/) · [fventuri SoapySDRPlay3 (RSPduo all modes)](https://github.com/fventuri/SoapySDRPlay3) · [fventuri gr-osmosdr](https://github.com/fventuri/gr-osmosdr)
- RTL tools — [f00b4r0/acarsdec](https://github.com/f00b4r0/acarsdec) · [szpajder/dumpvdl2](https://github.com/szpajder/dumpvdl2) · [projecthorus/radiosonde_auto_rx](https://github.com/projecthorus/radiosonde_auto_rx)
- In-repo — `chirp/README.md`, `chirp/requirements.txt`, `ui/chirp_client.py`, `disco/src/`, `sb3/sdrtrunk_client.py`, `sb3/reconciler/`
