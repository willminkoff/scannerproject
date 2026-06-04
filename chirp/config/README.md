# chirp/config — per-band daemon configuration

Each chirp daemon instance is bound to exactly one band (airband or ground)
at startup. The band selection drives:

  - which JSON config file the daemon reads (`airband.json` or `ground.json`)
  - the UDP command port (7400 / 7401)
  - the on-disk state path (`/var/lib/chirp/<band>.state.json`)
  - the hit log path (`/var/log/chirp/<band>_hits.jsonl`)
  - the default pool demod mode (AM for airband, NFM for ground)

## Selection

The daemon picks its config in this order, highest priority first:

  1. `CHIRP_BAND` env var (`airband` or `ground`). REQUIRED to be set for the
     ground daemon; defaults to `airband` if unset.
  2. The `band` field of the resolved JSON file is used as a label only —
     it does not override the env-selected band.

Once `CHIRP_BAND` is resolved, the daemon reads
`chirp/config/<band>.json` (relative to the package). Missing/corrupt JSON
is logged and falls through to hard-coded defaults; it never aborts boot.

## Schema (JSON)

All keys are optional. Each one is overridable by the matching `CHIRP_*`
env var (env wins over JSON). Unknown keys are ignored (forward-compat).

| Key                     | Type     | Default      | Env override                |
|-------------------------|----------|--------------|-----------------------------|
| `band`                  | string   | "airband"    | `CHIRP_BAND`                |
| `pool_mode`             | string   | "am"/"nfm"   | `CHIRP_POOL_MODE`           |
| `cmd_host`              | string   | "127.0.0.1"  | `CHIRP_CMD_HOST`            |
| `cmd_port`              | int      | 7400/7401    | `CHIRP_CMD_PORT`            |
| `source_samp_rate`      | float    | 1e6          | `CHIRP_SOURCE_SAMP_RATE`    |
| `source`                | string   | (none)       | `CHIRP_SOURCE`              |
| `audio_out`             | string   | (none)       | `CHIRP_AUDIO_OUT`           |
| `audio_rate`            | float    | 16000        | `CHIRP_AUDIO_RATE`          |
| `max_channels`          | int      | 32           | `CHIRP_MAX_CHANNELS`        |
| `event_sink`            | string   | (none)       | `CHIRP_EVENT_SINK`          |
| `log_level`             | string   | "INFO"       | `CHIRP_LOG_LEVEL`           |
| `state_path`            | string   | (none)       | `CHIRP_STATE_PATH`          |
| `hit_log_path`          | string   | (none)       | `CHIRP_HIT_LOG`             |
| `icecast_bitrate_kbps`  | int      | 32           | `CHIRP_ICECAST_BITRATE_KBPS`|
| `icecast_fallback_file` | string   | /tmp/...     | `CHIRP_ICECAST_FALLBACK_FILE` |

`pool_mode` controls which demod chain (AM envelope vs FM discriminator)
is wired into every slot of the channel pool. It is set at daemon startup
and immutable for the life of the process; the per-channel `mode` field in
`add_channel` requests must match the pool's mode or the request is rejected.

## Two-daemon coexistence

Running airband (AM) and ground (NFM) simultaneously on one Micro is the
Phase 4a design point. Each daemon owns:

  - its own UDP listener on 127.0.0.1:7400 (airband) or :7401 (ground)
  - its own state file: `/var/lib/chirp/airband.state.json` /
    `/var/lib/chirp/ground.state.json`
  - its own hit log: `/var/log/chirp/airband_hits.jsonl` /
    `/var/log/chirp/ground_hits.jsonl`
  - its own Icecast mountpoint (e.g. `/CHIRP_TEST.mp3` /
    `/CHIRP_GROUND_TEST.mp3` for tests; production mounts come in Phase 4d)
  - its own icecast fallback file (see `icecast_fallback_file`)

No global/singleton state is shared between daemons — see
`test_phase4a.py::TestTwoDaemonCoexistence`.

## Bad JSON / missing field handling

  - Empty/missing file: log INFO, use built-in defaults.
  - Malformed JSON: log WARN, use built-in defaults (boot succeeds).
  - Unknown `pool_mode`: hard ValueError at startup. Boot fails fast.
  - Unknown band: defaults to "airband"; no validation enforced because the
    band string flows through to filenames where the OS catches typos.
