Release Notes
=============

2026-02-04
----------
- SB3 dashboard: added Apply button to batch gain/squelch/filter changes and avoid multiple restarts.
- SB3 dashboard: embedded player now reloads the stream after apply/restart/profile changes to reduce buffering delay.
- Squelch behavior: UI clamps 0 to -1 dBFS for “closed” squelch; -120 remains fully open.
- Config refactor: UI, scripts, and systemd units use consistent config paths via `/etc/airband-ui.conf`.
- Status telemetry: UI shows pending/applied values and config/restart health for better trustworthiness.

2026-02-06
----------
- AP fallback: boot-time reachability check; starts `SB3-CTRL` access point at `192.168.4.1` when LAN is unreachable and keeps it up until reboot.
- AP fallback: stop any existing `hostapd`/`dnsmasq` services before launching; systemd unit uses `/usr/local/sbin/sb3-ap-fallback` and `network-online.target`.

2026-02-10 (v2.0.0)
------------------
- Digital subsystem: SDRTrunk adapter, systemd unit, and live-only API endpoints (start/stop/restart, profiles, mute, inspect).
- Digital profiles: filesystem-based profile directories with active symlink model and SB3 management UI.
- SB3: Digital tab, Digital profiles widget, last-hit pill, and mobile header stacking improvements.
