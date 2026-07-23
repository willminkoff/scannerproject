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

from . import install as install_mod
from . import killswitch
from . import update as update_mod


def build_parser() -> argparse.ArgumentParser:
    # --execute is accepted in BOTH positions — `sb3-ctl --execute kill` and
    # `sb3-ctl kill --execute`. It is the natural thing to type after the
    # subcommand, and the earlier "top-level only" form rejected exactly that
    # with an unhelpful "unrecognized arguments" error. A shared parent parser
    # puts the flag on the main parser AND every subparser; argparse merges the
    # two into one `args.execute`.
    common = argparse.ArgumentParser(add_help=False)
    # default=SUPPRESS is load-bearing: without it, the subparser's copy of
    # --execute defaults to False and OVERWRITES a True set at the top level, so
    # `sb3-ctl --execute kill` silently becomes execute=False. SUPPRESS means an
    # absent flag adds no attribute, so whichever position sets it wins and the
    # other does not clobber. main() reads it with getattr(..., False).
    common.add_argument("--execute", action="store_true",
                        default=argparse.SUPPRESS,
                        help="perform the action for real (default: dry-run)")

    p = argparse.ArgumentParser(
        prog="sb3-ctl",
        parents=[common],
        description="SB3 control plane. `kill` removes SB3; SDRangel and "
                    "SDRTrunk keep producing audio.",
        epilog="NOTE: this is NOT macos/killswitch/sdr-killswitch, whose `kill` "
               "means the opposite (it hands radios TO SDRangel). Here, "
               "SDRangel surviving is the invariant.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", parents=[common],
                   help="observe SB3 + backend state (read-only)")
    sub.add_parser("kill", parents=[common],
                   help="tear down the SB3 layer")
    sub.add_parser("resume", parents=[common],
                   help="bring SB3 back, adopting live backend state")
    sub.add_parser("update", parents=[common],
                   help="git-pull the deploy checkout and bounce agents onto it")
    pp = sub.add_parser("profile", parents=[common],
                        help="load / status / unload an SB3 profile")
    pp.add_argument("action", choices=["load", "status", "unload"])
    pp.add_argument("name", help="profile name (e.g. air.airband.nashville) or path")
    sub.add_parser("diff", parents=[common],
                   help="what would resume change? (not implemented)")
    ap = sub.add_parser("apply", parents=[common],
                        help="apply a profile (not implemented)")
    ap.add_argument("profile")
    rp = sub.add_parser("reconciler", parents=[common],
                        help="Phase 4.2: enable/disable the ACTIVE reconciler")
    rp.add_argument("action",
                    choices=["status", "enable", "disable", "dry-run",
                             "passive", "unpassive", "resume-action"],
                    help="status | enable | disable | dry-run | passive "
                         "(revert to 4.1 observe-only) | unpassive | "
                         "resume-action <role> <action>")
    rp.add_argument("role", nargs="?", help="for resume-action")
    rp.add_argument("act", nargs="?", help="for resume-action")
    sub.add_parser("install", parents=[common],
                   help="write the SB3 launchd plists")
    sub.add_parser("uninstall", parents=[common],
                   help="remove the SB3 launchd plists")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    execute = getattr(args, "execute", False)

    if args.cmd == "status":
        return killswitch.cmd_status()
    if args.cmd == "kill":
        return killswitch.cmd_kill(execute=execute)
    if args.cmd == "resume":
        return killswitch.cmd_resume(execute=execute)
    if args.cmd == "update":
        return update_mod.cmd_update(execute=execute)
    if args.cmd == "profile":
        from . import profilecmd
        return profilecmd.run(args.action, args.name, execute=execute)
    if args.cmd == "reconciler":
        from . import reconcilercmd
        return reconcilercmd.run(args.action, role=args.role, action_name=args.act,
                                 execute=execute)
    if args.cmd == "install":
        return install_mod.cmd_install(execute=execute)
    if args.cmd == "uninstall":
        return install_mod.cmd_uninstall(execute=execute)
    if args.cmd in ("diff", "apply"):
        print(f"sb3-ctl {args.cmd}: not implemented at Phase 1 (§6).")
        return killswitch.EXIT_NOT_IMPLEMENTED
    return killswitch.EXIT_NOT_IMPLEMENTED


if __name__ == "__main__":
    sys.exit(main())
