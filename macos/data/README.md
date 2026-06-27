# macos/data — HPDB → SDRangel/SDRTrunk converters

Turn the carried-over HomePatrol data into the formats the macOS backends import.

## Inputs
- `homepatrol.db` (SQLite, 52 MB) + `hp_state.json` — backed up off the Mini to
  `~/Downloads/sb6-data-backup-2026-06-26.tar.gz` before the wipe.
- **Verified schema** (this DB): `conventional_freqs(alpha_tag,freq_hz,mode,tone,service_tag)`
  for analog; `trunk_systems(system_name,protocol)` + `trunk_sites` + `trunk_freqs(freq_hz,lcn)`
  + `talkgroups(dec_tgid,alpha_tag)` for trunked. MTRTRS (7078) + TACN (6355) are `P25X2_TDMA`.
- `hp_state.json`: `lat/lon`, `range_miles`, `enabled_service_tags`, `favorites`, `custom_favorites` — the location + what Will monitors.

## `hpdb_to_sdrangel.py` (analog → Frequency-Scanner CSV)
Emits `Freq (Hz),Enable,Notes,Channel,Ch BW (Hz),TH (dB),Sq (dB)` (matches
`etc/mac/sdrangel/scan-38380.csv`). mode AM→`R0:0`/8 kHz, FM→`R0:1`/12.5 kHz.
```
python3 hpdb_to_sdrangel.py --db homepatrol.db --band airband > airband.csv
python3 hpdb_to_sdrangel.py --db homepatrol.db --band ground  > ground.csv
python3 hpdb_to_sdrangel.py --db homepatrol.db --band airband --state hp_state.json --near  # Nashville-local
```
Tested: airband CSV correct; `--near` → 150 channels for the Nashville fav location.

## `hpdb_to_sdrtrunk.py` (trunked → playlist XML)
Emits an SDRTrunk playlist: P25 systems + control-channel candidates + talkgroup alias list.
```
python3 hpdb_to_sdrtrunk.py --db homepatrol.db --list                       # list P25 systems
python3 hpdb_to_sdrtrunk.py --db homepatrol.db --system MTRTRS --system TACN -o playlist.xml
```
Tested: finds MTRTRS+TACN (P25X2_TDMA), emits real TG aliases (TroopNet, Highway Patrol…).

⚠️ **Validate the XML element names against a playlist EXPORTED from the installed
SDRTrunk** before relying on it — the exact `<channel>`/`<decode_config>` schema +
P25 decode block vary by version. HPDB has no explicit control-channel flag, so all
of a site's freqs are emitted as CC candidates (SDRTrunk auto-detects the live CC).

## Data flow
```
homepatrol.db ─┬─ hpdb_to_sdrangel.py ─→ *.csv  ─→ SDRangel Frequency Scanner (import)
               └─ hpdb_to_sdrtrunk.py ─→ playlist.xml ─→ SDRTrunk (load playlist)
hp_state.json ─→ (location + favorites filter for both)
```
