"""System unit control via systemd."""
import json
import os
import subprocess
import threading
import time
from typing import Optional, Tuple
from urllib.error import URLError
from urllib.request import urlopen

try:
    from .config import (
        UNITS,
        BT_HEAL_TIMER_UNIT,
        RTL_AIRBAND_STATS_PATH,
        RTL_AIRBAND_STATS_STALE_SEC,
    )
    from .sample_flow import rtl_airband_sample_flow_state
except ImportError:
    from ui.config import (
        UNITS,
        BT_HEAL_TIMER_UNIT,
        RTL_AIRBAND_STATS_PATH,
        RTL_AIRBAND_STATS_STALE_SEC,
    )
    from ui.sample_flow import rtl_airband_sample_flow_state


_TRUTHY = ("1", "true", "yes", "on")

# digital restart health state. Updated by restart_digital() on every
# invocation + post-start probe. Surfaced via digital_restart_state() so
# /api/status can render it for visibility into wedge incidents.
_DIGITAL_RESTART_STATE_LOCK = threading.Lock()
_DIGITAL_RESTART_STATE: dict = {
    "attempts_total": 0,
    "last_attempt_ts": 0.0,
    "last_attempt_reason": "",
    "wedge_recovery_total": 0,
    "last_wedge_recovery_ts": 0.0,
    "last_health_probe_result": "",   # "ok" / "wedged" / "skipped" / ""
    "last_health_probe_ts": 0.0,
    "last_health_probe_detail": "",
}


def digital_restart_state() -> dict:
    """Snapshot of the digital restart / health probe state. Thread-safe."""
    with _DIGITAL_RESTART_STATE_LOCK:
        return dict(_DIGITAL_RESTART_STATE)


# rtl-airband restart health state.  Mirrors _DIGITAL_RESTART_STATE so
# /api/status can render symmetric wedge-recovery telemetry for both
# pipelines.  Surfaced via rtl_restart_state().
_RTL_RESTART_STATE_LOCK = threading.Lock()
_RTL_RESTART_STATE: dict = {
    "attempts_total": 0,
    "last_attempt_ts": 0.0,
    "last_attempt_reason": "",
    "wedge_recovery_total": 0,
    "last_wedge_recovery_ts": 0.0,
    "last_health_probe_result": "",   # "ok" / "wedged" / "skipped" / ""
    "last_health_probe_ts": 0.0,
    "last_health_probe_detail": "",
}


def rtl_restart_state() -> dict:
    """Snapshot of the rtl-airband restart / health probe state. Thread-safe."""
    with _RTL_RESTART_STATE_LOCK:
        return dict(_RTL_RESTART_STATE)


def _record_rtl_restart_attempt(reason: str) -> None:
    with _RTL_RESTART_STATE_LOCK:
        _RTL_RESTART_STATE["attempts_total"] += 1
        _RTL_RESTART_STATE["last_attempt_ts"] = time.time()
        _RTL_RESTART_STATE["last_attempt_reason"] = str(reason or "unspecified")


def _record_rtl_health_probe(result: str, detail: str) -> None:
    with _RTL_RESTART_STATE_LOCK:
        _RTL_RESTART_STATE["last_health_probe_result"] = str(result or "")
        _RTL_RESTART_STATE["last_health_probe_ts"] = time.time()
        _RTL_RESTART_STATE["last_health_probe_detail"] = str(detail or "")


def _record_rtl_wedge_recovery() -> None:
    with _RTL_RESTART_STATE_LOCK:
        _RTL_RESTART_STATE["wedge_recovery_total"] += 1
        _RTL_RESTART_STATE["last_wedge_recovery_ts"] = time.time()


def _wait_for_rtl_airband_health(
    timeout_sec: float = 30.0,
    poll_interval_sec: float = 2.0,
) -> Tuple[bool, str]:
    """Poll ``rtl_airband_stats.txt`` until sample_flow_ok or timeout.

    rtl_airband writes its prometheus stats file on every output_thread
    cycle (~1 s in steady state).  After a fresh start, the binary needs
    a few seconds to walk through SoapySDR init for both DT tuners
    before the first cycle.  Anything later than ``timeout_sec`` means
    the channel pipeline is wedged — typical cause is a stuck SoapySDR
    handle inheriting bad state from the sdrplay daemon.

    Returns (ok, detail).  ok=True when the stats file is fresh (age <
    RTL_AIRBAND_STATS_STALE_SEC).  detail is a human-readable summary.
    """
    deadline = time.monotonic() + max(0.1, timeout_sec)
    last_state: dict = {}
    while True:
        state = rtl_airband_sample_flow_state(
            RTL_AIRBAND_STATS_PATH,
            RTL_AIRBAND_STATS_STALE_SEC,
        )
        if state.get("sample_flow_ok"):
            age = state.get("stats_age_sec")
            age_str = f"{age:.1f}s" if isinstance(age, (int, float)) else "?"
            return True, f"stats fresh (age {age_str})"
        last_state = state
        if time.monotonic() >= deadline:
            reason = state.get("reason") or "stats not fresh"
            return False, f"timeout after {timeout_sec:.1f}s; {reason}"
        time.sleep(max(0.0, poll_interval_sec))


def unit_active(unit: str) -> bool:
    """Check if a systemd unit is currently active."""
    return subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0


def unit_exists(unit: str) -> bool:
    """Check if a systemd unit exists."""
    result = subprocess.run(
        ["systemctl", "show", "-p", "LoadState", "--value", unit],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode != 0:
        return False
    return result.stdout.strip() != "not-found"


def _run_systemctl(args, use_sudo: bool = False):
    cmd = ["systemctl"] + list(args)
    if use_sudo:
        cmd = ["sudo"] + cmd
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def _enabled_from_result(result: subprocess.CompletedProcess[str]) -> tuple[bool, str]:
    if result.returncode != 0:
        return False, (result.stdout or result.stderr or "").strip()
    state = str(result.stdout or "").strip().lower()
    return state in ("enabled", "enabled-runtime"), state


def unit_enabled(unit: str, use_sudo: bool = False) -> bool:
    """Check if a systemd unit is enabled."""
    try:
        result = _run_systemctl(["is-enabled", unit], use_sudo=use_sudo)
    except Exception:
        return False
    enabled, _state = _enabled_from_result(result)
    return enabled


def _restart_unit(unit: str, use_sudo: bool = False) -> Tuple[bool, str]:
    """Restart a systemd unit and return (ok, error)."""
    try:
        result = _run_systemctl(["restart", unit], use_sudo=use_sudo)
    except Exception as e:
        return False, str(e)
    if result.returncode == 0:
        return True, ""
    err = (result.stderr or result.stdout or "").strip()
    if not err:
        err = f"restart failed (code {result.returncode})"
    return False, err


def _start_unit(unit: str, use_sudo: bool = False) -> Tuple[bool, str]:
    """Start a systemd unit and return (ok, error)."""
    try:
        result = _run_systemctl(["start", unit], use_sudo=use_sudo)
    except Exception as e:
        return False, str(e)
    if result.returncode == 0:
        return True, ""
    err = (result.stderr or result.stdout or "").strip()
    if not err:
        err = f"start failed (code {result.returncode})"
    return False, err


def _stop_unit(unit: str, use_sudo: bool = False) -> Tuple[bool, str]:
    """Stop a systemd unit and return (ok, error)."""
    try:
        result = _run_systemctl(["stop", unit], use_sudo=use_sudo)
    except Exception as e:
        return False, str(e)
    if result.returncode == 0:
        return True, ""
    err = (result.stderr or result.stdout or "").strip()
    if not err:
        err = f"stop failed (code {result.returncode})"
    return False, err


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _unit_configured(unit: str) -> bool:
    text = str(unit or "").strip()
    if not text:
        return False
    try:
        return unit_exists(text)
    except Exception:
        return False


def _kill_unit(unit: str) -> None:
    unit = str(unit or "").strip()
    if not unit:
        return
    try:
        _run_systemctl(["kill", "-s", "SIGKILL", unit], use_sudo=True)
    except Exception:
        pass


def _reset_failed_units(units) -> None:
    names = [str(unit or "").strip() for unit in units if str(unit or "").strip()]
    if not names:
        return
    try:
        _run_systemctl(["reset-failed"] + names, use_sudo=True)
    except Exception:
        pass


def _sdrplay_daemon_alive() -> bool:
    """True iff the sdrplay daemon process exists.

    Narrower than _sdrplay_daemon_healthy: doesn't look at segfaults, just
    checks whether the daemon is running at all. Used as a pre-flight gate
    before restart_digital decides whether to bounce the daemon.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-x", "sdrplay_apiServ"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except Exception:
        return False
    return result.returncode == 0 and bool((result.stdout or "").strip())


def _op25_http_status_ports() -> list:
    """Discover which OP25 instance HTTP status ports to probe after start.

    Multi-instance (Phase: split processes per RSPduo Master/Slave/aux) writes
    an instances.json under /run/scannerproject/op25/ listing each
    multi_rx.py instance's http_status_port. Single-instance falls back to
    the legacy OP25_STATUS_PORT env (default 8080).

    Returns a list of ints, deduplicated.
    """
    instances_path = os.getenv(
        "OP25_INSTANCES_PATH",
        "/run/scannerproject/op25/instances.json",
    ).strip()
    if instances_path:
        try:
            with open(instances_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                ports: list = []
                for entry in data:
                    if not isinstance(entry, dict):
                        continue
                    port = entry.get("http_status_port")
                    if isinstance(port, int) and port > 0 and port not in ports:
                        ports.append(port)
                if ports:
                    return ports
        except FileNotFoundError:
            pass
        except Exception:
            pass

    try:
        fallback = int(os.getenv("OP25_STATUS_PORT", "8080"))
    except Exception:
        fallback = 8080
    return [fallback] if fallback > 0 else []


def _probe_op25_http_port(
    port: int,
    host: str = "127.0.0.1",
    timeout_sec: float = 2.0,
) -> Tuple[bool, str]:
    """Single HTTP GET to op25's status port. True iff HTTP 200 returned.

    multi_rx.py binds its HTTP terminal only after gr-osmosdr + SoapySDR +
    sdrplay tuner-claim all complete. If the process is wedged at
    sdrplay_api_Open, the port never binds and this returns (False, ...).
    """
    url = f"http://{host}:{int(port)}/"
    try:
        with urlopen(url, timeout=timeout_sec) as resp:
            code = resp.getcode()
            if code == 200:
                return True, f"port {port}: HTTP 200"
            return False, f"port {port}: HTTP {code}"
    except URLError as exc:
        return False, f"port {port}: {exc.reason}"
    except Exception as exc:
        return False, f"port {port}: {type(exc).__name__}: {exc}"


def _wait_for_op25_health(
    timeout_sec: float = 45.0,
    poll_interval_sec: float = 2.0,
    per_port_timeout_sec: float = 2.0,
) -> Tuple[bool, str]:
    """Poll all discovered op25 status ports until each returns HTTP 200.

    Returns (ok, detail). ok=True only when every port returned 200 within
    timeout_sec. detail is a semicolon-joined per-port string suitable for
    logging or for /api/status surfacing.
    """
    ports = _op25_http_status_ports()
    if not ports:
        return False, "no op25 status ports discovered"

    deadline = time.monotonic() + max(0.1, timeout_sec)
    last_detail = ""
    while True:
        per_port = [
            _probe_op25_http_port(port, timeout_sec=per_port_timeout_sec)
            for port in ports
        ]
        if all(ok for ok, _ in per_port):
            return True, "; ".join(detail for _, detail in per_port)
        last_detail = "; ".join(detail for _, detail in per_port)
        if time.monotonic() >= deadline:
            return False, f"timeout after {timeout_sec:.1f}s; {last_detail}"
        time.sleep(max(0.0, poll_interval_sec))


def _record_digital_restart_attempt(reason: str) -> None:
    with _DIGITAL_RESTART_STATE_LOCK:
        _DIGITAL_RESTART_STATE["attempts_total"] += 1
        _DIGITAL_RESTART_STATE["last_attempt_ts"] = time.time()
        _DIGITAL_RESTART_STATE["last_attempt_reason"] = str(reason or "unspecified")


def _record_digital_health_probe(result: str, detail: str) -> None:
    with _DIGITAL_RESTART_STATE_LOCK:
        _DIGITAL_RESTART_STATE["last_health_probe_result"] = str(result or "")
        _DIGITAL_RESTART_STATE["last_health_probe_ts"] = time.time()
        _DIGITAL_RESTART_STATE["last_health_probe_detail"] = str(detail or "")


def _record_digital_wedge_recovery() -> None:
    with _DIGITAL_RESTART_STATE_LOCK:
        _DIGITAL_RESTART_STATE["wedge_recovery_total"] += 1
        _DIGITAL_RESTART_STATE["last_wedge_recovery_ts"] = time.time()


def _sdrplay_daemon_healthy() -> Tuple[bool, str]:
    """Probe whether the sdrplay daemon can be left alone during op25 restart.

    Healthy = daemon process exists AND no recent sdrplay_apiServ segfault
    in the kernel journal within OP25_SDRPLAY_HEALTH_PROBE_WINDOW_SEC (default
    30s). When healthy, restart_digital() skips the daemon restart — every
    restart-cycle was observed to segfault the daemon, so skipping is a
    correctness win, not just a perf win.

    Returns (healthy, reason). On probe errors, defaults to "unhealthy"
    so the cascade falls through to the existing restart path.
    """
    window_sec = int(_env_float("OP25_SDRPLAY_HEALTH_PROBE_WINDOW_SEC", 30.0, minimum=1.0))

    try:
        pgrep = subprocess.run(
            ["pgrep", "-x", "sdrplay_apiServ"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except Exception as e:
        return False, f"pgrep failed: {e}"
    if pgrep.returncode != 0 or not (pgrep.stdout or "").strip():
        return False, "daemon process not running"

    try:
        jc = subprocess.run(
            ["journalctl", "-k", "--since", f"{window_sec} seconds ago", "--no-pager"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except Exception as e:
        return False, f"journalctl probe failed: {e}"
    if jc.returncode != 0:
        return False, f"journalctl returned {jc.returncode}"
    for line in (jc.stdout or "").splitlines():
        if "sdrplay_apiServ" in line and "segfault" in line:
            return False, f"recent segfault within {window_sec}s"
    return True, f"daemon running, no segfault in {window_sec}s"


def unit_active_enter_epoch(unit: str):
    """Return ActiveEnterTimestampUSec as epoch seconds, or None."""
    def parse_epoch(result):
        if result.returncode != 0:
            return None
        val = (result.stdout or "").strip()
        if not val.isdigit():
            return None
        try:
            return int(val) / 1_000_000.0
        except Exception:
            return None

    def needs_sudo_fallback(result):
        if result.returncode == 0:
            return False
        detail = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
        return (
            "interactive authentication required" in detail
            or "access denied" in detail
            or "permission denied" in detail
        )

    try:
        result = _run_systemctl(["show", "-p", "ActiveEnterTimestampUSec", "--value", unit], use_sudo=False)
        epoch = parse_epoch(result)
        if epoch is not None:
            return epoch
        if not needs_sudo_fallback(result):
            return None
        result = _run_systemctl(["show", "-p", "ActiveEnterTimestampUSec", "--value", unit], use_sudo=True)
        return parse_epoch(result)
    except Exception:
        return None


def unit_restart_count(unit: str):
    """Return systemd's ``NRestarts`` counter for *unit*, or ``None``.

    ``NRestarts`` is monotonic over the unit's lifetime (since systemd
    daemon-reload).  Sampling deltas over a sliding window lets callers
    detect crash-loops that would otherwise be hidden behind the
    happens-to-be-active glitch where ``systemctl is-active`` returns
    ``active`` during the brief execution window of each restart cycle.
    """
    def parse_count(result):
        if result.returncode != 0:
            return None
        val = (result.stdout or "").strip()
        if not val.lstrip("-").isdigit():
            return None
        try:
            return int(val)
        except Exception:
            return None

    try:
        result = _run_systemctl(["show", "-p", "NRestarts", "--value", unit], use_sudo=False)
        count = parse_count(result)
        if count is not None:
            return count
        detail = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
        needs_sudo = (
            "interactive authentication required" in detail
            or "access denied" in detail
            or "permission denied" in detail
        )
        if not needs_sudo:
            return None
        result = _run_systemctl(["show", "-p", "NRestarts", "--value", unit], use_sudo=True)
        return parse_count(result)
    except Exception:
        return None


def restart_rtl(reason: str = "unspecified") -> Tuple[bool, str]:
    """Recover and restart rtl-airband with sequenced sdrplay handling.

    Mirrors restart_digital()'s gentle-then-escalate pattern so both
    SoapySDR-consuming pipelines have symmetric recovery behavior.

    Why this isn't a bare ``systemctl restart``
    -------------------------------------------
    Six failure modes observed in a single 24 h window all looked the
    same from the outside: ``systemctl is-active rtl-airband`` returns
    ``active``, but no samples are flowing.  The binary doesn't exit on
    a SoapySDR ``readStream TIMEOUT`` or ``Device has been removed`` —
    it logs a warning and limps along in a wait state.  systemd's
    ``Restart=on-failure`` never fires because the process didn't fail.
    A plain ``systemctl restart`` then talks to the same broken
    sdrplay daemon and inherits the same wedge.

    Recovery
    --------
    Stop rtl-airband (with SIGKILL escalation) → cycle sdrplay daemon
    if it's dead OR if we're escalating → start rtl-airband → settle
    → **poll the stats file mtime**.  The stats file is rtl_airband's
    contractual heartbeat (written every output_thread cycle); seeing
    fresh writes is the only reliable evidence that the channel
    pipeline actually came back to life.

    If the probe fails, escalate up to ``RTL_WEDGE_RECOVERY_MAX_ATTEMPTS``
    (default 2) with a forced daemon bounce each time.  Capped so we
    don't spin forever on hardware-level problems (cable unplugged,
    USB renumber, etc.).
    """
    rtl_unit = str(UNITS.get("rtl") or "").strip()
    sdrplay_unit = (
        os.getenv("UNIT_SDRPLAY")
        or os.getenv("SDRPLAY_SERVICE_NAME")
        or "sdrplay"
    ).strip()
    sdrplay_exists = _unit_configured(sdrplay_unit)
    sdrplay_settle_sec = _env_float("RTL_RESTART_SDRPLAY_SETTLE_SEC", 3.0)
    # rtl-airband DT-mode init takes ~3-6s to walk both tuners through
    # SoapySDR before the first stats write; settle BEFORE probing so
    # we don't false-alarm during normal startup.
    rtl_settle_sec = _env_float("RTL_RESTART_SETTLE_SEC", 6.0)

    probe_enabled = (
        os.getenv("RTL_POST_START_PROBE_ENABLED", "1").strip().lower() in _TRUTHY
    )
    probe_timeout_sec = _env_float(
        "RTL_POST_START_PROBE_TIMEOUT_SEC", 30.0, minimum=1.0
    )
    probe_poll_sec = _env_float(
        "RTL_POST_START_PROBE_POLL_SEC", 2.0, minimum=0.1
    )
    max_escalations = int(
        _env_float("RTL_WEDGE_RECOVERY_MAX_ATTEMPTS", 2.0, minimum=1.0)
    )

    _record_rtl_restart_attempt(reason)
    print(
        f"restart_rtl: reason={reason!r} probe_enabled={probe_enabled} "
        f"probe_timeout_sec={probe_timeout_sec}",
        flush=True,
    )

    if not rtl_unit:
        return False, "rtl unit not configured"

    def _attempt(force_sdrplay_restart: bool, attempt_label: str) -> Tuple[bool, str]:
        _stop_unit(rtl_unit, use_sudo=True)
        _kill_unit(rtl_unit)
        _reset_failed_units([rtl_unit])

        if sdrplay_exists:
            daemon_alive = _sdrplay_daemon_alive()
            if force_sdrplay_restart or not daemon_alive:
                print(
                    f"restart_rtl[{attempt_label}]: bouncing sdrplay daemon "
                    f"(force={force_sdrplay_restart}, alive={daemon_alive})",
                    flush=True,
                )
                ok, err = _restart_unit(sdrplay_unit, use_sudo=True)
                if not ok:
                    return False, f"sdrplay restart: {err}"
                if sdrplay_settle_sec > 0:
                    time.sleep(sdrplay_settle_sec)
            else:
                healthy, hreason = _sdrplay_daemon_healthy()
                if healthy:
                    print(
                        f"restart_rtl[{attempt_label}]: sdrplay daemon "
                        f"healthy ({hreason}); skipping restart",
                        flush=True,
                    )
                else:
                    print(
                        f"restart_rtl[{attempt_label}]: sdrplay daemon "
                        f"needs restart ({hreason})",
                        flush=True,
                    )
                    ok, err = _restart_unit(sdrplay_unit, use_sudo=True)
                    if not ok:
                        return False, f"sdrplay restart: {err}"
                    if sdrplay_settle_sec > 0:
                        time.sleep(sdrplay_settle_sec)

        ok, err = _start_unit(rtl_unit, use_sudo=True)
        if not ok:
            return False, f"rtl start: {err}"
        if rtl_settle_sec > 0:
            time.sleep(rtl_settle_sec)

        if probe_enabled:
            probe_ok, probe_detail = _wait_for_rtl_airband_health(
                timeout_sec=probe_timeout_sec,
                poll_interval_sec=probe_poll_sec,
            )
            _record_rtl_health_probe(
                "ok" if probe_ok else "wedged",
                probe_detail,
            )
            print(
                f"restart_rtl[{attempt_label}]: probe "
                f"{'ok' if probe_ok else 'WEDGED'} — {probe_detail}",
                flush=True,
            )
            if not probe_ok:
                return False, f"post-start probe failed: {probe_detail}"
        else:
            _record_rtl_health_probe("skipped", "probe disabled by env")

        return True, ""

    # Gentle attempt 1: don't force sdrplay unless the daemon is dead.
    ok, err = _attempt(force_sdrplay_restart=False, attempt_label="gentle")
    if ok:
        return True, ""
    print(
        f"restart_rtl: gentle attempt failed ({err}); escalating to wedge recovery",
        flush=True,
    )

    # Escalation: each retry forces a sdrplay daemon bounce to clear
    # stale client connections, then retries rtl-airband.  This is the
    # canonical pattern we kept executing by hand from bash one-liners.
    last_err = err
    for escalation in range(1, max_escalations + 1):
        ok, err = _attempt(
            force_sdrplay_restart=True,
            attempt_label=f"escalation-{escalation}",
        )
        _record_rtl_wedge_recovery()
        if ok:
            print(
                f"restart_rtl: wedge recovery succeeded on escalation {escalation}",
                flush=True,
            )
            return True, ""
        last_err = err

    return False, (
        f"wedge recovery exhausted after {max_escalations} escalations: {last_err}"
    )


def restart_ground() -> Tuple[bool, str]:
    """Restart the ground scanner."""
    return _restart_unit(UNITS["ground"])


def restart_icecast() -> Tuple[bool, str]:
    """Restart the Icecast service."""
    return _restart_unit(UNITS["icecast"])


def restart_keepalive() -> Tuple[bool, str]:
    """Restart the Icecast keepalive service."""
    return _restart_unit(UNITS["keepalive"])


def restart_ui() -> Tuple[bool, str]:
    """Restart the UI service."""
    return _restart_unit(UNITS["ui"])


def restart_digital(
    unit: Optional[str] = None,
    reason: str = "unspecified",
) -> Tuple[bool, str]:
    """Recover and restart the OP25 digital backend service.

    Stop op25 (with SIGKILL escalation) → optionally restart sdrplay daemon →
    start op25 → settle → **poll op25 HTTP status ports for HTTP 200**.

    The post-start probe is the load-bearing addition. multi_rx.py binds its
    HTTP terminal only after gr-osmosdr / SoapySDR / sdrplay tuner-claim all
    succeed. If the probe times out, op25 is wedged at sdrplay tuner-claim —
    the observed pattern is an alive sdrplay daemon with state corruption
    from accumulated client wedges across multiple restart cycles. The fix
    is to force a sdrplay daemon restart, then retry op25.

    Escalation is capped at OP25_WEDGE_RECOVERY_MAX_ATTEMPTS (default 2) so
    we never spin forever. The reason argument is recorded in
    _DIGITAL_RESTART_STATE so wedge incidents can be correlated to their
    triggers (profile_switch / vdl2_share_toggle / manager_restart / etc).
    """
    digital_unit = str(unit or UNITS["digital"] or "").strip()
    audio_unit = str(UNITS.get("digital_audio", "") or "").strip()
    audio_exists = _unit_configured(audio_unit)
    sdrplay_unit = (
        os.getenv("UNIT_SDRPLAY")
        or os.getenv("SDRPLAY_SERVICE_NAME")
        or "sdrplay"
    ).strip()
    sdrplay_exists = _unit_configured(sdrplay_unit)
    sdrplay_settle_sec = _env_float("DIGITAL_RESTART_SDRPLAY_SETTLE_SEC", 3.0)
    op25_settle_sec = _env_float("DIGITAL_RESTART_OP25_SETTLE_SEC", 16.0)

    probe_enabled = (
        os.getenv("OP25_POST_START_PROBE_ENABLED", "1").strip().lower() in _TRUTHY
    )
    probe_timeout_sec = _env_float(
        "OP25_POST_START_PROBE_TIMEOUT_SEC", 45.0, minimum=1.0
    )
    probe_poll_sec = _env_float(
        "OP25_POST_START_PROBE_POLL_SEC", 2.0, minimum=0.1
    )
    max_escalations = int(
        _env_float("OP25_WEDGE_RECOVERY_MAX_ATTEMPTS", 2.0, minimum=1.0)
    )

    _record_digital_restart_attempt(reason)
    print(
        f"restart_digital: reason={reason!r} probe_enabled={probe_enabled} "
        f"probe_timeout_sec={probe_timeout_sec}",
        flush=True,
    )

    if not digital_unit:
        return False, "digital unit not configured"

    def _attempt(force_sdrplay_restart: bool, attempt_label: str) -> Tuple[bool, str]:
        if audio_exists:
            _stop_unit(audio_unit, use_sudo=True)
        _stop_unit(digital_unit, use_sudo=True)
        _kill_unit(digital_unit)
        if audio_exists:
            _kill_unit(audio_unit)
        _reset_failed_units([digital_unit, audio_unit if audio_exists else ""])

        if sdrplay_exists:
            daemon_alive = _sdrplay_daemon_alive()
            if force_sdrplay_restart or not daemon_alive:
                print(
                    f"restart_digital[{attempt_label}]: bouncing sdrplay daemon "
                    f"(force={force_sdrplay_restart}, alive={daemon_alive})",
                    flush=True,
                )
                ok, err = _restart_unit(sdrplay_unit, use_sudo=True)
                if not ok:
                    return False, f"sdrplay restart: {err}"
                if sdrplay_settle_sec > 0:
                    time.sleep(sdrplay_settle_sec)
            else:
                healthy, hreason = _sdrplay_daemon_healthy()
                if healthy:
                    print(
                        f"restart_digital[{attempt_label}]: sdrplay daemon "
                        f"healthy ({hreason}); skipping restart",
                        flush=True,
                    )
                else:
                    print(
                        f"restart_digital[{attempt_label}]: sdrplay daemon "
                        f"needs restart ({hreason})",
                        flush=True,
                    )
                    ok, err = _restart_unit(sdrplay_unit, use_sudo=True)
                    if not ok:
                        return False, f"sdrplay restart: {err}"
                    if sdrplay_settle_sec > 0:
                        time.sleep(sdrplay_settle_sec)

        ok, err = _start_unit(digital_unit, use_sudo=True)
        if not ok:
            return False, f"digital start: {err}"
        if op25_settle_sec > 0:
            time.sleep(op25_settle_sec)

        if probe_enabled:
            probe_ok, probe_detail = _wait_for_op25_health(
                timeout_sec=probe_timeout_sec,
                poll_interval_sec=probe_poll_sec,
            )
            _record_digital_health_probe(
                "ok" if probe_ok else "wedged",
                probe_detail,
            )
            print(
                f"restart_digital[{attempt_label}]: probe "
                f"{'ok' if probe_ok else 'WEDGED'} — {probe_detail}",
                flush=True,
            )
            if not probe_ok:
                return False, f"post-start probe failed: {probe_detail}"
        else:
            _record_digital_health_probe("skipped", "probe disabled by env")

        if audio_exists:
            ok, err = _restart_unit(audio_unit, use_sudo=True)
            if not ok:
                return False, f"digital audio restart: {err}"
        return True, ""

    # Gentle attempt 1: don't force sdrplay unless the daemon is dead.
    ok, err = _attempt(force_sdrplay_restart=False, attempt_label="gentle")
    if ok:
        return True, ""
    print(
        f"restart_digital: gentle attempt failed ({err}); escalating to wedge recovery",
        flush=True,
    )

    # Escalation: each retry forces a sdrplay daemon bounce to clear stale
    # client connections, then retries op25.
    last_err = err
    for escalation in range(1, max_escalations + 1):
        ok, err = _attempt(
            force_sdrplay_restart=True,
            attempt_label=f"escalation-{escalation}",
        )
        _record_digital_wedge_recovery()
        if ok:
            print(
                f"restart_digital: wedge recovery succeeded on escalation {escalation}",
                flush=True,
            )
            return True, ""
        last_err = err

    return False, (
        f"wedge recovery exhausted after {max_escalations} escalations: {last_err}"
    )


def restart_digital_audio() -> Tuple[bool, str]:
    """Restart the OP25 audio bridge service."""
    return _restart_unit(UNITS["digital_audio"], use_sudo=True)


def set_bt_heal_auto_recovery(enabled: bool) -> Tuple[bool, str]:
    """Enable/disable periodic BT-heal auto-recovery timer."""
    timer_unit = str(BT_HEAL_TIMER_UNIT or "").strip()
    if not timer_unit:
        return False, "BT-heal timer unit not configured"
    args = ["enable", "--now", timer_unit] if enabled else ["disable", "--now", timer_unit]
    try:
        result = _run_systemctl(args, use_sudo=True)
    except Exception as e:
        return False, str(e)
    if result.returncode == 0:
        return True, ""
    err = (result.stderr or result.stdout or "").strip()
    if not err:
        err = f"bt-heal timer update failed (code {result.returncode})"
    return False, err


def reboot_host() -> Tuple[bool, str]:
    """Request a host reboot through systemd."""
    try:
        result = _run_systemctl(["reboot"], use_sudo=True)
    except Exception as e:
        return False, str(e)
    if result.returncode == 0:
        return True, ""
    err = (result.stderr or result.stdout or "").strip()
    if not err:
        err = f"host reboot request failed (code {result.returncode})"
    return False, err


def stop_rtl():
    """Stop the rtl-airband scanner."""
    subprocess.run(
        ["systemctl", "stop", UNITS["rtl"]],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def stop_ground():
    """Stop the ground scanner."""
    subprocess.run(
        ["systemctl", "stop", UNITS["ground"]],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def stop_digital() -> Tuple[bool, str]:
    """Stop the digital backend service."""
    return _stop_unit(UNITS["digital"])


def start_rtl():
    """Start the rtl-airband scanner."""
    subprocess.Popen(
        ["systemctl", "start", "--no-block", UNITS["rtl"]],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def start_ground():
    """Start the ground scanner."""
    subprocess.Popen(
        ["systemctl", "start", "--no-block", UNITS["ground"]],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def start_acars() -> Tuple[bool, str]:
    """Start the ACARS decoder service."""
    return _start_unit(UNITS["acars"], use_sudo=True)


def stop_acars() -> Tuple[bool, str]:
    """Stop the ACARS decoder service."""
    return _stop_unit(UNITS["acars"], use_sudo=True)


def restart_acars() -> Tuple[bool, str]:
    """Restart the ACARS decoder service."""
    return _restart_unit(UNITS["acars"], use_sudo=True)


def start_vdl2() -> Tuple[bool, str]:
    """Start the VDL2 decoder service."""
    return _start_unit(UNITS["vdl2"], use_sudo=True)


def stop_vdl2() -> Tuple[bool, str]:
    """Stop the VDL2 decoder service."""
    return _stop_unit(UNITS["vdl2"], use_sudo=True)


def restart_vdl2() -> Tuple[bool, str]:
    """Restart the VDL2 decoder service."""
    return _restart_unit(UNITS["vdl2"], use_sudo=True)


def start_radiosonde() -> Tuple[bool, str]:
    """Start the radiosonde decoder service."""
    return _start_unit(UNITS["radiosonde"], use_sudo=True)


def stop_radiosonde() -> Tuple[bool, str]:
    """Stop the radiosonde decoder service."""
    return _stop_unit(UNITS["radiosonde"], use_sudo=True)


def restart_radiosonde() -> Tuple[bool, str]:
    """Restart the radiosonde decoder service."""
    return _restart_unit(UNITS["radiosonde"], use_sudo=True)


def start_digital() -> Tuple[bool, str]:
    """Start the digital backend service."""
    return _start_unit(UNITS["digital"])


def ground_control_unit():
    """Determine which unit controls the ground frequency."""
    if unit_active(UNITS["ground"]):
        return "ground"
    if unit_active(UNITS["rtl"]):
        return "rtl"
    return "ground"
