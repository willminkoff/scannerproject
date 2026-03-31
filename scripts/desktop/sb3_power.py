#!/usr/bin/env python3
"""Standalone desktop power control for the SB3 scanner stack."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile
import time


ENV_FILE_PATH = os.getenv("SB3_POWER_ENV_FILE", "/etc/airband-ui.conf")
ZENITY_BIN = shutil.which("zenity")
PKEXEC_BIN = shutil.which("pkexec")
SUDO_BIN = shutil.which("sudo")

ZENITY_STATUS_WIDTH = "1080"
ZENITY_STATUS_HEIGHT = "760"
ZENITY_CONFIRM_WIDTH = "760"
ZENITY_CONFIRM_HEIGHT = "360"
ZENITY_ACTION_WIDTH = "760"
ZENITY_ACTION_HEIGHT = "420"


@dataclass(frozen=True)
class UnitSpec:
    key: str
    unit: str
    restore_default: bool = False
    holds_tuner: bool = False


def resolve_state_path(state_home: str | None = None) -> Path:
    override = os.getenv("SB3_POWER_STATE_PATH")
    if override:
        return Path(override)
    home = str(state_home or "").strip()
    if not home:
        sudo_user = str(os.getenv("SUDO_USER") or "").strip()
        if sudo_user:
            try:
                home = pwd.getpwnam(sudo_user).pw_dir
            except KeyError:
                home = ""
    if not home:
        pkexec_uid = str(os.getenv("PKEXEC_UID") or "").strip()
        if pkexec_uid.isdigit():
            try:
                home = pwd.getpwuid(int(pkexec_uid)).pw_dir
            except KeyError:
                home = ""
    if not home:
        home = str(Path.home())
    xdg_state_home = os.getenv("XDG_STATE_HOME")
    if xdg_state_home and not state_home:
        base = xdg_state_home
    else:
        base = os.path.join(home, ".local", "state")
    return Path(base) / "sb3-power" / "state.json"


def _strip_quotes(text: str) -> str:
    value = text.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def load_env_file(path: str = ENV_FILE_PATH) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except Exception:
        return env
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        env[key] = _strip_quotes(value)
    return env


def merged_env() -> dict[str, str]:
    env = load_env_file()
    env.update({k: v for k, v in os.environ.items() if v is not None})
    return env


def _selected_digital_unit(values: dict[str, str]) -> str:
    explicit = values.get("UNIT_DIGITAL") or values.get("DIGITAL_SERVICE_NAME")
    if explicit:
        return str(explicit).strip()
    backend = str(values.get("DIGITAL_BACKEND") or "").strip().lower()
    if backend == "op25":
        return str(values.get("OP25_SERVICE_NAME") or "scanner-digital-op25").strip() or "scanner-digital-op25"
    return "scanner-digital"


def build_unit_specs(env: dict[str, str] | None = None) -> list[UnitSpec]:
    values = env or merged_env()
    digital_unit = _selected_digital_unit(values)
    return [
        UnitSpec("icecast", values.get("UNIT_ICECAST", "icecast2"), restore_default=True),
        UnitSpec("keepalive", values.get("UNIT_KEEPALIVE", "icecast-keepalive"), restore_default=True),
        UnitSpec("keepalive_digital", values.get("UNIT_KEEPALIVE_DIGITAL", "icecast-keepalive-digital")),
        UnitSpec("rtl_last_hit", values.get("UNIT_RTL_LAST_HIT", "rtl-airband-last-hit")),
        UnitSpec("rtl", values.get("UNIT_RTL", "rtl-airband"), restore_default=True, holds_tuner=True),
        UnitSpec("ground", values.get("UNIT_GROUND", "rtl-airband-ground"), holds_tuner=True),
        UnitSpec("digital", digital_unit, restore_default=True, holds_tuner=True),
        UnitSpec("digital_mixer", values.get("UNIT_DIGITAL_MIXER", "scanner-digital-mixer")),
        UnitSpec("ui", values.get("UNIT_UI", "airband-ui"), restore_default=True),
    ]


def run_systemctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def unit_exists(unit: str) -> bool:
    result = run_systemctl(["show", "-p", "LoadState", "--value", unit])
    if result.returncode != 0:
        return False
    return (result.stdout or "").strip() != "not-found"


def unit_active(unit: str) -> bool:
    result = run_systemctl(["is-active", unit])
    return result.returncode == 0 and (result.stdout or "").strip() == "active"


def unit_enabled(unit: str) -> bool:
    result = run_systemctl(["is-enabled", unit])
    return result.returncode == 0


def collect_unit_status(specs: list[UnitSpec]) -> dict[str, dict[str, object]]:
    status: dict[str, dict[str, object]] = {}
    for spec in specs:
        exists = unit_exists(spec.unit)
        active = unit_active(spec.unit) if exists else False
        enabled = unit_enabled(spec.unit) if exists else False
        status[spec.key] = {
            "unit": spec.unit,
            "exists": exists,
            "active": active,
            "enabled": enabled,
            "holds_tuner": spec.holds_tuner,
            "restore_default": spec.restore_default,
        }
    return status


def load_state(path: Path) -> dict[str, object]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(payload: dict[str, object], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def default_restore_keys(specs: list[UnitSpec], status: dict[str, dict[str, object]]) -> list[str]:
    restore: list[str] = []
    for spec in specs:
        info = status.get(spec.key) or {}
        if not info.get("exists"):
            continue
        if spec.restore_default or info.get("enabled"):
            restore.append(spec.key)
    return restore


def restore_keys_from_state(
    specs: list[UnitSpec],
    status: dict[str, dict[str, object]],
    state: dict[str, object] | None = None,
) -> list[str]:
    payload = state or {}
    requested = payload.get("restore_units")
    if isinstance(requested, list) and requested:
        allowed = {spec.key for spec in specs}
        restore = [str(key) for key in requested if str(key) in allowed and status.get(str(key), {}).get("exists")]
        if restore:
            return restore
    return default_restore_keys(specs, status)


def summarize_stack_state(status: dict[str, dict[str, object]]) -> str:
    active_keys = [key for key, info in status.items() if info.get("active")]
    core_keys = [key for key in ("icecast", "rtl", "digital", "ui") if status.get(key, {}).get("exists")]
    core_active = [key for key in core_keys if status.get(key, {}).get("active")]
    if not active_keys:
        return "off"
    if core_keys and len(core_active) == len(core_keys):
        return "on"
    return "partial"


def _status_lines(status: dict[str, dict[str, object]]) -> list[str]:
    lines = []
    for key in ("ui", "icecast", "keepalive", "rtl", "ground", "digital", "digital_mixer", "rtl_last_hit", "keepalive_digital"):
        info = status.get(key)
        if not info or not info.get("exists"):
            continue
        state = "active" if info.get("active") else "inactive"
        suffix = " [tuner]" if info.get("holds_tuner") else ""
        lines.append(f"{info['unit']}: {state}{suffix}")
    return lines


def status_summary(
    specs: list[UnitSpec] | None = None,
    state: dict[str, object] | None = None,
    state_path: Path | None = None,
) -> tuple[str, list[str]]:
    unit_specs = specs or build_unit_specs()
    status = collect_unit_status(unit_specs)
    saved = state if state is not None else load_state(state_path or resolve_state_path())
    restore = restore_keys_from_state(unit_specs, status, saved)
    lines = [f"SB3 stack is {summarize_stack_state(status).upper()}."]
    lines.extend(_status_lines(status))
    if restore:
        restore_units = [str(status[key]["unit"]) for key in restore if key in status]
        lines.append("")
        lines.append("Restore set:")
        lines.extend(f"- {unit}" for unit in restore_units)
    return summarize_stack_state(status), lines


def _stop_order() -> list[str]:
    return [
        "ui",
        "digital_mixer",
        "digital",
        "ground",
        "rtl",
        "rtl_last_hit",
        "keepalive_digital",
        "keepalive",
        "icecast",
    ]


def _start_order() -> list[str]:
    return [
        "icecast",
        "keepalive",
        "keepalive_digital",
        "rtl_last_hit",
        "rtl",
        "ground",
        "digital",
        "digital_mixer",
        "ui",
    ]


def _wait_for_state(unit: str, *, expect_active: bool, timeout_sec: float = 20.0) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        active = unit_active(unit)
        if active == expect_active:
            return True
        time.sleep(0.5)
    return unit_active(unit) == expect_active


def _keys_requiring_transition(
    unit_specs: list[UnitSpec],
    status: dict[str, dict[str, object]],
    *,
    expect_active: bool,
    keys: list[str] | None = None,
) -> list[str]:
    selected: list[str] = []
    allowed = set(keys or [])
    for spec in unit_specs:
        if keys is not None and spec.key not in allowed:
            continue
        info = status.get(spec.key) or {}
        if not info.get("exists"):
            continue
        if bool(info.get("active")) == expect_active:
            continue
        selected.append(spec.key)
    return selected


def _ensure_transition(
    unit_specs: list[UnitSpec],
    status: dict[str, dict[str, object]],
    *,
    expect_active: bool,
    keys: list[str],
    action: str,
    lines: list[str],
    failures: list[str],
) -> list[str]:
    pending = list(keys)
    attempts = 2
    for attempt in range(attempts):
        next_pending: list[str] = []
        for key in pending:
            info = status.get(key) or {}
            unit = str(info.get("unit") or "")
            if not unit or not info.get("exists"):
                continue
            if _wait_for_state(unit, expect_active=expect_active):
                continue
            next_pending.append(key)
        if not next_pending:
            return []
        if attempt == attempts - 1:
            return next_pending
        for key in next_pending:
            info = status.get(key) or {}
            unit = str(info.get("unit") or "")
            if not unit:
                continue
            result = run_systemctl([action, unit])
            if result.returncode == 0:
                lines.append(f"Retried {action} for {unit}.")
            else:
                failures.append(f"{unit}: {(result.stderr or result.stdout or '').strip() or f'{action} failed ({result.returncode})'}")
        pending = next_pending
    return pending


def _keys_in_state(
    status: dict[str, dict[str, object]],
    keys: list[str],
    *,
    active: bool,
) -> list[str]:
    matches: list[str] = []
    for key in keys:
        info = status.get(key) or {}
        unit = str(info.get("unit") or "")
        if not unit or not info.get("exists"):
            continue
        if unit_active(unit) == active:
            matches.append(key)
    return matches


def _force_stop_units(
    status: dict[str, dict[str, object]],
    keys: list[str],
    *,
    lines: list[str],
    failures: list[str],
) -> list[str]:
    unresolved: list[str] = []
    for key in keys:
        info = status.get(key) or {}
        unit = str(info.get("unit") or "")
        if not unit or not info.get("exists"):
            continue
        kill_result = run_systemctl(["kill", "--kill-who=all", "--signal=SIGKILL", unit])
        if kill_result.returncode == 0:
            lines.append(f"Forced kill for {unit}.")
        stop_result = run_systemctl(["stop", unit])
        if stop_result.returncode != 0:
            failures.append(f"{unit}: {(stop_result.stderr or stop_result.stdout or '').strip() or f'stop failed ({stop_result.returncode})'}")
            unresolved.append(key)
            continue
        lines.append(f"Retried stop for {unit} after force-kill.")
        if not _wait_for_state(unit, expect_active=False, timeout_sec=8.0):
            unresolved.append(key)
    return unresolved


def stop_stack(specs: list[UnitSpec] | None = None, state_path: Path | None = None) -> tuple[bool, list[str]]:
    unit_specs = specs or build_unit_specs()
    state_path = state_path or resolve_state_path()
    status = collect_unit_status(unit_specs)
    active_restore = [spec.key for spec in unit_specs if status.get(spec.key, {}).get("active")]
    if active_restore:
        save_state(
            {
                "saved_at": time.time(),
                "restore_units": active_restore,
                "units": status,
            },
            path=state_path,
        )
    elif not state_path.exists():
        save_state(
            {
                "saved_at": time.time(),
                "restore_units": default_restore_keys(unit_specs, status),
                "units": status,
            },
            path=state_path,
        )

    lines = []
    failures = []
    keys_to_stop = _keys_requiring_transition(unit_specs, status, expect_active=False)
    for key in _stop_order():
        info = status.get(key)
        if not info or not info.get("exists") or not info.get("active"):
            continue
        unit = str(info["unit"])
        result = run_systemctl(["stop", unit])
        if result.returncode != 0:
            failures.append(f"{unit}: {(result.stderr or result.stdout or '').strip() or f'stop failed ({result.returncode})'}")
            continue
        lines.append(f"Stopped {unit}.")

    unresolved = _ensure_transition(
        unit_specs,
        status,
        expect_active=False,
        keys=keys_to_stop,
        action="stop",
        lines=lines,
        failures=failures,
    )
    if unresolved:
        still_active = _keys_in_state(status, unresolved, active=True)
        if still_active:
            lines.append("Escalating stop for lingering SB3 services...")
            unresolved = _force_stop_units(status, still_active, lines=lines, failures=failures)
        unresolved = _keys_in_state(status, unresolved, active=True)
        for key in unresolved:
            unit = str((status.get(key) or {}).get("unit") or key)
            failures.append(f"{unit}: did not reach inactive state")

    if failures:
        lines.append("")
        lines.append("Errors:")
        lines.extend(f"- {item}" for item in failures)
        return False, lines

    if not lines:
        lines.append("SB3 stack was already off.")
    else:
        lines.append("")
        lines.append("Tuner-owning services are inactive; RTL dongles are released.")
    return True, lines


def start_stack(specs: list[UnitSpec] | None = None, state_path: Path | None = None) -> tuple[bool, list[str]]:
    unit_specs = specs or build_unit_specs()
    state_path = state_path or resolve_state_path()
    status = collect_unit_status(unit_specs)
    saved = load_state(state_path)
    restore = restore_keys_from_state(unit_specs, status, saved)
    restore_set = set(restore)
    lines = []
    failures = []
    if not restore:
        return False, ["No restore set is available for SB3."]

    for key in _start_order():
        if key not in restore_set:
            continue
        info = status.get(key)
        if not info or not info.get("exists"):
            continue
        unit = str(info["unit"])
        result = run_systemctl(["start", unit])
        if result.returncode != 0:
            failures.append(f"{unit}: {(result.stderr or result.stdout or '').strip() or f'start failed ({result.returncode})'}")
            continue
        lines.append(f"Started {unit}.")

    unresolved = _ensure_transition(
        unit_specs,
        status,
        expect_active=True,
        keys=restore,
        action="start",
        lines=lines,
        failures=failures,
    )
    if unresolved:
        for key in unresolved:
            unit = str((status.get(key) or {}).get("unit") or key)
            failures.append(f"{unit}: did not reach active state")

    if failures:
        lines.append("")
        lines.append("Errors:")
        lines.extend(f"- {item}" for item in failures)
        return False, lines

    lines.append("")
    lines.append("SB3 stack restored.")
    return True, lines


def toggle_stack(specs: list[UnitSpec] | None = None, state_path: Path | None = None) -> tuple[bool, list[str]]:
    unit_specs = specs or build_unit_specs()
    status = collect_unit_status(unit_specs)
    state = summarize_stack_state(status)
    if state == "off":
        return start_stack(unit_specs, state_path=state_path)
    return stop_stack(unit_specs, state_path=state_path)


def _verify_outcome(command: str, specs: list[UnitSpec], state_path: Path) -> tuple[bool, list[str]]:
    status = collect_unit_status(specs)
    if command == "off":
        active_units = [
            str(info["unit"])
            for info in status.values()
            if info.get("exists") and info.get("active")
        ]
        if active_units:
            return False, ["SB3 is not fully off yet. Still active:"] + [f"- {unit}" for unit in active_units]
        return True, []
    if command == "on":
        restore = restore_keys_from_state(specs, status, load_state(state_path))
        missing = [
            str((status.get(key) or {}).get("unit") or key)
            for key in restore
            if status.get(key, {}).get("exists") and not status.get(key, {}).get("active")
        ]
        if missing:
            return False, ["SB3 restore is incomplete. Still inactive:"] + [f"- {unit}" for unit in missing]
        return True, []
    return True, []


def _result_lines(result: subprocess.CompletedProcess[str]) -> list[str]:
    lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    err_lines = [line for line in (result.stderr or "").splitlines() if line.strip()]
    if err_lines:
        lines.extend(err_lines)
    return lines


def _sudo_with_askpass(helper_args: list[str]) -> subprocess.CompletedProcess[str] | None:
    if not (SUDO_BIN and ZENITY_BIN and os.getenv("DISPLAY")):
        return None
    askpass_path = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write("#!/bin/sh\n")
            handle.write(f'exec "{ZENITY_BIN}" --password --title="SB3 Power Control Authentication"\n')
            askpass_path = handle.name
        os.chmod(askpass_path, 0o700)
        env = os.environ.copy()
        env["SUDO_ASKPASS"] = askpass_path
        env["SUDO_ASKPASS_REQUIRE"] = "force"
        return subprocess.run(
            [SUDO_BIN, "-A", *helper_args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            env=env,
        )
    except Exception:
        return None
    finally:
        if askpass_path:
            try:
                os.unlink(askpass_path)
            except OSError:
                pass


def invoke_elevated(command: str, state_home: str | None = None) -> tuple[bool, list[str]]:
    helper_args = [sys.executable, str(Path(__file__).resolve()), command, "--elevated-helper"]
    if state_home:
        helper_args.extend(["--state-home", state_home])
    failure_notes: list[str] = []
    result: subprocess.CompletedProcess[str] | None = None

    def _record_failure(label: str, proc: subprocess.CompletedProcess[str]) -> None:
        lines = _result_lines(proc)
        if lines:
            failure_notes.append(f"{label}: {lines[0]}")
            failure_notes.extend(lines[1:])
        else:
            failure_notes.append(f"{label}: command returned code {proc.returncode}")

    if os.geteuid() == 0:
        result = subprocess.run(helper_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    else:
        display = bool(os.getenv("DISPLAY"))
        if display and PKEXEC_BIN:
            proc = subprocess.run(
                [PKEXEC_BIN, *helper_args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if proc.returncode == 0:
                result = proc
            else:
                _record_failure("pkexec", proc)
        if result is None and SUDO_BIN:
            sudo_args = [SUDO_BIN, *helper_args] if sys.stdin.isatty() else [SUDO_BIN, "-n", *helper_args]
            proc = subprocess.run(sudo_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
            if proc.returncode == 0:
                result = proc
            else:
                _record_failure("sudo", proc)
                if not sys.stdin.isatty():
                    askpass_proc = _sudo_with_askpass(helper_args)
                    if askpass_proc is not None:
                        if askpass_proc.returncode == 0:
                            result = askpass_proc
                        else:
                            _record_failure("sudo-askpass", askpass_proc)
    if result is None:
        return False, ["Unable to elevate privileges for SB3 power control."]

    lines = _result_lines(result)
    if result.returncode != 0 and failure_notes:
        lines.extend(item for item in failure_notes if item not in lines)
    if not lines:
        lines.append(f"SB3 {command} command returned code {result.returncode}.")
    ok = result.returncode == 0
    if ok:
        verify_ok, verify_lines = _verify_outcome(command, build_unit_specs(), resolve_state_path(state_home))
        if not verify_ok:
            lines.append("")
            lines.extend(verify_lines)
        ok = verify_ok
    return ok, lines


def _supports_zenity() -> bool:
    return bool(ZENITY_BIN and os.getenv("DISPLAY"))


def _run_zenity(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [ZENITY_BIN, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def _show_scrollable_text(title: str, text: str) -> None:
    if not _supports_zenity():
        print(text)
        return
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(text.rstrip() + "\n")
        temp_path = handle.name
    try:
        _run_zenity(
            [
                "--text-info",
                "--title",
                title,
                f"--width={ZENITY_STATUS_WIDTH}",
                f"--height={ZENITY_STATUS_HEIGHT}",
                "--filename",
                temp_path,
                "--font=14",
                "--ok-label=Close",
            ]
        )
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def _confirm_action(action: str, state: str) -> bool:
    if not _supports_zenity():
        return True
    if action == "off":
        text = (
            "Turn SB3 OFF?\n\n"
            "This stops the UI, Icecast, analog scanning, and digital scanning,\n"
            "then releases the RTL dongles for other SDR software."
        )
        ok_label = "Turn Off"
    else:
        text = (
            "Turn SB3 ON?\n\n"
            "This restores the last saved SB3 service set and reclaims the RTL dongles\n"
            "for the scanner stack."
        )
        ok_label = "Turn On"
    result = _run_zenity(
        [
            "--question",
            "--title=SB3 Power Control",
            f"--width={ZENITY_CONFIRM_WIDTH}",
            f"--height={ZENITY_CONFIRM_HEIGHT}",
            "--text",
            text,
            "--ok-label",
            ok_label,
            "--cancel-label=Cancel",
        ]
    )
    return result.returncode == 0


def _choose_action_text(state: str, lines: list[str]) -> str | None:
    print("\n".join(lines))
    print("")
    if state == "off":
        prompt = "Choose action [on/status/cancel]: "
        allowed = {"on", "status", "cancel"}
    elif state == "on":
        prompt = "Choose action [off/status/cancel]: "
        allowed = {"off", "status", "cancel"}
    else:
        prompt = "Choose action [off/on/status/cancel]: "
        allowed = {"off", "on", "status", "cancel"}
    while True:
        try:
            selected = input(prompt).strip().lower()
        except EOFError:
            return None
        if not selected:
            return "status"
        if selected in {"q", "quit", "exit", "cancel"}:
            return None
        if selected in {"s", "status"} and "status" in allowed:
            return "status"
        if selected in {"on", "start"} and "on" in allowed:
            return "on"
        if selected in {"off", "stop"} and "off" in allowed:
            return "off"
        choices = "/".join(item for item in ("off", "on", "status", "cancel") if item in allowed)
        print(f"Enter one of: {choices}.")


def _confirm_action_text(action: str) -> bool:
    prompt = "Confirm turn SB3 OFF? [y/N]: " if action == "off" else "Confirm turn SB3 ON? [y/N]: "
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def _choose_action(state: str) -> str | None:
    if not _supports_zenity():
        return None
    options: list[tuple[str, str]]
    if state == "off":
        options = [
            ("TRUE", "Turn SB3 On"),
            ("FALSE", "Show Status"),
            ("FALSE", "Turn SB3 Off"),
        ]
    else:
        options = [
            ("TRUE", "Turn SB3 Off"),
            ("FALSE", "Show Status"),
            ("FALSE", "Turn SB3 On"),
        ]
    args = [
        "--list",
        "--radiolist",
        "--title=SB3 Power Control",
        f"--width={ZENITY_ACTION_WIDTH}",
        f"--height={ZENITY_ACTION_HEIGHT}",
        "--text",
        f"SB3 is currently {state.upper()}. Choose an action.",
        "--hide-header",
        "--column",
        "",
        "--column",
        "Action",
    ]
    for selected, label in options:
        args.extend([selected, label])
    result = _run_zenity(args)
    if result.returncode != 0:
        return None
    selected = (result.stdout or "").strip().lower()
    if selected == "turn sb3 off":
        return "off"
    if selected == "turn sb3 on":
        return "on"
    if selected == "show status":
        return "status"
    return None


def run_ui() -> int:
    specs = build_unit_specs()
    state_path = resolve_state_path()
    state, lines = status_summary(specs=specs, state_path=state_path)
    if not _supports_zenity():
        if sys.stdin.isatty():
            action = _choose_action_text(state, lines)
            if not action:
                return 1
            if action == "status":
                return 0
            if not _confirm_action_text(action):
                return 1
            ok, output = invoke_elevated(action, state_home=str(Path.home()))
            print("")
            print("\n".join(output))
            return 0 if ok else 1
        print("\n".join(lines))
        print("")
        print("Run with: on | off | toggle | status")
        return 0

    action = _choose_action(state)
    if not action:
        return 1
    if action == "status":
        _show_scrollable_text("SB3 Status", "\n".join(lines))
        return 0
    if not _confirm_action(action, state):
        return 1
    if action == "on":
        ok, output = invoke_elevated("on", state_home=str(Path.home()))
    elif action == "off":
        ok, output = invoke_elevated("off", state_home=str(Path.home()))
    else:
        ok, output = True, lines
    _show_scrollable_text("SB3 Power Control", "\n".join(output))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Desktop power control for the SB3 scanner stack.")
    parser.add_argument(
        "command",
        nargs="?",
        default="ui",
        choices=("ui", "status", "on", "off", "toggle"),
        help="Action to perform.",
    )
    parser.add_argument("--elevated-helper", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--state-home", default="", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    specs = build_unit_specs()
    state_path = resolve_state_path(args.state_home or None)
    if args.command == "status":
        _, lines = status_summary(specs=specs, state_path=state_path)
        print("\n".join(lines))
        return 0
    if args.command == "on":
        ok, lines = (
            start_stack(specs, state_path=state_path)
            if args.elevated_helper or os.geteuid() == 0
            else invoke_elevated("on", state_home=str(Path.home()))
        )
        print("\n".join(lines))
        return 0 if ok else 1
    if args.command == "off":
        ok, lines = (
            stop_stack(specs, state_path=state_path)
            if args.elevated_helper or os.geteuid() == 0
            else invoke_elevated("off", state_home=str(Path.home()))
        )
        print("\n".join(lines))
        return 0 if ok else 1
    if args.command == "toggle":
        ok, lines = (
            toggle_stack(specs, state_path=state_path)
            if args.elevated_helper or os.geteuid() == 0
            else invoke_elevated("toggle", state_home=str(Path.home()))
        )
        print("\n".join(lines))
        return 0 if ok else 1
    return run_ui()


if __name__ == "__main__":
    sys.exit(main())
