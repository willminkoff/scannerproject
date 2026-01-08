# SprontPi Scanner Project

This repo contains:
- `ui/airband_ui.py` (SprontPi Radio Control web UI)
- `profiles/*.conf` (rtl_airband profiles)
- `systemd/*.service` (systemd units used on the Pi)
- `icecast/icecast.xml.example` (example Icecast config; real passwords NOT included)

## Notes
- Replace any `CHANGEME` passwords locally on the Pi.
- This repo intentionally does NOT include binaries like rtl_airband.
- SDRTrunk ground backend mixes `/AIR.mp3` + `/GROUND.mp3` into `/GND.mp3` via `scanner-mixer.service`.
- `sdrtrunk-staging.service` uses a separate home at `/var/lib/sdrtrunk-staging` for testing.

## Sprint Notes (2026-01)
- UI: profiles render as a two-column grid of selectable cards and show an avoids summary for the active profile.
- Refresh only syncs status + sliders; it does not restart the scanner.
- Speed: profile/gain/squelch apply skips restart when no changes were made.
- Logging: `scripts/rtl-airband-with-freq.sh` runs rtl_airband with `-F -e` so systemd captures clean stderr logs (no TUI output).
- Icecast: example config now reduces buffering (`queue-size`, `burst-size`) and lowers `source-timeout` for faster recovery after restarts.
- UI: last hit shows "No hits yet" when empty and centers the pill text.
- UI: hit list uses scan activity logs; durations are inferred with a 10s gap reset and newest hits appear first (not true squelch timing).

## Ticket 16 checklist (keepalive fallback)
- Confirm keepalive source is up: `systemctl status icecast-keepalive`
- Inspect keepalive logs: `journalctl -u icecast-keepalive -n 200 --no-pager`
- Watch Icecast logs during rtl-airband restart: `tail -f /var/log/icecast2/error.log /var/log/icecast2/access.log`
- Check mounts live: `curl -s http://127.0.0.1:8000/status-json.xsl | jq '.icestats.source[] | {mount, listeners, listenurl}'`
- Verify fallback config: `grep -n "fallback-mount" /etc/icecast2/icecast.xml`

Expected observations (when rtl-airband restarts):
- Icecast logs should show `/GND.mp3` disconnect/reconnect events; if no disconnect, fallback won't trigger.
- `status-json.xsl` should briefly omit `/GND.mp3` or show it with no source while `/keepalive.mp3` stays present.
- Browser may still play buffered audio for ~20s; if logs show fallback but audio stays, buffering is the culprit.

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

## Ops Brief
- `assets/Brief from Codex CLI 1-2-26.txt`

## Desktop Buttons
- Scripts: `scripts/desktop/start_scanner.sh`, `scripts/desktop/stop_scanner.sh`
- Launchers: `assets/Start Scanner.desktop`, `assets/Stop Scanner.desktop`
- Install on Pi:
  - `chmod +x /home/willminkoff/scannerproject/scripts/desktop/*.sh`
  - `cp /home/willminkoff/scannerproject/scripts/desktop/*.sh /home/willminkoff/Desktop/`
  - `cp /home/willminkoff/scannerproject/assets/*.desktop /home/willminkoff/Desktop/`
