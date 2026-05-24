# DC — Aviation + Law Enforcement Travel Favorites

Saved favorites set for trips to the DC metro area. **Not loaded into
HPState** — this is a standalone file you import when in DC, replacing
the active `custom_favorites` list, then restore your Western VA set
(or any backup) when you come home.

- File: `favorites_sets/dc_aviation_le.json`
- Total entries: **35** (23 aviation + 12 LE control channels)
- Active set name (`favorites_name`): `"DC — Aviation + LE Travel"`

## What's in it

### Aviation (23 entries, all VHF AM voice — receivable in the clear)

| Facility | Channels |
|---|---|
| Ronald Reagan National (DCA) | Tower N 119.10, Tower S 132.65, Ground 121.70, ATIS 132.05, Clearance 134.05, Helicopter Routes 121.95 |
| Dulles International (IAD) | Tower 120.10, Ground 121.90, ATIS 134.85, Clearance 121.45 |
| Baltimore/Washington (BWI) | Tower 119.40, Ground 121.90, ATIS 127.80, Clearance 118.20 |
| Potomac TRACON | DCA Final 125.65, IAD Final 128.70, BWI 134.35, Mt Vernon 124.70 |
| Washington ARTCC (ZDC) | Casanova low 134.15, Linden low 135.40, Brooke 124.65 |
| Common | Guard (emergency) 121.50, HMX-1 / USCG Helo Common 130.30 |

### Law-enforcement control channels (12 entries — see schema note below)

| System | Frequencies (MHz) | Notes |
|---|---|---|
| DC OpenSky (DC-DTRS) — legacy UHF CCs | 484.6125, 485.4625, 485.6125, 486.2125 | Legacy UHF T-Band control channels. HPDB now lists the active DC-DTRS system at 769–860 MHz (see MPD trunked entries below); these UHF CCs may carry no traffic if DC has fully migrated. Left in for activity-detection completeness. |
| US Park Police | 855.4875 | P25 |
| Prince George's County PD | 853.1875, 854.6125, 855.6125 | P25 control channels |
| Fairfax County PD | 854.7625, 855.4875 | P25 control channels |
| Arlington County PD | 858.4625, 858.7125 | P25 control channels |

### MPD Dispatch — trunked entries (`kind: "trunked"`, added later)

These ride the live DC-DTRS P25 Phase 2 system (HPDB `trunk_id=7508`,
`system_name: "District of Columbia"`). Each entry carries all 29 of the
system's frequencies (769–860 MHz, pulled from `trunk_freqs` joined via
`trunk_sites WHERE trunk_id=7508`) in `control_channels` — the scanner /
op25 picks whichever control channel is live.

Audio is **encrypted**, so what you get is **talkgroup-activity monitoring**
(when MPD is busy, on which district) — not voice.

| Talkgroup ID | Alpha tag | Department |
|---:|---|---|
| 11025 | MPD Dispatch 1D | Metropolitan Police Department — 1st District (downtown / Capitol) |
| 11031 | MPD Dispatch 4D | Metropolitan Police Department — 4th District (upper NW / Petworth) |
| 11321 | MPD1 (citywide)  | Metropolitan Police Department — Citywide / Event Channel |

Three entries seeded; to add more districts (2D/3D/5D/6D/7D, Tac, Ops A/B, Secure variants, etc.) edit `custom_favorites` in the SB3 sidecar — TGIDs are in HPDB `talkgroups WHERE tgroup_id=18232` (the "Police" trunk group under trunk_id=7508), 66 entries total.

### Mentioned but not in the file (no specific frequencies)

- **USCP** (US Capitol Police) — encrypted P25 800 MHz; no public control-channel list to add. If you find specific freqs while in town, add them manually via the SB3 sidecar.
- **Montgomery County PD** — fully encrypted; no useful audio even if you tune the control channel. Listed for awareness, not added.

## Schema choice — why control channels are `kind: "conventional"` not `"trunked"`

The HPState `custom_favorites` validator
(`ui/hp_state.py:_coerce_custom_favorites`, around line 145) requires every
`kind: "trunked"` entry to carry a digit `talkgroup` — entries without a
talkgroup are silently dropped. We don't have specific talkgroups for these
systems and the intent is "monitor any activity on the control channel,"
which the scanner does fine when the control channel is added as a
conventional frequency. The audio is encrypted P25, so what you get is a
presence indicator (the scanner stops on the channel when it's modulated)
plus the synchronization buzz of P25 control traffic.

If, while in DC, you collect specific talkgroup IDs you want to follow
(via a P25 decoder), upgrade those entries to `kind: "trunked"` in the
SB3 sidecar — keep this file as the seed set.

## Field reference (per `ui/hp_state.py`)

Every entry has:

| field | type | example |
|---|---|---|
| `id` | str | `"dc-aviation-01"` (free-form unique-within-set) |
| `kind` | `"conventional"` \| `"trunked"` | `"conventional"` |
| `system_id` | int | `0` (custom favorites bypass HPDB; `0` means no HPDB linkage) |
| `system_key` | str | `"DC-aviation:Ronald Reagan…"` (free-form group key) |
| `system_name` | str | `"Ronald Reagan Washington National Airport (DCA)"` |
| `department_name` | str | `"DCA - Aircraft (VHF AM)"` |
| `alpha_tag` | str | `"Tower N"` (short label shown in scanner UI) |
| `service_tag` | int | `15` (Aviation) or `2` (Law Enforcement) |
| `talkgroup` | str | `""` for conventional |
| `control_channels` | list[float] | `[]` for conventional |
| `frequency` | float | `119.10` (MHz) |

`service_tag` values used here (matching the rest of the live HPState's
custom_favorites distribution): **15** Aviation, **2** Law Enforcement.

## Activation when you arrive in DC

> 🛑 **Importing this file REPLACES the active `custom_favorites` list.**
> Back up your current Western VA favorites first.

The endpoint is `POST /api/hp/state` on the Micro
(`micro.tail508e50.ts.net:5050`). Same surface the SB3 sidecar uses; no
auth, tailnet-only.

### 1) Back up your current favorites (do this first)

```bash
# Pull the current state and grab just the custom_favorites + name
curl -sS http://micro.tail508e50.ts.net:5050/api/hp/state \
  | python3 -c 'import sys,json; d=json.load(sys.stdin)["state"]; \
      out={"favorites_name":d["favorites_name"], \
           "custom_favorites":d["custom_favorites"]}; \
      print(json.dumps(out, indent=2))' \
  > ~/Documents/scannerproject/favorites_sets/backup_western_va_$(date +%Y%m%d_%H%M).json
```

### 2) Import the DC set

```bash
curl -sS -X POST http://micro.tail508e50.ts.net:5050/api/hp/state \
  -H 'Content-Type: application/json' \
  -d @/Users/willminkoff/Documents/scannerproject/favorites_sets/dc_aviation_le.json \
  | python3 -m json.tool | head -30
```

Expected: `200 OK`, `favorites_runtime_sync.changed: true`, and the
analog/digital frequency counts in the response update to reflect the new
list. The combined-config restart fires automatically — no extra
`systemctl` needed.

Then in the SB3 UI, the sidecar's favorites panel should show **35**
custom favorites under the name **DC — Aviation + LE Travel**.

### 3) Restore your home favorites when you get back

```bash
curl -sS -X POST http://micro.tail508e50.ts.net:5050/api/hp/state \
  -H 'Content-Type: application/json' \
  -d @~/Documents/scannerproject/favorites_sets/backup_western_va_<datestamp>.json
```

## Verifying the load

```bash
curl -sS http://micro.tail508e50.ts.net:5050/api/hp/state \
  | python3 -c 'import sys,json; d=json.load(sys.stdin)["state"]; \
      cf=d["custom_favorites"]; \
      print("favorites_name:", d["favorites_name"]); \
      print("count:", len(cf)); \
      from collections import Counter; \
      print("service_tags:", Counter(c["service_tag"] for c in cf))'
```

Expected after a successful DC load:
- `favorites_name: DC — Aviation + LE Travel`
- `count: 35`
- `service_tags: Counter({15: 23, 2: 12})`

## Notes on coverage

- **DC-area sweep range.** Once HPState's lat/lon is set to a DC-area
  ZIP (either via the SB3 Travel Mode button or the sidecar location
  picker), the `range_miles` setting controls how many of these
  facilities are in physical receive range. ZIP 20001 (DC central) plus
  ~25 miles covers DCA, IAD, BWI, and all the LE control channels.
- **The aviation entries don't depend on location** — they're explicit
  freq favorites that the scanner adds to the active scan pool regardless
  of ZIP. They just need a tuner that can hear them.
- **VHF AM on UHF-capable dongles** — DCA/IAD/BWI traffic is 118-137
  MHz airband. Make sure an airband-capable tuner profile is selected
  before importing.

## File locations

- JSON (this set): `/Users/willminkoff/Documents/scannerproject/favorites_sets/dc_aviation_le.json`
- Doc (this file): `/Users/willminkoff/Documents/scannerproject/favorites_sets/dc_aviation_le.md`
- Backups (your home set, created in step 1): same directory,
  `backup_western_va_<YYYYMMDD_HHMM>.json`
