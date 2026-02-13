# SprontPi Scanner Project

**Version:** 2.0.0 (2026-02-10)

Scanner control UI and configuration for RTL-SDR dual-dongle airband/GMRS/WX receiver on Raspberry Pi.

**Current Architecture**: Refactored (Jan 2026) from 1,928-line monolith to 11 modular Python units + static web assets.

## Directory Structure

```
.
├── ui/                           # Web UI application (refactored modular architecture)
│   ├── airband_ui.py            # Entry point (14 lines, thin wrapper)
│   ├── app.py                   # HTTP server orchestration
│   ├── config.py                # Centralized constants & env var overrides
│   ├── handlers.py              # HTTP request routing & REST API
│   ├── scanner.py               # Hit detection from journalctl & Icecast
│   ├── profile_config.py        # Profile/control file I/O
│   ├── actions.py               # Business logic dispatcher
│   ├── systemd.py               # systemd unit control
│   ├── icecast.py               # Icecast stream monitoring
│   ├── server_workers.py        # Background worker threads
│   ├── diagnostic.py            # Diagnostic log generation
│   ├── __init__.py              # Package marker
│   ├── sb3.html                 # Scanner Box 3 dashboard (standalone)
│   └── static/                  # Web assets
│       ├── index.html           # UI structure (5.2 KB)
│       ├── style.css            # Styling with CSS variables (5 KB)
│       └── script.js            # Client-side logic (14.3 KB)
├── profiles/                    # rtl_airband frequency profiles
│   ├── rtl_airband_*.conf       # Individual scanner profiles
│   └── trunking/                # P25 talkgroup configs

Profiles notes:
- **Labeling convention:** Profiles use the ICAO airport code as the human-facing label (e.g., **KATL (Atlanta)**). The profile *id* should be short and lowercase (e.g., `"atl"`).
- **Frequency rules:** Only VHF airband frequencies (118.0–136.0 MHz) should be placed in `freqs = (...)` blocks. Out-of-band entries (UHF/other) may be ignored by the UI or treated as invalid.
- **Icecast mount convention:** All profiles must use the single mountpoint `GND.mp3` (do not create profile-specific mount names like `ATL_TWR.mp3`). Metadata and frequency tags are still sent with each hit.
- **Deploying a profile:** Copy or symlink the profile into `/usr/local/etc/airband-profiles` and restart the UI service to pick up label changes:

  sudo cp profiles/rtl_airband_atl.conf /usr/local/etc/airband-profiles/
  sudo systemctl restart airband-ui.service

- **Example:** `profiles/rtl_airband_atl.conf` implements **KATL (Atlanta)** and contains Tower, Approach, and Departure channels (all VHF) and uses `GND.mp3` as the single Icecast mount.
├── scripts/
│   ├── build-combined-config.py # Generates combined dual-scanner config
│   ├── rtl-airband              # Launch wrapper (preserves SIGHUP capability)
│   ├── rtl-airband-*.sh         # Utility scripts for hit logging
│   ├── sb3-ap-fallback.sh       # Boot-time AP fallback when LAN unreachable
│   └── desktop/                 # Desktop button scripts
├── systemd/                     # systemd service units
│   ├── rtl-airband.service      # Main scanner service
│   ├── airband-ui.service       # Web UI service
│   ├── icecast-keepalive.service
│   ├── sb3-ap-fallback.service  # AP fallback (hostapd + dnsmasq)
│   └── trunk-recorder*.service
├── icecast/                     # Icecast configuration
├── admin/                       # Operational files
│   ├── trouble_tickets.csv      # Issue tracking
│   └── logs/                    # Diagnostic logs
├── combined_config.py           # Config generator core logic
├── RELEASE_NOTES.md             # Release notes
└── README.md
```

## Architecture Overview

### High-Level Data Flow

```
RTL-SDR Devices (2)
    ↓
rtl-airband (combined process)
    ├─ Airband scanner (118-136 MHz)
    └─ Ground scanner (VHF/UHF other)
    ↓
Mixer (in rtl-airband)
    ↓
Icecast Mount (/GND.mp3) @ 16 kbps
    ├→ Browser (audio player)
    ├→ Journalctl (activity logging)
    └→ Frequency metadata
    ↓
airband-ui.service (Web UI backend)
    ├─ Reads: journalctl, Icecast status, config files
    ├─ Writes: profile configs, control values
    └─ Exposes: REST API on port 5050
    ↓
Browser (http://sprontpi.local:5050)
    ├─ Displays: profile cards, gain/squelch sliders
    ├─ Shows: last hit pills, hit list, avoids
    └─ Sends: profile/control changes via API
```

### Web UI Architecture (Refactored)

**Before (Jan 2026)**: Single 1,928-line file with no separation of concerns.

**After (Jan 2026)**: Layered modular architecture with clear responsibilities:

1. **Entry Point** (`airband_ui.py`)
   - Sets up Python path
   - Calls `ui.app.main()` via relative imports with absolute fallback
   - Allows systemd execution without package context

2. **HTTP Server** (`app.py`)
   - Initializes `ThreadedHTTPServer` for concurrent requests
   - Starts background worker threads
   - Calls `serve_forever()`

3. **Request Handling** (`handlers.py`)
   - Routes GET/POST requests
   - Serves index.html for `/`
   - Serves static assets with MIME detection
   - Handles REST API endpoints:
     - `GET /api/status` → system status JSON
     - `GET /api/hits` → last 50 hits with time/freq/duration
     - `POST /api/profile` → switch profile
     - `POST /api/apply` → set gain/squelch
     - `POST /api/avoid` → add/clear avoid frequencies
     - `POST /api/diagnostic` → generate diagnostic log

4. **Configuration** (`config.py`)
   - Centralized constants: paths, ports, regex patterns, UI settings
   - Environment variable overrides (prefix: `UI_*`)
   - Sensible defaults for all values
   - Single source of truth for configuration

5. **Scanner Logic** (`scanner.py`)
   - Reads frequency hits from journalctl (`Activity on XXX MHz` patterns)
   - Filters by frequency range (airband 118-136 MHz, ground other)
   - Caches results to reduce journalctl overhead
   - Supports Icecast metadata fallback (if available)

6. **Profile Management** (`profile_config.py`)
   - Read/write rtl_airband.conf and rtl_airband_ground.conf
   - Parse gain/squelch controls with regex
   - Manage avoids (frequency filtering)
   - Symlink-based profile switching
   - Write combined config for dual-scanner setup

7. **System Integration** (`systemd.py`)
   - Control rtl-airband/ground/keepalive services
   - Query service status
   - Non-blocking service restarts

8. **Icecast Monitoring** (`icecast.py`)
   - Query Icecast stream status
   - Extract metadata from audio title
   - Monitor keepalive mount

9. **Business Logic** (`actions.py`)
   - Dispatcher for user actions
   - Profile switching with smart restart (skip if no frequency change)
   - Control apply with debounce
   - Atomic config writes before restart
   - Error handling with rollback support

10. **Background Workers** (`server_workers.py`)
    - Action queue with debounce (0.2 seconds)
    - Icecast monitor thread (updates stream status)
    - Hit log cache updates
    - Runs independently from request handlers

11. **Diagnostics** (`diagnostic.py`)
    - Generates timestamped log bundles
    - Includes system info, service status, recent journalctl logs
    - Supports git commit tagging for correlation

### Tech Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| **Frontend** | HTML5 + CSS3 + Vanilla JS | Web UI (no frameworks) |
| **Backend HTTP** | Python 3.13 `http.server` | HTTP server with threading |
| **OS Integration** | systemd `systemctl` + journalctl | Service control & logging |
| **Audio Stream** | Icecast2 @ 16 kbps mono | Low-latency audio delivery |
| **SDR Hardware** | rtl-airband v5.1.1 | Dual RTL-SDR device control |
| **Data Format** | JSON (REST API) + INI-style `.conf` | Configuration & communication |
| **Version Control** | Git + GitHub | Change tracking & deployment |
| **Process Model** | Threading (background workers) | Concurrent request handling |

### Performance Characteristics

| Metric | Value | Notes |
|--------|-------|-------|
| Profile switch latency | 10-14s | Only restarts if frequency list changes; otherwise instant |
| Audio latency (Tx→Ear) | ~11s | 16 kbps bitrate + Icecast buffering + browser buffer |
| Control debounce | 0.2s | Gain/squelch apply waits 200ms for batching |
| API response time | <100ms | Journalctl scan (200 lines) + JSON serialize |
| Hit detection refresh | 1.5s | Browser poll interval |
| Service restart time | ~10-14s | Device init + Icecast reconnect |
| Web UI load time | ~1-2s | HTML + static assets + first API call |
| HTTP server concurrency | 10+ threads | ThreadingMixIn allows parallel requests |

### Import Pattern

All UI modules support dual import modes for compatibility:

```python
try:
    # Relative imports (package context - preferred)
    from .config import CONFIG_SYMLINK
    from .scanner import read_last_hit_airband
except ImportError:
    # Absolute fallback (systemd context without package setup)
    from ui.config import CONFIG_SYMLINK
    from ui.scanner import read_last_hit_airband
```

This enables:
- Running via systemd (which doesn't set up package context)
- Manual execution with `python3 ui/airband_ui.py`
- Interactive debugging
- Import from other scripts in the repo

## REST API Reference

All endpoints respond with JSON unless otherwise noted.

### GET /
Returns `index.html` (serves entire single-page app).

### GET /api/status
System status snapshot.

**Response** (JSON):
```json
{
  "rtl_active": true,
  "ground_active": false,
  "ground_exists": true,
  "icecast_active": true,
  "keepalive_active": true,
  "profile_airband": "airband",
  "profile_ground": "none_ground",
  "profiles_airband": [
    {"id": "airband", "label": "Airband", "path": "...", "exists": true},
    ...
  ],
  "profiles_ground": [...],
  "missing_profiles": [],
  "gain": 32.8,
  "squelch": 5.0,
  "airband_gain": 32.8,
  "airband_squelch_mode": "dbfs",
  "airband_squelch_dbfs": -80,
  "ground_gain": 36.4,
  "ground_squelch_mode": "dbfs",
  "ground_squelch_dbfs": -90,
  "last_hit": "119.3500",
  "last_hit_airband": "119.3500",
  "last_hit_ground": "",
  "avoids_airband": ["121.900"],
  "avoids_ground": []
}
```
**Notes**:
- `*_squelch_dbfs` is the active squelch value (dBFS).
- Legacy `squelch` fields may still appear for backward compatibility.

### GET /api/hits
Last 50 detected hits (from journalctl activity logs).

**Response** (JSON):
```json
{
  "items": [
    {"time": "10:55:48", "freq": "119.3500", "duration": 0},
    {"time": "10:55:27", "freq": "118.4000", "duration": 0},
    ...
  ]
}
```

**Notes**:
- Duration inferred from time gaps between activity logs (10s reset threshold)
- Frequencies from journalctl `Activity on X.XXX MHz` pattern matches
- Fallback to Icecast metadata if journalctl is empty

### POST /api/profile
Switch to a profile.

**Request** (JSON):
```json
{
  "profile": "airband",
  "target": "airband"
}
```

**Response** (JSON):
```json
{"ok": true, "changed": true}
```

**Behavior**:
- Regenerates combined config
- Skips restart if frequency list unchanged
- Restarts rtl-airband only when device config differs

### POST /api/apply
Set gain and squelch for a target scanner (dBFS squelch).

**Request** (JSON):
```json
{
  "gain": 32.8,
  "squelch_mode": "dbfs",
  "squelch_dbfs": -80,
  "target": "airband"
}
```
Legacy clients may still POST `squelch` (SNR). Backend defaults to dBFS mode in the current UI.

**Response** (JSON):
```json
{"ok": true, "changed": true}
```

**Behavior**:
- Validated gain ranges
- dBFS squelch: `0` = auto, negative opens more (try -70 to -100)
- Writes config file
- Debounced 0.2 seconds (batches rapid changes)
- Restarts rtl-airband if change detected

### POST /api/avoid
Add or clear avoid frequencies.

**Request** (JSON):
```json
{
  "action": "add",
  "freq": "121.900",
  "target": "airband"
}
```

Or to clear:
```json
{
  "action": "clear",
  "target": "airband"
}
```

**Response** (JSON):
```json
{"ok": true}
```

**Notes**:
- Avoids stored in rtl_airband config as notch list
- Survives profile changes (moves with config)

### POST /api/diagnostic
Generate diagnostic log bundle.

**Request** (JSON):
```json
{}
```

**Response** (JSON):
```json
{"path": "/home/willminkoff/scannerproject/admin/logs/diagnostic-20260110-165106.txt"}
```

**Contents**: System info, systemd status, journalctl logs, Icecast status, git commit info.

### Digital (Experimental)
Live-only digital backend control with in-memory metadata (no recording or persistence).

**Environment variables**:
- `DIGITAL_BACKEND` (default: `sdrtrunk`)
- `DIGITAL_SERVICE_NAME` (systemd unit name, default: `scanner-digital`)
- `DIGITAL_PROFILES_DIR` (profiles root directory)
- `DIGITAL_ACTIVE_PROFILE_LINK` (symlink pointing at the active profile dir)
- `DIGITAL_BOOT_DEFAULT_PROFILE` (default: `default`; used at boot if active symlink is missing/broken)
- `DIGITAL_LOG_PATH` (sdrtrunk log path used for last-event parsing)
- `DIGITAL_PLAYLIST_PATH` (SDRTrunk playlist XML updated on profile switch; default: `~/SDRTrunk/playlist/default.xml`)
- `DIGITAL_EVENT_LOG_DIR` (SDRTrunk event logs directory; default: `~/SDRTrunk/event_logs`)
- `DIGITAL_EVENT_LOG_MODE` (`auto` | `event_logs` | `app_log`)
- `DIGITAL_EVENT_LOG_TAIL_LINES` (default: `500`)
- `AIRBAND_RTL_SERIAL` (preferred airband RTL serial; optional)
- `GROUND_RTL_SERIAL` (preferred ground RTL serial; optional)
- `DIGITAL_RTL_SERIAL` (dedicated RTL-SDR serial for SDRTrunk; recommended)
- `DIGITAL_RTL_SERIAL_SECONDARY` (optional second digital RTL serial for traffic capacity)
- `DIGITAL_RTL_DEVICE` (RTL-SDR device index or serial; used by your SDRTrunk profile configuration)
- `DIGITAL_PREFERRED_TUNER` (optional explicit SDRTrunk tuner name; overrides `DIGITAL_RTL_SERIAL` when set)
- `DIGITAL_USE_MULTI_FREQ_SOURCE` (default: `1`; when enabled, runtime writes multi-frequency control-channel source config)
- `DIGITAL_SOURCE_ROTATION_DELAY_MS` (default: `500`; rotation delay for multi-frequency control scanning)
- `DIGITAL_SDRTRUNK_STREAM_NAME` (default: `DIGITAL`; SDRTrunk stream name used for alias `broadcastChannel` wiring)
- `DIGITAL_ATTACH_BROADCAST_CHANNEL` (default: `1`; auto-adds `broadcastChannel` IDs for active profile alias talkgroups)
- `DIGITAL_MIXER_ENABLED` (default: `0`) - enable mixing SDRTrunk audio into `scannerbox.mp3`
- `DIGITAL_MIXER_AIRBAND_MOUNT` (default: `GND-air.mp3`) - raw airband+ground input mount for the mixer
- `DIGITAL_MIXER_DIGITAL_MOUNT` (default: `DIGITAL.mp3`) - SDRTrunk input mount for the mixer
- `DIGITAL_MIXER_OUTPUT_MOUNT` (default: `scannerbox.mp3`) - final mixed output mount
- `LIQUIDSOAP_BIN` (default: `/usr/bin/liquidsoap`) - liquidsoap binary path for mixer runtime
- `DIGITAL_MIXER_LIQ_PATH` (default: `/run/scanner-digital-mixer.liq`) - generated runtime liquidsoap script
- `DIGITAL_MIXER_LIQ_QUIET` (default: `1`) - run liquidsoap with `-q` for quieter logs
- `DIGITAL_LOCAL_MONITOR` (default: `0`) - when `0`, SDRTrunk direct local Java sink inputs are auto-muted on service startup
- `DIGITAL_LOCAL_MONITOR_WAIT_SEC` (default: `20`) - startup wait window to find SDRTrunk sink inputs before giving up
- `DIGITAL_LOCAL_MONITOR_POLL_SEC` (default: `1`) - poll interval while waiting for SDRTrunk sink inputs
- `VLC_HTTP_RECONNECT` (default: `1`) - enables local Pi VLC reconnect for Play button stream playback
- `VLC_NETWORK_CACHING_MS` (default: `1000`) - local Pi VLC network buffer/caching for stream stability
- `ICECAST_SOURCE_PASSWORD` (default: `062352`) - must match your Icecast source password if customized

**Profile model**:
- Each profile is a directory under `DIGITAL_PROFILES_DIR` (e.g. `metro-p25`, `regional-dmr`).
- `POST /api/digital/profile` updates the `DIGITAL_ACTIVE_PROFILE_LINK` symlink, updates `DIGITAL_PLAYLIST_PATH` runtime frequency to the first entry in `control_channels.txt`, and restarts the service.

**RTL device binding (SDRTrunk)**:
Set `DIGITAL_RTL_SERIAL` to the RTL serial you want SDRTrunk to use, then reference that serial in your SDRTrunk profile configuration (exact field name depends on your SDRTrunk version/export).
If you must use indexes, use `DIGITAL_RTL_DEVICE`, but serial binding is strongly recommended.
This repo already tracks RTL-SDR serials for the analog scanners in `profiles/rtl_airband_*.conf` and enforces serials in the combined config; reuse the same serials to avoid device collisions.
At runtime, `ensure-digital-runtime.py` and profile switches now write the SDRTrunk `source_configuration` from `control_channels.txt`:
- first control channel is still honored
- when multiple control channels exist, it uses a multi-frequency tuner source
- preferred tuner is set from `DIGITAL_PREFERRED_TUNER`, or `DIGITAL_RTL_SERIAL` if no explicit preferred tuner is provided

**Dongle serial locking**:
These mappings prevent device-busy conflicts:
- Airband (rtl-airband): `00000002`
- Ground (rtl-airband): `70613472`
- Digital (SDRTrunk): `56919602`

Enforcement:
- Airband/Ground serials are set in `profiles/rtl_airband_*.conf` and flow into the combined config.
- Digital serial is set via `DIGITAL_RTL_SERIAL` and selected in SDRTrunk’s tuner configuration.
- `/api/digital/preflight` reports recent tuner-busy errors and expected serials.

**Second digital dongle (P25 ready)**:
Use this when you want one digital tuner pinned for control-channel tracking and an extra tuner available for traffic channels.

Example `/etc/airband-ui.conf`:
```bash
DIGITAL_RTL_SERIAL=56919602
DIGITAL_RTL_SERIAL_SECONDARY=56919603
DIGITAL_USE_MULTI_FREQ_SOURCE=1
DIGITAL_SOURCE_ROTATION_DELAY_MS=500
```

Optional exact tuner-name pinning (from SDRTrunk tuner label):
```bash
DIGITAL_PREFERRED_TUNER="RTL2832 SDR/R820T 56919602"
```

Verification:
- `GET /api/digital/preflight` should show:
  - `expected_serials.digital` and `expected_serials.digital_secondary`
  - `playlist_source_ok=true`
  - `playlist_source_type=TUNER_MULTIPLE_FREQUENCIES` (when profile has >1 control channel)
  - `playlist_preferred_tuner` matching your configured control tuner

**SprontPi recommended defaults**:
If you are on SprontPi, set these in `/etc/airband-ui.conf` (or your UI EnvironmentFile):
```bash
AIRBAND_RTL_SERIAL=00000002
GROUND_RTL_SERIAL=70613472
DIGITAL_RTL_SERIAL=56919602
DIGITAL_BOOT_DEFAULT_PROFILE=tacn-all
DIGITAL_LOCAL_MONITOR=0
AIRBAND_FALLBACK_PROFILE_PATH=/usr/local/etc/airband-profiles/rtl_airband_airband.conf
GROUND_FALLBACK_PROFILE_PATH=/usr/local/etc/airband-profiles/rtl_airband_wx.conf
```

For local-audio debugging sessions, set `DIGITAL_LOCAL_MONITOR=1` and restart `scanner-digital` to leave SDRTrunk's direct monitor path unmuted.

**Digital profiles (filesystem layout)**:
Profiles live under `DIGITAL_PROFILES_DIR` and the active profile is pointed to by `DIGITAL_ACTIVE_PROFILE_LINK`.
Example scaffolding lives in `deploy/examples/digital-profiles/profiles/` (includes `profiles/example/`).
Each profile directory should contain the SDRTrunk config exports you want to run for that profile.

**Install steps (profiles)**:
```bash
sudo mkdir -p /etc/scannerproject/digital/profiles
sudo cp -R /home/willminkoff/scannerproject/deploy/examples/digital-profiles/profiles/vanderbilt-university /etc/scannerproject/digital/profiles/
sudo ln -sfn /etc/scannerproject/digital/profiles/vanderbilt-university /etc/scannerproject/digital/active
sudo systemctl restart scanner-digital
```
Permissions: ensure the service user (e.g. `pi` or your dedicated account) can read the profile directories and write to `/var/log/sdrtrunk/`. If you run as a non-root user, set ownership accordingly, for example:
```bash
sudo chown -R pi:pi /etc/scannerproject/digital/profiles
sudo chown -R pi:pi /var/log/sdrtrunk
```

**Systemd unit (headless SDRTrunk)**:
Unit file: `systemd/scanner-digital.service`
Notes:
Defaults to `User=pi`; update the unit if your service account differs.
`WorkingDirectory` points at `DIGITAL_ACTIVE_PROFILE_LINK` so the active profile is the runtime directory.
Logs append to `/var/log/sdrtrunk/sdrtrunk.log` (systemd `LogsDirectory=sdrtrunk` ensures the folder exists).
Adjust `ExecStart` if your SDRTrunk binary path or headless flag differs.

**Install/enable**:
```bash
sudo install -m 0644 /home/willminkoff/scannerproject/systemd/scanner-digital.service /etc/systemd/system/scanner-digital.service
sudo systemctl daemon-reload
sudo systemctl enable --now scanner-digital
```

**Allow UI to control `scanner-digital` (no interactive auth prompts)**:
The UI runs as a non-root user. To allow start/stop/restart from the browser, add a sudoers rule:
```bash
sudo visudo -f /etc/sudoers.d/airband-ui
```
Add (replace `willminkoff` with your user):
```
willminkoff ALL=NOPASSWD: /bin/systemctl start scanner-digital, /bin/systemctl stop scanner-digital, /bin/systemctl restart scanner-digital
```

**Digital audio mixing (optional)**:
Mix SDRTrunk audio into the `scannerbox.mp3` stream via a resilient Liquidsoap mixer.

1. **Set mixer env vars** (example in `/etc/airband-ui.conf`):
```bash
DIGITAL_MIXER_ENABLED=1
DIGITAL_MIXER_AIRBAND_MOUNT=GND-air.mp3
DIGITAL_MIXER_DIGITAL_MOUNT=DIGITAL.mp3
DIGITAL_MIXER_OUTPUT_MOUNT=scannerbox.mp3
# ICECAST_SOURCE_PASSWORD=062352  # set if you changed your Icecast password
```

2. **Restart rtl-airband** (so combined config uses the new airband mount):
```bash
sudo systemctl restart rtl-airband
```

3. **Configure SDRTrunk streaming**:
   - In **Streaming** tab, add an Icecast stream:
     - Server: `127.0.0.1`
     - Port: `8000`
     - Mount: `DIGITAL.mp3`
     - User: `source`
     - Password: your Icecast source password
   - Save and enable the stream.

4. **Install + enable mixer service**:
```bash
sudo apt-get update
sudo apt-get install -y liquidsoap
sudo install -m 0644 /home/willminkoff/scannerproject/systemd/scanner-digital-mixer.service /etc/systemd/system/scanner-digital-mixer.service
sudo systemctl daemon-reload
sudo systemctl enable --now scanner-digital-mixer
```

Notes:
- The mixer reads the runtime mute flag from `/run/airband_ui_digital_mute.json`; the Digital mute toggle will drop SDRTrunk audio from the mix.
- If one input stream goes offline, Liquidsoap keeps the output alive and substitutes silence for the missing leg.

**Create profiles + set active**:
```bash
sudo mkdir -p /etc/scannerproject/digital/profiles/metro-p25
sudo mkdir -p /etc/scannerproject/digital/profiles/regional-dmr
sudo ln -sfn /etc/scannerproject/digital/profiles/metro-p25 /etc/scannerproject/digital/active
sudo systemctl restart scanner-digital
```

**/api/status fields**:
- `digital_active` (boolean)
- `digital_backend` (string)
- `digital_profile` (string)
- `digital_muted` (boolean)
- `digital_last_label` (string)
- `digital_last_mode` (string, optional)
- `digital_last_time` (epoch ms, 0 if none)
- `digital_last_error` (string, optional)
- `expected_serials` (object with airband/ground/digital expected RTL serials)
- `serial_mismatch` (boolean)
- `serial_mismatch_detail` (array with mismatch details)

**Endpoints**:
- `POST /api/digital/start`
- `POST /api/digital/stop`
- `POST /api/digital/restart`
- `POST /api/digital/mute` → body: `{ "muted": true }`
- `GET  /api/digital/profiles`
- `POST /api/digital/profile` → body: `{ "profileId": "..." }`
- `POST /api/digital/profile/create` → body: `{ "profileId": "..." }`
- `POST /api/digital/profile/delete` → body: `{ "profileId": "..." }`
- `POST /api/digital/profile/inspect` → body: `{ "profileId": "..." }`
- `GET  /api/digital/talkgroups?profileId=...`
- `POST /api/digital/talkgroups/listen` → body: `{ "profileId": "...", "items": [{"dec":"47008","listen":true}] }`
- `GET  /api/digital/preflight` → tuner-busy detection + expected serials

**SB3 Digital profile management**:
- Digital tab → Digital Profiles widget lets you create/delete profile folders, select an active profile, and load a preview of files in the profile directory.
- Talkgroup Listen panel now includes Mode-aware controls:
  - Columns: `TGID`, `Mode`, `Label`, `Tag`
  - Filters: listen-state (`All`/`Listening`/`Muted`) + mode (`All Modes`/`Clear`/`Encrypted`/`D`/`T`/`DE`/`TE`)
  - Bulk actions: `Select Filtered`, `Unselect Filtered`, and `Mute Encrypted`
  - Sorting: click `TGID`/`Mode`/`Label`/`Tag` headers to sort asc/desc
  - `Mute Encrypted` mutes encrypted-only TGIDs (it does not auto-mute TGIDs that have both clear and encrypted entries)

**RadioReference TXT import helper**:
- `scripts/rr_txt_to_profiles.py` parses RR TXT exports into digital-profile scaffolding.
- TACN example output is tracked at `deploy/examples/digital-profiles/profiles/tacn/tacn-all/`:
  - `control_channels.txt` (all TACN control channels)
  - `talkgroups.csv` / `talkgroups_with_group.csv` (combined TACN talkgroups)

**HomePatrol HPDB offline database workflow**:
- `scripts/homepatrol_db.py` imports Uniden HomePatrol `.hpd` files into SQLite so you can build new profiles locally without web lookups.
- Example import from a mounted HomePatrol SD card:
```bash
mkdir -p /home/willminkoff/scanner-db
python3 scripts/homepatrol_db.py \
  --db /home/willminkoff/scanner-db/homepatrol.db \
  import \
  --hpdb-root "/media/willminkoff/NO NAME/HomePatrol/HPDB"
```
- Fast lookups:
```bash
# Find systems
python3 scripts/homepatrol_db.py --db /home/willminkoff/scanner-db/homepatrol.db \
  find-system "middle tennessee"

# Find talkgroups by keyword/TGID
python3 scripts/homepatrol_db.py --db /home/willminkoff/scanner-db/homepatrol.db \
  find-talkgroup "vanderbilt" --system "middle tennessee"

# List site frequencies (for control/channel planning)
python3 scripts/homepatrol_db.py --db /home/willminkoff/scanner-db/homepatrol.db \
  site-freqs --system "Middle Tennessee Regional Trunked Radio System" --site "Goodlettsville"
```
- Build a digital profile directory directly:
```bash
python3 scripts/homepatrol_db.py --db /home/willminkoff/scanner-db/homepatrol.db \
  build-profile \
  --system "Middle Tennessee Regional Trunked Radio System" \
  --site "Davidson County Simulcast" \
  --site "Goodlettsville" \
  --group "Vanderbilt University" \
  --group "Davidson County - Goodlettsville" \
  --group "Davidson County - Belle Meade" \
  --out /etc/scannerproject/digital/profiles \
  --profile-name "MTRTRS Metro Clear" \
  --profile-slug mtrtrs-metro-clear
```
- Build an analog `rtl_airband` profile directly from conventional channels in DB:
```bash
# Airband example
python3 scripts/homepatrol_db.py --db /home/willminkoff/scanner-db/homepatrol.db \
  build-analog-profile \
  --system "Nashville International Airport (BNA)" \
  --state TN \
  --profile-type airband \
  --out /usr/local/etc/airband-profiles \
  --profile-id bna-hpdb

# Ground example
python3 scripts/homepatrol_db.py --db /home/willminkoff/scanner-db/homepatrol.db \
  build-analog-profile \
  --system "Highway Patrol (THP)" \
  --state TN \
  --profile-type ground \
  --out /usr/local/etc/airband-profiles \
  --profile-id thp-ground
```
- Output files match the Digital UI expectations:
  - `control_channels.txt`
  - `talkgroups.csv`
  - `talkgroups_with_group.csv`
- Note: HomePatrol exports do not mark control channels explicitly, so generated `control_channels.txt` contains site frequencies for selected sites.

**HomePatrol Favorites (HPCOPY.zip) converter**:
- `scripts/homepatrol_favorites.py` reads `HPCOPY.zip` backups and converts a Favorites list into scannerproject files.
- List available Favorites and quick counts:
```bash
python3 scripts/homepatrol_favorites.py \
  --zip /home/willminkoff/Desktop/HPCOPY.zip \
  list
```
- Export one Favorites list (example: active `POLICE`) into a profile folder:
```bash
python3 scripts/homepatrol_favorites.py \
  --zip /home/willminkoff/Desktop/HPCOPY.zip \
  export \
  --favorite POLICE \
  --out /etc/scannerproject/digital/profiles
```
- Export creates:
  - `control_channels.txt`
  - `talkgroups.csv`
  - `talkgroups_with_group.csv`
  - `conventional.csv` (analog reference)
  - `README.md` with source details
- This is useful when your HomePatrol Favorites lists are better-curated than broad HPDB/system-wide imports.

**Adding control channels + talkgroups (RadioReference workflow)**:
1. **Find the system on RadioReference**: note the system type (P25/DMR/etc.), site(s), and **control channels** (primary + alternate).
2. **Create the trunked system in SDRTrunk**:
   - Add a new trunked system matching the RadioReference system type.
   - Add a site and paste the control channels (RR lists them as red/blue).
3. **Add talkgroups**:
   - Create a talkgroup/alias list and import from RadioReference (CSV export if you have a premium account), or paste/enter TGIDs manually.
   - Keep labels short; these show up as the Digital “last hit” pill (truncated to ~10 chars in SB3).
4. **Bind the tuner**:
   - In SDRTrunk, select the RTL device serial that matches `DIGITAL_RTL_SERIAL`.
5. **Export/copy the SDRTrunk config into your profile folder**:
   - Use the Digital Profiles widget to create a folder, then copy your SDRTrunk export into `/etc/scannerproject/digital/profiles/<profile>`.
   - Activate it in the UI (or update the `DIGITAL_ACTIVE_PROFILE_LINK` symlink) and restart `scanner-digital`.

**Notes**:
- RadioReference data is subject to their terms; use it as your source and avoid redistributing proprietary exports.
- SDRTrunk configuration file names may vary by version; the “Inspect” endpoint lets you confirm what’s inside a profile.

### GET /static/*
Serve static web assets.

**Supported files**:
- `static/index.html` (5.2 KB)
- `static/style.css` (5 KB)
- `static/script.js` (14.3 KB)

**MIME types**:
- `.html` → `text/html`
- `.css` → `text/css`
- `.js` → `application/javascript`

### GET /sb3.html
Scanner Box 3 (SB3) - Alternative dashboard UI with modern widget-based layout.

**Access**: `http://sprontpi.local:5050/sb3` (or `http://sprontpi.local:5050/sb3.html`)

---

## SB3 Dashboard (Scanner Box 3)

A production-ready alternative UI built as a single-page HTML file with embedded CSS and JavaScript. Designed for at-a-glance monitoring with a utilitarian, widget-based layout.

### Layout

**Scanner View**
```
┌─────────────────────────────────────────────────────────┐
│ SB3   [SDR1●] [SDR2●] [Ice●]   119.3500    Connected    │  ← Header
├────────────────────────┬────────────────────────────────┤
│ ⚙️ Radio Controls      │ 🎯 Profiles                    │  ← Row 1
│ [Airband] [Ground]     │ [KBNA (Nashville)]  ●          │
│ Gain ────●──── 22.9    │ [Nashville Centers]            │
│ Squelch ──●──── -80    │ [TOWER (118.600)]              │
│ Filter ───●──── 2900   │ [KHOP (Campbell)]              │
│ [Play][Tune][Avoid][Clear][Apply]                      │
│ Player (Icecast)       │ Manage Profiles / Edit Freqs   │
│ Pi Health tiles        │                               │
├────────────────────────┼────────────────────────────────┤
│ 📊 Signal Activity     │ 📋 Recent Hits                 │  ← Row 2
│ Hits/hr  Session       │ Time     Frequency      Dur    │
│ Avoids   Local Time    │ 14:06:28  119.350 MHz   7s     │
├────────────────────────┴────────────────────────────────┤
│ ✈️ ADS-B Traffic (iframe)                               │  ← Row 3
└─────────────────────────────────────────────────────────┘
```

**Sitrep View (flip from SB3 logo)**
```
┌─────────────────────────────────────────────────────────┐
│ SITUATION REPORT                         Updated 10:42 │
├─────────────────────────────────────────────────────────┤
│ Live Status: last hit, profiles, tune/hold state        │
│ Gain/Squelch/Filter applied (Airband + Ground)          │
│ Services: airband, ground, icecast, keepalive, ui, adsb │
│ Heartbeat dots + last-seen timestamps                   │
├─────────────────────────────────────────────────────────┤
│ Activity Log (live)                                     │
└─────────────────────────────────────────────────────────┘
```

### Features

| Feature | Description |
|---------|-------------|
| **Dual SDR Status** | SDR1 (airband) and SDR2 (ground) indicators with pulsing animation |
| **Icecast Status** | Shows streaming server health |
| **Airband/Ground Tabs** | Switch between radio targets with independent settings |
| **Apply Button** | Slider changes batch until you click **Apply** (single restart) |
| **GAIN_STEPS Array** | 29 discrete RTL-SDR gain values (0.0 to 49.6 dB) |
| **Profile Switching** | Visual profile grid, active profile highlighted |
| **Hit List** | Scrollable list of recent frequency hits with duration |
| **Session Counter** | Tracks hits since page load with hits/hour rate |
| **ADS-B Map** | Embedded adsb.lol iframe for live flight tracking |
| **SSE + Polling** | Real-time updates via Server-Sent Events, polling fallback |
| **Embedded Player** | Built-in audio player for the Icecast stream |
| **Sitrep** | Flip view for system status, config sync, and health |

### Technical Details

**Data Sources**:
- `/api/status` - SDR states, Icecast status, current profiles, gain/squelch values
- `/api/hits` - Last 50 hits from Icecast hit log + journalctl fallback
- `/api/stream` (SSE) - Real-time status and hit updates

**Gain Control**:
```javascript
const GAIN_STEPS = [
  0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7,
  16.6, 19.7, 20.7, 22.9, 25.4, 28.0, 29.7, 32.8, 33.8,
  36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48.0, 49.6,
];
```
Slider maps to index (0-28), value sent to backend is the actual dB value.

**Squelch Control (dBFS)**:
- UI slider ranges from **-120 to 0**
- `-120` = fully open
- `-1` = tightest/closed (UI clamps 0 to -1 for rtl_airband behavior)

**Update Strategy**:
- SSE merges new hits (doesn't overwrite) to prevent flashing
- Polling every 2 seconds as backup
- Last hit display falls back: `last_hit` → `last_hit_airband` → `last_hit_ground`

**CSS Grid Layout**:
```css
.grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.grid-full { grid-column: 1 / -1; }  /* ADS-B spans full width */
```

### Error Responses

**400 Bad Request**:
```json
{"ok": false, "error": "unknown profile"}
```

**500 Server Error**:
```json
{"ok": false, "error": "combine failed: ..."}
```

## Profiles Registry
Profiles are now stored in a JSON registry at:
`/usr/local/etc/airband-profiles/profiles.json`

Format:
```json
{
  "profiles": [
    {"id":"airband","label":"KBNA (Nashville)","path":"/usr/local/etc/airband-profiles/rtl_airband_airband.conf","airband":true}
  ]
}
```

If the registry does not exist, it is bootstrapped from the legacy `PROFILES` list.

### Profile API (CRUD + Frequency Editor)

- `GET /api/profiles` -> lists all profiles and active ids
- `POST /api/profile` -> switch active scan profile (existing behavior)
- `POST /api/profile/create` -> clone a profile (optionally accepts `freqs_text`)
- `POST /api/profile/update` -> update a profile label
- `POST /api/profile/delete` -> delete a non-active profile
- `GET /api/profile?id=<profile_id>` -> read frequencies/labels for a profile
- `POST /api/profile/update_freqs` -> update frequencies (and optional labels) for a profile

Frequency editor textarea format (one per line):
```
118.600 TOWER
119.350 APP
124.750
```

Notes:
- Labels are optional. If you omit labels, existing labels are preserved only when the count still matches.
- If you edit the currently-active profile, the backend regenerates the combined config and restarts `rtl-airband` only when needed (skip restart when nothing changed).

## Deployment

### Quick Deploy
```bash
cd /home/willminkoff/scannerproject
git pull origin main
sudo python3 scripts/build-combined-config.py
sudo systemctl restart rtl-airband airband-ui
```

### Full Deploy (from Mac)
```bash
# Deploy code
rsync -av ui/ willminkoff@sprontpi.local:/home/willminkoff/scannerproject/ui/

# Rebuild config
ssh willminkoff@sprontpi.local "cd /home/willminkoff/scannerproject && sudo python3 scripts/build-combined-config.py"

# Restart services
ssh willminkoff@sprontpi.local "sudo systemctl restart rtl-airband airband-ui"
```

### Verify Deployment
```bash
# Check UI is responsive
curl -s http://sprontpi.local:5050/api/status | jq '.profile_airband'

# Check rtl-airband logs
ssh willminkoff@sprontpi.local "journalctl -u rtl-airband -n 20 --no-pager"

# Check Icecast stream
curl -s http://sprontpi.local:8000/status-json.xsl | jq '.icestats.source[] | {mount, listeners}'
```
- UI: profiles render as a two-column grid of selectable cards and show an avoids summary for the active profile.
- Speed: profile/gain/squelch apply skips restart when no changes were made.
- Logging: `systemd/rtl-airband.service` now points at `scripts/rtl-airband-with-freq.sh` to strip control codes from logs.
- Icecast: example config now reduces buffering (`queue-size`, `burst-size`) and lowers `source-timeout` for faster recovery after restarts.
- UI: last hit shows "No hits yet" when empty and centers the pill text.

## Last Hit Pills
The UI displays two "hit pills" showing the most recent frequency detected on each scanner:
- **Airband Hits**: Shows frequencies in the 118.0–136.0 MHz range
- **Ground Hits**: Shows all other frequencies (VHF/UHF)

How it works:
- `scripts/rtl-airband-last-hit.sh` monitors the journalctl output from `rtl-airband` unit
- Each detected frequency is filtered by range: if it falls within 118–136 MHz, it updates the airband pill; otherwise it updates the ground pill
- Files: `/run/rtl_airband_last_freq_airband.txt` and `/run/rtl_airband_last_freq_ground.txt`
- The UI refreshes these pills every 1.5 seconds from the backend API

This approach works correctly even though both scanners run in a single combined rtl_airband process, since they output to the same journalctl unit and need to be separated by frequency range rather than by unit.

## Combined Config Generation

SprontPi runs two RTL-SDR dongles and two scanner profiles in a single `rtl-airband` process:
- **Device 0**: Airband profiles (118–136 MHz, aviation)
- **Device 1**: Ground profiles (GMRS, WX, other VHF/UHF)

### How It Works

The `combined_config.py` script generates `${COMBINED_CONFIG_PATH}` (defaults to `/usr/local/etc/rtl_airband_combined.conf`) from:
- Active Airband profile: symlink target of `${CONFIG_SYMLINK}` (defaults to `/usr/local/etc/rtl_airband.conf`)
- Active Ground profile: `${GROUND_CONFIG_PATH}` (defaults to `/usr/local/etc/rtl_airband_ground.conf`)

**Generation steps**:
1. Extract device configs from airband + ground profiles
2. Enforce device indexes (0, 1) and serials to prevent collisions
3. Replace per-channel outputs with mixer references
4. Define single Icecast output at mixer (16 kbps mono)
5. Write atomically to `${COMBINED_CONFIG_PATH}`

**Bitrate override**: All profiles' bitrate is overridden to **16 kbps** for low-latency streaming (less buffering = faster audio startup).

### Profile Switching Logic

When user selects new profile:

1. **Write config** to symlink target (`${CONFIG_SYMLINK}`)
2. **Regenerate combined config** via `build-combined-config.py`
3. **Check if changed**: Only restart rtl-airband if combined config differs from previous version
4. **Avoid unnecessary restarts**: If frequency list is identical, skip restart entirely

This optimization eliminates 10-15 second delays when switching between profiles with same frequencies.

**Startup fallback behavior**:
- If `${CONFIG_SYMLINK}` or `${GROUND_CONFIG_PATH}` is missing/broken at service startup, `build-combined-config.py` falls back to:
  - `${AIRBAND_FALLBACK_PROFILE_PATH}` (default: `/usr/local/etc/airband-profiles/rtl_airband_airband.conf`)
  - `${GROUND_FALLBACK_PROFILE_PATH}` (default: `/usr/local/etc/airband-profiles/rtl_airband_wx.conf`)
- Combined config output directory is created automatically before atomic write.

### Systemd Integration

The `rtl-airband.service` runs:
- **ExecStartPre**: Regenerate combined config
- **ExecStart**: Launch rtl-airband with combined config + logging wrapper
- **RestartSec=2**: Controlled restart loop on failure
- **TimeoutStopSec=1**: Fast shutdown (1 second)
- **KillSignal=SIGINT**: Clean shutdown signal
- **StartLimitIntervalSec=0**: Prevent lockout after repeated startup failures (keeps retrying)

The `scanner-digital.service` runs:
- **ExecStartPre**: `scripts/ensure-digital-runtime.py`
  - Repairs/creates `${DIGITAL_ACTIVE_PROFILE_LINK}` if missing/broken
  - Picks `${DIGITAL_BOOT_DEFAULT_PROFILE}` or first profile directory as fallback
  - Syncs `${DIGITAL_PLAYLIST_PATH}` channel frequency from active profile `control_channels.txt`
- **ExecStartPost**: `scripts/sdrtrunk-local-monitor.py apply`
  - Mutes SDRTrunk's direct local Java sink inputs by default (`DIGITAL_LOCAL_MONITOR=0`) so local speakers follow the mixed Icecast/VLC path
  - Skip mute for debug by setting `DIGITAL_LOCAL_MONITOR=1`
- **Restart=always + StartLimitIntervalSec=0**: Keeps recovering after boot-time or runtime failures

## Pi Notes
- Repo path on SprontPi: `/home/willminkoff/scannerproject`
- User: `willminkoff` (prompt shows `willminkoff@SprontPi`)
- Avoids summary files: `/home/willminkoff/Desktop/scanner_logs/airband_avoids.txt`, `/home/willminkoff/Desktop/scanner_logs/ground_avoids.txt`
- Config overrides:
  - `/etc/airband-ui.conf` (EnvironmentFile) can set `CONFIG_DIR`, `CONFIG_SYMLINK`, `GROUND_CONFIG_PATH`, `COMBINED_CONFIG_PATH`.
- Deploy commands:
  - `cd /home/willminkoff/scannerproject`
  - `git pull origin main`
  - `sudo systemctl daemon-reload`
  - `sudo systemctl restart rtl-airband`
  - `sudo systemctl restart airband-ui`
  - `sudo systemctl disable --now rtl-airband-ground` (one-time, if present; stays disabled after)
- Power-loss hardening rollout:
  - `chmod +x /home/willminkoff/scannerproject/scripts/ensure-digital-runtime.py /home/willminkoff/scannerproject/scripts/reboot-stack-check.sh`
  - `sudo install -m 0644 /home/willminkoff/scannerproject/systemd/rtl-airband.service /etc/systemd/system/rtl-airband.service`
  - `sudo install -m 0644 /home/willminkoff/scannerproject/systemd/scanner-digital.service /etc/systemd/system/scanner-digital.service`
  - `sudo install -m 0644 /home/willminkoff/scannerproject/systemd/scanner-digital-mixer.service /etc/systemd/system/scanner-digital-mixer.service`
  - `sudo install -m 0644 /home/willminkoff/scannerproject/systemd/airband-ui.service /etc/systemd/system/airband-ui.service`
  - `sudo systemctl daemon-reload`
  - `sudo systemctl enable --now icecast2 rtl-airband scanner-digital scanner-digital-mixer airband-ui rtl-airband-last-hit`
  - `sudo systemctl restart rtl-airband scanner-digital scanner-digital-mixer airband-ui`
  - `sudo /home/willminkoff/scannerproject/scripts/reboot-stack-check.sh 90`
- Unit update (last-hit):
  - `sudo cp /home/willminkoff/scannerproject/systemd/rtl-airband-last-hit.service /etc/systemd/system/`
  - `sudo systemctl daemon-reload`
  - `sudo systemctl restart rtl-airband-last-hit`
- AP fallback (optional, start AP when LAN unreachable):
  - `sudo install -m 755 /home/willminkoff/scannerproject/scripts/sb3-ap-fallback.sh /usr/local/sbin/sb3-ap-fallback`
  - `sudo cp /home/willminkoff/scannerproject/systemd/sb3-ap-fallback.service /etc/systemd/system/`
  - Optional config: `/etc/sb3-ap-fallback.conf` (defaults: `PING_IP=1.1.1.1`, `BOOT_WAIT_SEC=25`, `AP_SSID=SB3-CTRL`, `AP_IP=192.168.4.1`)
  - `sudo systemctl daemon-reload && sudo systemctl enable --now sb3-ap-fallback`

## Ops Brief
- `assets/Brief from Codex CLI 1-2-26.txt`

## Desktop Buttons
- Scripts: `scripts/desktop/start_scanner.sh`, `scripts/desktop/stop_scanner.sh`
- Launchers: `assets/Start Scanner.desktop`, `assets/Stop Scanner.desktop`
- Install on Pi:
  - `chmod +x /home/willminkoff/scannerproject/scripts/desktop/*.sh`
  - `cp /home/willminkoff/scannerproject/scripts/desktop/*.sh /home/willminkoff/Desktop/`
  - `cp /home/willminkoff/scannerproject/assets/*.desktop /home/willminkoff/Desktop/`
