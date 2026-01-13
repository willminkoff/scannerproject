import re

RE_AIRBAND = re.compile(r'^\s*airband\s*=\s*(true|false)\s*;\s*$', re.I)
RE_UI_DISABLED = re.compile(r'^\s*ui_disabled\s*=\s*(true|false)\s*;\s*$', re.I)
RE_LOG_SCAN = re.compile(r'^\s*log_scan_activity\s*=', re.I)
RE_STATS_PATH = re.compile(r'^\s*stats_filepath\s*=', re.I)
RE_INDEX = re.compile(r'^\s*index\s*=\s*(\d+)\s*;', re.I)
RE_SERIAL = re.compile(r'^\s*serial\s*=\s*"[^\"]*"\s*;', re.I)
RE_ICECAST_BLOCK = re.compile(r'\{\s*[^{}]*type\s*=\s*"icecast"[^{}]*\}', re.S)
RE_MOUNTPOINT = re.compile(r'(\s*mountpoint\s*=\s*)\"/?([^\"];+)\"(\s*;)', re.I)
RE_BITRATE = re.compile(r'(\s*bitrate\s*=\s*)\d+(\s*;)', re.I)

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
        if RE_LOG_SCAN.match(line):
            continue
        if RE_STATS_PATH.match(line):
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


def override_icecast_bitrate(icecast_block: str, bitrate: int = 16) -> str:
    """Override bitrate in icecast block for lower latency."""
    return RE_BITRATE.sub(rf'\g<1>{bitrate}\g<2>', icecast_block)


def indent_block(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line.rstrip() for line in text.strip().splitlines())


def build_combined_config(airband_path: str, ground_path: str, mixer_name: str) -> str:
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
    # Detect if WX profile is selected for ground
    wx_selected = False
    wx_freq = None
    wx_modulation = None
    wx_bandwidth = None
    wx_squelch = None
    wx_gain = None
    wx_output = None
    # Parse ground profile for WX
    if "freqs = (162.5500)" in ground_text:
        wx_selected = True
        # Extract WX channel block
        match = re.search(r"channels:\s*\(\s*\{([^}]*)\}\s*\)\s*;", ground_text, re.S)
        if match:
            ch_block = match.group(1)
            freq_match = re.search(r"freqs\s*=\s*\(([^)]*)\)", ch_block)
            if freq_match:
                wx_freq = freq_match.group(1).strip()
            mod_match = re.search(r'modulation\s*=\s*"([^"]+)"', ch_block)
            if mod_match:
                wx_modulation = mod_match.group(1)
            bw_match = re.search(r"bandwidth\s*=\s*(\d+)", ch_block)
            if bw_match:
                wx_bandwidth = bw_match.group(1)
            squelch_match = re.search(r"squelch_snr_threshold\s*=\s*([\d.]+)", ch_block)
            if squelch_match:
                wx_squelch = squelch_match.group(1)
            gain_match = re.search(r"gain\s*=\s*([\d.]+)", ground_text)
            if gain_match:
                wx_gain = gain_match.group(1)
            output_match = re.search(r"outputs:\s*\(([^)]*)\)", ch_block, re.S)
            if output_match:
                wx_output = output_match.group(1)
    payloads = [
        (airband_text, 1, airband_disabled, "00000001"),
        (ground_text, 0, ground_disabled, "70613472"),
    ]
    for text, desired_index, disabled, serial in payloads:
        if disabled:
            continue
        payload = extract_devices_payload(text)
        # If WX profile is selected, merge WX channel into ground SDR
        if wx_selected and desired_index == 0 and payload:
            # Find channels block and add WX channel
            # Find insertion point after 'channels:('
            lines = payload.splitlines()
            new_lines = []
            inserted = False
            for line in lines:
                new_lines.append(line)
                if not inserted and "channels:" in line:
                    new_lines.append("      (")
                    # Insert WX channel
                    new_lines.append(f"        {{\n          freqs = ({wx_freq});\n          labels = (\"162.550 WX\");\n          modulation = \"{wx_modulation}\";\n          bandwidth = {wx_bandwidth};\n          squelch_snr_threshold = {wx_squelch};\n          squelch_delay = 0.8;\n          outputs: (\n{wx_output}\n          );\n        }}")
                    inserted = True
            payload = "\n".join(new_lines)
        if payload:
            payload = enforce_device_index(payload, desired_index)
            payload = enforce_device_serial(payload, serial)
            payload = replace_outputs_with_mixer(payload, mixer_name)
            device_payloads.append(payload.strip().rstrip(","))

    icecast_block = (
        "{
"
        "  type = "icecast";
"
        "  server = "127.0.0.1";
"
        "  port = 8000;
"
        "  mountpoint = "GND.mp3";
"
        "  username = "source";
"
        "  password = "062352";
"
        "  name = "SprontPi Radio";
"
        "  genre = "Mixed";
"
        "  bitrate = 16;
"
        "  send_scan_freq_tags = true;
"
        "}
"
    )

    combined = []
    combined.append("# Auto-generated by build-combined-config.py. Do not edit directly.")
    combined.append("")
    combined.append("log_scan_activity = true;")
    combined.append("stats_filepath = \"/run/rtl_airband_stats.txt\";")
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
