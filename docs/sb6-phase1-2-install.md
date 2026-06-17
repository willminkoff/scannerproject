# SB6 Phase 1 + 2 — chirp self-instrumentation + JSON-load hard-fail

**Scope:** Phase 1 adds a Prometheus `/metrics` endpoint to each chirp daemon.
Phase 2 makes a broken JSON config a hard-fail (no more silent fallback to
defaults) and makes the dataclass the single source of truth for every default.
No RF/DSP/audio chain change — the demod path is byte-for-byte unchanged when the
config loads cleanly.

**Target host:** the Micro — `100.67.20.40` (Tailscale), hostname `micro`,
repo at `/home/ubuntu/scannerproject`.

**Source of truth:** `reference_sb6_architecture_plan.md` §1 (Phase 1/2),
`reference_scanner_dual_rspduo_architecture.md` § Recovery items B + open bug 1.

**Depends on:** Phase 0 (Prometheus + Grafana + node/icecast exporters) already
deployed.

---

## What changes

| Component | File | Effect |
|---|---|---|
| chirp metrics exporter | `chirp/metrics.py` (new) | stdlib-only in-process Prometheus endpoint (no `prometheus_client` dep) |
| chirp daemon | `chirp/daemon.py` | starts `/metrics`; hard-fails on broken config; dataclass = sole default source |
| Prometheus scrape | `monitoring/prometheus.yml` | new `chirp` job: airband `:9101`, ground `:9102` |
| Alert rule | `monitoring/alerts.yml` | `ChirpConfigLoadFailed` (observe-only; no NTFY) |
| Tests | `chirp/tests/test_metrics.py`, `chirp/tests/test_phase2_config_hardfail.py` | new |

### Metrics exposed (per daemon, `daemon="airband"|"ground"`)

| Metric | Type | Notes |
|---|---|---|
| `chirp_config_load_status` | gauge 0/1 | **the voice-as-noise canary** — 1 = clean load |
| `chirp_config_path` | gauge 1 | info-style; label carries the resolved path |
| `chirp_flowgraph_alive_seconds_total` | counter | resets on restart (the real restart signal) |
| `chirp_daemon_restart_total` | counter | process-local (weak); reason label |
| `chirp_cmd_bus_request_seconds` | histogram | dispatch latency by command |
| `chirp_audio_bytes_published_total` | counter | icecast bytes by mount |

### Env-var controls (rollback / tuning)

| Var | Default | Effect |
|---|---|---|
| `CHIRP_METRICS_ENABLED` | `1` | `0` → no `/metrics`, zero behavior change (Phase 1 rollback) |
| `CHIRP_METRICS_PORT` | `9101` airband / `9102` ground | override scrape port |
| `CHIRP_METRICS_BIND` | `127.0.0.1` | bind addr (loopback; Prometheus is local) |
| `CHIRP_CONFIG_FAIL_GRACE_S` | `20` | seconds to hold `config_load_status=0` before exit, so Prometheus scrapes it |
| `CHIRP_CONFIG_REQUIRED` | `0` | `1` → a *missing* config file is also a hard-fail (set in prod systemd) |

Phase 2 has no kill-switch by design — the hard-fail *is* the fix. To revert,
`git revert` the Phase 2 commit (the old silent-fallback returns, but Phase 1
telemetry still catches it).

---

## Deploy

```bash
# 1. On the Micro: pull the branch
ssh ubuntu@100.67.20.40
cd /home/ubuntu/scannerproject
git fetch origin && git checkout sb6-phase1-2-chirp-config-hardfail

# 2. Run the chirp test suite (needs GNU Radio, present on the Micro)
python3 -m pytest chirp/tests/test_metrics.py chirp/tests/test_phase2_config_hardfail.py -q

# 3. Deploy Prometheus config + rules, validate, reload
sudo cp monitoring/prometheus.yml /etc/prometheus/prometheus.yml
sudo cp monitoring/alerts.yml /etc/prometheus/rules/alerts.yml
promtool check config /etc/prometheus/prometheus.yml
promtool check rules /etc/prometheus/rules/alerts.yml
curl -X POST http://localhost:9090/-/reload   # or: sudo systemctl reload prometheus

# 4. (prod) Make a missing config a hard-fail too — add to a systemd drop-in:
#    sudo systemctl edit gr-demod@.service
#    [Service]
#    Environment="CHIRP_CONFIG_REQUIRED=1"

# 5. Restart the chirp daemons (one at a time to limit blast radius)
sudo systemctl restart gr-demod@airband
sudo systemctl restart gr-demod@ground
```

> ⚠️ Avoid 5+ in-session chirp restarts — the sdrplay daemon wedge accumulates
> (see North Star § The wedge). If a restart hangs, use
> `scripts/recover-sdrplay.sh`.

---

## Verify (done-gates)

```bash
# Phase 1: both daemons expose metrics, config loaded clean
curl -s localhost:9101/metrics | grep chirp_config_load_status   # => ...{daemon="airband"} 1
curl -s localhost:9102/metrics | grep chirp_config_load_status   # => ...{daemon="ground"} 1

# In Prometheus: both series readable
#   chirp_config_load_status{daemon="airband"}  -> 1
#   chirp_config_load_status{daemon="ground"}   -> 1
# In Grafana: add a stat/timeseries panel on the same query.
```

**Phase 1 alert gate** — break the config on purpose and confirm the alert fires
within ~60s, then restore:

```bash
cp chirp/config/airband.json /tmp/airband.json.bak
printf '%s' "$(cat chirp/config/airband.json | sed 's/}$/,}/')" > chirp/config/airband.json  # add trailing comma
sudo systemctl restart gr-demod@airband
# journalctl shows: "CONFIG LOAD FAILED — refusing to start: invalid JSON ..."
# Within ~60s in Prometheus: ALERTS{alertname="ChirpConfigLoadFailed"} is firing
#   (config_load_status==0 during the grace window, then up==0 after exit)
cp /tmp/airband.json.bak chirp/config/airband.json   # restore
sudo systemctl restart gr-demod@airband              # alert clears
```

**Phase 2 audio gate (needs Will + the Micro):** with `airband.json` loaded
clean (`chirp_config_load_status==1`), listen to **BNA Approach West (119.35)**
and confirm clean ATC voice matching the Uniden BC125AT on the same antenna feed.
This is the gate that the 2026-06-14 investigation could not close.

---

## Rollback

- **Phase 1:** `CHIRP_METRICS_ENABLED=0` (drop-in) + restart → no `/metrics`,
  no behavior change. Or `git revert` the Phase 1 commit.
- **Phase 2:** `git revert` the Phase 2 commit → silent-fallback returns, but
  Phase 1 telemetry still flags the next occurrence.
- **Prometheus:** restore the previous `prometheus.yml` / `alerts.yml` and reload.
