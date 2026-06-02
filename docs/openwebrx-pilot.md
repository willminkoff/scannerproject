# OpenWebRX+ pilot — Live IQ (waterfall) replacement

Replaces the custom 2-dongle stitched waterfall (`scripts/waterfall.py`, now in
`archive/`) with **OpenWebRX+** (luarvique fork) running in Docker on Micro.
OWRX+ gives drag-to-tune across the whole RTL-SDR range, multi-mode demod
(AM/NFM/WFM/SSB), auto-leveling contrast, and a maintained codebase.

This is **Phase 1**: the *waterfall* is replaced. `scripts/vfo.py` and the
`/VFO.mp3` icecast mount are **untouched** (see "VFO" below) — retiring them is
Phase 2.

## Dongle topology (after pilot)

| Serial     | Before            | After                 |
|------------|-------------------|-----------------------|
| `83241970` | waterfall B (Blog V4) | **OpenWebRX+** (drag-to-tune) |
| `70613472` | waterfall A       | **spare** (waterfall masked) |
| `80000003` | VFO               | VFO (unchanged) — `/VFO.mp3` |
| `61108285` | disco             | disco (unchanged)     |
| `45469635` | disco (down)      | disco (down; replug)  |

RSPduo ×2 (rtl-airband airband + ground), op25, dumpvdl2: **untouched**.

## Why Option A (one RTL-SDR, drag-to-tune)

Will's complaint was visibility/contrast and not being able to *move* the
stitched window (4.8 MHz was "fine, just locked in place"). A single dongle at
2.4 MS/s with click-drag tuning across 24 MHz–1.7 GHz is a better UX than a
wider-but-fixed stitched view, and it frees a dongle. OWRX+ does not stitch
dongles, so the stitched view could not be preserved as-is anyway.

## VFO / `/VFO.mp3` shim

`vfo.py` keeps running on its own dongle (`80000003`), so `/VFO.mp3` continues
to publish to icecast exactly as before — BT (PipeWire) and the `/sb5` embed
player are unaffected. This is the least-invasive shim (Step 3 option ii):
zero new moving parts. Retiring `vfo.py` would require bridging OWRX's
per-client audio WebSocket → ffmpeg → icecast, which is **Phase 2**.

## Install (Micro, Ubuntu 24.04 / amd64)

Ubuntu noble has no native OWRX package (upstream apt repo tops out at mantic),
so we use Docker.

```bash
sudo apt-get install -y docker.io
sudo docker pull slechev/openwebrxplus
sudo mkdir -p /opt/owrx-docker/{var,etc,plugins}
# seed SDR config (single RTL-SDR 83241970 + portable starting profiles):
sudo cp config/owrx/settings.json /opt/owrx-docker/var/settings.json
sudo chown 103:104 /opt/owrx-docker/var/settings.json   # openwebrx uid:gid in image
sudo docker run -d --name owrxp \
  --device /dev/bus/usb --tmpfs=/tmp \
  -p 8073:8073 \
  -v /opt/owrx-docker/var:/var/lib/openwebrx \
  -v /opt/owrx-docker/etc:/etc/openwebrx \
  -v /opt/owrx-docker/plugins:/usr/lib/python3/dist-packages/htdocs/plugins \
  -e TZ=America/New_York \
  -e OPENWEBRX_ADMIN_USER=admin -e OPENWEBRX_ADMIN_PASSWORD=scannerproject \
  --restart unless-stopped slechev/openwebrxplus
```

- Web UI: `http://<box>:8073/`  (iframed into `/sb5` Live IQ pane by hostname).
- Admin login: `admin` / `scannerproject` — **change this** via the OWRX gear →
  user settings, or `OPENWEBRX_ADMIN_PASSWORD`.
- Container sees all USB via `--device /dev/bus/usb` but only opens the serial
  named in `settings.json` (`83241970`) — it does **not** touch the RSPduos or
  other RTL-SDRs.

## `/sb5` wiring

The Live IQ pane (`ui/sb5.html`, `#pane-waterfall`) now hosts an `<iframe>`
whose `src` is built in `boot()` as `http://<location.hostname>:8073/`, so it
works over LAN (`micro.local`), the LAN IP, or Tailscale without hardcoding.
The legacy stitched-waterfall JS (`renderWaterfall`, `pollWaterfall`,
`wireWfCenterInput`, `attachDrag("wf-window")`) is no longer wired in `boot()`.

Heartbeat (`ui/handlers.py`): the two waterfall dongle rows are replaced by one
`_owrx_health_row()` that pings `127.0.0.1:8073` (warn, not bad, if down — it's
an auxiliary view). The VFO row is unchanged.

## Revert (known-good restore)

```bash
# Mac repo: revert the pilot commit, push; on Micro git pull + restart airband-ui.
# On Micro, restore the waterfall service + free OWRX off the dongle:
sudo docker stop owrxp && sudo docker rm owrxp        # release 83241970
sudo systemctl unmask scanner-waterfall.service
sudo mv /etc/systemd/system/scanner-waterfall.service.owrx-pilot-bak \
        /etc/systemd/system/scanner-waterfall.service
sudo systemctl daemon-reload
sudo systemctl enable --now scanner-waterfall.service
```

## Phase 2 follow-ups

- Bridge OWRX audio → `/VFO.mp3` (WebSocket → ffmpeg → icecast), then retire
  `scripts/vfo.py` + `scanner-vfo.service` and free `80000003`.
- HTTPS: if `/sb5` is served over TLS (Tailscale serve), the http iframe is
  mixed-content — put OWRX behind the same TLS reverse proxy.
- Optionally prune the now-dead waterfall JS/handlers (`renderWaterfall`,
  `/api/waterfall`, `_waterfall_*`) from `ui/sb5.html` and `ui/handlers.py`.
- Consider `slechev/openwebrxplus-softmbe` if digital-voice demod (DMR/P25/etc)
  in the Live IQ pane becomes useful for a new location.
- Disco (SDRangel) and op25 (trunk-recorder) migrations — see
  `SDR_TOOLS_RESEARCH.md` (deferred).
```
