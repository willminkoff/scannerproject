# Travel mode — iPhone-driven SB3 ZIP push

When Will travels, his iPhone pushes its current location to SB3 over the
tailnet. SB3 updates `HPState.zip/lat/lon` so the scan pool follows him.
Bobby and NSW broadcast stay Nashville-anchored — they do not consume this
endpoint.

> **TL;DR for Will:** Toggle the Travel Mode button to **ON** in the SB3 UI
> before traveling. Toggle it back to **OFF** when home. Travel Mode is purely
> a gate over the push endpoint — the toggle does NOT reset the ZIP. When you
> get home, use the sidecar Location screen to set your ZIP back manually.

## Tailnet-only by design

The push endpoint is **unauthenticated**. It is safe only because the SB3
UI listens on a tailnet-only interface — there is no Tailscale Funnel, no
public reverse proxy, no port-forward to :5050. The iPhone reaches it via
the Tailscale iOS app, which is the access control.

> **Do not expose port 5050 publicly without re-adding auth.** Enabling
> Funnel, an nginx/Caddy reverse proxy, an ngrok tunnel, or a router port
> forward to :5050 would let anyone on the internet move the scanner's ZIP.
> The shared-secret auth layer at commit `61864b5` is the pattern to revive
> if public exposure is ever needed.

## Architecture

```
iPhone (Shortcut, on arrival / every 30 min, over Tailscale)
  └─ POST http://<sb3-host>.<tailnet>.ts.net:5050/api/hp/location/push
     body: {"zip": "10001", "lat": 40.7128, "lon": -74.0060, "source": "ios_shortcut"}
     │
     ▼
SB3 UI
  └─ /api/hp/location/push
     ├─ Gate: reject 409 if HPState.travel_mode_enabled is False
     ├─ Validate ZIP (5 digits) + lat/lon ranges
     ├─ Mutate ONLY HPState.zip / .lat / .lon
     ├─ HPState.save() + favorites_runtime_sync
     └─ Append receipt to HP_LOCATION_PUSH_LOG_PATH (accepted/rejected both logged)

SB3 UI Travel Mode button (header)
  └─ POST /api/hp/travel_mode/toggle  { "enabled": true | false }
     ├─ Mutate ONLY HPState.travel_mode_enabled
     └─ Never touches zip/lat/lon or anything else. The toggle is a pure
        gate; manual sidecar ZIP entry remains the way to set baseline.
```

The push endpoint never modifies `use_location`, `strict_location`,
`range_miles`, `enabled_service_tags`, `favorites`, or any other
user-controlled field. The toggle endpoint is even stricter — it only
flips its own flag and leaves every other field, including ZIP, alone.
Both invariants are enforced by `tests/test_travel_mode_push.py`.

## UI control: Travel Mode button

Visible in the SB3 header next to the DISCO button. Two visual states:

- **OFF** (default, neutral chip): pushes return 409, ZIP stays at whatever
  it currently is. Scanner keeps working wherever it was last pointed.
- **ON** (bright amber, hard to miss): pushes are accepted and mutate
  `HPState.zip/lat/lon`. Last-push relative time and source render below
  the button when a push has landed in the last 24 hours.

The toggle confirms before flipping. It does NOT reset the ZIP in either
direction — turning OFF just stops accepting new pushes. When you get home
and want to scan local frequencies again, set the ZIP via the sidecar
Location screen (the same way you'd change it any other time).

## iPhone Shortcut setup (one-time)

Open the Shortcuts app on iPhone and build a new Shortcut:

1. **Get Current Location** (Location → Get Current Location).
2. **Get Postal Code from Location** (Location → Get Component of Address →
   Postal Code, from the previous step). Apple Maps reverse-geocodes the
   coordinates and returns the local postal code.
3. **Get Latitude of Location** (Location → Get Details of Location → Latitude).
4. **Get Longitude of Location** (Location → Get Details of Location → Longitude).
5. **Text** action — build the JSON body. Use Magic Variables for the postal
   code, latitude, and longitude:

   ```
   {"zip": "<postal-code>", "lat": <latitude>, "lon": <longitude>, "source": "ios_shortcut"}
   ```

   Make sure the ZIP is quoted as a string and lat/lon are NOT quoted.

6. **Get contents of URL** action:
   - URL: `http://<sb3-host>.<tailnet>.ts.net:5050/api/hp/location/push`
     (use the SB3's tailnet hostname — the iPhone must be on the tailnet via
     the Tailscale iOS app for this to reach SB3)
   - Method: `POST`
   - Headers: `Content-Type: application/json`
   - Request Body: `File` → use the Text from step 5
7. **Show Notification** action — show the result of the previous step so a
   failure is visible.

### Automation trigger

Open the **Automation** tab in Shortcuts → "+" → New Automation → choose one:

- **When I arrive** (recommended) — fires only when location actually changes,
  no battery drain at home.
- **Time of day**, every 30 minutes — simpler but pings constantly.

Set the automation to **Run Immediately** so it doesn't prompt every time.

### Test the Shortcut once manually

1. Toggle Travel Mode **ON** in the SB3 UI.
2. Run the Shortcut from the Shortcuts app on iPhone with Tailscale active.
3. Confirm:
   - Notification shows a 200 response with the new ZIP.
   - SB3 UI shows the pushed ZIP and "Last push N min ago from ios_shortcut".
   - `tail` the receipt JSONL (`admin/logs/travel_mode_push.jsonl`) on the Micro for the structured record.
4. Toggle Travel Mode **OFF**. ZIP stays as-is.
5. To restore home: open the sidecar Location screen and set ZIP to 37221.

When OFF, running the Shortcut returns 409 and the UI shows "Last push REJECTED".

## Restoring home ZIP

The Travel Mode toggle does not reset the ZIP. To go back to home (37221):

- **Primary path:** open the sidecar Location screen in the SB3 UI and enter
  your home ZIP. Same flow you'd use to change ZIP at any other time.
- **Emergency / scripted path:** `scripts/reset-home-zip.sh` calls the local
  `/api/hp/state` endpoint with `HOME_ZIP=37221` (and resolves lat/lon).
  See that script's header for details. Marked emergency-only because the
  sidecar is the normal UX.

## Notes

- Disco (port 8092) does not consume this endpoint. ULS / band-plan logic is
  national, not ZIP-driven.
- Bobby + NSW broadcast read their own ZIP from elsewhere (different boxes);
  this endpoint cannot affect them.
- Home defaults (`HOME_ZIP=37221`, `HOME_LAT=36.0662`, `HOME_LON=-86.9639`)
  are used by `scripts/reset-home-zip.sh` and are configurable via env vars
  on the airband-ui service.
