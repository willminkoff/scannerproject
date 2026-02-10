#!/usr/bin/env python3
"""Parse a RadioReference RTF export into digital profile scaffolding.

This script converts an RTF export to text (using macOS textutil),
extracts site/control channels and talkgroup groups, and generates
per-agency profile folders with talkgroup CSVs plus a shared control
channel list for a selected site.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path


SITE_HEADER = "Sites and Frequencies"
TALKGROUP_HEADER = "Talkgroups"


def _normalize(line: str) -> str:
    return (line or "").replace("\xa0", " ").strip()


def rtf_to_text(path: Path) -> str:
    try:
        out = subprocess.check_output(
            ["textutil", "-convert", "txt", "-stdout", str(path)],
            text=True,
        )
    except FileNotFoundError as exc:
        raise SystemExit("textutil not found (macOS required)") from exc
    return out


def parse_system_info(lines: list[str]) -> dict:
    info = {}
    keys = {
        "System Name:": "system_name",
        "Location:": "location",
        "County:": "county",
        "System Type:": "system_type",
        "System Voice:": "system_voice",
        "System ID:": "system_id",
        "Last Updated:": "last_updated",
    }
    for i, line in enumerate(lines):
        if line in keys and i + 1 < len(lines):
            info[keys[line]] = lines[i + 1]
    return info


def parse_sites(lines: list[str]) -> list[dict]:
    sites = []
    try:
        start = lines.index(SITE_HEADER) + 1
    except ValueError:
        return sites

    # find end
    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i] == TALKGROUP_HEADER:
            end = i
            break

    def next_non_empty(idx: int) -> int:
        i = idx
        while i < end and lines[i] == "":
            i += 1
        return i

    rfss_pat = re.compile(r"^\d+\s*\([0-9A-Za-z]+\)$")
    freq_pat = re.compile(r"^\d+\.\d+(?:[cC])?$")

    i = start
    while i < end:
        i = next_non_empty(i)
        if i >= end:
            break
        line = lines[i]
        j = next_non_empty(i + 1)
        if j < end and rfss_pat.match(line) and rfss_pat.match(lines[j]):
            rfss = line
            site = lines[j]
            name_idx = next_non_empty(j + 1)
            county_idx = next_non_empty(name_idx + 1)
            if county_idx >= end:
                break
            name = lines[name_idx]
            county = lines[county_idx]
            freqs = []
            k = next_non_empty(county_idx + 1)
            while k < end:
                if rfss_pat.match(lines[k]) and rfss_pat.match(lines[next_non_empty(k + 1)]):
                    break
                if lines[k] == TALKGROUP_HEADER:
                    k = end
                    break
                if freq_pat.match(lines[k]):
                    raw = lines[k]
                    control = raw.lower().endswith("c")
                    freq = raw[:-1] if control else raw
                    freqs.append({"freq": freq, "control": control})
                k += 1
            sites.append({
                "rfss": rfss,
                "site": site,
                "name": name,
                "county": county,
                "frequencies": freqs,
            })
            i = k
            continue
        i += 1
    return sites


def parse_talkgroups(lines: list[str]) -> dict[str, list[dict]]:
    records: dict[str, list[dict]] = {}
    try:
        start = lines.index(TALKGROUP_HEADER) + 1
    except ValueError:
        return records

    def next_non_empty(idx: int) -> int:
        i = idx
        while i < len(lines) and lines[i] == "":
            i += 1
        return i

    i = start
    current_group = None
    while i < len(lines):
        i = next_non_empty(i)
        if i >= len(lines):
            break
        line = lines[i]
        if line == "":
            i += 1
            continue
        # group header -> next non-empty is DEC
        j = next_non_empty(i + 1)
        if j < len(lines) and lines[j] == "DEC":
            current_group = line
            records.setdefault(current_group, [])
            # skip header tokens
            i = j
            while i < len(lines) and lines[i] != "Tag":
                i += 1
            i += 1
            continue
        # data record
        if re.match(r"^\d{2,6}$", line):
            dec = line
            i = next_non_empty(i + 1); hexv = lines[i] if i < len(lines) else ""
            i = next_non_empty(i + 1); mode = lines[i] if i < len(lines) else ""
            i = next_non_empty(i + 1); alpha = lines[i] if i < len(lines) else ""
            i = next_non_empty(i + 1); desc = lines[i] if i < len(lines) else ""
            i = next_non_empty(i + 1); tag = lines[i] if i < len(lines) else ""
            if current_group is None:
                current_group = "Ungrouped"
                records.setdefault(current_group, [])
            records[current_group].append({
                "dec": dec,
                "hex": hexv,
                "mode": mode,
                "alpha": alpha,
                "description": desc,
                "tag": tag,
            })
            i += 1
            continue
        i += 1
    return records


def slugify(name: str) -> str:
    name = name.strip().lower()
    name = re.sub(r"[^a-z0-9._@\\-\\s]", "", name)
    name = re.sub(r"\\s+", "-", name)
    name = re.sub(r"-+", "-", name)
    name = re.sub(r"^[^a-z0-9]+", "", name)
    return name[:64] or "unknown"


def is_noise_group(name: str) -> bool:
    lower = name.lower()
    if lower in ("grouped", "all talkgroups", "new/updated talkgroups"):
        return True
    if "talkgroup" in lower:
        return True
    if "as of" in lower:
        return True
    if "no longer" in lower or "moved to" in lower:
        return True
    if "phase ii" in lower or "tdma" in lower or "fdma" in lower:
        return True
    if lower.startswith("see "):
        return True
    if re.search(r"\\b\\d{1,2}/\\d{1,2}\\b", lower):
        return True
    if len(name) > 70:
        return True
    return False


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["DEC", "HEX", "Mode", "Alpha Tag", "Description", "Tag"])
        for r in rows:
            writer.writerow([r["dec"], r["hex"], r["mode"], r["alpha"], r["description"], r["tag"]])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rtf", required=True, help="Path to RadioReference RTF export")
    ap.add_argument("--out", required=True, help="Output directory")
    ap.add_argument("--site", default="Davidson Co Simulcast", help="Site name for control channels")
    args = ap.parse_args()

    rtf_path = Path(args.rtf).expanduser()
    out_dir = Path(args.out).expanduser()
    text = rtf_to_text(rtf_path)
    lines = [_normalize(l) for l in text.splitlines()]

    info = parse_system_info(lines)
    sites = parse_sites(lines)
    talkgroups = parse_talkgroups(lines)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "system.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    (out_dir / "sites.json").write_text(json.dumps(sites, indent=2), encoding="utf-8")

    # control channels for selected site
    control_channels = []
    for site in sites:
        if site.get("name") == args.site:
            control_channels = [f["freq"] for f in site.get("frequencies", []) if f.get("control")]
            break
    (out_dir / "control_channels.txt").write_text("\n".join(control_channels) + "\n", encoding="utf-8")

    # agencies index
    agencies = []
    for group, rows in talkgroups.items():
        if is_noise_group(group):
            continue
        agencies.append({
            "name": group,
            "slug": slugify(group),
            "count": len(rows),
        })
    agencies = sorted(agencies, key=lambda x: x["name"].lower())
    (out_dir / "agencies.json").write_text(json.dumps(agencies, indent=2), encoding="utf-8")
    with (out_dir / "agencies.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Agency", "Slug", "Talkgroup Count"])
        for a in agencies:
            writer.writerow([a["name"], a["slug"], a["count"]])

    # per-agency folders
    for group, rows in talkgroups.items():
        if is_noise_group(group):
            continue
        slug = slugify(group)
        agency_dir = out_dir / slug
        agency_dir.mkdir(parents=True, exist_ok=True)
        write_csv(agency_dir / "talkgroups.csv", rows)
        (agency_dir / "control_channels.txt").write_text("\n".join(control_channels) + "\n", encoding="utf-8")
        readme = (
            f"# {group}\n\n"
            f"Profile scaffold generated from RadioReference RTF export.\n\n"
            f"- Site: {args.site}\n"
            f"- Control channels: `control_channels.txt`\n"
            f"- Talkgroups: `talkgroups.csv`\n\n"
            "Copy your SDRTrunk export into this folder, then import or merge the talkgroups list.\n"
        )
        (agency_dir / "README.md").write_text(readme, encoding="utf-8")

    # summary readme
    summary = (
        f"# {info.get('system_name', 'System')}\n\n"
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"Site used for control channels: **{args.site}**\n\n"
        "This directory contains per-agency profile scaffolds and talkgroup CSVs.\n"
    )
    (out_dir / "README.md").write_text(summary, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
