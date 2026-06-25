#!/usr/bin/env python3
"""
build_sugar_tree_playlist.py
Generates an SDRTrunk 0.6.1 playlist (playlist version="4") for the "Sugar Tree"
channel list — Sugar Tree / Decatur County, TN (zip 38380).

Layout requested by Will:
  * ANALOG on RSPduo Tuner 1  (GMRS, Marine VHF, Aviation, local VHF public safety)
  * DIGITAL on RSPduo Tuner 2 (TACN P25 Phase II — THP District 8 + TWRA Region 1)

Frequencies/talkgroups sourced from the project HomePatrol DB
(data/homepatrol.db): TN state_id=47, Decatur county_id=2447, Perry 2495,
TACN trunk_id=6355 (nearest site Lobelville, backup Clifton).

NOTE on the RSPduo: in DUAL-tuner mode each tuner is capped at ~1.536 MHz.
That window can't span all the analog bands at once, and can't follow TACN's
~4 MHz of voice channels. So: the local-VHF public-safety cluster is enabled
live on Tuner 1; everything else is defined-but-disabled — enable the cluster
you want per session (each cluster below fits one 1.5 MHz window). See the notes
printed at the end.
"""
import xml.sax.saxutils as su

TUNER1 = "RSPduo SER:180903EF32 Tuner 1"   # analog
TUNER2 = "RSPduo SER:180903EF32 Tuner 2"   # digital
ALIAS_LIST = "Sugar Tree"

RED   = -65536      # 0xFFFF0000  LE / THP
GREEN = -16744448   # 0xFF008000  TWRA
BLUE  = -16776961   # 0xFF0000FF  (unused spare)

def esc(s): return su.escape(str(s))

# ---- analog conventional channels: (name, system, site, freq_hz, mode, bw, enabled) ----
# mode: AM (aviation) or NBFM (everything else)
analog = []

# Local VHF public safety  -- ENABLED live cluster (154.40-155.64, fits one 1.5 MHz window)
for nm, hz in [
    ("Decatur Co Fire Dispatch", 154400000),
    ("Perry Co Fire Dispatch",   154807500),
    ("Decatur Co Rescue/EMS",    155265000),
    ("Perry Co EMS Dispatch",    155527500),
    ("Perry Co Emergency Svcs",  155640000),
]:
    analog.append((nm, "Sugar Tree", "VHF Public Safety", hz, "NBFM", "BW_12_5", True))

# Marine VHF simplex working channels (156.30-157.10, fits one window) -- DISABLED
for nm, hz in [
    ("Marine Ch 06 Intership Safety", 156300000),
    ("Marine Ch 09 Hailing",          156450000),
    ("Marine Ch 13 Bridge-to-Bridge", 156650000),
    ("Marine Ch 16 Distress/Calling", 156800000),
    ("Marine Ch 22A USCG Liaison",    157100000),
    ("Marine Ch 68 Non-Commercial",   156425000),
    ("Marine Ch 69 Non-Commercial",   156475000),
    ("Marine Ch 72 Intership",        156625000),
]:
    analog.append((nm, "Sugar Tree", "Marine VHF", hz, "NBFM", "BW_12_5", False))

# GMRS main repeater/simplex channels (462.55-462.725, fits one window) -- DISABLED
for i, hz in enumerate([462550000,462575000,462600000,462625000,
                        462650000,462675000,462700000,462725000], start=15):
    analog.append((f"GMRS {i} ({hz/1e6:.4f})", "Sugar Tree", "GMRS", hz, "NBFM", "BW_12_5", False))

# Aviation (AM). NOTE: 119-135 MHz spans ~16 MHz -> does NOT fit one 1.5 MHz window;
# enable only the 1-2 you want at a time, or run the RSPduo single-tuner. -- DISABLED
for nm, hz in [
    ("KMKL CTAF / Tower",   127150000),
    ("KMKL Ground",         120900000),
    ("KMKL ASOS",           119325000),
    ("KMKL Unicom",         122950000),
    ("Sibley Unicom",       135075000),
    ("Air Guard 121.5",     121500000),
    ("UNICOM 122.800",      122800000),
    ("MULTICOM 122.900",    122900000),
    ("CTAF 123.000",        123000000),
]:
    analog.append((nm, "Sugar Tree", "Aviation", hz, "AM", "BW_8_33", False))

# ---- digital trunked P25 channels: (name, site, [freqs], enabled) ----
trunked = [
    ("TACN Lobelville (P25 P2)", "Lobelville",
        [769106250, 773031250, 770806250], True),
    ("TACN Clifton (P25 P2)", "Clifton",
        [851325000, 852187500, 852725000, 853100000, 853987500], False),
]

# ---- aliases (talkgroups) ----  (name, group, tgid, color)
aliases = [
    ("THP D8 Dispatch 1", "LE - THP District 8", 47030, RED),
    ("THP D8 Dispatch 2", "LE - THP District 8", 47031, RED),
    ("THP D8 Car-to-Car", "LE - THP District 8", 47032, RED),
    ("THP D8 Event 1",    "LE - THP District 8", 47033, RED),
    ("THP Statewide",     "LE - THP",            801,   RED),
    ("THP Statewide 2",   "LE - THP",            2049,  RED),
    ("TWRA R1 Dispatch 1","TWRA - Region 1 West",15201, GREEN),
    ("TWRA R1 Dispatch 2","TWRA - Region 1 West",15203, GREEN),
    ("TWRA R1 Tactical",  "TWRA - Region 1 West",15204, GREEN),
    ("TWRA R1 Maintenance","TWRA - Region 1 West",15202, GREEN),
    ("TWRA R2 Dispatch 1","TWRA - Region 2 Middle",15205, GREEN),
    ("TWRA R2 Dispatch 2","TWRA - Region 2 Middle",15207, GREEN),
    ("TWRA Comms Tech",   "TWRA - Statewide",     15217, GREEN),
]

def channel_analog(nm, system, site, hz, mode, bw, enabled):
    dc = (f'<decode_configuration type="decodeConfigAM" bandwidth="{bw}" squelch="-50" autoTrack="true" talkgroup="1"/>'
          if mode == "AM" else
          f'<decode_configuration type="decodeConfigNBFM" bandwidth="{bw}" squelch="-50" talkgroup="1"/>')
    return f'''  <channel name="{esc(nm)}" order="1" system="{esc(system)}" site="{esc(site)}" enabled="{str(enabled).lower()}">
    <source_configuration type="sourceConfigTuner" preferred_tuner="{esc(TUNER1)}" frequency="{hz}" source_type="TUNER"/>
    {dc}
    <record_configuration/>
    <alias_list_name>{esc(ALIAS_LIST)}</alias_list_name>
    <event_log_configuration/>
    <aux_decode_configuration/>
  </channel>'''

def channel_trunk(nm, site, freqs, enabled):
    freq_xml = "\n".join(f'      <frequency>{f}</frequency>' for f in freqs)
    return f'''  <channel name="{esc(nm)}" order="1" system="TACN" site="{esc(site)}" enabled="{str(enabled).lower()}">
    <source_configuration type="sourceConfigTunerMultipleFrequency" preferred_tuner="{esc(TUNER2)}" source_type="TUNER_MULTIPLE_FREQUENCIES">
{freq_xml}
    </source_configuration>
    <decode_configuration type="decodeConfigP25Phase2"/>
    <record_configuration/>
    <alias_list_name>{esc(ALIAS_LIST)}</alias_list_name>
    <event_log_configuration/>
    <aux_decode_configuration/>
  </channel>'''

def alias_xml(nm, group, tgid, color):
    return f'''  <alias name="{esc(nm)}" list="{esc(ALIAS_LIST)}" group="{esc(group)}" color="{color}">
    <id type="talkgroup" value="{tgid}" protocol="APCO25"/>
  </alias>'''

parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', '<playlist version="4">']
for c in analog:
    parts.append(channel_analog(*c))
for t in trunked:
    parts.append(channel_trunk(*t))
for a in aliases:
    parts.append(alias_xml(*a))
parts.append('</playlist>')

import os
out = os.path.expanduser("~/SDRTrunk/playlist/sugar_tree.xml")
with open(out, "w") as f:
    f.write("\n".join(parts) + "\n")

print(f"Wrote {out}")
print(f"  analog channels : {len(analog)}  (5 enabled: VHF public safety)")
print(f"  trunked channels: {len(trunked)} (1 enabled: TACN Lobelville)")
print(f"  aliases         : {len(aliases)}")
