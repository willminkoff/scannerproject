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
Agents + logs live under `~/scannerproject/...` on Neptune. Plist paths are absolute
(`/Users/willminkoff/...`) since launchd does not expand `~`.

**`~/scannerproject` IS a git clone** — `origin` is the GitHub repo and its upstream is
`origin/main`, so **deploy is `git pull --ff-only`, not rsync.** Verified on the box
2026-11-08; this section previously said the opposite ("the audio harness was deployed
there; the git clone is separately at `~/Documents/scannerproject`") and that cost a
deploy's worth of guessing.

Both `com.scannerproject.chirp-ground` and `com.scannerproject.sb3-ui` have
WorkingDirectory `~/scannerproject`, so **one pull deploys chirp and the UI together.**
sb3-ui reads `ui/sb3.html` from disk per request (no restart needed for HTML), but
imports `sb3/ui/routes.py` at startup — Python changes need `launchctl kickstart -k`.
chirp-ground takes **~44 s** to answer UDP 7401 after a kickstart (SDRplay open +
flowgraph build), so poll rather than assuming it failed. Its channel pool **survives**
a restart via `CHIRP_STATE_PATH`.

Two other trees exist and neither is a deploy target:

- `/opt/scannerproject/app` — **does not exist on this box.** Plist comments that cite
  it (and `/opt/scannerproject/etc/icecast.xml`) are stale; they describe the intended
  TCC-safe layout from the SB7.5 design, not what is installed.
- `~/Documents/scannerproject` — a third clone whose only remote is `micro` (dead), yet
  its HEAD tracks the laptop's commits within seconds and its reflog shows them as
  locally created. iCloud Documents sync is the only explanation. **Never deploy from
  it**, and avoid editing it from two machines at once.

## icecast config lives somewhere else than you expect
The running server is `/opt/homebrew/bin/icecast -c ~/.config/icecast/icecast.xml`
(Homebrew build, user config). Note that `scripts/mac-bootstrap.sh` writes
`$PREFIX/etc/icecast.xml` = `/opt/scannerproject/etc/icecast.xml`, which is **not** the
file in use here — editing the bootstrap template changes future provisioning only, so a
live buffering change must also be made in `~/.config/icecast/icecast.xml`.
`/opt/homebrew/etc/icecast.xml` is a third copy, carries two conflicting `<burst-size>`
tags, and is unused.

Restarting icecast is cheap and safe: measured cost on 2026-11-08 was one drop /
one reconnect per source, with the mount answering again in **1 s**. That is nowhere
near the chirp sink's escalation gate, which needs `fatal_reconnect_failures=10`
**and** `fatal_window_s=600` together.

## no log rotation
Nothing rotates `~/Library/Logs/chirp/`. As of 2026-11-08: `ground.out.log` **1.0 GB**,
`airband.out.log` 85 MB (stale since Sep 10), `mount-watchdog.log` 20 MB,
`hits.jsonl` 30 MB. Disk is fine (68 GB free), but **never run a bare `grep`/`cat` over
`ground.out.log`** — it times out. Use `tail -n`, `sed -n` on a line range, or an
anchored pattern with `--max-count`.

## launchd gotcha
`Bootstrap failed: 5: Input/output error` = a half-registration race, not a bad plist.
Clear it: `launchctl bootout gui/$UID <plist-path>`, `launchctl enable gui/$UID/<label>`,
then `bootstrap` again. Validate the app itself by running its binary directly first.

## SDRplay healer (ground radio self-recovery)
- `com.scannerproject.sdrplay-healer` — runs `macos/bin/sdrplay-healer.py` every 30 s.

**The failure it handles.** Ground's RSP1B stops answering at the USB layer — the
kernel logs `transaction error` / `babble error` on endpoint 0x81, then stalled
pipes (`0xe0005000`). chirp doesn't crash: the flowgraph just stops getting samples,
the `neptune-ground.mp3` mount drops, and the daemon sits alive-but-useless. From
then on the radio stays wedged — restarting chirp or `sdrplay_apiService` gives
`activateStream() Init() failed: sdrplay_api_Fail` every time. Only re-enumerating
the USB device clears it.

**What it does.** Watches `chirp_audio_bytes_published_total` on ground's metrics
endpoint (`:9102`) — it climbs even when every channel is squelched, because chirp
publishes silence. If it stops for 90 s, the healer runs the recovery that was proven
by hand: stop chirp-ground (and ACARS/VDL2 if running) → stop `sdrplay_apiService` →
`usb-reenumerate <serial>` (software unplug/replug) → start the API → start ground →
restore the others. It leaves ground alone if you unloaded it on purpose, waits 150 s
for a restarted daemon to come up, backs off 2→5→10→20→30 min after a recovery that
didn't work, and never does more than 4 recoveries in an hour. Log:
`~/Library/Logs/chirp/sdrplay-healer.log` (JSON lines, transitions only).

**chirp-mount-watchdog no longer handles ground.** Its blind `kickstart -k` every
30 s could never re-enumerate a wedged radio, and that churn is what wedged the
SDRplay API on 2026-10-14/15.

**Root cause is hardware.** Every kernel USB error since 2026-10-12 is on
`AppleEmbeddedUSBXHCIFL1100` — the Fresco Logic controller behind the M1 mini's two
**USB-A** ports. The two **USB-C** ports are on Apple's own `AppleT8103USBXHCI`
controllers and logged zero errors. Put the SDRplay radios on USB-C (directly, or via
a powered hub on a USB-C port). The healer is defence in depth, not a substitute.

Setup on the host (once):
```
~/scannerproject/macos/usb-reenumerate/build.sh
cp ~/scannerproject/etc/mac/launchd/neptune/com.scannerproject.sdrplay-healer.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.scannerproject.sdrplay-healer.plist
```
Needs passwordless `sudo` for `launchctl` (to restart `system/com.sdrplay.service`).
