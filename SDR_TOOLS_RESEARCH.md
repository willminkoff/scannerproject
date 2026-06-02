# Open-Source SDR Tools — Adoption Research

**Target:** replace ~3000 LOC of custom Python in `scannerproject` with maintained upstream tools.
**Box:** `ubuntu@micro.local` (ARM Linux SBC), 2× SDRplay RSPduo + 6× RTL-SDR R820T2, icecast on :8000, UI at `ui/sb5.html`.
**Date of research:** June 2026.

---

## #1 — Waterfall replacement (`scripts/waterfall.py`, ~671 LOC)

**Top pick: OpenWebRX+ (luarvique fork).**
[github.com/luarvique/openwebrx](https://github.com/luarvique/openwebrx) — active, 4,389 commits on master; last tagged release `1.2.102` on 2024-06-05 with continuous unreleased commits since. **AGPL-3.0** (viral if redistributed; fine for self-host).

Runner-up: [github.com/jketterl/openwebrx](https://github.com/jketterl/openwebrx) (upstream, also active, fewer built-in decoders).

**Hardware fit.** RTL-SDR R820T2 native. RSPduo supported via `SoapySDRPlay3` including the dual-tuner `rspduo-mode` parameter (1/2/4/8 = single / dual / master / slave). OpenWebRX+ readme calls out "more reliable SDRPlay operation." Pre-built Raspberry Pi disk images exist, so ARM is first-class. Each SDR is **owned exclusively** by OpenWebRX, but OWRX itself multiplexes one device to many web clients — i.e. you don't need a second consumer per dongle; multiple browsers share. Two RTL-SDRs running in OWRX show up as two profiles, but OWRX does **not** stitch them into one wide window — you switch profiles. If wider continuous bandwidth is the goal, the RSPduo's 10 MHz single-tuner mode is the cleaner answer (one dongle, no stitching, drag-to-tune in the canvas works natively).

**Integration.** Runs headless on port **8073**. Drag-to-tune, bookmarks, scanner module, and recorder all built in. Iframe `/sb5` to `http://micro.local:8073/` — works but it's a heavy canvas; expect the iframe to dominate the layout. The state channel is a WebSocket of JSON messages, not a clean REST endpoint, so if you want hits scraped into `airband-ui.service` you'd write a small WebSocket subscriber. Hand-off from `scanner-waterfall.service`: `systemctl stop` releases the dongles, OWRX picks them up on its next profile load — clean.

**Risk / blocker.** AGPL. SDRplay API has to be installed separately on ARM (small chore). The bundled stack pulls in csdr/digiham/direwolf/wsjt-x — disk and RAM aren't free on a micro SBC.

**LOC delta.** Delete ~671 LOC of waterfall + drop most of `ui/sb5.html`'s waterfall canvas. Add ~50–150 LOC of WebSocket subscriber + iframe wiring. **Net: −500 to −600 LOC.**

---

## #2 — Disco sweep + classifier (`disco_coordinator.py` ~494 LOC + `disco/src/sweep.py`)

**Top pick: SDRangel (`sdrangelsrv` headless).**
[github.com/f4exb/sdrangel](https://github.com/f4exb/sdrangel) — very active, 10,925 commits, 322 releases, latest **v7.24.0 on 2026-03-28**. **GPL-3.0**.

**Why over the alternatives.** `rtl_power_fftw` ([github.com/AD-Vega/rtl-power-fftw](https://github.com/AD-Vega/rtl-power-fftw), GPL-3.0) is rock-solid for the *sweep* part but RTL-SDR only and gives you spectrum without classification. `qspectrumanalyzer` ([github.com/xmikos/qspectrumanalyzer](https://github.com/xmikos/qspectrumanalyzer)) — last release 2017, effectively abandoned. `gqrx-scanner` ([github.com/neural75/gqrx-scanner](https://github.com/neural75/gqrx-scanner)) drives gqrx over rigctl but gqrx isn't truly headless. `Artemis` ([github.com/AresValley/Artemis](https://github.com/AresValley/Artemis), v4.1.0 2024-10-20) is a *signal-identification database*, not a receiver — useful as embedded reference data, not as a sweep engine.

**Hardware fit.** SDRangel handles RTL-SDR and the RSPduo (dual-tuner plugin included) in the same instance as multiple "device sets." That's exactly your topology. Each device is exclusive to the SDRangel process, but you can drive sweep + classify across multiple device sets from one daemon.

**Integration.** This is the killer feature: SDRangel exposes a **full Swagger/OpenAPI REST API** (default port 8091) for set/get of frequency, gain, demod mode, channel analyzer, etc. — and a *Reverse API* that pushes state to your endpoint. You replace `disco_coordinator.py` with a thin Python client that calls REST, and let `airband-ui.service` poll the same API for the disco-state JSON the UI already wants. Companion `sdrangelcli` web UI (separate repo) optional.

**Risk / blocker.** Heavy C++/Qt build; use the maintainer's deb repo on ARM rather than building from source. SDRplay API install is required. Big surface area = learning curve.

**LOC delta.** Delete ~494 + ~the sweep module. Add ~150–250 LOC REST glue. **Net: −400 to −600 LOC** and your classifier becomes SDRangel's channel-analyzer plugins rather than handrolled.

---

## #3 — VFO (`scripts/vfo.py`, ~962 LOC)

**Top pick: fold into OpenWebRX+.**

OpenWebRX+ already does AM/NFM/WFM/USB/LSB demodulation, drag-to-tune, and per-client audio streams. It does **not** publish to icecast natively, but neither do the other candidates. [gqrx](https://github.com/gqrx-sdr/gqrx) (v2.17.7, 2025-05-27, GPL-3.0) has no true headless mode — it needs Qt/X. [SDR++](https://github.com/AlexandreRouma/SDRPlusPlus) (GPL-3.0) has a `-s` server flag but the server protocol is proprietary binary for SDR++ clients only, not browsers. The maintained ARM fork [sannysanoff/SDRPlusPlusBrown](https://github.com/sannysanoff/SDRPlusPlusBrown) (release 2026-05-07) is the better ARM build of SDR++ but still no browser-friendly endpoint.

**Recommendation.** Drop `vfo.py`'s standalone service. Tune via OpenWebRX+ when a human is at the UI. If `/VFO.mp3` on icecast must persist for headless playout, keep a *tiny* (~100 LOC) shim that captures OWRX's per-client audio WebSocket → ffmpeg → icecast source. **Net: −800+ LOC.**

**Risk.** OWRX's audio is per-client codec'd (Opus/MP3) — extracting a stable raw stream for icecast republishing takes a small amount of probing.

---

## #4 — op25 trunked → trunk-recorder?

[github.com/TrunkRecorder/trunk-recorder](https://github.com/TrunkRecorder/trunk-recorder) — very active, 2,493 commits, **v5.2.1 on 2026-04-08**, **GPL-3.0**. RTL-SDR R820T2 is the primary supported device. RSPduo is *listed* as supported but community reports (incl. issue #422) flag gain-handling and control-channel-tracking flakiness on SDRplay; the RadioReference consensus is "use RTL-SDR with trunk-recorder, use SDRtrunk if you must use SDRplay." Headless daemon, no built-in web UI — pair with **Rdio Scanner** (iframeable). Emits per-call JSON metadata files, MQTT plugin for live status, Prometheus exporter plugin.

**Recommendation: pilot, but on an RTL-SDR pair — not your RSPduo.** Reassign the P25 trunked workload off the RSPduo currently running op25 and onto two RTL-SDRs (your spares), keep the RSPduo for airband/ground. Migration is *not* drop-in: trunk-recorder configs are JSON site/system definitions, GNU Radio 3.10 dependency is heavy; on ARM, use the official Docker image rather than building from source. Does **not** decode DMR trunked — confirm your P25 site is P25 (or SmartNet) before committing.

**LOC delta.** Modest — op25 itself isn't yours. You'd delete the multi_rx wrapper scripts (~100–200 LOC) and add Rdio Scanner iframe + JSON-metadata watcher (~100 LOC). **Net: roughly neutral**, but you trade an aging op25 codebase for an actively maintained one.

---

## #5 — Tuner broker (`tuner_broker.py`, ~535 LOC) — KEEP CUSTOM

No upstream project arbitrates dongle ownership across heterogeneous consumers (Disco / ACARS / Sounding). It's policy code specific to your topology. Slim it where possible but don't migrate.

---

## #6 — Bluetooth speaker (UE BOOM 2) auto-reconnect

**First try (zero deps):** WirePlumber 0.5+ `bluez5.auto-connect` config drop-in.
[pipewire.pages.freedesktop.org/wireplumber/daemon/configuration/bluetooth.html](https://pipewire.pages.freedesktop.org/wireplumber/daemon/configuration/bluetooth.html). Set `bluez5.auto-connect = [ a2dp_sink ]` in `/etc/wireplumber/wireplumber.conf.d/51-bluez-autoconnect.conf`. Reconnects automatically *once BlueZ sees the device*.

**If BlueZ doesn't re-page the speaker on its own** (likely your actual failure mode): layer **[omerardic/bluetooth-autoconnect-fixer](https://github.com/omerardic/bluetooth-autoconnect-fixer)** on top — MIT, last commit **2026-02-04**, listens on DBus resume signals and re-runs `bluetoothctl connect`. Audio-stack agnostic (works with PipeWire). Trivial interactive installer.

Avoid: **[jrouleau/bluetooth-autoconnect](https://github.com/jrouleau/bluetooth-autoconnect)** — archived 2025-12-21, no PipeWire support. Avoid `qspectrumanalyzer`-style stale projects.

**LOC delta.** Net add ~30 LOC of config + one systemd unit; replaces ad-hoc recovery scripts.

---

## Prioritized pilot

1. **WirePlumber `bluez5.auto-connect` config** (one hour, biggest UX win per unit work — the BOOM 2 just *works* again).
2. **OpenWebRX+ for waterfall + VFO together** (one weekend). Single migration nets ~−1500 LOC across `waterfall.py` and `vfo.py`, gives you drag-to-tune and a maintained spectrum UI. Iframe it into `/sb5`. This is the highest leverage swap.

Defer SDRangel and trunk-recorder until after #2 lands — both are bigger lifts and benefit from having the OWRX iframe pattern already proven on `/sb5`.
