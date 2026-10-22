# Burns MilAir — UHF milair actually heard at Burns TN

Built 2026-10-21 from **measured activity**, not from a database. Every channel
here was observed transmitting at 36.047/-87.306 (Burns, ZIP 37029) on the
evening of 2026-10-21, using disco's 225-400 MHz HackRF sweep cross-checked
against the chirp RSP1B.

- File: `favorites_sets/burns_milair.json` — 9 entries
- Favorites name / tile: `Burns MilAir` (`fav-burns-milair`)
- Band: **airband only** (225-400 routes to the AM pool). No ground, no digital.

| Fit | |
|---|---|
| Channels | 9 / 12 |
| LO clusters | **5 / 6** → 3.0 s revisit |
| Global squelch | **-80 dBFS** (band floor here is -87 to -90) |

## Channels

| Cluster | MHz | Evidence |
|---:|---|---|
| 1 | 262.125 | **live voice**, 15 dB bursts, peak -72.9 |
| 1 | 262.175 | **live voice**, 17 dB bursts, peak -71.1 |
| 1 | 262.225 | **live voice**, 15 dB bursts, peak -72.3 |
| 2 | 286.900 | disco centre 286.903, 26 dB AM voice, still active 21:13 |
| 2 | 286.925 | comb |
| 2 | 286.950 | disco 24 h centre 286.966 |
| 3 | 303.025 | disco voice; sits near the 303.000 air-to-air common |
| 4 | 311.700 | disco voice x17, active 21:15 |
| 5 | 321.975 | disco voice x10 |

## Why combs instead of single channels

disco locates an emitter to about **±25 kHz at best and ±60 kHz in practice** —
its FFT bins are ~21 kHz and the same emitter was reported at 286.903 one
half-hour and 286.966 the next. Military UHF channels are spaced 25 kHz and
chirp's channel filter is 6 kHz wide, so a disco centre is *not* precise enough
to tune directly: being one bin off means hearing nothing.

So the two strongest regions get a 3-channel comb on the 25 kHz grid rather
than a single guessed frequency. chirp has the resolution disco lacks; the comb
lets the receiver pick the right one. Once the hit log shows which member of
each comb actually produces audio, the other two can be dropped and the slots
reused.

## What was deliberately excluded

**Continuously-keyed carriers.** These would pin scan-hold for 30 s out of every
33 exactly like an ATIS (see `ft_campbell_uhf.md`):

| MHz | Why |
|---|---|
| 361.25-361.50 | 44+31 detections, **zero** voice classifications |
| 386.07-386.33 | 35+23 detections, zero voice |
| 338.575, 357.575, 340.975 | zero voice |
| 231.850, 232.250 | sat squelch-open in ~100% of samples |
| **227.100** | **disco called it AM_VOICE — it is not.** Measured median -73.1 dBFS, 16 dB above the band floor, burst only 5.1 dB. A steady carrier. It was in the first build of this profile and held scan-hold 91% of the time. |

227.100 is the cautionary one: **disco's modulation classifier is not
sufficient evidence on its own.** A channel earns a slot only if the receiver
shows it bursting well above its own median — a continuous signal has a high
median and a *low* burst, which is the opposite signature from voice.

## Measurement method

With squelch pinned shut (threshold -5 dBFS) so scan-hold never engages and the
LO rotates evenly, sample `signal_level_dbfs` per channel for several minutes:

- `median` — that channel's noise floor
- `burst` = `max - median` — voice lifts the max well above the median

Verdicts used: `median > -78` → continuous carrier, drop; `burst >= 8` → voice;
`burst 5-8` → intermittent; below that → quiet.

**Watch the cluster geometry when measuring.** In one pass 232.250 and 234.225
were grouped into a single 2 MHz cluster, which left both ~990 kHz off centre
on the filter skirt: they read -100 dBFS there versus -67 when centred. A
channel far from its cluster centre is attenuated, and the reading is garbage.
Every channel in this profile sits within 50 kHz of its cluster centre.

## Noise floor is not flat across 225-400

Measured here at `sdr_gain_db=20`:

| Region | Floor |
|---|---|
| 262 MHz | -87.6 to -88.1 |
| 286-287 | -88.7 to -89.3 |
| 303, 311, 321 | -89.7 to -90.3 |
| 227 | -73.1 (occupied by the continuous carrier above) |

-80 global clears all of them by 7.6-10 dB. An earlier -75 was fine for the
250-307 Ft Campbell set but trips on the noisier low end, and the -55 inherited
from VHF airband sat **24 dB above** this band's floor — nothing weak would
ever have opened it.

Per-channel squelch does not survive: `add_channel` accepts `squelch_dbfs`, but
the daemon's global squelch overrides it, and `_hp_push_chirp` hardcodes -40 for
AM anyway. Set `CHIRP_GLOBAL_SQUELCH_DBFS` instead.

## Still unidentified

None of these could be matched to a published assignment. The RadioReference
Nashville Milcom wiki (last touched 2020) and the Memphis ARTCC listings have
nothing at 286.9, 262.2 or 311.7, and the ±25 kHz uncertainty makes database
lookup unreliable anyway. 303.025 is the one plausible match — the wiki lists
303.000 as SODA Control / air-to-air common.

Identification is best done from the audio: catch a callsign and work backwards.

## Refining it

chirp logs every hit to `~/.local/share/chirp/logs/hits.jsonl`. After this has
run a while, that file says which channels actually produced audio — collapse
each comb to its winner and spend the freed slots on the next candidates:
293.525, 312.475, 319.600, 234.225.
