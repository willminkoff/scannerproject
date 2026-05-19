# Travel mode — iPhone-driven SB3 ZIP push

When Will travels, his iPhone pushes its current location to SB3 over public
HTTPS. SB3 updates `HPState.zip/lat/lon` so the scan pool follows him.
Bobby and NSW broadcast stay Nashville-anchored — they do not consume this
endpoint.

## Architecture

```
iPhone (Shortcut, on arrival / every 30 min)
  └─ POST https://<host>.<tailnet>.ts.net/api/hp/location/push
     headers: X-Travel-Secret: <HP_LOCATION_PUSH_SECRET>
     body:    {"zip": "10001", "lat": 40.7128, "lon": -74.0060, "source": "ios_shortcut"}
     │
     ▼
SB3 UI (Tailscale Funnel terminates TLS)
  └─ /api/hp/location/push
     ├─ hmac.compare_digest secret check
     ├─ Validate ZIP (5 digits) + lat/lon ranges
     ├─ Mutate ONLY HPState.zip / .lat / .lon
     ├─ HPState.save() + favorites_runtime_sync
     └─ Append receipt to HP_LOCATION_PUSH_LOG_PATH
```

The endpoint never modifies `use_location`, `strict_location`, `range_miles`,
`enabled_service_tags`, `favorites`, or any other user-controlled field. That
isolation is enforced by `tests/test_travel_mode_push.py`.

## Server-side setup (on the Micro)

1. Set the shared secret in the systemd unit environment:

   ```
   HP_LOCATION_PUSH_SECRET=<long random string>
   ```

   Generate one with `openssl rand -hex 32`. If unset, the endpoint returns
   404 and the UI logs a warning on startup.

2. Restart the UI service:

   ```
   sudo systemctl restart airband-ui
   ```

3. Enable Funnel (first run only; subsequent reboots persist):

   ```
   ./scripts/enable-tailscale-funnel.sh
   ```

   This may fail the first time with "Funnel is not enabled on your tailnet".
   Go to the Tailscale admin → DNS → HTTPS Certificates + Funnel, grant the
   `funnel` attribute to the SB3 device, then re-run the script.

4. Verify the public URL responds:

   ```
   curl -sS https://<host>.<tailnet>.ts.net/api/status
   ```

## iPhone Shortcut setup

Build a new Shortcut in the iOS Shortcuts app. Steps:

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
   - URL: `https://<host>.<tailnet>.ts.net/api/hp/location/push`
   - Method: `POST`
   - Headers:
     - `Content-Type: application/json`
     - `X-Travel-Secret: <paste the secret value here>`
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

Run it from the Shortcuts app on iPhone and confirm:

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

- The endpoint is exempt from CSRF / origin checks because it requires the
  shared secret. Treat the secret as you would an API token: rotate by
  changing the env var, restarting the UI, and updating the Shortcut.
- Disco (port 8092) does not consume this endpoint. ULS / band-plan logic is
  national, not ZIP-driven.
- Bobby + NSW broadcast read their own ZIP from elsewhere (different boxes);
  this endpoint cannot affect them.
