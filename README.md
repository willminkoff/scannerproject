# SprontPi Scanner Project

This repo contains:
- `ui/airband_ui.py` (SprontPi Radio Control web UI)
- `profiles/*.conf` (rtl_airband profiles)
- `systemd/*.service` (systemd units used on the Pi)
- `icecast/icecast.xml.example` (example Icecast config; update passwords to match your Icecast instance)

## Notes
- Keep Icecast source/admin passwords consistent between Icecast and profile outputs.
- This repo intentionally does NOT include binaries like rtl_airband.
- Profiles include `airband = true/false;` to select the dongle group (airband uses RTL-SDR index 0, ground uses index 1).
- The UI supports multi-select profiles and merges them per dongle into `/usr/local/etc/rtl_airband_airband.conf` and `/usr/local/etc/rtl_airband_ground.conf`.

## Sprint Notes (2026-01)
- UI: profiles render as a two-column grid of selectable cards and show an avoids summary for the active profile.
- Refresh only syncs status + sliders; it does not restart the scanner.
- Speed: profile/gain/squelch apply skips restart when no changes were made.
- Logging: rtl_airband services run with `-F -e` so systemd captures clean stderr logs (no TUI output).
- Icecast: example config now reduces buffering (`queue-size`, `burst-size`) and lowers `source-timeout` for faster recovery after restarts.
- UI: last hit shows "No hits yet" when empty and centers the pill text.

## Pi Notes
- Repo path on SprontPi: `/home/willminkoff/scannerproject`
- User: `willminkoff` (prompt shows `willminkoff@SprontPi`)
- Avoids summary file: `/home/willminkoff/Desktop/scanner_logs/airband_avoids.txt`
- Deploy commands:
  - `cd /home/willminkoff/scannerproject`
  - `git pull origin main`
  - `sudo systemctl daemon-reload`
  - `sudo systemctl restart rtl-airband rtl-airband-ground`
  - `sudo systemctl restart airband-ui`

## Ops Brief
- `assets/Brief from Codex CLI 1-2-26.txt`

## Desktop Buttons
- Scripts: `scripts/desktop/start_scanner.sh`, `scripts/desktop/stop_scanner.sh`
- Launchers: `assets/Start Scanner.desktop`, `assets/Stop Scanner.desktop`
- Install on Pi:
  - `chmod +x /home/willminkoff/scannerproject/scripts/desktop/*.sh`
  - `cp /home/willminkoff/scannerproject/scripts/desktop/*.sh /home/willminkoff/Desktop/`
  - `cp /home/willminkoff/scannerproject/assets/*.desktop /home/willminkoff/Desktop/`
