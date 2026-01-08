# SprontPi Scanner Project

This repo contains:
- `ui/airband_ui.py` (SprontPi Radio Control web UI)
- `profiles/*.conf` (rtl_airband profiles)
- `systemd/*.service` (systemd units used on the Pi)
- `icecast/icecast.xml.example` (example Icecast config; update passwords to match your Icecast instance)

## Notes
- Keep Icecast source/admin passwords consistent between Icecast and profile outputs.
- This repo intentionally does NOT include binaries like rtl_airband.
- SprontPi keeps `/GND.mp3` as the listener-facing mount. Ground can be fed by rtl_airband (legacy) or SDRTrunk (new).
- `systemd/rtl-airband-last-hit.service` uses `ExecStart=/bin/bash ...` so it works even if the script loses its executable bit; keep the script executable for manual runs.

## Sprint Notes (2026-01)
- UI: profiles render as a two-column grid of selectable cards and show an avoids summary for the active profile.
- Speed: profile/gain/squelch apply skips restart when no changes were made.
- Logging: `systemd/rtl-airband.service` now points at `scripts/rtl-airband-with-freq.sh` to strip control codes from logs.
- Icecast: example config now reduces buffering (`queue-size`, `burst-size`) and lowers `source-timeout` for faster recovery after restarts.
- UI: last hit shows "No hits yet" when empty and centers the pill text.

## Audio Mixing (Single Listener Mount)
SprontPi keeps `/GND.mp3` as the single listener-facing mount. There are two modes:

Legacy (rtl_airband combined):
- Airband/Tower profiles run on device index 0.
- GMRS/WX profiles run on device index 1.
- Both channel outputs are routed into a mixer named `combined`.
- The mixer outputs `http://127.0.0.1:8000/GND.mp3`.

SDRTrunk ground backend:
- rtl_airband outputs Airband to `/AIR.mp3` (set `AIR_MOUNT` in `/etc/default/rtl-airband`).
- SDRTrunk outputs Ground to `/GROUND.mp3` (configure in SDRTrunk playlist).
- `scanner-mixer.service` mixes `/AIR.mp3` + `/GROUND.mp3` into `/GND.mp3`.

Legacy combined config details:
- `scripts/build-combined-config.py` generates `/usr/local/etc/rtl_airband_combined.conf`.
- The generator pulls the active Airband profile from `/usr/local/etc/rtl_airband.conf` and the Ground profile from `/usr/local/etc/rtl_airband_ground.conf`.
- It replaces per-channel `outputs` with a mixer output and defines a single Icecast output at the mixer.
- The UI regenerates the combined config when profiles or gain/squelch values change.

Systemd expectations (SDRTrunk mode):
- `rtl-airband.service` runs with `AIR_MOUNT=AIR.mp3` in `/etc/default/rtl-airband`.
- `sdrtrunk.service` runs headless and streams to `/GROUND.mp3`.
- `scanner-mixer.service` mixes to `/GND.mp3`.
- Set `GROUND_BACKEND=sdrtrunk` in `/etc/default/airband-ui` so the UI uses SDRTrunk profiles.

Quick verification:
- Confirm combined config exists:
  - `sudo /usr/bin/python3 /home/willminkoff/scannerproject/scripts/build-combined-config.py`
  - `sudo head -n 120 /usr/local/etc/rtl_airband_combined.conf`
- Confirm Icecast mounts are live:
  - `curl -s http://127.0.0.1:8000/status-json.xsl | grep -E '"mount"|listenurl|listeners'`

## SDRTrunk Profiles
- Ground profiles live in `profiles/sdrtrunk/*`.
- Each profile has `profile.json` metadata and an optional `sdrtrunk/` directory with a playlist/config to sync.
- The sync helper is `scripts/sdrtrunk_profile_sync.py`; it copies the profile into `/var/lib/sdrtrunk/SDRTrunk`.
- Vanderbilt P25 metadata lives in `profiles/sdrtrunk/vanderbilt_p25/profile.json` (talkgroups in `trunk-recorder/p25_talkgroups.json`).
- Env file templates: `systemd/rtl-airband.env.example`, `systemd/airband-ui.env.example`, `systemd/sdrtrunk.env.example`, `systemd/scanner-mixer.env.example`.
- `sdrtrunk-staging.service` runs with `SDRTRUNK_HOME=/var/lib/sdrtrunk-staging` for isolated testing.

## Pi Notes
- Repo path on SprontPi: `/home/willminkoff/scannerproject`
- User: `willminkoff` (prompt shows `willminkoff@SprontPi`)
- Avoids summary files: `/home/willminkoff/Desktop/scanner_logs/airband_avoids.txt`, `/home/willminkoff/Desktop/scanner_logs/ground_avoids.txt`
- Deploy commands:
  - `cd /home/willminkoff/scannerproject`
  - `git pull origin main`
  - `sudo systemctl daemon-reload`
  - `sudo systemctl restart rtl-airband`
  - `sudo systemctl restart sdrtrunk scanner-mixer` (SDRTrunk mode)
  - `sudo systemctl restart airband-ui`
  - `sudo systemctl disable --now rtl-airband-ground` (one-time, if present; stays disabled after)
- Unit update (last-hit):
  - `sudo cp /home/willminkoff/scannerproject/systemd/rtl-airband-last-hit.service /etc/systemd/system/`
  - `sudo systemctl daemon-reload`
  - `sudo systemctl restart rtl-airband-last-hit`

## Ops Brief
- `assets/Brief from Codex CLI 1-2-26.txt`

## Desktop Buttons
- Scripts: `scripts/desktop/start_scanner.sh`, `scripts/desktop/stop_scanner.sh`
- Launchers: `assets/Start Scanner.desktop`, `assets/Stop Scanner.desktop`
- Install on Pi:
  - `chmod +x /home/willminkoff/scannerproject/scripts/desktop/*.sh`
  - `cp /home/willminkoff/scannerproject/scripts/desktop/*.sh /home/willminkoff/Desktop/`
  - `cp /home/willminkoff/scannerproject/assets/*.desktop /home/willminkoff/Desktop/`
