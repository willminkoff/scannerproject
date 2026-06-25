# Mac mini port — full replacement of `micro`

**Goal (chosen path, "Path A"):** the Mac mini *becomes* the scannerproject box —
headless, always-on, multi-band airband + ground scan, op25 P25 digital, Icecast
streaming, the web UI, WX decoders, **and** local/Bluetooth audio output. Full
feature parity with the Linux `micro` deployment.

This is a multi-week port. The good news from the feasibility assessment: the
RF decoders all build on Apple Silicon, the dongles attach to the Mac host
natively (no USB passthrough — Docker/VM is a confirmed dead end), and Icecast /
ffmpeg / SoapySDR / the SDRplay API are all native. The real work is in two
layers: **service control** (systemd → launchd/supervisor) and **local audio**
(PipeWire/PulseAudio → CoreAudio).

## Architecture decisions

### 1. Filesystem layout — `/opt/scannerproject`
`ui/config.py` reads nearly every path from `os.getenv(...)` with a Linux
default, so the entire FHS coupling collapses into one env file:
`etc/mac/scannerproject.env` (installed to `/opt/scannerproject/etc/`).

| Linux | macOS |
|---|---|
| `/usr/local/etc`, `/etc/scannerproject` | `/opt/scannerproject/etc` |
| `/run/...` | `/opt/scannerproject/run` |
| `/var/log/*` | `/opt/scannerproject/log` |
| `/home/ubuntu/...` | repo paths / `/opt/scannerproject` |
| system python | `/opt/scannerproject/venv` (3.12) |

`/opt` is absolute and login-independent, so a boot-time LaunchDaemon running as
the user can reach it without a stable `$HOME`.

### 2. Service control — a backend abstraction in `ui/systemd.py`
`ui/systemd.py` (~1540 lines) is the chokepoint, but its ~1000 lines of recovery
logic (gentle→escalate, SDRplay daemon coordination, MA/SL Master-before-Slave
ordering, HTTP/stats health probes) sit on **~10 thin primitives** that shell out
to `systemctl`:
`unit_active`, `unit_exists`, `unit_enabled`, `unit_active_enter_epoch`,
`unit_restart_count`, `_run_systemctl`, `_start_unit`, `_stop_unit`,
`_restart_unit`, `_kill_unit`, `_reset_failed_units` — plus one `journalctl -k`
SDRplay-segfault probe in `_sdrplay_daemon_healthy()`.

**Plan:** extract those primitives behind a `ServiceBackend` interface and
dispatch on `SCANNER_SERVICE_BACKEND` (`systemd` | `launchd`). The launchd
backend maps logical unit names (the `config.UNITS{}` dict, already env-driven)
to launchd labels and calls `launchctl kickstart -k / bootout / print`. The
recovery brain is untouched. The `journalctl -k` segfault probe has no macOS
analog → replace with a stats-freshness + `log show --predicate` check (or just
drop the segfault heuristic and rely on the HTTP/stats probes that already
exist).

### 3. Process supervision — one LaunchDaemon booting a supervisor
Do **not** hand-translate ~39 systemd units to 39 plists — service *ordering* is
load-bearing (SDRplay concurrent-open deadlock; Master tuner must open before
Slave). Instead: a single launchd LaunchDaemon boots a dependency-aware
supervisor (`s6-rc` or `runit`, both on Homebrew) that models the ordering
natively. `ExecStartPre` steps become wrapper scripts. The `ui/systemd.py`
launchd backend talks to the supervisor for start/stop/restart.

### 4. Local audio — CoreAudio (Path A only)
PipeWire/PulseAudio/`wpctl`/`pw-cat`/`bluez` do not exist on macOS. Surface:
`ui/audio_leveler.py`, `ui/band_mute.py`, `ui/vlc.py`, `scripts/op25-audio-bridge.py`,
`scripts/bt-*.sh`. Approach: pair the BT speaker via macOS System Settings (let
CoreAudio own routing); replace VLC `--aout=pulse` with `--aout=auhal`; implement
volume/mute via a small CoreAudio shim (or `osascript`/`SwitchAudioSource`).
`op25-audio-bridge.py` keeps its UDP→ffmpeg→Icecast core; the `pw-cat` local-play
branch becomes a CoreAudio output.

### 5. Python — pin 3.12
System python is **3.14**, which removed `audioop` (used by
`scripts/op25-audio-bridge.py`). All scannerproject processes run under the
`/opt/scannerproject/venv` (3.12). op25 itself runs under Homebrew gnuradio's
python.

## Known blockers / drops
- **Docker / Linux-VM**: dead end (no USB passthrough on Apple Silicon; SDRplay
  daemon on the wrong side of the VM boundary). Run native on the host.
- **`sb3-ap-fallback`** (hostapd WiFi AP): no macOS equivalent → drop, or use
  macOS Internet Sharing manually.
- **`journalctl -k`** segfault probe → replace (see §2).
- **systemd `Type=notify` + `WatchdogSec`** → external health-poller (the HTTP
  and stats-freshness probes in `ui/systemd.py` already do most of this).

## Milestone roadmap

- **M0 — Foundation** *(this commit)*: `etc/mac/scannerproject.env`,
  `scripts/mac-bootstrap.sh`, this doc.
- **M1 — Core analog + streaming** *(needs HW)*: bootstrap → build rtl_airband →
  Icecast up → generate combined config → airband + ground stream to
  `/ANALOG.mp3` / `/ANALOG_GROUND.mp3`. Proof-of-life by hand first.
- **M2 — Service-control backend**: `ServiceBackend` abstraction in
  `ui/systemd.py`; launchd backend; replace the `journalctl -k` probe. Unit
  tests run with no hardware.
- **M3 — Supervisor + launchd**: s6-rc/runit topology encoding SDRplay open
  ordering; one bootstrapping LaunchDaemon; wire the M2 backend to it.
- **M4 — Web UI + waterfall**: bring up `airband-ui` on :5050; make the
  `/sys/bus/usb` walks no-ops on macOS; replace `journalctl` hit-parsing in
  `ui/scanner.py` with chirp-hits-JSONL tailing; waterfall WebSocket feed.
- **M5 — op25 P25 + local/Bluetooth audio**: op25 headless (http-terminal),
  `op25-audio-bridge.py` on py3.12; CoreAudio for `audio_leveler`/`band_mute`/
  `vlc`; BT speaker via System Settings. Validate RSPduo dual-tuner contention.
- **M6 — WX + health/monitoring/hardening**: acarsdec/dumpvdl2/radiosonde;
  Prometheus + Grafana (Homebrew) + the stdlib `icecast_exporter.py`; port the
  SDRplay wedge-recovery (`mac-start-sdrtrunk.sh --reset` pattern) into supervisor
  health policy; runbook + drop-list.

## Quickstart (M0 → M1)
```bash
# provision (core; add --op25 --wx for the heavy source builds)
scripts/mac-bootstrap.sh
# load runtime config into the shell
set -a; source /opt/scannerproject/etc/scannerproject.env; set +a
# [needs HW] confirm the SDR is visible
SoapySDRUtil --find
```
