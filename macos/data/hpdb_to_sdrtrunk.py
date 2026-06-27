#!/usr/bin/env python3
"""hpdb_to_sdrtrunk.py — HomePatrol SQLite (trunked P25) → SDRTrunk playlist XML.

Reads the trunked tables from homepatrol.db and emits an SDRTrunk playlist:
  trunk_systems (protocol P25*/P25X2_TDMA)  -> system + a P25 control channel
  trunk_sites                               -> per-site channel config
  trunk_freqs (freq_hz, lcn)                -> control-channel frequency candidates
  trunk_groups + talkgroups (dec_tgid)      -> alias list (talkgroup labels)

Verified for this DB: MTRTRS (trunk_id 7078) + TACN (6355) are protocol
'P25X2_TDMA' — SDRTrunk decodes these (Phase-1 CC + Phase-2 TDMA voice).

⚠️ SCHEMA CAVEAT: the exact SDRTrunk playlist XML element/attribute names + the
P25 decode-config block vary by SDRTrunk version. The structure here is a
best-effort skeleton; **validate against a playlist you export from the installed
SDRTrunk** (Playlist → export) and adjust element names before relying on it.
Also: HPDB `trunk_freqs` has no explicit control-channel flag, so we emit ALL of a
site's frequencies as control-channel CANDIDATES — SDRTrunk auto-detects the live
CC from the list (good enough for P25).

Usage:
  python3 hpdb_to_sdrtrunk.py --db homepatrol.db --system MTRTRS --system TACN -o playlist.xml
  python3 hpdb_to_sdrtrunk.py --db homepatrol.db --list            # list P25 systems
"""
from __future__ import annotations
import argparse, sqlite3, sys, xml.etree.ElementTree as ET
from xml.dom import minidom


def p25_systems(con):
    return con.execute(
        "SELECT trunk_id, system_name, system_type, protocol FROM trunk_systems "
        "WHERE protocol LIKE 'P25%' ORDER BY system_name").fetchall()


def build(con, names):
    pl = ET.Element("playlist", version="4")
    aliases = ET.SubElement(pl, "alias_list")  # talkgroup labels
    chosen = []
    for row in p25_systems(con):
        tid, sysname, stype, proto = row
        if names and not any(n.lower() in (sysname or "").lower() for n in names):
            continue
        chosen.append((tid, sysname, proto))
        # --- control-channel candidates across all sites of this system ---
        ccs = [r[0] for r in con.execute(
            "SELECT DISTINCT tf.freq_hz FROM trunk_freqs tf "
            "JOIN trunk_sites ts ON tf.site_id=ts.site_id WHERE ts.trunk_id=? "
            "ORDER BY tf.freq_hz", (tid,))]
        ch = ET.SubElement(pl, "channel", name=sysname or f"sys{tid}",
                           system=sysname or "", enabled="true")
        dec = ET.SubElement(ch, "decode_config", type="P25_PHASE1")  # verify: SDRTrunk uses P25P1 CC even for P2 voice systems
        ET.SubElement(dec, "protocol").text = proto or "P25"
        cclist = ET.SubElement(ch, "control_channels")
        for hz in ccs:
            ET.SubElement(cclist, "frequency").text = str(int(hz))
        # --- talkgroup aliases for this system ---
        for tg in con.execute(
            "SELECT t.dec_tgid, t.alpha_tag FROM talkgroups t "
            "JOIN trunk_groups g ON t.tgroup_id=g.tgroup_id WHERE g.trunk_id=? "
            "AND t.dec_tgid IS NOT NULL", (tid,)):
            ET.SubElement(aliases, "alias", **{
                "talkgroup": str(tg[0]),
                "name": (tg[1] or "").strip()[:60],
                "system": sysname or "",
            })
    return pl, chosen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--system", action="append", default=[], help="name substring; repeatable")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("-o", "--out", default="-")
    a = ap.parse_args()
    con = sqlite3.connect(a.db)
    if a.list:
        for tid, name, stype, proto in p25_systems(con):
            print(f"  {tid:>6}  {proto:<12}  {name}")
        return
    pl, chosen = build(con, a.system)
    xml = minidom.parseString(ET.tostring(pl)).toprettyxml(indent="  ")
    if a.out == "-":
        sys.stdout.write(xml)
    else:
        open(a.out, "w").write(xml)
    print(f"# systems: {', '.join(c[1] for c in chosen) or '(none matched)'}", file=sys.stderr)
    print("# NOTE: validate element names against an SDRTrunk-exported playlist before use.", file=sys.stderr)


if __name__ == "__main__":
    main()
