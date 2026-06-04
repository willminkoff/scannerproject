# airband-ui

`airband-ui` is the operator dashboard for the SB5 scanner — the Python HTTP
service that drives rtl-airband (and, behind a feature flag, the chirp
GNU Radio daemons).  The user-facing single-page app lives at `ui/sb5.html`;
the HTTP API + plumbing is in `ui/handlers.py`.

## SB5 chirp feature flag

Phase 4c introduced a single env-var feature flag:

```
SB5_USE_GR_DEMOD=true   # route operator endpoints through chirp daemons
SB5_USE_GR_DEMOD=false  # rtl-airband path (default)
```

When the flag is on, four operator endpoints proxy to chirp daemons
(`127.0.0.1:7400` for airband, `127.0.0.1:7401` for ground) via UDP
JSON instead of writing rtl-airband config + invoking
`safe_restart_rtl_airband`:

| Endpoint                              | Where the branch lives                              |
| ---                                   | ---                                                  |
| `/api/airband/squelch_preset`          | `ui/handlers.py` → `ui/chirp_adapter.apply_squelch_preset_via_chirp` |
| `/api/airband/squelch_auto`            | `ui/handlers.py` → `ui/chirp_adapter.set_squelch_auto_via_chirp` |
| `/api/hp/state/activate`               | `ui/handlers.py` → `ui/chirp_adapter.activate_favorite_via_chirp` |
| `/api/sitrep/action reset_radios`     | `ui/handlers.py:_run_sitrep_action` → `ui/chirp_adapter.reset_radios_via_chirp` |

Plus:

- `ui/squelch_tracker.py` — when the flag is on, reads noise floor
  from `chirp.get_status` and applies via `chirp.set_squelch` instead
  of writing `rtl_airband.conf` + restarting the unit.
- `ui/handlers.py:_compute_heartbeat_payload` — when the flag is on,
  adds `chirp-airband`, `chirp-ground`, and per-daemon icecast-state
  rows to the heartbeat evidence list.

Every chirp call is logged one-line-per-call to
`~/.cache/airband-ui/chirp_client.jsonl` for operator forensics.

### Flipping the flag

```
sudo systemctl edit airband-ui.service
# Add under [Service]:
#   Environment=SB5_USE_GR_DEMOD=true
sudo systemctl restart airband-ui.service
```

### Rollback (≤30 s)

```
sudo systemctl edit airband-ui.service     # remove the SB5_USE_GR_DEMOD line
sudo systemctl restart airband-ui.service  # back to rtl-airband path
```

The chirp path is purely additive — production rtl-airband config is
NOT touched while the flag is on.  Reverting just turns the chirp path
back off; nothing to "undo" on disk.

## Architecture (relevant modules)

```
ui/
  handlers.py            HTTP handler — all operator endpoints. The
                         four chirp-aware endpoints branch ONCE at the
                         top via `_chirp_use_gr_demod()` and delegate
                         to `chirp_adapter`.
  chirp_client.py        Sync UDP JSON client for the chirp daemon
                         command bus.  ChirpClient(host, port) with
                         singletons for the airband and ground daemons.
                         Dormant when the flag is off (no threads,
                         no socket activity at import time).
  chirp_adapter.py       Chirp-on implementations of the four endpoint
                         handlers.  Each helper returns the same shape
                         as the legacy implementation so the HTTP
                         response surface is byte-identical from the
                         dashboard's POV.
  squelch_tracker.py     Continuous noise-floor tracker (579 lines).
                         When the flag is on, _run_cycle_for_band
                         branches ONCE at the top into
                         _run_cycle_for_band_via_chirp, which reads
                         get_status and applies set_squelch instead
                         of writing rtl_airband.conf + restarting.
  squelch_preset.py      Preset margin definitions (sensitive=3,
                         balanced=6, selective=12 dB).  Shared between
                         both back-ends — the chirp adapter imports
                         these directly to keep the preset logic in
                         ONE place.
  managed_analog_controls.py
                         Per-band override persistence.  Shared.
  app.py                 HTTP server entry point.

chirp/scripts/migrate_state.py
                         One-time script that reads data/hp_state.json
                         + profiles/managed_analog_controls.json and
                         writes equivalent chirp.state.ChirpState JSON
                         files to /var/lib/chirp/{airband,ground}.state.json.
                         Used during Phase 4d cutover.  Default mode
                         is --dry-run; --apply opts in to writes.
                         Idempotent.
```

## Tests

```
python3 -m pytest ui/tests/ -q
# 52 passed (as of Phase 4c)
```

Subdirectory layout:

- `ui/tests/test_chirp_client.py` — UDP JSON client (36 tests).
- `ui/tests/test_tracker_chirp_swap.py` — tracker source/sink swap (9 tests).
- `ui/tests/test_heartbeat_chirp.py` — heartbeat-row awareness (7 tests).

Plus chirp-side end-to-end integration:

- `chirp/tests/test_phase4c.py` — real mock UDP daemon + adapter helpers (8 tests).
- `chirp/tests/test_migrate_state.py` — state migration unit + CLI (21 tests).
