# SIC MilAir — UHF military aviation at Sea Isle City NJ (08243)

UHF milair profile for Sea Isle City. Built to be run **instead of**
`Cape_May_Shore`, not alongside it — see "Why it can't coexist" below.

- File: `favorites_sets/sic_milair.json`
- Favorites name: `SIC_MilAir`
- Entries: **11 conventional**, all UHF AM, `service_tag` 15 (Aircraft)
- Built **2026-10-14**
- Radio: the **airband chirp** (scannerbox-1, RSP1B `2405265A60`, port 7400, mode `am`)

`service_tag` is 15 (Aircraft) rather than 30 (Military) deliberately:
`enabled_service_tags` in HPState is `[5, 9, 15]`, so tag 30 entries would
be filtered out of the UI. These are aircraft frequencies; 15 is accurate.

## Channels

Ordered by cluster, because that is what governs how often each one is heard.

| Freq (MHz) | Alpha tag | What it is |
|---|---|---|
| 255.000 | Giant Killer W107 | FACSFAC VACAPES, Warning Area W-107 — directly offshore |
| 255.800 | Huntress | NORAD Eastern Air Defense Sector |
| 260.900 | Huntress CAP | NORAD EADS, DC/DE area CAP |
| 261.000 | 119FS Devil Ops | 119th FS NJ ANG "Devil Ops", KACY |
| 281.450 | ZDC ACY RCAG | Washington Center, Atlantic City RCAG |
| 283.100 | Warren Grove Range | 177 FW gunnery range, NJ (primary) |
| 285.400 | ZDC Sea Isle RCAG | Washington Center, **Sea Isle RCAG — the local site** |
| 286.200 | Warren Grove MOA 2 | Warren Grove MOA (secondary) |
| 298.600 | 119FS ACY | 119th FS discrete |
| 298.875 | ACY Final Approach | KACY Final Approach Control (UHF) |
| 327.125 | ACY Approach UHF | KACY Approach/Departure (UHF) |

### Why these

Your civilian SIC channels are the **VHF halves of ZDC/ZNY sectors**, radiated
from local RCAG sites; ATC runs paired VHF/UHF per sector. So the UHF twins of
sites you already hear well beat any generic milair list. `n4-10-sector-51-casino-low`
(127.700) transmits from the **Sea Isle VOR in Cape May County** — you sit on
top of that RCAG, which makes **285.400** the single best target here.

## Cluster plan — verified against `chirp/dsp/cluster_planner.py`

```
1. center 255.4000  span 0.800 MHz   Giant Killer W107, Huntress
2. center 260.9500  span 0.100 MHz   Huntress CAP, 119FS Devil Ops
3. center 282.2750  span 1.650 MHz   ZDC ACY RCAG, Warren Grove Range
4. center 285.8000  span 0.800 MHz   ZDC Sea Isle RCAG, Warren Grove MOA 2
5. center 298.7375  span 0.275 MHz   119FS ACY, ACY Final Approach
6. center 327.1250  span 0.000 MHz   ACY Approach UHF

clusters 6/6    channels 11/12    revisit 3.6 s at dwell 0.6 s
```

The pairing is the whole point: 11 channels for the cluster cost of 6. Eleven
unpaired milair frequencies would need 11 clusters and a 6.6 s revisit.

## Why it can't coexist with Cape_May_Shore

Milair spans ~72 MHz here and the LO window (`iq_bw_hz`) is 2 MHz, so channels
almost never share a cluster. `Cape_May_Shore`'s airband block already uses 5 of
the 6 clusters on its own. Loading both exceeds `lo_scheduler.max_clusters` and
**chirp rejects an oversized `add_channel` batch in full** — which once left
airband scanning a single frequency. Run one or the other.

## Caveats

- **Cluster 3 sits at 82.5% of Nyquist.** A 1.650 MHz span puts both channels
  ±0.825 MHz from an LO centred in a 2 Msps window, where the decimation filter
  rolls off. If Warren Grove reads weak, split it to its own cluster first
  (needs `CHIRP_LO_MAX_CLUSTERS=7`, revisit 4.2 s).
- **Single-source frequencies.** From the
  [UDXF Mid-Atlantic milair list](https://www.udxf.nl/Mid-AtlanticMilair-freqlist.pdf),
  community-compiled, rev. 31 Dec 2024. The *sites* are confirmed local; the
  specific UHF numbers are not independently corroborated. Prune by what
  actually produces audio.
- **17% duty cycle.** 0.6 s dwell across 6 clusters. `scan_hold` latches once a
  transmission starts, but short calls will slip through. Inherent to one SDR
  rotating across this much spectrum.
- **Sensitivity is unproven up here.** disco saw zero energy on 225–400 in 24 h,
  but that was the HackRF (poor noise figure, no preamp) on the 8-way splitter.
  The RSP1B is a much better receiver. If this profile hears nothing at all, the
  antenna/LNA is the problem, not the frequency list.

## Next tier

Both caps bind — `max_channels: 12` (`CHIRP_MAX_CHANNELS`) and
`max_clusters: 6` (`CHIRP_LO_MAX_CLUSTERS`) — **clusters first**. Best value per
extra cluster:

| Add | Cost | Why |
|---|---|---|
| 294.725 + 295.400 | 1 cluster | NJ ANG F-16 discrete + their AR discrete — actual 177 FW ops |
| 238.100 + 238.675 | 1 cluster | Giant Killer W-386 check-in + 119th FS |
| 292.300 + 293.550 | 1 cluster | Giant Killer W-386 + W-107 |
| 243.000 | 1 cluster | UHF Guard — classic, mostly silent |
