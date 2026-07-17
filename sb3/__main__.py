"""sb3.__main__ — `python3 -m sb3` / `bin/sb3-ctl`.

Command surface (§4.3):

    sb3-ctl status              what SB3 thinks; what the backends report; the diff
    sb3-ctl kill                full SB3 teardown. Backends untouched. Audio continues.
    sb3-ctl resume              bring SB3 back; adopt LIVE backend state
    sb3-ctl diff                dry run: what would resume change?
    sb3-ctl apply <profile>     one-shot translate+verify (works while killed)

Phase 1 implements `status` for real and `kill` as dry-run only. `--execute` is
parsed and refused; Phase 1.1 enables it after review.

Exit codes:
    0  ok
    1  invariant violated (a guarded mount that was live is no longer live)
    2  not implemented at this phase
    3  refused (e.g. --execute before Phase 1.1)
"""

from __future__ import annotations

import argparse
import sys

from . import killswitch


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sb3-ctl",
        description="SB3 control plane. `kill` removes SB3; SDRangel and "
                    "SDRTrunk keep producing audio.",
        epilog="NOTE: this is NOT macos/killswitch/sdr-killswitch, whose `kill` "
               "means the opposite (it hands radios TO SDRangel). Here, "
               "SDRangel surviving is the invariant.",
    )
    p.add_argument("--execute", action="store_true",
                   help="perform actions for real (REFUSED until Phase 1.1)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="observe SB3 + backend state (read-only)")
    sub.add_parser("kill", help="tear down the SB3 layer (dry-run in Phase 1)")
    sub.add_parser("resume", help="bring SB3 back, adopting live backend state")
    sub.add_parser("diff", help="what would resume change? (not implemented)")
    ap = sub.add_parser("apply", help="apply a profile (not implemented)")
    ap.add_argument("profile")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "status":
        return killswitch.cmd_status()
    if args.cmd == "kill":
        return killswitch.cmd_kill(execute=args.execute)
    if args.cmd == "resume":
        return killswitch.cmd_resume(execute=args.execute)
    if args.cmd in ("diff", "apply"):
        print(f"sb3-ctl {args.cmd}: not implemented at Phase 1 (§6).")
        return killswitch.EXIT_NOT_IMPLEMENTED
    return killswitch.EXIT_NOT_IMPLEMENTED


if __name__ == "__main__":
    sys.exit(main())
