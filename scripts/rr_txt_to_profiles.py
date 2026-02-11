#!/usr/bin/env python3
"""Parse a RadioReference TXT export into digital profile scaffolding.

This parser is designed for plain-text exports where site and talkgroup rows
are tab-delimited (for example, TACN.txt saved from RadioReference pages).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path


SITE_HEADER = "Sites and Frequencies"
TALKGROUP_HEADER = "Talkgroups"
_RFSS_RE = re.compile(r"^\d+\s*\([0-9A-Za-z]+\)$")
_FREQ_RE = re.compile(r"\b\d+\.\d+(?:[cC])?\b")


def _normalize(line: str) -> str:
    return (line or "").replace("\ufeff", "").replace("\xa0", " ").rstrip()


def _split_tabs(line: str) -> list[str]:
    return [part.strip() for part in line.split("\t")]


def _freq_tokens(line: str) -> list[tuple[str, bool]]:
    out = []
    for raw in _FREQ_RE.findall(line or ""):
        control = raw.lower().endswith("c")
        freq = raw[:-1] if control else raw
        out.append((freq, control))
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
    for line in lines:
        if "\t" not in line:
            continue
        key, value = line.split("\t", 1)
        key = key.strip()
        if key in keys:
            info[keys[key]] = value.strip()
    return info


def parse_sites(lines: list[str]) -> list[dict]:
    try:
        start = lines.index(SITE_HEADER) + 1
    except ValueError:
        return []

    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].strip() == TALKGROUP_HEADER:
            end = i
            break

    sites = []
    current = None
    for line in lines[start:end]:
        parts = _split_tabs(line)
        if len(parts) >= 4 and _RFSS_RE.match(parts[0]) and _RFSS_RE.match(parts[1]):
            if current:
                sites.append(current)
            current = {
                "rfss": parts[0],
                "site": parts[1],
                "name": parts[2],
                "county": parts[3],
                "frequencies": [],
            }
            for freq, control in _freq_tokens(line):
                current["frequencies"].append({"freq": freq, "control": control})
            continue

        if current is None:
            continue
        for freq, control in _freq_tokens(line):
            current["frequencies"].append({"freq": freq, "control": control})

    if current:
        sites.append(current)
    return sites


def _looks_like_group_name(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    if s in {"Grouped", "All Talkgroups", "New/Updated Talkgroups", "Talkgroups"}:
        return False
    if s.startswith("DEC"):
        return False
    if re.match(r"^\d{1,2}/\d{1,2}", s):
        return False
    lower = s.lower()
    noise_tokens = (
        "covers the following counties",
        "all talkgroups have possibility",
        "may be used for",
        "regional medical communications center",
        "located at",
        "see more at",
        "now in use",
    )
    if any(tok in lower for tok in noise_tokens):
        return False
    # Ignore long county-list lines and obvious plain notes.
    if "," in s and len(s) > 120:
        return False
    return True


def parse_talkgroups(lines: list[str]) -> dict[str, list[dict]]:
    try:
        start = lines.index(TALKGROUP_HEADER) + 1
    except ValueError:
        return {}

    groups: dict[str, list[dict]] = {}
    current_group = "Ungrouped"
    pending_group = ""

    for line in lines[start:]:
        s = line.strip()
        if not s:
            continue

        parts = _split_tabs(line)
        if len(parts) >= 6 and parts[0].isdigit() and re.match(r"^[0-9A-Fa-f]+$", parts[1]):
            groups.setdefault(current_group, []).append(
                {
                    "dec": parts[0],
                    "hex": parts[1],
                    "mode": parts[2],
                    "alpha": parts[3],
                    "description": parts[4],
                    "tag": parts[5],
                }
            )
            continue

        if s.startswith("DEC"):
            if pending_group:
                current_group = pending_group
                groups.setdefault(current_group, [])
            continue

        if _looks_like_group_name(s):
            pending_group = s

    return groups


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["DEC", "HEX", "Mode", "Alpha Tag", "Description", "Tag"])
        for r in rows:
            writer.writerow([r["dec"], r["hex"], r["mode"], r["alpha"], r["description"], r["tag"]])


def write_csv_with_group(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Group", "DEC", "HEX", "Mode", "Alpha Tag", "Description", "Tag"])
        for r in rows:
            writer.writerow([r["group"], r["dec"], r["hex"], r["mode"], r["alpha"], r["description"], r["tag"]])


def dedupe_preserve(seq: list[str]) -> list[str]:
    out = []
    seen = set()
    for item in seq:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--txt", required=True, help="Path to RadioReference TXT export")
    ap.add_argument("--out", required=True, help="Output system directory")
    ap.add_argument("--combined-slug", default="tacn-all", help="Combined profile folder name")
    ap.add_argument(
        "--primary-control",
        default="",
        help="Optional preferred control channel to place first (MHz, e.g. 769.83125)",
    )
    args = ap.parse_args()

    txt_path = Path(args.txt).expanduser()
    out_dir = Path(args.out).expanduser()
    lines = [_normalize(line) for line in txt_path.read_text(encoding="utf-8", errors="ignore").splitlines()]

    info = parse_system_info(lines)
    sites = parse_sites(lines)
    talkgroups = parse_talkgroups(lines)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "system.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    (out_dir / "sites.json").write_text(json.dumps(sites, indent=2), encoding="utf-8")

    control_channels = []
    for site in sites:
        for freq in site.get("frequencies", []):
            if freq.get("control"):
                control_channels.append(str(freq.get("freq", "")).strip())
    control_channels = dedupe_preserve([c for c in control_channels if c])
    if args.primary_control and args.primary_control in control_channels:
        control_channels = [args.primary_control] + [c for c in control_channels if c != args.primary_control]

    combined_rows = []
    for group, rows in talkgroups.items():
        for row in rows:
            combined_rows.append({**row, "group": group})

    combined_dir = out_dir / args.combined_slug
    combined_dir.mkdir(parents=True, exist_ok=True)
    write_csv(
        combined_dir / "talkgroups.csv",
        [{k: r[k] for k in ("dec", "hex", "mode", "alpha", "description", "tag")} for r in combined_rows],
    )
    write_csv_with_group(combined_dir / "talkgroups_with_group.csv", combined_rows)
    (combined_dir / "control_channels.txt").write_text("\n".join(control_channels) + "\n", encoding="utf-8")

    readme = (
        "# TACN Combined Talkgroups\n\n"
        "All talkgroups merged into a single TACN profile from RR TXT export.\n\n"
        f"- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"- Controls: `{len(control_channels)}` channels in `control_channels.txt`\n"
        f"- Talkgroups: `{len(combined_rows)}` rows in `talkgroups.csv`\n"
        "- Grouped reference: `talkgroups_with_group.csv`\n"
    )
    (combined_dir / "README.md").write_text(readme, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
