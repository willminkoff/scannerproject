# OpenWebRX+ — operations runbook

Day-to-day ops for the OpenWebRX+ Live IQ engine that replaced the custom
waterfall (pilot commit `406ed67`; see `docs/openwebrx-pilot.md` for the why).

| Fact | Value |
|------|-------|
| Container name | `owrxp` |
| Image | `slechev/openwebrxplus` (NOT `luarvique/openwebrx` — see "Update") |
| Port | `:8073` (all interfaces, via docker-proxy) |
| Restart policy | `unless-stopped` (auto-restarts on boot/crash, **not** after a manual `docker stop`) |
| Dongle | RTL-SDR Blog V4 serial `83241970` (opened on-demand per client) |
| Config (repo) | `config/owrx/settings.json` → deployed to `/opt/owrx-docker/var/settings.json` |
| Theme override | `config/owrx/plugins/receiver/{init.js,sb5_override.css}` → `/opt/owrx-docker/plugins/receiver/` |
| Admin login | `admin` / `scannerproject` |
| Health/diag | `/api/owrx/diag` on airband-ui (`:5050`); OWRX's own `/status.json` + `/metrics` on `:8073` |

> Docker on Micro requires root: the `ubuntu` user is **not** in the `docker`
> group, so prefix every `docker …` command below with `sudo`.

## Logs

```bash
sudo docker logs owrxp            # full log since container start
sudo docker logs -f owrxp         # follow live
sudo docker logs --tail 100 owrxp # last 100 lines
```

## Restart cleanly

```bash
sudo docker restart owrxp
```

Use this to pick up an edited `config/owrx/settings.json` or a changed theme
override (`config/owrx/plugins/receiver/*`). The theme files are static assets
served from the bind-mounted plugins dir, so a hard browser reload of the iframe
also picks them up — `docker restart` just guarantees a clean state.

## Swap which dongle OWRX drives

1. Find the target serial (non-disruptive):
   ```bash
   for f in /sys/bus/usb/devices/*/serial; do cat "$f"; echo; done | grep -E '^[0-9]{8}$' | sort -u
   ```
2. Edit the repo config and redeploy it to the container's volume:
   ```bash
   # edit config/owrx/settings.json → sdrs.rtlsdr-owrx.device = "<new-serial>"
   sudo cp config/owrx/settings.json /opt/owrx-docker/var/settings.json
   sudo chown 103:104 /opt/owrx-docker/var/settings.json   # openwebrx uid:gid in image
   sudo docker restart owrxp
   ```
3. Confirm: `curl -s http://localhost:5050/api/owrx/diag | python3 -m json.tool`
   (`sdr_serial` reflects the repo config; `sdr_name` comes from OWRX itself).

> Make sure the new serial isn't already owned by another service (rtl-airband,
> op25, vfo). `83241970` and the spare `70613472` are the safe waterfall-side
> dongles; see the SDR device map in project memory.

## Update OpenWebRX+

The running image is **`slechev/openwebrxplus`** (the deployment doc's earlier
`luarvique/openwebrx` reference is stale — `docker inspect owrxp` is the truth).
A bare `docker restart` does **not** adopt a newly pulled image; you must
recreate the container. The bind-mounted volumes (config, plugins) and creds are
preserved across the recreate:

```bash
sudo docker pull slechev/openwebrxplus
sudo docker stop owrxp && sudo docker rm owrxp
# re-run with the exact args from docs/openwebrx-pilot.md "Install" block
sudo docker logs --tail 50 owrxp        # confirm it came up clean
```

Pin a known-good tag instead of `latest` if an update regresses.

## Admin UI

OWRX serves its own settings UI at `http://<box>:8073/settings` (gear button in
the top bar, or the "Settings" button). Log in with the admin creds above.
Change the admin password there, or via the `OPENWEBRX_ADMIN_PASSWORD` env on the
container. The receiver name/location/avatar are also set here (the running
container still shows OWRX's stock `Budapest, Hungary` placeholder location —
cosmetic only; the iframe hides the avatar + description band via the theme
override).

## Health / diagnostics

```bash
# airband-ui's enriched view (profile, dongle, center freq, listeners, reachability)
curl -s http://localhost:5050/api/owrx/diag | python3 -m json.tool

# OWRX's own status + connected-client gauge
curl -s http://localhost:8073/status.json | python3 -m json.tool
curl -s http://localhost:8073/metrics | grep openwebrx_users

# container health + uptime (needs sudo)
sudo docker inspect owrxp --format '{{.State.Health.Status}} | up since {{.State.StartedAt}}'

# full regression check
./scripts/smoke_owrx_pilot.sh
```

The `/sb5` heartbeat carries a `Live IQ (OpenWebRX+)` row (enriched with profile
name + listener count). It is **warn-not-bad** when OWRX is down — the Live IQ
pane is auxiliary and must never flip the whole dashboard badge red.

## Revert to the old waterfall + VFO system

> ⚠️ The shorthand `unmask && start && docker stop` does **not** work as-is. The
> pilot (a) masked `scanner-waterfall` with a `/dev/null` symlink **and** moved
> the real unit to `…service.owrx-pilot-bak`, and (b) archived `scripts/waterfall.py`
> out to `archive/`. Unmasking alone leaves no unit file, and even if started the
> `ExecStart` would point at a missing script. Use the full sequence below
> (verified against the live unit paths on Micro; `scanner-vfo`/`/VFO.mp3` are
> untouched throughout — they kept running the whole pilot).

```bash
# 1. free dongle 83241970 (manual stop is not auto-restarted by unless-stopped)
sudo docker stop owrxp

# 2. restore the waterfall script that drives the unit's ExecStart
cp ~/scannerproject/archive/waterfall.py ~/scannerproject/scripts/waterfall.py

# 3. unmask (removes the /dev/null symlink) and restore the real unit file
sudo systemctl unmask scanner-waterfall.service
sudo mv /etc/systemd/system/scanner-waterfall.service.owrx-pilot-bak \
        /etc/systemd/system/scanner-waterfall.service
sudo systemctl daemon-reload

# 4. bring it back
sudo systemctl enable --now scanner-waterfall.service
systemctl is-active scanner-waterfall.service   # expect: active
```

To return to OWRX afterwards: re-mask + re-archive (reverse of the above) and
`sudo docker start owrxp`.
