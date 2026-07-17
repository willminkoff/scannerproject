"""sb3.agents._stub — the shared idle-loop body for the Phase 1 agent stubs.

These stubs exist to prove ONE thing: that the launchd lifecycle SB3's kill
switch depends on actually works on Neptune — bootstrap, run under KeepAlive,
take a SIGTERM from ``launchctl bootout``, and exit cleanly without launchd
respawning them or leaving a zombie.

They do no SDR work, hold no leases, and open no devices. Phase 2+ replaces the
bodies with real logic; the lifecycle contract stays.

**Why a stub at all.** Phase 1's acceptance criterion is "the SB3 controller can
start/stop without breaking the current stack", and ``--execute`` cannot be
meaningfully enabled — or reviewed — against an empty SB3 layer. A kill switch
first exercised against real, load-bearing processes is a kill switch tested in
production. These give it something harmless to kill.

**The SIGTERM contract, and why it is written down.** Every agent here must exit
*cleanly and promptly* on SIGTERM. That is not politeness:

  * ``launchctl bootout`` SIGTERMs and does not wait — §4.3's teardown ordering
    assumes each agent is gone before the next step, and an agent that ignores
    SIGTERM turns a clean teardown into a timeout.
  * The habit matters more than these stubs do. The real consumers wrap
    ``broker.client run``, where the lease IS the open socket, so a clean exit is
    what releases the device (§4.3 step 3). An agent that dies dirty leaks a
    lease.
  * And the surrounding hardware is unforgiving about exactly this: SIGKILL to
    SDRTrunk dirty-releases the RSPduo, which then drops off the USB bus and
    needs a REBOOT (§5.4 #1). SB3 never stops SDRTrunk — but the discipline is
    the same one, and it is cheaper to build it into the stubs than to retrofit
    it into the real thing.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

LOG_DIR = Path(os.environ.get(
    "SB3_LOG_DIR", os.path.expanduser("~/Library/Logs/sb3")))

HEARTBEAT_SEC = float(os.environ.get("SB3_STUB_HEARTBEAT_SEC", "30"))

#: Poll interval for the shutdown flag. Deliberately much shorter than the
#: heartbeat: a SIGTERM must be acted on in well under a second, not slept
#: through for up to 30s. `launchctl bootout` does not wait around.
_TICK_SEC = 0.25


class Stub:
    """An idle-loop agent that logs a heartbeat and exits cleanly on SIGTERM."""

    def __init__(self, name: str, log_dir: Optional[Path] = None) -> None:
        self.name = name
        self.log_dir = Path(log_dir) if log_dir else LOG_DIR
        self._stop = False
        self.log = logging.getLogger(f"sb3.{name}")

    # -- lifecycle ---------------------------------------------------------

    def _configure_logging(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(self.log_dir / f"{self.name}.log")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"))
        self.log.addHandler(handler)
        self.log.addHandler(logging.StreamHandler(sys.stdout))
        self.log.setLevel(logging.INFO)

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._handle_signal)

    def _handle_signal(self, signum, _frame) -> None:
        # Set a flag and return. Do NOT do teardown work in the handler — it
        # runs on the main thread between bytecodes and anything non-trivial
        # here is how you get a half-exited process that launchd then has to
        # SIGKILL, which is the exact ending this contract exists to avoid.
        self._stop = True
        self.log.info("received %s — shutting down cleanly",
                      signal.Signals(signum).name)

    def run(self) -> int:
        self._configure_logging()
        self._install_signal_handlers()
        self.log.info("%s stub up (pid %d) — Phase 1 placeholder, no SDR work, "
                      "no leases held", self.name, os.getpid())

        last_beat = 0.0
        while not self._stop:
            now = time.monotonic()
            if now - last_beat >= HEARTBEAT_SEC:
                self.log.info("heartbeat — idle (%s)", self.name)
                last_beat = now
            time.sleep(_TICK_SEC)

        self.log.info("%s stub exiting 0 — clean SIGTERM shutdown", self.name)
        for h in list(self.log.handlers):
            h.flush()
            h.close()
            self.log.removeHandler(h)
        return 0
