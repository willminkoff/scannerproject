# SDR killswitch

Hand the SDR hardware between the **scanner** (the scannerproject daemons + the
sb5 UI) and two **desktop apps** — **SDRangel** and **desktop SDRTrunk** — and
back again, cleanly. Exactly one owner of the radios at a time.

## Why this exists

The scannerproject daemons grab the RSP/RTL **exclusively** through the
tuner-broker: chirp airband/ground and the managed SDRTrunk each run under
`broker.client run … -- <proc>`, and the broker lease *is* the open socket — so
while the scanner is up, SDRangel or a desktop SDRTrunk simply can't open the
devices. The killswitch stops the scanner cleanly, waits for the SDRplay
apiService to let go, and confirms nothing is still holding a radio before you
hand it to a desktop app — then brings the scanner back on demand.

## The four states

| Command | What it does |
|---|---|
| `scanner`  | Quit desktop apps, then bring the scannerproject stack up in dependency order (icecast → broker → chirp air/ground → managed SDRTrunk → sb5 UI → caffeinate). |
| `sdrangel` | Release the scanner, then launch SDRangel for you to drive. |
| `sdrtrunk` | Release the scanner, then launch **desktop** SDRTrunk for you to drive. |
| `release`  | Take the scanner fully down and free the radios — neutral hand-off; any desktop app can now claim them. |
| `status`   | Current mode, which launchd agents are loaded, live broker leases, launch-target health, and any lingering radio holders. |

`scanner` mode includes the **managed** SDRTrunk (your digital P25 engine, on
the RTL-SDR Blog V4 per fleet policy v3.0). `sdrtrunk` mode is the *same* app
run **standalone** with the rest of the scanner torn down — so you never end up
with two SDRTrunk instances fighting for the tuner.

## Usage

```bash
macos/killswitch/sdr-killswitch status      # what owns the radios right now
macos/killswitch/sdr-killswitch release     # free everything (neutral)
macos/killswitch/sdr-killswitch sdrangel    # hand off to SDRangel
macos/killswitch/sdr-killswitch sdrtrunk    # hand off to desktop SDRTrunk
macos/killswitch/sdr-killswitch scanner     # back to the scanner + sb5
```

Add `--reset-sdrplay` to any mode to also bounce the vendor SDRplay apiService
(needs sudo) — use it only when an RSP shows up **MISSING/wedged** after a
switch. RTL-only handoffs never need it.

Put it on your PATH for convenience:

```bash
ln -s "$PWD/macos/killswitch/sdr-killswitch" /usr/local/bin/sdr-killswitch
sdr-killswitch status
```

Because it's just launchctl + `open`, it works the same over SSH / the
conversational-control path from your phone.

## How stopping/starting actually works

- Every scanner agent is a launchd **user-agent with `KeepAlive=true`**, so a
  plain `kill` would respawn it. The killswitch stops with
  `launchctl bootout gui/$UID/<label>`, which SIGTERMs the broker wrapper →
  the wrapped SDR process exits → the lease (and the device handle) releases.
- After stopping a SoapySDR holder it waits **`DRAIN_SECONDS` (default 6s)** so
  the shared `sdrplay_apiService` fully releases the RSP before anything else
  opens it. This is the same settle rule `scripts/disco-svc-ctl` uses.
- The vendor `com.sdrplay.service` apiService is **left running** — SDRangel and
  desktop SDRTrunk both use it. Only `--reset-sdrplay` touches it.
- `release` verifies success by confirming **no** `chirp.daemon` /
  `broker.client` / `sdr-trunk` / `vfo.py` process is still alive. If any is,
  it says so and does not pretend the radios are free.

Nothing on macOS fights the killswitch: the `reliability_watchdog` is
Linux/systemd-only and no longer restarts bands.

## Configuration

Launch targets are **auto-detected at runtime**; override at the top of
`sdr-killswitch` (or via env) only if detection misses:

| Var | Default / detection |
|---|---|
| `SDRANGEL_APP` | auto: `/Applications/SDRangel.app`, then `~/Applications/SDRangel.app` |
| `SDRTRUNK_BIN` | auto: `~/SDRTrunk/bin/sdr-trunk`, then `/Applications/SDRTrunk/bin/sdr-trunk` |
| `LAUNCH_AGENTS_DIR` | `~/Library/LaunchAgents` |
| `DRAIN_SECONDS` | `6` |
| `SCANNER_BROKER_SOCKET` | `/opt/scannerproject/run/broker.sock` |
| `BROKER_PY` / `SCANNERPROJECT_DIR` | `/opt/scannerproject/venv/bin/python` / `/opt/scannerproject/app` (for lease queries in `status`) |

If a launch target can't be found, `status` **warns** (it doesn't fail), and the
matching mode command errors with a clear message telling you which var to set.

## Menubar toggle (optional — SwiftBar / xbar)

`sdr-mode.5s.sh` is a menubar plugin that shows the current owner (📡 Scanner /
🎛 SDRangel / 📻 Trunk / ⚪️ Idle) and switches modes from a dropdown. It's a thin
wrapper — the CLI stays the source of truth — so it is **not** a dependency.

To use it:

```bash
brew install --cask swiftbar        # launch SwiftBar, choose a plugin folder
cp macos/killswitch/sdr-mode.5s.sh <your-swiftbar-plugin-folder>/
chmod +x <your-swiftbar-plugin-folder>/sdr-mode.5s.sh
# set KILLSWITCH at the top of the copied plugin if the auto-path is wrong
```

(xbar uses the same plugin format and the same filename convention.)

## Files

| File | What |
|---|---|
| `sdr-killswitch` | The CLI — all logic lives here. |
| `sdr-mode.5s.sh` | Optional SwiftBar/xbar menubar plugin (wraps the CLI). |
| `sdrplay-reset` | Optional single-action root helper: bounces `com.sdrplay.service`. |
| `sdrplay-reset.sudoers` | Optional NOPASSWD snippet for `sdrplay-reset` (edit the username). |

## Safety notes

- Switching always **releases before it launches**, so you never double-open a
  device.
- `--reset-sdrplay` is deliberately a separate, hardcoded-action helper so it
  can get NOPASSWD sudo without opening `launchctl` wholesale.
- Related open item (tracked separately): scanner mode brings chirp air+ground
  back on the one RSPduo's two tuners — the dual-open path flagged UNVALIDATED
  in `etc/mac/sdr_fleet_policy.json`. The killswitch is safe to use; that
  hazard is about the scanner config itself, not the switching.
