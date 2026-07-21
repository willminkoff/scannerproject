# Antenna Test Log — 2026-07-11

Bench S11 (return loss / VSWR) tests with a **NanoVNA-H** (DiSlord fw 1.2.44).

- **Calibration:** SOLT, 50 kHz – 900 MHz (reference plane at the VNA CH0 port unless noted).
- **Port 2 (CH1):** terminated with a 50 Ω load throughout.
- **Sweep:** 50 kHz – 900 MHz, 101 points (NanoVNA-H hardware max per sweep); fine sweeps used to pin resonances.
- **Files per test:** `.s1p` (Touchstone S11, real/imag) + `.csv` (freq / S11 / mag / return-loss / VSWR).

VSWR guide: **< 1.5 excellent, < 2.0 good, 2–3 marginal, > 3 poor.**
Note: for **receive-only scanner** use, VSWR matters far less than for transmit — a 3:1 antenna still hears fine.

---

## Summary

| # | Antenna | Intended | Best match (measured) | Verdict |
|---|---------|----------|-----------------------|---------|
| 1 | Zer one wideband glass-mount (SMA-M, 30–1200 MHz) | 30–1200 MHz "full band" | **880 MHz, VSWR 1.03 (36 dB)** | Bench whip is really an **800–900 MHz** antenna. Sub-500 MHz poor — but tested **bare, not on glass**, so low-band figures are not representative of the glass-coupled design. |
| 2 | SDR magnetic-mount telescoping whip (RG58, SMA-M) | Adjustable / broadband | **145 MHz, VSWR 1.14 (23 dB)** | Narrowband, **retunable by whip length**. Tested on bench with **no ground plane** — wants a metal counterpoise for real use. |
| 3 | ABBREE 18.8″ foldable (SMA-F, 144/430 MHz) | 144 / 430 MHz (handheld) | UHF **419 MHz, VSWR 1.23 (20 dB)** | Rated UHF is **real**. VHF (2 m) needs a **counterpoise** — collapses on the bench without one. See two runs below. |

---

## Details

### 1 — Zer one wideband glass-mount (tested BARE)
- Excellent, flat match **810–900 MHz**; best **880 MHz VSWR 1.03**, still improving at the 900 MHz cal edge (center likely > 900).
- Poor below ~550 MHz; effectively deaf below 200 MHz.
- **Caveat:** glass-mount antennas couple capacitively **through window glass** — the low bands can't be judged with the whip bare on the bench. Retest with the puck sandwiched on glass to assess the full 30–1200 MHz claim.
- Good candidate for **800 MHz P25** receive.

### 2 — SDR magnetic-mount telescoping whip
- Sharp resonance **145 MHz VSWR 1.14**; narrow (VSWR ~2 by 167 MHz).
- Additional resonances at 351 / 585 / 819 MHz.
- **Retunable:** shorten whip → resonance up, lengthen → down. Tell me a target band to re-center.
- **Caveat:** mag base needs a **metal ground plane** (car roof / sheet) as counterpoise; bench results shift without one.

### 3 — ABBREE 144/430 foldable — two runs (instructive)
This handheld whip was tested two ways; the difference is a good lesson in feedline + counterpoise effects.

**a) `WITH-25ft-KMR240-cable`** (antenna + 25 ft XRDS-RF KMR240 coax):
- Apparent resonances at **154 MHz (1.10)** and **736 MHz (1.13)**.
- These flatter the antenna: (i) cal plane is at the VNA, so the 25 ft cable is *in* the measurement; (ii) cable loss attenuates the reflection round-trip, **lowering measured VSWR** (worse higher in frequency); (iii) the **coax braid acted as the VHF counterpoise**, creating the strong 154 MHz match.

**b) `DIRECT-no-counterpoise`** (antenna straight to VNA):
- **UHF resonance real: 419 MHz VSWR 1.23** — near rated 430. ✅
- **VHF match collapsed** (VSWR ~1.9–4.4 across 126–180 MHz) — no counterpoise, nothing for the 2 m quarter-wave to work against.
- Strong higher-order resonance at **756 MHz**.
- **In real use on a radio**, the handset body + hand provide the 2 m counterpoise, so VHF works — the bench simply lacks one.

**Takeaways:** calibrate at the antenna (or test direct) to judge the antenna itself; a long feedline's loss makes VSWR look better than it is; a counterpoise-less whip borrows its counterpoise from whatever it's attached to (radio body, coax braid, ground plane).

---

## How to open the data
- `.s1p` — any RF tool: **nanovna-saver**, scikit-rf, Qucs, VNA software.
- `.csv` — spreadsheet; columns: `freq_hz, freq_mhz, s11_re, s11_im, s11_mag, return_loss_db, vswr`.
