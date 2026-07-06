# SDRangel scan — Yuma TN (ZIP 38380), RSPduo

A "cycle a known channel list" scanner for SDRangel covering, from a location at
**Yuma, Carroll County, West-Central TN (ZIP 38380, ~35.8 N / 88.3 W)**:

- **Airband (AM):** Memphis Center (ZME) enroute sectors overhead, plus the
  Nashville (KBNA) and Memphis (KMEM) TRACONs.
- **Ground (NFM):** local severe-weather amateur radio (SKYWARN / ARES) repeaters.

Runs on the **RSPduo** (single tuner). The SDRangel **Frequency Scanner** channel
retunes the device centre per channel, so the full 118–162 MHz span is covered by
one tuner — the RSPduo's ~2 MHz instantaneous window is **not** a limit for a
list scan. Importable list: [`etc/mac/sdrangel/scan-38380.csv`](../etc/mac/sdrangel/scan-38380.csv).

> **Status: VERIFIED WORKING on the RSPduo (2026-06-21).** Built and tested
> end-to-end — device streams, the scanner retunes the RSPduo across the band,
> imports the CSV with the per-row demod mapping intact (AM rows → `R0:0`, NFM
> rows → `R0:1`), measures power per channel, and stops/demodulates active ones.
> The threshold was tuned against the real noise floor (see below).

---

## Frequency list (what's in the CSV)

Confidence + source per the research notes. `Enable=false` rows are loaded for
reference but excluded from the active scan cycle — either continuous broadcasts
(they'd hog a continuous scan) or lower-confidence entries to verify on-air first.

### Airband — AM (demod `R0:0`, ~8 kHz)

| Freq (MHz) | Label | On? | Conf |
|---|---|---|---|
| 121.500 | **GUARD** emergency | ✓ | — |
| 134.650 | Memphis Ctr — McKellar Low (Sec 07), **nearest to Yuma** | ✓ | high |
| 124.350 | Memphis Ctr — McKellar/Holly Springs Low (Sec 14) | ✓ | high |
| 126.450 | Memphis Ctr — Jacks Creek High (Sec 26), overhead | ✓ | med |
| 132.900 | Memphis Ctr — Nashville Low (I-40 east) | ✓ | med |
| 118.400 / 119.350 | Nashville App/Dep East / West (KBNA TRACON) | ✓ | high |
| 118.600 / 121.900 / 126.050 | KBNA Tower / Ground / Clnc-PDC | ✓ | high |
| 135.100 | KBNA D-ATIS (continuous bcast) | ✗ | high |
| 120.600 / 127.175 | Nashville App alt sectors | ✗ | med |
| 119.100 / 125.800 | Memphis Approach (KMEM TRACON) | ✓ | high |
| 124.150 / 124.650 | Memphis Departure | ✓ | high |
| 118.300 / 121.000 / 125.200 | KMEM Tower / Ground / Clnc | ✓ | high |
| 127.750 | KMEM D-ATIS (continuous bcast) | ✗ | high |
| 119.700 / 128.425 | KMEM Tower secondary runways | ✗ | high |

### Ground — NFM (demod `R0:1`, ~12.5 kHz)

| Freq (MHz) | Repeater / use | PL | On? | Conf |
|---|---|---|---|---|
| 146.715 | **KO4PKJ Huntingdon (Carroll Co) — PRIMARY local SKYWARN** | 141.3 | ✓ | high |
| 147.210 | WF4Q Jackson — West TN SKYWARN/ARES Dist 8 | 107.2 | ✓ | high |
| 146.970 | WT4WA Medina — West TN SKYWARN West Primary | 107.2 | ✓ | high |
| 146.700 | WA4YGM Union City — West TN SkyWarn net | 100.0 | ✓ | high |
| 147.360 | KJ4ISZ Paris — SKYWARN net | 123.0 | ✓ | med |
| 146.835 | KF4ZGK Huntingdon — Carroll Co EOC | 123.0 | ✓ | high |
| 146.520 | 2m National Simplex Calling | — | ✓ | high |
| 146.865 | WT4WA Trenton (Gibson) SKYWARN | 127.3? | ✗ | med |
| 146.820 | W4BS Memphis — MEG region-wide SKYWARN (~90 mi, likely out of range) | — | ✗ | high |
| 162.550 | NOAA WX WXK60 Jackson (continuous, RX-only) | — | ✗ | high |

**Carroll County is in NWS Memphis (MEG)'s warning area**, not Nashville (OHX) —
the western boundary of OHX is the Tennessee River. The MEG-designated primary
SKYWARN repeater is W4BS Memphis (146.820), but it's ~90 mi SW; the practical
local net for Yuma is **146.715 (Huntingdon)**.

> PL/CTCSS tones are listed for reference only — a scanner hears the repeater
> **output** regardless of tone. Don't enable tone squelch on the NFM demod
> unless you want to filter. `146.520` is simplex (carrier squelch, no tone).

---

## Setup in SDRangel (apply once, RSPduo connected)

> **Gotcha — plug the SDR in BEFORE launching SDRangel.** SDRangel enumerates
> hardware at startup. If it was already running when you connected the RSPduo,
> the device won't appear in "Add Rx device" — and the in-dialog rescan button
> did **not** pick it up either. **Quit and relaunch SDRangel** with the radio
> already plugged in; then both `SDRPlayV3[0]` and `RTL-SDR[0]` show up.

**Channel add-order matters** — the CSV's per-row `Channel` field assumes the AM
demod is `R0:0` and the NFM demod is `R0:1`, which is what you get if you add
them in this order (confirmed: the import matches these ids and fills the `Ch`
column automatically):

1. **Add the device** — Add Rx device ▸ **`SDRPlayV3[0]`** (Single-tuner /
   Independent mode is fine). Set sample rate ~**2 MS/s**, enable IF AGC or a
   moderate manual gain. (The scanner retunes centre frequency itself.)
2. **Add Channel ▸ AM Demod** → this becomes channel **`R0:0`** (airband).
3. **Add Channel ▸ NFM Demod** → channel **`R0:1`** (ground). Set its RF BW ~12.5 kHz.
4. **Add Channel ▸ Frequency Scanner** → channel `R0:2`.
5. In the Frequency Scanner, **Import** [`scan-38380.csv`](../etc/mac/sdrangel/scan-38380.csv)
   (the import button — "Import frequencies from .csv file"). *File-dialog tip:*
   it's a Qt dialog that treats typed `/` as folder navigation, so typing a full
   path is fiddly — easiest is to drop the CSV in a simple folder (e.g.
   `~/Downloads`) and type just the bare filename.
6. Set the scanner globals (not in the CSV):
   - **Run Mode:** Continuous
   - **Priority:** Table order (top of list wins — Guard/primary SKYWARN are first)
   - **Scan Time (tₛ):** ~0.2–0.5 s per channel
   - **Retransmission (t_rtx):** a few seconds (how long to wait after a signal
     drops before resuming)
   - **Channel (main):** point at `R0:0` (AM) as the default
7. **Verify the per-row `Ch` column** after import: airband rows should show the
   AM demod, ground rows the NFM demod. If any dropdown is **blank** (the
   `R0:0`/`R0:1` ids didn't match your channel layout), just pick the right demod
   from the dropdown — AM for 118–135 MHz rows, NFM for 146–162 MHz rows.
8. **Tune the threshold (TH):** watch the measured Power column; set TH ~8–10 dB
   above the noise floor. The CSV now ships **`-55 dB`**, chosen from the live
   noise floor measured on 2026-06-21 (≈ −65 dBFS across the airband with IF AGC,
   antenna connected). At the original `-70` placeholder *every* channel tripped
   "active" and the scanner parked on the loudest noise; at `-55` the noise-floor
   channels are skipped and only real signals (≈ −45 dB and up) stop the scan.
   Re-tune for your own gain/antenna — the right value is ~10 dB over whatever
   floor the Power column shows you.

### Want one band at a time?
Toggle the **Enable** checkbox per group (or edit the `Enable` column to
`false`). Airband-only = disable the 146–162 MHz rows, and vice-versa.

---

## Caveats / verify-before-relying

- **Aviation:** cross-check the high sectors and the two med-conf Nashville
  approach freqs (120.600 / 127.175) against the current FAA Chart Supplement
  (Southeast) before trusting them. `134.650` (McKellar Low) is the strongest bet
  overhead Yuma — confirmed via the KMKL Chart Supplement entry.
- **TRACON vs Center:** Yuma (~85 nm ENE of KMEM) is generally *outside* the
  Memphis TRACON and inside Memphis **Center** airspace — the TRACON freqs will
  mostly carry Memphis-area traffic, not aircraft directly overhead Yuma.
- **Ham:** verify 146.865 (Trenton) and 147.360 (Paris) tones on-air; both had
  source disagreements. The Carroll County Weather & Emergency Group site
  (cctnwx.org) was down at research time though 146.715 is confirmed on RepeaterBook.
- **RSPduo:** this uses a single tuner. Don't try to run a second simultaneous
  scanner on the other tuner — that re-introduces the dual-tuner contention this
  project has hit before.

## Sources
- Airband: airnav.com (KBNA, KMEM), FAA NFDC, FAA Chart Supplement (KMKL/KSNH),
  globalair.com, seairscan.com (ZME sectors), radioreference.com (ZME).
- Ham/SKYWARN: repeaterbook.com (TN by county), radioreference.com,
  weather.gov/meg SKYWARN pages, West TN SkyWarn (wtnskywarn.wordpress.com).
- SDRangel Frequency Scanner CSV format: f4exb/sdrangel v7.25.1 freqscanner source.
