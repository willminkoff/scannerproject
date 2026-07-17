"""sb3.agents.controller_stub — placeholder for the SB3 controller/reconciler.

Runs as `com.scannerproject.sb3-controller`. Logs to
~/Library/Logs/sb3/controller.log.

Phase 2+ replaces this with the reconciler that translates profiles onto
SDRangel/SDRTrunk. Two constraints it will inherit, worth stating now:

  * It is the FIRST thing `kill` stops (§4.3 step 2) — stop asserting before
    anything else moves.
  * On resume it must ADOPT live backend state, never replay a snapshot, and on
    divergence the LIVE backend wins (§4.4). A human may have retuned SDRangel
    by hand while SB3 was gone; that is intent, not drift. This is deliberately
    the inverse of sdrangel-restore.py's 10-minute re-assert.
"""

from __future__ import annotations

import sys

from ._stub import Stub


def main() -> int:
    return Stub("controller").run()


if __name__ == "__main__":
    sys.exit(main())
