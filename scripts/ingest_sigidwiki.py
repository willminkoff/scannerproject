#!/usr/bin/env python3
"""Generate disco signature-catalog entries for common signal types.

PR #34. Bulk-augments disco/configs/service_signatures.yaml with widely-known
signal types — many catalogued on the community Signal Identification Wiki
(https://www.sigidwiki.com/wiki/Database) — that the fingerprinter didn't
cover yet. The fingerprinter algorithm is unchanged; this is pure catalog
data.

We took the **manual-composition** route (Option B): rather than scrape
sigidwiki at build time (fragile, rate-limited, and offline here), the
curated table below encodes well-established signal parameters from public
references. Each emitted entry is tagged ``source: manual`` and carries a
reference note, so entries can be audited or removed later.

``allowed_bands`` is computed automatically from the real US band plan
(disco/configs/us_band_plan.yaml): an entry's allowed_bands is exactly the
set of bands its [freq_min, freq_max] overlaps. This guarantees the entries
pass the PR #29 band-scope coverage invariant (no missing / unreachable
bands) without hand-maintaining the lists. Entries whose frequency range
falls outside the band plan's covered range (HF, microwave) are emitted
without allowed_bands (unscoped) — the freq range alone gates them.

Usage:
    python3 scripts/ingest_sigidwiki.py            # print YAML stanzas
    python3 scripts/ingest_sigidwiki.py --append   # append to the catalog

Re-running is safe: entries whose name already exists in the catalog are
skipped.
"""
from __future__ import annotations

import argparse
import os
import sys

import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
_CATALOG = os.path.normpath(os.path.join(_HERE, "..", "disco", "configs", "service_signatures.yaml"))
_BANDPLAN = os.path.normpath(os.path.join(_HERE, "..", "disco", "configs", "us_band_plan.yaml"))

# Curated signal table. Tuples:
#   (name, freq_min_hz, freq_max_hz, bw_min_hz, bw_max_hz, shape, duty, cw, note)
# shape ∈ {narrow_carrier, wide_flat, ofdm_multicarrier, fsk_two_tone,
#          broadband_noise, unknown}; duty ∈ {continuous, bursty, hopping, unknown}.
ENTRIES = [
    # --- Satellite VHF (137-138 MHz, METSAT_VHF) ---
    ("NOAA APT (weather sat)", 137_000_000, 138_000_000, 30_000, 40_000, "wide_flat", "continuous", 0.7, "NOAA POES automatic picture transmission, 137 MHz, ~34 kHz FM."),
    ("Meteor-M LRPT", 137_000_000, 138_000_000, 110_000, 140_000, "wide_flat", "continuous", 0.7, "Russian Meteor-M low-rate picture transmission, QPSK ~120 kHz."),
    ("Orbcomm (downlink)", 137_000_000, 138_000_000, 10_000, 20_000, "narrow_carrier", "bursty", 0.6, "Orbcomm LEO data sat downlink, 137-138 MHz SDPSK."),
    # --- Aviation / data ---
    ("HFDL (HF aircraft data)", 2_800_000, 22_000_000, 1_700, 2_700, "narrow_carrier", "bursty", 0.6, "HF Data Link, aircraft. Out of normal RSPduo VHF range; listed for reference."),
    # --- Marine ---
    ("AIS (marine AIS1/AIS2)", 161_900_000, 162_100_000, 12_000, 16_000, "narrow_carrier", "bursty", 0.85, "Automatic Identification System, 161.975 / 162.025 MHz GMSK 9600."),
    ("NAVTEX", 490_000, 520_000, 300, 600, "narrow_carrier", "bursty", 0.6, "Maritime safety text, 518 kHz FSK. Out of RSPduo range; reference."),
    # --- Amateur digital voice / data (2m + 70cm) ---
    ("APRS (144.39)", 144_000_000, 148_000_000, 12_000, 16_000, "narrow_carrier", "bursty", 0.7, "Automatic Packet Reporting System, 144.39 MHz AFSK 1200 over NFM."),
    ("D-STAR (amateur DV)", 144_000_000, 148_000_000, 5_000, 7_000, "narrow_carrier", "bursty", 0.6, "Amateur digital voice, GMSK 4800; 2m. See 70cm variant."),
    ("D-STAR 70cm (amateur DV)", 420_000_000, 450_000_000, 5_000, 7_000, "narrow_carrier", "bursty", 0.6, "Amateur D-STAR digital voice on 70cm."),
    ("System Fusion C4FM (amateur)", 420_000_000, 450_000_000, 10_000, 13_000, "fsk_two_tone", "bursty", 0.6, "Yaesu System Fusion C4FM digital voice, 70cm."),
    ("M17 (amateur DV)", 420_000_000, 450_000_000, 8_000, 11_000, "fsk_two_tone", "bursty", 0.6, "Open-source amateur digital voice/data, 4FSK 9600, 70cm."),
    ("Packet AX.25 (VHF 1200)", 144_000_000, 148_000_000, 12_000, 16_000, "narrow_carrier", "bursty", 0.6, "Amateur AX.25 packet, AFSK1200 over NFM, 2m."),
    ("Packet AX.25 (UHF 9600)", 420_000_000, 450_000_000, 12_000, 18_000, "fsk_two_tone", "bursty", 0.6, "Amateur AX.25 G3RUH 9600 baud direct FSK, 70cm."),
    # --- Land-mobile trunked / commercial ---
    ("EDACS (trunked)", 450_000_000, 470_000_000, 10_000, 14_000, "narrow_carrier", "continuous", 0.6, "GE/Ericsson EDACS trunked control, UHF. Also exists VHF/800."),
    ("LTR (trunked)", 450_000_000, 470_000_000, 10_000, 14_000, "narrow_carrier", "continuous", 0.6, "Logic Trunked Radio sub-audible, UHF business."),
    ("MPT-1327 (trunked)", 450_000_000, 470_000_000, 10_000, 14_000, "narrow_carrier", "continuous", 0.55, "MPT-1327 analog trunking control, UHF."),
    ("dPMR (446 / UHF)", 450_000_000, 470_000_000, 5_000, 7_000, "fsk_two_tone", "bursty", 0.55, "Digital PMR 6.25 kHz 4FSK, UHF business."),
    ("P25 Phase 2 (TDMA)", 450_000_000, 470_000_000, 10_000, 13_000, "fsk_two_tone", "bursty", 0.7, "P25 Phase 2 H-DQPSK TDMA traffic, UHF LMR. Also 700/800."),
    ("P25 Phase 2 (800 TDMA)", 851_000_000, 869_000_000, 10_000, 13_000, "fsk_two_tone", "bursty", 0.7, "P25 Phase 2 TDMA traffic, 800 MHz public safety."),
    ("TETRA (control)", 450_000_000, 470_000_000, 22_000, 27_000, "ofdm_multicarrier", "continuous", 0.55, "TETRA pi/4-DQPSK 25 kHz; uncommon in US, common abroad."),
    # --- Paging variants (avoid dup with POCSAG/FLEX) ---
    ("ERMES paging", 169_000_000, 170_000_000, 20_000, 30_000, "fsk_two_tone", "bursty", 0.5, "European paging, 169 MHz 4FSK. Rare in US."),
    ("Mobitex (data)", 900_000_000, 941_000_000, 10_000, 14_000, "fsk_two_tone", "bursty", 0.5, "Mobitex packet data network, 900 MHz."),
    # --- Beacons / emergency ---
    ("Radiosonde (weather balloon)", 400_000_000, 406_000_000, 8_000, 14_000, "narrow_carrier", "continuous", 0.7, "Weather balloon telemetry, 400-406 MHz GFSK (RS41 etc)."),
    ("EPIRB / PLB (406)", 406_000_000, 406_100_000, 20_000, 30_000, "narrow_carrier", "bursty", 0.7, "Emergency beacon distress burst, 406 MHz. Transmits briefly."),
    # --- ISM 2.4 GHz family ---
    ("Bluetooth (2.4 GHz)", 2_400_000_000, 2_483_500_000, 900_000, 1_100_000, "fsk_two_tone", "hopping", 0.6, "Bluetooth/BLE FHSS, 1-2 MHz channels, 2.4 GHz. Out of RSPduo range."),
    ("ZigBee / 802.15.4 (2.4)", 2_400_000_000, 2_483_500_000, 1_800_000, 2_200_000, "ofdm_multicarrier", "bursty", 0.6, "ZigBee O-QPSK 2 MHz channels, 2.4 GHz. Out of RSPduo range."),
    # --- ISM 915 family ---
    ("Wi-SUN (915 MHz)", 902_000_000, 928_000_000, 150_000, 320_000, "fsk_two_tone", "bursty", 0.55, "Wi-SUN FAN 2FSK utility mesh, 915 MHz ISM."),
    ("Z-Wave (908 MHz)", 902_000_000, 928_000_000, 30_000, 60_000, "fsk_two_tone", "bursty", 0.55, "Z-Wave home automation, ~908.4 MHz GFSK, 915 ISM."),
    ("RFID active (915)", 902_000_000, 928_000_000, 100_000, 500_000, "unknown", "bursty", 0.4, "Active RFID / EPC Gen2 interrogator, 915 MHz ISM."),
    # --- ISM 433 family ---
    ("LoRa (433 MHz)", 433_050_000, 434_790_000, 100_000, 550_000, "unknown", "bursty", 0.55, "LoRa chirp spread spectrum, 433 MHz ISM (EU/global)."),
    ("Remote keyless entry (315)", 314_000_000, 316_000_000, 10_000, 60_000, "narrow_carrier", "bursty", 0.5, "Car RKE / TPMS OOK/FSK burst, 315 MHz."),
    # --- Broadcast / HD ---
    ("HD Radio (FM IBOC)", 87_900_000, 108_000_000, 100_000, 200_000, "ofdm_multicarrier", "continuous", 0.6, "iBiquity HD Radio OFDM sidebands around an FM carrier."),
    ("DRM (digital HF/MW)", 150_000, 30_000_000, 8_000, 11_000, "ofdm_multicarrier", "continuous", 0.5, "Digital Radio Mondiale, HF/MW OFDM. Out of RSPduo VHF range."),
    # --- Data / telemetry VHF/UHF ---
    ("SCADA telemetry (VHF)", 150_800_000, 174_000_000, 8_000, 16_000, "fsk_two_tone", "bursty", 0.45, "Utility SCADA / remote telemetry FSK, VHF land-mobile."),
    ("SCADA telemetry (UHF)", 450_000_000, 470_000_000, 8_000, 16_000, "fsk_two_tone", "bursty", 0.45, "Utility SCADA / remote telemetry FSK, UHF land-mobile."),
    # --- Government / military VHF-UHF (broad, low confidence) ---
    ("MIL UHF SATCOM (UFO)", 240_000_000, 270_000_000, 5_000, 25_000, "narrow_carrier", "bursty", 0.4, "Military UHF SATCOM downlink, 240-270 MHz. NFM/PSK."),
    ("CW beacon (VHF)", 144_000_000, 148_000_000, 100, 500, "narrow_carrier", "bursty", 0.4, "Amateur CW propagation beacon, very narrow, 2m."),
    # --- Cellular / LTE (covered bands) ---
    ("GSM 850 (downlink)", 869_000_000, 894_000_000, 180_000, 220_000, "ofdm_multicarrier", "continuous", 0.6, "Legacy GSM 850 downlink, 200 kHz GMSK carriers."),
    ("5G NR n71 (600 MHz DL)", 614_000_000, 698_000_000, 5_000_000, 20_000_000, "ofdm_multicarrier", "continuous", 0.6, "T-Mobile 5G NR band n71 downlink, 600 MHz, wide OFDM."),
    # --- Misc ---
    ("Wireless mic (VHF)", 174_000_000, 216_000_000, 80_000, 200_000, "narrow_carrier", "bursty", 0.45, "VHF wireless microphones in the high-VHF TV band, NFM."),
    ("STL studio link (950)", 940_000_000, 960_000_000, 150_000, 300_000, "wide_flat", "continuous", 0.5, "Aural studio-transmitter link, 944-952 MHz, wide FM."),
    ("Inmarsat (L-band)", 1_525_000_000, 1_660_000_000, 5_000, 40_000, "narrow_carrier", "continuous", 0.4, "Inmarsat geostationary L-band. Out of RSPduo range; reference."),
    ("Iridium (L-band)", 1_616_000_000, 1_626_500_000, 30_000, 50_000, "narrow_carrier", "bursty", 0.4, "Iridium LEO L-band bursts. Out of RSPduo range; reference."),
    ("GPS L1 (C/A)", 1_575_000_000, 1_576_000_000, 1_900_000, 2_100_000, "broadband_noise", "continuous", 0.5, "GPS L1 C/A spread-spectrum, ~2 MHz, -130 dBm. Out of range; reference."),
    ("ADS-B (1090, fingerprint)", 1_089_000_000, 1_091_000_000, 50_000, 100_000, "narrow_carrier", "bursty", 0.5, "ADS-B Mode S extended squitter envelope, 1090 MHz. dump1090 owns decode; this is the spectral-shape fallback."),
    ("DECT (cordless phone)", 1_880_000_000, 1_900_000_000, 1_500_000, 1_900_000, "ofdm_multicarrier", "hopping", 0.5, "DECT cordless phone, 1.728 MHz GFSK TDMA. Out of RSPduo range; reference."),
    ("RTTY (HF)", 1_800_000, 30_000_000, 200, 600, "fsk_two_tone", "continuous", 0.4, "Radioteletype 45/50 baud FSK, HF. Out of RSPduo VHF range; reference."),
    ("PSK31 (HF)", 1_800_000, 30_000_000, 31, 100, "narrow_carrier", "continuous", 0.4, "Narrowband keyboard mode, HF. Out of RSPduo range; reference."),
    ("FT8 (HF/VHF digital)", 1_800_000, 148_000_000, 50, 100, "narrow_carrier", "bursty", 0.4, "FT8 weak-signal digital, 15 s bursts, very narrow. HF mostly."),
    ("WSPR (HF beacon)", 1_800_000, 30_000_000, 6, 20, "narrow_carrier", "continuous", 0.4, "Weak-signal propagation reporter, ultra-narrow, HF. Reference."),
    ("Pager FLEX (929 wide)", 929_000_000, 932_000_000, 15_000, 30_000, "fsk_two_tone", "bursty", 0.55, "FLEX paging 1600/3200/6400 bps 4FSK in the 929-932 MHz band."),
    ("Marine DSC (156.525)", 156_400_000, 156_600_000, 1_500, 3_000, "narrow_carrier", "bursty", 0.55, "Digital Selective Calling on marine ch70, 156.525 MHz FSK."),
    ("Railroad ATCS (UHF data)", 450_000_000, 470_000_000, 8_000, 14_000, "fsk_two_tone", "bursty", 0.5, "Advanced Train Control System data, UHF, FSK."),
    ("Itinerant business (UHF)", 450_000_000, 470_000_000, 8_000, 14_000, "narrow_carrier", "bursty", 0.4, "Part 90 itinerant business NFM (MURS-like UHF), licensee via ULS."),
]


def _band_ranges():
    with open(_BANDPLAN) as f:
        bands = yaml.safe_load(f)["bands"]
    return [(b["name"], b["freq_min_hz"], b["freq_max_hz"]) for b in bands]


def _spanning(fmin, fmax, band_ranges):
    return [n for (n, lo, hi) in band_ranges if not (hi <= fmin or lo >= fmax)]


def _existing_names():
    with open(_CATALOG) as f:
        cat = yaml.safe_load(f)
    return {e["name"] for e in (cat or {}).get("signatures", [])}


def build_entries():
    band_ranges = _band_ranges()
    existing = _existing_names()
    out = []
    for (name, fmin, fmax, bwmin, bwmax, shape, duty, cw, note) in ENTRIES:
        if name in existing:
            continue
        entry = {
            "name": name,
            "freq_min_hz": fmin,
            "freq_max_hz": fmax,
            "bandwidth_3db_hz_min": bwmin,
            "bandwidth_3db_hz_max": bwmax,
            "shape": shape,
            "duty_cycle": duty,
            "confidence_weight": cw,
            "source": "manual",
            "notes": note + " (ref: sigidwiki.com/wiki/Database)",
        }
        spanned = _spanning(fmin, fmax, band_ranges)
        if spanned:
            entry["allowed_bands"] = spanned
        out.append(entry)
    return out


def to_yaml_stanzas(entries):
    lines = []
    for e in entries:
        lines.append(f'  - name: "{e["name"]}"')
        lines.append(f'    freq_min_hz: {e["freq_min_hz"]}')
        lines.append(f'    freq_max_hz: {e["freq_max_hz"]}')
        lines.append(f'    bandwidth_3db_hz_min: {e["bandwidth_3db_hz_min"]}')
        lines.append(f'    bandwidth_3db_hz_max: {e["bandwidth_3db_hz_max"]}')
        lines.append(f'    shape: {e["shape"]}')
        lines.append(f'    duty_cycle: {e["duty_cycle"]}')
        lines.append(f'    confidence_weight: {e["confidence_weight"]}')
        lines.append(f'    source: {e["source"]}')
        if "allowed_bands" in e:
            lines.append("    allowed_bands:")
            for b in e["allowed_bands"]:
                lines.append(f"      - {b}")
        lines.append(f'    notes: "{e["notes"]}"')
        lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--append", action="store_true", help="append to the catalog file")
    args = ap.parse_args()
    entries = build_entries()
    stanzas = (
        "\n  # ============================================================\n"
        "  # PR #34 — community / manual signal-type catalog (sigidwiki-\n"
        "  # informed). source: manual on each. allowed_bands computed from\n"
        "  # the US band plan by scripts/ingest_sigidwiki.py.\n"
        "  # ============================================================\n"
        + to_yaml_stanzas(entries)
    )
    if args.append:
        with open(_CATALOG, "a") as f:
            f.write(stanzas)
        print(f"appended {len(entries)} entries to {_CATALOG}", file=sys.stderr)
    else:
        sys.stdout.write(stanzas)


if __name__ == "__main__":
    main()
