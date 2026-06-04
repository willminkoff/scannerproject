# SB5 Analog Demod — Architecture & Design

**Date:** 2026-06-03
**Project:** Replace rtl-airband with a Python-controlled GNU Radio flowgraph. Module name: `chirp`. Branch: `gr-demod/airband` (until Phase 4 parity).
**Status:** Operator-reviewed 2026-06-03. Section 13 open questions resolved (see below). Phase 0 spike validated all 3 critical assumptions. Phase 1 ready to start.
**Predecessors:** `SDR_DEMOD_PROJECT_PLAN.md`, `SDR_DEMOD_DECISION_2026-06-03.md`.

This document is the contract for what we are building before code is written. Decisions are made; open questions are explicit; the flowgraph, the wire protocol, the systemd shape, and the cutover plan are all pinned down enough that Phase 1 starts from a known target.

---

## 1. System context

**Problem.** rtl-airband is a non-restartable monolith. Every operator action (squelch, gain, preset) requires SIGINT → process death → SDRplay re-acquire (5–15 s wedge). The AUTO tracker compounds it; the race occasionally corrupts SDRplay shared-memory semaphores and wedges every subsequent open. Phase 3 (auto gain) can't ship until the restart cost is gone.

`chirp` solves four things rtl-airband structurally cannot: **hot reload** (long-running flowgraph mutates via `set_center_freq()`, `set_threshold()`, `set_gain()`); **wedge avoidance** (we never SIGKILL the SDRplay process tree to apply config); **observability** (structured logs + per-channel metrics + `/healthz` + async events replace journalctl-grep); **runtime control** (JSON command bus decouples dashboard from DSP — dashboard speaks intent, daemon owns hardware).

**In scope.** Airband AM scanning (118–137 MHz); ground NFM scanning; per-channel squelch + hit detection; Icecast publish to `/ANALOG.mp3` and `/ANALOG_GROUND.mp3`; runtime control from airband-ui.

**Out of scope.** P25 / trunked decoding (op25 stays). disco-coordinator VFO sweep (scanner-vfo stays). Waterfall (stays). Digital decoding.

---

## 2. Process model

**Decision: one daemon per band.** Two units, `gr-demod@airband.service` and `gr-demod@ground.service`, each owning its own SoapySDR source. Justification: **blast radius** — a crash on one band must not silence the other; the bands have different SDRs (RSPduo master/slave or two RTL dongles); a fault on one is almost always a hardware fault on that device. **Restart asymmetry** — bouncing one band leaves the other live, halving operator-visible silence. **op25 parity** — op25 already runs as a separate process; mirroring that structure reuses the same systemd/lifecycle/supervision idioms. **Not embedded in airband-ui** — embedding would couple Flask request lifecycle to GR top-block lifecycle; a 500 in the dashboard must never tear down the demod, and a demod stall must never block the dashboard's heartbeat.

**systemd shape.**

```
gr-demod@.service          (template; %i = airband|ground)
  After=network-online.target icecast2.service
  Requires=icecast2.service
  ExecStart=/usr/bin/python3 -m chirp.daemon --band %i --config /etc/chirp/%i.yaml
  Restart=on-failure
  RestartSec=2
  TimeoutStopSec=5
  KillSignal=SIGTERM
  StartLimitBurst=10
  StartLimitIntervalSec=60
```

The daemon traps SIGTERM, drains the GR flowgraph cleanly (`tb.stop(); tb.wait()`), releases the SDR, and exits 0 within `TimeoutStopSec`. SIGUSR1 triggers structured-log dump. SIGUSR2 is reserved for future use. Hot reloads do NOT use signals — they go over the JSON command bus.

---

## 3. Module structure

New top-level package `chirp/` at repo root:

```
chirp/
  README.md
  pyproject.toml                  # installable; ham2mon code vendored under chirp.dsp
  src/chirp/
    __init__.py
    daemon.py                     # entry point: parse args, build flowgraph, start command server
    flowgraph.py                  # gr.top_block subclass; owns the source + per-channel demods
    source.py                     # SoapySDR / osmosdr source wrapper; centralizes RSPduo config
    channel.py                    # ChannelDemod hier_block — one per active scan freq
    am_demod.py                   # AM hier_block (vendored from ham2mon TunerDemodAM, ported)
    nfm_demod.py                  # NFM hier_block (vendored from ham2mon TunerDemodNBFM)
    squelch_probe.py              # message-port adapter: pwr_squelch_cc → hit events
    audio_sink.py                 # libshout output sink (one mountpoint per daemon)
    hit_log.py                    # JSONL hit writer; mirrors current /run/rtl_airband_last_freq_*.txt
    command_server.py             # UDP JSON listener; dispatches to flowgraph
    command_schema.py             # pydantic-style validators for each command
    state.py                      # in-process state model; persistence to disk on change
    metrics.py                    # structured logging + /metrics text format + healthz dict
    config.py                     # YAML loader for per-band static config
  config/
    airband.yaml                  # band-static config (SDR args, sample rate, audio mountpoint)
    ground.yaml
  systemd/
    gr-demod@.service
  tests/
    test_command_schema.py        # validator tests
    test_state.py                 # persistence round-trips
    test_flowgraph_smoke.py       # file-source IQ → expect N hits at known times
    fixtures/
      airband_iq_sample.cf32      # 4 MHz IQ captured around 121 MHz, 5 s, with a known transmission
```

**Vendoring policy.** Ham2mon hier_blocks live in-tree (`chirp.dsp`, GPL header preserved). No submodule — ham2mon is lightly maintained, the GR 3.10 port may diverge, and we want one repo to bisect. Op25 stays external; we borrow the *pattern* (UDP JSON), not the code.

---

## 4. DSP architecture

**Topology (per band, one daemon).** A single SoapySDR source feeds N parallel per-channel demodulator hier_blocks; each demodulator's output goes to a mixer/adder which feeds one libshout-backed audio sink.

```
                                       ┌─ ChannelDemod(118.4) ─┐
                                       ├─ ChannelDemod(118.6) ─┤
SoapySDR source ─── stream tee ────────┼─ ChannelDemod(119.35)─┼── add_ff ── audio LPF ── shout_sink ─→ /ANALOG.mp3
   (RSPduo master,                     ├─ ChannelDemod(119.45)─┤
    4 MHz, ~127.5 MHz)                 └─ ChannelDemod(...)    ┘
                                                              │
                                       FFT probe ─────────────┘   (spectrum probe for waterfall handoff)
```

**Per-channel pipeline** (mirrors ham2mon `TunerDemodAM`, with NFM swap-in):

```
in ──► freq_xlating_fir_filter_ccc ──► fir_filter_ccc (12.5 kHz LPF) ──►
       pwr_squelch_cc (msg port out) ──► agc3_cc ──► AM | NFM demod ──►
       audio LPF ──► pfb_arb_resampler_fff (→ 16 kHz) ──► (out to adder)
```

For NFM, swap `agc3 → quadrature_demod_cf`; rest unchanged. Output sample rate is 16 kHz mono float; libshout sink resamples/encodes to MP3 at 32 kbps to match the existing mountpoint contract.

**Hot-tunable parameters** (settable while flowgraph runs):

- `freq_xlating_fir_filter_ccc.set_center_freq(offset_hz)` — per-channel tuning relative to source LO.
- `pwr_squelch_cc.set_threshold(dbfs)` — per-channel squelch.
- `agc3_cc.set_reference(level)` — per-channel volume / AM gain.
- `source.set_gain(db)` — master gain (source-level).
- `source.set_center_freq(hz)` — master LO retune (rare; only for ground-band sub-band hops).

**Compile-time parameters** (require flowgraph rebuild → bounce daemon):

- Source sample rate (4 MHz default for airband; 2 MHz for RSPduo slave on ground).
- Max channels per daemon (pre-allocated pool — see channel management below).
- Demod mode at the hier_block level (a `ChannelDemod` is either AM or NFM at construction).

**Channel add / remove without flowgraph rebuild.** Two strategies; we use both:

1. **Pre-allocated pool (primary).** At daemon start we instantiate `MAX_CHANNELS` (default 32) AM `ChannelDemod` instances and wire them to the source + adder. Inactive channels sit with `freq_xlating` at `0` Hz and squelch at `+0` dBFS (gate slammed shut). `add_channel` claims a slot from the free list, calls `set_center_freq` + `set_threshold`. `remove_channel` reverses. **No `connect()` / `disconnect()` calls at runtime.** This is what op25 does for its trunked voice slots and what ham2mon does for its N demods — it's the proven-safe path.

2. **Hot reconnect (escape hatch).** If the operator exceeds `MAX_CHANNELS` we log a warning and reject the add. We do not implement runtime `gr.hier_block2.connect()` reconfiguration in Phase 1/2; it's possible but has historically been fragile (buffer reallocation races). Defer to Phase 5 if ever needed.

**Polyphase channelizer vs. freq_xlating tradeoff.** Start with N parallel `freq_xlating_fir_filter_ccc`. Ham2mon-tested, CPU profile known, per-channel filter shape independent. `pfb_channelizer_ccf` has fixed channel count compiled in and per Phase 0 spike #2 runtime `set_channel_map()` is questionable. Current scan lists top out at ~25 channels; we don't need 64+. v1 is 25 kHz channels only — no EU 8.33 kHz support (deferred). Revisit if scale or spacing requirements change.

**CPU budget (Intel x86).** Micro is Intel x86, **not** ARM — earlier Pi-class CPU concerns do not apply. Phase 0 spike showed plentiful headroom for the per-channel `freq_xlating + LPF + squelch + AGC + AM demod + resampler` chain. The 32-channel pre-allocated pool is conservative; we expect to be able to run it without breaking a sweat. Real numbers land in Phase 1's benchmark, but we are not budget-constrained for the foreseeable channel counts.

---

## 5. Command protocol

**Decision: UDP JSON, loopback only.** Op25 pattern, already understood by the SB5 codebase (`ui/op25_adapter.py`), zero serialization-library footprint, trivial to debug with `nc -u`, supports multiple clients (airband-ui + debug CLI). Unix socket gives stronger access control but loses multi-client. HTTP/Flask adds deps and synchronous request handling fights the GR runtime.

**Wire format.** Newline-delimited JSON. One object per datagram. Daemon binds `127.0.0.1:7400` (airband) and `127.0.0.1:7401` (ground). Max datagram 4 KB.

**Request envelope.**

```json
{ "v": 1, "id": "req-abc-123", "cmd": "<command>", "args": { ... } }
```

`v` is the protocol version. `id` is a client-chosen correlation id (echoed in the reply). `cmd` and `args` are command-specific.

**Commands.**

```json
{ "cmd": "add_channel",   "args": { "id": "ch01", "freq_mhz": 121.025, "mode": "am",
                                     "squelch_dbfs": -68.0, "gain_db": null, "label": "TWR" } }
{ "cmd": "remove_channel","args": { "id": "ch01" } }
{ "cmd": "set_squelch",   "args": { "id": "ch01", "dbfs": -70.0 } }
{ "cmd": "set_gain",      "args": { "id": "ch01", "db": 0.0 } }
{ "cmd": "set_freq",      "args": { "id": "ch01", "mhz": 121.500 } }
{ "cmd": "set_mode",      "args": { "id": "ch01", "mode": "am" } }   // RESERVED — v1 returns ENOTSUP; mode is baked at add_channel
{ "cmd": "set_master_gain","args": { "db": 32.8 } }
{ "cmd": "get_status",    "args": {} }
{ "cmd": "reset",         "args": {} }                                // config-only reset; flowgraph keeps running
{ "cmd": "subscribe",     "args": { "events": ["hit","level","health"] } }
```

`set_mode` is **not** a runtime command. Mode (`am` | `nfm`) is baked at `add_channel` time only. A channel keeps its mode for its lifetime; to switch modes, `remove_channel` then `add_channel` with the new mode. This avoids the slot-reassignment complexity (and the failure mode where the other pool is full) at the cost of one extra round-trip on the rare mode switch. The `set_mode` command in the table above is reserved but returns `ENOTSUP` in v1.

**Response envelope.**

```json
{ "v": 1, "id": "req-abc-123", "ok": true,  "result": { ... } }
{ "v": 1, "id": "req-abc-123", "ok": false, "error": { "code": "EBUSY",
                                                        "message": "channel pool exhausted" } }
```

Error codes: `EBADREQ` (schema), `ENOENT` (unknown channel id), `EBUSY` (pool full), `ERANGE` (value out of bounds), `EHWFAIL` (SDR error), `ENOTSUP` (command not supported in this version — e.g. `set_mode`), `EINTERNAL`.

**Async event stream.** Same UDP socket, daemon-initiated, sent to all subscribers. No correlation id; instead `evt` field.

```json
{ "v": 1, "evt": "hit_start",  "ts": 1748952301.123, "ch": "ch01", "freq_mhz": 121.025, "level_dbfs": -42.1 }
{ "v": 1, "evt": "hit_end",    "ts": 1748952304.987, "ch": "ch01", "duration_s": 3.86, "peak_dbfs": -31.4 }
{ "v": 1, "evt": "level",      "ts": 1748952302.500, "ch": "ch01", "noise_dbfs": -74.3, "peak_dbfs": -33.0 }
{ "v": 1, "evt": "health",     "ts": 1748952305.000, "src_overrun": 0, "channels_active": 8 }
{ "v": 1, "evt": "warn",       "ts": 1748952306.000, "code": "SRC_DROPOUT", "message": "..." }
```

**Versioning.** `v: 1` is the only defined version. Breaking changes bump `v`; daemon advertises supported versions via `get_status` (missing `v` is treated as `v=1`). Adding commands within a version is non-breaking; changing semantics requires a bump. Clients must ignore unknown response fields and unknown event types.

---

## 6. State persistence

**On-disk store.** A single JSON file per daemon: `/var/lib/chirp/<band>.state.json`. Atomic write via `os.replace`. Schema:

```json
{
  "version": 1,
  "updated_ms": 1748952301123,
  "master": { "gain_db": 32.8, "center_freq_hz": 127500000, "samp_rate": 4000000 },
  "channels": [
    { "id": "ch01", "freq_mhz": 121.025, "mode": "am",
      "squelch_dbfs": -68.0, "gain_db": 0.0, "label": "TWR" }
  ]
}
```

**What survives restart.** Channel list, per-channel settings, master gain, master LO. The daemon loads this on boot before opening the SDR, applies it after the flowgraph starts.

**What is recomputed.** Per-channel observed noise floors, hit counters, running level estimates, last-hit timestamps. These are runtime telemetry, not config; recomputing them is correct.

**Migration from existing SB5 state.** The Phase-1-cutover script reads:

- `ui/data/managed_analog_controls.json` → per-band squelch preset + gain → seeds `master.gain_db` and per-channel `squelch_dbfs` via the existing `recommended_managed_controls()` function in `ui/managed_analog_controls.py`.
- The active rtl-airband combined config (`/etc/rtl_airband_combined.conf`) → freq list → seeds `channels`.
- `data/hp_state.json` (HomePatrol favorites pool) → label resolution → fills `channels[].label`.

The migration runs once, idempotent; the source of truth then shifts to `<band>.state.json`. Old files become read-only fallbacks.

---

## 7. Hit detection + audio output

**Hit detection.** `pwr_squelch_cc` exposes a message port that fires on open/close transitions. A Python `msg_handler` per channel translates port events into `hit_start` / `hit_end` events, timestamps them, and appends to the hit log. More reliable than journalctl-grep (current SB5 path via `scripts/rtl-airband-last-hit.sh`) — we get the event at the same source that gated audio.

**Hit log writer.** Port the JSONL format. New writer in `chirp.hit_log` writes `/var/log/chirp/<band>_hits.jsonl` (daily rotation, **30-day retention**; logrotate config ships in `chirp/systemd/`). The `/run/` path is process-state, the `/var/log/` path is durable so 30-day history survives reboots. For backward compatibility we also write the legacy `/run/rtl_airband_last_freq_<band>.txt` so `ui/scanner.py:read_last_hit_airband()` keeps working through cutover.

**Audio output: native `python-shout` via a custom GR sink wrapping libshout.** Reconnect on Icecast drop without subprocess management. Encode + send stays in-process — no pipes, no signal handling between us and the network. Keepalive: during squelch-closed we feed the encoder zeros at the same sample rate; libmp3lame produces valid silent MP3 frames so Icecast listeners see continuous bytes. The `pwr_squelch_ff` gate after the adder controls audibility, not byte flow — preserving the `mount_publishing` heuristic in `ui/sample_flow.py`.

**Mountpoints.** Daemon publishes directly: `gr-demod@airband` → `/ANALOG.mp3`, `gr-demod@ground` → `/ANALOG_GROUND.mp3`. No separate transcoder. Icecast keepalive units become obsolete but stay installed for rollback. During Phase 3 the new daemon writes to `/ANALOG_NEW.mp3` for A/B compare against rtl-airband; **at Phase 4 cutover, chirp overwrites `/ANALOG.mp3` (and `/ANALOG_GROUND.mp3`) directly** — no permanent `/ANALOG_NEW.mp3` URL, so existing bookmarked stream URLs keep working. The `/ANALOG_NEW.mp3` mountpoint is removed once cutover is signed off.

---

## 8. Failure modes + recovery

| Failure | Detection | Action |
|---|---|---|
| `osmocom_source` errors (SDR unplugged) | GR runtime exception | Daemon exits non-zero → systemd `Restart=on-failure` after 2 s. `StartLimitBurst=10/60s` caps crashloops; `/healthz` flips red. |
| Channelizer / source overruns | Buffer underrun counters tick | `warn` event `code=SRC_DROPOUT`, exposed via `/metrics`. No restart — operator-action problem (too many channels for the sample rate). |
| libshout / Icecast drop | `shout.send_raw` returns error | Sink reconnects with 1/2/5/10 s backoff; continues feeding silence locally. `warn` event; `/healthz` → `audio_sink: degraded`. |
| Hier_block raises Python exception | GR runtime catches; flowgraph stops | Daemon dumps traceback, exits 1, systemd restarts. |
| Command server thread dies | Watchdog observes thread death | Exit process; systemd restarts. |
| Invalid command from client | Schema validator rejects | Reply `EBADREQ`; no state mutation; no log spam beyond one DEBUG line. |

**Crash semantics.** Unhandled exception → non-zero exit → systemd restart on backoff. `StartLimitBurst=10/60s` prevents crashloops masquerading as availability. State is unaffected — channels reload from `<band>.state.json` on next boot.

**Observability.** Structured JSON logs to stderr → systemd journal (one line per command, hit, warning). UDP `get_status` returns full snapshot. Optional `/metrics` HTTP endpoint on `127.0.0.1:7500+%i` exposes Prometheus-text counters (`channel_squelch_open_total`, `src_overrun_total`, `shout_reconnect_total`, `audio_bytes_sent_total`, `channels_active`). `GET /healthz` returns 200 + `{"ok": true, "channels_active": 8, "audio_sink": "ok"}` or 503; airband-ui polls it from `/api/heartbeat`.

---

## 9. Integration with existing airband-ui dashboard

**Existing endpoints.** SB5 has ~50 routes in `ui/handlers.py`. The integration surface for `chirp` is small:

| Endpoint | Today (rtl-airband) | After cutover (chirp) |
|---|---|---|
| `POST /api/airband/squelch` | writes controls file, sets `pending_restart` | thin proxy → `set_squelch` per channel; immediate |
| `POST /api/airband/gain` | writes controls file, sets `pending_restart` | thin proxy → `set_master_gain`; immediate |
| `POST /api/airband/squelch_preset` | computes per-channel threshold list, writes to rtl-airband config, queues restart | computes same list, sends `set_squelch` for each channel; no restart |
| `POST /api/airband/squelch_auto` | toggles tracker thread on/off | unchanged — tracker still runs in airband-ui |
| `GET  /api/heartbeat` | journal-grep heuristic | adds `chirp` `/healthz` polled state |
| `GET  /api/sitrep` | aggregates many subsystems | `airband` subsystem reads from chirp, not rtl-airband stats |
| `POST /api/sitrep/action reset_radios` | systemd restart of rtl-airband + sdrplay-daemon recovery | becomes `reset` command to chirp; only escalates to systemd restart if `reset` doesn't take |
| `/run/rtl_airband_*_stats.txt` consumers | rtl-airband writes Prometheus-style file | chirp writes the same file format for compatibility during cutover |

**Squelch tracker — decision: keep in airband-ui.** The tracker is already 579 lines of mature Python with hysteresis, chip-lock coordination, and persistence; moving it into the daemon doubles daemon scope and complicates testing. The daemon exposes the primitives the tracker needs (`level` events, `set_squelch` command); the tracker becomes a *thinner* client, not embedded.

**Heartbeat.** Daemon publishes `/healthz`; airband-ui's `/api/heartbeat` polls it every 5 s and surfaces it alongside op25/disco/vfo health. Same UI widget, new source.

**Operator-visible UI changes.** None, intentionally. The chip click → audible change loop gets faster (sub-second) and the "pending restart" spinner disappears. Everything else looks identical.

---

## 10. Testing strategy

**Unit tests.** Command schema validators (`test_command_schema.py`) cover every error path. State persistence round-trips (`test_state.py`) ensure no migration regressions. Pure-Python — no GR runtime needed.

**Integration tests.** A `FileSource` mode replaces SoapySDR with a `gr.blocks.file_source` reading captured IQ from `tests/fixtures/`. `test_flowgraph_smoke.py` builds a daemon with 4 channels, feeds 5 seconds of canned IQ with one known transmission, and asserts: exactly one `hit_start` + `hit_end`, the right channel id, audio bytes > 0 from the shout sink (mocked).

**Regression fixtures — enshrine the rtl-airband software bugs.** Each known software-side rtl-airband failure mode that bit us in production gets a dedicated test fixture under `chirp/tests/fixtures/` and a test that proves chirp does the right thing instead. Targeted bugs (all in scope):

- **Squelch poison value.** rtl-airband's per-channel stats file occasionally writes garbage noise-floor numbers (e.g. very small or NaN values) that the SB5 squelch presets then consume verbatim, producing nonsense thresholds. Fixture: a stats-file-shaped input with poison values; test: chirp's state loader / squelch logic rejects or clamps them, never produces an out-of-band threshold.
- **SDRplay master/slave wedge on restart.** rtl-airband SIGKILL leaves the SDRplay shared-memory semaphores in a state where the next process can't claim master OR slave; subsequent opens wedge. Fixture: simulated wedged semaphore state; test: chirp's source open path detects the wedge, performs recovery, and surfaces a clean `EHWFAIL` rather than blocking.
- **libshout drop without reconnect.** rtl-airband's libshout integration drops the Icecast connection on transient network/Icecast restarts and never reconnects without a full process restart. Fixture: a mock Icecast that drops the connection mid-stream; test: chirp's audio sink reconnects with backoff and resumes sending bytes within N seconds, no manual restart required.
- **Noise-floor init race.** rtl-airband's per-channel noise-floor estimator initializes from zero and takes seconds to converge; squelch decisions in the first window are wrong. Fixture: a fresh start with the IQ file source; test: chirp either seeds noise floor from a sane prior or gates squelch decisions until convergence, so no spurious `hit_start` events fire in the first second.

**Explicitly out of scope (hardware-side, not enshrineable in software fixtures).** The "USB dongle flap" failure mode where the RTL-SDR or RSPduo physically disconnects/re-enumerates is a hardware/kernel-level event. Chirp inherits whatever resilience SoapySDR + `osmocom_source` provide; we test that we surface a clean error and let systemd restart us, but we do not build a fixture for the hardware flap itself.

**On-hardware tests (Phase 3).** Shadow mode: gr-demod runs alongside rtl-airband on the same SDRplay (RSPduo can present two tuners to two processes if we use master/slave carefully, OR we use a separate RTL dongle for shadow). gr-demod publishes to `/ANALOG_NEW.mp3`; rtl-airband stays on `/ANALOG.mp3`. We tail both hit logs for a 24 h window and confirm hit counts within 10% of each other.

---

## 11. Migration / cutover plan

| Phase | Deliverable | Cutover step | Rollback |
|---|---|---|---|
| 1 | Working one-channel AM daemon. Audio to file. Hot retune via UDP. | None — runs on Micro, no production wiring. | n/a |
| 2 | N-channel scanner. Hot add/remove. Shadow-tested on file-source IQ. | None — still no production wiring. | n/a |
| 3 | Hit detection + Icecast publish to `/ANALOG_NEW.mp3`. systemd unit installed but `disabled`. | Operator manually `start`s it for 24 h shadow window. | `systemctl stop gr-demod@airband`. |
| 4 | Dashboard cutover: feature flag `SB5_USE_GR_DEMOD=1` flips `/api/airband/*` from rtl-airband proxies to chirp proxies. Mountpoint flips: chirp publishes to `/ANALOG.mp3`, rtl-airband stopped. | Flip env var in `/etc/airband-ui.conf`, restart airband-ui. Stop rtl-airband units. | Flip flag back, restart rtl-airband. ≤30 s revert. |
| 5 | rtl-airband uninstalled. Keepalive units removed. | Cleanup PR. | git revert; reinstall from apt. |

**Feature flag.** `SB5_USE_GR_DEMOD=1` (env in `/etc/airband-ui.conf`, read in `ui/config.py`). Default `0` until Phase 4 signoff. When `1`, `ui/airband_restart.py` becomes a no-op and `/api/airband/*` handlers proxy to UDP `127.0.0.1:7400/7401`. When `0`, current path is unchanged.

**Rollback.** Phase 4: `SB5_USE_GR_DEMOD=0` + `systemctl start rtl-airband-{airband,ground}` — ~30 s, no data loss. Phase 5: `apt install rtl-airband` + restore config from git — ~10 min.

---

## 12. Hardware abstraction

**Primary: SDRplay RSPduo master/slave.** `osmosdr.source(args="soapy=0,driver=sdrplay,rspduo_mode=master,antenna=Tuner 1 50ohm")` for airband; `rspduo_mode=slave` for ground. `source.py` centralizes arg-string construction so the rest of `chirp` stays SDR-agnostic. Phase 0 spike #3 must confirm this streams both tuners from a single `gr-osmosdr` process.

**RTL-SDR fallback.** Supported via `args="rtl=0"` / `rtl=1`. Sample rate caps at 2.4 MHz per dongle; sub-band hops, which we already do, cover the gap.

**Other SDRs via SoapySDR.** Wrapper supports it for free. Defer until asked.

---

## 13. Operator decisions (resolved 2026-06-03)

These were the open questions on the original draft; resolutions from operator review on the night of 2026-06-03 below. Body of the document above is patched to match; this section preserves the rationale.

1. **Module name.** → `chirp`. Locked in. Branch is `gr-demod/airband` until Phase 4 parity (kept descriptive for the branch; the Python package is `chirp`). Systemd unit names retain `gr-demod@` for continuity with the branch and existing internal language.
2. **Branch strategy.** → Single long-lived `gr-demod/airband`. Merges to `main` behind feature flag at Phase 4 cutover.
3. **EU airband 8.33 kHz.** → **Out for v1.** 25 kHz channels only. 8.33 kHz support deferred (would need an `am_833` mode with ~6 kHz channel bandwidth). Revisit only if/when we have an EU deployment requirement.
4. **Mountpoint flip vs. parallel.** → **Overwrite `/ANALOG.mp3` directly at cutover.** `/ANALOG_NEW.mp3` exists only during Phase 3 shadow; at Phase 4 chirp takes over the canonical mountpoint and `/ANALOG_NEW.mp3` is removed. Bookmarked stream URLs keep working.
5. **Hit log retention.** → **30 days** on-disk rotation under `/var/log/chirp/`. Daily rotation, 30 files retained. Journal-grep path keeps working through cutover via legacy `/run/rtl_airband_last_freq_*.txt`.
6. **rtl-airband pain to enshrine as tests.** → See Section 10 regression fixtures. Four targeted bugs: squelch poison value, SDRplay master/slave wedge on restart, libshout drop without reconnect, noise-floor init race. The USB dongle physical flap is explicitly out of scope (hardware-side, not enshrineable in a software fixture).
7. **`set_mode` cost.** → **No runtime AM↔NFM flip.** Mode is baked at `add_channel` time. Switching mode = `remove_channel` + `add_channel`. The `set_mode` command is reserved in v1 and returns `ENOTSUP`. Avoids slot-reassignment edge cases (other pool full, mid-flight hit truncation).
8. **CPU budget.** → **Micro is Intel x86, not ARM.** Earlier Pi-class CPU concerns do not apply. Phase 0 spike showed plentiful headroom; the 32-channel pre-allocated pool is conservative. Phase 1 ships a benchmark for the record, but we are not budget-constrained.
