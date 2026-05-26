# RSPduo MA/SL Split-Process Architecture

## Why

The single-process rtl-airband-in-DT-mode arrangement we built on 2026-05-25
proved unreliable across 24h of field use.  The specific failure: in DT
mode, Tuner 1's sample stream stalls intermittently (verified
empirically — 2 `readStream TIMEOUT` events in 30 minutes on T1, zero
on T2 same window, same RSPduo, same process).  When T1 stalls,
rtl-airband doesn't exit — it just keeps trying, and the channel
pipeline sits silent while systemd happily reports "active".

ST mode is reliable but uses one tuner only — wasting the other half
of an RSPduo.  Buying a two-tuner SDR and running it as a one-tuner
SDR is paying for hardware we don't use.

MA/SL mode (Master/Slave) is the SDRplay-blessed way to run both
tuners.  The SoapySDR driver enforces that the Master and Slave each
run in their own process; the sdrplay daemon coordinates their shared
clock.  This is exactly how disco-sweep uses these same RSPduos —
`disco-sweep@A-T1.service` (Master) and `@A-T2.service` (Slave) — and
it's been rock-solid for sweep work.

So: replace one rtl-airband process running both bands in DT mode with
**two** rtl-airband processes — Master on Tuner 1 (airband), Slave on
Tuner 2 (ground VHF + UHF retunes) — each fully independent, each with
its own config, its own icecast mount, its own restart cycle.  One
band's wedge no longer takes down the other.

## What changes

### Filesystem layout (after)

```
/usr/local/etc/airband-profiles/
    rtl_airband_hp3_favorites_airband.conf     # one device, MA mode, Tuner 1, airband chans only
    rtl_airband_hp3_favorites_ground.conf      # one device, SL mode, Tuner 2, ground chans only
    rtl_airband_bandscan_marine.conf           # one device, SL mode, Tuner 2 (ground-band)
    rtl_airband_bandscan_airband.conf          # one device, MA mode, Tuner 1 (airband-band)
    ...                                        # (all profiles already migrated 2026-05-26)
    rtl_airband_none_airband.conf              # placeholder (no device block)
    rtl_airband_none_ground.conf               # placeholder

/usr/local/etc/
    rtl_airband.conf            -> [symlink to active airband profile]
    rtl_airband_ground.conf     -> [symlink to active ground profile]
    # rtl_airband_combined.conf -> GONE.  Replaced by:
    rtl_airband_airband_runtime.conf   # standalone, generated, what rtl-airband-airband.service reads
    rtl_airband_ground_runtime.conf    # standalone, generated, what rtl-airband-ground.service reads

/etc/systemd/system/
    rtl-airband.service              -> renamed to rtl-airband-airband.service  (Tuner 1 / MA)
    rtl-airband-ground.service       NEW (Tuner 2 / SL)
    # existing aliases: rtl-airband.service stays as a Wants= aggregate for back-compat

/etc/icecast2/icecast.xml
    # existing /ANALOG.mp3 stays (airband stream)
    # NEW /ANALOG_GROUND.mp3 mount block, same auth + fallback chain
```

### Profile structure change

Today every profile file declares BOTH airband and ground device blocks
(legacy from the dual-RTL-SDR-dongle world).  In the new world each
profile file declares ONE device block — the band determined by its
`mode=` token (MA → airband Tuner 1, SL → ground Tuner 2).

The build script (`build-service-configs.py`, replacing
`build-combined-config.py`) emits two complete standalone rtl-airband
configs, one per service.  No more cross-profile combining.

### Build script signature change

```
# OLD
build_combined_config(airband_path, ground_path, mixer_name, ...) -> str

# NEW
build_service_config(profile_path, service: "airband"|"ground",
                     mixer_name, icecast_mount, bitrate_kbps, ...) -> str
```

Each invocation reads exactly one profile, emits exactly one rtl-airband
config with one device block + that band's icecast output block.

### Systemd

`rtl-airband.service` becomes `rtl-airband-airband.service` with these
edits:

- `ExecStartPre=/usr/bin/python3 /home/ubuntu/scannerproject/scripts/build-service-configs.py --service airband`
- `ExecStart=/bin/bash /home/ubuntu/scannerproject/scripts/rtl-airband-filter.sh /usr/local/etc/rtl_airband_airband_runtime.conf`

New `rtl-airband-ground.service` is a near-duplicate but `--service ground`
and the `_ground_runtime.conf` path.

Order matters: the Master (airband, MA mode) MUST start before the Slave
(ground, SL mode).  The Slave's open will fail if the Master isn't
already running.  Express this via systemd `After=rtl-airband-airband.service`
+ `Requires=` on the ground unit.

Both share `After=icecast2.service` + `Requires=icecast2.service` for
their libshout outputs.

### Icecast

Add `/ANALOG_GROUND.mp3` mount in icecast.xml mirroring `/ANALOG.mp3` —
same auth, same `<fallback-mount>` chain to `/keepalive-ground.mp3`,
same mp3 bitrate.

The existing `keepalive-analog.mp3` source becomes per-band:
`keepalive-airband.mp3` + `keepalive-ground.mp3`.  Or simpler: leave
one shared `keepalive-analog.mp3` and use it as fallback for both real
mounts (icecast allows this).

### Backend (Python)

#### ui/config.py

```python
# OLD
RTL_AIRBAND_STATS_PATH = "/run/rtl_airband_stats.txt"

# NEW
RTL_AIRBAND_AIRBAND_STATS_PATH = "/run/rtl_airband_airband_stats.txt"
RTL_AIRBAND_GROUND_STATS_PATH = "/run/rtl_airband_ground_stats.txt"
RTL_AIRBAND_STATS_STALE_SEC = 15.0  # unchanged
```

Each rtl-airband instance points at a different `stats_filepath` in its
config so the watchdog can tell them apart.

#### ui/systemd.py

```python
# OLD
restart_rtl(reason) -> (ok, err)

# NEW
restart_rtl_airband(reason) -> (ok, err)   # sequenced recovery for airband service
restart_rtl_ground(reason) -> (ok, err)    # sequenced recovery for ground service
```

Same gentle-then-escalate pattern (proven in commits 6ec94dc + 4dd819c).
Each function:

1. Stops only its own unit
2. Probes only its own sample-flow signal
3. On escalation, bounces sdrplay daemon — but coordinates with **both**
   peer services (the other rtl-airband + OP25) before doing so, because
   a daemon bounce affects all clients.

The OP25 coordination already shipped in 4dd819c.  We extend it: when
`restart_rtl_airband` escalates, it also stops the ground service before
the daemon bounce, then restarts both in order
(airband first as Master, ground second as Slave).

#### ui/sample_flow.py

Stays the same shape (`rtl_airband_sample_flow_state(path, threshold)`),
but now called twice per `/api/status` poll — once per service.

#### ui/config_validator.py

`validate_combined_config_text` (the text it validates is now a
single-device service config, not a combined-two-device config).  The
existing tuner-collision and capacity checks still apply but at a
different layer: cross-service validation that the two configs together
declare exactly one MA+one SL on the same serial.  Wraps in a
`validate_dual_service_configs(airband_text, ground_text)` helper.

#### ui/handlers.py

`/api/status` payload gains:

- `rtl_airband_active`, `rtl_airband_sample_flow_ok`, `rtl_airband_stats_age_sec`
- `rtl_ground_active`,  `rtl_ground_sample_flow_ok`,  `rtl_ground_stats_age_sec`
- `rtl_airband_restart_attempts` / `rtl_ground_restart_attempts`
- `icecast_mount_analog_alive` (airband — keeping name for back-compat)
- `icecast_mount_analog_ground_alive` (NEW)

The legacy `rtl_active` keeps existing — it's the OR of both services'
truth signals so existing UI code that just wants "is the analog stack
up" gets a single bool.

#### ui/actions.py

`action_set_profile(profile_id, target)` already routes by `target` =
"airband" or "ground".  In the new world, each target switches its own
profile + rebuilds only its own service config + restart only its own
unit.  The validator gate runs against the new generated config.  The
rollback path stays the same.

### Frontend (sb3.html)

Two changes:

**1. Player band selector.** A small toggle next to the play button:

```
[ Airband ] [ Ground ] [ Both ]
```

- `Airband` (default): player streams `/ANALOG.mp3`
- `Ground`: player streams `/ANALOG_GROUND.mp3`
- `Both`: simplest implementation is a quick A/B toggle following the
  hit feed — when a hit lands on an airband freq, switch to ANALOG.mp3
  for N seconds, then back to whichever was selected.  Skip for v1 if
  complex; user can manually flip the toggle.

For v1: just Airband + Ground.  "Both" can be a follow-up.

**2. Sitrep heartbeats split.** The current "Combined Scanner" row
becomes two rows: "Airband Scanner" + "Ground Scanner" each with its
own heartbeat dot.  Existing dots (`#sitrep-airband-dot`,
`#sitrep-ground-dot`) already exist — they just need to be wired to
the new `rtl_airband_sample_flow_ok` / `rtl_ground_sample_flow_ok`
signals instead of the shared one.

**3. Restart buttons split.** Existing "Restart Airband" stays as-is
(now talks to `restart_rtl_airband`).  Add a parallel "Restart Ground"
button calling `restart_rtl_ground`.

## Cutover plan (deployment)

Single-cutover steps to flip from the existing one-service-DT to
two-service-MA/SL.  Estimated downtime: ~3 minutes during the actual
restart cycle, plus the time to read this list.

```
# On Micro, ordered:

# 1. Stop everything that holds sdrplay handles
sudo systemctl stop rtl-airband.service scanner-digital-op25.service
sudo systemctl restart sdrplay.service

# 2. Install new code (ui/ + scripts/)
# (Done via rsync from worktree → Micro)

# 3. Install new systemd unit + icecast.xml mount
sudo install -m 644 etc/systemd/system/rtl-airband-airband.service /etc/systemd/system/
sudo install -m 644 etc/systemd/system/rtl-airband-ground.service  /etc/systemd/system/
sudo install -m 644 etc/icecast2/mounts.d/analog-ground.xml /etc/icecast2/mounts.d/
# (icecast.xml main file includes mounts.d/*.xml — verify that include exists)
sudo systemctl daemon-reload
sudo systemctl restart icecast2.service

# 4. Migrate profile files (already migrated in repo, deploy to Micro)
# Each profile becomes single-device-block; backups at .pre-ma-sl-split-20260526

# 5. Rebuild per-service configs from active profile symlinks
sudo -u ubuntu python3 /home/ubuntu/scannerproject/scripts/build-service-configs.py --service airband
sudo -u ubuntu python3 /home/ubuntu/scannerproject/scripts/build-service-configs.py --service ground

# 6. Restart UI to pick up new ui/ code
sudo systemctl restart airband-ui.service

# 7. Start the two analog services in order
sudo systemctl enable --now rtl-airband-airband.service
sleep 12
sudo systemctl enable --now rtl-airband-ground.service
sleep 8
sudo systemctl start scanner-digital-op25.service

# 8. Verify
curl -s http://localhost:5050/api/status | python3 -c '
import json, sys
d = json.load(sys.stdin)
for k in ("rtl_airband_sample_flow_ok", "rtl_ground_sample_flow_ok",
          "icecast_mount_analog_alive", "icecast_mount_analog_ground_alive"):
    print(f"{k}: {d.get(k)}")
'
```

Rollback (if MA/SL also fails for some reason):

```
sudo systemctl disable --now rtl-airband-airband.service rtl-airband-ground.service
sudo systemctl enable --now rtl-airband.service
# Restore old combined config build path:
sudo cp /home/ubuntu/scannerproject/scripts/build-combined-config.py.pre-ma-sl-split-20260526 \
       /home/ubuntu/scannerproject/scripts/build-combined-config.py
# Restore old profile files:
for f in /home/ubuntu/scannerproject/profiles/*.pre-ma-sl-split-20260526; do
  sudo cp "$f" "${f%.pre-ma-sl-split-20260526}"
done
sudo systemctl restart rtl-airband.service
```

## Open questions to revisit during implementation

1. **Both mode in the player** — skipping for v1; revisit when we have data on usage patterns
2. **Disco mixer (Listen feature)** — currently airband-tile only; stays on the airband service, no change
3. **`build_combined_config.py` callers** — `ui/actions.py` calls `write_combined_config()` in profile_config which calls the script.  Need to track every caller and update.
4. **Marine Channel 16 priority interleave** — that's a UI-side frequency interleaver into existing tile freqs; should "just work" since it's per-band already

## Effort estimate

- Task 2 (profile + build script refactor): ~2 h
- Task 3 (systemd unit + icecast mount): ~1 h
- Task 4 (backend split): ~3 h
- Task 5 (frontend updates): ~1 h
- Task 6 (tests): ~1.5 h
- Task 7 (cutover): ~30 min execution

Total: **~9 hours of focused engineering** + testing.  Spans this session
plus likely a second one.
