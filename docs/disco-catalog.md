# Disco — signature catalog (`service_signatures.yaml`)

The fingerprinter (`disco/src/fingerprint.py`) identifies a signal's
**service/standard** by matching measured features against the curated
catalog at `disco/configs/service_signatures.yaml`. This doc explains the
entry format, how to add entries, and the attribution policy.

## Entry format

```yaml
  - name: "NOAA Weather Radio"        # label shown in the UI / Claude prompt
    freq_min_hz: 162395000            # inclusive freq range where plausible
    freq_max_hz: 162555000
    bandwidth_3db_hz_min: 10000       # expected 3 dB bandwidth bracket
    bandwidth_3db_hz_max: 18000
    shape: narrow_carrier             # see shapes below
    duty_cycle: continuous            # continuous | bursty | hopping | unknown
    confidence_weight: 1.0            # operator hint [0,1]; lower = more generic
    allowed_bands:                    # optional band-name allowlist (see below)
      - NOAA_WX
    source: manual                    # optional provenance tag (PR #34)
    notes: "free-form; cite references here"
```

**Required fields:** `name`, `freq_min_hz`, `freq_max_hz`,
`bandwidth_3db_hz_min`, `bandwidth_3db_hz_max`, `shape`, `duty_cycle`.

**Shapes:** `narrow_carrier`, `wide_flat`, `ofdm_multicarrier`,
`fsk_two_tone`, `broadband_noise`, `unknown` (see
`fingerprint._classify_shape`).

## Band scope — `allowed_bands` / `forbidden_bands`

Optional band-name constraints layered on top of the freq range (added in
PR #28/#29):

- `allowed_bands`: the entry is **rejected** unless the detection's band
  (from `us_band_plan.yaml`) is in the list.
- `forbidden_bands`: rejected when the band **is** in the list.

**Invariant (enforced by tests):** if an entry has `allowed_bands`, the list
must equal *exactly* the set of band-plan bands its `[freq_min, freq_max]`
overlaps — no missing bands (silent rejection bug) and no unreachable bands
(dead refs). Entries whose freq range falls entirely outside the band plan
(HF, microwave) carry **no** band scope; the freq range alone gates them.

`tests/test_disco_band_scope_expansion.py` and
`tests/test_disco_catalog.py` enforce these invariants on every entry.

## Adding entries

### By hand
Append a stanza to `service_signatures.yaml`. Make sure:
1. All required fields are present, shape/duty are valid.
2. If the freq range overlaps the band plan, set `allowed_bands` to the
   exact overlapping band set (run the band-plan check below).
3. Tag provenance with `source:` and cite references in `notes`.

### In bulk — `scripts/ingest_sigidwiki.py`
The generator holds a curated table of well-known signal types and emits
YAML stanzas with `allowed_bands` **computed automatically** from the band
plan (so the coverage invariant always holds):

```bash
python3 scripts/ingest_sigidwiki.py            # preview stanzas on stdout
python3 scripts/ingest_sigidwiki.py --append   # append to the catalog
```

Re-running is idempotent — entries whose `name` already exists are skipped.

## Attribution policy

Every entry should record where it came from:

- `source: manual` — composed from public references (datasheets, the
  community **Signal Identification Wiki**, https://www.sigidwiki.com/wiki/Database,
  band-plan knowledge). The `notes` field cites the reference.
- (entries without a `source:` tag predate PR #34 and are the original
  hand-built core: broadcast, NOAA, P25, GMRS, etc.)

Tagging lets us audit or bulk-remove a provenance class later without
touching the hand-built core. `source: sigidwiki` is reserved for entries
produced by a future automated scrape of the wiki; the current bulk set is
`source: manual` (composed offline, not scraped).

## Coverage today

~84 entries after PR #34 (was ~30): aviation, marine, weather/NOAA + sats
(APT/LRPT/Orbcomm), public-safety P25 (Phase 1 + 2), trunked (EDACS/LTR/
MPT-1327/TETRA), amateur digital (D-STAR/Fusion/M17/APRS/packet), paging
(POCSAG/FLEX/ERMES), ISM (315/433/915/2.4 — LoRa/Z-Wave/Wi-SUN/ZigBee/BT),
cellular/LTE/5G, beacons (EPIRB/radiosonde), and HF/microwave reference
entries (RTTY/PSK31/WSPR/Inmarsat/Iridium/GPS/DECT) that are catalogued for
completeness even though they're outside the RSPduo's normal sweep range.

Extending the catalog is **YAML-only** — the fingerprinter algorithm is
unchanged. No `prompt_v` bump is needed for catalog edits.
