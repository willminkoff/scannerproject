# SB6 Phase 0 — Telemetry stack install runbook

**Scope:** Observability only. **No** changes to chirp, op25, disco, vfo,
waterfall, audio_leveler, icecast2, or any production service. Four new
services that read and expose metrics; nothing in the RF/audio path is touched.

**Target host:** the Micro — `100.67.20.40` (Tailscale), hostname `micro`.

**Source of truth:** `reference_sb6_architecture_plan.md` §0, §9. North Star and
invariants in `reference_scanner_dual_rspduo_architecture.md`.

---

## What gets installed

| Component | Source | Port | Purpose |
|---|---|---|---|
| Prometheus | apt `prometheus` (Debian/Ubuntu repo) | 9090 | TSDB + scrape + alert rules |
| Grafana | apt `grafana` (packages.grafana.com repo) | 3000 | Dashboards |
| node_exporter | apt `prometheus-node-exporter` | 9100 | Host CPU/mem/disk/net/uptime |
| icecast_exporter | this repo (`monitoring/icecast_exporter.py`) | 9146 | Icecast listener counts + byte rate |

All four bind to localhost except Grafana (3000) which Will reaches over
Tailscale. Prometheus (9090), node_exporter (9100), and icecast_exporter (9146)
are loopback-only — Grafana proxies Prometheus, so nothing else needs to be
exposed. (Tailscale ACLs already gate the LAN; loopback binding is defense in
depth.)

---

## Pre-flight (read-only; run before touching anything)

> **The Micro must be online.** As of the last check it was offline in Tailscale
> (`tailscale status` → `last seen …`). Confirm reachability first:
>
> ```bash
> tailscale status | grep micro
> ping -c2 100.67.20.40
> ssh will@100.67.20.40 true && echo "SSH OK"
> ```

Then capture the baseline:

```bash
ssh will@100.67.20.40 '
  echo "=== identity ==="; whoami; hostname; pwd
  echo "=== repo path ==="; ls -d ~/scannerproject /home/*/scannerproject 2>/dev/null
  echo "=== OS ==="; lsb_release -d; uname -r; uname -m
  echo "=== RAM/disk ==="; free -h | head -2; df -h /
  echo "=== CPU ==="; nproc
  echo "=== already installed? (expect empty) ==="
  dpkg -l | grep -iE "prometheus|grafana" || echo "none"
  ss -ltnp 2>/dev/null | grep -E ":(3000|9090|9100|9146)\b" || echo "ports free"
  echo "=== icecast admin reachable? ==="
  curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/admin/stats || true
'
```

**Two host-specific values to confirm here (do not assume):**

1. **Deploy user + repo path.** `systemd/scanner-vfo.service` in this repo uses
   `User=ubuntu` with the repo at `/home/ubuntu/scannerproject`, but the
   dispatch says the SSH user is `will`. Whatever the pre-flight prints is
   authoritative. Update `User=` and the `ExecStart`/`Documentation` paths in
   `monitoring/systemd/icecast-exporter.service` to match before enabling it.
2. **Icecast admin password.** Read from the live config (not the redacted
   repo example):
   ```bash
   ssh will@100.67.20.40 'sudo grep -E "admin-user|admin-password" /etc/icecast2/icecast.xml'
   ```
   The example shows `<admin-user>source</admin-user>`; confirm the real
   `admin-password`. This goes into `/etc/icecast-exporter.env` (below), never
   into the repo.

**Abort criteria:** if `dpkg -l` shows an existing Prometheus or Grafana, stop
and reconcile with Will before continuing — do not clobber an existing install.

---

## Install steps

> Run after Will greenlights. Each block is independently reversible (see
> Rollback). `$REPO` = the confirmed repo path; `$USER` = the confirmed deploy
> user.

### 1. node_exporter

```bash
sudo apt-get update
sudo apt-get install -y prometheus-node-exporter
sudo systemctl enable --now prometheus-node-exporter
curl -s localhost:9100/metrics | head -3   # expect node_* metrics
```

### 2. Prometheus

```bash
sudo apt-get install -y prometheus
# Deploy our scrape config + alert rules:
sudo install -m644 "$REPO/monitoring/prometheus.yml" /etc/prometheus/prometheus.yml
sudo mkdir -p /etc/prometheus/rules
sudo install -m644 "$REPO/monitoring/alerts.yml" /etc/prometheus/rules/alerts.yml
# 14-day retention via the Debian ARGS file:
echo 'ARGS="--storage.tsdb.retention.time=14d --web.enable-lifecycle"' \
  | sudo tee /etc/default/prometheus
# Validate before (re)starting:
promtool check config /etc/prometheus/prometheus.yml
promtool check rules /etc/prometheus/rules/alerts.yml
sudo systemctl enable --now prometheus
sudo systemctl restart prometheus
# Confirm targets:
curl -s 'localhost:9090/api/v1/targets' | grep -o '"health":"[a-z]*"' | sort | uniq -c
```

### 3. icecast_exporter (this repo)

```bash
# Secret env file (mode 600, NOT in the repo):
sudo tee /etc/icecast-exporter.env >/dev/null <<EOF
ICECAST_ADMIN_USER=source
ICECAST_ADMIN_PASSWORD=<paste real admin-password>
EOF
sudo chmod 600 /etc/icecast-exporter.env

# Unit -- EDIT User= and ExecStart path first to match the pre-flight values:
sudo install -m644 "$REPO/monitoring/systemd/icecast-exporter.service" \
  /etc/systemd/system/icecast-exporter.service
sudo systemctl daemon-reload
sudo systemctl enable --now icecast-exporter
curl -s localhost:9146/metrics | grep -E '^icecast_(up|listener)'   # expect icecast_up 1
```

### 4. Grafana

```bash
sudo apt-get install -y apt-transport-https software-properties-common wget
sudo mkdir -p /etc/apt/keyrings
wget -qO - https://apt.grafana.com/gpg.key | gpg --dearmor \
  | sudo tee /etc/apt/keyrings/grafana.gpg >/dev/null
echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" \
  | sudo tee /etc/apt/sources.list.d/grafana.list
sudo apt-get update
sudo apt-get install -y grafana

# Provisioning: datasource + dashboard provider + dashboard JSON
sudo install -m644 "$REPO/monitoring/grafana/provisioning/datasources/prometheus.yml" \
  /etc/grafana/provisioning/datasources/prometheus.yml
sudo install -m644 "$REPO/monitoring/grafana/provisioning/dashboards/sb6.yml" \
  /etc/grafana/provisioning/dashboards/sb6.yml
sudo mkdir -p /var/lib/grafana/dashboards/sb6
sudo install -m644 "$REPO/monitoring/grafana/dashboards/sb6-host-overview.json" \
  /var/lib/grafana/dashboards/sb6/sb6-host-overview.json
sudo systemctl enable --now grafana-server
```

Then browse **http://100.67.20.40:3000** (default login `admin`/`admin`; change
on first login). The **SB6 Host Overview** dashboard should be in the SB6
folder, pulling live data.

---

## Done gates (verify all four)

1. **All services up + enabled:**
   ```bash
   systemctl is-active prometheus grafana-server prometheus-node-exporter icecast-exporter
   systemctl is-enabled prometheus grafana-server prometheus-node-exporter icecast-exporter
   ```
2. **Grafana dashboard** at `http://100.67.20.40:3000` shows host CPU/mem/disk/
   uptime from Prometheus.
3. **icecast_exporter** exposes per-mount series for the live mounts:
   ```bash
   curl -s localhost:9146/metrics | grep -E 'icecast_listener_(count|byte_rate)'
   # expect mount="/ANALOG.mp3", "/ANALOG_GROUND.mp3", "/DIGITAL.mp3", "/VFO.mp3"
   #   for whichever sources are currently connected
   ```
   (A mount only appears while its source is connected — that's Icecast
   behavior, not a bug. Start the relevant source to see it.)
4. **Alert rule loaded:**
   ```bash
   curl -s localhost:9090/api/v1/rules | grep -o '"name":"Icecast[A-Za-z]*"'
   # expect IcecastConsumerWedge (and IcecastExporterDown)
   ```

---

## The alert + notification destination

Phase 0 ships the alert **rule** (`monitoring/alerts.yml`):

- **IcecastConsumerWedge** — `icecast_listener_byte_rate < 1000 and
  icecast_listener_count > 0` for 2m. Catches the VLC-wedge (consumer connected,
  not draining). 64 kbps ≈ 8000 B/s, so 1000 B/s only trips on a true stall.
- **IcecastExporterDown** — `icecast_up == 0` for 2m, so a dead exporter can't
  silently blind the wedge canary.

**Notification delivery is a Will decision (pending).** The rule fires and is
visible in Prometheus `/alerts` and on the dashboard regardless. To actually
get paged, pick one:

- **Grafana-managed alerting** (no extra package) — add a contact point
  (NTFY / Slack / email) in Grafana and a notification policy. Simplest given
  Grafana is already installed.
- **Alertmanager** (`apt install prometheus-alertmanager`, port 9093) — uncomment
  the `alerting:` block in `prometheus.yml` and configure a receiver.

Wire whichever Will picks; until then the alert is observe-only.

---

## Rollback

Each component is independently removable. Nothing in the RF/audio path is
touched, so there is no production-service rollback to coordinate.

```bash
# Stop/disable any subset:
sudo systemctl disable --now icecast-exporter prometheus grafana-server prometheus-node-exporter

# Full purge (clean — apt packages remove cleanly):
sudo apt-get purge -y prometheus grafana prometheus-node-exporter
sudo rm -f /etc/systemd/system/icecast-exporter.service /etc/icecast-exporter.env
sudo rm -f /etc/apt/sources.list.d/grafana.list /etc/apt/keyrings/grafana.gpg
sudo rm -rf /etc/prometheus/rules /var/lib/grafana/dashboards/sb6
sudo systemctl daemon-reload
```

- **icecast_exporter** is a single stdlib-only Python file + one env file.
  Removing the unit and env file leaves no trace.
- **Prometheus/Grafana/node_exporter** are stock apt packages; `purge` removes
  config and data.
- The Icecast admin password lives only in `/etc/icecast-exporter.env` (mode
  600) on the host and is never committed.

---

## Local verification already done (off-host, before any install)

- `python3 -m py_compile monitoring/icecast_exporter.py` — clean.
- `python3 monitoring/test_icecast_exporter.py` — 7/7 pass, covering byte-rate
  math, wedge detection (0 B/s with a listener), counter-reset clamp,
  disappearing-mount cleanup, the failure path (`icecast_up 0`), and label
  escaping.
- `promtool check config/rules` — to be run **on the host** at install time
  (promtool ships with the Prometheus package).

## Rollout discipline (Will's workflow)

- Plans-first: this runbook is the plan. No host changes until greenlit.
- Env-var config + secret in a 600 env file (no secrets in repo).
- Debug logging: `ICECAST_EXPORTER_DEBUG=1` in the env file for verbose stderr.
- Regression tests committed alongside the exporter.
- Haste makes waste: install order is node_exporter → Prometheus →
  icecast_exporter → Grafana, verifying each before the next.
```
