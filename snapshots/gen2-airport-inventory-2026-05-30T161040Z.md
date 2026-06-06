# Gen-2 Airport Profile Inventory

**Generated:** 2026-05-30T16:10:40Z
**Host:** ubuntu@micro.local
**Project:** /home/ubuntu/scannerproject
**Branch:** main @ 105b7a7 (Phase 2A)

Read-only inventory of the 8 remaining gen-2 airport `.conf` profiles to inform a keep / drop / migrate decision per id.

## Surfaces audited

| Surface | What it is |
|---|---|
| `profiles/rtl_airband_<id>.conf` | Source gen-2 rtl-airband config (channels, freqs, labels) |
| `runtime/v3/canonical_config.json` | v3 runtime registry (`analog.profiles[]`) |
| `profiles/profiles.json` | Regenerated registry served by `/api/profiles` |
| `ui/config.py` `PROFILES` list | Legacy gen-2 static profile list |
| `ui/sb3.html` | Hardcoded tile / dropdown / DOM references |
| `data/hp_state.json` → `favorites[*].custom_favorites` | HPDB favorites — used for freq-overlap migration safety |
| `data/favorites_seeds/*.json` | Durable seed files (gen-2 → seed migration target) |

**HPDB enabled state matters.** Only `fav-sic` is `enabled=True`. The other 9 favorites (`fav-nashville-main`, `fav-vu-pd-only`, `fav-acy-airshow`, etc.) are `enabled=False` — present in HPDB but not currently active. An overlap with a disabled favorite means the freqs *exist* in HPDB and can be re-enabled, but they aren't streamed today.

## Summary table

| id | ch / freqs | range | canon | profiles.json | ui/config.py | sb3.html hardcode | seed file | HPDB overlap (enabled / total) | Recommendation |
|---|---|---|---|---|---|---|---|---|---|
| `acy_airshow` | 2 / 18 | 118.0–134.1 MHz | yes | yes | **no** | none | yes (`fav-acy-airshow.json`, 22 entries — but **not** a superset, see notes) | 5/18 in `fav-sic` (enabled); **only 3/18** in the existing acy seed; **11/18 in no HPDB favorite at all** | **NEEDS WILL'S CALL — seed does NOT cover the conf** (drop loses 11 freqs unless seed is extended) |
| `airband` | 1 / 15 | 118.4–135.1 MHz | yes | yes | yes (L375) | none (band concept only) | no | 2/15 in `fav-sic`; **12/15** in `fav-vu-pd-only` (disabled); 14/15 across all favs; only **135.1** unique | **NEEDS WILL'S CALL** (Nashville KBNA — likely KEEP; if dropping, migrate to seed) |
| `atl` | 3 / 10 | 119.1–133.475 MHz | yes | yes | yes (L376) | none | no | 1/10 in `fav-sic`; 3/10 across all favs; **7 freqs unique** to this conf | **NEEDS WILL'S CALL** (not local — drop unless you want ATL tower/app/dep coverage) |
| `khop` | 1 / 9 | 118.1–142.9 MHz | yes | yes | yes (L380) | none | no | 1/9 in `fav-sic` (only 121.5 Guard); 2/9 across all favs; **7 freqs unique** (Campbell Army Airfield) | **NEEDS WILL'S CALL** (Fort Campbell — drop unless tracking Campbell ops) |
| `kmqy` | 1 / 2 | 118.4–118.5 MHz | yes | yes | yes (L381) | none | no | 0/2 in `fav-sic`; **118.4** in disabled `fav-nashville-main` / `fav-vu-pd-only`; **118.5 (Smyrna Tower) unique** | **NEEDS WILL'S CALL** (Smyrna home airport — likely KEEP; migrate to seed, only 2 freqs) |
| `nashville_centers` | 1 / 25 | 118.4–135.5 MHz | yes | yes | yes (L377) | none | no | 2/25 in `fav-sic`; **12/25** in `fav-vu-pd-only` (disabled); 14/25 across all favs; **11 freqs unique** (mostly Center sectors) | **NEEDS WILL'S CALL** (Nashville Centers — likely KEEP; this is the biggest one) |
| `tower` | 1 / 1 | 118.6 MHz | yes | yes | yes (L379) | none | no | 0/1 in `fav-sic`; **1/1** in `fav-nashville-main` + `fav-vu-pd-only` (both disabled) | **DROP (overlap exists in HPDB)** — but only in disabled favs; effectively a one-tap shortcut to BNA Tower |
| `tune_atis` | 1 / 1 | 127.075 MHz | yes | yes | yes (L382) | none | no | **0/1 in any HPDB favorite** — 127.075 nowhere | **NEEDS WILL'S CALL** (drop = lose this freq unless migrated; freq looks like a non-BNA ATIS — what airport is this?) |

**Notes on the table:**

- "ch / freqs" = number of channel blocks in the rtl-airband conf / total unique frequencies across those blocks.
- "sb3.html hardcode" = grep for the literal id as a tile DOM-id or `data-profile`. All 8 profiles are surfaced in the UI only via the dynamic profile dropdown loaded from `/api/profiles` into `#profiles-list` (rendered by JS around L8576). The 61 hits for the word `airband` in sb3.html refer to the *band concept* (target=airband vs target=ground), not the `airband` profile id.
- "HPDB overlap" is computed against `data/hp_state.json` `favorites[*].custom_favorites` where `kind == "conventional"` and `frequency > 0`.
- No id is a "ghost" — all 8 are present in canonical + profiles.json. `acy_airshow` is the only one *missing* from `ui/config.py PROFILES`, which is consistent with its half-migrated state (v3 registry knows it; legacy static list doesn't).

## Per-profile notes

### `acy_airshow` — ACY Airshow (Atlantic City)
- 2 channel groups, 18 unique freqs. Gain 32.8, AM, 12k BW.
- Conf Group 1 = 7 ACY airport ATC freqs (118.0 Clnc Del, 120.3 ATC, 121.5 Guard, 121.9 Ground, 124.6 ATC, 126.2 Mil/ATC Common, 127.85 App/Dep S).
- Conf Group 2 = 11 airshow VHF chatter freqs (122.75 GA Air-to-Air, 122.8 CTAF, 122.85 Airshow Common, 122.925 Aerial Demo, 123.025 Heli Air-to-Air, 123.3, 123.35 Airshow Coord, 123.4 Performer Coord, 123.45, 123.5 Flight Test, 134.1 NORAD/Intercept).
- **CORRECTION on Will's initial premise** — the seed `data/favorites_seeds/fav-acy-airshow.json` (22 conv entries) is **not** a superset of the conf. The seed and conf overlap on only **3 freqs**: 120.3 ATC/ATIS, 122.85 (seed labels "Air Boss", conf labels "Airshow Common"), and 124.6 ATC/ATIS.
- The seed adds 19 freqs the conf cannot tune (Air Boss UHF/VHF Demo Team/177th FW/Marine — 120.6, 122.475, 123.15, 135.65, 138.05, 138.125, 156.8, 157.05, 238.15, 239.0, 255.0, 255.15, 261.0, 277.7, 285.4, 311.225, 316.15, 327.125, 384.55).
- The seed is missing 15 conf freqs (118.0, 121.5, 121.9, 122.75, 122.8, 122.925, 123.025, 123.3, 123.35, 123.4, 123.45, 123.5, 126.2, 127.85, 134.1). Of those, **11 appear in no HPDB favorite at all** (118.0, 122.75, 122.8, 122.925, 123.025, 123.3, 123.35, 123.45, 123.5, 126.2, 134.1) — dropping the conf without extending the seed loses them.
- The matching HPDB row `fav-acy-airshow` is loaded into `hp_state.json` (currently `enabled=false`) and reflects the seed, not the conf.
- **Net:** drop is not as safe as the original task brief implied. Either extend the seed to cover the conf's 15 missing freqs (cheap, scripted), or keep the conf until that migration is done.

### `airband` — KBNA (Nashville) airband
- 1 channel group, 15 freqs spanning 118.4 (BNA Dep East) → 135.1 (BNA ATIS).
- Includes BNA Tower 118.6, Ground 121.9, Clnc Del 126.05, Final 124.75, plus airline ops (United 128.825, SWA 130.125, Frontier 130.725, Delta 131.45) and ramp freqs.
- `fav-vu-pd-only` (disabled) already carries 12 of these 15 freqs with the same labeling. Only **135.1 BNA ATIS** is genuinely unique to the conf.
- This is the primary BNA stream profile — if Will keeps it as a one-tap selector, leaving the conf is the easy path. If consolidating to HPDB-only, enabling `fav-vu-pd-only` (or a fresh BNA seed) + adding 135.1 covers it.

### `atl` — KATL (Atlanta Hartsfield)
- 3 channel groups (Tower 5 / App 2 / Dep 3), 10 unique freqs. Gain 25.4.
- Labels are mechanical ("119.1000 ATL TWR" etc.) — looks scraped, not curated.
- 7 of 10 freqs (119.3, 119.5, 121.225, 123.85, 127.9, 128.0, 133.475) appear in **no** HPDB favorite. Dropping the conf without a seed loses those.
- Atlanta is ~250 mi from Nashville; this looks like a vestigial test or travel profile rather than a daily-use one.

### `khop` — KHOP Campbell Army Airfield (Fort Campbell, KY/TN)
- 1 channel group, 9 freqs incl. CAMPBELL TOWER 120.9, GROUND 121.8, App/Dep 118.1, ATIS 125.175, CLEARANCE 138.8, KHOP BASE OPS 142.9.
- 7 of 9 freqs (everything except 121.5 Guard and 122.95 CTAF) appear in no HPDB favorite — most are mil-air outside the civil airband HPDB tends to cover.
- Fort Campbell is ~50 mi NW of Nashville. Possibly relevant to Will for severe-weather Army Airfield monitoring; otherwise drop.

### `kmqy` — KMQY (Smyrna, TN)
- 1 channel group, only 2 freqs: 118.4 (Nashville App/Dep — shared with BNA) and 118.5 (Smyrna Tower — unique).
- Smyrna is ~20 mi SE of central Nashville; KMQY is the busy GA reliever for BNA.
- 118.5 SMYRNA TOWER is **not** in any HPDB favorite. Dropping without a seed loses it.
- Trivially small (2 freqs) — easy to add to an existing Nashville HPDB favorite or seed if you want to retire the conf.

### `nashville_centers` — Nashville Centers + Airband
- 1 channel group, **25 freqs** spanning 118.4 → 135.5 MHz. Superset of `airband` (15 freqs) plus 10 ZME Center / TRACON sector frequencies labeled "Smyrna Sector 62", "Clarksville Sector 61", "Jackson Holly Springs", "Troy Elvis Sector 33", "Shelbyville Sector 60", "Nashville Low Sector 40", "Bowling Green Low Sector 41", "Jackson McKellar", "Troy TNGS Sector 25", "Jackson Jacks Creek".
- 12 of 25 in disabled `fav-vu-pd-only`, but the 11 Center-sector freqs (118.875, 124.125, 124.35, 126.45, 127.975, 128.15, 132.9, 133.85, 134.65, 135.1, 135.5) live **only** in this conf.
- This is the most data-rich gen-2 profile. If dropping, the Center sectors really should land in a `fav-nashville-centers.json` seed first.

### `tower` — TOWER (118.600)
- 1 freq, 1 channel: BNA Tower 118.6 only. Effectively a one-tap shortcut.
- 118.6 already appears in disabled `fav-nashville-main` and `fav-vu-pd-only` with matching "Tower" labels.
- Dropping costs nothing on the data side; the only loss is the convenience tile (and there's no hardcoded tile — it's only in the dynamic dropdown anyway).

### `tune_atis` — Tune ATIS
- 1 freq, 1 channel: **127.0750** MHz, bandwidth 25000 (wider than the standard 12000 used by everything else), gain 42.1.
- 127.075 is **not** in any HPDB favorite, not in any other gen-2 conf, not in canonical analog channels elsewhere. Dropping this conf loses 127.075 entirely unless you migrate.
- The label is just "Tune ATIS" — no airport hint. 127.075 isn't a BNA ATIS (BNA ATIS is 135.1). Worth asking Will what airport this targets before deciding.

## Open questions for Will

1. **`acy_airshow`** — the original "drop-safe (seed exists)" premise is incorrect. Seed and conf overlap on only 3/18 freqs; 11 conf freqs are in no HPDB favorite. Want me to draft a `fav-acy-airshow.json` extension that adds the conf's 15 missing freqs before dropping?
2. **`airband` (KBNA)** — keep the conf as a one-tap selector, or consolidate to HPDB? If consolidating, do you want a fresh `fav-bna-airband.json` seed, or just enable `fav-vu-pd-only` and add 135.1?
3. **`nashville_centers`** — same call as airband, but with 11 unique Center sectors that need seed-migration before dropping. Seed it?
4. **`kmqy` Smyrna** — keep conf, or migrate the 2 freqs (especially 118.5 Tower) into an HPDB seed?
5. **`atl`, `khop`** — any reason to keep? They're well outside your daily range.
6. **`tune_atis`** — what airport is 127.075? If it's something you want, seed it; otherwise drop.
7. **`tower`** — purely a convenience shortcut to BNA Tower 118.6. Drop?

## Hard rules respected

- Read-only: no edits, no commits, no service restarts.
- No "ghost" ids found (all 8 in canonical + profiles.json). `acy_airshow` absent from `ui/config.py` PROFILES is noted as half-migrated, not ghost.
- All 8 `.conf` files exist on disk.
