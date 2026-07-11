# Neptune (m1mini / M1) LaunchAgents

Host-scoped LaunchAgents for the mobile box. Two classes:

## Headless services (audio harness — loaded now, self-healing)
- `com.scannerproject.icecast` — icecast server on :8000 (`neptune.mp3` mount).
- `com.scannerproject.neptune-audio-bridge` — ffmpeg UDP→MP3→icecast bridge.
- `com.scannerproject.copytoudp-watchdog` — arms the SDRangel UDP tap + re-applies the
  2m SKYWARN config on SDRangel restart (runs `macos/bin/sdrangel-restore.py`).

These stream analog audio to the phone and survive crashes/reboots. Loaded into
`gui/$UID` and running.

## Login-triggered UIs (start the GUI apps when Will logs in via CRD)
- `com.scannerproject.sdrangel` — `/Applications/SDRangel.app`.
- `com.scannerproject.sdrtrunk` — `~/sdr-trunk-osx-aarch64-v0.6.1/bin/sdr-trunk`
  (jpackage runtime-image launcher; there is **no** `sdr-trunk.app` bundle in this build).

**Pattern:** `RunAtLoad=true` + `LimitLoadToSessionType=Aqua` → the app starts when the
user logs into a graphical session (so it's visible over Chrome Remote Desktop), and only
in a graphical session (never a headless SSH-triggered load). `KeepAlive={SuccessfulExit:
false}` restarts the app if it **crashes** (non-zero exit) but respects a clean **Quit**
(exit 0) — Will can close a window without launchd immediately relaunching it.

These are intentionally **not** `launchctl load`ed at deploy time: doing so while an app is
already running risks a KeepAlive race with the live instance (e.g. SDRTrunk decoding P25).
They activate on the **next login cycle** (logout/login or reboot). To force-activate
without a login cycle: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist`.

## Deploy base
Agents + logs live under `~/scannerproject/...` on Neptune (the audio harness was deployed
there; the git clone is separately at `~/Documents/scannerproject`). Plist paths are
absolute (`/Users/willminkoff/...`) since launchd does not expand `~`.

## launchd gotcha
`Bootstrap failed: 5: Input/output error` = a half-registration race, not a bad plist.
Clear it: `launchctl bootout gui/$UID <plist-path>`, `launchctl enable gui/$UID/<label>`,
then `bootstrap` again. Validate the app itself by running its binary directly first.
