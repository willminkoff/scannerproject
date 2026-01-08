import re

RE_AIRBAND = re.compile(r'^\s*airband\s*=\s*(true|false)\s*;\s*$', re.I)
RE_UI_DISABLED = re.compile(r'^\s*ui_disabled\s*=\s*(true|false)\s*;\s*$', re.I)
RE_INDEX = re.compile(r'^\s*index\s*=\s*(\d+)\s*;', re.I)
RE_SERIAL = re.compile(r'^\s*serial\s*=\s*"[^\"]*"\s*;', re.I)
RE_ICECAST_BLOCK = re.compile(r'\{\s*[^{}]*type\s*=\s*"icecast"[^{}]*\}', re.S)
RE_MOUNTPOINT = re.compile(r'(\s*mountpoint\s*=\s*)\"/?([^\";]+)\"(\s*;)', re.I)


def extract_top_level_settings(text: str) -> list:
    lines = []
    for line in text.splitlines():
        if line.strip().startswith("devices:"):
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if RE_AIRBAND.match(line):
            continue
        if RE_UI_DISABLED.match(line):
            continue
        lines.append(line.rstrip())
    return lines


def extract_devices_payload(text: str) -> str:
    idx = text.find("devices:")
    if idx == -1:
        return ""
    start = text.find("(", idx)
    if start == -1:
        return ""
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1:i].strip()
    return ""


def enforce_device_index(text: str, desired_index: int) -> str:
    changed = False
    out_lines = []
    insert_at = None
    for idx, line in enumerate(text.splitlines()):
        if insert_at is None and "{" in line:
            insert_at = idx + 1
        match = RE_INDEX.match(line)
        if match:
            out_lines.append(f"  index = {desired_index};")
            changed = True
        else:
            out_lines.append(line)
    if not changed:
        if insert_at is None:
            insert_at = 0
        out_lines.insert(insert_at, f"  index = {desired_index};")
    return "\n".join(out_lines)


def enforce_device_serial(text: str, desired_serial: str) -> str:
    changed = False
    out_lines = []
    insert_at = None
    for idx, line in enumerate(text.splitlines()):
        if insert_at is None and "{" in line:
            insert_at = idx + 1
        if RE_SERIAL.match(line):
            out_lines.append(f"  serial = \"{desired_serial}\";")
            changed = True
        else:
            out_lines.append(line)
    if not changed and desired_serial:
        if insert_at is None:
            insert_at = 0
        out_lines.insert(insert_at, f"  serial = \"{desired_serial}\";")
    return "\n".join(out_lines)


def replace_outputs_with_mixer(text: str, mixer_name: str) -> str:
    replacement = [
        "      outputs:",
        "      (",
        "        {",
        "          type = \"mixer\";",
        f"          name = \"{mixer_name}\";",
        "        }",
        "      );",
    ]
    lines = text.splitlines()
    out_lines = []
    in_outputs = False
    depth = 0
    for line in lines:
        tokens = line.strip().split()
        if not in_outputs and tokens and tokens[0] == "outputs:":
            in_outputs = True
            depth = line.count("(") - line.count(")")
            out_lines.extend(replacement)
            continue
        if in_outputs:
            depth += line.count("(") - line.count(")")
            if depth <= 0 and ");" in line:
                in_outputs = False
            continue
        out_lines.append(line)
    return "\n".join(out_lines)


def normalize_mountpoint(text: str) -> str:
    return RE_MOUNTPOINT.sub(lambda m: f"{m.group(1)}\"{m.group(2)}\"{m.group(3)}", text)


def extract_icecast_block(text: str) -> str:
    match = RE_ICECAST_BLOCK.search(text)
    if not match:
        return ""
    return normalize_mountpoint(match.group(0))


def override_icecast_mountpoint(text: str, mountpoint: str) -> str:
    if not mountpoint:
        return text
    normalized = mountpoint.lstrip("/")
    if RE_MOUNTPOINT.search(text):
        return RE_MOUNTPOINT.sub(
            lambda m: f"{m.group(1)}\"{normalized}\"{m.group(3)}",
            text,
        )
    return text


def indent_block(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line.rstrip() for line in text.strip().splitlines())


def build_combined_config(airband_path: str, ground_path: str, mixer_name: str, icecast_mount: str = "") -> str:
    with open(airband_path, "r", encoding="utf-8", errors="ignore") as f:
        airband_text = f.read()
    with open(ground_path, "r", encoding="utf-8", errors="ignore") as f:
        ground_text = f.read()
    airband_disabled = bool(RE_UI_DISABLED.search(airband_text))
    ground_disabled = bool(RE_UI_DISABLED.search(ground_text))

    top_lines = []
    seen = set()
    for line in extract_top_level_settings(airband_text) + extract_top_level_settings(ground_text):
        if line not in seen:
            seen.add(line)
            top_lines.append(line)

    device_payloads = []
    payloads = [
        (airband_text, 1, airband_disabled, "00000001"),
        (ground_text, 0, ground_disabled, "70613472"),
    ]
    for text, desired_index, disabled, serial in payloads:
        if disabled:
            continue
        payload = extract_devices_payload(text)
        if payload:
            payload = enforce_device_index(payload, desired_index)
            payload = enforce_device_serial(payload, serial)
            payload = replace_outputs_with_mixer(payload, mixer_name)
            device_payloads.append(payload.strip().rstrip(","))

    icecast_block = extract_icecast_block(airband_text) or extract_icecast_block(ground_text)
    if not icecast_block:
        icecast_block = (
            "{\n"
            "  type = \"icecast\";\n"
            "  server = \"127.0.0.1\";\n"
            "  port = 8000;\n"
            "  mountpoint = \"GND.mp3\";\n"
            "  username = \"source\";\n"
            "  password = \"062352\";\n"
            "  name = \"SprontPi Radio\";\n"
            "  genre = \"Mixed\";\n"
            "  bitrate = 32;\n"
            "}"
        )
    if icecast_mount:
        icecast_block = override_icecast_mountpoint(icecast_block, icecast_mount)

    combined = []
    combined.append("# Auto-generated by build-combined-config.py. Do not edit directly.")
    combined.append("")
    combined.extend(top_lines)
    if top_lines:
        combined.append("")
    combined.append("mixers: {")
    combined.append(f"  {mixer_name}: {{")
    combined.append("    outputs:")
    combined.append("    (")
    combined.append(indent_block(icecast_block, 6))
    combined.append("    );")
    combined.append("  };")
    combined.append("};")
    combined.append("")
    combined.append("devices:")
    combined.append("(")
    combined.append(",\n".join(indent_block(payload, 2) for payload in device_payloads))
    combined.append(");")
    combined.append("")
    return "\n".join(combined)
