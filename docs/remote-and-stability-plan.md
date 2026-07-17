# Remote control + listening + digital — stability plan

**Status: PROPOSED (2026-07-03).** Answers "how do I get the SDRangel desk experience,
phone tune/listen, and digital P25 — all reliably?" on the macOS Mac mini backend.
Builds on `docs/macos-backend-migration-scope.md` (decision) and the
`mac-sdr-arbiter-and-8mhz` branch (sdrctl arbiter, SDRTrunk shutdown-hook fork,
scannerctl mobile dashboard).

## Snapshot at time of writing (2026-07-03 18:53)

- **Nothing is on the air.** SDRangel quit at 18:42 today (malloc double-free logged
  after quit — SDRangel being SDRangel); SDRTrunk last wrote a log 2026-07-01; scannerctl
  not running. Nothing restarted any of them.
- **No supervision installed.** `etc/mac/com.scannerproject.{sdrangel,sdrtrunk}.plist`
  exist (untracked) but are NOT bootstrapped into launchd; `launchctl list` shows only
  Tailscale + the SDRplay apiService LaunchDaemon.
- **No remote audio exists on the Mac.** No icecast, `~/SDRTrunk/streaming/` empty, no
  broadcaster in the playlist, scannerctl has no audio player. Phone listening is the
  single biggest missing piece.
- Hardware healthy: both RSPduos enumerated (0x1df7:0x3020 ×2), 2× NESDR SMArt v5 +
  RTL-SDR Blog V4, apiService up.
- Working tree dirty: `macos/bin/sdrctl` modified + 8 untracked files (plists,
  sdrangel-start/restore, sudoers installer, `analog_scanlists.json`, MTEARS CSV).

## The doctrine (what SB3→SB6 taught us)

Every major outage in the journey traces to one of three causes: **SDRplay lifecycle
abuse** (semaphore wedges, dual-tuner segfaults, rapid cycling), **one process doing too
many jobs** (SB3 monolith; chirp daemon owning demod+bus+state), or **no supervision**
(a crash at 18:42 and the box stays silent). Hence three rules:

1. **One owner per radio, forever.** RSP-A `180903EF32` → SDRTrunk (P25) — this
   is Neptune's device. RSP-B `1809063632` → SDRangel (70cm/ground) — that one
   is **Venus's**, a different host; the two RSPduos do not share a box. RTL
   dongles → SDRangel airband / spares.
   `sdrctl` is the only thing that starts/stops RSP consumers (30 s throttle, apiService
   kickstart). Analog-on-RTL + digital-on-RSP coexist; two RSP consumers never run at once.
2. **Remote access taps audio; it never becomes a second SDR consumer.** No
   OpenWebRX/SpyServer/rtl_tcp on the RSPs, ever — that's how the contention wars start.
   The phone consumes *streams* and drives a *thin control plane*.
3. **Supervise everything; assume SDRangel crashes.** SDRangel is the cockpit you love
   and the least reliable process in the fleet. launchd KeepAlive + `sdrangel-restore.py`
   makes its crashes a 30-second blip instead of a silent night.

## Target architecture

```
RSP-A ──► SDRTrunk (P25 MTRTRS/TACN) ──► CoreAudio (BOOM)
  │             └─ built-in Icecast broadcaster ──► icecast /P25.mp3 ──► phone
RSP-B ──► SDRangel 70cm/ground ─┐
RTL   ──► SDRangel airband AM ──┤─ GUI = desk experience (native / Screen Sharing)
                                └─ per-demod UDP audio copy ──► audio-bridge
                                        (UDP PCM → ffmpeg → icecast /AIRBAND.mp3,
                                         /GROUND.mp3) ──► phone
scannerctl (:5050) — fleet switch, scan/squelch, P25 call feed, + audio player strip
sdrctl — single-consumer arbiter (unchanged)
launchd — KeepAlive: sdrtrunk, scannerctl, icecast, audio-bridge; sdrangel via
          sdrangel-start.sh + restore-on-crash
Tailscale — the only network anything is exposed on
```

| Want | How you get it |
|---|---|
| Technical SDRangel feel at the desk | SDRangel GUI, untouched (native or Screen Sharing `vnc://wills-mac-mini-1`) |
| Listen on phone | icecast mounts in any browser/player: `/P25.mp3`, `/AIRBAND.mp3`, `/GROUND.mp3` — low bandwidth, works on cellular over Tailscale |
| Tune from phone | scannerctl: scan start/stop, squelch, fleet switch (exists); add VFO retune (device centerFrequency via REST — safe) + scanlist swap (via sdrctl restart + `sdrangel-restore.py`, since FreqScanner lists are NOT API-writable) |
| Digital | SDRTrunk always-on under KeepAlive; call feed already live in scannerctl |
| Anything the UI doesn't cover | Path B: Claude/Dispatch over SSH, unchanged |

## Phases

### Phase 1 — Back on the air, supervised (the reliability core)
1. Commit the WIP on `mac-sdr-arbiter-and-8mhz` (sdrctl edits + the 8 untracked files).
2. Install launchd agents: sdrtrunk (KeepAlive, via the shutdown-hook fork start
   script), sdrangel (`sdrangel-start.sh`, restore config after crash), scannerctl.
   `launchctl bootstrap gui/$(id -u) …`.
3. Boot survival: `sudo pmset autorestart on`, auto-login enabled (per
   `project-mac-mini-boot-autostart`), sudoers rule for sdrctl's apiService kickstart.
4. Prove it: kill -9 SDRangel → back with channels in <60 s; reboot → full stack up
   with no keyboard.

### Phase 2 — Phone audio (the missing piece)
1. `brew install icecast`; bind Tailscale/localhost; launchd KeepAlive. (Reuse mount
   naming from the Linux-era `icecast/` config.)
2. Digital: enable SDRTrunk's Icecast broadcaster in the playlist (edit while stopped —
   SDRTrunk rewrites live files on exit) → `/P25.mp3`.
3. Analog: turn on **UDP audio copy** on the AM + NFM demods in SDRangel; resurrect the
   `op25-audio-bridge.py` core (UDP PCM → ffmpeg → icecast) as
   `macos/bin/audio-bridge.py`, launchd-supervised → `/AIRBAND.mp3`, `/GROUND.mp3`.
4. scannerctl: add a player strip (one `<audio>` element per mount, tap to listen).

### Phase 3 — Phone control polish
- VFO retune + per-scanlist swap in scannerctl (swap = `sdrctl stop sdrangel` →
  restore with chosen list from `analog_scanlists.json` → start; respects throttle).
- Keep scannerctl reachable only via tailnet (bind the Tailscale IP, or leave the
  macOS firewall blocking non-tailnet).

### Phase 4 — Watchdog + soak
- Small health agent (launchd, every 2–5 min): SDRTrunk event_log freshness (CC lock),
  SDRangel REST `:8091` up + deviceset `running`, icecast mount freshness. Remediate
  **only via sdrctl** (throttle preserved), escalate to apiService kickstart, log to one
  place scannerctl can show.
- 72 h soak. Then, optionally, a phone *waterfall* (exploratory tuning): OpenWebRX+ is
  Linux-only — if ever wanted, a Pi/VM appliance with its **own RTL dongle**, never the
  RSPs. Explicitly out of scope for stability.

### Phase 5 (optional) — IQ to the phone (researched + verified 2026-07-03)
Raw-IQ-to-phone IS viable, doctrine-compliant, and mostly already installed:
- **Remote TCP Sink channel** (present in our SDRangel 7.25.1 as `libremotetcpsink.dylib`,
  full feature set since v7.22.2): serves a decimated IQ slice of an existing device set —
  a *tap*, not a second SDR consumer. Two modes: `RTL0` (rtl_tcp-compatible, 8-bit,
  uncompressed — what all iOS apps speak) and `SDRA` (8/16/24/32-bit, FLAC/zlib
  compression, IQ squelch, decimation, max-rate cap, client limits, wss).
- **rtl_tcp on a spare NESDR** (61108285 / 56919602; needs `brew install librtlsdr`):
  phone app fully drives the dongle = true remote VFO. launchd-supervised, RTLs only.
- **Clients** — iOS (all rtl_tcp/RTL0 only): CoronaSDR (free, June 2026, AM/NFM/WFM/SSB/CW
  + waterfall), HotPaw rtl_tcp SDR ($12.99, SSB/CW), SDR Receiver ($9.99, AM/NFM/WFM,
  reduced-rate profiles). No SpyServer or SDRA client exists on iOS. Android: SDRangel
  Android (Play, free — RemoteTCPInput client, but stale v7.21.3.1 = pre-compression;
  uncompressed SDRA or RTL0) or SDR++ nightly APK (actively maintained, rtl_tcp +
  SpyServer sources).
- **Bandwidth math (verified)**: rtl_tcp = rate × 16 bits → 2.048 MS/s ≈ 33 Mbit/s (never
  on cellular), 1.024 ≈ 16 (WiFi/strong LTE only), 0.25 ≈ 4.1 (workable). Tailscale DERP
  fallback ≈ 2 Mbit/s kills IQ (Peer Relays GA Feb 2026 can fix DERP-prone paths); home
  cable uplink is often the real ceiling.
- **SpyServer: ruled out** — no macOS build exists (verified airspy.com 2026-07-03,
  Windows/Linux only, closed source); Docker-on-mac has no USB passthrough.
- **Scope note**: none of these phone apps decode P25 — digital stays SDRTrunk → icecast.
  IQ-to-phone is the exploratory/VFO tool; icecast audio remains the reliable mobile path.

## Non-goals (and why)

| Rejected | Why |
|---|---|
| OpenWebRX+/SpyServer/rtl_tcp on the Mini's RSPs | Second SDRplay consumer = the exact contention class sdrctl exists to prevent |
| sdrangelsrv headless + sdrangelcli as the main remote | Loses the GUI you love at the desk, no audio in browser anyway, and doubles the SDRangel surface area to keep alive |
| VNC as the primary phone path | Works (keep as fallback) but clunky + bandwidth-heavy; streams + scannerctl cover 95 % of mobile use |
| Raw IQ to the phone | Multi-Mbps, fragile on cellular, pointless next to icecast |

## Cross-references
- `docs/macos-backend-migration-scope.md` — the platform decision this executes.
- `macos/sdrtrunk/README.md` — single-tuner pin + CQPSK facts; edit-while-stopped rule.
- `docs/sdrtrunk-shutdown-hook-fork.md`, memory `project-mac-mini-contention-release` —
  clean-release fork + arbiter.
- Linux-era prior art: `scripts/op25-audio-bridge.py` (UDP→ffmpeg→icecast core),
  `icecast/` mount layout, `ui/reliability.py` health-probe patterns.
