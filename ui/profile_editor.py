"""Profile editor helpers for analog and digital sidecar workflows."""

from __future__ import annotations

import csv
import io
import os
import re
from typing import Any

try:
    from .config import DIGITAL_PROFILES_DIR, GROUND_CONFIG_PATH
    from .profile_config import (
        find_profile,
        load_profiles_registry,
        parse_freqs_labels,
        parse_freqs_text,
        read_active_config_path,
        replace_freqs_labels,
        safe_profile_path,
    )
    from .digital import (
        read_digital_talkgroups,
        validate_digital_profile_id,
        write_digital_listen,
    )
except ImportError:
    from ui.config import DIGITAL_PROFILES_DIR, GROUND_CONFIG_PATH
    from ui.profile_config import (
        find_profile,
        load_profiles_registry,
        parse_freqs_labels,
        parse_freqs_text,
        read_active_config_path,
        replace_freqs_labels,
        safe_profile_path,
    )
    from ui.digital import (
        read_digital_talkgroups,
        validate_digital_profile_id,
        write_digital_listen,
    )


_MODULATION_RE = re.compile(r'(^\s*modulation\s*=\s*")[^"]*("\s*;)', re.M)
_BANDWIDTH_RE = re.compile(r'(^\s*bandwidth\s*=\s*)[0-9.]+(\s*;)', re.M)
_VALID_MODULATION_RE = re.compile(r"^[a-z0-9_+\-]{2,16}$", re.I)
_DIGITAL_TGID_MAX = 65535


def _atomic_write_text(path: str, text: str) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _normalize_tgid(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw.isdigit():
        return ""
    try:
        dec = int(raw)
    except Exception:
        return ""
    if dec <= 0 or dec > _DIGITAL_TGID_MAX:
        return ""
    return str(dec)


def _parse_bool_text(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in ("1", "true", "yes", "on", "y", "t"):
        return True
    if text in ("0", "false", "no", "off", "n", "f"):
        return False
    return None


def _normalize_control_channel(text: Any) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    try:
        value = float(raw)
    except Exception:
        return ""
    if value <= 0:
        return ""
    return f"{value:.5f}".rstrip("0").rstrip(".")


def _parse_control_channels_text(control_channels_text: str) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for raw in (control_channels_text or "").splitlines():
        line = str(raw or "")
        line = line.split("#", 1)[0].split(";", 1)[0].strip()
        if not line:
            continue
        token = line.replace(",", " ").split()[0]
        value = _normalize_control_channel(token)
        if not value:
            raise ValueError(f"bad control channel: {raw}")
        if value in seen:
            continue
        seen.add(value)
        items.append(value)
    if not items:
        raise ValueError("no control channels provided")
    return items


def _find_analog_profile(profile_id: str, target: str) -> tuple[dict | None, str | None, str]:
    pid = str(profile_id or "").strip()
    if not pid:
        return None, None, "missing id"

    normalized_target = str(target or "airband").strip().lower()
    if normalized_target not in ("airband", "ground"):
        return None, None, "invalid target"

    profiles = load_profiles_registry()
    profile = find_profile(profiles, pid)
    if not profile:
        return None, None, "profile not found"

    is_airband = bool(profile.get("airband"))
    expected_target = "airband" if is_airband else "ground"
    if expected_target != normalized_target:
        return None, None, f"profile belongs to {expected_target}"

    path = safe_profile_path(str(profile.get("path") or ""))
    if not path or not os.path.isfile(path):
        return None, None, "profile file not found"

    return profile, path, ""


def _read_analog_modulation_bandwidth(path: str) -> tuple[str, int]:
    text = ""
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()

    modulation = "am"
    bandwidth = 12000

    m_mod = re.search(r'^\s*modulation\s*=\s*"([^"]+)"\s*;', text, re.M)
    if m_mod:
        modulation = str(m_mod.group(1) or "am").strip() or "am"

    m_bw = re.search(r'^\s*bandwidth\s*=\s*([0-9.]+)\s*;', text, re.M)
    if m_bw:
        try:
            bandwidth = int(round(float(m_bw.group(1))))
        except Exception:
            bandwidth = 12000

    return modulation, bandwidth


def _format_analog_freqs_text(freqs: list[float], labels: list[str] | None) -> str:
    lines: list[str] = []
    use_labels = bool(labels) and len(labels) == len(freqs)
    for idx, freq in enumerate(freqs):
        freq_text = f"{float(freq):.4f}"
        label = ""
        if use_labels:
            label = str(labels[idx] or "").strip()
        if label:
            lines.append(f"{freq_text} {label}")
        else:
            lines.append(freq_text)
    return "\n".join(lines)


def get_analog_editor_payload(profile_id: str, target: str) -> tuple[bool, str, dict]:
    profile, path, err = _find_analog_profile(profile_id, target)
    if err:
        return False, err, {}

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        freqs, labels = parse_freqs_labels(text)
        modulation, bandwidth = _read_analog_modulation_bandwidth(path)
    except Exception as e:
        return False, str(e), {}

    payload = {
        "ok": True,
        "target": target,
        "profile": {
            "id": profile.get("id", ""),
            "label": profile.get("label", ""),
            "path": path,
            "airband": bool(profile.get("airband")),
        },
        "modulation": modulation,
        "bandwidth": int(bandwidth),
        "freqs": [f"{float(v):.4f}" for v in (freqs or [])],
        "labels": labels or [],
        "freqs_text": _format_analog_freqs_text(freqs or [], labels),
    }
    return True, "", payload


def _apply_analog_modulation_bandwidth_text(
    text: str,
    modulation: str,
    bandwidth: int,
) -> tuple[bool, str, str]:
    if not _VALID_MODULATION_RE.fullmatch(modulation or ""):
        return False, "", "invalid modulation"

    try:
        bw = int(bandwidth)
    except Exception:
        return False, "", "invalid bandwidth"
    if bw < 2000 or bw > 250000:
        return False, "", "bandwidth out of range"

    updated, mod_count = _MODULATION_RE.subn(rf'\1{modulation}\2', text, count=1)
    if mod_count == 0:
        return False, "", "modulation setting not found"

    updated, bw_count = _BANDWIDTH_RE.subn(rf'\g<1>{bw}\2', updated, count=1)
    if bw_count == 0:
        return False, "", "bandwidth setting not found"

    return True, updated, ""


def save_analog_editor_payload(
    profile_id: str,
    target: str,
    freqs_text: str,
    modulation: str,
    bandwidth: int,
) -> tuple[bool, str, dict]:
    profile, path, err = _find_analog_profile(profile_id, target)
    if err:
        return False, err, {}

    try:
        freqs, labels = parse_freqs_text(freqs_text)
    except Exception as e:
        return False, str(e), {}

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            original = f.read()
    except Exception as e:
        return False, str(e), {}

    try:
        updated = replace_freqs_labels(original, freqs, labels)
    except Exception as e:
        return False, str(e), {}

    ok, updated_with_settings, mod_err = _apply_analog_modulation_bandwidth_text(
        updated,
        str(modulation or "").strip().lower(),
        int(bandwidth),
    )
    if not ok:
        return False, mod_err, {}

    changed = updated_with_settings != original
    if changed:
        try:
            _atomic_write_text(path, updated_with_settings)
        except Exception as e:
            return False, str(e), {}

    payload = {
        "ok": True,
        "changed": bool(changed),
        "profile": {
            "id": profile.get("id", ""),
            "label": profile.get("label", ""),
            "path": path,
            "airband": bool(profile.get("airband")),
        },
    }
    return True, "", payload


def _digital_profile_dir(profile_id: str) -> tuple[str, str]:
    pid = str(profile_id or "").strip()
    if not validate_digital_profile_id(pid):
        return "", "invalid profileId"
    base = os.path.realpath(DIGITAL_PROFILES_DIR)
    path = os.path.realpath(os.path.join(DIGITAL_PROFILES_DIR, pid))
    if not base or not path.startswith(base + os.sep):
        return "", "invalid profile path"
    if not os.path.isdir(path):
        return "", "profile not found"
    return path, ""


def _read_control_channels(path: str) -> list[str]:
    if not os.path.isfile(path):
        return []
    items: list[str] = []
    seen: set[str] = set()
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                line = str(raw or "").split("#", 1)[0].split(";", 1)[0].strip()
                if not line:
                    continue
                value = _normalize_control_channel(line)
                if not value or value in seen:
                    continue
                seen.add(value)
                items.append(value)
    except Exception:
        return []
    return items


def _render_talkgroups_text(items: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["DEC", "MODE", "ALPHA", "DESCRIPTION", "TAG", "LISTEN"])
    for item in items or []:
        writer.writerow(
            [
                str(item.get("dec") or "").strip(),
                str(item.get("mode") or "").strip(),
                str(item.get("alpha") or "").strip(),
                str(item.get("description") or "").strip(),
                str(item.get("tag") or "").strip(),
                "1" if bool(item.get("listen")) else "0",
            ]
        )
    return out.getvalue().rstrip()


def get_digital_editor_payload(profile_id: str) -> tuple[bool, str, dict]:
    profile_dir, err = _digital_profile_dir(profile_id)
    if err:
        return False, err, {}

    ok, talkgroups_payload = read_digital_talkgroups(profile_id, max_rows=10000)
    if not ok:
        return False, str(talkgroups_payload), {}

    items = list((talkgroups_payload or {}).get("items") or [])
    controls_path = os.path.join(profile_dir, "control_channels.txt")
    control_channels = _read_control_channels(controls_path)

    payload = {
        "ok": True,
        "profileId": str(profile_id).strip(),
        "talkgroups_source": str((talkgroups_payload or {}).get("source") or ""),
        "control_channels": control_channels,
        "control_channels_text": "\n".join(control_channels),
        "talkgroups_text": _render_talkgroups_text(items),
        "talkgroups_count": len(items),
    }
    return True, "", payload


def _parse_talkgroups_text(talkgroups_text: str) -> tuple[list[dict], bool]:
    text = str(talkgroups_text or "").strip()
    if not text:
        raise ValueError("no talkgroups provided")

    lines = [line for line in text.splitlines() if str(line or "").strip()]
    if not lines:
        raise ValueError("no talkgroups provided")

    first = next(csv.reader([lines[0]]), [])
    first0 = str(first[0] if first else "").strip()
    has_header = not first0.isdigit()

    rows: list[dict] = []
    listen_present = False
    seen_dec: set[str] = set()

    if has_header:
        reader = csv.DictReader(io.StringIO(text))
        for idx, row in enumerate(reader, start=2):
            if not row:
                continue
            norm = {str(k or "").strip().lower(): str(v or "").strip() for k, v in row.items()}
            dec = _normalize_tgid(norm.get("dec") or norm.get("decimal") or norm.get("tgid") or "")
            if not dec:
                raise ValueError(f"invalid DEC at line {idx}")
            if dec in seen_dec:
                raise ValueError(f"duplicate DEC {dec}")
            seen_dec.add(dec)

            mode = (norm.get("mode") or "D").strip().upper() or "D"
            if not re.fullmatch(r"[A-Z0-9]{1,6}", mode):
                raise ValueError(f"invalid mode at line {idx}")

            alpha = norm.get("alpha") or norm.get("alpha tag") or norm.get("alpha_tag") or ""
            description = norm.get("description") or norm.get("desc") or ""
            tag = norm.get("tag") or ""

            listen_raw = norm.get("listen")
            listen_value = _parse_bool_text(listen_raw)
            if listen_raw is not None and str(listen_raw).strip() != "":
                if listen_value is None:
                    raise ValueError(f"invalid listen value at line {idx}")
                listen_present = True

            if not alpha and description:
                alpha = description
            if not description and alpha:
                description = alpha
            if not alpha and not description:
                alpha = f"TG {dec}"
                description = alpha

            rows.append(
                {
                    "dec": dec,
                    "hex": format(int(dec), "x"),
                    "mode": mode,
                    "alpha": str(alpha).strip(),
                    "description": str(description).strip(),
                    "tag": str(tag).strip(),
                    "listen": bool(listen_value) if listen_value is not None else True,
                }
            )
    else:
        reader = csv.reader(io.StringIO(text))
        for idx, raw in enumerate(reader, start=1):
            row = [str(x or "").strip() for x in (raw or [])]
            if not any(row):
                continue
            dec = _normalize_tgid(row[0] if len(row) > 0 else "")
            if not dec:
                raise ValueError(f"invalid DEC at line {idx}")
            if dec in seen_dec:
                raise ValueError(f"duplicate DEC {dec}")
            seen_dec.add(dec)

            mode = (row[1] if len(row) > 1 else "D").upper() or "D"
            if not re.fullmatch(r"[A-Z0-9]{1,6}", mode):
                raise ValueError(f"invalid mode at line {idx}")

            alpha = row[2] if len(row) > 2 else ""
            description = row[3] if len(row) > 3 else ""
            tag = row[4] if len(row) > 4 else ""
            listen_value = _parse_bool_text(row[5] if len(row) > 5 else "")
            if len(row) > 5 and str(row[5]).strip() != "":
                if listen_value is None:
                    raise ValueError(f"invalid listen value at line {idx}")
                listen_present = True

            if not alpha and description:
                alpha = description
            if not description and alpha:
                description = alpha
            if not alpha and not description:
                alpha = f"TG {dec}"
                description = alpha

            rows.append(
                {
                    "dec": dec,
                    "hex": format(int(dec), "x"),
                    "mode": mode,
                    "alpha": str(alpha).strip(),
                    "description": str(description).strip(),
                    "tag": str(tag).strip(),
                    "listen": bool(listen_value) if listen_value is not None else True,
                }
            )

    if not rows:
        raise ValueError("no talkgroups provided")

    return rows, listen_present


def _read_group_map(talkgroups_with_group_path: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not os.path.isfile(talkgroups_with_group_path):
        return mapping
    try:
        with open(talkgroups_with_group_path, "r", encoding="utf-8", errors="ignore", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if not row:
                    continue
                norm = {str(k or "").strip().lower(): str(v or "").strip() for k, v in row.items()}
                dec = _normalize_tgid(norm.get("dec") or norm.get("decimal") or "")
                if not dec:
                    continue
                mapping[dec] = norm.get("group") or ""
    except Exception:
        return {}
    return mapping


def _render_talkgroups_csv(rows: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["DEC", "HEX", "Mode", "Alpha Tag", "Description", "Tag"])
    for row in rows:
        writer.writerow(
            [
                row["dec"],
                row["hex"],
                row["mode"],
                row["alpha"],
                row["description"],
                row["tag"],
            ]
        )
    return out.getvalue()


def _render_talkgroups_with_group_csv(rows: list[dict], groups: dict[str, str]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["Group", "DEC", "HEX", "Mode", "Alpha Tag", "Description", "Tag"])
    for row in rows:
        writer.writerow(
            [
                groups.get(row["dec"], ""),
                row["dec"],
                row["hex"],
                row["mode"],
                row["alpha"],
                row["description"],
                row["tag"],
            ]
        )
    return out.getvalue()


def save_digital_editor_payload(
    profile_id: str,
    control_channels_text: str,
    talkgroups_text: str,
) -> tuple[bool, str, dict]:
    profile_dir, err = _digital_profile_dir(profile_id)
    if err:
        return False, err, {}

    try:
        control_channels = _parse_control_channels_text(control_channels_text)
        tg_rows, listen_present = _parse_talkgroups_text(talkgroups_text)
    except Exception as e:
        return False, str(e), {}

    talkgroups_path = os.path.join(profile_dir, "talkgroups.csv")
    talkgroups_with_group_path = os.path.join(profile_dir, "talkgroups_with_group.csv")
    control_channels_path = os.path.join(profile_dir, "control_channels.txt")

    changed = False

    new_controls_text = "\n".join(control_channels).strip() + "\n"
    old_controls_text = ""
    if os.path.isfile(control_channels_path):
        try:
            with open(control_channels_path, "r", encoding="utf-8", errors="ignore") as f:
                old_controls_text = f.read()
        except Exception:
            old_controls_text = ""
    if new_controls_text != old_controls_text:
        try:
            _atomic_write_text(control_channels_path, new_controls_text)
            changed = True
        except Exception as e:
            return False, str(e), {}

    new_tg_csv = _render_talkgroups_csv(tg_rows)
    old_tg_csv = ""
    if os.path.isfile(talkgroups_path):
        try:
            with open(talkgroups_path, "r", encoding="utf-8", errors="ignore") as f:
                old_tg_csv = f.read()
        except Exception:
            old_tg_csv = ""
    if new_tg_csv != old_tg_csv:
        try:
            _atomic_write_text(talkgroups_path, new_tg_csv)
            changed = True
        except Exception as e:
            return False, str(e), {}

    if os.path.isfile(talkgroups_with_group_path):
        groups = _read_group_map(talkgroups_with_group_path)
        new_tg_with_group = _render_talkgroups_with_group_csv(tg_rows, groups)
        old_tg_with_group = ""
        try:
            with open(talkgroups_with_group_path, "r", encoding="utf-8", errors="ignore") as f:
                old_tg_with_group = f.read()
        except Exception:
            old_tg_with_group = ""
        if new_tg_with_group != old_tg_with_group:
            try:
                _atomic_write_text(talkgroups_with_group_path, new_tg_with_group)
                changed = True
            except Exception as e:
                return False, str(e), {}

    if listen_present:
        listen_items = [{"dec": row["dec"], "listen": bool(row["listen"])} for row in tg_rows]
        ok, listen_err = write_digital_listen(profile_id, listen_items)
        if not ok:
            return False, listen_err or "failed to write listen map", {}

    payload = {
        "ok": True,
        "changed": bool(changed),
        "profileId": str(profile_id).strip(),
        "talkgroups": len(tg_rows),
        "control_channels": len(control_channels),
        "listen_updated": bool(listen_present),
    }
    return True, "", payload


def analog_profile_is_active(path: str) -> bool:
    real = os.path.realpath(str(path or ""))
    if not real:
        return False
    airband = os.path.realpath(read_active_config_path())
    ground = os.path.realpath(GROUND_CONFIG_PATH)
    return real in (airband, ground)
