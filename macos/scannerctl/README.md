# scannerctl — thin mobile-first web UI

The macOS replacement for airband-ui's **user-facing** role: the quick controls Will
reaches for on his phone. Deep config lives in the native SDRangel/SDRTrunk apps;
this is the glanceable panel. Complements **Path B** (conversational control via Claude).

> **Status: skeleton.** Flask routes + client wiring are real; it renders and the
> endpoints call the clients. But it can't be meaningfully run until SDRangel
> (REST :8091) + SDRTrunk are up on macOS — `/api/status` needs live backends.
> Real iteration happens against running instances.

## Layout
- `app.py` — Flask app: `/`, `/api/status` (analog+digital), `/api/scan/<start|stop>`, `/api/squelch`, `/api/digital/restart`.
- `templates/index.html` — single-column on phone, 2-col ≥680px; light/dark (follows OS, toggle button); polls `/api/status` every 5s.
- `static/style.css` — mobile-first, safe-area insets, big touch targets.
- `requirements.txt` — Flask + requests.

## Run (once macOS + backends are up)
```
cd macos/scannerctl
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
SCANNERCTL_PORT=5050 SDRANGEL_REST=http://127.0.0.1:8091 .venv/bin/python app.py
# then http://<mac-tailscale>:5050 from the phone
```
Auto-start via `macos/launchd/com.scannerproject.scannerctl.plist`.

## Design notes / asymmetry
- **Analog (SDRangel):** real-time control via REST — scan start/stop, squelch slider work live.
- **Digital (SDRTrunk):** no runtime API → the UI can show decode activity (log scrape) and **reload the playlist** (restart), but not tweak a channel live. Keep digital interactions coarse.
- Verify SDRangel field names against the live Swagger before trusting the squelch/scan writes (see `../clients/`).
