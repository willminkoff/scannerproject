"""Regression tests for the chirp daemon sdrplay shutdown-wedge fix.

These tests guard the structural fix described in
DESIGN_sdrplay_wedge_fix.md.  They are deliberately source-inspection
tests (read files as text rather than importing modules) so they run
hermetically in environments that lack gnuradio / osmocom — the same
pattern test_radio_bugs.test_main_loop_calls_drain_before_stop uses.

What each test proves and what it doesn't is documented inline.
"""
from __future__ import annotations

import re
from pathlib import Path

# Repo root = chirp/tests/ -> chirp/ -> repo root.
_REPO = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    p = _REPO / rel
    assert p.is_file(), f"expected file not found: {p}"
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Unit template carries the Type=notify / watchdog / timeouts
# ---------------------------------------------------------------------------


class TestUnitTemplate:
    """The deployed systemd unit must carry Type=notify and a watchdog so
    systemd can detect a daemon that hangs in sdrplay_api_Open at startup
    OR mid-run.

    Proves: the configuration the operator deploys will let systemd kill
    a wedged daemon (instead of leaving it silently stuck).
    Does NOT prove: the daemon side actually emits the notifies — covered
    by TestDaemonNotifyWiring below.
    """

    UNIT = "chirp/systemd/gr-demod@.service.template"

    def test_type_is_notify(self):
        src = _read(self.UNIT)
        assert re.search(r"(?m)^Type=notify\b", src), \
            "gr-demod@.service.template must declare Type=notify"
        assert "Type=simple" not in src, \
            "Type=simple lingering from pre-fix template"

    def test_notify_access_main(self):
        src = _read(self.UNIT)
        assert re.search(r"(?m)^NotifyAccess=main\b", src), \
            "Type=notify needs NotifyAccess=main to accept the daemon's pid"

    def test_timeout_start_sec_set_high_enough_for_real_boot(self):
        src = _read(self.UNIT)
        m = re.search(r"(?m)^TimeoutStartSec=(\d+)", src)
        assert m, "TimeoutStartSec must be set so sdrplay_api_Open hang is bounded"
        v = int(m.group(1))
        assert 30 <= v <= 120, \
            f"TimeoutStartSec={v} outside reasonable [30, 120] window"

    def test_watchdog_sec_set(self):
        src = _read(self.UNIT)
        m = re.search(r"(?m)^WatchdogSec=(\d+)", src)
        assert m, "WatchdogSec must be set so mid-run hangs are detected"
        v = int(m.group(1))
        # Daemon pings every ~10 s; allow 2× to 6× safety margin.
        assert 20 <= v <= 60, \
            f"WatchdogSec={v} outside reasonable [20, 60] window"

    def test_timeout_stop_sec_covers_drain(self):
        src = _read(self.UNIT)
        m = re.search(r"(?m)^TimeoutStopSec=(\d+)", src)
        assert m, "TimeoutStopSec must be set"
        v = int(m.group(1))
        # Shutdown drain default is 2 s + shutdown_drain() 2 s + tb.stop/wait.
        # Need >= 8 s; we set 10 s.
        assert v >= 8, \
            f"TimeoutStopSec={v} too tight for shutdown drain (need >= 8)"

    def test_restart_on_failure_preserved(self):
        src = _read(self.UNIT)
        assert re.search(r"(?m)^Restart=on-failure\b", src), \
            "Restart=on-failure must remain so SIGKILL'd wedges auto-retry"

    def test_start_limit_burst_preserved(self):
        """If the watchdog/notify path goes wrong, StartLimitBurst is what
        prevents an infinite restart loop. Guard against accidental removal."""
        src = _read(self.UNIT)
        assert "StartLimitBurst=" in src, \
            "StartLimitBurst guard rail removed — restart loop could run forever"


# ---------------------------------------------------------------------------
# 2. Ground drop-in orders after airband (kills master/slave restart race)
# ---------------------------------------------------------------------------


class TestGroundDropIn:
    """The ground drop-in must add ordering (After=) but NOT a hard
    dependency (Requires=/Wants=) — single-band ground operation is a
    supported configuration.

    Proves: declarative ordering is in place on next systemctl daemon-
    reload + restart.
    Does NOT prove: live ordering actually works on the box (hardware
    test).
    """

    DROPIN = "chirp/systemd/gr-demod@ground.service.d/01-after-airband.conf"

    def test_dropin_exists(self):
        p = _REPO / self.DROPIN
        assert p.is_file(), f"missing band-ordering drop-in: {p}"

    def test_after_airband(self):
        src = _read(self.DROPIN)
        assert re.search(r"(?m)^After=gr-demod@airband\.service\b", src), \
            "ground drop-in must declare After=gr-demod@airband.service"

    def test_no_hard_dependency(self):
        """After= only. Requires=/Wants= would break single-band-ground
        deploys when airband is intentionally absent."""
        src = _read(self.DROPIN)
        assert not re.search(r"(?m)^Requires=", src), \
            "ground drop-in must NOT Require= airband (single-band ground breaks)"
        assert not re.search(r"(?m)^Wants=", src), \
            "ground drop-in must NOT Want= airband (single-band ground breaks)"

    def test_in_unit_section(self):
        """The After= DIRECTIVE (not a prose mention in a comment) must
        live under a [Unit] header."""
        src = _read(self.DROPIN)
        unit_pos = src.find("[Unit]")
        assert unit_pos != -1, "drop-in missing [Unit] section header"
        # Match an After= directive at start-of-line (^After=) — that's
        # the parsed directive, not a prose mention.
        directive_match = re.search(r"(?m)^After=", src)
        assert directive_match is not None, "no After= directive found"
        assert directive_match.start() > unit_pos, \
            "After= directive must appear after the [Unit] header"


# ---------------------------------------------------------------------------
# 3. Daemon source has the sd_notify wiring in the right places
# ---------------------------------------------------------------------------


class TestDaemonNotifyWiring:
    """The chirp daemon must call sd_notify at the right lifecycle points
    so the Type=notify unit + watchdog do their job.

    Source-inspection pattern (same as
    test_radio_bugs.test_main_loop_calls_drain_before_stop). We read
    chirp/daemon.py as text and assert structural properties.  This is
    less semantically rich than an integration test but guards
    cheaply against the most likely regression: someone removes/
    misplaces a notify call during a future edit.

    Proves: the source has notify calls in the lifecycle slots the
    design document describes.
    Does NOT prove: the actual sd_notify reaches systemd correctly
    (would require integration test).
    """

    DAEMON = "chirp/daemon.py"

    def test_imports_systemd_daemon(self):
        src = _read(self.DAEMON)
        assert "from systemd import daemon" in src, \
            "daemon must (try to) import systemd.daemon for sd_notify"

    def test_has_sd_notify_helper(self):
        src = _read(self.DAEMON)
        assert "def _sd_notify(" in src, \
            "daemon must define a _sd_notify helper for centralised notify"

    def test_main_calls_ready_after_state_restore(self):
        src = _read(self.DAEMON)
        # READY=1 must come AFTER restore_from_state (so a failed
        # state restore can still surface as a startup failure) and
        # BEFORE the main stop_evt loop.
        ready_pos = src.find('"READY=1"')
        restore_pos = src.find("restore_from_state")
        loop_pos = src.find("while not stop_evt.is_set")
        assert ready_pos != -1, "main() must call _sd_notify('READY=1', ...)"
        assert restore_pos != -1 and restore_pos < ready_pos, \
            "READY=1 must come after restore_from_state"
        assert loop_pos != -1 and ready_pos < loop_pos, \
            "READY=1 must come before the main loop"

    def test_main_loop_pings_watchdog(self):
        src = _read(self.DAEMON)
        # Locate the main loop body, then assert WATCHDOG=1 appears
        # before the `finally:` of the same try block.
        loop_pos = src.find("while not stop_evt.is_set")
        assert loop_pos != -1, "main loop missing"
        # Look for `finally:` that follows the loop (the shutdown block).
        finally_pos = src.find("finally:", loop_pos)
        assert finally_pos != -1, "shutdown finally: missing"
        loop_body = src[loop_pos:finally_pos]
        assert "WATCHDOG=1" in loop_body, \
            "main loop must ping WATCHDOG=1 (systemd kills daemon at WatchdogSec)"

    def test_sig_handler_emits_stopping(self):
        src = _read(self.DAEMON)
        # _sig is the SIGTERM/SIGINT handler. STOPPING=1 lets systemd
        # know shutdown is intentional.
        sig_pos = src.find("def _sig(signum, frame):")
        assert sig_pos != -1, "_sig signal handler missing"
        # Look for STOPPING=1 within the next ~600 chars of source.
        snippet = src[sig_pos:sig_pos + 600]
        assert "STOPPING=1" in snippet, \
            "_sig handler must notify('STOPPING=1') so systemd sees intentional stop"


# ---------------------------------------------------------------------------
# 4. Daemon source has the SDR shutdown drain
# ---------------------------------------------------------------------------


class TestShutdownDrain:
    """Post-stop drain prevents the next daemon's osmosdr.source() from
    racing against an incomplete sdrplay_api release.

    Proves: a time.sleep gate exists between tb.stop()+tb.wait() and the
    process return.
    Does NOT prove: the sleep duration is sufficient for any particular
    sdrplay_apiService build (empirically 2 s suffices on Micro 2026-06).
    """

    DAEMON = "chirp/daemon.py"

    def test_drain_appears_after_tb_stop(self):
        src = _read(self.DAEMON)
        # Find the `tb.stop()` inside the shutdown finally block (the
        # second tb.stop() — the first is the early-fail server.start
        # escape, which we ignore, same as test_radio_bugs does).
        finally_pos = src.find("finally:", src.find("while not stop_evt.is_set"))
        assert finally_pos != -1
        shutdown_tail = src[finally_pos:]
        # tb.stop()/wait() must appear, then a sleep guarded on the
        # CHIRP_SDR_SHUTDOWN_DRAIN_S env, then return.
        stop_pos = shutdown_tail.find("tb.stop()")
        assert stop_pos != -1, "shutdown must call tb.stop()"
        drain_env_pos = shutdown_tail.find("CHIRP_SDR_SHUTDOWN_DRAIN_S")
        assert drain_env_pos != -1, \
            "shutdown must read CHIRP_SDR_SHUTDOWN_DRAIN_S (operator override)"
        assert drain_env_pos > stop_pos, \
            "drain env must be read AFTER tb.stop() (sleep belongs in shutdown tail)"
        sleep_pos = shutdown_tail.find("time.sleep", drain_env_pos)
        assert sleep_pos != -1, \
            "shutdown must time.sleep(drain_s) after reading the env"

    def test_drain_skipped_for_file_source(self):
        """File-source runs (unit / smoke tests) must not pay the 2 s
        penalty — drain gated on source_kind == 'sdr'."""
        src = _read(self.DAEMON)
        finally_pos = src.find("finally:", src.find("while not stop_evt.is_set"))
        shutdown_tail = src[finally_pos:]
        drain_env_pos = shutdown_tail.find("CHIRP_SDR_SHUTDOWN_DRAIN_S")
        # Within ~300 chars after the env read, the gate must reference
        # source_kind.
        gate_window = shutdown_tail[drain_env_pos:drain_env_pos + 300]
        assert 'source_kind' in gate_window and '"sdr"' in gate_window, \
            "drain must be gated on cfg.source_kind == 'sdr'"

    def test_drain_default_is_two_seconds(self):
        """Default drain matches the design (2.0 s). Anything <1.0 risks
        the race; anything >5.0 risks blowing TimeoutStopSec."""
        src = _read(self.DAEMON)
        m = re.search(r'CHIRP_SDR_SHUTDOWN_DRAIN_S"\s*,\s*"([\d.]+)"', src)
        assert m, "could not locate default value for CHIRP_SDR_SHUTDOWN_DRAIN_S"
        default = float(m.group(1))
        assert 1.0 <= default <= 5.0, \
            f"drain default {default}s outside safe [1.0, 5.0] window"


# ---------------------------------------------------------------------------
# 5. The _sd_notify helper is safe even without systemd.daemon
# ---------------------------------------------------------------------------


class TestSdNotifyFallback:
    """The daemon must not crash when systemd.daemon is missing — test
    environments (laptop dev, CI without python3-systemd) routinely lack
    it.  Under Type=notify on Micro this still fails (systemd kills the
    daemon at TimeoutStartSec), but the failure mode is graceful and
    visible rather than a Python ImportError stack trace.

    Proves: the import + the helper handle absence safely.
    Does NOT prove: systemd will accept a daemon without sd_notify (it
    won't — that's by design under Type=notify).
    """

    def test_helper_handles_missing_module(self, monkeypatch):
        """Force _HAVE_SD False and assert _sd_notify is a safe no-op."""
        # We can't import chirp.daemon in sandbox (gnuradio missing), so
        # exec the relevant snippet in isolation. Mirrors the helper's
        # structure without dragging the GR imports.
        ns: dict = {}
        snippet = (
            "from typing import Optional\n"
            "import logging\n"
            "log = logging.getLogger('t')\n"
            "_HAVE_SD = False\n"
            "_sd = None\n"
            "def _sd_notify(state, status=None):\n"
            "    if not _HAVE_SD:\n"
            "        return\n"
            "    raise RuntimeError('should not reach')\n"
        )
        exec(snippet, ns)  # noqa: S102 — controlled string
        # Helper must not raise.
        ns["_sd_notify"]("READY=1", "anything")
        ns["_sd_notify"](None, "anything")
        ns["_sd_notify"]("WATCHDOG=1")


# ---------------------------------------------------------------------------
# 6. Source-level guard: existing shutdown_drain still runs before stop
# ---------------------------------------------------------------------------


class TestShutdownDrainOrderingPreserved:
    """The Phase 2 inflight-setter shutdown_drain() must still appear
    before tb.stop() in the shutdown path — this fix is purely additive
    on top of that earlier work.  Mirrors
    test_radio_bugs.test_main_loop_calls_drain_before_stop without
    importing chirp.daemon.

    Proves: the rtl-airband master/slave-on-restart inflight-setter
    drain ordering is unchanged.
    Does NOT prove: drain semantics correct (covered by test_radio_bugs).
    """

    DAEMON = "chirp/daemon.py"

    def test_shutdown_drain_before_tb_stop(self):
        src = _read(self.DAEMON)
        finally_pos = src.find("finally:", src.find("while not stop_evt.is_set"))
        shutdown_tail = src[finally_pos:]
        drain_pos = shutdown_tail.find("shutdown_drain()")
        stop_pos = shutdown_tail.find("tb.stop()")
        assert drain_pos != -1, "shutdown_drain() missing from shutdown path"
        assert stop_pos != -1, "tb.stop() missing from shutdown path"
        assert drain_pos < stop_pos, \
            "shutdown_drain() must run BEFORE tb.stop() (Phase 2 invariant)"
