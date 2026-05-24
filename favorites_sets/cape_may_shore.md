# Cape May Shore — Sea Isle City NJ (08243)

Saved favorites profile for Will's drive to **Sea Isle City NJ**. Already
registered alongside the existing 7 named profiles in HPState as the
**`Cape_May_Shore`** tile in the SB3 favorites grid — tap it to activate
on arrival.

- File: `favorites_sets/cape_may_shore.json`
- Profile id in HPState: `fav-cape-may-shore`
- Total entries: **42** (36 conventional + 6 trunked)
- Active profile when registered: **DC_Aviation_LE** (left untouched)

## What's in it

### Aviation (11 conventional, VHF AM, `service_tag` 15)

| Facility | Channels (MHz) |
|---|---|
| Atlantic City International (KACY) | Tower 124.6 • Ground 121.9 • ATIS 108.6 |
| Atlantic City TRACON | KACY Approach 134.85 |
| Cape May County Airport (KWWD) | CTAF / UNICOM 122.7 |
| Ocean City Municipal (KOXB) | CTAF / UNICOM 122.7 |
| McGuire AFB (KWRI) | Tower 126.05 |
| Washington / NY ARTCC (ZNY) | 124.85, 125.25, 132.45 (low coastal NJ sectors) |
| Common | Guard 121.50 |

### Marine / USCG (3 conventional, `service_tag` 11)

| Channel | Freq | Purpose |
|---|---|---|
| Marine Ch 16 | 156.800 | Distress / hailing — ALWAYS monitor |
| Marine Ch 22A | 157.100 | USCG working channel |
| USCG Sector Delaware Bay | 157.050 | USCG Sector Delaware Bay (working) |

**Not in this profile** because HPDB doesn't carry them as discrete
labeled freqs in the conventional tables:

- **USCG Cape May (Operations)** — Will's list mentioned it; HPDB has no
  dedicated entry. USCG operations typically ride 157.0500 (Ch 21A) or
  the Sector Delaware Bay freq above; or add manually if you find a
  specific one.
- **USCG District 5** — HQ-level coordination; no single dedicated freq.

### NJSP Conventional (6, `service_tag` 29 for OEM, 7 for repeaters)

| Channel | Freq (MHz) | Notes |
|---|---|---|
| NJSP OEM Statewide | 39.760 | Low-VHF NJSP OEM |
| NJSP OEM Central | 39.800 | |
| NJSP OEM North | 39.840 | |
| NJSP OEM South | 39.920 | **Covers Cape May** |
| NJSP Repeater 1 | 852.3125 | NJSP UHF talkaround |
| NJSP Repeater 2 | 852.7625 | |

### Sea Isle City (7 conventional — Will's destination)

| Channel | Freq | Service |
|---|---|---|
| Police Dispatch | 151.0775 | LE Dispatch (2) |
| Fire Ground Ops | 153.8150 | Fire Tac (8) |
| EMS Disp + Public Works | 154.0400 | EMS (1) |
| Fire Dispatch | 154.1300 | Fire Dispatch (3) |
| Beach Patrol Ops | 154.7850 | Lifeguard / Beach (25) |
| EMS Operations | 155.3550 | (25) |
| Police Dispatch/Ops | 155.5650 | LE Dispatch (2) |

### Other shore towns (8 conventional)

| Town | Channels |
|---|---|
| Avalon | Fire/EMS 154.3850 • Beach Patrol 453.9875 |
| Stone Harbor | Fire Dispatch 154.3850 |
| Cape May City | Police Backup 155.7000 • Fire Dispatch 155.8800 |
| Ocean City NJ | Fire Ground 153.8450 • Fire (AC P25 sim) 154.4450 |

### Cape May County Fire/EMS Dispatch (2 conventional)

| Channel | Freq | Service |
|---|---|---|
| CMC Fire Dispatch Ch 1 | 154.1300 | Fire Dispatch (3) |
| CMC EMS Dispatch | 155.2950 | EMS (4) |

### NJICS Trunked (6 entries — `service_tag` 2)

All ride **NJICS** (HPDB `trunk_id=7021`, P25X2_TDMA, **171 frequencies** across
the statewide system — pulled from `trunk_freqs` joined via
`trunk_sites WHERE trunk_id=7021`; each entry carries the full list in
`control_channels` so op25 / disco can lock whichever site is decoding cleanly).

| TGID | Alpha tag | Notes |
|---:|---|---|
| 5289 | Sea Isle City PD | **Will's destination** — primary PD activity TG |
| 5171 | Avalon PD | Cape May County / Avalon |
| 4991 | Stone Harbor PD | Cape May County / Stone Harbor |
| 5217 | Cape May PD | Cape May City |
| 2205 | NJSP Troop A Fleetwide | Troop A covers South Jersey — Cape May area |
| 2589 | NJSP Dispatch Interop | Statewide NJSP interop dispatch |

More district / county TGs available in HPDB at `tgroup_id=20541` (Cape May
County on NJICS, 38 TGs total) — Wildwood PD 5273, North Wildwood PD 5283,
Lower Township PD 4639, Middle Township PD 4455, Cape May County Sheriff
4261, etc. Add via the SB3 sidecar editor as needed.

## HPDB findings

| System | trunk_id | Protocol | Freqs | Coverage relevant to Cape May |
|---|---:|---|---:|---|
| **NJICS** (New Jersey Interoperability Communications System) | 7021 | P25X2_TDMA | 171 | **Yes** — NJSP Troops A/B/C statewide, Cape May County PDs (Sea Isle, Avalon, Stone Harbor, Cape May, Wildwood, etc.), county fire/EMS dispatch |
| Atlantic County | 7643 | P25X2_TDMA | 53 | Adjacent (Atlantic City PD, Ocean City NJ Fire); not added to this profile but available |
| Ocean County | 7231 | P25X2_TDMA | 15 | North of Cape May — relevant on the drive up; not added |

**No dedicated Cape May County trunked system in HPDB.** All county
LE / fire / EMS operations run on **NJICS** under the "Cape May County"
talkgroup group (`tgroup_id=20541`, 38 TGs).

NJSP conventional freqs come from the HPDB `AgencyId:753` entry:
- Group `21840`: NJ State Police OEM (low-VHF 39 MHz block)
- Group `36392`: NJ State Police (UHF talkaround 850 MHz)

## Activation when Will arrives

The profile is already in the SB3 grid. To activate:

1. Open the SB3 sidecar / favorites grid.
2. Tap the **Cape_May_Shore** tile.
3. The active profile shifts; `favorites_runtime_sync` regenerates the
   analog and digital configs, op25 restarts on the NJICS control channels.

To return to a Tennessee profile later, tap that tile instead — switching
is a single tap.

### Travel Mode (location)
Don't forget to set the scanner's location to 08243 (Sea Isle City) so
range-based filters work — either via the Travel Mode button on the SB3
main display (one-tap GPS refresh) or manually via the sidecar's location
panel.

## Schema reference (per `ui/hp_state.py`)

Same shape as the DC profile. Trunked entries carry the talkgroup ID +
the full 171-freq `control_channels` list from NJICS. Conventional entries
have `kind: "conventional"`, `system_id: 0`, and a single `frequency`.

## File locations

- JSON (this profile, mirror of the registered tile):
  `/Users/willminkoff/Documents/scannerproject/favorites_sets/cape_may_shore.json`
- Doc (this file):
  `/Users/willminkoff/Documents/scannerproject/favorites_sets/cape_may_shore.md`

The on-disk JSON is for backup / portability; the source of truth Will
interacts with is the **Cape_May_Shore** tile in the SB3 favorites grid.
