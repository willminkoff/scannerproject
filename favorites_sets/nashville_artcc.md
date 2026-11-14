# Nashville ARTCC — Memphis Center (ZME) enroute sectors

Six VHF AM channels, airband only. No ground, no digital.

Source: the HomePatrol DB on Neptune (`~/scanner-db/homepatrol.db`,
RadioReference-curated), system `AgencyId:2233`. The join is
`conventional_groups.parent_key='AgencyId' AND parent_id=2233` — `parent_key`
is the literal string, the number lives in `parent_id`. ARTCC groups have NULL
lat/lon, so sites have to be matched by name, not distance.

| MHz | Sector | RCAG site | HPDB cfreq_id |
|---|---|---|---|
| 118.875 | Sector 62 Smyrna Hi | Joelton/Nashville | 245983 |
| 124.125 | Sector 61 Clarksville Super Hi | Joelton/Nashville | 245985 |
| 124.275 | Sector 63 Campbell Hi | Graham, TN | 246323 |
| 125.850 | Sector 42 Graham Low | Graham, TN | 246324 |
| 132.900 | **Sector 40 Nashville Low** | Joelton/Nashville | 245989 |
| 133.850 | Sector 41 Bowling Green Low | Joelton/Nashville | 245987 |

Joelton is the RCAG site that serves Nashville. Shelbyville's 128.150
(Sector 60, group 58691) is deliberately absent: with it the set needs four
2 MHz LO windows and chirp plans at most three, so `_hp_push_chirp` would trim
it anyway — silently. Jackson TN's sectors (124.350 / 126.450 / 134.650 /
136.175) are west Tennessee, ~120 miles away, and are not included.

`_hp_extract_pool` routes 118-137 MHz to the airband daemon, so all six land on
AM airband (port 7400) and none touch ground.

NOT VERIFIED ON AIR. The HPDB is a periodic dump and ARTCC assignments change.
Only 132.900 is independently corroborated (docs/sdrangel-scan-38380.md and
RadioReference's ZME page). Sectors 40 and 41 are reported combined and
simulcast on both frequencies when traffic is light, so the same audio on two
channels is expected rather than a fault. UHF pairs exist for every sector
(290.300 / 285.500 / 269.425 / 257.750 / 288.350 / 379.250) and are omitted.
