# Ft Campbell UHF — Campbell AAF military air (225-400 MHz)

UHF milair for Campbell AAF (KHOP) and the Fort Campbell ranges. Built
2026-10-21 for the Nashville location, sized to the airband pool's limits
(12 channels / 6 LO clusters of 2 MHz).

- File: `favorites_sets/ft_campbell_uhf.json` — 8 entries
- Favorites name / tile: `Ft Campbell UHF` (`fav-ft-campbell-uhf`)
- Band: **airband only** (225-400 MHz routes to the AM pool). No ground, no digital.

| Band | Contents | Fit |
|---|---|---|
| Airband | 8 Campbell AAF / range UHF channels | 8/12 channels, **6/6 clusters** → 3.6 s revisit |

The cluster budget binds before the channel budget: UHF milair is spread over
~60 MHz here and each LO window is only 2 MHz wide, so seven channels is what
six windows buy. Adding a ninth frequency costs a whole cluster unless it lands
within 2 MHz of one already listed.

## Channels

| Cluster | MHz | Label | Who transmits |
|---:|---|---|---|
| 1 | 250.200 | Range Control STRIKER | ground + aircraft on ranges |
| 1 | 251.200 | ALCE (Airlift Command Element) | ground + airlift crews |
| 2 | 269.525 | **Campbell Approach/Departure West** | **aircraft at altitude** |
| 3 | 278.800 | **Campbell Tower (UHF)** | aircraft on final/departure |
| 4 | 285.625 | **Eagle Control / Eagle Radio** | aircraft entering R3701/R3702, MOAs, drop zones |
| 5 | 297.100 | **160th SOAR Operations** | Night Stalker ops |
| 3 | 280.250 | **Refuel?** — unidentified, locally strong | **aircraft** |
| 6 | 307.025 | **Campbell Approach/Departure East** | **aircraft at altitude** |

### 280.250 "Refuel?"

Heard on the Uniden at Burns on 2026-10-21 and confirmed on the RSP1B the same
evening: median -89.1 dBFS with a **17.2 dB burst** to -71.9. Real traffic,
locally strong. Not identified — the name is a hypothesis from the AR-46V /
AR-47V Night Stalker tracks running overhead, not a known assignment.

It rides free: 280.250 is 1.45 MHz from 278.800, so the two share one 2 MHz
window and the channel count goes 7 → 8 with the cluster count unchanged.

**disco never saw it** — zero detections in 279.5-281.0 MHz. Not a coverage
gap (the sweep is a continuous 225-400) but a duty-cycle limit: 200 ms dwell
and 1.8 MHz steps means ~97 steps per pass and a revisit near 20-30 s, so any
one frequency is being watched about 1% of the time. disco finds busy channels
and misses quiet ones. **An exact frequency off the Uniden's display is worth
more than disco's estimate** — disco locates to +/-25 kHz at best, the Uniden
reads the channel exactly.

## No continuous broadcasts — ever

**ATIS 308.400 was in the first version of this profile and was removed.** It is
a looping broadcast that never stops keying, and under scan-hold that is fatal:

- `CHIRP_SCAN_HOLD_MAX_SEC=30.0` — a hold is force-released after 30 s
- `CHIRP_LO_DWELL_SEC=0.6` — the other five clusters then take ~3 s to sweep
- ATIS is still transmitting when the rotation returns, so it holds again

That is **~30 s out of every 33 parked on the weather loop** — about 91% of
airtime — and the rest of the profile gets the 3 s scraps. There is no escape
from the operator's side: chirp's command set is `add_channel`,
`remove_channel`, `set_squelch`, `set_freq`, `set_gain` and friends. **There is
no skip, avoid or lockout.** The only way out is editing the profile.

So the rule for any chirp pool: **no ATIS, AWOS, ASOS, VOLMET, HIWAS, TIBS,
weather-voice or any other continuously-keyed channel.** Not even as a
"propagation indicator" — the diagnostic value is real, but it costs the whole
scan, and disco answers the propagation question without touching the
listening pool.

Two older profiles still carry this landmine and predate the rule:
`cape_may_shore.json` (108.6 KACY ATIS) and `dc_aviation_le.json` (132.05 DCA,
134.85 IAD, 127.8 BWI ATIS).

## Range reality — read this before judging the profile

**Campbell is ~55 statute miles (48 nm) from Nashville.** Radio horizon is
roughly `1.23 x (sqrt(antenna_ft) + sqrt(aircraft_ft))` nm, so from a ~20 ft
antenna:

| Transmitter | Height | Horizon | Reaches Nashville? |
|---|---|---|---|
| Tower, Ground, range control | ground level | ~6 nm | **No** |
| Helicopter in the pattern | 500 ft | ~33 nm | No |
| Helicopter on a low-level | 1,200 ft | ~48 nm | Marginal |
| Aircraft on approach | 3,000 ft | ~73 nm | Yes |
| Aircraft at altitude / on an AR track | 10,000 ft+ | ~128 nm | Yes |

So from Nashville this profile is an **air-side** catch: you hear the aircraft,
not Campbell. The approach/departure pairs (269.525 and 307.025) are the two
that earn their place — that is where aircraft talk while high enough to be
heard. The ground-side channels are in the list for when the rig is closer to
Campbell, and because they cost nothing extra in their clusters.

Add ~11 dB for the 8-way splitter with no LNA ahead of it, and the marginal
rows above get worse, not better.

## What is deliberately left out

- **VHF Campbell** (120.9 tower, 121.8 ground, 118.1 approach, 138.8
  clearance). Same range problem, and they would compete for the same
  12-channel pool. This profile is UHF per the request. VHF ATIS 125.175 is
  excluded for the same reason as its UHF twin — see above.
- **Campbell ground/range FM** (32.5, 40.15, 48.5, 65.2, 75.25) — low-band,
  ground-to-ground, no chance at 55 mi.
- **The Fort Campbell trunked system** — a ground-based trunk 55 mi away will
  not reach. Worth revisiting only from much closer.
- **Nashville-local UHF**, which would actually perform better from here:
  KBNA Tower 257.800, Ground 348.600, A/D East 360.700, A/D West 387.000;
  Memphis ARTCC Joelton Low 285.500, Nashville High 257.750, Nashville Ultra
  High 363.000. These belong in a separate `nashville_uhf` profile, not mixed
  into this one.

## Loading it costs you civil airband

The airband pool is shared and holds 12 channels total. Enabling this profile
replaces whatever VHF airband is loaded. The two cannot run at once.

The airband SDR's gain is currently tuned for VHF (`sdr_gain_db` 31,
`sdr_gain_amp_db` 28, RSP1B `2405265A60`). Those numbers were set at 119 MHz
and are not necessarily right at 270-310 MHz; expect to retune after switching.

## Source note — the UHF rows in `fort-campbell-milair.csv` are wrong

`fort-campbell-milair.csv` at the repo root predates this profile. Its **VHF**
rows check out (120.9, 121.8, 118.1, 125.175, 138.8, 139.9 all confirmed), but
its **UHF** rows do not match any authoritative source:

| CSV claims | Actually |
|---|---|
| 241.000 "Campbell Tower UHF" | 278.800 |
| 275.800 "Campbell Ground UHF" | 266.800 |
| 246.600 "Campbell Approach/Departure" | 269.525 (West) / 307.025 (East) |
| 372.200 "Pilot-to-Dispatcher" | 230.100 |
| 344.600 "Campbell Weather G2A" | 343.300 (PMSV Metro — interactive, not a loop) |
| 280.900 "Sabre Heliport" | no corroboration found |
| 367.300 "Eagle Range Radio" | 285.625 (Eagle Control / Eagle Radio) |

Nothing in this profile comes from that CSV. Every frequency here is from the
RadioReference database entry for Campbell AAF (aid/5349), the RadioReference
Nashville Milcom wiki, and AirNav's KHOP page, and appears in at least two of
the three. Treat the CSV's UHF half as unverified until it is corrected.

## Currency

The RadioReference airfield entry was last updated 2012 and the Nashville
Milcom wiki 2020; AirNav is current and agrees on tower, ground, approach and
ATIS. Military frequencies do move. The official source is the DoD FLIP
Chart Supplement, which is not public. If a channel is dead for a long stretch
while aircraft are clearly working the field, sweep 225-400 with disco and
compare — that is what disco is for.
