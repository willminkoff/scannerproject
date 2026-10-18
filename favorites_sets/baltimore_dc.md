# Baltimore-DC — Potomac TRACON, marine, Baltimore County PD

Profile for the Baltimore / Washington area. Built 2026-10-18 for the current
stack (airband chirp on scannerbox-1, ground chirp on Neptune, op25 on
scannerbox-1's RSPduo) and sized to its limits — unlike the older
`dc_aviation_le.json`, which predates them (23 airband channels against a pool
of 12; law-enforcement control channels entered as conventional 485/853-858 MHz
frequencies that SB3 silently drops; DC MPD talkgroups that are encrypted).

- File: `favorites_sets/baltimore_dc.json` — 38 entries
- Favorites name / tile: `Baltimore-DC` (`fav-baltimore-dc`), registered disabled

| Band | Contents | Fit |
|---|---|---|
| Airband | 12 Potomac TRACON (PCT) sectors | 12/12 channels, 4 of 6 LO clusters → 2.4 s revisit |
| Ground | 15 common marine channels (same set as SIC) | 15/16 channels, 1 cluster (156.300-157.100) |
| Digital | Baltimore County PD — 10 precinct dispatch + Sheriff main | 1 trunked system, 11 talkgroups |

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

## Digital — Baltimore County PD (HPDB trunk 7117)

| TG | Alpha |
|---:|---|
| 9600-9609 | Wilkens, Woodlawn, Franklin, Pikesville, Towson, Cockeysville, Parkville, White Marsh, Essex, Dundalk — precinct Ops (dispatch) |
| 9410 | Sheriff Main |

**Why Baltimore County.** op25 on this stack follows exactly one trunked system
(two systems generate two instances on one RSPduo tuner; the second can never
open it). Most of the region's police are encrypted: a Fairfax County PD
briefing dated 2026-03-10 lists DC MPD, Montgomery County PD, Baltimore City PD
and Anne Arundel County PD (plus Virginia State Police and several Northern
Virginia agencies) as having encrypted all radio traffic, and Fairfax PD's own
dispatch channels are planned to follow. Baltimore County PD is not on that
list; RadioReference shows encryption only for some investigative units, and
Broadcastify streams all 10 precinct dispatch channels. The Tac, ISU, CID and
intelligence talkgroups are deliberately left out.

Encryption status changes. If a precinct goes quiet in the clear, check
RadioReference for that talkgroup before blaming the radio.

### op25's tuning window

Baltimore County's simulcast site spreads its 32 channels over
854.0125-860.9625 MHz (6.95 MHz). op25 used to open 2.4 MS/s centred on the
selected control channel -- a 1.536 MHz IF filter on the RSPduo -- which left
most of the site outside the window. Since d69f55d the generator centres on the
site and opens 8 MS/s (8 MHz filter) for it, in single-tuner mode. If the RSPduo
is ever put in dual-tuner (MA/SL) mode it is capped at 2 MS/s and this site
won't fit again.
