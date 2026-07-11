# Philadelphia trip scan config (Kater St, South Philly / Bella Vista)

Built 2026-07 for a Neptune (M1) weekend trip. Two deliverables:

- **Digital:** `etc/mac/sdrtrunk/philadelphia-p25.xml` — SDRTrunk playlist, PPD/PFD P25.
- **Analog:** `etc/mac/sdrangel/scan-philadelphia.csv` — SKYWARN + severe-weather amateur.

## Digital — Philadelphia P25 (SDRTrunk)
- **System:** "Philadelphia" trunked (RadioReference sid 7141) — **P25 Phase II simulcast**, 800 MHz.
- **Sysid `3B2`, WACN `BEE00`** (informational — SDRTrunk auto-detects these from the control channel; they don't need to be set).
- **Modulation `CQPSK`** — it's a **simulcast** system (like Nashville MTRTRS); C4FM won't sync.
- **Control channels in the playlist** = the two 800 MHz simulcast zones (Zone 1 site 003 + Zone 2 site 004), 8 CCs spanning 853.3125–853.8375 MHz. SDRTrunk scans them and locks whichever is receivable at Kater St. All 8 fit inside one RSPduo tuner window.
- **NOT included:** the 700 MHz sites (005 Broad St subway zone ~774 MHz, 007 ~769 MHz) — different tuner window, secondary coverage. Add a second channel if the 800 MHz zones don't lock in South Philly.
- **Talkgroups:** one catch-all alias **"All P25 Voice"** (`talkgroupRange` 1–65535, protocol `APCO25`) carries a `broadcastChannel` id so *all* decoded voice routes to the phone stream (below). It also labels calls `[All P25 Voice]` in the event log. Encrypted talkgroups (e.g. TG 9007 shows `ENCRYPTION:DES OFB`) decode metadata but produce no audio — expected. Add named per-TG aliases later to identify PPD districts / PFD companies. (South Philly ≈ PPD 1st/3rd/17th; PFD dispatch + local engine/ladder companies.)

### Phone audio — SDRTrunk → icecast (`neptune-digital.mp3`)
- The playlist carries a `<stream type="icecastHTTPConfiguration">` broadcaster pushing MP3 (16 kbps) to the **existing Neptune icecast** on `127.0.0.1:8000`, mount **`/neptune-digital.mp3`**. Separate mount from the analog `neptune.mp3` (SDRangel) — two streams, VLC picks either. Phone URL over Tailscale: `http://100.102.3.20:8000/neptune-digital.mp3`.
- **Password:** the committed playlist has `password="REPLACE_WITH_ICECAST_SOURCE_PASSWORD"`. The live `~/SDRTrunk/playlist/default.xml` on Neptune uses the real value from `~/scannerproject/etc/mac/icecast-neptune-credentials.env` (same source password icecast validates). Substitute it when deploying; never commit the real value.
- SDRTrunk streaming is **per-alias** (audio only flows for talkgroups matched by an alias whose alias list = the channel's `alias_list_name`, here `Philadelphia P25`, and which carries a `broadcastChannel` id). The catch-all range alias is what makes *all* voice stream.
- Apply changes with a **graceful** SDRTrunk stop (SIGTERM, never SIGKILL — dirty-releases the RSPduo → reboot) and wait ~25 s for the apiService to release the tuner before relaunching, or it comes up `Discovered [0]`.

## Analog — SKYWARN / severe weather (SDRangel)
Source: **NWS Mount Holly (PHI) SKYWARN page** (authoritative for this CWA). Enabled by default = the three Philadelphia County SKYWARN nets + 2m simplex + the Philmont 147.030 Will named. Regional Bears/Complex repeaters are included but **disabled** (verified frequencies, but reach from South Philly is uncertain — enable if needed).

| Freq | PL | Role | Verified |
|---|---|---|---|
| 147.360 | 131.8 | Philadelphia Co SKYWARN **primary** | ✅ NWS |
| 444.050 | 131.8 | Philadelphia Co SKYWARN **secondary** | ✅ NWS |
| 224.500 | 131.8 | Philadelphia Co SKYWARN (1.25m) | ✅ NWS |
| 146.520 | — | 2m national simplex | ✅ |
| 147.030 | 131.8? | W3PMR Philmont (per Will) | ⚠️ PL unverified; NWS lists 147.360 as primary |
| 147.300 / 444.200 / 442.950 / 447.125 / 145.230 | 131.8 / 77.0 | regional Bears/Complex SKYWARN | ✅ freq (NWS); reach uncertain → disabled |

**Squelch/gain:** the −55 TH/Sq values are placeholders. On Neptune, tune gain + squelch the same way we did on Venus (fixed RTL gain, squelch ~5–10 dB above the measured noise floor) — the numbers here are a starting point, not dialed-in.

## ⚠️ Fit-to-Neptune (blockers)
1. **No RSPduo on Neptune** — last check it had only 2 RTLs (`61108285`, `83241970`). **SDRTrunk digital won't work until an RSPduo is physically plugged in.** The analog CSV runs on an RTL and is fine.
2. **Neptune is macOS Tahoe 26.5.2**, where we saw **`system_profiler` report no USB SDRs** and RSPduo enumeration was uncertain. If a plugged-in RSPduo doesn't appear via `ioreg`/`rtl_test`/SDRTrunk's SDRplay discovery, digital is dead regardless of this playlist — that's a hardware/OS issue to solve first (reseat, try ports, apiService kickstart).
3. The FreqScanner CSV is imported via the SDRangel **GUI** (7.25.1 can't set the freq list over REST); the `Channel` column (`R1:2`) points at the demod the scanner drives — adjust to Neptune's deviceset/channel layout on import.

Sources: NWS Mount Holly SKYWARN (weather.gov/phi/skywarn); RadioReference sid 7141 (system/control channels).
