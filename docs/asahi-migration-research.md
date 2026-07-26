# Asahi Linux migration — architecture evaluation (rev 3, locked stack)

**Date:** 2026-07-26 · **Branch:** `sb3-asahi-research` · **Scope:** read-only research, **no box changes**.
**Target:** Ubuntu Asahi on Neptune (M1 Mac mini), running a native Linux SDR stack, replacing macOS SDRangel + SDRTrunk-on-macOS.

> **Rev 3.** Will locked the architecture. This evaluates it engine-by-engine and reworks the install plan and cost. Revs 1–2 (RTL-first, then op25-for-digital) are superseded: **digital is now SDRTrunk headless**, which — importantly — is a tool this project already runs and whose log-observer code (`sdrtrunk_client.py`) is **reused unchanged**. LOC counts and the phase-estimate structure are preserved; the estimate **drops** because two of the three decode engines and their client code already exist in-repo.

---

## Locked stack & role model

| Radio | Serial | Role | Engine | Engine status |
|---|---|---|---|---|
| **RSPduo** | `180903EF32` | Two concurrent trunked P25 systems (Tuner 1 + Tuner 2) | **SDRTrunk headless** | exists; used today on macOS |
| **RSP1B** | `2405265A60` | Analog scanning (airband/NFM) | **chirp** | **in-repo** (`chirp/`), Linux-native |
| **HackRF One** | `c66c63dc35742683` | Disco / spectrum survey + RF classifier | **disco** | **in-repo** (`disco/`), SoapySDR-based |
| **RTL (SMArTee, bias-tee)** | `95339533` | **Radiosonde** | radiosonde_auto_rx | mature 3rd-party |
| **RTL (SMArt)** | `61108285` | **ACARS** | acarsdec (f00b4r0) | mature 3rd-party |
| **RTL (SMArt)** | `56919602` | **VDL2** | dumpvdl2 (szpajder) | mature 3rd-party |

**Coherence verdict (the implied question): the stack is coherent, and the Linux move actively *fixes* two things it doesn't just move.** SDRTrunk-under-systemd+Xvfb eliminates the macOS root/`sudo`/Aqua-session/`HOME=/var/root` mess we've been fighting (it becomes a normal `systemctl` service), and SDRTrunk natively drives the **RSPduo dual-tuner** (two tuners → two systems) — the exact thing op25 could only do via an unproven dual-instance pilot in rev 2. Two flagged items, neither a blocker: **(1)** SDRTrunk needs a virtual display (Xvfb) — pilot its 24/7 stability; **(2)** disco must be **retargeted from SDRplay to HackRF** (SoapySDR driver swap + classifier re-validation on 8-bit HackRF samples).

---

## Executive summary + recommendation

Every engine is either already in the repo (SDRTrunk client, chirp, disco) or a mature third-party tool with a clean Ubuntu build (acarsdec, dumpvdl2, radiosonde_auto_rx). The shared platform dependency is the **SDRplay v3 API** (RSPduo for SDRTrunk, RSP1B for chirp) — Ubuntu-LTS-tested, so **Ubuntu Asahi** remains the right base (vendor-tested SDRplay, apt-native everything). Asahi's `macsmc-hwmon` gives fan control on the M1, and the USB-A ports handle all six single-function SDRs.

**The one genuine unknown is SDRTrunk headless (§1).** It is a JavaFX desktop app with no native headless mode (DSheirer issue #92, open for years); the community answer is **Xvfb**. That is workable and, on Linux, cleaner than what we run today — but 24/7 Xvfb+JavaFX stability must be piloted. If it disappoints, the fallback is **op25** (rev 2's plan, native-headless but with the dual-tuner pilot) or **Trunk Recorder**.

**Recommendation:** proceed with the **dual-boot Ubuntu Asahi pilot** (zero-risk; macOS + SDRangel/SDRTrunk stay as instant fallback). Install order (§7): SDRplay API → **SDRTrunk headless under Xvfb** (prove first — it's the load-bearing unknown, and Cumberland Public Safety on RSPduo Tuner 1 is tonight's real target) → chirp on RSP1B → disco retargeted to HackRF → acarsdec/dumpvdl2/radiosonde_auto_rx on the RTLs. If Xvfb+SDRTrunk is flaky after a couple evenings, pivot digital to op25 without changing anything else in the stack.

**Migration cost: ≈2 SB3-phase-equivalents** (down from 3) — because SDRTrunk is reused (its log observer `sdrtrunk_client.py` works unchanged) and chirp/disco + `ui/chirp_client.py` already exist. The rewrite concentrates on the *control* adapters and the reconciler.

---

## 1. SDRTrunk headless — the load-bearing question

**Verdict: needs Xvfb (virtual framebuffer). No native `--nogui`; not a fork; workable and, on Linux+systemd, cleaner than today.**

- **No headless mode.** SDRTrunk is JavaFX and "won't run headless because it needs various GUI bits." The request has been open for years (DSheirer/sdrtrunk **issue #92, "Command line with no GUI"**). `-Djava.awt.headless=true` "is less likely to work" for JavaFX. [SDRTrunk issue #92], [SDRTrunk headless thread]
- **The workaround is Xvfb** — `xvfb-run java -jar sdr-trunk.jar` (or a systemd unit that starts an Xvfb display, then SDRTrunk against it). This is the standard pattern for headless JavaFX. Some run it under a minimal WM/VNC instead. [Xvfb for GUI apps]
- **Config is file-based — no GUI needed to operate.** Channels, aliases, and the icecast broadcaster live in `~/.config/SDRTrunk/playlist/default.xml` (we already edit this directly on Neptune — the whole MTRTRS/Cumberland playlist work was file edits, no GUI). So SB3's "apply" for digital = write `default.xml` + restart the service. Tuner config is `tuner_configuration.json` (the RSP1B blacklist / RSPduo assignment we already manage).
- **Linux fixes the macOS pain.** Today SDRTrunk runs as a `sudo`-launched Aqua-session GUI app with `HOME=/var/root`, unkillable from SSH — the exact saga blocking Cumberland right now. Under **systemd + Xvfb** it becomes a normal user service: `systemctl --user restart sdrtrunk`, clean SIGTERM, predictable `$HOME`, logs to the journal. **This is a net simplification, not a lateral move.**
- **RSPduo dual-tuner is native.** SDRTrunk already enumerates `RSPduo Tuner 1` + `Tuner 2` (we've seen this in Neptune's logs) and can source one channel/system per tuner — delivering the two-concurrent-systems requirement **without** op25's unproven dual-instance pilot. It needs the SDRplay v3 API present (shared with chirp).
- **Observer reused as-is.** `sb3/sdrtrunk_client.py` tails `sdrtrunk_app.log` with regexes for broadcaster/tuner/P25 activity. **The log format is identical on Linux** → this file is **REUSE, not rewrite** (rev 2 had it as needs-rewrite). Big cost drop.
- **Pilot risk:** 24/7 Xvfb+JavaFX robustness (memory footprint, occasional JavaFX exceptions). **Mitigation / fallback:** op25 (native headless; rev 2 dual-instance pilot) or **Trunk Recorder** (native C++ trunking, headless-first) if Xvfb proves unreliable.

---

## 2. chirp on Ubuntu Asahi (analog / RSP1B)

**Buildable — confirmed.** `chirp/requirements.txt` states GNU Radio 3.10 is a **system apt package** (`apt-get install gnuradio python3-gi python3-numpy`), plus pip `pydantic>=2.10`, `numpy`. It uses `osmosdr.source` (probe log: `gr-osmosdr 0.2.0.0 / gnuradio 3.10.9.2`).

- **Deps on Ubuntu aarch64:** `gnuradio` + `gr-osmosdr` + `python3-gi`/`numpy` are apt packages; `pydantic` via pip. The only source build is the **SDRplay bridge** — `SoapySDRPlay3` + `gr-osmosdr` with SDRplay (fventuri's `sdrplay3` branch supports RSPduo all modes), the same build SDRTrunk's SDRplay support relies on the API for. No aarch64-specific blockers.
- **Port issues:** chirp was proven on the (x86_64) Ubuntu Scannerbox; aarch64 is not expected to differ for pure-Python-over-GNU-Radio. The known operational hazard is the `sdrplay_apiService` wedge on daemon bounce (chirp's whole README is about handling it) — **Linux-native and already engineered for**, unlike the macOS variant.
- **`ui/chirp_client.py` — stable interface?** Yes: chirp exposes a **JSON command bus over loopback UDP** (retune / squelch / add-remove scan slot **without restarting the SDR**) and `ui/chirp_client.py` + `ui/chirp_adapter.py` already speak it, with tests (`ui/tests/test_chirp_client.py`, `test_chirp_adapter.py`). It's a deliberate, tested contract — SB3 adopts it rather than writing an analog client. Analog is a **port-back**, not a rewrite.

---

## 3. disco on HackRF (spectrum survey + classifier)

**Status: substantial and complete-ish, not a skeleton — but currently SDRplay-targeted; needs a HackRF retarget.**

- **What's there:** `disco/` has `src/` (`band_plan`, `classifier`, `fingerprint`, `identification`, `current_location`, `dashboard`, `hpdb`, `cdbs`), a `training/` pipeline (RadioML CNN → `export_onnx.py` → `radioml.onnx`), `models/`, `configs/`, `bin/`, and **systemd units** (`disco-sweep@`, `disco-coordinator`, `disco-classifier`). It was deployed on the Micro. It is a full SoapySDR-sweep → ONNX-CNN classification → identification subsystem.
- **SDR path:** SoapySDR, **not** hackrf_sweep — `disco/src/phase0_smoketest.py` opens `SoapySDR.Device({"driver":"sdrplay","serial":...,"mode":"DT"})`. Because it's SoapySDR-abstracted, moving to HackRF is a **driver-string swap** to `{"driver":"hackrf"}` + `soapysdr-module-hackrf` (apt on Ubuntu).
- **The real work is validation, not porting:** the CNN classifier was trained against RSPduo-captured samples at a set rate; HackRF is 8-bit (vs 14-bit RSP) with up to 20 MHz (wider than RSP's 10). Sweep parameters and possibly a classifier re-validation/retrain on HackRF captures are needed. `training/synthesize.py` + `train_radioml.py` exist to regenerate the model. **Flag: budget a HackRF classifier-validation pass; the plumbing is done.**
- **SoapyHackRF on Ubuntu Asahi:** `apt install soapysdr-module-hackrf hackrf` — HackRF arm64 support is mature. 🟢

---

## 4. RTL dongles — tool selection + serial→role mapping

| Role | Tool (recommended) | Why / install | Citation |
|---|---|---|---|
| **ACARS** | **`f00b4r0/acarsdec`** | TLeconte's original is now **legacy**; the f00b4r0 fork is the maintained one — multi-channel (up to 8 freqs/dongle), rtl_sdr/**soapysdr**/airspy/sdrplay front-ends, optional `acarsserv` DB. Build from source (cmake); compiles on any modern Linux incl. aarch64. | [f00b4r0/acarsdec], [TLeconte/acarsdec (legacy)] |
| **VDL2** | **`szpajder/dumpvdl2`** | Canonical VDL Mode 2 decoder (Tomasz Lemiech). Build from source: apt `build-essential cmake libglib2.0-dev librtlsdr-dev` + **libacars** dep, then cmake/make. Up to 8 VDL2 channels/dongle. openwebrx maintains a Debian-packaging fork if we want `.deb`. | [szpajder/dumpvdl2], [rtl-sdr.com dumpvdl2] |
| **Radiosonde** | **`projecthorus/radiosonde_auto_rx`** | Active, RTL/AirSpy, multi-sonde (RS41/RS92/DFM/iMet), auto-scan+decode, web UI + telemetry upload. Ubuntu apt deps documented; **Docker image is the recommended install**. | [radiosonde_auto_rx], [rtl-sdr.com auto_rx] |

**Note on attribution:** acarsdec is Thierry Leconte's (not Fabrice Bellard's); dumpvdl2 is Tomasz Lemiech / *szpajder* (not Szymon Ludwiczak). Recommending the maintained forks above. **acars-decoder-typescript is a parsing *library*, not an RTL front-end — acarsdec is the correct choice** for decoding off a dongle.

### Serial → role mapping (capability-driven)

- **`95339533` (Nooelec SMArTee — has bias-tee) → Radiosonde.** Radiosondes (400–406 MHz, often weak/distant balloon signals) benefit most from a mast **LNA powered over the coax** — exactly what the SMArTee's bias-tee provides. `radiosonde_auto_rx` can also toggle the bias-tee.
- **`61108285` (SMArt) → ACARS** (~131 MHz, strong line-of-sight aircraft — no LNA needed).
- **`56919602` (SMArt) → VDL2** (~136 MHz). ⚠️ **Validate this dongle first** — its history includes a 1275 Hz internal IQ artifact cleared only by a USB replug (the old "VFO Nooelec" incident; it was the fleet "sounding" dongle). VDL2's digital decode is fairly tolerant, but confirm a clean spectrum before committing it.
- Frequency ranges don't constrain the mapping — all three are R820T2/RTL2832 (~24–1766 MHz) and cover 131/136/400 MHz. The bias-tee is the only hardware differentiator, so it drives the radiosonde assignment.

---

## 5. Aggregation / UI backend routing

`sb3.html` (683 KB, served verbatim) talks only to `/api/*` — so all migration work is behind that contract. New routing, **no SDRangel REST anywhere**:

| Surface | Source on Linux | Mechanism | Reuse? |
|---|---|---|---|
| Digital status/hits | **SDRTrunk** | tail `sdrtrunk_app.log` → `digital_*` dict | **`sdrtrunk_client.py` reused as-is** |
| Digital control | **SDRTrunk** | write `default.xml` + `systemctl restart sdrtrunk` | new (thin: file + service) |
| Analog status/control | **chirp** | JSON command bus (loopback UDP) | **`ui/chirp_client.py` reused** |
| Disco | **disco** | its `state/` + `dashboard.py` + classifier output | adopt disco's own surface |
| ACARS / VDL2 / Radiosonde | acarsdec / dumpvdl2 / auto_rx | each emits text/JSON (acarsdec → UDP/JSON or `acarsserv`; dumpvdl2 → JSON/ZMQ; auto_rx → telemetry/log) | new: 3 small log/JSON tailers → unified hits feed |

**Sketch:** `/api/status` fans out to {`sdrtrunk_client` (digital), `chirp_client` (analog), `disco` state, ACARS/VDL2/sonde tailers}; `/api/hits` aggregates SDRTrunk call events + chirp hits + the three RTL-tool feeds into the existing activity feed. The aggregation pattern (tail a log/JSON, normalize to a hit) is exactly what `sdrtrunk_client.py` already does — replicate it three times for the RTL tools. No new protocol; a `HitSource` interface with 6 implementations behind the current `/api/*`.

---

## 6. SB3 control-plane inventory & revised cost

**Totals (unchanged):** 33 `.py`, **6,972 LOC** under `sb3/`; `ui/sb3.html` (683 KB) served verbatim. Separately in-repo and now **reused as engines**: `chirp/` (large — `daemon.py` ~95 KB + dsp/tests) and `disco/` (full subsystem).

**What changes vs rev 2 (the cost drop):**
- `sb3/sdrtrunk_client.py` (115) — **REUSE** (was rewrite). Linux SDRTrunk logs identically.
- Analog client — **REUSE** `ui/chirp_client.py` / `chirp_adapter.py` (was "write new").
- Digital control — thin new adapter: write `default.xml` + `systemctl restart` (far simpler than SDRangel's deviceset/channel REST choreography).

**Still needs rewrite (~1,900 LOC, was ~2,300):** `sdrangel.py` (418) → replaced by the thin SDRTrunk-file + chirp-bus adapters (net smaller); `translator.py` (340, apply/verify/unload) → apply engine over file+systemctl+chirp-bus; `reconciler/observer.py` (543) → observe SDRTrunk log + chirp bus + disco state; `reconciler/actions.py` (250) → repairs via service restarts + chirp bus; SDRangel halves of `backends.py` (~150) and the `routes.py` write path (~300).

**Reuses unchanged (~3,000+ LOC):** kill switch, launchd→**systemd** unit patterns (translate plists to units — the ownership/label mechanism reuses), git deploy/update, fail-closed `state.py`, CLI, reconciler **safety+config** + **classifier taxonomy**, profile **validator**, the **wizard/HPDB** path, the HTTP server shell, `sb3.html`.

**Revised estimate: ≈2 SB3-phase-equivalents.** Phase A = control adapters (digital file+service, analog chirp-bus) + apply engine; Phase B = observers (reuse SDRTrunk client + chirp client + disco) + UI routing + the 3 RTL-tool hit tailers. The reconciler retarget folds into A/B rather than a full third phase, because SDRTrunk/chirp/disco are pre-existing and their observers are largely done. **The drop from 3→2 is real reuse: the digital decoder + its observer come for free by choosing SDRTrunk over op25.**

---

## 7. Install plan for Neptune (M1 Mac mini) — locked-stack sequence

Dual-boot Ubuntu Asahi; keep macOS as instant fallback. Partition **≥200 GB**.

1. **Ubuntu Asahi** install (ubuntuasahi.org flow); base packages; confirm USB-A enumerates all SDRs (`lsusb`), fan control (`macsmc-hwmon`, `fan_control=1`).
2. **SDRplay v3 API** — arm64 `.run`, enable `sdrplay_apiService`; `SoapySDRUtil --find` sees RSPduo + RSP1B.
3. **SDRTrunk headless (the load-bearing prove-out)** — install JRE + SDRTrunk; run under `xvfb-run` (or an Xvfb systemd unit); drop in the **Cumberland Public Safety** `default.xml` (CCs 453.650/460.1125/460.2125/460.625, P25.2) on **RSPduo Tuner 1**; confirm CC lock + Crossville PD/Sheriff/EMS/Fire + icecast → `neptune-trunk.mp3`. **Then add Tuner 2** for the second system. If Xvfb is flaky → op25 fallback.
4. **chirp** (RSP1B analog) — `apt gnuradio python3-gi python3-numpy` + pip reqs + SoapySDRPlay3/gr-osmosdr build; start airband daemon; verify Crossville CTAF/AWOS/Guard → icecast; combined mount → `neptune.mp3`.
5. **disco** (HackRF) — `apt soapysdr-module-hackrf hackrf`; swap SoapySDR driver to `hackrf`; run `phase0_smoketest`; validate/retune the classifier on HackRF captures; enable `disco-*` systemd units.
6. **acarsdec** (RTL `61108285`) — build f00b4r0 fork; monitor 131.550/130.025/etc → JSON feed.
7. **dumpvdl2** (RTL `56919602`, validate first) — build libacars + dumpvdl2; 136.975/136.725/etc → JSON feed.
8. **radiosonde_auto_rx** (RTL `95339533` + bias-tee) — Docker or source; auto-scan 400–406 MHz.
9. **Wire feeds** into `/api/*` per §5; port SB3 launchd plists → systemd units.

---

## 8. Risk assessment + coherence answer

- **Not recoverable if the pilot fails? Nothing** — dual-boot preserves macOS + today's rig; reboot restores it.
- **Load-bearing risk: SDRTrunk under Xvfb for 24/7.** Mitigation: prove single-system first; fallback op25 or Trunk Recorder. Everything downstream (playlists, `sdrtrunk_client`, icecast) is unaffected by which digital decoder wins.
- **Second risk: disco→HackRF classifier validity** — plumbing done, model may need re-validation on 8-bit HackRF samples (training pipeline is in-repo).
- **Third: SDRplay-on-Ubuntu-Asahi** — lowest of the three (vendor tests on Ubuntu; Ubuntu Asahi is community but the kernel/USB/SDR-userland is standard Ubuntu).
- **Is the stack coherent, or is there a mismatch to address before committing?** **Coherent — with eyes open on two points.** (a) SDRTrunk is a GUI app; you are committing to running a JavaFX app under Xvfb 24/7. That is genuinely fine on Linux+systemd (and *better* than the macOS Aqua/root situation), but it's the piece to pilot first, and op25 remains the escape hatch. (b) disco moves from the RSPduo it was trained on to the HackRF — validate the classifier. Neither is a mismatch that should stop the commit; both are pilot line-items. Everything else — SDRplay on Ubuntu, chirp, the three RTL tools, the UI aggregation — is well-trodden or already in your repo.

---

## 9. Fallback: Pi 5 hybrid (unchanged, carried)

If Asahi fights the RSPs, relocate the digital+analog RSP stack to a **Pi 5 (8 GB)** running the proven Debian SDRplay + SDRTrunk/op25 stack, keep RSP1B analog on macOS SDRangel, and let Neptune aggregate over the LAN via the icecast pattern this project already runs (two-box harness; philly-exit Pi `rtl_tcp`). Simpler to first-decode; costs one box. Asahi is the elegant consolidation and is zero-risk to try (dual-boot), so pilot it first.

---

## Sources

- SDRTrunk headless — [issue #92 "Command line with no GUI"](https://github.com/DSheirer/sdrtrunk/issues/92) · [headless/performance thread](https://groups.google.com/g/sdrtrunk/c/Meh-Vnd5V18) · [Xvfb for GUI apps](https://github.com/processing/processing/wiki/Running-without-a-Display)
- chirp / disco — in-repo `chirp/README.md`, `chirp/requirements.txt`, `ui/chirp_client.py`, `disco/src/phase0_smoketest.py`, `disco/training/`
- ACARS — [f00b4r0/acarsdec (maintained)](https://github.com/f00b4r0/acarsdec) · [TLeconte/acarsdec (legacy)](https://github.com/TLeconte/acarsdec)
- VDL2 — [szpajder/dumpvdl2](https://github.com/szpajder/dumpvdl2) · [rtl-sdr.com: dumpvdl2](https://www.rtl-sdr.com/dumpvdl2-lightweight-vdl2-decoder/) · [openwebrx/dumpvdl2-debian](https://github.com/openwebrx/dumpvdl2-debian)
- Radiosonde — [projecthorus/radiosonde_auto_rx](https://github.com/projecthorus/radiosonde_auto_rx) · [rtl-sdr.com: auto_rx](https://www.rtl-sdr.com/tracking-radiosondes-with-an-rtl-sdr-and-radiosonde_auto_rx/)
- SDRplay / Asahi / SoapySDRPlay3 — [SDRplay API](https://www.sdrplay.com/api/) · [fventuri SoapySDRPlay3](https://github.com/fventuri/SoapySDRPlay3) · [fventuri gr-osmosdr (RSPduo all modes)](https://github.com/fventuri/gr-osmosdr) · [Ubuntu Asahi](https://ubuntuasahi.org/) · [Fedora Asahi Remix 43](https://www.linuxteck.com/fedora-asahi-remix-43-apple-silicon/) · [Asahi M1 feature support](https://asahilinux.org/docs/platform/feature-support/m1/) · [Asahi fan control](https://datcu-andrei-2.gitbook.io/)
