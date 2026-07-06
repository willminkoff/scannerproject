"""System unit control.

Historically systemd-only; since SB7.2 the low-level primitives
(``unit_active``, ``_restart_unit``, ...) are thin shims over the
``ServiceBackend`` abstraction in ui/service_backend.py so the same
recovery logic drives either systemd (Linux) or launchd (the Mac mini
port).  The module-level function names and signatures are preserved on
purpose — every call site (handlers/actions/dongle_power/device_ownership)
and a large test corpus reference them by name.
"""
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
        RTL_AIRBAND_AIRBAND_STATS_PATH,
        RTL_AIRBAND_GROUND_STATS_PATH,
        RTL_AIRBAND_STATS_STALE_SEC,
    )
    from .sample_flow import rtl_airband_sample_flow_state
    from .service_backend import get_backend
except ImportError:
    from ui.config import (
        UNITS,
        BT_HEAL_TIMER_UNIT,
        RTL_AIRBAND_STATS_PATH,
        RTL_AIRBAND_AIRBAND_STATS_PATH,
        RTL_AIRBAND_GROUND_STATS_PATH,
        RTL_AIRBAND_STATS_STALE_SEC,
    )
    from ui.sample_flow import rtl_airband_sample_flow_state
    from ui.service_backend import get_backend


_TRUTHY = ("1", "true", "yes", "on")

# Icecast mount names for the post-start health probe.  Used in addition to
# stats-file freshness so the probe succeeds the instant rtl-airband
# connects and starts publishing, not when its stats writer happens to
# flush.  Race-free because icecast sets stream_start only on a real
# source connect.
RTL_AIRBAND_AIRBAND_MOUNT = os.getenv("RTL_AIRBAND_AIRBAND_MOUNT", "ANALOG.mp3").strip().lstrip("/")
RTL_AIRBAND_GROUND_MOUNT = os.getenv("RTL_AIRBAND_GROUND_MOUNT", "ANALOG_GROUND.mp3").strip().lstrip("/")

# SB7.2 Task 3 (open P0): restart_rtl_airband(), restart_rtl_ground() and
# restart_digital() are invoked from independent API handler threads
# (profile switch on one band while the operator mashes Restart Digital,
# watchdog firing during a manual recovery, ...).  All three bounce the
# SHARED sdrplay daemon during escalation.  With no mutual exclusion, two
# overlapping stop-all → daemon-restart → ordered-start sequences
# interleave: flow A restarts the daemon while flow B is mid-way through
# its own bounce, B's client reconnects into A's half-initialized daemon,
# and we manufacture exactly the multi-client state corruption these
# cascades exist to recover from.
#
# This lock serializes the daemon-bounce critical sections ONLY:
#   * each escalation attempt in full (stop-all → sdrplay restart →
#     ordered start + probes) — the whole sequence must be atomic with
#     respect to other flows' bounces, and
#   * the daemon check-and-bounce block inside gentle attempts (a gentle
#     attempt bounces the daemon too when it finds it dead/unhealthy).
# The rest of the gentle path (stop/start/probe of the caller's own unit)
# stays unguarded on purpose: it touches only that flow's units, and
# holding the lock through a 30-45 s health probe would needlessly stall
# an unrelated band's recovery.
#
# RLock, not Lock: the escalation wrapper holds it around the whole
# attempt, and the shared daemon check-and-bounce block inside re-acquires
# it (no-op for the same thread; the real guard for gentle attempts).
_SDRPLAY_RESTART_LOCK = threading.RLock()

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
    stats_path: Optional[str] = None,
    mount_name: Optional[str] = None,
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

    ``stats_path`` is optional: if omitted, the function probes the
    legacy ``RTL_AIRBAND_STATS_PATH`` (single-process pre-split layout).
    The MA/SL split passes a per-service path so each service can be
    probed independently — see ``restart_rtl_airband`` /
    ``restart_rtl_ground`` callers.
    """
    target_path = stats_path if stats_path else RTL_AIRBAND_STATS_PATH
    deadline = time.monotonic() + max(0.1, timeout_sec)
    last_state: dict = {}
    while True:
        # Race-free signal: icecast stream_start is set the instant a
        # source connects.  Catches the common case where rtl-airband is
        # publishing audio bytes well before it has flushed its first
        # stats line (~20-60s delay during cold-start of MA/SL pair).
        if mount_name:
            try:
                # Lazy import to dodge a circular: ui/icecast.py imports
                # unit_active from this module at top-level.
                try:
                    from .sample_flow import mount_publishing as _mount_publishing
                    from .icecast import fetch_local_icecast_status as _fetch_ice
                except ImportError:
                    from ui.sample_flow import mount_publishing as _mount_publishing
                    from ui.icecast import fetch_local_icecast_status as _fetch_ice
                status_text = _fetch_ice()
                if status_text and not status_text.startswith("ERROR:") and _mount_publishing(status_text, mount_name):
                    return True, f"mount /{mount_name} publishing"
            except Exception:
                pass  # fall through to stats check
        state = rtl_airband_sample_flow_state(
            target_path,
            RTL_AIRBAND_STATS_STALE_SEC,
        )
        if state.get("sample_flow_ok"):
            age = state.get("stats_age_sec")
            age_str = f"{age:.1f}s" if isinstance(age, (int, float)) else "?"
            return True, f"stats fresh (age {age_str})"
        last_state = state
        if time.monotonic() >= deadline:
            reason = state.get("reason") or "stats not fresh"
            mount_note = f"; mount /{mount_name} not publishing" if mount_name else ""
            return False, f"timeout after {timeout_sec:.1f}s; {reason}{mount_note}"
        time.sleep(max(0.0, poll_interval_sec))


# ---------------------------------------------------------------------------
# Low-level primitives (SB7.2): each is a shim over the ServiceBackend so
# the recovery cascades below run unmodified on systemd AND launchd.  The
# subprocess bodies that used to live here moved VERBATIM to
# ui/service_backend.py::SystemdBackend — do not re-inline them.
# ---------------------------------------------------------------------------


def unit_active(unit: str) -> bool:
    """Check if a service unit is currently active."""
    return get_backend().active(unit)


def unit_exists(unit: str) -> bool:
    """Check if a service unit exists (is known to the service manager)."""
    return get_backend().exists(unit)


def _run_systemctl(args, use_sudo: bool = False):
    """Generic escape hatch for operations without a typed primitive
    (BT-heal timer enable/disable, host reboot).  On the launchd backend
    this raises NotImplementedError — both in-tree callers already wrap
    it in try/except Exception and surface (False, msg)."""
    return get_backend().run(args, use_sudo=use_sudo)


def unit_enabled(unit: str, use_sudo: bool = False) -> bool:
    """Check if a service unit is enabled to start at boot/login."""
    return get_backend().enabled(unit, use_sudo=use_sudo)


def _restart_unit(unit: str, use_sudo: bool = False) -> Tuple[bool, str]:
    """Restart a service unit and return (ok, error)."""
    return get_backend().restart(unit, use_sudo=use_sudo)


def _start_unit(unit: str, use_sudo: bool = False) -> Tuple[bool, str]:
    """Start a service unit and return (ok, error)."""
    return get_backend().start(unit, use_sudo=use_sudo)


def _stop_unit(unit: str, use_sudo: bool = False) -> Tuple[bool, str]:
    """Stop a service unit and return (ok, error)."""
    return get_backend().stop(unit, use_sudo=use_sudo)


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
    """SIGKILL a unit's process (best-effort, never raises)."""
    get_backend().kill(unit)


def _reset_failed_units(units) -> None:
    """Clear failed/start-limit latches so the next start isn't refused.

    No-op on launchd (no failed-state latch exists there — see
    LaunchdBackend.reset_failed for the full rationale)."""
    get_backend().reset_failed(units)


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

    # SB7.2: the segfault scan below reads the KERNEL journal — a
    # Linux/systemd-ism with no launchd equivalent (macOS crash reports
    # land in ~/Library/Logs/DiagnosticReports, not a greppable journal).
    # On non-systemd backends, report healthy once the daemon process
    # exists: "healthy" only means "safe to leave the daemon alone", and
    # the post-start HTTP/stats probes + wedge escalation still catch a
    # corrupted daemon and force the bounce on the next attempt.
    if get_backend().name != "systemd":
        return True, (
            f"daemon running; kernel-journal segfault probe skipped "
            f"({get_backend().name} backend)"
        )

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
    """Return the unit's last activation time as epoch seconds, or None.

    systemd: ActiveEnterTimestampUSec (with the polkit sudo-fallback dance
    — pinned by tests/test_systemd.py).  launchd: best-effort process
    start time via pid + ``ps -o lstart=``.  None is the universal
    "don't know"; all callers tolerate it."""
    return get_backend().active_enter_epoch(unit)


def unit_restart_count(unit: str):
    """Return the unit's monotonic restart counter, or ``None``.

    systemd's ``NRestarts`` is monotonic over the unit's lifetime (since
    daemon-reload).  Sampling deltas over a sliding window lets callers
    detect crash-loops that would otherwise be hidden behind the
    happens-to-be-active glitch where ``systemctl is-active`` returns
    ``active`` during the brief execution window of each restart cycle.
    launchd has no NRestarts analog and reports a constant 0 (delta 0 =
    loop detection disabled rather than false-alarming).
    """
    return get_backend().restart_count(unit)


def restart_rtl(reason: str = "unspecified") -> Tuple[bool, str]:
    """Recover and restart rtl-airband with sequenced sdrplay handling.

    Post-MA/SL-split auto-dispatch: if the new
    ``rtl-airband-airband.service`` unit is present, route to
    ``restart_rtl_airband()`` instead of operating on the retired
    legacy ``rtl-airband.service``.  This keeps all the existing
    actions.py callers (profile switches, control changes, etc.)
    working through the cutover without touching ten separate call
    sites.  Pre-cutover or post-rollback, the legacy code path
    below still runs.

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
    # Post-MA/SL-split auto-dispatch.  The new
    # ``rtl-airband-airband.service`` replaces the legacy
    # ``rtl-airband.service`` for everything actions.py wants when
    # it calls "restart_rtl()" — keep the gentle/escalate semantics
    # consistent across both pre- and post-cutover deployments.
    new_airband_unit = str(UNITS.get("rtl_airband") or "").strip()
    if new_airband_unit and _unit_configured(new_airband_unit):
        # Defined later in this module — Python resolves at call time so
        # forward reference is fine.
        return restart_rtl_airband(reason)

    rtl_unit = str(UNITS.get("rtl") or "").strip()
    # OP25 holds a persistent client connection to the same sdrplay
    # daemon.  When restart_rtl forces a daemon bounce, the daemon's
    # state can still be corrupted by OP25's surviving connection —
    # the post-probe wedge we kept observing at 12:00–12:02 was caused
    # by OP25 immediately reconnecting and confusing the daemon's
    # multi-tuner coordination.  On escalation we briefly stop OP25,
    # bounce the daemon clean, bring rtl-airband back, verify its
    # sample flow, THEN restart OP25.  This is exactly the manual
    # bash-one-liner clean-cycle that always worked.
    # restart_rtl() is the airband/legacy path (the old combined
    # rtl-airband.service was the airband master); verify the airband
    # mount. The prior `service_label == "rtl_airband"` here was a
    # copy-paste from the _restart_rtl_service() worker (which DOES take
    # a service_label param) — `service_label` is undefined in this
    # function's scope, so this line raised NameError on every legacy
    # fallback (pre-existing crash, only reachable when the gr-demod@
    # dispatch at the top of this function doesn't fire).
    target_mount = RTL_AIRBAND_AIRBAND_MOUNT
    digital_unit = str(UNITS.get("digital") or "").strip()
    sdrplay_unit = (
        os.getenv("UNIT_SDRPLAY")
        or os.getenv("SDRPLAY_SERVICE_NAME")
        or "sdrplay"
    ).strip()
    sdrplay_exists = _unit_configured(sdrplay_unit)
    digital_exists = _unit_configured(digital_unit) if digital_unit else False
    sdrplay_settle_sec = _env_float("RTL_RESTART_SDRPLAY_SETTLE_SEC", 3.0)
    # rtl-airband DT-mode init takes ~3-6s to walk both tuners through
    # SoapySDR before the first stats write; settle BEFORE probing so
    # we don't false-alarm during normal startup.
    rtl_settle_sec = _env_float("RTL_RESTART_SETTLE_SEC", 6.0)
    # Settle after restarting OP25 — short, since we don't probe OP25
    # health here (restart_digital owns that).  Just enough for systemd
    # to start its process.
    digital_settle_sec = _env_float("RTL_RESTART_DIGITAL_SETTLE_SEC", 2.0)

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

        # Stop OP25 too on escalations so the sdrplay daemon can fully
        # clear all client state before we cycle it.  Only on
        # escalation: gentle attempts don't touch OP25 because if a
        # plain rtl-airband restart fixes things, there's no reason
        # to interrupt digital audio.
        op25_was_stopped = False
        if (
            force_sdrplay_restart
            and digital_exists
            and unit_active(digital_unit)
        ):
            print(
                f"restart_rtl[{attempt_label}]: stopping OP25 to free "
                f"sdrplay daemon for clean cycle",
                flush=True,
            )
            _stop_unit(digital_unit, use_sudo=True)
            _kill_unit(digital_unit)
            op25_was_stopped = True

        _reset_failed_units(
            [rtl_unit, digital_unit if op25_was_stopped else ""]
        )

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
                # rtl-airband itself didn't come back; leave OP25
                # stopped so the next escalation can re-cycle the
                # whole stack cleanly.  Restarting OP25 here would
                # just re-corrupt the daemon for the next attempt.
                return False, f"post-start probe failed: {probe_detail}"
        else:
            _record_rtl_health_probe("skipped", "probe disabled by env")

        # rtl-airband is healthy.  If we stopped OP25 on the way in,
        # bring it back up so the operator gets a complete recovery
        # from one button click.  We don't probe OP25 health here —
        # restart_digital() owns that — but we wait the systemd-start
        # settle so a quick follow-up status read can see it active.
        if op25_was_stopped:
            print(
                f"restart_rtl[{attempt_label}]: restarting OP25 "
                f"(rtl-airband recovered)",
                flush=True,
            )
            ok_start, err_start = _start_unit(digital_unit, use_sudo=True)
            if not ok_start:
                # Don't fail the whole restart_rtl call for this —
                # rtl-airband IS recovered.  Log the OP25 problem
                # in the health-probe detail so /api/status surfaces
                # it, then let the operator restart digital manually.
                _record_rtl_health_probe(
                    "ok-rtl-but-op25-failed-to-restart",
                    f"rtl-airband healthy; op25 restart err: {err_start}",
                )
                print(
                    f"restart_rtl[{attempt_label}]: WARN OP25 restart "
                    f"failed: {err_start} (rtl-airband still ok)",
                    flush=True,
                )
            elif digital_settle_sec > 0:
                time.sleep(digital_settle_sec)

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


def restart_ground(reason: str = "unspecified") -> Tuple[bool, str]:
    """Restart the ground scanner.

    Post-MA/SL-split auto-dispatch: route to ``restart_rtl_ground()``
    (the new SL-mode unit with sequenced-recovery semantics) when
    the new unit is present.  Pre-cutover / post-rollback we fall
    through to the legacy ``rtl-airband-ground`` unit start.

    ``reason`` is plumbed through to ``restart_rtl_ground`` so journal
    triage can attribute the restart to its caller; the legacy fallback
    path does not consume it.
    """
    new_ground_unit = str(UNITS.get("rtl_ground") or "").strip()
    if new_ground_unit and _unit_configured(new_ground_unit):
        return restart_rtl_ground(reason=reason)
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
        # SB7.2 Task 3: escalation bounces the SHARED sdrplay daemon and
        # restarts the stack in order — hold the bounce lock across the
        # whole sequence so a concurrent restart_rtl_airband/_ground
        # escalation can't interleave its own bounce (see the
        # _SDRPLAY_RESTART_LOCK comment).  Gentle attempts stay unguarded
        # except for the daemon check-and-bounce block inside.
        if force_sdrplay_restart:
            with _SDRPLAY_RESTART_LOCK:
                return _attempt_impl(force_sdrplay_restart, attempt_label)
        return _attempt_impl(force_sdrplay_restart, attempt_label)

    def _attempt_impl(force_sdrplay_restart: bool, attempt_label: str) -> Tuple[bool, str]:
        if audio_exists:
            _stop_unit(audio_unit, use_sudo=True)
        _stop_unit(digital_unit, use_sudo=True)
        _kill_unit(digital_unit)
        if audio_exists:
            _kill_unit(audio_unit)
        _reset_failed_units([digital_unit, audio_unit if audio_exists else ""])

        if sdrplay_exists:
            # RLock: no-op re-acquire on escalation (outer wrapper already
            # holds it); the real guard for GENTLE attempts, whose
            # dead/unhealthy-daemon bounce must not interleave with
            # another flow's escalation.  check + bounce are atomic.
            with _SDRPLAY_RESTART_LOCK:
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


def stop_rtl() -> Tuple[bool, str]:
    """Stop the rtl-airband scanner.

    Was a hardcoded `subprocess.run(["systemctl", ...])` — bypassed the
    ServiceBackend abstraction entirely, so it raised FileNotFoundError on
    any backend without a literal `systemctl` binary (e.g. LaunchdBackend on
    macOS) instead of routing through get_backend(). Found during the SB7 UI
    macOS bring-up, 2026-07-06. All callers discard the return value already,
    so widening None -> Tuple[bool, str] to match _stop_unit is safe.
    """
    return _stop_unit(UNITS["rtl"])


def stop_ground() -> Tuple[bool, str]:
    """Stop the ground scanner. See stop_rtl() docstring for why this
    changed from a hardcoded systemctl call to the backend abstraction."""
    return _stop_unit(UNITS["ground"])


def stop_digital() -> Tuple[bool, str]:
    """Stop the digital backend service."""
    return _stop_unit(UNITS["digital"])


def start_rtl() -> Tuple[bool, str]:
    """Start the rtl-airband scanner. See stop_rtl() docstring."""
    return _start_unit(UNITS["rtl"])


def start_ground() -> Tuple[bool, str]:
    """Start the ground scanner. See stop_rtl() docstring."""
    return _start_unit(UNITS["ground"])


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


# ---------------------------------------------------------------------------
# MA/SL split-process restart machinery (2026-05-26)
#
# The legacy ``restart_rtl()`` above operates on the single pre-split
# unit ``rtl-airband.service``.  After the MA/SL cutover (see
# docs/rspduo_ma_sl_split.md) that unit is masked and TWO new units
# take over: ``rtl-airband-airband.service`` (Master / Tuner 1) and
# ``rtl-airband-ground.service`` (Slave / Tuner 2).  Each runs in its
# own process.  Each writes its own stats file.  Each gets its own
# restart function with telemetry mirrored from the legacy
# ``_RTL_RESTART_STATE`` pattern.
#
# Gentle attempt only touches the target service.  Escalation
# (forced sdrplay daemon bounce) stops BOTH new services AND OP25
# because all three share the daemon — leaving any client connected
# during the bounce risks the same state-corruption pattern we saw
# with the pre-split DT-mode setup.  Per-band escalations then bring
# everything back up in the correct order: airband first (Master),
# ground second (Slave depends on Master), OP25 last.
# ---------------------------------------------------------------------------

_RTL_AIRBAND_RESTART_STATE_LOCK = threading.Lock()
_RTL_AIRBAND_RESTART_STATE: dict = {
    "attempts_total": 0,
    "last_attempt_ts": 0.0,
    "last_attempt_reason": "",
    "wedge_recovery_total": 0,
    "last_wedge_recovery_ts": 0.0,
    "last_health_probe_result": "",
    "last_health_probe_ts": 0.0,
    "last_health_probe_detail": "",
}

_RTL_GROUND_RESTART_STATE_LOCK = threading.Lock()
_RTL_GROUND_RESTART_STATE: dict = {
    "attempts_total": 0,
    "last_attempt_ts": 0.0,
    "last_attempt_reason": "",
    "wedge_recovery_total": 0,
    "last_wedge_recovery_ts": 0.0,
    "last_health_probe_result": "",
    "last_health_probe_ts": 0.0,
    "last_health_probe_detail": "",
}


def rtl_airband_restart_state() -> dict:
    """Snapshot of the airband-service restart / probe state."""
    with _RTL_AIRBAND_RESTART_STATE_LOCK:
        return dict(_RTL_AIRBAND_RESTART_STATE)


def rtl_ground_restart_state() -> dict:
    """Snapshot of the ground-service restart / probe state."""
    with _RTL_GROUND_RESTART_STATE_LOCK:
        return dict(_RTL_GROUND_RESTART_STATE)


def _record_service_restart_attempt(state_lock, state, reason: str) -> None:
    with state_lock:
        state["attempts_total"] += 1
        state["last_attempt_ts"] = time.time()
        state["last_attempt_reason"] = str(reason or "unspecified")


def _record_service_health_probe(state_lock, state, result: str, detail: str) -> None:
    with state_lock:
        state["last_health_probe_result"] = str(result or "")
        state["last_health_probe_ts"] = time.time()
        state["last_health_probe_detail"] = str(detail or "")


def _record_service_wedge_recovery(state_lock, state) -> None:
    with state_lock:
        state["wedge_recovery_total"] += 1
        state["last_wedge_recovery_ts"] = time.time()


def _restart_rtl_service(
    *,
    service_label: str,
    target_unit: str,
    peer_unit: str,
    stats_path: str,
    mount_name: str,
    state_lock: threading.Lock,
    state: dict,
    reason: str,
) -> Tuple[bool, str]:
    """Generic sequenced-restart worker for either rtl-airband-airband or
    rtl-airband-ground.

    Why this is a shared helper instead of two copies: the gentle/
    escalate dance, sdrplay daemon coordination, OP25 coordination,
    and post-start probe logic are all identical between the two
    services.  Only three things differ: the unit name, the peer
    service that needs to come down during escalation, and the stats
    file the probe reads.  Parameterizing those keeps the recovery
    logic in one place — what we tune for one band, both bands get.

    Service ordering on a full-stack escalation:

      gentle:    stop TARGET → start TARGET → probe
      escalate:  stop TARGET + PEER + OP25 → bounce daemon →
                 start airband (Master) → probe airband →
                 start ground (Slave)  → start OP25
    """
    digital_unit = str(UNITS.get("digital") or "").strip()
    digital_exists = _unit_configured(digital_unit) if digital_unit else False
    peer_exists = _unit_configured(peer_unit) if peer_unit else False
    sdrplay_unit = (
        os.getenv("UNIT_SDRPLAY")
        or os.getenv("SDRPLAY_SERVICE_NAME")
        or "sdrplay"
    ).strip()
    sdrplay_exists = _unit_configured(sdrplay_unit)

    sdrplay_settle_sec = _env_float("RTL_RESTART_SDRPLAY_SETTLE_SEC", 3.0)
    rtl_settle_sec = _env_float("RTL_RESTART_SETTLE_SEC", 6.0)
    digital_settle_sec = _env_float("RTL_RESTART_DIGITAL_SETTLE_SEC", 2.0)

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

    _record_service_restart_attempt(state_lock, state, reason)
    print(
        f"restart_{service_label}: reason={reason!r} probe_enabled={probe_enabled} "
        f"probe_timeout_sec={probe_timeout_sec}",
        flush=True,
    )

    if not target_unit:
        return False, f"{service_label}: target unit not configured"

    def _attempt(force_sdrplay_restart: bool, attempt_label: str) -> Tuple[bool, str]:
        # SB7.2 Task 3: escalation stops the whole analog+digital stack,
        # bounces the SHARED sdrplay daemon and restarts everything in
        # dependency order — hold the bounce lock across the whole
        # sequence so a concurrent restart_digital (or the other band's
        # escalation) can't interleave its own bounce (see the
        # _SDRPLAY_RESTART_LOCK comment).  Gentle attempts stay unguarded
        # except for the daemon check-and-bounce block inside.
        if force_sdrplay_restart:
            with _SDRPLAY_RESTART_LOCK:
                return _attempt_impl(force_sdrplay_restart, attempt_label)
        return _attempt_impl(force_sdrplay_restart, attempt_label)

    def _attempt_impl(force_sdrplay_restart: bool, attempt_label: str) -> Tuple[bool, str]:
        _stop_unit(target_unit, use_sudo=True)
        _kill_unit(target_unit)

        # On escalation: bring down the peer service AND OP25 so the
        # sdrplay daemon can fully clear all client state before its
        # restart.  Gentle attempts leave peers running.
        peer_was_stopped = False
        op25_was_stopped = False
        if force_sdrplay_restart:
            if peer_exists and unit_active(peer_unit):
                print(
                    f"restart_{service_label}[{attempt_label}]: stopping peer "
                    f"{peer_unit} to free sdrplay daemon",
                    flush=True,
                )
                _stop_unit(peer_unit, use_sudo=True)
                _kill_unit(peer_unit)
                peer_was_stopped = True
            if digital_exists and unit_active(digital_unit):
                print(
                    f"restart_{service_label}[{attempt_label}]: stopping OP25 "
                    f"to free sdrplay daemon",
                    flush=True,
                )
                _stop_unit(digital_unit, use_sudo=True)
                _kill_unit(digital_unit)
                op25_was_stopped = True

        units_to_reset = [target_unit]
        if peer_was_stopped:
            units_to_reset.append(peer_unit)
        if op25_was_stopped:
            units_to_reset.append(digital_unit)
        _reset_failed_units(units_to_reset)

        if sdrplay_exists:
            # RLock: no-op re-acquire on escalation (outer wrapper already
            # holds it); the real guard for GENTLE attempts, whose
            # dead/unhealthy-daemon bounce must not interleave with
            # another flow's escalation.  check + bounce are atomic.
            with _SDRPLAY_RESTART_LOCK:
                daemon_alive = _sdrplay_daemon_alive()
                if force_sdrplay_restart or not daemon_alive:
                    print(
                        f"restart_{service_label}[{attempt_label}]: bouncing sdrplay "
                        f"daemon (force={force_sdrplay_restart}, alive={daemon_alive})",
                        flush=True,
                    )
                    ok, err = _restart_unit(sdrplay_unit, use_sudo=True)
                    if not ok:
                        return False, f"sdrplay restart: {err}"
                    if sdrplay_settle_sec > 0:
                        time.sleep(sdrplay_settle_sec)
                else:
                    healthy, hreason = _sdrplay_daemon_healthy()
                    if not healthy:
                        print(
                            f"restart_{service_label}[{attempt_label}]: sdrplay "
                            f"daemon needs restart ({hreason})",
                            flush=True,
                        )
                        ok, err = _restart_unit(sdrplay_unit, use_sudo=True)
                        if not ok:
                            return False, f"sdrplay restart: {err}"
                        if sdrplay_settle_sec > 0:
                            time.sleep(sdrplay_settle_sec)

        # Service start ordering: airband (Master) ALWAYS comes up
        # before ground (Slave).  Even if the operator restarted the
        # ground service, the Master must be open before the Slave's
        # sdrplay_api_Open call.  We resolve the airband unit by
        # checking if the target IS airband or if its peer is.
        airband_unit = (
            target_unit if service_label == "rtl_airband" else peer_unit
        )
        ground_unit = (
            target_unit if service_label == "rtl_ground" else peer_unit
        )

        if force_sdrplay_restart and peer_was_stopped:
            # Full stack restart: start airband first, probe it healthy,
            # then start ground.  This is the canonical clean-cycle.
            ok, err = _start_unit(airband_unit, use_sudo=True)
            if not ok:
                return False, f"airband start: {err}"
            if rtl_settle_sec > 0:
                time.sleep(rtl_settle_sec)
            if probe_enabled:
                probe_ok, probe_detail = _wait_for_rtl_airband_health(
                    timeout_sec=probe_timeout_sec,
                    poll_interval_sec=probe_poll_sec,
                    stats_path=RTL_AIRBAND_AIRBAND_STATS_PATH,
                    mount_name=RTL_AIRBAND_AIRBAND_MOUNT,
                )
                if not probe_ok:
                    _record_service_health_probe(
                        state_lock, state, "wedged",
                        f"airband (Master) probe failed: {probe_detail}",
                    )
                    return False, f"airband probe failed: {probe_detail}"
            ok, err = _start_unit(ground_unit, use_sudo=True)
            if not ok:
                return False, f"ground start: {err}"
            if rtl_settle_sec > 0:
                time.sleep(rtl_settle_sec)
            if probe_enabled:
                # Probe whichever service the caller is restarting.
                # Target's stats path is what matters for THIS call's
                # success — the peer's probe success is implicit if
                # the start succeeded.
                probe_ok, probe_detail = _wait_for_rtl_airband_health(
                    timeout_sec=probe_timeout_sec,
                    poll_interval_sec=probe_poll_sec,
                    stats_path=stats_path,
                    mount_name=mount_name,
                )
                _record_service_health_probe(
                    state_lock, state,
                    "ok" if probe_ok else "wedged",
                    probe_detail,
                )
                if not probe_ok:
                    return False, f"post-start probe failed: {probe_detail}"
            else:
                _record_service_health_probe(
                    state_lock, state, "skipped", "probe disabled by env"
                )
        else:
            # Gentle path: just bring the one target back up.
            ok, err = _start_unit(target_unit, use_sudo=True)
            if not ok:
                return False, f"{service_label} start: {err}"
            # Cascade-restart safety net: stopping target_unit also stopped
            # peer via Requires=.  Systemd Upholds= (drop-in) will restart
            # it, but bring it up explicitly here too so we don't race the
            # post-start probe against an empty mountpoint.
            if peer_unit and peer_exists and not unit_active(peer_unit):
                peer_ok, peer_err = _start_unit(peer_unit, use_sudo=True)
                if not peer_ok:
                    print(
                        f"restart_{service_label}[gentle]: WARN peer "
                        f"{peer_unit} restart after gentle {target_unit} "
                        f"failed: {peer_err}",
                        flush=True,
                    )
            if rtl_settle_sec > 0:
                time.sleep(rtl_settle_sec)
            if probe_enabled:
                probe_ok, probe_detail = _wait_for_rtl_airband_health(
                    timeout_sec=probe_timeout_sec,
                    poll_interval_sec=probe_poll_sec,
                    stats_path=stats_path,
                    mount_name=mount_name,
                )
                _record_service_health_probe(
                    state_lock, state,
                    "ok" if probe_ok else "wedged",
                    probe_detail,
                )
                if not probe_ok:
                    return False, f"post-start probe failed: {probe_detail}"
            else:
                _record_service_health_probe(
                    state_lock, state, "skipped", "probe disabled by env"
                )

        # Finally, if we'd stopped OP25 for the daemon bounce, bring
        # it back.  Failure here doesn't fail the analog restart —
        # surface in the probe detail and let the operator hit
        # Restart Digital manually.
        if op25_was_stopped:
            ok_d, err_d = _start_unit(digital_unit, use_sudo=True)
            if not ok_d:
                _record_service_health_probe(
                    state_lock, state,
                    "ok-but-op25-failed-to-restart",
                    f"{service_label} healthy; op25 restart err: {err_d}",
                )
                print(
                    f"restart_{service_label}[{attempt_label}]: WARN op25 "
                    f"restart failed: {err_d} ({service_label} still ok)",
                    flush=True,
                )
            elif digital_settle_sec > 0:
                time.sleep(digital_settle_sec)

        return True, ""

    # Gentle attempt 1: just the target service, no daemon touch.
    ok, err = _attempt(force_sdrplay_restart=False, attempt_label="gentle")
    if ok:
        return True, ""
    print(
        f"restart_{service_label}: gentle attempt failed ({err}); "
        f"escalating to wedge recovery",
        flush=True,
    )

    last_err = err
    for escalation in range(1, max_escalations + 1):
        ok, err = _attempt(
            force_sdrplay_restart=True,
            attempt_label=f"escalation-{escalation}",
        )
        _record_service_wedge_recovery(state_lock, state)
        if ok:
            print(
                f"restart_{service_label}: wedge recovery succeeded on "
                f"escalation {escalation}",
                flush=True,
            )
            return True, ""
        last_err = err

    return False, (
        f"wedge recovery exhausted after {max_escalations} escalations: {last_err}"
    )


def restart_rtl_airband(reason: str = "unspecified") -> Tuple[bool, str]:
    """Sequenced recovery for the airband (Master / Tuner 1) service.

    Gentle path touches only ``rtl-airband-airband.service``.
    Escalation cycles airband + ground + OP25 together with a forced
    sdrplay daemon bounce, then brings them up in dependency order
    (airband → ground → op25).
    """
    return _restart_rtl_service(
        service_label="rtl_airband",
        target_unit=str(UNITS.get("rtl_airband") or "").strip(),
        peer_unit=str(UNITS.get("rtl_ground") or "").strip(),
        stats_path=RTL_AIRBAND_AIRBAND_STATS_PATH,
        mount_name=RTL_AIRBAND_AIRBAND_MOUNT,
        state_lock=_RTL_AIRBAND_RESTART_STATE_LOCK,
        state=_RTL_AIRBAND_RESTART_STATE,
        reason=reason,
    )


def restart_rtl_ground(reason: str = "unspecified") -> Tuple[bool, str]:
    """Sequenced recovery for the ground (Slave / Tuner 2) service.

    Gentle path touches only ``rtl-airband-ground.service``.
    Escalation cycles airband + ground + OP25 — airband must come up
    BEFORE ground because the SDRplay Slave open requires the Master
    to already be present.
    """
    return _restart_rtl_service(
        service_label="rtl_ground",
        target_unit=str(UNITS.get("rtl_ground") or "").strip(),
        peer_unit=str(UNITS.get("rtl_airband") or "").strip(),
        stats_path=RTL_AIRBAND_GROUND_STATS_PATH,
        mount_name=RTL_AIRBAND_GROUND_MOUNT,
        state_lock=_RTL_GROUND_RESTART_STATE_LOCK,
        state=_RTL_GROUND_RESTART_STATE,
        reason=reason,
    )
