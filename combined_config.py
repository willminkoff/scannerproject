import re

RE_AIRBAND = re.compile(r'^\s*airband\s*=\s*(true|false)\s*;\s*$', re.I)
RE_UI_DISABLED = re.compile(r'^\s*ui_disabled\s*=\s*(true|false)\s*;\s*$', re.I)
RE_LOG_SCAN = re.compile(r'^\s*log_scan_activity\s*=', re.I)
RE_STATS_PATH = re.compile(r'^\s*stats_filepath\s*=', re.I)
RE_SQUELCH_THRESHOLD = re.compile(r'^\s*squelch_threshold\s*=', re.I)
RE_SQUELCH_SNR_THRESHOLD = re.compile(r'^\s*squelch_snr_threshold\s*=', re.I)
RE_INDEX = re.compile(r'^\s*index\s*=\s*(\d+)\s*;', re.I)
RE_SERIAL = re.compile(r'^\s*serial\s*=\s*"[^\"]*"\s*;', re.I)
RE_ICECAST_BLOCK = re.compile(r'\{\s*[^{}]*type\s*=\s*"icecast"[^{}]*\}', re.S)
RE_MOUNTPOINT = re.compile(r'(\s*mountpoint\s*=\s*)\"/?([^\";]+)\"(\s*;)', re.I)
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
        # Never carry profile-specific squelch controls into global top-level.
        if RE_SQUELCH_THRESHOLD.match(line):
            continue
        if RE_SQUELCH_SNR_THRESHOLD.match(line):
            continue
        lines.append(line.rstrip())
    return lines


def profile_ui_disabled(text: str) -> bool:
    """Return True only when ui_disabled is explicitly set to true."""
    match = RE_UI_DISABLED.search(text)
    return bool(match and match.group(1).lower() == "true")


def extract_devices_payload(text: str) -> str:
    idx = text.find("devices:")
    if idx == -1:
        return ""
    start = text.find("(", idx)
    if start == -1:
        return ""
    return _extract_parenthesized_payload(text, start)


def _extract_parenthesized_payload(text: str, start: int) -> str:
    """Extract the payload inside a parenthesized block.

    The rtl_airband config frequently contains parentheses in quoted labels
    and comments; those must not affect structural depth tracking.
    """
    depth = 0
    in_string = False
    in_line_comment = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            continue

        if in_string:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == "\"":
                in_string = False
            continue

        if ch == "\"":
            in_string = True
            continue

        if ch == "#":
            in_line_comment = True
            continue
        if ch == "/" and nxt == "/":
            in_line_comment = True
            continue

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


def replace_outputs_with_mixer(text: str, mixer_name: str, continuous: bool = True) -> str:
    continuous_value = "true" if continuous else "false"
    replacement = [
        "      outputs:",
        "      (",
        "        {",
        "          type = \"mixer\";",
        f"          name = \"{mixer_name}\";",
        f"          continuous = {continuous_value};",
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


def override_icecast_bitrate(icecast_block: str, bitrate: int = 32) -> str:
    """Override bitrate in icecast block for lower latency."""
    return RE_BITRATE.sub(rf'\g<1>{bitrate}\g<2>', icecast_block)


def override_icecast_mountpoint(icecast_block: str, mountpoint: str) -> str:
    if not mountpoint:
        return icecast_block
    mount = mountpoint.strip().lstrip("/")
    if not mount:
        return icecast_block
    return RE_MOUNTPOINT.sub(lambda m: f'{m.group(1)}"{mount}"{m.group(3)}', icecast_block)


def upsert_icecast_bool_option(icecast_block: str, option: str, enabled: bool) -> str:
    value = "true" if enabled else "false"
    pattern = re.compile(rf'(\s*{re.escape(option)}\s*=\s*)(true|false)(\s*;)', re.I)
    if pattern.search(icecast_block):
        return pattern.sub(rf'\g<1>{value}\g<3>', icecast_block)

    lines = icecast_block.splitlines()
    insert_idx = len(lines)
    indent = "  "
    for idx, line in enumerate(lines):
        if RE_BITRATE.search(line):
            insert_idx = idx + 1
            indent = re.match(r'^\s*', line).group(0)
            break
    if insert_idx == len(lines):
        for idx, line in enumerate(lines):
            if line.strip() == "}":
                insert_idx = idx
                indent = re.match(r'^\s*', line).group(0) + "  "
                break
    lines.insert(insert_idx, f"{indent}{option} = {value};")
    return "\n".join(lines)


def indent_block(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line.rstrip() for line in text.strip().splitlines())


def build_combined_config(
    airband_path: str,
    ground_path: str,
    mixer_name: str,
    mount_name: str = "",
    analog_continuous: bool = True,
    mixer_output_continuous: bool = True,
    analog_bitrate_kbps: int = 64,
) -> str:
    with open(airband_path, "r", encoding="utf-8", errors="ignore") as f:
        airband_text = f.read()
    with open(ground_path, "r", encoding="utf-8", errors="ignore") as f:
        ground_text = f.read()
    airband_disabled = profile_ui_disabled(airband_text)
    ground_disabled = profile_ui_disabled(ground_text)

    top_lines = []
    seen_setting_keys = set()
    for line in extract_top_level_settings(airband_text) + extract_top_level_settings(ground_text):
        setting_key = line.split("=", 1)[0].strip().lower()
        if not setting_key or setting_key in seen_setting_keys:
            continue
        seen_setting_keys.add(setting_key)
        top_lines.append(line)

    device_payloads = []
    payloads = [
        (airband_text, 1, airband_disabled, "00000002"),
        (ground_text, 0, ground_disabled, "70613472"),
    ]
    for text, desired_index, disabled, serial in payloads:
        if disabled:
            continue
        payload = extract_devices_payload(text)
        if payload:
            payload = enforce_device_index(payload, desired_index)
            payload = enforce_device_serial(payload, serial)
            payload = replace_outputs_with_mixer(payload, mixer_name, continuous=mixer_output_continuous)
            device_payloads.append(payload.strip().rstrip(","))

    icecast_block = extract_icecast_block(airband_text) or extract_icecast_block(ground_text)
    try:
        normalized_bitrate_kbps = int(analog_bitrate_kbps)
    except Exception:
        normalized_bitrate_kbps = 64
    normalized_bitrate_kbps = max(8, min(320, normalized_bitrate_kbps))
    if not icecast_block:
        icecast_block = (
            "{\n"
            "  type = \"icecast\";\n"
            "  server = \"127.0.0.1\";\n"
            "  port = 8000;\n"
            "  mountpoint = \"scannerbox.mp3\";\n"
            "  username = \"source\";\n"
            "  password = \"062352\";\n"
            "  name = \"SprontPi Radio\";\n"
            "  genre = \"Mixed\";\n"
            f"  bitrate = {normalized_bitrate_kbps};\n"
            "  send_scan_freq_tags = true;\n"
            "}\n"
        )
    else:
        # Keep analog stream at a desktop-browser-friendly encoding profile.
        icecast_block = override_icecast_bitrate(icecast_block, normalized_bitrate_kbps)
    icecast_block = upsert_icecast_bool_option(icecast_block, "continuous", analog_continuous)
    if mount_name:
        icecast_block = override_icecast_mountpoint(icecast_block, mount_name)

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
