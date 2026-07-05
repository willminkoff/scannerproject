"""broker.__main__ — daemon entrypoint: ``python -m broker``.

Loads the fleet policy (``--policy`` > ``$SCANNER_FLEET_POLICY`` >
``/opt/scannerproject/etc/sdr_fleet_policy.json``), then serves the
tuner-broker until SIGTERM/SIGINT.

Exit codes:
    0   clean shutdown (signal)
    1   startup failure that is NOT a policy problem (e.g. another live
        broker already owns the socket — two arbiters would both hand out
        'exclusive' leases, so we refuse)
    3   PolicyError — the fleet policy is missing/malformed.  Matches
        chirp's config hard-fail convention.  A broker running on a guessed
        policy would LOOK like arbitration while enforcing nothing (the
        exact "useful liar" shape of SB6's silent-empty exclusion
        resolver), so the only correct move is to not run.

Under launchd (etc/mac/launchd/com.scannerproject.tuner-broker.plist) the
agent has ``KeepAlive`` with a throttle, so a transient failure retries and
a persistent exit-3 shows up plainly in /opt/scannerproject/log/tuner-broker.log.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys

from broker.policy import DEFAULT_POLICY_PATH, POLICY_ENV_VAR, PolicyError, load_policy
from broker.server import BrokerAlreadyRunning, BrokerServer

EXIT_STARTUP_FAILURE = 1
EXIT_POLICY_ERROR = 3

log = logging.getLogger("broker")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m broker",
        description="scannerproject tuner-broker: single-host SDR device-ownership arbiter",
    )
    parser.add_argument(
        "--policy",
        default=None,
        help=f"fleet policy path (default: ${POLICY_ENV_VAR} or {DEFAULT_POLICY_PATH})",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    try:
        policy = load_policy(args.policy)
    except PolicyError as exc:
        # Structured diagnostic on stderr, then the hard-fail exit.  No
        # degraded "broker without a policy" mode exists on purpose.
        print(json.dumps(exc.to_dict()), file=sys.stderr)
        log.error("fleet policy hard-fail: %s", exc)
        return EXIT_POLICY_ERROR

    server = BrokerServer(policy)

    def _stop(signum, _frame):
        log.info("signal %d: shutting down", signum)
        server.shutdown()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    try:
        server.serve_forever()
    except BrokerAlreadyRunning as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_STARTUP_FAILURE
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
