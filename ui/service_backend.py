"""Service-manager backend abstraction (SB7.2 workstream C).

Why this module exists
----------------------
ui/systemd.py grew ~1000 lines of hard-won recovery logic (gentle→escalate
restart cascades, sdrplay daemon coordination, post-start health probes)
on top of ~10 tiny primitives that shell out to ``systemctl``.  The SB7
"no third state" program moves the whole stack onto a single Mac mini,
where there is no systemd — service supervision is launchd.  Rewriting
the recovery logic would throw away months of debugged scar tissue, so
instead the primitives are abstracted here:

  * ``ServiceBackend``   — the abstract contract (one method per primitive).
  * ``SystemdBackend``   — the CURRENT subprocess logic, moved verbatim
                           from ui/systemd.py.  Bit-identical behavior,
                           including the sudo-fallback dance for
                           ``systemctl show`` (see active_enter_epoch).
  * ``LaunchdBackend``   — maps the same logical operations onto
                           ``launchctl`` for the macOS port.

ui/systemd.py keeps its module-level function names (``unit_active``,
``_restart_unit``, ...) as thin shims that delegate to ``get_backend()``,
so the recovery logic and its extensive test corpus are untouched.

Backend selection is via env ``SCANNER_SERVICE_BACKEND`` ("systemd"
default, "launchd"), resolved once and cached — services don't change
init system mid-flight, and the primitives are called from hot status
paths where re-parsing env on every call would be wasted work.
"""
import abc
import os
import subprocess
import threading
import time
from typing import Iterable, List, Optional, Tuple


def _enabled_from_result(result: "subprocess.CompletedProcess[str]") -> Tuple[bool, str]:
    """Interpret ``systemctl is-enabled`` output.  Moved verbatim from
    ui/systemd.py (SB7.2); only "enabled"/"enabled-runtime" count —
    "static", "masked", "disabled" and error output all read as False."""
    if result.returncode != 0:
        return False, (result.stdout or result.stderr or "").strip()
    state = str(result.stdout or "").strip().lower()
    return state in ("enabled", "enabled-runtime"), state


class ServiceBackend(abc.ABC):
    """Contract for the low-level service-manager primitives.

    ``unit`` everywhere is the logical unit name as stored in
    ``config.UNITS`` — a systemd unit name ("gr-demod@airband") on Linux,
    a launchd label ("com.scannerproject.rtl-airband") on macOS.  The
    UNITS env-file indirection (UNIT_RTL etc.) is what lets one codebase
    address both namespaces without translation tables.
    """

    #: short backend identifier ("systemd" / "launchd"); used by callers
    #: that must gate genuinely backend-specific probes (e.g. the
    #: ``journalctl -k`` segfault scan in ui/systemd.py).
    name: str = "abstract"

    @abc.abstractmethod
    def active(self, unit: str) -> bool:
        """True iff the unit/service is currently running."""

    @abc.abstractmethod
    def exists(self, unit: str) -> bool:
        """True iff the unit/service is known to the service manager."""

    @abc.abstractmethod
    def enabled(self, unit: str, use_sudo: bool = False) -> bool:
        """True iff the unit is enabled to start at boot/login."""

    @abc.abstractmethod
    def start(self, unit: str, use_sudo: bool = False) -> Tuple[bool, str]:
        """Start the unit.  Returns (ok, error_detail)."""

    @abc.abstractmethod
    def stop(self, unit: str, use_sudo: bool = False) -> Tuple[bool, str]:
        """Stop the unit's process while leaving the service loadable /
        startable later.  Returns (ok, error_detail)."""

    @abc.abstractmethod
    def restart(self, unit: str, use_sudo: bool = False) -> Tuple[bool, str]:
        """Restart the unit.  Returns (ok, error_detail)."""

    @abc.abstractmethod
    def kill(self, unit: str) -> None:
        """SIGKILL the unit's process.  Best-effort; never raises —
        callers use this as escalation after a polite stop and must not
        abort the recovery cascade because the kill itself hiccuped."""

    @abc.abstractmethod
    def reset_failed(self, units: Iterable[str]) -> None:
        """Clear any 'failed'/start-limit latch so the next start is not
        refused.  Best-effort; never raises."""

    @abc.abstractmethod
    def active_enter_epoch(self, unit: str) -> Optional[float]:
        """Epoch seconds when the unit last became active, or None when
        unknown.  Callers throughout ui/handlers.py and ui/op25_adapter.py
        already tolerate None (the systemd impl returns it on any parse or
        permission failure), so None is the universal 'don't know'."""

    @abc.abstractmethod
    def restart_count(self, unit: str) -> Optional[int]:
        """Monotonic restart counter for crash-loop detection, or None
        when the manager can't say."""

    @abc.abstractmethod
    def run(self, args: List[str], use_sudo: bool = False) -> "subprocess.CompletedProcess[str]":
        """Generic escape hatch: run a raw service-manager command.

        Only for operations with no portable abstraction (systemd timer
        enable/disable, host reboot).  Callers using this are by
        definition backend-specific and must be prepared for it to fail
        on other backends.
        """


class SystemdBackend(ServiceBackend):
    """systemctl-backed implementation.

    Every method body below is the pre-SB7.2 ui/systemd.py logic moved
    verbatim (only ``_run_systemctl(...)`` became ``self.run(...)``).
    Do NOT "clean up" the error-string formats or the sudo-fallback
    heuristics — the recovery cascades and their tests pin them.
    """

    name = "systemd"

    def run(self, args: List[str], use_sudo: bool = False) -> "subprocess.CompletedProcess[str]":
        # verbatim: ui/systemd.py::_run_systemctl
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

    def active(self, unit: str) -> bool:
        # verbatim: ui/systemd.py::unit_active — bespoke invocation (no
        # output capture; --quiet) kept as-is rather than funneled
        # through run() so behavior stays bit-identical.
        return subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0

    def exists(self, unit: str) -> bool:
        # verbatim: ui/systemd.py::unit_exists
        result = subprocess.run(
            ["systemctl", "show", "-p", "LoadState", "--value", unit],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if result.returncode != 0:
            return False
        return result.stdout.strip() != "not-found"

    def enabled(self, unit: str, use_sudo: bool = False) -> bool:
        # verbatim: ui/systemd.py::unit_enabled
        try:
            result = self.run(["is-enabled", unit], use_sudo=use_sudo)
        except Exception:
            return False
        enabled, _state = _enabled_from_result(result)
        return enabled

    def restart(self, unit: str, use_sudo: bool = False) -> Tuple[bool, str]:
        # verbatim: ui/systemd.py::_restart_unit
        try:
            result = self.run(["restart", unit], use_sudo=use_sudo)
        except Exception as e:
            return False, str(e)
        if result.returncode == 0:
            return True, ""
        err = (result.stderr or result.stdout or "").strip()
        if not err:
            err = f"restart failed (code {result.returncode})"
        return False, err

    def start(self, unit: str, use_sudo: bool = False) -> Tuple[bool, str]:
        # verbatim: ui/systemd.py::_start_unit
        try:
            result = self.run(["start", unit], use_sudo=use_sudo)
        except Exception as e:
            return False, str(e)
        if result.returncode == 0:
            return True, ""
        err = (result.stderr or result.stdout or "").strip()
        if not err:
            err = f"start failed (code {result.returncode})"
        return False, err

    def stop(self, unit: str, use_sudo: bool = False) -> Tuple[bool, str]:
        # verbatim: ui/systemd.py::_stop_unit
        try:
            result = self.run(["stop", unit], use_sudo=use_sudo)
        except Exception as e:
            return False, str(e)
        if result.returncode == 0:
            return True, ""
        err = (result.stderr or result.stdout or "").strip()
        if not err:
            err = f"stop failed (code {result.returncode})"
        return False, err

    def kill(self, unit: str) -> None:
        # verbatim: ui/systemd.py::_kill_unit (always sudo — SIGKILL of a
        # system unit is never allowed unprivileged)
        unit = str(unit or "").strip()
        if not unit:
            return
        try:
            self.run(["kill", "-s", "SIGKILL", unit], use_sudo=True)
        except Exception:
            pass

    def reset_failed(self, units: Iterable[str]) -> None:
        # verbatim: ui/systemd.py::_reset_failed_units
        names = [str(unit or "").strip() for unit in units if str(unit or "").strip()]
        if not names:
            return
        try:
            self.run(["reset-failed"] + names, use_sudo=True)
        except Exception:
            pass

    def active_enter_epoch(self, unit: str) -> Optional[float]:
        # verbatim: ui/systemd.py::unit_active_enter_epoch — including the
        # sudo fallback: unprivileged `systemctl show` can be rejected with
        # "Interactive authentication required" / "Access denied" on some
        # polkit configs; only THOSE failures justify retrying with sudo
        # (a plain "unit not found" must stay a clean None, no sudo spam).
        # tests/test_systemd.py::UnitActiveEnterEpochTests pins this.
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
            result = self.run(["show", "-p", "ActiveEnterTimestampUSec", "--value", unit], use_sudo=False)
            epoch = parse_epoch(result)
            if epoch is not None:
                return epoch
            if not needs_sudo_fallback(result):
                return None
            result = self.run(["show", "-p", "ActiveEnterTimestampUSec", "--value", unit], use_sudo=True)
            return parse_epoch(result)
        except Exception:
            return None

    def restart_count(self, unit: str) -> Optional[int]:
        # verbatim: ui/systemd.py::unit_restart_count (same sudo-fallback
        # heuristic as active_enter_epoch)
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
            result = self.run(["show", "-p", "NRestarts", "--value", unit], use_sudo=False)
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
            result = self.run(["show", "-p", "NRestarts", "--value", unit], use_sudo=True)
            return parse_count(result)
        except Exception:
            return None


class LaunchdBackend(ServiceBackend):
    """launchctl-backed implementation for the Mac mini port.

    Domain choice
    -------------
    Defaults to the *user* GUI session domain ``gui/<uid>`` (i.e. jobs
    installed as LaunchAgents), overridable via env
    ``SCANNER_LAUNCHD_DOMAIN``.  USB SDR device access on macOS needs a
    logged-in user session (IOKit device claims from a LaunchDaemon in the
    system domain hit sandbox/TCC friction), so the SDR services live in
    the user session by design — see docs/mac-mini-port.md.

    stop() semantics — why ``bootout`` and not ``kill SIGTERM``
    -----------------------------------------------------------
    ``systemctl stop`` guarantees two things the recovery cascades depend
    on: (1) the process is actually down afterwards and STAYS down, and
    (2) the service remains loadable so a later start works.  launchd
    offers two candidates:

      * ``launchctl kill SIGTERM``  — terminates the process, but for a
        KeepAlive job (which our long-running SDR daemons are — launchd's
        analog of Restart=on-failure) launchd RESPAWNS it immediately.
        That silently breaks the stop-all → sdrplay-bounce → ordered-start
        sequence: the "stopped" client would reconnect to the daemon in
        the middle of its bounce, which is precisely the state-corruption
        pattern the cascade exists to avoid.
      * ``launchctl bootout``       — unloads the job from the domain;
        launchd SIGTERMs the process and does NOT respawn it.  The plist
        stays on disk, and start() below re-bootstraps from
        ~/Library/LaunchAgents when the job isn't loaded, so the service
        remains "loadable" from the caller's point of view.

    ``bootout`` is therefore the faithful mapping; the asymmetry (a
    stopped service is *unloaded* rather than loaded-but-inactive) is
    hidden entirely inside this class by start()'s bootstrap fallback.

    sudo
    ----
    ``use_sudo`` is accepted for signature parity and ignored: sudo'd
    launchctl talks to root's Mach bootstrap namespace, NOT the gui/<uid>
    domain, so "sudo for more privilege" would actually make every call
    miss the services entirely.  The UI process runs as the login user
    that owns the domain.
    """

    name = "launchd"

    def __init__(self) -> None:
        domain = str(os.getenv("SCANNER_LAUNCHD_DOMAIN", "") or "").strip()
        if not domain:
            domain = f"gui/{os.getuid()}"
        self._domain = domain

    # -- plumbing -----------------------------------------------------

    def _target(self, unit: str) -> str:
        return f"{self._domain}/{unit}"

    def _launchctl(self, args: List[str]) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            ["launchctl"] + list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

    def _plist_path(self, unit: str) -> str:
        return os.path.expanduser(f"~/Library/LaunchAgents/{unit}.plist")

    @staticmethod
    def _err_text(result: "subprocess.CompletedProcess[str]", fallback: str) -> str:
        err = (result.stderr or result.stdout or "").strip()
        return err if err else fallback

    @staticmethod
    def _not_loaded(result: "subprocess.CompletedProcess[str]") -> bool:
        """True when launchctl failed because the job isn't loaded in the
        domain (as opposed to a real operational failure).  launchctl's
        wording varies across macOS releases, so match the family."""
        detail = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
        return (
            "could not find service" in detail
            or "no such process" in detail
            or "service not found" in detail
        )

    # -- primitives ---------------------------------------------------

    def active(self, unit: str) -> bool:
        # `launchctl print gui/<uid>/<label>` exits non-zero when the job
        # isn't loaded and reports `state = running` when the process is
        # up.  Only "running" counts — "waiting"/"spawn scheduled" map to
        # systemd's activating/inactive, which unit_active() also reports
        # as False (is-active --quiet is strict).
        try:
            result = self._launchctl(["print", self._target(unit)])
        except Exception:
            return False
        if result.returncode != 0:
            return False
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("state") and "=" in line:
                return line.split("=", 1)[1].strip() == "running"
        return False

    def exists(self, unit: str) -> bool:
        # Loaded-in-domain is launchd's closest notion of "unit exists".
        # A plist sitting on disk but not bootstrapped reads as False —
        # matching systemd where a masked/not-found unit reads False and
        # the callers' _unit_configured() gating then skips it.
        try:
            result = self._launchctl(["print", self._target(unit)])
        except Exception:
            return False
        return result.returncode == 0

    def enabled(self, unit: str, use_sudo: bool = False) -> bool:
        # Best-effort: launchd keeps an explicit disabled-overrides list
        # per domain (`launchctl print-disabled`).  A label absent from
        # that list is enabled by default once its plist is present.
        # Wording differs across macOS releases ("=> true" vs
        # "=> disabled"), so match both.
        try:
            result = self._launchctl(["print-disabled", self._domain])
        except Exception:
            return False
        if result.returncode != 0:
            # Can't read the override list — fall back to "is it loaded",
            # the strongest remaining signal.
            return self.exists(unit)
        for line in (result.stdout or "").splitlines():
            if f'"{unit}"' in line and "=>" in line:
                verdict = line.split("=>", 1)[1].strip().strip(";").lower()
                return not (verdict.startswith("true") or verdict.startswith("disabled"))
        return True

    def start(self, unit: str, use_sudo: bool = False) -> Tuple[bool, str]:
        # kickstart starts a loaded-but-not-running job.  If the job isn't
        # loaded (fresh boot into a stopped state, or after our bootout-
        # based stop()), bootstrap it from ~/Library/LaunchAgents first.
        target = self._target(unit)
        try:
            result = self._launchctl(["kickstart", target])
        except Exception as e:
            return False, str(e)
        if result.returncode == 0:
            return True, ""

        plist = self._plist_path(unit)
        if os.path.isfile(plist):
            try:
                boot = self._launchctl(["bootstrap", self._domain, plist])
                if boot.returncode == 0:
                    # Bootstrap loads AND launches (RunAtLoad) or at least
                    # loads; kickstart again to guarantee a running process
                    # either way.
                    retry = self._launchctl(["kickstart", target])
                    if retry.returncode == 0:
                        return True, ""
                    return False, self._err_text(
                        retry, f"start failed after bootstrap (code {retry.returncode})"
                    )
                return False, self._err_text(
                    boot, f"bootstrap failed (code {boot.returncode})"
                )
            except Exception as e:
                return False, str(e)

        err = self._err_text(result, f"start failed (code {result.returncode})")
        if self._not_loaded(result):
            err = f"{err}; no plist at {plist} to bootstrap from"
        return False, err

    def stop(self, unit: str, use_sudo: bool = False) -> Tuple[bool, str]:
        # bootout, NOT kill SIGTERM — see class docstring for the full
        # rationale (KeepAlive respawn would defeat the recovery
        # cascades' stop-all guarantee).
        try:
            result = self._launchctl(["bootout", self._target(unit)])
        except Exception as e:
            return False, str(e)
        if result.returncode == 0:
            return True, ""
        if self._not_loaded(result):
            # Already stopped/unloaded.  `systemctl stop` of an inactive
            # unit exits 0, and the cascades stop units unconditionally
            # before restarting them — "already stopped" must be success.
            return True, ""
        return False, self._err_text(result, f"stop failed (code {result.returncode})")

    def restart(self, unit: str, use_sudo: bool = False) -> Tuple[bool, str]:
        # kickstart -k kills the running instance (if any) and relaunches
        # — launchd's native restart.  If the job simply isn't loaded,
        # fall through to start() for the bootstrap path so restart of a
        # stopped service behaves like `systemctl restart` (which starts
        # inactive units).
        try:
            result = self._launchctl(["kickstart", "-k", self._target(unit)])
        except Exception as e:
            return False, str(e)
        if result.returncode == 0:
            return True, ""
        if self._not_loaded(result):
            return self.start(unit)
        return False, self._err_text(result, f"restart failed (code {result.returncode})")

    def kill(self, unit: str) -> None:
        # Escalation kill.  Note: unlike stop(), this leaves the job
        # loaded, so a KeepAlive job may respawn — same as systemd where
        # `systemctl kill -s SIGKILL` on a Restart=always unit respawns.
        # The cascades always call kill AFTER stop, so in practice the
        # job is already booted out and this is a no-op safety net.
        unit = str(unit or "").strip()
        if not unit:
            return
        try:
            self._launchctl(["kill", "SIGKILL", self._target(unit)])
        except Exception:
            pass

    def reset_failed(self, units: Iterable[str]) -> None:
        # Deliberate no-op.  systemd latches crash-looping units into a
        # persistent "failed" state (StartLimitBurst) that REFUSES further
        # starts until `systemctl reset-failed` clears it — that's the
        # only reason the cascades call this.  launchd has no such latch:
        # kickstart always attempts a spawn, and its respawn throttling
        # (ThrottleInterval) is purely time-based with no CLI to clear.
        # There is nothing to reset, so doing nothing IS the faithful
        # mapping.
        return

    def active_enter_epoch(self, unit: str) -> Optional[float]:
        # Best-effort: launchd doesn't expose an activation timestamp, so
        # approximate with the process start time — PID from `launchctl
        # print`, then `ps -o lstart=`.  Slightly later than systemd's
        # ActiveEnterTimestamp (exec vs. cgroup-activation) but callers
        # only use this for "seconds since (re)start" coarse math.
        # Returns None on any failure — the proven fallback contract: the
        # systemd impl returns None on parse/permission failures and every
        # caller (ui/handlers.py uptime rows, ui/op25_adapter.py status)
        # already handles it.
        try:
            result = self._launchctl(["print", self._target(unit)])
            if result.returncode != 0:
                return None
            pid: Optional[int] = None
            for line in (result.stdout or "").splitlines():
                line = line.strip()
                if line.startswith("pid") and "=" in line:
                    val = line.split("=", 1)[1].strip()
                    if val.isdigit():
                        pid = int(val)
                    break
            if pid is None or pid <= 0:
                return None
            ps = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
            if ps.returncode != 0:
                return None
            lstart = (ps.stdout or "").strip()
            if not lstart:
                return None
            # lstart format: "Fri Jul  4 09:15:02 2026" — exactly
            # time.strptime's default format ("%a %b %d %H:%M:%S %Y").
            return time.mktime(time.strptime(lstart))
        except Exception:
            return None

    def restart_count(self, unit: str) -> Optional[int]:
        # Best-effort 0: launchd publishes no NRestarts analog (`launchctl
        # print` has spawn info but no monotonic restart counter).  The
        # sole consumer is the crash-loop detector in ui/handlers.py,
        # which samples DELTAS over a sliding window — a constant 0 yields
        # delta 0, i.e. loop detection quietly disabled on launchd rather
        # than false-alarming.  (None would read as "probe broken" in the
        # rollup; a steady 0 is the honest "no evidence of looping".)
        return 0

    def run(self, args: List[str], use_sudo: bool = False) -> "subprocess.CompletedProcess[str]":
        # The escape hatch is inherently systemctl-shaped (its two in-tree
        # callers pass systemd verb-args: `enable --now <timer>` for the
        # BT-heal timer, `reboot` for host reboot).  There is no faithful
        # generic translation, so fail loudly instead of guessing — both
        # callers wrap in try/except Exception and surface (False, msg).
        # The BT-heal timer is a Linux/BlueZ concern that doesn't exist on
        # the Mac deployment; a mac reboot path can be added deliberately
        # if/when the UI needs it.
        raise NotImplementedError(
            "ServiceBackend.run() has no launchd mapping "
            f"(args={list(args)!r}); use the typed primitives instead"
        )


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

_BACKEND: Optional[ServiceBackend] = None
_BACKEND_LOCK = threading.Lock()


def get_backend() -> ServiceBackend:
    """Resolve the process-wide ServiceBackend (cached after first call).

    Env ``SCANNER_SERVICE_BACKEND``: "systemd" (default) or "launchd".
    An unrecognized value raises ValueError on the FIRST service call
    rather than silently falling back to systemd — a typo'd env file on
    the Mac would otherwise leave every restart button no-oping against a
    non-existent systemctl, which is exactly the class of silent
    misconfiguration (see the UNITS ghost-name outage) SB7 is stamping out.
    """
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    with _BACKEND_LOCK:
        if _BACKEND is None:
            raw = str(os.getenv("SCANNER_SERVICE_BACKEND", "") or "").strip().lower()
            choice = raw or "systemd"
            if choice == "systemd":
                _BACKEND = SystemdBackend()
            elif choice == "launchd":
                _BACKEND = LaunchdBackend()
            else:
                raise ValueError(
                    f"SCANNER_SERVICE_BACKEND={raw!r} is not a known backend "
                    "(expected 'systemd' or 'launchd')"
                )
    return _BACKEND


def _reset_backend_for_tests() -> None:
    """Drop the cached backend so tests can re-dispatch after mutating
    SCANNER_SERVICE_BACKEND / SCANNER_LAUNCHD_DOMAIN.  Test-only hook —
    production code must never toggle backends mid-flight."""
    global _BACKEND
    with _BACKEND_LOCK:
        _BACKEND = None
