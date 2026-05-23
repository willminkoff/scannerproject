# Travel Mode — Owntracks iPhone adapter (PR #35)

## What it does

Adds a `POST /api/hp/owntracks` endpoint that accepts the
[Owntracks](https://owntracks.org/) iOS app's native JSON payload and
routes it through the existing Travel Mode push pipeline. Will runs
Owntracks in background mode; the app pushes GPS over the tailnet to the
Micro at significant-location-change cadence; the scanner follows.

Same pipeline as the existing iOS Shortcut (`/api/hp/location/push`):
mutates only `HPState.zip` / `lat` / `lon`, gated on
`travel_mode_enabled`, writes a receipt to the push log. The new
endpoint adds an offline lat/lon → nearest-US-ZIP reverse lookup so the
adapter can synthesize a ZIP from Owntracks' raw coordinates.

### Why Owntracks (vs. the Shortcut)

| | Shortcut | Owntracks |
|---|---|---|
| Battery cost | high — every push wakes the app | ~1 %/day (uses iOS significant-location-change) |
| Reliability | breaks when Shortcut updates | maintained iOS app |
| Setup once | per phone | per phone |
| Privacy | tailnet-only | tailnet-only (data only leaves the phone to the Micro) |

## iOS setup

1. Install [**Owntracks**](https://apps.apple.com/app/owntracks/id692424691) from the App Store.
2. Open the app → tap the gear / **Settings** icon (top right).
3. Tap **Mode** → choose **HTTP**.
4. Open the **Server** / **Reporting** section:
   - **URL**: `http://micro.tail508e50.ts.net:5050/api/hp/owntracks`
   - **DeviceID**: anything (e.g. `iphone`)
   - **TrackerID**: short string (e.g. `wp` for "Will phone")
   - Leave Authentication blank — the endpoint is tailnet-trusted, **no
     password**.
5. Back on the main screen, tap the **publish** button (arrow icon, top
   left) to confirm a manual push works.
6. **iOS Settings → Owntracks → Location → Always** so the app can
   publish in the background.
7. **iOS Settings → Owntracks → Background App Refresh: ON**.

Tailscale must be running on the phone for the `tail508e50.ts.net`
hostname to resolve — the endpoint is not reachable off-tailnet by design.

## Verifying without the iPhone

From any tailnet-connected machine, post a sample payload by hand:

```bash
curl -sS -X POST http://micro.tail508e50.ts.net:5050/api/hp/owntracks \
  -H 'Content-Type: application/json' \
  -d '{
    "_type":"location","tid":"wp",
    "lat":36.81203,"lon":-81.57894,"tst":1716480000,
    "acc":10,"alt":280,"vel":0,"cog":0,"batt":78
  }'
```

Expected: `200 OK` with `{"ok": true, "zip": "24354", …}`. Then:

```bash
curl -sS http://micro.tail508e50.ts.net:5050/api/status | \
  python3 -c 'import sys,json;d=json.load(sys.stdin); \
   print({k:d[k] for k in d if k.startswith("owntracks_")})'
```

shows the new counter fields incrementing.

The push receipt is appended to the same JSONL log as Shortcut pushes
(`HP_LOCATION_PUSH_LOG_PATH`), tagged `"source": "owntracks"` and
carrying the iPhone-side telemetry (`owntracks_tid`, `owntracks_acc_m`,
`owntracks_vel_kmh`, `owntracks_battery_pct`).

## /api/status fields

| Field | Meaning |
|---|---|
| `owntracks_invocations_total` | every POST hit, including ignored `_type`s |
| `owntracks_pushes_accepted_total` | location pushes that mutated HPState |
| `owntracks_pushes_rejected_total` | malformed / 409 / out-of-coverage |
| `owntracks_last_push_ts` | epoch of last accepted push |
| `owntracks_last_lat`, `owntracks_last_lon` | last accepted coords |
| `owntracks_last_battery_pct` | last reported iPhone battery |

## Message-type handling

Owntracks publishes several `_type`s; the adapter routes only `location`:

| `_type` | Behavior |
|---|---|
| `location` | parsed, reverse-geocoded, pushed through Travel Mode |
| `lwt`, `transition`, `waypoint`, anything else | 200 OK, logged, no action |
| (no `_type` field) | 200 OK, ignored |

Errors:

| Status | Reason |
|---|---|
| 400 | location with missing / non-numeric / out-of-range lat/lon |
| 400 | reverse-lookup empty (outside the US ZCTA dataset) |
| 409 | `travel_mode_enabled = false` — rejection logged with `accepted=false` |
| 500 | HPState load/save failure |

## Security posture

**TAILNET-ONLY-TRUSTED. No auth on the endpoint.** Same posture as
`/api/hp/location/push`: safe only because the UI binds to a Tailscale
interface. If `tailscale funnel`, an nginx/Caddy proxy, ngrok, or any port
forward on :5050 is ever introduced, re-add the shared-secret check
(`hmac.compare_digest` pattern from commit `61864b5`) before exposing.

## Reverse-lookup data

`ui/data/us_zip_lat_lon.json` — 33,144 US ZIP centroids from the **Census
2020 ZCTA Gazetteer** (public domain, ~894 KB). Loaded at first lookup
and cached in memory; brute-force scan runs in ~3 ms. Override path via
`HP_US_ZIP_LAT_LON_PATH` for tests / alternate deployments.

## Rollback

Revert this PR. The travel-push pipeline still works via the iOS Shortcut
at `/api/hp/location/push` exactly as before — the Owntracks adapter is
purely additive.
