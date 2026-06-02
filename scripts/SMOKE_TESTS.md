# Smoke tests

Operator-run reliability checks. These are **not** part of CI — several of them
bounce live backends and drop audio, so they are run by hand when nobody is
relying on uninterrupted listening.

## `smoke_favorite_switch.py` — favorite-switch reliability

Exercises the full favorite-switch path (rtl-airband + op25 reconfigure) and
reports green/red per check: rtl freqs updated, op25 systems updated, no service
failed, hits flow on a known-active frequency, then switches back to the
original favorite.

> ⚠️ **Switching favorites bounces rtl-airband and op25 — audio drops for a few
> seconds.** The script never auto-executes: by default it runs read-only, and
> `--execute` still requires typing a confirmation phrase at the prompt.

```bash
# read-only (safe): shows current favorite + the plan, makes no changes
python3 scripts/smoke_favorite_switch.py

# full run (bounces backends — prompts for confirmation first)
python3 scripts/smoke_favorite_switch.py --execute
```

Useful env overrides: `SMOKE_KNOWN_ACTIVE_FREQ` (default `172.8120`, Ground
Control — set to a freq known active at the current site), `SMOKE_UI_BASE`,
`SMOKE_RECONFIGURE_WAIT_SEC`, `SMOKE_HITS_WAIT_SEC`.

**Run this when ready to verify favorite-switch reliability** — i.e. during a
maintenance window, not while someone is actively listening.

## `smoke_owrx_pilot.sh` — OpenWebRX+ Live IQ regression check

Verifies the OpenWebRX+ pilot (commit `406ed67`) did not regress the surrounding
stack. **Read-only and non-disruptive** — it only probes HTTP endpoints, systemd
state, and `/sys`; it never bounces a service or grabs a dongle, so unlike the
favorite-switch test it is safe to run **any time, even while people are
listening**. Exits non-zero on the first hard failure.

```bash
./scripts/smoke_owrx_pilot.sh
```

Checks: OWRX serving on `:8073` (+ `status.json`); container health
(`docker inspect owrxp`); all four icecast mounts (`/ANALOG.mp3`,
`/ANALOG_GROUND.mp3`, `/DIGITAL.mp3`, `/VFO.mp3`) publishing **and** flowing data;
core services active (`airband-ui`, both `rtl-airband-*`, both
`scanner-digital-op25*`, `scanner-vfo`); `scanner-waterfall` is *not* running
(intentionally masked); `/api/heartbeat` returns 200 with no stale
"waterfall service" false-positive and a `Live IQ (OpenWebRX+)` row; and dongle
`83241970` is present and assigned to OWRX.

> **Docker health is a soft check.** The `ubuntu` user isn't in the `docker`
> group, so the container-health step is **SKIPPED** unless Docker is reachable
> (HTTP 200 already proves OWRX is serving). To include it, pre-authorize sudo
> (`sudo -v`) before running, or run the whole script under `sudo`.

Useful env overrides: `OWRX_HOST`/`OWRX_PORT` (default `localhost`/`8073`),
`ICECAST_PORT` (`8000`), `UI_PORT` (`5050`), `OWRX_SERIAL` (`83241970`),
`OWRX_CONTAINER` (`owrxp`), `MOUNT_DATA_TIMEOUT` (`5` s).
