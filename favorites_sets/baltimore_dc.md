# Baltimore-DC — Potomac TRACON, marine, Laurel / PG County police

Profile for the Baltimore / Washington area. Built 2026-10-18 for the current
stack (airband chirp on scannerbox-1, ground chirp on Neptune, op25 on
scannerbox-1's RSPduo) and sized to its limits — unlike the older
`dc_aviation_le.json`, which predates them (23 airband channels against a pool
of 12; law-enforcement control channels entered as conventional 485/853-858 MHz
frequencies that SB3 silently drops; DC MPD talkgroups that are encrypted).

- File: `favorites_sets/baltimore_dc.json` — 31 entries
- Favorites name / tile: `Baltimore-DC` (`fav-baltimore-dc`), registered disabled

| Band | Contents | Fit |
|---|---|---|
| Airband | 12 Potomac TRACON (PCT) sectors | 12/12 channels, 4 of 6 LO clusters → 2.4 s revisit |
| Ground | 15 common marine channels (same set as SIC) | 15/16 channels, 1 cluster (156.300-157.100) |
| Digital | Laurel PD + PGPD District 6 (Beltsville) | 1 trunked system, 4 talkgroups |

## Airband — Potomac TRACON (VHF AM)

Chosen to pack into 2 MHz LO windows, weighted toward BWI.

| MHz | Sector | Cluster |
|---|---|---|
| 119.000 | BWI final north — Class B 020-100 (BWIFN) | 1 |
| 119.400 | BWI monitor south (BWIMS) | 1 |
| 119.700 | BWI final south — Class B 131-180 (BWIFS) | 1 |
| 119.850 | Class B west (OJAAY) | 1 |
| 120.450 | Class B 241-330 (MANNE) | 1 |
| 124.200 | Class B east (ENSUE) | 2 |
| 124.550 | Class B 101-130 (GRACO) | 2 |
| 124.700 | Final approach west (DCA) | 2 |
| 126.100 | Class B 331-090 (MULRR) | 3 |
| 126.650 | Departure south (TILLY) | 3 |
| 126.800 | App/Dep 171-263 (FLTRK) | 3 |
| 128.700 | Class B 181-019 (WOOLY) | 4 |

PCT has 43 VHF frequencies in HPDB. Swapping sectors is fine as long as the
set stays at 12 channels and 6 clusters; anything within 2 MHz of a cluster
above rides along for free. Two clusters are spare.

## Ground — marine

The 15 common marine channels from SIC: 06, 07A, 08, 68, 09, 69, 10, 11, 13,
16, 77, 18A, 19A, 80A, 22A. Chesapeake / Baltimore harbor traffic uses the same
calling, bridge-to-bridge and USCG channels.

## Digital — Prince George's County (HPDB trunk 6341)

| TG | Alpha |
|---:|---|
| 199 | Laurel PD — dispatch |
| 201 | Laurel PD Tac |
| 55 | PGPD C — Charlie Sector, District 6 (Beltsville) dispatch |
| 57 | PGPD C Tac |

P25 Phase II (TDMA voice; boatbod op25 handles it). From Laurel the resolver
picks the North Simulcast site (14 channels, 769.28-774.68 MHz, 5.4 MHz → a
6 MS/s window); NAC 2A0. None of these talkgroups is marked encrypted on
RadioReference; PG's SWAT/special units are.

op25 follows exactly one trunked system, so this is the system for where the
box actually is. Set the ZIP to match: the resolver picks sites by distance
from it and skips systems with no site in range.

**Why not Baltimore County** (the first version of this profile, 2026-10-18):
its only site is the Towson-centred simulcast (20.6 mi radius), 27 mi from
Laurel; op25 never decoded a control message there. Its precinct dispatch
talkgroups (9600-9609, Sheriff 9410) are in the clear and are the right pick
when the box is in Baltimore County itself — but its 32 channels span
854.0125-860.9625 MHz and need the 8 MS/s window.

**Also clear near Laurel:** Howard County (trunk 10849, Phase I) HCPD North
10961 / South 10963 dispatch; the rest of HCPD is encrypted. Its site spans
9.2 MHz — wider than one 8 MHz window — so op25 falls back to a 2.4 MS/s window
on the selected control channel and misses voice outside it.

Encrypted in the region (Fairfax County PD briefing, 2026-03-10): DC MPD,
Montgomery County PD, Baltimore City PD, Anne Arundel County PD, Virginia State
Police and several Northern Virginia agencies.
