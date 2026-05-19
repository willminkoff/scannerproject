# Travel mode — iPhone-driven SB3 ZIP push

When Will travels, his iPhone pushes its current location to SB3 over the
tailnet. SB3 updates `HPState.zip/lat/lon` so the scan pool follows him.
Bobby and NSW broadcast stay Nashville-anchored — they do not consume this
endpoint.

## Tailnet-only by design

The endpoint is **unauthenticated**. It is safe only because the SB3 UI
listens on a tailnet-only interface — there is no Tailscale Funnel, no
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
     ├─ Validate ZIP (5 digits) + lat/lon ranges
     ├─ Mutate ONLY HPState.zip / .lat / .lon
     ├─ HPState.save() + favorites_runtime_sync
     └─ Append receipt to HP_LOCATION_PUSH_LOG_PATH
```

The endpoint never modifies `use_location`, `strict_location`, `range_miles`,
`enabled_service_tags`, `favorites`, or any other user-controlled field. That
isolation is enforced by `tests/test_travel_mode_push.py`.

## iPhone Shortcut setup

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

Run it from the Shortcuts app on iPhone with Tailscale active and confirm:

- Notification shows a 200 response with the new ZIP.
- `tail -f /run/airband_ui_travel_mode.jsonl` on the Micro shows the receipt.

## Panic reset

If a push went wrong (Shortcut sent the wrong ZIP, or you want to force home
while debugging), SSH to the Micro and run:

```
./scripts/reset-home-zip.sh
```

Default home ZIP is `37221`. Override with `HOME_ZIP=NNNNN ./scripts/reset-home-zip.sh`.

## Notes

- Disco (port 8092) does not consume this endpoint. ULS / band-plan logic is
  national, not ZIP-driven.
- Bobby + NSW broadcast read their own ZIP from elsewhere (different boxes);
  this endpoint cannot affect them.
