# SprontPi Scanner Project

This repo contains:
- `ui/airband_ui.py` (SprontPi Radio Control web UI)
- `profiles/*.conf` (rtl_airband profiles)
- `systemd/*.service` (systemd units used on the Pi)
- `icecast/icecast.xml.example` (example Icecast config; real passwords NOT included)

## Notes
- Replace any `CHANGEME` passwords locally on the Pi.
- This repo intentionally does NOT include binaries like rtl_airband.

## Sprint Notes (2026-01)
- UI: profiles render as a two-column grid of selectable cards and show an avoids summary for the active profile.
- Refresh only syncs status + sliders; it does not restart the scanner.
- Speed: profile/gain/squelch apply skips restart when no changes were made.
- Logging: `systemd/rtl-airband.service` now points at `scripts/rtl-airband-with-freq.sh` to strip control codes from logs.
