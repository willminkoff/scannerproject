"""sb3.agents.broker_stub — placeholder for the SB3 tuner-broker.

Runs as `com.scannerproject.sb3-broker`. Logs to ~/Library/Logs/sb3/broker.log.

Phase 2+ replaces this with the real broker (`python3 -m broker`), which already
exists and is unchanged by this plan (§8). Until then this only holds the
launchd slot so `kill`'s teardown ordering has a real process to stop.

Ordering note (§4.3): the broker is the LAST thing `kill` stops. Lease consumers
go first — each runs under `broker.client run … -- <proc>` and the lease IS the
open socket, so killing the broker first would yank that socket out from under a
live child.
"""

from __future__ import annotations

import sys

from ._stub import Stub


def main() -> int:
    return Stub("broker").run()


if __name__ == "__main__":
    sys.exit(main())
