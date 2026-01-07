# SprontPi Scanner Project

This repo contains:
- `ui/airband_ui.py` (SprontPi Radio Control web UI)
- `profiles/*.conf` (rtl_airband profiles)
- `systemd/*.service` (systemd units used on the Pi)
- `icecast/icecast.xml.example` (example Icecast config; update passwords to match your Icecast instance)

## Notes
- Keep Icecast source/admin passwords consistent between Icecast and profile outputs.
- This repo intentionally does NOT include binaries like rtl_airband.
- SprontPi uses a single Icecast mount (`/GND.mp3`) fed by a mixer that combines Airband + Ground in one rtl_airband process.
- `systemd/rtl-airband-last-hit.service` uses `ExecStart=/bin/bash ...` so it works even if the script loses its executable bit; keep the script executable for manual runs.

## Sprint Notes (2026-01)
- UI: profiles render as a two-column grid of selectable cards and show an avoids summary for the active profile.
- Speed: profile/gain/squelch apply skips restart when no changes were made.
- Logging: `systemd/rtl-airband.service` now points at `scripts/rtl-airband-with-freq.sh` to strip control codes from logs.
- Icecast: example config now reduces buffering (`queue-size`, `burst-size`) and lowers `source-timeout` for faster recovery after restarts.
- UI: last hit shows "No hits yet" when empty and centers the pill text.

## Combined Mixer Setup (Single Icecast Mount)
SprontPi runs both dongles inside one rtl_airband process and mixes them into a single Icecast mount:
- Airband/Tower profiles run on device index 0.
- GMRS/WX profiles run on device index 1.
- Both channel outputs are routed into a mixer named `combined`.
- The mixer outputs a single Icecast stream at `http://127.0.0.1:8000/GND.mp3`.

How it works:
- `scripts/build-combined-config.py` generates `/usr/local/etc/rtl_airband_combined.conf`.
- The generator pulls the active Airband profile from `/usr/local/etc/rtl_airband.conf` and the active Ground profile from `/usr/local/etc/rtl_airband_ground.conf`.
- It replaces per-channel `outputs` with a mixer output and defines a single Icecast output at the mixer.
- The generator hard-enforces device indexes (0 for Airband, 1 for Ground) so dongles do not collide.
- The UI regenerates the combined config when profiles or gain/squelch values change.

Systemd service expectations:
- `rtl-airband.service` should run the combined config:
  - `ExecStartPre=/usr/bin/python3 /home/willminkoff/scannerproject/scripts/build-combined-config.py`
  - `ExecStart=/home/willminkoff/scannerproject/scripts/rtl-airband-with-freq.sh /usr/local/etc/rtl_airband_combined.conf`
- The old `rtl-airband-ground.service` should be disabled to avoid mount conflicts.

Quick verification:
- Confirm combined config exists:
  - `sudo /usr/bin/python3 /home/willminkoff/scannerproject/scripts/build-combined-config.py`
  - `sudo head -n 120 /usr/local/etc/rtl_airband_combined.conf`
- Confirm Icecast stream is live:
  - `curl -s http://127.0.0.1:8000/status-json.xsl | grep -E '"mount"|listenurl|listeners'`

## Pi Notes
- Repo path on SprontPi: `/home/willminkoff/scannerproject`
- User: `willminkoff` (prompt shows `willminkoff@SprontPi`)
- Avoids summary files: `/home/willminkoff/Desktop/scanner_logs/airband_avoids.txt`, `/home/willminkoff/Desktop/scanner_logs/ground_avoids.txt`
- Deploy commands:
  - `cd /home/willminkoff/scannerproject`
  - `git pull origin main`
  - `sudo systemctl daemon-reload`
  - `sudo systemctl restart rtl-airband`
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
