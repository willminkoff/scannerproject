"""sb3.reconciler.observer — the 30s read-classify-log loop.  PASSIVE.

Runs as ``com.scannerproject.sb3-reconciler``.  Every pass it reads live backend
state, hands it to :mod:`sb3.reconciler.classifier`, and writes one structured
line per role to ``~/Library/Logs/sb3/reconciler.log``.  Then it does nothing.

**This module must never acquire the ability to act.**  Phase 4.1's whole
purpose is to watch the box for a night and find out what drift really looks
like before anything is allowed to correct it.  Two structural guarantees keep
that honest, rather than relying on nobody adding a write later:

  * Every read goes through :mod:`sb3.backends`, whose module contract is
    read-only and which contains no write verb anywhere.  This file never opens
    a socket or builds a request itself.
  * ``tests/test_sb3_reconciler.py`` greps this package's source for PATCH /
    POST / PUT / DELETE and for the writing entry points (``translator.apply``,
    ``SDRangelClient``), and fails if one appears.  Passivity is asserted, not
    assumed.

It also inherits the §4.3 SIGTERM contract from ``sb3/agents/_stub.py``: exit
cleanly and promptly, because ``launchctl bootout`` SIGTERMs and does not wait,
and the teardown ordering assumes each agent is gone before the next step.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import signal
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .. import backends, ownership, sdrtrunk_client
from ..state import State
from . import actions as A
from . import classifier as C
from . import config as CFG
from . import safety as S

# -- defaults (overridable via ~/sb3/config.json) ---------------------------

DEFAULT_POLL_SEC = 30.0
DEFAULT_LOG_PATH = "~/Library/Logs/sb3/reconciler.log"
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024      # ~10 MB, per the 4.1 brief
DEFAULT_LOG_BACKUPS = 3
DEFAULT_CONFIG_PATH = "~/sb3/config.json"

#: Poll the stop flag far more often than we classify, so a SIGTERM is acted on
#: in well under a second instead of being slept through for up to 30s.
_TICK_SEC = 0.25

DIGITAL_MOUNT = "neptune-trunk.mp3"

#: Where quarantine state is persisted (shared with sb3.reconcilercmd).
QUARANTINE_PATH = "~/.local/state/sb3/reconciler_quarantine.json"


def load_config(path: Optional[Path] = None) -> Dict:
    """Read ~/sb3/config.json if present. Missing/malformed → defaults.

    Fail-SOFT on purpose, unlike profile loading: a typo in an optional tuning
    file must not stop the observer from observing.  A profile that is wrong
    changes the radio; a config that is wrong changes a poll interval.
    """
    p = Path(os.path.expanduser(str(path or DEFAULT_CONFIG_PATH)))
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data.get("reconciler", data) if isinstance(data.get("reconciler", data), dict) else {}


class Observer:
    """Reads, classifies, logs. Holds no backend state and asserts nothing."""

    def __init__(self, *, config: Optional[Dict] = None,
                 state: Optional[State] = None,
                 log_path: Optional[Path] = None,
                 rc: Optional["CFG.ReconcilerConfig"] = None) -> None:
        cfg = config if config is not None else load_config()
        self.poll_sec = float(cfg.get("poll_interval_sec", DEFAULT_POLL_SEC))
        self.log_path = Path(os.path.expanduser(
            str(log_path or cfg.get("log_path", DEFAULT_LOG_PATH))))
        self.log_max_bytes = int(cfg.get("log_max_bytes", DEFAULT_LOG_MAX_BYTES))
        self.log_backups = int(cfg.get("log_backups", DEFAULT_LOG_BACKUPS))
        self.state = state or State()
        self.log = logging.getLogger("sb3.reconciler")
        self._stop = False
        self._last_backend_pids: Optional[Dict[str, str]] = None
        self.passes = 0

        # -- Phase 4.2: the acting half. Re-read from disk every pass (see
        #    _reload_config) so `sb3-ctl reconciler enable/disable` takes effect
        #    within one poll interval without restarting the agent.
        self.rc = rc if rc is not None else CFG.load()
        self.limiter = S.RateLimiter(base=self.rc.base_backoff,
                                     cap=self.rc.max_backoff)
        # Quarantine is persisted to the SAME file `sb3-ctl reconciler
        # resume-action` clears, so a human can release it from another process.
        self.failures = S.FailureCounter(
            threshold=self.rc.quarantine_threshold,
            path=Path(os.path.expanduser(QUARANTINE_PATH)))
        self.guard = S.BackendGuard(pause_seconds=self.rc.emergency_pause_seconds)
        self.actions_taken = 0
        self._static_rc = rc is not None    # tests inject; don't reload over it

    # -- lifecycle ---------------------------------------------------------

    def _configure_logging(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            self.log_path, maxBytes=self.log_max_bytes,
            backupCount=self.log_backups)
        # The line is already fully structured (ts=… state=…), so the formatter
        # adds nothing — a second timestamp would just make it harder to grep.
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.log.addHandler(handler)
        self.log.addHandler(logging.StreamHandler(sys.stdout))
        self.log.setLevel(logging.INFO)

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._handle_signal)

    def _handle_signal(self, signum, _frame) -> None:
        # Flag only. Teardown work in a handler is how you get a half-exited
        # process that launchd then has to SIGKILL (§4.3).
        self._stop = True
        self.log.info("%s received %s — shutting down cleanly",
                      _now_iso(), signal.Signals(signum).name)

    def run(self) -> int:
        self._configure_logging()
        self._install_signal_handlers()
        self.log.info(
            "%s scope=startup state=UP pid=%d poll=%gs log=%s mode=%s "
            "actions=%s",
            _now_iso(), os.getpid(), self.poll_sec, self.log_path,
            self.rc.describe(),
            ",".join(sorted(a for a in CFG.ACTIONABLE
                            if self.rc.action_enabled(a))) or "none")

        last_pass = 0.0
        while not self._stop:
            now = time.monotonic()
            if last_pass == 0.0 or (now - last_pass) >= self.poll_sec:
                try:
                    for line in self.classify_once():
                        self.log.info(line)
                except Exception as exc:            # never die on a bad read
                    # A reconciler that crashes on one malformed response stops
                    # observing exactly when something interesting is happening.
                    self.log.info("%s scope=pass state=BROKEN issue=observer_error "
                                  "detail=%r", _now_iso(), exc)
                last_pass = now
            time.sleep(_TICK_SEC)

        self.log.info("%s scope=shutdown state=DOWN passes=%d — clean SIGTERM exit",
                      _now_iso(), self.passes)
        for h in list(self.log.handlers):
            h.flush()
            h.close()
            self.log.removeHandler(h)
        return 0

    # -- one pass ----------------------------------------------------------

    def classify_once(self) -> List[str]:
        """Read live state, classify every role, return the log lines.

        Separated from run() so a test can drive a single pass with fakes and
        assert on the exact lines, without a loop or a live box.
        """
        self.passes += 1
        self._reload_config()
        ts = _now_iso()
        lines: List[str] = []

        # -- one bounded read of everything (all read-only)
        loaded = set(backends.launchctl_loaded())
        devicesets = backends.sdrangel_devicesets()
        sdrangel_reachable = bool(devicesets)
        profiles = self.state.read_loaded_profiles()

        # If SB3 is marked killed, the layer is deliberately absent. Say so once
        # and classify nothing: "everything is missing" during an intended kill
        # is not a finding, it is the design working (§4.4).
        if self.state.is_killed():
            lines.append(f"ts={ts} scope=system state=CLEAN issue=sb3_killed "
                         f"detail=sentinel-present-observing-only")
            return lines

        mount_flags: List[bool] = []
        role_results = []

        # -- analog roles (SDRangel-backed), one line each
        for role, record in sorted(profiles.items()):
            obs = self.build_role_observation(
                role, record, devicesets, sdrangel_reachable)
            mount_flags.append(obs.mount_present)
            cls = C.classify_role(obs)
            lines.append(C.format_line(ts, cls, role=role,
                                       profile=obs.profile_name,
                                       ds=obs.deviceset_index))
            role_results.append((role, record, obs, cls))

        # -- digital role (SDRTrunk), always classified: it has no SB3 profile
        #    record but it is the always-on core and its mount is the invariant
        #    everything else is measured against.
        dig_running = "com.scannerproject.sdrtrunk" in loaded
        dig_mount = backends.mount_state(DIGITAL_MOUNT)
        mount_flags.append(dig_mount.present)
        trunk = sdrtrunk_client.observe(running=dig_running)
        # broadcaster_status is None when the log tail shows no broadcaster line
        # at all — that is "unknown", not "disconnected", and must not be
        # reported as a fault.
        bstatus = trunk.get("digital_broadcaster_status")
        dcls = C.classify_digital(C.DigitalObservation(
            profile_name=str(trunk.get("digital_profile") or "sdrtrunk"),
            running=dig_running,
            mount_status=dig_mount.http_status,
            mount_present=dig_mount.present,
            connected=(None if bstatus is None else bstatus == "Connected"),
            tuners=tuple(trunk.get("digital_tuner_targets") or ()),
        ))
        lines.append(C.format_line(ts, dcls, role="digital",
                                   profile="sdrtrunk"))

        # -- box-level findings
        missing, churn = self._backend_delta(loaded)
        scls = C.classify_system(
            sdrangel_reachable=sdrangel_reachable,
            mounts_present=mount_flags,
            backend_pids_changed=churn,
            missing_backends=missing,
        )
        if not scls.is_clean or self.passes == 1:
            lines.append(C.format_line(ts, scls, scope="system"))

        # -- Phase 4.2: act on RECOVERABLE, if and only if every brake permits.
        lines.extend(self.act_phase(ts, role_results, sdrangel_reachable))

        return lines

    # -- the acting half (Phase 4.2) ---------------------------------------

    def act_phase(self, ts: str, role_results, sdrangel_reachable: bool) -> List[str]:
        """Decide and perform at most one repair per role. Returns log lines.

        Every early return in here is a brake. They are checked in order of how
        cheap they are to check and how bad it would be to skip them.
        """
        lines: List[str] = []

        # BRAKE 0 — the config / sentinel. BROKEN is never actionable at all:
        # a wedged REST or churning backend needs `sb3-ctl kill`/`resume` by a
        # human, and "reconcile harder" is the wrong response to it.
        if not self.rc.may_act():
            return lines

        # BRAKE — emergency pause from a previous backend-PID violation.
        if self.guard.paused():
            lines.append(f"ts={ts} scope=action state=SKIPPED "
                         f"reason=emergency_pause "
                         f"remaining={self.guard.pause_remaining():.0f}s "
                         f"cause={self.guard.last_violation}")
            return lines

        planned = []
        for role, record, obs, cls in role_results:
            if cls.state != C.RECOVERABLE:
                # Converged (or BENIGN/BROKEN) — clear this role's backoff so a
                # later, unrelated problem starts from 30s and not from 240s.
                self.limiter.record_clean(role)
                continue

            action = A.action_for(cls.issues)
            if action is None:
                lines.append(f"ts={ts} scope=action role={role} state=SKIPPED "
                             f"reason=no_action_for_issue issue={','.join(cls.issues)}")
                continue
            if not self.rc.action_enabled(action):
                lines.append(f"ts={ts} scope=action role={role} action={action} "
                             f"state=SKIPPED reason=action_disabled")
                continue

            # BRAKE — broken-state pause. Never act on a reading we could not take.
            ok, why = S.readings_trustworthy(
                sdrangel_reachable=sdrangel_reachable,
                mount_status=obs.mount_status)
            if not ok:
                lines.append(f"ts={ts} scope=action role={role} action={action} "
                             f"state=SKIPPED reason={why}")
                continue

            # BRAKE — quarantine.
            if self.failures.quarantined(role, action):
                lines.append(f"ts={ts} scope=action role={role} action={action} "
                             f"state=SKIPPED reason=quarantined "
                             f"detail=needs-`sb3-ctl reconciler resume-action "
                             f"{role} {action}`")
                continue

            # BRAKE — rate limit / backoff.
            if not self.limiter.allowed(role):
                reason = ("alarm_backoff_exhausted" if self.limiter.alarmed.get(role)
                          else "rate_limited")
                lines.append(f"ts={ts} scope=action role={role} action={action} "
                             f"state=SKIPPED reason={reason} "
                             f"streak={self.limiter.streak(role)}")
                continue

            ctx = self._build_context(role, record, obs, cls, action)
            if ctx is None:
                lines.append(f"ts={ts} scope=action role={role} action={action} "
                             f"state=SKIPPED reason=profile_unreadable")
                continue
            planned.append((role, action, ctx))

        # BRAKE — shared-tap de-duplication (Air + VFO share idx -1 → :9998).
        kept, dropped = S.dedupe_tap_actions(planned)
        for role in dropped:
            lines.append(f"ts={ts} scope=action role={role} "
                         f"action=mount_404_with_healthy_backend state=SKIPPED "
                         f"reason=shared_tap_already_being_repaired")

        for role, action, ctx in kept:
            lines.extend(self._execute_action(ts, role, action, ctx))
        return lines

    def _execute_action(self, ts: str, role: str, action: str, ctx) -> List[str]:
        """Run one action inside the PID guard and the path auditor."""
        lines: List[str] = []
        mode = "DRYRUN" if ctx.dry_run else "ACT"
        trace: List[str] = []

        before = backends.process_pids()
        result = A.run_action(ctx, emit=trace.append)
        after = backends.process_pids()

        # BRAKE — did anything we are not allowed to touch move?
        moved = self.guard.compare(before, after)
        # BRAKE — did we call anything outside SDRangel's allowlist?
        violations = S.PathAuditor.audit(result.calls,
                                         result.base or S.EXPECTED_BASE)

        if moved or violations:
            msg = self.guard.trip(moved or [f"path:{v}" for v in violations])
            lines.append(f"ts={ts} scope=action role={role} action={action} "
                         f"state=EMERGENCY {msg} "
                         f"violations={';'.join(violations) or 'none'}")
            # An emergency is never counted as an ordinary failure — it must not
            # be quietly absorbed by the quarantine counter and retried.
            return lines

        self.actions_taken += 1
        self.limiter.record_action(role)
        if result.ok:
            self.failures.record_success(role, action)
            state = "OK"
        else:
            quarantined = self.failures.record_failure(role, action, result.detail)
            state = "QUARANTINED" if quarantined else "FAILED"

        lines.append(
            f"ts={ts} scope=action role={role} action={action} mode={mode} "
            f"state={state} detail={result.detail} calls={len(result.calls)} "
            f"fails={self.failures.failures(role, action)} "
            f"backoff={self.limiter.backoff_for(self.limiter.streak(role)):.0f}s")
        for t in trace:
            lines.append(f"ts={ts} scope=action role={role} action={action} "
                         f"trace={t.strip()}")
        return lines

    def _build_context(self, role: str, record: Dict, obs, cls, action: str):
        """Assemble the ActionContext, loading the profile from disk."""
        prof = self._load_profile(record)
        if prof is None and action != "unbound_device":
            # Only the run-it action can proceed without profile detail.
            return None
        audio_name, audio_index = "", 0
        if prof is not None:
            audio_name, audio_index = backends.resolve_audio_tap(prof.audio_strategy)
            if audio_name is None:
                audio_name, audio_index = "", 0
        return A.ActionContext(
            role=role,
            action=action,
            profile=prof,
            deviceset_index=obs.deviceset_index,
            mount=obs.want_mount,
            dry_run=self.rc.dry_run,
            missing_offsets=tuple(cls.missing_offsets),
            audio_name=audio_name or "",
            audio_index=audio_index if audio_index is not None else 0,
            udp_address=(prof.udp_address if prof else "127.0.0.1"),
            udp_port=(prof.udp_port if prof else 9998),
        )

    def _load_profile(self, record: Dict):
        try:
            from ..profile import load_profile
            from ..profilecmd import resolve_profile_path
            path = resolve_profile_path(str(record.get("name", "")))
            return load_profile(path) if path else None
        except Exception:
            return None

    def _reload_config(self) -> None:
        """Re-read config each pass so enable/disable lands within one interval."""
        if self._static_rc:
            return
        try:
            self.rc = CFG.load()
            self.limiter.base = self.rc.base_backoff
            self.limiter.cap = self.rc.max_backoff
            self.failures.threshold = self.rc.quarantine_threshold
            # Re-read quarantine so `resume-action` from the CLI lands here.
            self.failures.load()
        except Exception:
            pass    # keep the previous config rather than failing the pass

    def _backend_delta(self, loaded: Sequence[str]):
        """Which BACKEND agents vanished since the last pass. Read-only.

        Churn in the backend layer is the one thing a passive observer can spot
        that no per-role check would: SDRangel restarting under SB3 looks CLEAN
        on the very next pass, because the new process answers just fine.
        """
        live = {l for l in loaded if l in ownership.BACKEND}
        prev = self._last_backend_pids
        self._last_backend_pids = {l: "1" for l in live}
        if prev is None:
            return [], False
        vanished = sorted(set(prev) - live)
        return vanished, bool(vanished)

    # -- building the observation -----------------------------------------

    def build_role_observation(self, role: str, record: Dict,
                               devicesets: Sequence,
                               sdrangel_reachable: bool) -> C.RoleObservation:
        """Assemble one role's expected-vs-live picture. Read-only."""
        idx = int(record.get("deviceset_index", 0))
        mount = str(record.get("mount", ""))
        ds = next((d for d in devicesets if d.index == idx), None)

        expected = self._expected_channels(record)

        live_channels: List[C.LiveChannel] = []
        if ds is not None and not ds.is_phantom:
            for ch in backends.sdrangel_channels(idx):
                ci = ch.get("index")
                body = backends.channel_settings_body(
                    backends.sdrangel_channel_settings(idx, ci))
                live_channels.append(C.LiveChannel(
                    index=ci,
                    offset_hz=body.get("inputFrequencyOffset"),
                    squelch_db=body.get("squelch"),
                    volume=body.get("volume"),
                    rf_bw_hz=body.get("rfBandwidth"),
                ))

        m = backends.mount_state(mount) if mount else backends.MountState("", None, False)

        return C.RoleObservation(
            role=role,
            profile_name=str(record.get("name", "")),
            deviceset_index=idx,
            mode=str(record.get("mode", "")),
            want_serial=record.get("serial"),
            want_center_hz=record.get("center_freq_hz"),
            want_mount=mount,
            expected_channels=expected,
            sdrangel_reachable=sdrangel_reachable,
            ds_present=ds is not None,
            ds_phantom=bool(ds is not None and ds.is_phantom),
            ds_hw_type=(ds.hw_type if ds else None),
            ds_serial=(ds.serial if ds else None),
            ds_state=(ds.state if ds else None),
            ds_center_hz=(ds.center_hz if ds else None),
            live_channels=live_channels,
            mount_status=m.http_status,
            mount_present=m.present,
        )

    def _expected_channels(self, record: Dict) -> List[C.ExpectedChannel]:
        """Expected channels in APPLY order, from the profile file on disk.

        The loaded-profile record carries only channel FREQUENCIES, which is
        enough to count channels but not to tell a retuned squelch from a
        missing one.  The squelch/volume expectations live in the profile JSON,
        so read it — and degrade to frequency-only if it has since moved or
        changed, rather than refusing to classify at all.
        """
        center = record.get("center_freq_hz") or 0
        try:
            from ..profile import load_profile
            from ..profilecmd import resolve_profile_path
            path = resolve_profile_path(str(record.get("name", "")))
            if path:
                prof = load_profile(path)
                return [C.ExpectedChannel(
                    offset_hz=ch.freq_hz - prof.center_freq_hz,
                    squelch_db=ch.squelch_db,
                    volume=ch.volume,
                    rf_bw_hz=ch.rf_bw_hz,
                    keepalive=ch.keepalive,
                    title=ch.title,
                ) for ch in prof.channels_apply_order()]
        except Exception:
            pass    # fall through to the frequency-only view
        return [C.ExpectedChannel(offset_hz=int(f) - int(center),
                                  squelch_db=float("nan"), volume=float("nan"),
                                  rf_bw_hz=0)
                for f in (record.get("channel_freqs") or [])]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    # --once: run a single classification pass and print it. For deploy-time
    # verification without waiting a full poll interval, and for a human to see
    # exactly what the agent will be writing all night.
    if "--once" in argv:
        obs = Observer()
        for line in obs.classify_once():
            print(line)
        return 0
    return Observer().run()
